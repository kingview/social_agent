from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .contracts import AgentProgress, DynamicAgentPlan
from .harness_client import HarnessError, HarnessJsonRpcClient
from .planner import SelectedSession
from .plugins import default_plugin_root
from .policy import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from .settings import LLMSettings


HARNESS_VERSION = "0.1.1-rc.2"
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
        self._client: HarnessJsonRpcClient | None = None

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

    def propose(self, message: str, session: SelectedSession) -> DynamicAgentPlan:
        self.policy.validate_message(message)
        client = self._start_client(mode="plan")
        try:
            prompt = _planning_prompt(message, session)
            first_turn = client.run_turn(
                session_id=f"{self.conversation_id}-plan",
                prompt=prompt,
            )
            try:
                return _validated_dynamic_plan(
                    _json_object(first_turn.final_response),
                    message=message,
                    session=session,
                    max_tool_calls=self.policy.max_tool_calls,
                )
            except (HarnessError, ValidationError):
                repair_turn = client.run_turn(
                    session_id=f"{self.conversation_id}-plan-repair-{uuid.uuid4().hex[:8]}",
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
                    max_tool_calls=self.policy.max_tool_calls,
                )
        finally:
            client.close()
            self._client = None

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
        client = self._start_client(mode="execute")
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
                session_id=f"{self.conversation_id}-execute-{uuid.uuid4().hex[:8]}",
                prompt=_execution_prompt(plan, self.policy),
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
            client.close()
            self._client = None

    def cancel(self) -> None:
        if self._client is not None:
            self._client.cancel()

    def _start_client(self, *, mode: Literal["plan", "execute"]) -> HarnessJsonRpcClient:
        available, reason = self.is_available(self.project_root)
        if not available:
            raise HarnessError(reason)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.session_root.mkdir(parents=True, exist_ok=True)
        config = self.project_root / "harness" / f"cordis-{mode}.yml"
        env = {
            "DSH_CORDIS_CONFIG": str(config),
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
        self._client = client
        return client


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
    return any(marker in message for marker in dynamic_markers)


def _planning_persona() -> str:
    return """你是 Social Agent 的只读任务规划器。你没有任何工具，也不能执行任务。
把用户目标规划为固定或动态步骤，只输出一个 JSON 对象，禁止 Markdown：
{"summary":"简洁计划说明","steps":["步骤1","步骤2"],"risk_notes":["提醒"]}
允许的能力只有：在已授权的比特浏览器会话中打开页面、观察、点击搜索/导航控件、
输入非敏感搜索词、按键、滚动或翻页；浏览抖音/小红书/X、下载会话可见的帖子媒体、
分析本地媒体、检测并生成去水印副本、生成本地文案草稿。禁止登录、发布、点赞、
评论、关注、私信、转发、修改代理或索取 Cookie/密码/验证码。所有执行都要用户确认。"""


def _planning_prompt(message: str, session: SelectedSession) -> str:
    return json.dumps(
        {
            "task": message,
            "selected_platform": session.platform,
            "profile_name": session.profile_name,
            "instruction": (
                "只输出一个严格 JSON 对象，不调用任何工具。"
                "summary 必须是字符串，steps 和 risk_notes 必须是字符串数组。"
            ),
        },
        ensure_ascii=False,
    )


def _planning_repair_prompt(
    message: str,
    session: SelectedSession,
    invalid_response: str,
) -> str:
    return json.dumps(
        {
            "task": message,
            "selected_platform": session.platform,
            "instruction": (
                "上一次输出不是合法 JSON。重新输出且只输出："
                '{"summary":"字符串","steps":["步骤"],"risk_notes":["提醒"]}'
            ),
            "invalid_response_excerpt": invalid_response[:2_000],
        },
        ensure_ascii=False,
    )


def _validated_dynamic_plan(
    payload: dict[str, Any],
    *,
    message: str,
    session: SelectedSession,
    max_tool_calls: int,
) -> DynamicAgentPlan:
    return DynamicAgentPlan.model_validate(
        {
            "mode": "dynamic_harness",
            "objective": message,
            "platform": session.platform,
            "session_ref": session.session_ref,
            "summary": payload.get("summary"),
            "steps": payload.get("steps"),
            "risk_notes": payload.get("risk_notes", []),
            "max_tool_calls": min(20, max_tool_calls),
            "requires_confirmation": True,
        }
    )


def _execution_persona() -> str:
    return """你是 Social Agent 的动态执行内核。用户已经在桌面端确认本次整体计划。
你只能调用 mcp__social__ 命名空间下的工具，不能调用或假设任何其他能力。
标准能力可以直接调用具名工具；新增插件能力先调用 list_plugin_tools 查看，再通过
call_plugin_tool 调用清单中明确声明的工具。插件未安装或未启用时不得假设其可用。
严格使用任务中提供的 selected_session_ref，不得索取或输出 Cookie、密码、验证码、
代理或指纹信息。平台不匹配时停止并说明。浏览最多100条；下载工具每次最多20个URL，
超过时分批调用，总下载预算默认5000MB。只处理下载结果返回的本地文件路径。
需要通用页面操作时，先调用 browser_operate(action="observe") 获取 element_ref；
只允许搜索、浏览和翻页，不得输入密码/验证码或点击发布、互动、交易、删除类控件。
去水印必须保留原文件。文案只生成本地草稿，绝不发布。根据工具结果可以调整搜索、
分析、筛选和后续步骤。完成后用中文汇总实际调用、结果、输出目录、警告和未完成项。"""


def _execution_prompt(plan: DynamicAgentPlan, policy: ExecutionPolicy) -> str:
    return json.dumps(
        {
            "execution_authorized": True,
            "objective": plan.objective,
            "selected_platform": plan.platform,
            "selected_session_ref": plan.session_ref,
            "approved_plan": {"summary": plan.summary, "steps": plan.steps},
            "constraints": {
                "read_only_platform": True,
                "local_generation_only": True,
                "max_browse_items": policy.max_browse_items,
                "max_download_urls_per_call": policy.max_download_urls_per_call,
                "max_total_download_mb": policy.max_total_download_mb,
                "max_tool_calls": min(plan.max_tool_calls, policy.max_tool_calls),
            },
        },
        ensure_ascii=False,
    )


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
