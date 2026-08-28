from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Callable, Protocol, TypeAlias

from .contracts import (
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
        session: SelectedSession,
        previous_plan: AgentPlan | None = None,
    ) -> RuntimePlan: ...

    def execute(
        self,
        plan: RuntimePlan,
        *,
        progress: ProgressCallback | None = None,
    ) -> AgentExecutionResult: ...

    def cancel(self) -> None: ...

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
        session: SelectedSession,
        previous_plan: AgentPlan | None = None,
    ) -> AgentPlan:
        plan = ConversationalPlanner(
            ollama_base_url=self.settings.base_url,
            ollama_model=self.settings.model,
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
        session: SelectedSession,
        previous_plan: AgentPlan | None = None,
    ) -> DynamicAgentPlan:
        del previous_plan
        plan = self.backend.propose(message, session)
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

    def propose(
        self,
        message: str,
        session: SelectedSession,
        previous_plan: AgentPlan | None = None,
    ) -> RuntimePlan:
        try:
            self.policy.validate_message(message)
        except ExecutionPolicyError as exc:
            raise PlanningPolicyError(str(exc)) from exc
        if requires_dynamic_harness(message):
            return self._harness().propose(message, session, previous_plan)
        try:
            return self._deterministic().propose(message, session, previous_plan)
        except PlanningPolicyError:
            raise
        except PlanningError:
            return self._harness().propose(message, session, previous_plan)

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

    def _deterministic(self) -> DeterministicAgentRuntime:
        return DeterministicAgentRuntime(
            registry_path=self.registry_path,
            output_root=self.output_root,
            settings=self.settings,
            policy=self.policy,
        )

    def _harness(self) -> DeepSeekHarnessRuntime:
        return DeepSeekHarnessRuntime(
            registry_path=self.registry_path,
            output_root=self.output_root,
            conversation_id=self.conversation_id,
            settings=self.settings,
            policy=self.policy,
        )


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
