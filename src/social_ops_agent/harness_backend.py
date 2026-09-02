from __future__ import annotations

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

from .contracts import AgentAttachment, AgentProgress, DynamicAgentPlan
from .execution_policy_channel import ExecutionPolicyChannel
from .harness_client import HarnessError, HarnessJsonRpcClient
from .harness_prompts import (
    content_blocks,
    execution_persona,
    execution_prompt,
    planning_persona,
    planning_prompt,
    planning_repair_prompt,
)
from .planner import SelectedSession, requested_download_limit
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
        policy_name = re.sub(r"[^A-Za-z0-9_.-]", "_", self.conversation_id)
        self.execution_policy_path = (
            self.state_root / "execution-policies" / f"{policy_name}.json"
        )
        self.execution_policy_channel = ExecutionPolicyChannel(self.execution_policy_path)
        self._clients: dict[Literal["plan", "execute"], HarnessJsonRpcClient] = {}
        self._active_client: HarnessJsonRpcClient | None = None
        self._runtime_epoch = secrets.token_hex(6)
        self._plan_generation = 0
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
        plan_session_id = (
            f"{self.conversation_id}-plan-{self._runtime_epoch}-{self._plan_generation}"
        )
        try:
            prompt = planning_prompt(
                message,
                session,
                attachments=attachments,
                media_context=media_context,
                context_summary=context_summary,
            )
            first_turn = client.run_turn(
                session_id=plan_session_id,
                content_blocks=content_blocks(prompt, attachments),
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
                    prompt=planning_repair_prompt(
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
        execution_id = secrets.token_urlsafe(18)
        self.execution_policy_channel.grant(
            execution_id,
            max_download_posts=plan.max_download_posts,
        )
        if approval_token:
            # X publishing keeps its stronger one-shot process isolation.
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
            execution_session_id = (
                f"{self.conversation_id}-execute-publish-{execution_id}"
                if approval_token
                else (
                    f"{self.conversation_id}-execute-{self._runtime_epoch}-"
                    f"{self._execute_generation}"
                )
            )
            turn = client.run_turn(
                session_id=execution_session_id,
                content_blocks=content_blocks(
                    execution_prompt(
                        plan,
                        self.policy,
                        publish_approval_token=approval_token,
                    ),
                    tuple(plan.attachments),
                ),
                on_event=on_event,
            )
            response = turn.final_response.strip()
            if not response:
                # An idle turn with no assistant message is not a successful task.
                # Discard only the broken execution process so the next user retry
                # starts cleanly; the persistent planning session still owns the
                # conversational intent and can reconstruct the approved plan.
                self._drop_client("execute")
                self._execute_generation += 1
                raise HarnessError(
                    "Execution Harness ended without an assistant response; "
                    "the execution session was reset and the task can be retried."
                )
            notify(AgentProgress(stage="done", completed=1, total=1, message="Harness 动态任务执行完成。"))
            return HarnessExecutionResult(
                plan=plan,
                response=response,
                session_id=turn.session_id,
                tool_calls=turn.tool_calls,
                finish_reason=turn.finish_reason,
            )
        finally:
            self._active_client = None
            self.execution_policy_channel.revoke(execution_id)
            if approval_token:
                # Revoke the one-time write grant even if no Tool was called.
                self._drop_client("execute")

    def cancel(self) -> None:
        if self._active_client is not None:
            active = self._active_client
            active.cancel()
            for mode, client in list(self._clients.items()):
                if client is active:
                    self._clients.pop(mode, None)
                    if mode == "plan":
                        self._plan_generation += 1
                    else:
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
            "DSH_SYSTEM_PROMPT": planning_persona() if mode == "plan" else execution_persona(),
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
            "SOCIAL_AGENT_EXECUTION_POLICY_PATH": str(self.execution_policy_path),
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
    """Compatibility helper: every non-empty natural-language command uses Harness."""
    return bool(message.strip())


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
    explicit_download_limit = requested_download_limit(message)
    model_download_limit = payload.get("max_download_posts")
    max_download_posts = (
        explicit_download_limit
        if explicit_download_limit is not None
        else model_download_limit
    )
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
            "max_download_posts": max_download_posts,
            "write_actions": write_actions,
            "max_tool_calls": min(20, max_tool_calls),
            "requires_confirmation": bool(write_actions),
        }
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
