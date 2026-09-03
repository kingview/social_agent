"""Worker threads only; no GUI widget ownership or cross-conversation state."""
from pathlib import Path
from PySide6.QtCore import QObject, QThread, Signal
from .agent_runtime import RuntimePlan, RuntimeRouter
from .contracts import AgentExecutionResult, DynamicAgentPlan
from .diagnostics import diagnostic_context, record_exception, register_secrets
from .multimodal import MultimodalInputError, prepare_multimodal_input
from .planner import PlanningError, SelectedSession
from .desktop_support import default_output_root

class PlanWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    status_changed = Signal(str)

    def __init__(
        self,
        *,
        message: str,
        session: SelectedSession | None,
        available_sessions: tuple[SelectedSession, ...],
        attachment_paths: list[Path],
        context_summary: str | None,
        conversation_id: str,
        registry_path: Path,
        output_root: Path,
        plugin_root: Path,
        router: RuntimeRouter,
        task_id: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._message = message
        self._session = session
        self._available_sessions = available_sessions
        self._attachment_paths = list(attachment_paths)
        self._context_summary = context_summary
        self._conversation_id = conversation_id
        self._registry_path = registry_path
        self._output_root = output_root
        self._plugin_root = plugin_root
        self._router = router
        self._task_id = task_id

    def run(self) -> None:
        with diagnostic_context(replace=True, task_id=self._task_id, conversation_id=self._conversation_id):
            self._run_with_context()

    def _run_with_context(self) -> None:
        try:
            if self._session is None and self._available_sessions:
                self.status_changed.emit(
                    f"正在根据任务从 {len(self._available_sessions)} 个比特浏览器窗口中自动选择…"
                )
            if self._attachment_paths:
                self.status_changed.emit("正在解析附件；视频和音频可能需要几分钟…")
            prepared = prepare_multimodal_input(
                self._attachment_paths,
                message=self._message,
                conversation_id=self._conversation_id,
                output_root=self._output_root,
                registry_path=self._registry_path,
                plugin_root=self._plugin_root,
                settings=self._router.settings,
            )
            self.status_changed.emit("Harness 正在结合附件和会话上下文生成计划…")
            plan = self._router.propose(
                self._message,
                self._session,
                available_sessions=self._available_sessions,
                attachments=prepared.attachments,
                media_context=prepared.media_context,
                context_summary=self._context_summary,
                task_id=self._task_id,
            )
        except (PlanningError, MultimodalInputError) as exc:
            record_exception("agent", "gui.planning", exc, state_root=self._output_root / ".social-agent-state", task_id=self._task_id)
            self.failed.emit(str(exc))
        except Exception as exc:
            register_secrets(getattr(getattr(self._router, "settings", None), "api_key", ""))
            record_exception("agent", "gui.planning", exc, state_root=self._output_root / ".social-agent-state", task_id=self._task_id)
            detail = str(exc).strip()
            self.failed.emit(
                detail
                or "无法生成执行计划，请确认 Harness、Node 24 与配置的模型服务正在运行。"
            )
        else:
            self.succeeded.emit(plan)


class ExecutionWorker(QThread):
    progress_changed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        plan: RuntimePlan,
        router: RuntimeRouter,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._plan = plan
        self._cancel_requested = False
        self._router = router

    def cancel_after_current_batch(self) -> None:
        self._cancel_requested = True
        self._router.cancel()

    def run(self) -> None:
        try:
            result = self._router.execute(
                self._plan,
                progress=self.progress_changed.emit,
            )
        except Exception as exc:
            if self._cancel_requested:
                self.succeeded.emit(
                    AgentExecutionResult(
                        runtime=(
                            "deepseek_harness"
                            if isinstance(self._plan, DynamicAgentPlan)
                            else "deterministic"
                        ),
                        plan=self._plan,
                        summary="任务已由用户停止。",
                        cancelled=True,
                        finish_reason="cancelled",
                    )
                )
                return
            register_secrets(getattr(getattr(self._router, "settings", None), "api_key", ""))
            record_exception("agent", "gui.execution", exc,
                state_root=Path(getattr(self._router, "output_root", default_output_root())) / ".social-agent-state",
                task_id=getattr(self._plan, "task_id", None))
            message = str(exc).strip() or "Agent 执行失败。"
            self.failed.emit(message)
        else:
            self.succeeded.emit(result)
