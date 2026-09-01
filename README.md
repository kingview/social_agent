# Social Agent

`Social Agent` 是一个可单独启动的本地桌面 Agent，也提供 Python Runtime。它通过会话式自然语言把任务编排为现有 Tool 调用，不直接实现平台爬取或下载逻辑。

当前支持：

- 对话输入可以选择或拖入图片、视频和音频；允许只有附件、不选择浏览器会话的本地任务。
- 图片通过 Harness `ImageBlock` 和 `dsh-attachment-local` 原生进入模型与持久会话。
- 视频和音频由媒体 Tool 提取 OCR、关键画面理解、ASR/字幕和结构化摘要，再写入同一个 Harness 会话。
- 同一 GUI 会话内持续复用 Harness 进程与 session；后续的“根据刚才图片/视频”等指令可使用原生历史与压缩上下文。
- 使用 PostDrop 注册的抖音、小红书、X / Twitter、Telegram Web `session_ref`。
- 关键词搜索、用户主页、推荐流/时间线、指定页面。
- 只获取帖子 URL，或继续批量下载图片/视频。
- 常规任务单次浏览最多 100 条；下载固定拆成每批最多 20 个 URL。
- Telegram 指定频道/群组支持向上遍历历史消息，保存图片、视频和随附文本；“全部/所有/全量”任务由下载 Tool 在一次调用内确定性执行，并通过本地 manifest 断点去重。
- 默认跨批次总下载预算为 5 GB，不会把每批上限无限累加。
- 会话中继续说“改成前50条”等方式调整上一个计划。
- 输入任务后点击一次“发送”，Agent 会在后台生成计划并直接执行；公开发布等外部写操作仍使用单独的一次性授权。
- 当前批次完成后停止、实时进度、结果数量和下载目录。
- 可在任务中明确要求“有水印就去水印”；Agent 会调用独立的 `media.process_watermark`，原视频始终保留。
- 复杂任务由 DeepSeek Harness 动态规划：下载后分析、标签/摘要、按结果继续筛选、生成本地文案草稿。
- 明确要求“发布到 X”时，可在确认高风险计划后通过已登录的比特浏览器自动发布一条 X 帖子；不使用 X 官方 API。
- Harness 通过 MCP 只获得已安装且启用的白名单 Tool；没有 Shell、文件编辑或自动登录能力。
- 可通过 `browser_operate` 在指定 `session_ref` 的比特浏览器窗口中观察页面、打开 HTTPS 页面、点击、输入搜索词、按键、滚动、上划、下划和翻页。

示例：

```text
通过关键词“web3”在抖音上搜索并下载前100个帖子
```

需要水印处理时：

```text
通过关键词“web3”在抖音搜索并下载前100个帖子，有水印就去水印
```

Telegram 频道全量下载示例：

```text
下载 https://t.me/weme_download 频道的所有图片、视频和随附文本
```

该任务复用已登录 Telegram Web 的比特浏览器窗口及其代理，不导出登录凭据；Agent 对频道
地址调用一次 `download_media(telegram_scope="channel")`。Tool 持续写入检查点，重复执行会
跳过已完成消息；默认最多 2000 条、总下载预算 5GB，并返回完成状态与停止原因。

完整处理并发布到 X：

```text
搜索 X 上的“web3”视频帖子，下载前20条，去除高置信度水印，分析并生成一条中文文案，发布到 X
```

该任务会在计划卡片和独立确认框中明确提示外部发布；一次确认只允许发布一条，结果不明时不会自动重试。

对应的确定性执行图：

```text
自然语言
→ AgentPlan（平台、session_ref、搜索条件、数量、是否下载）
→ 用户确认
→ social.browse_posts（最多 100 条）
→ social.download_media（每批最多 20 条，共最多 5 批）
→ AgentExecutionResult
```

动态执行图：

```text
自然语言 + 图片 / 视频 / 音频
→ 全局策略拒绝检查
→ 图片：Harness Attachment Store + ImageBlock
→ 视频/音频：媒体 Tool 输入归一化 → 结构化媒体上下文
→ 无 Tool 的 Harness Planning Cordis
→ DynamicAgentPlan
→ 用户确认
→ Harness Execution Cordis
→ MCP 插件桥（通用浏览器操作 / 帖子浏览 / 下载 / 分析 / 去水印 / 文案草稿 / 一次性 X 发布）
→ AgentExecutionResult
```

GUI 不再直接选择或实例化具体 Runtime：

```text
GUI Worker
→ RuntimeRouter
   ├── DeterministicAgentRuntime
   └── DeepSeekHarnessRuntime
→ ExecutionPolicy
→ AgentExecutionResult
```

`ExecutionPolicy` 统一拥有浏览数量、每批 URL、总下载容量和 Tool 调用预算；两个 Runtime 和 MCP Server 不能各自放宽这些限制。

当前锁定 npm 已发布的 DeepSeek Harness `0.1.1-rc.2`。图片链路使用 Harness 官方
`AttachmentStore`、`admitEncodedImages` 和 `ImageBlock`；本仓库仅补充一个与上游
`0.1.2-alpha.1` SDK 内联图片准入等价的薄 JSON-RPC 入口。上游 alpha 正式发布并完成
兼容测试后即可删除该入口。Harness 仍处于 Developer Preview，因此通过独立
JSON-RPC/MCP Adapter 隔离，后续升级不会改变 Python Tool 契约。

Harness 当前原生附件只覆盖 PNG、JPEG、WebP、GIF 图片，没有 `AudioBlock` 或
`VideoBlock`。视频/音频不是伪装成原生附件，而是先由媒体插件分析，结果作为明确标注的
结构化上下文进入当前 Harness session。

## 本地运行

macOS：

```bash
brew install node@24
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,build]'
./scripts/install_harness.sh
.venv/bin/social-ops-agent
```

Windows PowerShell：

```powershell
# 先安装 Node.js 24 LTS
py -m venv .venv
.venv\Scripts\pip install -e ".[dev,build]"
.\scripts\install_harness.ps1
.venv\Scripts\social-ops-agent.exe
```

常规搜索下载优先使用确定性中文解析；无法固定解析的表达，以及包含分析、筛选、总结、文案等动态任务，使用 OpenAI-compatible 模型端点。GUI 顶部的 **LLM** 按钮可以在以下来源间切换，保存后立即应用到 Harness 规划、执行、多媒体预分析和 Tool 插件：

- 本地 Ollama；默认 `qwen3.5:9b`。
- OpenAI API；填写 API Key 和模型 ID。
- 其他 OpenAI-compatible 服务，例如 LiteLLM、vLLM 或 SGLang。

远端 API Key 不写入 JSON 或日志，而是存放在 macOS 钥匙串或 Windows 凭据管理器。OpenAI API 需要 [OpenAI Platform API Key](https://platform.openai.com/api-keys)，不能直接复用 ChatGPT 网页登录或订阅。模型 ID 可手动填写，也可以点击“测试并读取模型”从端点的 `/models` 接口载入。

无人值守部署仍可使用环境变量；它们优先于桌面设置：

```text
SOCIAL_AGENT_LLM_BASE_URL=http://127.0.0.1:11434/v1
SOCIAL_AGENT_LLM_MODEL=qwen3.5:9b
SOCIAL_AGENT_LLM_API_KEY=local-model
```

旧的 `SOCIAL_AGENT_OLLAMA_BASE_URL`、`SOCIAL_AGENT_OLLAMA_MODEL`、`SOCIAL_AGENT_OLLAMA_API_KEY` 仍兼容，但通用变量优先。没有环境变量时，非敏感设置保存在用户级 `llm-settings.json`，密钥始终与该文件分离。

模型只能提出白名单计划。最终计划仍必须经过 Pydantic 契约、平台会话匹配、数量上限和用户确认；确认前的 Harness Runtime 完全不加载 Tool。
要使用原生图片理解，配置的模型端点必须真正支持 OpenAI-compatible 图片消息；默认
`qwen3.5:9b` 路由在 Harness 中声明为 `text + image`。

Harness MCP 可以单独启动，供兼容 MCP 的 Agent Runtime 使用：

```bash
SOCIAL_AGENT_SESSION_REGISTRY=/path/to/sessions.json \
SOCIAL_AGENT_OUTPUT_ROOT=/path/to/output \
SOCIAL_AGENT_STATE_ROOT=/path/to/state \
.venv/bin/social-agent-mcp
```

MCP 保留七个标准具名 Tool，并增加 `list_plugin_tools` 与 `call_plugin_tool`，使后续插件无需修改 Agent 核心即可暴露新能力。`list_plugin_tools` 默认从插件的实际 MCP 服务读取 description、input schema 和 output schema；`plugin.json` 只维护允许暴露的 Tool 名称，避免两份 schema 漂移。

## Tool 插件

Agent App 不再静态打包爬虫、Playwright、OpenCV、ONNX、FFmpeg 或视频修复模型。当前两个插件是：

- `social-content.socialtool`：帖子浏览、比特浏览器操作、媒体下载和经确认的 X 发布。
- `media-content.socialtool`：媒体分析、水印处理和本地文案生成。

生成安装包：

```bash
./scripts/build_plugins.sh
```

Windows：

```powershell
.\scripts\build_plugins.ps1
```

输出位于 `dist/plugins/`。可以在 Agent GUI 顶部点击“Tool 插件”安装、启用、禁用或卸载，也可以使用 CLI：

```bash
social-agent-plugin install dist/plugins/social-content.socialtool
social-agent-plugin list
social-agent-plugin disable com.socialagent.media-content
social-agent-plugin uninstall com.socialagent.media-content
```

插件安装到用户数据目录，不写入 `.app`：

- macOS：`~/Library/Application Support/SocialAgent/plugins`
- Windows：`%LOCALAPPDATA%\SocialAgent\plugins`

每个插件仍拥有独立 Python 环境和进程隔离，但 Agent 内的 Plugin Host 会按需启动并常驻复用 MCP 进程；连续步骤不会反复加载 Python、模型和浏览器依赖。禁用、升级或卸载插件会立即关闭对应进程。

安装包会记录每个 wheel 的 SHA-256，安装前验证文件集合和摘要；包内多个 wheel 会全部安装。构建时还会生成当前平台和 Python ABI 专用的 hash-pinned 依赖锁（例如 `requirements-macos-arm64-cp312.lock`），安装时由 pip 的 `--require-hashes` 强制执行；正式发布应在 macOS/Windows 各自构建对应锁。安装完成后，插件环境中内容完全相同的依赖文件会硬链接到用户级共享包存储 `SocialAgent/runtimes/package-store-v1`，保留每个插件独立的依赖版本视图，同时避免 Qt、MCP 等相同文件重复占用磁盘。大型修复模型仍在首次使用时下载到共享模型缓存。安装依赖需要联网；打包 App 会优先选择与插件锁匹配的 Python。媒体 OCR/语音插件目前推荐 Python 3.12（Paddle 尚不支持本机 Python 3.14），也可以设置 `SOCIAL_AGENT_PLUGIN_PYTHON` 明确指定。

## 构建桌面 App

构建脚本只把 Agent GUI、插件桥、锁定的 Harness 运行依赖和 Node 放入 App；各 Tool 单独安装：

```bash
./scripts/build_macos.sh
./scripts/package_macos.sh
```

```powershell
.\scripts\build_windows.ps1
```

macOS 默认使用 `/opt/homebrew/opt/node@24/bin/node`；可用 `SOCIAL_AGENT_NODE` 指定要随包携带的 Node 22.19+/24 可执行文件。
`package_macos.sh` 会生成同时包含 `SocialAgent.app` 和当前 `.socialtool` 安装包的 `dist/SocialAgent-macOS-arm64.zip`。

## 安全边界

- Agent 不支持自动登录、点赞、评论、关注、私信或转发；当前唯一平台写操作是经确认后发布一条 X 帖子。
- `browser_operate` 仍只允许搜索、浏览和翻页；密码/文件输入控件以及发布、互动、交易、删除类点击会被 Tool 拒绝。发布只能调用独立 `publish_x_post`。
- `publish_x_post` 要求 X 专属 `session_ref`、GUI 已确认的动态计划和只在本次执行有效的一次性令牌；令牌在浏览器操作前消费，成功、失败或结果不明均不会自动重试。
- 发布媒体最多 4 个，且必须来自 Social Agent 输出目录，防止模型上传任意本地文件；发布 Tool 的审计摘要不记录令牌。
- `session_ref` 只映射用户已手动登录的比特浏览器 Profile；不向模型发送 Cookie、密码、代理或指纹参数。
- Profile 的代理和指纹在比特浏览器中预先配置，Agent 不轮换或修改它们。
- 使用 `session_ref` 下载时，媒体请求会优先复用该 Profile 的 HTTP/HTTPS/SOCKS5 代理；Profile 为 `noproxy` 时使用本机网络直接下载，结果会明确标记实际路由。
- 页面内容是不可信数据，不作为 Agent 指令执行。
- 其他平台写操作在进入固定规划器或 Harness 之前统一拒绝，不能通过复杂表述绕过；X 发布意图则强制进入 Harness 高风险计划。
- 规划 Cordis 没有 Tool；执行 Cordis 只加载 `mcp__social__*`，禁用 Bash、Jobs、Skills 和工作区上下文。
- 只应访问和保存有权使用的内容，并遵守平台规则与适用法律。

## Harness 与 LangGraph

当前常规下载路径保留有限状态 Runtime，非固定路径使用 DeepSeek Harness。领域层不依赖 Harness 类型以外的内部事件；未来需要服务端复杂图或多 Agent 复核时，可在相同 Runtime Adapter 后增加 LangGraph，而不改动 MCP/Pydantic Tool 契约。

Harness 通过 npm 安装模块化运行组件，不把完整 GitHub 源码 vendoring 到本仓库。`harness/package.json` 与 `package-lock.json` 锁定版本，`npm ci` 负责可重复安装；只有要修改 Harness 内核时才应维护独立 Fork 或 Git submodule。
