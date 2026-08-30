from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Callable, Protocol, TypeAlias

from .contracts import (
    AgentAttachment,
    AgentExecutionResult,
    AgentPlan,
    AgentProgress,
    AgentRunResult,
    DynamicAgentPlan,
    RuntimeHealth,
)
from .harness_backend import (
    DeepSeekHarnessBackend,
    HarnessExecutionResult,
    requires_dynamic_harness,
)
from .planner import (
    ConversationalPlanner,
    PlanningError,
    PlanningPolicyError,
    SelectedSession,
)
from .policy import DEFAULT_EXECUTION_POLICY, ExecutionPolicy, ExecutionPolicyError
from .runtime import SocialOperationsAgent
from .settings import LLMSettings


RuntimePlan: TypeAlias = AgentPlan | DynamicAgentPlan
ProgressCallback: TypeAlias = Callable[[AgentProgress], None]


class AgentRuntime(Protocol):
    name: str

    def propose(
        self,
        message: str,
        session: SelectedSession | None,
        previous_plan: AgentPlan | None = None,
        *,
        attachments: tuple[AgentAttachment, ...] = (),
        media_context: str | None = None,
        context_summary: str | None = None,
    ) -> RuntimePlan: ...

    def execute(
        self,
        plan: RuntimePlan,
        *,
        progress: ProgressCallback | None = None,
    ) -> AgentExecutionResult: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...

    def health(self) -> RuntimeHealth: ...


class DeterministicAgentRuntime:
    name = "deterministic"

    def __init__(
        self,
        *,
        registry_path: Path,
        output_root: Path,
        settings: LLMSettings,
        policy: ExecutionPolicy,
    ) -> None:
        self.registry_path = registry_path
        self.output_root = output_root
        self.settings = settings
        self.policy = policy
        self._cancelled = threading.Event()

    def propose(
        self,
        message: str,
        session: SelectedSession | None,
        previous_plan: AgentPlan | None = None,
        *,
        attachments: tuple[AgentAttachment, ...] = (),
        media_context: str | None = None,
        context_summary: str | None = None,
    ) -> AgentPlan:
        del media_context, context_summary
        if attachments:
            raise PlanningError("固定 Workflow 不接受多媒体附件。")
        if session is None:
            raise PlanningError("平台浏览任务需要选择一个比特浏览器会话。")
        plan = ConversationalPlanner(
            ollama_base_url=self.settings.base_url,
            ollama_model=self.settings.model,
            api_key=self.settings.api_key,
        ).create_plan(message, session, previous_plan)
        self.policy.validate_plan(plan)
        return plan

    def execute(
        self,
        plan: RuntimePlan,
        *,
        progress: ProgressCallback | None = None,
    ) -> AgentExecutionResult:
        if not isinstance(plan, AgentPlan):
            raise TypeError("deterministic runtime requires AgentPlan")
        self.policy.validate_plan(plan)
        self._cancelled.clear()
        agent = SocialOperationsAgent.local(
            session_registry_path=self.registry_path,
            output_root=self.output_root,
            settings=self.settings,
        )
        result = asyncio.run(
            agent.execute_plan(
                plan,
                progress=progress,
                should_cancel=self._cancelled.is_set,
                authorization_confirmed=True,
            )
        )
        return _fixed_result(result)

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        return None

    def health(self) -> RuntimeHealth:
        return RuntimeHealth(
            runtime="deterministic",
            available=True,
            detail="确定性 Runtime 已就绪",
        )


class DeepSeekHarnessRuntime:
    name = "deepseek_harness"

    def __init__(
        self,
        *,
        registry_path: Path,
        output_root: Path,
        conversation_id: str,
        settings: LLMSettings,
        policy: ExecutionPolicy,
    ) -> None:
        self.policy = policy
        self.backend = DeepSeekHarnessBackend(
            registry_path=registry_path,
            output_root=output_root,
            conversation_id=conversation_id,
            settings=settings,
            policy=policy,
        )

    def propose(
        self,
        message: str,
        session: SelectedSession | None,
        previous_plan: AgentPlan | None = None,
        *,
        attachments: tuple[AgentAttachment, ...] = (),
        media_context: str | None = None,
        context_summary: str | None = None,
    ) -> DynamicAgentPlan:
        del previous_plan
        plan = self.backend.propose(
            message,
            session,
            attachments=attachments,
            media_context=media_context,
            context_summary=context_summary,
        )
        self.policy.validate_plan(plan)
        return plan

    def execute(
        self,
        plan: RuntimePlan,
        *,
        progress: ProgressCallback | None = None,
    ) -> AgentExecutionResult:
        if not isinstance(plan, DynamicAgentPlan):
            raise TypeError("Harness runtime requires DynamicAgentPlan")
        self.policy.validate_plan(plan)
        return _harness_result(self.backend.execute(plan, progress=progress))

    def cancel(self) -> None:
        self.backend.cancel()

    def close(self) -> None:
        self.backend.close()

    def health(self) -> RuntimeHealth:
        available, detail = self.backend.health()
        return RuntimeHealth(
            runtime="deepseek_harness",
            available=available,
            detail=detail,
        )


class RuntimeRouter:
    """Selects and owns the active runtime outside of any UI framework."""

    def __init__(
        self,
        *,
        registry_path: Path,
        output_root: Path,
        conversation_id: str,
        settings: LLMSettings | None = None,
        policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    ) -> None:
        self.registry_path = registry_path.expanduser().resolve()
        self.output_root = output_root.expanduser().resolve()
        self.conversation_id = conversation_id
        self.settings = settings or LLMSettings.from_env()
        self.policy = policy
        self._active_runtime: AgentRuntime | None = None
        self._cancel_requested = False
        self._deterministic_runtime: DeterministicAgentRuntime | None = None
        self._harness_runtime: DeepSeekHarnessRuntime | None = None

    def propose(
        self,
        message: str,
        session: SelectedSession | None,
        previous_plan: AgentPlan | None = None,
        *,
        attachments: tuple[AgentAttachment, ...] = (),
        media_context: str | None = None,
        context_summary: str | None = None,
    ) -> RuntimePlan:
        try:
            self.policy.validate_message(message)
        except ExecutionPolicyError as exc:
            raise PlanningPolicyError(str(exc)) from exc
        if attachments or requires_dynamic_harness(message):
            return self._harness().propose(
                message,
                session,
                previous_plan,
                attachments=attachments,
                media_context=media_context,
                context_summary=context_summary,
            )
        if session is None:
            raise PlanningError("请添加媒体附件，或选择一个比特浏览器会话。")
        try:
            return self._deterministic().propose(
                message,
                session,
                previous_plan,
                context_summary=context_summary,
            )
        except PlanningPolicyError:
            raise
        except PlanningError:
            return self._harness().propose(
                message,
                session,
                previous_plan,
                context_summary=context_summary,
            )

    def execute(
        self,
        plan: RuntimePlan,
        *,
        progress: ProgressCallback | None = None,
    ) -> AgentExecutionResult:
        runtime: AgentRuntime = (
            self._harness() if isinstance(plan, DynamicAgentPlan) else self._deterministic()
        )
        self._active_runtime = runtime
        try:
            if self._cancel_requested:
                raise RuntimeError("Agent execution was cancelled before the runtime started")
            return runtime.execute(plan, progress=progress)
        finally:
            self._active_runtime = None
            self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True
        if self._active_runtime is not None:
            self._active_runtime.cancel()

    def health(self) -> list[RuntimeHealth]:
        return [self._deterministic().health(), self._harness().health()]

    def close(self) -> None:
        if self._harness_runtime is not None:
            self._harness_runtime.close()
        if self._deterministic_runtime is not None:
            self._deterministic_runtime.close()
        self._harness_runtime = None
        self._deterministic_runtime = None

    def _deterministic(self) -> DeterministicAgentRuntime:
        if self._deterministic_runtime is None:
            self._deterministic_runtime = DeterministicAgentRuntime(
                registry_path=self.registry_path,
                output_root=self.output_root,
                settings=self.settings,
                policy=self.policy,
            )
        return self._deterministic_runtime

    def _harness(self) -> DeepSeekHarnessRuntime:
        if self._harness_runtime is None:
            self._harness_runtime = DeepSeekHarnessRuntime(
                registry_path=self.registry_path,
                output_root=self.output_root,
                conversation_id=self.conversation_id,
                settings=self.settings,
                policy=self.policy,
            )
        return self._harness_runtime


def _fixed_result(result: AgentRunResult) -> AgentExecutionResult:
    state = "已停止" if result.cancelled else "执行完成"
    summary = (
        f"{state}：发现 {len(result.discovered_urls)} 条，下载 {result.downloaded_items} 条，"
        f"生成 {result.artifact_count} 个文件，使用 {result.tool_calls_used} 次 Tool 调用。"
    )
    if result.plan.remove_watermark:
        summary += (
            f"\n检测到水印 {result.watermark_detected_count} 个，"
            f"生成去水印副本 {result.watermark_processed_count} 个。"
        )
    return AgentExecutionResult(
        runtime="deterministic",
        plan=result.plan,
        summary=summary,
        tool_calls_used=result.tool_calls_used,
        output_directories=[
            *result.output_directories,
            *result.watermark_output_directories,
        ],
        warnings=result.warnings,
        metrics={
            "discovered_urls": len(result.discovered_urls),
            "downloaded_items": result.downloaded_items,
            "artifact_count": result.artifact_count,
            "watermark_detected_count": result.watermark_detected_count,
            "watermark_processed_count": result.watermark_processed_count,
        },
        cancelled=result.cancelled,
        finish_reason="cancelled" if result.cancelled else "completed",
    )


def _harness_result(result: HarnessExecutionResult) -> AgentExecutionResult:
    return AgentExecutionResult(
        runtime="deepseek_harness",
        plan=result.plan,
        summary=result.response or "任务完成，但模型没有返回文字总结。",
        tool_calls=result.tool_calls,
        tool_calls_used=len(result.tool_calls),
        cancelled=result.cancelled,
        finish_reason=result.finish_reason,
    )
