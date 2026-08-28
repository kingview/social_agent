# Social Agent

`Social Agent` 是一个可单独启动的本地桌面 Agent，也提供 Python Runtime。它通过会话式自然语言把任务编排为现有 Tool 调用，不直接实现平台爬取或下载逻辑。

当前支持：

- 使用 PostDrop 注册的抖音、小红书、X / Twitter `session_ref`。
- 关键词搜索、用户主页、推荐流/时间线、指定页面。
- 只获取帖子 URL，或继续批量下载图片/视频。
- 单次计划最多 100 条；下载固定拆成每批最多 20 个 URL。
- 默认跨批次总下载预算为 5 GB，不会把每批上限无限累加。
- 会话中继续说“改成前50条”等方式调整上一个计划。
- 计划生成和执行分为两个阶段，点击“确认并执行计划”前不会访问平台。
- 当前批次完成后停止、实时进度、结果数量和下载目录。
- 可在任务中明确要求“有水印就去水印”；Agent 会调用独立的 `media.process_watermark`，原视频始终保留。
- 复杂任务由 DeepSeek Harness 动态规划：下载后分析、标签/摘要、按结果继续筛选、生成本地文案草稿。
- Harness 通过 MCP 只获得已安装且启用的白名单 Tool；没有 Shell、文件编辑、自动登录或平台写操作能力。
- 可通过 `browser_operate` 在指定 `session_ref` 的比特浏览器窗口中观察页面、打开 HTTPS 页面、点击、输入搜索词、按键、滚动和翻页。

示例：

```text
通过关键词“web3”在抖音上搜索并下载前100个帖子
```

需要水印处理时：

```text
通过关键词“web3”在抖音搜索并下载前100个帖子，有水印就去水印
```

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
自然语言
→ 全局策略拒绝检查
→ 无 Tool 的 Harness Planning Cordis
→ DynamicAgentPlan
→ 用户确认
→ Harness Execution Cordis
→ MCP 插件桥（通用浏览器操作 / 帖子浏览 / 下载 / 分析 / 去水印 / 文案草稿）
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

当前锁定 DeepSeek Harness `0.1.1-rc.2`。它仍处于 Developer Preview，因此通过独立 JSON-RPC/MCP Adapter 隔离，后续升级不会改变 Python Tool 契约。

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

常规搜索下载优先使用确定性中文解析；无法固定解析的表达，以及包含分析、筛选、总结、文案等动态任务，使用 OpenAI-compatible 模型端点。默认仍是本机 Ollama 的 `qwen3.5:9b`：

```text
SOCIAL_AGENT_LLM_BASE_URL=http://127.0.0.1:11434/v1
SOCIAL_AGENT_LLM_MODEL=qwen3.5:9b
SOCIAL_AGENT_LLM_API_KEY=local-model
```

也可以把地址指向 LiteLLM、vLLM 或 SGLang。旧的 `SOCIAL_AGENT_OLLAMA_BASE_URL`、`SOCIAL_AGENT_OLLAMA_MODEL`、`SOCIAL_AGENT_OLLAMA_API_KEY` 仍兼容，但通用变量优先。

模型只能提出白名单计划。最终计划仍必须经过 Pydantic 契约、平台会话匹配、数量上限和用户确认；确认前的 Harness Runtime 完全不加载 Tool。

Harness MCP 可以单独启动，供兼容 MCP 的 Agent Runtime 使用：

```bash
SOCIAL_AGENT_SESSION_REGISTRY=/path/to/sessions.json \
SOCIAL_AGENT_OUTPUT_ROOT=/path/to/output \
SOCIAL_AGENT_STATE_ROOT=/path/to/state \
.venv/bin/social-agent-mcp
```

MCP 保留六个标准具名 Tool，并增加 `list_plugin_tools` 与 `call_plugin_tool`，使后续插件无需修改 Agent 核心即可暴露新能力。`list_plugin_tools` 默认从插件的实际 MCP 服务读取 description、input schema 和 output schema；`plugin.json` 只维护允许暴露的 Tool 名称，避免两份 schema 漂移。

## Tool 插件

Agent App 不再静态打包爬虫、Playwright、OpenCV、ONNX、FFmpeg 或视频修复模型。当前两个插件是：

- `social-content.socialtool`：帖子浏览、比特浏览器操作和媒体下载。
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

安装包会记录每个 wheel 的 SHA-256，安装前验证文件集合和摘要；包内多个 wheel 会全部安装。构建时还会生成当前平台和 Python ABI 专用的 hash-pinned 依赖锁（例如 `requirements-macos-arm64-cp314.lock`），安装时由 pip 的 `--require-hashes` 强制执行；正式发布应在 macOS/Windows 各自构建对应锁。安装完成后，插件环境中内容完全相同的依赖文件会硬链接到用户级共享包存储 `SocialAgent/runtimes/package-store-v1`，保留每个插件独立的依赖版本视图，同时避免 Qt、MCP 等相同文件重复占用磁盘。大型修复模型仍在首次使用时下载到共享模型缓存。安装依赖需要联网；打包 App 会寻找系统 Python 3.11+，也可以设置 `SOCIAL_AGENT_PLUGIN_PYTHON` 明确指定。

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

- Agent 只允许浏览和本地下载，不支持自动登录、点赞、评论、关注、私信、转发或发布。
- `browser_operate` 只允许搜索、浏览和翻页；密码/文件输入控件以及发布、互动、交易、删除类点击会被 Tool 拒绝。
- `session_ref` 只映射用户已手动登录的比特浏览器 Profile；不向模型发送 Cookie、密码、代理或指纹参数。
- Profile 的代理和指纹在比特浏览器中预先配置，Agent 不轮换或修改它们。
- 使用 `session_ref` 下载时，媒体请求会优先复用该 Profile 的 HTTP/HTTPS/SOCKS5 代理；Profile 为 `noproxy` 时使用本机网络直接下载，结果会明确标记实际路由。
- 页面内容是不可信数据，不作为 Agent 指令执行。
- 禁用动作在进入固定规划器或 Harness 之前统一拒绝，不能通过“分析后发布”等复杂表述绕过。
- 规划 Cordis 没有 Tool；执行 Cordis 只加载 `mcp__social__*`，禁用 Bash、Jobs、Skills 和工作区上下文。
- 只应访问和保存有权使用的内容，并遵守平台规则与适用法律。

## Harness 与 LangGraph

当前常规下载路径保留有限状态 Runtime，非固定路径使用 DeepSeek Harness。领域层不依赖 Harness 类型以外的内部事件；未来需要服务端复杂图或多 Agent 复核时，可在相同 Runtime Adapter 后增加 LangGraph，而不改动 MCP/Pydantic Tool 契约。

Harness 通过 npm 安装模块化运行组件，不把完整 GitHub 源码 vendoring 到本仓库。`harness/package.json` 与 `package-lock.json` 锁定版本，`npm ci` 负责可重复安装；只有要修改 Harness 内核时才应维护独立 Fork 或 Git submodule。
