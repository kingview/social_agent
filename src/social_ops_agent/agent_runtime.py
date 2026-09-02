from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, TypeAlias

from .contracts import (
    AgentAttachment,
    AgentExecutionResult,
    AgentPlan,
    AgentProgress,
    DynamicAgentPlan,
    RuntimeHealth,
)
from .harness_backend import (
    DeepSeekHarnessBackend,
    HarnessExecutionResult,
)
from .legacy_runtime import DeterministicAgentRuntime, fixed_result as _fixed_result
from .planner import (
    PlanningPolicyError,
    SelectedSession,
)
from .policy import DEFAULT_EXECUTION_POLICY, ExecutionPolicy, ExecutionPolicyError
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
        return self._harness().propose(
            message,
            session,
            previous_plan,
            attachments=attachments,
            media_context=media_context,
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
