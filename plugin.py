from pydantic import Field

from nekro_agent.api.plugin import ConfigBase, NekroPlugin

plugin = NekroPlugin(
    name="MiMo 语音合成",
    module_name="mimo_tts",
    description="调用 MiMo TTS 接口合成语音，并发送到当前会话。",
    version="0.1.0",
    author="AzurLane",
    url="https://api.xiaomimimo.com/",
    support_adapter=["onebot_v11"],
    allow_sleep=True,
    sleep_brief="用于将文本合成为语音并发送成语音消息，仅在需要语音回复时激活。",
)


@plugin.mount_config()
class MiMoTTSConfig(ConfigBase):
    MIMO_API_KEY: str = Field(
        default="",
        title="MiMo API Key",
        description="小米 MiMo 平台的 API Key。",
    )
    BASE_URL: str = Field(
        default="https://api.xiaomimimo.com/v1",
        title="API Base URL",
        description="MiMo OpenAI 兼容接口基础地址，默认带 /v1。",
    )
    MODEL: str = Field(
        default="mimo-v2-tts",
        title="模型",
        description="当前默认支持 mimo-v2-tts。",
    )
    DEFAULT_VOICE: str = Field(
        default="mimo_default",
        title="默认音色",
        description="默认音色，可选 mimo_default/default_zh/default_en。",
    )
    DEFAULT_STYLE: str = Field(
        default="",
        title="默认风格",
        description="可选。会拼接为 <style>风格</style> 放在待合成文本前。",
    )
    DEFAULT_USER_MESSAGE: str = Field(
        default="",
        title="默认用户提示",
        description="可选。会作为 user 角色消息发送，帮助调整语气与风格。",
    )
    AUDIO_FORMAT: str = Field(
        default="wav",
        title="输出格式",
        description="建议使用 wav，便于直接发送为语音消息。",
    )
    REQUEST_TIMEOUT: int = Field(
        default=90,
        title="超时秒数",
        description="合成请求超时时间。",
    )
    ENABLE_PROXY_ACCESS: bool = Field(
        default=False,
        title="启用代理访问",
        description="启用后通过系统默认代理访问 MiMo API。",
    )


config = plugin.get_config(MiMoTTSConfig)
