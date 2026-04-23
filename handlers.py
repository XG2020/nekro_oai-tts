import base64
import time
from pathlib import Path
from typing import Annotated

import httpx
from nonebot.adapters.onebot.v11 import MessageSegment

from nekro_agent.api import core
from nekro_agent.api.plugin import (
    Arg,
    CmdCtl,
    CommandExecutionContext,
    CommandPermission,
    CommandResponse,
    SandboxMethodType,
)
from nekro_agent.api.schemas import AgentCtx

from .plugin import config, plugin


def _voice_hint_text() -> str:
    return (config.VOICE_OPTIONS_HINT or "").strip()


def _audio_tag_examples_text() -> str:
    return (config.AUDIO_TAG_EXAMPLES or "").strip()


def _get_proxy() -> str | None:
    if not config.ENABLE_PROXY_ACCESS:
        return None
    proxy = core.config.DEFAULT_PROXY
    if not proxy:
        return None
    if isinstance(proxy, str) and proxy.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return proxy
    return f"http://{proxy}"


def _normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("MiMo API 地址不能为空。")
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")]
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def _build_messages(text: str, user_message: str = "") -> list[dict[str, str]]:
    assistant_text = text.strip()
    if not assistant_text:
        raise ValueError("待合成文本不能为空。")

    messages: list[dict[str, str]] = []
    effective_user_message = user_message.strip()
    if effective_user_message:
        messages.append({"role": "user", "content": effective_user_message})
    messages.append({"role": "assistant", "content": assistant_text})
    return messages


def _extract_audio_bytes(payload: dict) -> bytes:
    try:
        audio_b64 = payload["choices"][0]["message"]["audio"]["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"MiMo 返回内容中缺少音频数据: {payload}") from exc
    try:
        return base64.b64decode(audio_b64)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("MiMo 返回的音频数据不是合法的 base64。") from exc


def _parse_chat_target(chat_key: str) -> tuple[str, str]:
    if "-" in chat_key:
        _, target = chat_key.split("-", 1)
    else:
        target = chat_key
    if "_" not in target:
        raise ValueError(f"无法解析 chat_key: {chat_key}")
    chat_type, chat_id = target.rsplit("_", 1)
    return chat_type, chat_id


async def _synthesize_audio(text: str, voice: str = "", user_message: str = "") -> bytes:
    api_key = (config.MIMO_API_KEY or "").strip()
    if not api_key:
        raise ValueError("请先在插件配置中填写 MIMO_API_KEY。")

    model = (config.MODEL or "mimo-v2.5-tts").strip()
    base_url = _normalize_base_url(config.BASE_URL)
    endpoint = f"{base_url}/chat/completions"
    
    audio_payload = {"format": (config.AUDIO_FORMAT or "wav").strip()}
    
    final_user_message = user_message or config.DEFAULT_USER_MESSAGE or ""

    if model == "mimo-v2.5-tts-voiceclone":
        clone_source = (config.VOICE_CLONE_SOURCE or "").strip()
        if not clone_source:
            raise ValueError("请先在插件配置中填写 VOICE_CLONE_SOURCE。")
        
        if not clone_source.startswith("data:audio/"):
            raise ValueError("VOICE_CLONE_SOURCE 格式不正确，应为 'data:audio/...' 格式的 Data URI。")

        audio_payload["voice"] = clone_source
        final_user_message = ""  # VoiceClone 不支持 user message

    elif model == "mimo-v2.5-tts-voicedesign":
        if not final_user_message:
             raise ValueError("使用 mimo-v2.5-tts-voicedesign 模型时，必须提供 user_message 用于描述音色。")
        # VoiceDesign 不需要 voice id

    else:  # 默认为 mimo-v2.5-tts
        effective_voice = (voice or config.DEFAULT_VOICE or "").strip()
        if not effective_voice:
            raise ValueError("请先配置默认音色，或在调用时显式传入 voice。")
        audio_payload["voice"] = effective_voice

    payload = {
        "model": model,
        "messages": _build_messages(
            text=text,
            user_message=final_user_message,
        ),
        "audio": audio_payload,
    }
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=max(int(config.REQUEST_TIMEOUT), 10), proxy=_get_proxy()) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        return _extract_audio_bytes(data)


async def _write_audio_file(_ctx: AgentCtx, audio_bytes: bytes) -> Path:
    suffix = (config.AUDIO_FORMAT or "wav").strip().lower() or "wav"
    filename = f"mimo_tts_{int(time.time() * 1000)}.{suffix}"
    target = _ctx.fs.shared_path / filename
    target.write_bytes(audio_bytes)
    return target


async def _send_onebot_voice(_ctx: AgentCtx, audio_bytes: bytes, audio_path: Path) -> None:
    bot = await _ctx.get_onebot_v11_bot()
    chat_type, chat_id = _parse_chat_target(_ctx.chat_key)
    record_segment = MessageSegment.record(file=f"base64://{base64.b64encode(audio_bytes).decode('utf-8')}")

    try:
        if "group" in chat_type:
            await bot.send_group_msg(group_id=int(chat_id), message=record_segment)
        else:
            await bot.send_private_msg(user_id=int(chat_id), message=record_segment)
        return
    except Exception as exc:  # noqa: BLE001
        plugin.logger.warning(f"OneBot base64 语音发送失败，尝试文件 URI 回退: {exc}")

    fallback_segment = MessageSegment.record(file=audio_path.as_uri())
    if "group" in chat_type:
        await bot.send_group_msg(group_id=int(chat_id), message=fallback_segment)
    else:
        await bot.send_private_msg(user_id=int(chat_id), message=fallback_segment)


async def _send_audio_message(_ctx: AgentCtx, audio_bytes: bytes) -> str:
    if _ctx.adapter_key != "onebot_v11":
        raise RuntimeError(f"MiMo TTS 当前仅支持 onebot_v11，当前适配器为: {_ctx.adapter_key}")

    audio_path = await _write_audio_file(_ctx, audio_bytes)
    await _send_onebot_voice(_ctx, audio_bytes, audio_path)
    return str(audio_path)


async def _run_tts_and_send(
    _ctx: AgentCtx,
    content: str,
    voice: str = "",
    user_message: str = "",
) -> str:
    text = (content or "").strip()
    if not text:
        raise ValueError("待合成文本不能为空。")

    audio_bytes = await _synthesize_audio(
        text=text,
        voice=voice,
        user_message=user_message,
    )
    sent_path = await _send_audio_message(_ctx, audio_bytes)
    
    model = (config.MODEL or "mimo-v2.5-tts").strip()
    if model == "mimo-v2.5-tts-voiceclone":
        display_voice = "克隆音色"
    elif model == "mimo-v2.5-tts-voicedesign":
        display_voice = "设计音色"
    else:
        display_voice = voice or config.DEFAULT_VOICE

    plugin.logger.info(f"[{_ctx.chat_key}] MiMo 语音发送成功: model={model}, voice={display_voice}, path={sent_path}")
    return sent_path


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="发送MiMo语音",
    description="调用 MiMo TTS 把文本合成为语音，并发送到当前会话。支持 AI 自行切换音色，并在正文中直接编写细粒度音频标签。",
)
async def send_mimo_voice(
    _ctx: AgentCtx,
    content: str,
    voice: str = "",
    user_message: str = "",
) -> str:
    """发送 MiMo 语音消息。

    Args:
        content (str): 要合成的正文内容。可以直接包含细粒度音频标签，例如 [小声]、[沉默片刻]、[咳嗽]、[提高音量喊话] 等。
        voice (str): 可选音色，留空则使用插件默认音色。可用音色见插件帮助。
        user_message (str): 用于辅助调整语气。使用 mimo-v2.5-tts 时为风格描述，使用 mimo-v2.5-tts-voicedesign 时为音色描述。

    Returns:
        str: 发送结果说明。

    Example:
        send_mimo_voice(content="今天辛苦啦，早点休息。", voice="冰糖")
        send_mimo_voice(content="[紧张][深呼吸]呼……冷静，冷静。", voice="苏打")
        send_mimo_voice(content="这个新功能太棒了！", user_message="用活泼的少女音说")
    """

    sent_path = await _run_tts_and_send(
        _ctx,
        content=content,
        voice=voice,
        user_message=user_message,
    )
    return f"已发送 MiMo 语音消息，音频文件路径: {sent_path}"


@plugin.mount_prompt_inject_method(name="mimo_tts_prompt")
async def mimo_tts_prompt(_ctx: AgentCtx) -> str:
    voice_hint = _voice_hint_text() or "冰糖, 茉莉, 苏打, 白桦, Mia, Chloe, Milo, Dean"
    audio_tag_examples = _audio_tag_examples_text()

    return (
        "你可以在需要“读出来”“配音”“语音回复”“情绪化朗读”时主动调用 `发送MiMo语音`。\n"
        "使用规则：\n"
        "1. 你可以根据场景自行选择 `voice`，而不是固定使用默认音色。\n"
        "2. 你可以直接把细粒度音频标签写进 `content` 正文，标签会原样参与语音合成。\n"
        "3. 你可以通过 `user_message` 传递自然语言指令来控制语音风格。\n"
        "4. 如果用户强调情绪、演绎、停顿、呼吸、耳语、喊话、咳嗽等表现，请优先在 `content` 中加入音频标签。\n\n"
        f"可用音色参考：\n{voice_hint}\n\n"
        f"细粒度音频标签示例：\n{audio_tag_examples}"
    )


@plugin.mount_command(
    name="mimo_tts_speak",
    description="将文本合成为 MiMo 语音并发送到当前会话",
    aliases=["mimo-tts-speak"],
    usage="mimo_tts_speak 文本",
    permission=CommandPermission.USER,
    category="语音",
)
async def mimo_tts_speak_cmd(
    context: CommandExecutionContext,
    args_str: Annotated[str, Arg("参数", positional=True, greedy=True)] = "",
) -> CommandResponse:
    raw = (args_str or "").strip()
    if not raw:
        return CmdCtl.failed("用法: /mimo_tts_speak 文本")

    ctx = await AgentCtx.create_by_chat_key(context.chat_key)
    try:
        await _run_tts_and_send(
            ctx,
            content=raw,
        )
    except Exception as exc:  # noqa: BLE001
        plugin.logger.exception(f"[{context.chat_key}] MiMo 命令发送失败: {exc}")
        return CmdCtl.failed(f"MiMo 语音发送失败: {exc}")

    model = (config.MODEL or "mimo-v2.5-tts").strip()
    if model == "mimo-v2.5-tts-voiceclone":
        display_voice = "克隆音色"
    elif model == "mimo-v2.5-tts-voicedesign":
        display_voice = "设计音色"
    else:
        display_voice = config.DEFAULT_VOICE

    return CmdCtl.success(f"MiMo 语音已发送，模型: {model}, 音色: {display_voice}")


@plugin.mount_command(
    name="mimo_tts_help",
    description="查看 MiMo TTS 插件帮助",
    aliases=["mimo-tts-help"],
    usage="mimo_tts_help",
    permission=CommandPermission.USER,
    category="语音",
)
async def mimo_tts_help_cmd(context: CommandExecutionContext) -> CommandResponse:
    return CmdCtl.success(
        "MiMo TTS 插件 (v2.5) 用法:\n"
        "/mimo_tts_speak [文本] - 使用配置的默认模型和音色合成语音。\n"
        "/mimo_tts_speak [小声][温柔]明天见 - 使用音频标签控制语音。\n\n"
        "AI 能力:\n"
        "- AI 可以根据配置的 `MODEL` 选择合成方式。\n"
        "- `mimo-v2.5-tts`: 使用 `voice` 参数选择预置音色，或用 `user_message` 控制风格。\n"
        "- `mimo-v2.5-tts-voicedesign`: 使用 `user_message` 描述想要的音色。\n"
        "- `mimo-v2.5-tts-voiceclone`: 使用 `VOICE_CLONE_SOURCE` 配置的音频 Data URI 克隆音色。\n"
        "- AI 可以在 `content` 中使用音频标签进行细粒度控制。\n\n"
        "主要配置项:\n"
        "- `MODEL`: 使用的模型 (mimo-v2.5-tts, mimo-v2.5-tts-voicedesign, mimo-v2.5-tts-voiceclone)。\n"
        "- `DEFAULT_VOICE`: 预置音色模型的默认音色 (如: 冰糖)。\n"
        "- `VOICE_CLONE_SOURCE`: 音色克隆用的音频 Data URI。\n"
        "- `DEFAULT_USER_MESSAGE`: `mimo-v2.5-tts` 模型的默认风格指令。\n"
        f"可用预置音色:\n{_voice_hint_text()}"
    )
