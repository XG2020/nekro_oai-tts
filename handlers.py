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


def _build_messages(text: str, style: str = "", user_message: str = "") -> list[dict[str, str]]:
    assistant_text = text.strip()
    if not assistant_text:
        raise ValueError("待合成文本不能为空。")
    effective_style = style.strip()
    if effective_style:
        assistant_text = f"<style>{effective_style}</style>{assistant_text}"

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


async def _synthesize_audio(text: str, style: str = "", voice: str = "", user_message: str = "") -> bytes:
    api_key = (config.MIMO_API_KEY or "").strip()
    if not api_key:
        raise ValueError("请先在插件配置中填写 MIMO_API_KEY。")

    effective_voice = (voice or config.DEFAULT_VOICE or "").strip()
    if not effective_voice:
        raise ValueError("请先配置默认音色，或在调用时显式传入 voice。")

    base_url = _normalize_base_url(config.BASE_URL)
    endpoint = f"{base_url}/chat/completions"
    payload = {
        "model": (config.MODEL or "mimo-v2-tts").strip(),
        "messages": _build_messages(
            text=text,
            style=style or config.DEFAULT_STYLE,
            user_message=user_message or config.DEFAULT_USER_MESSAGE,
        ),
        "audio": {
            "format": (config.AUDIO_FORMAT or "wav").strip(),
            "voice": effective_voice,
        },
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
    style: str = "",
    voice: str = "",
    user_message: str = "",
) -> str:
    text = (content or "").strip()
    if not text:
        raise ValueError("待合成文本不能为空。")

    audio_bytes = await _synthesize_audio(
        text=text,
        style=style,
        voice=voice,
        user_message=user_message,
    )
    sent_path = await _send_audio_message(_ctx, audio_bytes)
    plugin.logger.info(f"[{_ctx.chat_key}] MiMo 语音发送成功: voice={voice or config.DEFAULT_VOICE}, path={sent_path}")
    return sent_path


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="发送MiMo语音",
    description="调用 MiMo TTS 把文本合成为语音，并发送到当前会话。",
)
async def send_mimo_voice(
    _ctx: AgentCtx,
    content: str,
    style: str = "",
    voice: str = "",
    user_message: str = "",
) -> str:
    """发送 MiMo 语音消息。

    Args:
        content (str): 要合成的正文内容。
        style (str): 可选语音风格，会拼接为 <style>风格</style> 前缀。
        voice (str): 可选音色，留空则使用插件默认音色。
        user_message (str): 可选 user 角色消息，用于辅助调整语气。

    Returns:
        str: 发送结果说明。

    Example:
        send_mimo_voice(content="今天辛苦啦，早点休息。")
        send_mimo_voice(content="明天就是周五了", style="开心", voice="default_zh")
    """

    sent_path = await _run_tts_and_send(
        _ctx,
        content=content,
        style=style,
        voice=voice,
        user_message=user_message,
    )
    return f"已发送 MiMo 语音消息，音频文件路径: {sent_path}"


@plugin.mount_command(
    name="mimo_tts_speak",
    description="将文本合成为 MiMo 语音并发送到当前会话",
    aliases=["mimo-tts-speak"],
    usage="mimo_tts_speak [风格|]文本",
    permission=CommandPermission.USER,
    category="语音",
)
async def mimo_tts_speak_cmd(
    context: CommandExecutionContext,
    args_str: Annotated[str, Arg("参数", positional=True, greedy=True)] = "",
) -> CommandResponse:
    raw = (args_str or "").strip()
    if not raw:
        return CmdCtl.failed("用法: mimo_tts_speak [风格|]文本")

    if "|" in raw:
        style, content = raw.split("|", 1)
    else:
        style, content = "", raw

    ctx = await AgentCtx.create_by_chat_key(context.chat_key)
    try:
        await _run_tts_and_send(
            ctx,
            content=content,
            style=style,
        )
    except Exception as exc:  # noqa: BLE001
        plugin.logger.exception(f"[{context.chat_key}] MiMo 命令发送失败: {exc}")
        return CmdCtl.failed(f"MiMo 语音发送失败: {exc}")

    effective_voice = (config.DEFAULT_VOICE or "").strip() or "未配置"
    if style.strip():
        return CmdCtl.success(f"MiMo 语音已发送，音色: {effective_voice}，风格: {style.strip()}")
    return CmdCtl.success(f"MiMo 语音已发送，音色: {effective_voice}")


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
        "MiMo TTS 插件用法:\n"
        "/mimo_tts_speak 文本\n"
        "/mimo_tts_speak 开心|明天就是周五了\n\n"
        "主要配置项:\n"
        "- MIMO_API_KEY: MiMo 平台 API Key\n"
        "- DEFAULT_VOICE: 默认音色，如 mimo_default/default_zh/default_en\n"
        "- DEFAULT_STYLE: 默认风格，可留空\n"
        "- DEFAULT_USER_MESSAGE: 可选 user 角色提示\n"
        "- AUDIO_FORMAT: 建议使用 wav"
    )
