from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from threading import Event
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import (
    AgentAttachment,
    AgentProgress,
    BrowserSessionBinding,
    DynamicAgentPlan,
)
from .execution_policy_channel import ExecutionPolicyChannel
from .browser_cleanup import cleanup_task_browsers
from .browser_scheduler import BrowserTaskScheduler, BrowserWaitCancelled, resources_for_plan
from .execution_lifecycle import ExecutionLifecycle
from .execution_tracking import ExecutionTracker
from .harness_client import (
    HarnessError,
    HarnessJsonRpcClient,
    recover_logged_final_response,
    recover_logged_turn_error,
)
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
from .policy import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from .diagnostics import record_exception, register_secrets
from .settings import LLMSettings
from .task_intent import resolve_write_actions
from .task_store import TaskRecord, TaskStore


HARNESS_VERSION = "0.1.1-rc.2 + native-image compatibility"
class HarnessExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: DynamicAgentPlan
    response: str
    session_id: str
    tool_calls: list[str]
    warnings: list[str] = Field(default_factory=list)
    finish_reason: str | None = None
    cancelled: bool = False
    completion_status: Literal["completed", "partial", "failed"] = "completed"
    completed_steps: int = 0
    total_steps: int = 0
    publish_state: Literal["not_requested", "not_attempted", "published", "failed", "unknown"] = "not_requested"

    def user_summary(self) -> str:
        summary = self.response or "执行结束，但模型没有返回文字总结。"
        if self.completion_status != "completed":
            summary = (f"任务未全部完成：实际完成 {self.completed_steps}/{self.total_steps} 步。\n"
                       + "\n".join(self.warnings)
                       + "\n\n模型总结（完成状态以工具核验结果为准）：\n" + summary)
        return summary


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
        self.task_store = TaskStore(self.state_root)
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
        self._cancel_requested = Event()
        self._cancelled_client: HarnessJsonRpcClient | None = None
        self.browser_scheduler = BrowserTaskScheduler()

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
        self, message: str, session: SelectedSession | None, *,
        available_sessions: tuple[SelectedSession, ...] = (),
        attachments: tuple[AgentAttachment, ...] = (), media_context: str | None = None,
        context_summary: str | None = None, task_id: str | None = None,
    ) -> DynamicAgentPlan:
        task_id = self.task_store.ensure_task(self.conversation_id, message, task_id)
        history = self.task_store.history(self.conversation_id, exclude=task_id)
        try:
            plan = self._propose(
                message, session, available_sessions=available_sessions, attachments=attachments,
                media_context=media_context,
                context_summary=context_summary or self.task_store.model_context(self.conversation_id, exclude=task_id),
                task_history=history,
            ).model_copy(update={"task_id": task_id})
        except BaseException as exc:
            register_secrets(self.settings.api_key)
            record_exception("agent", "harness.planning", exc, state_root=self.state_root,
                             task_id=task_id, conversation_id=self.conversation_id)
            self.task_store.planning_failed(task_id, str(exc))
            raise
        self.task_store.set_plan(task_id, plan.model_dump(mode="json"))
        return plan

    def _propose(
        self,
        message: str,
        session: SelectedSession | None,
        *,
        available_sessions: tuple[SelectedSession, ...] = (),
        attachments: tuple[AgentAttachment, ...] = (),
        media_context: str | None = None,
        context_summary: str | None = None,
        task_history: tuple[TaskRecord, ...] = (),
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
                available_sessions=available_sessions,
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
                    available_sessions=available_sessions,
                    attachments=attachments,
                    media_context=media_context,
                    max_tool_calls=self.policy.max_tool_calls,
                    context_summary=context_summary,
                    task_history=task_history,
                    require_step_tools=True,
                )
            except (HarnessError, ValidationError) as exc:
                register_secrets(self.settings.api_key)
                record_exception("agent", "harness.planning_repair", exc,
                    state_root=self.state_root, conversation_id=self.conversation_id)
                repair_turn = client.run_turn(
                    session_id=plan_session_id,
                    prompt=planning_repair_prompt(
                        message,
                        session,
                        first_turn.final_response,
                        available_sessions=available_sessions,
                        context_summary=context_summary,
                        validation_error=str(exc),
                    ),
                )
                return _validated_dynamic_plan(
                    _json_object(repair_turn.final_response),
                    message=message,
                    session=session,
                    available_sessions=available_sessions,
                    attachments=attachments,
                    media_context=media_context,
                    max_tool_calls=self.policy.max_tool_calls,
                    context_summary=context_summary,
                    require_step_tools=True,
                    task_history=task_history,
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
        self, plan: DynamicAgentPlan, *,
        progress: Callable[[AgentProgress], None] | None = None,
    ) -> HarnessExecutionResult:
        self.policy.validate_plan(plan)
        task_id = self.task_store.ensure_task(self.conversation_id, plan.objective, plan.task_id)
        # Recheck lineage at execution time, not only at planning time.
        resolve_write_actions(plan.model_dump(), plan.objective,
                              self.task_store.history(self.conversation_id, exclude=task_id))
        plan = plan.model_copy(update={"task_id": task_id})
        self.task_store.set_plan(task_id, plan.model_dump(mode="json"))
        execution_id = secrets.token_urlsafe(18)
        self.task_store.start(task_id, execution_id)
        tracker = ExecutionTracker(plan)

        def notify(event: AgentProgress) -> None:
            self.task_store.checkpoint(task_id, execution_id, tracker.report())
            if progress is not None:
                progress(event)

        def waiting(label, owner):
            owner_label = f"对话 {owner[-6:]}" if owner else "其他任务"
            notify(AgentProgress(stage="waiting_browser", completed=0, total=len(plan.steps),
                message=f"等待浏览器窗口「{label}」：{owner_label} 正在使用，释放后会自动继续。"))

        def finish_runtime(failed):
            try:
                if failed or plan.write_actions or self._cancel_requested.is_set():
                    self._drop_client("execute")
            finally:
                self._active_client = None
                self._join_cancelled_runtime()

        lifecycle = ExecutionLifecycle(scheduler=self.browser_scheduler,
            policy_channel=self.execution_policy_channel, plan=plan, execution_id=execution_id,
            conversation_id=self.conversation_id, cancelled=self._cancel_requested,
            finish_runtime=finish_runtime,
            report_error=lambda exc: record_exception("agent", "harness.teardown", exc,
                state_root=self.state_root, task_id=task_id, execution_id=execution_id,
                conversation_id=self.conversation_id))
        try:
            with lifecycle:
                lifecycle.acquire(lambda: resources_for_plan(plan, self.registry_path), on_wait=waiting)
                result = self._execute(plan, execution_id=execution_id, tracker=tracker, progress=notify)
                lifecycle.stop_runtime()
                if result.completion_status == "completed" and not result.cancelled:
                    resources = self.state_root / "browser-resources" / execution_id
                    if resources.is_dir():
                        notify(AgentProgress(stage="cleanup", completed=len(plan.steps), total=len(plan.steps),
                                             message="任务已完成，正在关闭本次新开的浏览器窗口和标签页…"))
                        cleaned = cleanup_task_browsers(self.state_root, execution_id)
                        result.warnings.extend(cleaned["warnings"])
                        detail = (f"已清理本次任务的 {cleaned['closed_tabs']} 个标签页、"
                                  f"{cleaned['closed_windows']} 个浏览器窗口。")
                        if cleaned["warnings"]:
                            detail += "\n" + "\n".join(cleaned["warnings"])
                        result.response = (result.response + "\n\n" + detail).strip()
                state = "cancelled" if result.cancelled else "succeeded" if result.completion_status == "completed" else result.completion_status
                self.task_store.checkpoint(task_id, execution_id,
                    {**tracker.report(), "summary": result.user_summary(), "warnings": result.warnings, "cancelled": result.cancelled,
                     "completion_status": result.completion_status, "finish_reason": result.finish_reason}, state=state)
                return result
        except BrowserWaitCancelled:
            result = HarnessExecutionResult(plan=plan, response="已取消等待浏览器窗口，未开始执行任务。",
                session_id="", tool_calls=[], cancelled=True, completion_status="failed",
                total_steps=len(plan.steps), finish_reason="cancelled")
            self.task_store.checkpoint(task_id, execution_id,
                {**tracker.report(), "summary": result.response, "cancelled": True,
                 "completion_status": "failed", "finish_reason": "cancelled"}, state="cancelled")
            return result
        except BaseException as exc:
            register_secrets(self.settings.api_key)
            record_exception("agent", "harness.execution", exc, state_root=self.state_root,
                             task_id=task_id, execution_id=execution_id, conversation_id=self.conversation_id)
            if plan.write_actions:
                tracker.reconcile_publication(self.task_store.execution(task_id)["publish_state"])
            self.task_store.checkpoint(task_id, execution_id,
                {**tracker.report(), "error": str(exc),
                 "completion_status": "partial" if tracker.completed else "failed"},
                state="partial" if tracker.completed else "failed")
            raise

    def _execute(
        self,
        plan: DynamicAgentPlan,
        *,
        execution_id: str,
        tracker: ExecutionTracker,
        progress: Callable[[AgentProgress], None] | None = None,
    ) -> HarnessExecutionResult:
        self.policy.validate_plan(plan)
        notify = progress or (lambda _event: None)
        total_steps = len(plan.steps)
        notify(
            AgentProgress(
                stage="step",
                completed=0,
                total=total_steps,
                message=f"准备执行，共 {total_steps} 步。",
            )
        )
        write_actions = tuple(plan.write_actions)
        approval_token = secrets.token_urlsafe(32) if write_actions == ("publish_x",) else None
        if self._cancel_requested.is_set():
            raise BrowserWaitCancelled("任务已取消，未启动执行模型。")
        if approval_token:
            # X publishing keeps its stronger one-shot process isolation.
            self._drop_client("execute")
        client = self._start_client(
            mode="execute",
            publish_approval_token=approval_token,
        )
        self._active_client = client
        if self._cancel_requested.is_set():
            client.cancel()
            raise HarnessError("任务已由用户停止。")
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
                notify(tracker.called(data))
            elif event_type == "tool/result" and isinstance(data, dict):
                update = tracker.returned(data)
                if plan.write_actions:
                    tracker.reconcile_publication(self.task_store.execution(plan.task_id)["publish_state"])
                notify(update.model_copy(update={"completed": len(tracker.completed)}))

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
        warnings: list[str] = []
        if not response:
            response = recover_logged_final_response(
                self.session_root / "execute",
                execution_session_id,
            ).strip()
            if response:
                warnings.append(
                    "Harness 在最终文本生成后异常结束；已从本地会话日志恢复完整结果。"
                )
        if response and turn.finish_reason == "error":
            failure = recover_logged_turn_error(
                self.session_root / "execute",
                execution_session_id,
            )
            warnings.append(
                "模型在生成最终结果后报告错误"
                f"{f'：{failure}' if failure else ''}；结果已保留，执行会话已重置。"
            )
            self._drop_client("execute")
            self._execute_generation += 1
        if not response:
            # An idle turn with no assistant message is not a successful task.
            # Discard only the broken execution process so the next user retry
            # starts cleanly; the persistent planning session still owns the
            # conversational intent and can reconstruct the approved plan.
            self._drop_client("execute")
            self._execute_generation += 1
            raise HarnessError(
                "Execution Harness 未返回最终总结；执行会话已重置，请重试。"
            )
        if plan.write_actions:
            tracker.reconcile_publication(self.task_store.execution(plan.task_id)["publish_state"])
        final_progress = tracker.finish(normal_end=turn.finish_reason not in {"error", "cancelled", "interrupted"})
        completion_status = (
            "completed" if final_progress.stage == "done"
            else "partial" if tracker.completed else "failed"
        )
        if completion_status != "completed":
            warnings.append("未完成步骤：" + "；".join(tracker.unfinished()))
            if plan.write_actions and tracker.publish_state != "published":
                warnings.append(
                    "X 发布未执行。" if tracker.publish_state == "not_attempted"
                    else "X 发布未获成功确认，不能自动重试，请先核对账号。"
                )
        notify(final_progress)
        return HarnessExecutionResult(
            plan=plan,
            response=response,
            session_id=turn.session_id,
            tool_calls=tracker.tool_calls or turn.tool_calls,
            warnings=list(dict.fromkeys(warnings)),
            finish_reason=turn.finish_reason,
            cancelled=self._cancel_requested.is_set() or turn.finish_reason in {"cancelled", "interrupted"},
            completion_status=completion_status,
            completed_steps=len(tracker.completed),
            total_steps=total_steps,
            publish_state=tracker.publish_state,
        )
    def cancel(self) -> None:
        self._cancel_requested.set()
        if self._active_client is not None:
            active = self._active_client
            self._cancelled_client = active
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

    def _join_cancelled_runtime(self) -> None:
        client, self._cancelled_client = self._cancelled_client, None
        if client is not None:
            client.close()

    def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        self._active_client = None
        try:
            for client in clients:
                client.close()
        finally:
            self._join_cancelled_runtime()

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
            # Explicit empty value revokes any credential inherited from the
            # host environment for planning/read-only executions.
            "SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN": publish_approval_token or "",
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
        self._clients[mode] = client
        return client

    def _drop_client(self, mode: Literal["plan", "execute"]) -> None:
        client = self._clients.pop(mode, None)
        if client is not None:
            client.close()


def requires_dynamic_harness(message: str) -> bool:
    """Compatibility helper: every non-empty natural-language command uses Harness."""
    return bool(message.strip())


def _tool_label(name: str) -> str:
    labels = {
        "mcp__social__browse_posts": "正在搜索并读取帖子",
        "mcp__social__browser_operate": "正在操作浏览器",
        "mcp__social__download_media": "正在下载帖子内容",
        "mcp__social__analyze_content": "正在分析图片、视频和文本",
        "mcp__social__process_watermark": "正在检测并处理水印",
        "mcp__social__generate_post_copy": "正在生成文案",
        "mcp__social__publish_x_post": "正在发布到 X",
    }
    return labels.get(name, f"正在调用 {name}")


def _validated_dynamic_plan(
    payload: dict[str, Any],
    *,
    message: str,
    session: SelectedSession | None,
    available_sessions: tuple[SelectedSession, ...] = (),
    attachments: tuple[AgentAttachment, ...],
    media_context: str | None,
    max_tool_calls: int,
    context_summary: str | None = None,
    require_step_tools: bool = False,
    task_history: tuple[TaskRecord, ...] = (),
) -> DynamicAgentPlan:
    write_actions, resume_turn_id = resolve_write_actions(payload, message, task_history)
    step_tools = payload.get("step_tools", [])
    if require_step_tools and (
        not isinstance(step_tools, list)
        or len(step_tools) != len(payload.get("steps", []))
        or not step_tools
    ):
        raise HarnessError("step_tools 必须与 steps 一一对应。")
    if step_tools and ("publish_x_post" in step_tools) != bool(write_actions):
        raise HarnessError("发布步骤与用户授权不一致；继续历史任务时必须提供 resume_turn_id 和 write_actions。")
    explicit_download_limit = requested_download_limit(message)
    model_download_limit = payload.get("max_download_posts")
    max_download_posts = (
        explicit_download_limit
        if explicit_download_limit is not None
        else model_download_limit
    )
    selected_sessions = _select_browser_sessions(
        payload,
        message=message,
        manual_session=session,
        available_sessions=available_sessions,
    )
    primary = selected_sessions[0] if selected_sessions else None
    return DynamicAgentPlan.model_validate(
        {
            "mode": "dynamic_harness",
            "objective": message,
            "platform": primary.platform if primary else None,
            "session_ref": primary.session_ref if primary else None,
            "browser_sessions": selected_sessions,
            "summary": payload.get("summary"),
            "steps": payload.get("steps"),
            "step_tools": step_tools,
            "step_units": payload.get("step_units", []),
            "resume_turn_id": resume_turn_id,
            "attachments": list(attachments),
            "media_context": media_context,
            "max_download_posts": max_download_posts,
            "write_actions": write_actions,
            "publish_media_required": payload.get("publish_media_required"),
            "max_tool_calls": min(20, max_tool_calls),
            "requires_confirmation": False,
        }
    )


def _select_browser_sessions(
    payload: dict[str, Any],
    *,
    message: str,
    manual_session: SelectedSession | None,
    available_sessions: tuple[SelectedSession, ...],
) -> list[BrowserSessionBinding]:
    """Resolve model choices only against the local session registry snapshot."""
    candidates = _unique_sessions(
        (manual_session,) if manual_session is not None else available_sessions
    )
    if manual_session is not None:
        selected = [manual_session]
    else:
        raw_refs = payload.get("session_refs", payload.get("selected_session_refs", []))
        if raw_refs is None:
            raw_refs = []
        if isinstance(raw_refs, str):
            raw_refs = [raw_refs]
        if not isinstance(raw_refs, list) or any(not isinstance(item, str) for item in raw_refs):
            raise HarnessError("planning response session_refs must be a string array")
        by_ref = {item.session_ref: item for item in candidates}
        unknown = [item for item in raw_refs if item not in by_ref]
        if unknown:
            raise HarnessError("planning response selected an unregistered browser session")
        selected = _unique_sessions(tuple(by_ref[item] for item in raw_refs))

        # Models occasionally omit a required platform even though it is explicit
        # in the command. Fill only from the trusted local candidate list.
        selected_platforms = {item.platform for item in selected}
        for platform in _mentioned_platforms(message):
            if platform in selected_platforms:
                continue
            inferred = _preferred_session(message, candidates, platform)
            if inferred is not None:
                selected.append(inferred)
                selected_platforms.add(platform)

        if not selected and len(candidates) == 1 and _looks_like_browser_task(message):
            selected = [candidates[0]]

    return [
        BrowserSessionBinding(
            session_ref=item.session_ref,
            platform=item.platform,
            profile_name=getattr(item, "profile_name", ""),
        )
        for item in selected
    ]


def _unique_sessions(sessions: tuple[SelectedSession, ...]) -> list[SelectedSession]:
    seen: set[str] = set()
    unique: list[SelectedSession] = []
    for item in sessions:
        if item.session_ref in seen:
            continue
        seen.add(item.session_ref)
        unique.append(item)
    return unique


def _mentioned_platforms(message: str) -> list[str]:
    patterns = {
        "douyin": r"抖音|douyin(?:\.com)?",
        "xiaohongshu": r"小红书|xiaohongshu|xhs(?:link)?",
        "telegram": r"telegram|电报|t\.me",
        "x": r"twitter|推特|x\.com|(?<![A-Za-z])x(?![A-Za-z])",
    }
    matches: list[tuple[int, str]] = []
    for platform, pattern in patterns.items():
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            matches.append((match.start(), platform))
    return [platform for _, platform in sorted(matches)]


def _preferred_session(
    message: str,
    candidates: list[SelectedSession],
    platform: str,
) -> SelectedSession | None:
    platform_sessions = [item for item in candidates if item.platform == platform]
    if not platform_sessions:
        return None
    normalized_message = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", message).casefold()
    named = [
        item
        for item in platform_sessions
        if item.profile_name
        and re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", item.profile_name).casefold()
        in normalized_message
    ]
    if named:
        return named[0]
    requested_numbers = set(re.findall(r"(?:窗口|账号|profile)\s*0*(\d+)", message, re.IGNORECASE))
    if requested_numbers:
        for item in platform_sessions:
            if requested_numbers.intersection(re.findall(r"\d+", item.profile_name)):
                return item
    return platform_sessions[0]


def _looks_like_browser_task(message: str) -> bool:
    return any(
        word in message.casefold()
        for word in (
            "搜索",
            "浏览",
            "帖子",
            "下载",
            "频道",
            "发布",
            "打开网页",
            "timeline",
            "feed",
        )
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
