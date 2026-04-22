# MiMo TTS 插件

基于小米 MiMo OpenAI 兼容接口的 NekroAgent 语音合成插件。

它会把文本请求发到 MiMo TTS 接口，拿到音频后直接以 QQ 语音消息形式发回当前会话。

## 文件结构

```text
mimo_tts/
├── __init__.py
├── plugin.py
├── handlers.py
└── README.md
```

## 功能

- 提供一个 AI 可调用的行为方法 `发送MiMo语音`
- 提供测试命令 `/mimo_tts_speak [风格|]文本`
- 支持默认音色、默认风格、默认 user 提示
- 支持系统代理访问 MiMo API

## 配置项

- `MIMO_API_KEY`: MiMo 平台 API Key
- `BASE_URL`: API 基础地址，默认 `https://api.xiaomimimo.com/v1`
- `MODEL`: 默认 `mimo-v2-tts`
- `DEFAULT_VOICE`: 默认音色，默认 `mimo_default`
- `DEFAULT_STYLE`: 默认风格，自动转成 `<style>...</style>`
- `DEFAULT_USER_MESSAGE`: 可选的 user 角色提示
- `AUDIO_FORMAT`: 默认 `wav`
- `REQUEST_TIMEOUT`: 请求超时秒数
- `ENABLE_PROXY_ACCESS`: 是否通过系统代理访问

## 命令

- `/mimo_tts_speak 文本`
- `/mimo_tts_speak 开心|明天就是周五了`
- `/mimo_tts_help`

## AI 方法

方法名：`发送MiMo语音`

参数：

- `content`: 必填，待合成文本
- `style`: 可选，风格
- `voice`: 可选，音色
- `user_message`: 可选，额外 user 角色提示

## 使用说明

1. 把 `mimo_tts` 目录放到 NekroAgent 的 `plugins/workdir/` 下。
2. 在插件配置中填写 `MIMO_API_KEY`。
3. 重新加载插件。
4. 使用 `/mimo_tts_speak 你好呀` 测试发送。

## 说明

- MiMo 接口要求待合成文本放在 `assistant` 角色消息里，这个插件已经按该要求构造请求。
- 为了更稳地发送成语音消息，默认输出格式使用 `wav`。
- 当前实现仅面向 `onebot_v11`，不再考虑其他适配器。
