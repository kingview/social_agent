from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .contracts import AgentAttachment, AgentProgress, AttachmentModality, DynamicAgentPlan
from .harness_client import HarnessError, HarnessJsonRpcClient
from .planner import SelectedSession
from .plugins import default_plugin_root
from .policy import DEFAULT_EXECUTION_POLICY, ExecutionPolicy, requested_write_actions
from .settings import LLMSettings


HARNESS_VERSION = "0.1.1-rc.2 + native-image compatibility"
class HarnessExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: DynamicAgentPlan
    response: str
    session_id: str
    tool_calls: list[str]
    finish_reason: str | None = None
    cancelled: bool = False


class DeepSeekHarnessBackend:
    def __init__(
        self,
        *,
        registry_path: Path,
        output_root: Path,
        conversation_id: str,
        project_root: Path | None = None,
        settings: LLMSettings | None = None,
        policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    ) -> None:
        self.project_root = (project_root or _project_root()).resolve()
        self.registry_path = registry_path.expanduser().resolve()
        self.output_root = output_root.expanduser().resolve()
        self.conversation_id = conversation_id
        self.settings = settings or LLMSettings.from_env()
        self.policy = policy
        self.state_root = self.output_root / ".social-agent-state"
        self.session_root = self.state_root / "harness-sessions"
        self._clients: dict[Literal["plan", "execute"], HarnessJsonRpcClient] = {}
        self._active_client: HarnessJsonRpcClient | None = None
        self._execute_generation = 0

    @staticmethod
    def is_available(project_root: Path | None = None) -> tuple[bool, str]:
        root = (project_root or _project_root()).resolve()
        try:
            node = _node_executable()
        except HarnessError as exc:
            return False, str(exc)
        runtime = _runtime_script(root)
        if not runtime.is_file():
            return False, "Harness 依赖尚未安装，请运行 scripts/install_harness.sh。"
        return True, f"DeepSeek Harness {HARNESS_VERSION} · {node}"

    def propose(
        self,
        message: str,
        session: SelectedSession | None,
        *,
        attachments: tuple[AgentAttachment, ...] = (),
        media_context: str | None = None,
        context_summary: str | None = None,
    ) -> DynamicAgentPlan:
        self.policy.validate_message(message)
        client = self._start_client(mode="plan")
        self._active_client = client
        plan_session_id = f"{self.conversation_id}-plan"
        try:
            prompt = _planning_prompt(
                message,
                session,
                attachments=attachments,
                media_context=media_context,
                context_summary=context_summary,
            )
            first_turn = client.run_turn(
                session_id=plan_session_id,
                content_blocks=_content_blocks(prompt, attachments),
            )
            try:
                return _validated_dynamic_plan(
                    _json_object(first_turn.final_response),
                    message=message,
                    session=session,
                    attachments=attachments,
                    media_context=media_context,
                    max_tool_calls=self.policy.max_tool_calls,
                )
            except (HarnessError, ValidationError):
                repair_turn = client.run_turn(
                    session_id=plan_session_id,
                    prompt=_planning_repair_prompt(
                        message,
                        session,
                        first_turn.final_response,
                    ),
                )
                return _validated_dynamic_plan(
                    _json_object(repair_turn.final_response),
                    message=message,
                    session=session,
                    attachments=attachments,
                    media_context=media_context,
                    max_tool_calls=self.policy.max_tool_calls,
                )
        finally:
            self._active_client = None

    def health(self) -> tuple[bool, str]:
        available, harness_detail = self.is_available(self.project_root)
        if not available:
            return False, harness_detail
        model_available, model_detail = self.settings.health()
        return model_available, f"{harness_detail}；{model_detail}"

    def execute(
        self,
        plan: DynamicAgentPlan,
        *,
        progress: Callable[[AgentProgress], None] | None = None,
    ) -> HarnessExecutionResult:
        self.policy.validate_plan(plan)
        notify = progress or (lambda _event: None)
        notify(AgentProgress(stage="harness", completed=0, total=1, message="Harness 正在规划并调用 Tools…"))
        write_actions = tuple(plan.write_actions)
        approval_token = secrets.token_urlsafe(32) if write_actions == ("publish_x",) else None
        if approval_token:
            # Each external write gets a fresh MCP process and an ephemeral one-time grant.
            self._drop_client("execute")
        client = self._start_client(
            mode="execute",
            publish_approval_token=approval_token,
        )
        self._active_client = client
        tool_count = 0

        def on_event(event: dict[str, Any]) -> None:
            nonlocal tool_count
            event_type = event.get("type")
            data = event.get("data")
            if event_type == "tool/call" and isinstance(data, dict):
                tool_count += 1
                if tool_count > min(plan.max_tool_calls, self.policy.max_tool_calls):
                    client.cancel()
                    raise HarnessError(
                        f"Harness exceeded the approved Tool call budget ({plan.max_tool_calls})."
                    )
                name = str(data.get("name") or "Tool")
                notify(
                    AgentProgress(
                        stage="tool",
                        completed=0,
                        total=1,
                        message=f"Harness 正在调用 {name}（第 {tool_count} 次）…",
                    )
                )
            elif event_type == "tool/result":
                notify(
                    AgentProgress(
                        stage="tool",
                        completed=0,
                        total=1,
                        message=f"第 {tool_count} 次 Tool 调用完成，Harness 正在决定下一步…",
                    )
                )

        try:
            turn = client.run_turn(
                session_id=f"{self.conversation_id}-execute-{self._execute_generation}",
                content_blocks=_content_blocks(
                    _execution_prompt(
                        plan,
                        self.policy,
                        publish_approval_token=approval_token,
                    ),
                    tuple(plan.attachments),
                ),
                on_event=on_event,
            )
            notify(AgentProgress(stage="done", completed=1, total=1, message="Harness 动态任务执行完成。"))
            return HarnessExecutionResult(
                plan=plan,
                response=turn.final_response,
                session_id=turn.session_id,
                tool_calls=turn.tool_calls,
                finish_reason=turn.finish_reason,
            )
        finally:
            self._active_client = None
            if approval_token:
                # Revoke the grant even if Harness never called the publishing Tool.
                self._drop_client("execute")

    def cancel(self) -> None:
        if self._active_client is not None:
            active = self._active_client
            active.cancel()
            for mode, client in list(self._clients.items()):
                if client is active:
                    self._clients.pop(mode, None)
                    if mode == "execute":
                        self._execute_generation += 1
                    break
            self._active_client = None

    def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        self._active_client = None
        for client in clients:
            client.close()

    def _start_client(
        self,
        *,
        mode: Literal["plan", "execute"],
        publish_approval_token: str | None = None,
    ) -> HarnessJsonRpcClient:
        existing = self._clients.get(mode)
        if existing is not None:
            return existing
        available, reason = self.is_available(self.project_root)
        if not available:
            raise HarnessError(reason)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.session_root.mkdir(parents=True, exist_ok=True)
        config = self.project_root / "harness" / f"cordis-{mode}.yml"
        env = {
            "DSH_CORDIS_CONFIG": str(config),
            "DSH_HOME": str(self.state_root / "harness-home"),
            "DSH_SESSION_ROOT": str(self.session_root / mode),
            "DSH_SYSTEM_PROMPT": _planning_persona() if mode == "plan" else _execution_persona(),
            "SOCIAL_AGENT_LLM_API_KEY": self.settings.api_key,
            "SOCIAL_AGENT_LLM_BASE_URL": self.settings.base_url,
            "SOCIAL_AGENT_LLM_MODEL": self.settings.model,
            "SOCIAL_AGENT_PYTHON": sys.executable,
            "SOCIAL_AGENT_MCP_ARGS": json.dumps(
                ["--mcp-server"]
                if getattr(sys, "frozen", False)
                else ["-m", "social_ops_agent.mcp_server"]
            ),
            "SOCIAL_AGENT_PROJECT_ROOT": str(self.project_root),
            "SOCIAL_AGENT_SESSION_REGISTRY": str(self.registry_path),
            "SOCIAL_AGENT_OUTPUT_ROOT": str(self.output_root),
            "SOCIAL_AGENT_STATE_ROOT": str(self.state_root),
            "SOCIAL_AGENT_PLUGIN_ROOT": str(default_plugin_root()),
            "SOCIAL_AGENT_PYTHONPATH": _pythonpath(self.project_root),
        }
        if publish_approval_token:
            env["SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN"] = publish_approval_token
        client = HarnessJsonRpcClient(
            launch_args=[str(_node_executable()), str(_runtime_script(self.project_root))],
            cwd=self.project_root,
            env=env,
        )
        client.start(
            provider=self.settings.provider_id,
            model=self.settings.model,
            max_tokens=4096 if mode == "plan" else 8192,
        )
        self._clients[mode] = client
        return client

    def _drop_client(self, mode: Literal["plan", "execute"]) -> None:
        client = self._clients.pop(mode, None)
        if client is not None:
            client.close()


def requires_dynamic_harness(message: str) -> bool:
    dynamic_markers = (
        "分析",
        "总结",
        "摘要",
        "标签",
        "分类",
        "筛选",
        "比较",
        "竞品",
        "研究",
        "根据结果",
        "继续搜索",
        "打开页面",
        "点击",
        "输入",
        "翻页",
        "滚动",
        "按下",
        "生成文案",
        "生成选题",
        "报告",
    )
    return bool(requested_write_actions(message)) or any(marker in message for marker in dynamic_markers)


def _planning_persona() -> str:
    return """你是 Social Agent 的只读任务规划器。你没有任何工具，也不能执行任务。
把用户目标规划为固定或动态步骤，只输出一个 JSON 对象，禁止 Markdown：
{"summary":"简洁计划说明","steps":["步骤1","步骤2"]}
图片会作为 Harness 原生 ImageBlock 随用户消息提供；视频和音频的本地分析证据会作为
同一条消息中的结构化媒体上下文提供。必须结合当前消息、附件和已有会话历史理解意图。
允许的能力只有：分析用户明确附加的本地媒体；在已授权的比特浏览器会话中打开页面、观察、点击搜索/导航控件、
输入非敏感搜索词、按键、滚动或翻页；浏览抖音/小红书/X、下载会话可见的帖子媒体、
分析本地媒体、检测并生成去水印副本、生成本地文案草稿；仅当用户明确要求且选择 X
登录会话时，可以把最终文案和媒体发布为一条 X 帖子。禁止登录、向其他平台发布、点赞、
评论、关注、私信、转发、修改代理或索取 Cookie/密码/验证码。不要输出通用的版权、平台规则、
内容适用性或只读能力提醒；所有执行都由应用层统一要求用户确认。"""


def _planning_prompt(
    message: str,
    session: SelectedSession | None,
    *,
    attachments: tuple[AgentAttachment, ...] = (),
    media_context: str | None = None,
    context_summary: str | None = None,
) -> str:
    return json.dumps(
        {
            "task": message,
            "selected_platform": session.platform if session else None,
            "profile_name": session.profile_name if session else None,
            "browser_session_available": session is not None,
            "attachments": [_attachment_manifest(item) for item in attachments],
            "video_audio_analysis": media_context,
            "previous_execution_summary": context_summary,
            "instruction": (
                "只输出一个严格 JSON 对象，不调用任何工具。"
                "summary 必须是字符串，steps 必须是字符串数组。不要输出其他字段或通用提醒。"
                "结合本轮文字、图片、视频音频分析以及此前会话上下文规划。"
                "没有浏览器会话时，只规划本地媒体相关任务，不规划平台浏览。"
            ),
        },
        ensure_ascii=False,
    )


def _planning_repair_prompt(
    message: str,
    session: SelectedSession | None,
    invalid_response: str,
) -> str:
    return json.dumps(
        {
            "task": message,
            "selected_platform": session.platform if session else None,
            "instruction": (
                "上一次输出不是合法 JSON。重新输出且只输出："
                '{"summary":"字符串","steps":["步骤"]}'
            ),
            "invalid_response_excerpt": invalid_response[:2_000],
        },
        ensure_ascii=False,
    )


def _validated_dynamic_plan(
    payload: dict[str, Any],
    *,
    message: str,
    session: SelectedSession | None,
    attachments: tuple[AgentAttachment, ...],
    media_context: str | None,
    max_tool_calls: int,
) -> DynamicAgentPlan:
    write_actions = list(requested_write_actions(message))
    return DynamicAgentPlan.model_validate(
        {
            "mode": "dynamic_harness",
            "objective": message,
            "platform": session.platform if session else None,
            "session_ref": session.session_ref if session else None,
            "summary": payload.get("summary"),
            "steps": payload.get("steps"),
            "attachments": list(attachments),
            "media_context": media_context,
            "write_actions": write_actions,
            "max_tool_calls": min(20, max_tool_calls),
            "requires_confirmation": True,
        }
    )


def _execution_persona() -> str:
    return """你是 Social Agent 的动态执行内核。用户已经在桌面端确认本次整体计划。
你只能调用 mcp__social__ 命名空间下的工具，不能调用或假设任何其他能力。
标准能力必须直接调用 browse_posts、browser_operate、download_media、analyze_content、
process_watermark、generate_post_copy 或 publish_x_post；禁止通过 call_plugin_tool 重复调用这些标准工具。
只有新增插件能力才先调用 list_plugin_tools 查看，再通过 call_plugin_tool 调用清单中明确声明的工具。
插件未安装或未启用时不得假设其可用。
有 selected_session_ref 时必须严格使用，不得索取或输出 Cookie、密码、验证码、代理或
指纹信息；没有时只能处理用户附加的本地媒体，不得调用浏览器 Tool。平台不匹配时停止并说明。
图片是 Harness 原生 ImageBlock；视频音频证据来自媒体 Tool 的结构化预处理。浏览最多100条；下载工具每次最多20个URL，
超过时分批调用，总下载预算默认5000MB。只处理下载结果返回的本地文件路径。
搜索抖音/小红书/X 帖子时优先直接调用 browse_posts，不要先手动操作搜索框。
需要通用页面操作时，严格使用以下格式：观察页面只传 action="observe"；导航只传
action="navigate", url="https://..."；输入先观察获取 element_ref，再传
action="input", element_ref="eN", value="输入内容"，其中 text 只用于按可见文字定位控件，
不是输入值；点击传 action="click", element_ref="eN"。不要使用 set_value，max_elements 不得超过100。
只允许搜索、浏览和翻页，不得输入密码/验证码或点击发布、互动、交易、删除类控件。
去水印必须保留原文件。默认只生成本地文案草稿；只有 approved_write_actions 包含
publish_x 且存在一次性 approval_token 时，才可调用 publish_x_post，并且整次执行最多
调用一次。发布前自行确定唯一的最终文案和最多4个媒体文件；提交后无论结果成功、失败或
unknown 都不得自动重试，也不得输出 approval_token。根据工具结果可以调整搜索、分析、
筛选和后续步骤。同一个 Tool 返回同类校验错误或空 posts 时最多调整参数重试一次；第二次仍为空就停止该路径，
不得连续更换关键词盲目重试。完成后用中文汇总实际调用、发布状态/帖子地址、输出目录、警告和未完成项。"""


def _execution_prompt(
    plan: DynamicAgentPlan,
    policy: ExecutionPolicy,
    *,
    publish_approval_token: str | None = None,
) -> str:
    return json.dumps(
        {
            "execution_authorized": True,
            "objective": plan.objective,
            "selected_platform": plan.platform,
            "selected_session_ref": plan.session_ref,
            "attached_media": [_attachment_manifest(item) for item in plan.attachments],
            "video_audio_analysis": plan.media_context,
            "approved_plan": {"summary": plan.summary, "steps": plan.steps},
            "approved_write_actions": plan.write_actions,
            "x_publish_approval_token": publish_approval_token,
            "constraints": {
                "read_only_platform": not bool(publish_approval_token),
                "local_generation_only": not bool(publish_approval_token),
                "x_publish_allowed_once": bool(publish_approval_token),
                "max_browse_items": policy.max_browse_items,
                "max_download_urls_per_call": policy.max_download_urls_per_call,
                "max_total_download_mb": policy.max_total_download_mb,
                "max_tool_calls": min(plan.max_tool_calls, policy.max_tool_calls),
            },
        },
        ensure_ascii=False,
    )


def _attachment_manifest(item: AgentAttachment) -> dict[str, Any]:
    return {
        "display_name": item.display_name,
        "media_type": item.media_type,
        "modality": item.modality,
        "size_bytes": item.size_bytes,
        "local_path": item.path,
    }


def _content_blocks(
    prompt: str,
    attachments: tuple[AgentAttachment, ...],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in attachments:
        if item.modality is not AttachmentModality.IMAGE:
            continue
        path = Path(item.path).expanduser().resolve(strict=True)
        blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": item.media_type,
                "name": item.display_name,
            }
        )
    return blocks


def _project_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parents[2]


def _runtime_script(root: Path) -> Path:
    return root / "harness" / "node_modules" / "@deepseek-ai" / "dsh-sdk-jsonrpc-demo" / "lib" / "bin.js"


def _node_executable() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    candidates = [
        os.getenv("SOCIAL_AGENT_NODE"),
        str(Path(bundled_root) / ("node.exe" if os.name == "nt" else "node"))
        if bundled_root
        else None,
        str(Path(sys.executable).resolve().parent / "node"),
        str(Path(sys.executable).resolve().parent / "node.exe"),
        "/opt/homebrew/opt/node@24/bin/node",
        shutil.which("node"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            continue
        try:
            output = subprocess.check_output([str(path), "--version"], text=True, timeout=5).strip()
            match = re.match(r"v(\d+)\.(\d+)\.", output)
            if match and (int(match.group(1)) >= 24 or (int(match.group(1)) == 22 and int(match.group(2)) >= 19)):
                return path
        except (OSError, subprocess.SubprocessError):
            continue
    raise HarnessError("未找到 Node.js 22.19+ 或 24+；请运行 brew install node@24。")


def _pythonpath(root: Path) -> str:
    entries = [
        root / "src",
        root.parent / "tools" / "social_content_crawler" / "src",
        root.parent / "tools" / "media_content_analyzer" / "src",
    ]
    existing = os.getenv("PYTHONPATH")
    if existing:
        return os.pathsep.join([*(str(path) for path in entries), existing])
    return os.pathsep.join(str(path) for path in entries)


def _json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise HarnessError("Harness planning response did not contain JSON")
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        excerpt = cleaned[start : min(end + 1, start + 240)]
        raise HarnessError(
            f"Harness planning response was invalid JSON near position {exc.pos}: {excerpt!r}"
        ) from exc
    if not isinstance(value, dict):
        raise HarnessError("Harness planning response must be a JSON object")
    return value
