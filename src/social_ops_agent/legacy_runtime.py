from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Callable

from .contracts import (
    AgentAttachment,
    AgentExecutionResult,
    AgentPlan,
    AgentProgress,
    AgentRunResult,
    DynamicAgentPlan,
    RuntimeHealth,
)
from .planner import ConversationalPlanner, PlanningError, SelectedSession
from .policy import ExecutionPolicy
from .runtime import SocialOperationsAgent
from .settings import LLMSettings


class DeterministicAgentRuntime:
    """Legacy fixed-workflow adapter; the desktop router never selects it."""

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
        plan: AgentPlan | DynamicAgentPlan,
        *,
        progress: Callable[[AgentProgress], None] | None = None,
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
        return fixed_result(result)

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        return None

    def health(self) -> RuntimeHealth:
        return RuntimeHealth(
            runtime="deterministic",
            available=True,
            detail="确定性 Runtime 已就绪（仅兼容模式）",
        )


def fixed_result(result: AgentRunResult) -> AgentExecutionResult:
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
