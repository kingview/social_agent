"""Per-conversation execution owner. The view renders signals; the workspace sees a snapshot."""
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, Signal
from .agent_runtime import RuntimeRouter
from .conversation import ConversationCoordinator
from .conversation_workers import PlanWorker, ExecutionWorker


class ConversationPhase(str, Enum):
    IDLE = "就绪"
    PLANNING = "规划中"
    EXECUTING = "执行中"
    WAITING = "等待窗口"
    CANCELLING = "正在停止"
    CANCELLED = "已停止"
    COMPLETED = "已完成"
    PARTIAL = "未完成"
    FAILED = "失败"


@dataclass(frozen=True)
class ConversationViewState:
    conversation_id: str
    title: str
    status: str
    busy: bool


class ConversationController(QObject):
    changed = Signal()
    plan_ready = Signal(object)
    planning_status = Signal(str)
    planning_failed = Signal(str)
    planning_finished = Signal()
    execution_started = Signal()
    execution_progress = Signal(object)
    execution_succeeded = Signal(object)
    execution_failed = Signal(str)
    execution_finished = Signal()

    def __init__(self, *, output_root, registry_path, plugin_root, settings,
                 conversation_id=None, create_new=False, parent=None):
        super().__init__(parent)
        self.output_root = output_root
        self.registry_path = registry_path
        self.plugin_root = plugin_root
        self.settings = settings
        self.conversation = ConversationCoordinator(output_root / ".social-agent-state",
            conversation_id=conversation_id, create_new=create_new)
        self.router = self._new_router()
        self.plan_worker = None
        self.execution_worker = None
        self.active_turn_id = None
        self.pending_plan = None
        self.last_result = self.conversation.last_result()
        self.phase = ConversationPhase.IDLE
        self.percent = 0

    @property
    def conversation_id(self):
        return self.conversation.conversation_id

    @property
    def busy(self):
        return self.has_workers or self.active_turn_id is not None

    @property
    def has_workers(self):
        return self.plan_worker is not None or self.execution_worker is not None

    @property
    def view_state(self):
        turns = self.conversation.turns
        title = turns[0].user_message.replace("\n", " ")[:16] if turns else "新对话"
        status = self.phase.value
        if self.phase == ConversationPhase.EXECUTING:
            status += f" {self.percent}%"
        return ConversationViewState(self.conversation_id, title, status, self.busy)

    def _new_router(self):
        return RuntimeRouter(registry_path=self.registry_path, output_root=self.output_root,
                             conversation_id=self.conversation_id, settings=self.settings)

    def configure(self, settings):
        if self.busy:
            raise RuntimeError("任务运行期间不能更换模型。")
        self.router.close()
        self.settings = settings
        self.router = self._new_router()
        self.pending_plan = None

    def start_planning(self, message, *, session, available_sessions, attachment_paths):
        if self.busy:
            return
        context = self.conversation.context_for_next_turn()
        self.active_turn_id = self.conversation.begin_turn(message,
            attachment_names=[path.name for path in attachment_paths],
            session_ref=session.session_ref if session else None,
            platform=session.platform if session else None)
        self.phase = ConversationPhase.PLANNING
        worker = PlanWorker(message=message, session=session, available_sessions=available_sessions,
            attachment_paths=attachment_paths, context_summary=context, conversation_id=self.conversation_id,
            registry_path=self.registry_path, output_root=self.output_root, plugin_root=self.plugin_root,
            router=self.router, task_id=self.active_turn_id, parent=self)
        worker.status_changed.connect(self.report_planning)
        worker.succeeded.connect(self.accept_plan)
        worker.failed.connect(self.fail_planning)
        worker.finished.connect(self.finish_planning)
        self.plan_worker = worker
        self.changed.emit()
        worker.start()

    def report_planning(self, message):
        self.planning_status.emit(message)

    def accept_plan(self, plan):
        if self.execution_worker is not None:
            return
        self.pending_plan = plan
        if self.active_turn_id:
            self.conversation.mark_planned(self.active_turn_id, plan)
        self.plan_ready.emit(plan)

    def fail_planning(self, message):
        self.pending_plan = None
        self._fail("planning", message)
        self.planning_failed.emit(message)

    def finish_planning(self):
        worker, self.plan_worker = self.plan_worker, None
        if worker is not None:
            worker.deleteLater()
        self.planning_finished.emit()
        self.changed.emit()

    def start_execution(self):
        if self.pending_plan is None or self.execution_worker is not None:
            return
        plan, self.pending_plan = self.pending_plan, None
        if self.active_turn_id:
            self.conversation.mark_executing(self.active_turn_id)
        self.phase = ConversationPhase.EXECUTING
        self.percent = 0
        worker = ExecutionWorker(plan=plan, router=self.router, parent=self)
        worker.progress_changed.connect(self.report_execution)
        worker.succeeded.connect(self.accept_result)
        worker.failed.connect(self.fail_execution)
        worker.finished.connect(self.finish_execution)
        self.execution_worker = worker
        self.execution_started.emit()
        self.changed.emit()
        worker.start()

    def cancel(self):
        if self.execution_worker is None:
            return False
        self.phase = ConversationPhase.CANCELLING
        self.execution_worker.cancel_after_current_batch()
        self.changed.emit()
        return True

    def report_execution(self, event):
        self.percent = max(0, min(100, int(event.completed / max(event.total, 1) * 100)))
        if self.phase != ConversationPhase.CANCELLING:
            self.phase = ConversationPhase.WAITING if event.stage == "waiting_browser" else ConversationPhase.EXECUTING
        self.execution_progress.emit(event)
        self.changed.emit()

    def accept_result(self, result):
        self.last_result = result
        if self.active_turn_id:
            self.conversation.mark_succeeded(self.active_turn_id, result)
            self.active_turn_id = None
        self.phase = (ConversationPhase.CANCELLED if result.cancelled else
            ConversationPhase.COMPLETED if result.completion_status == "completed" else ConversationPhase.PARTIAL)
        self.execution_succeeded.emit(result)
        self.changed.emit()

    def _fail(self, stage, message):
        if self.active_turn_id:
            self.conversation.mark_failed(self.active_turn_id, stage=stage, error=message)
            self.active_turn_id = None
        self.phase = ConversationPhase.FAILED
        self.changed.emit()

    def fail_execution(self, message):
        self._fail("execution", message)
        self.execution_failed.emit(message)

    def finish_execution(self):
        worker, self.execution_worker = self.execution_worker, None
        if worker is not None:
            worker.deleteLater()
        self.execution_finished.emit()
        self.changed.emit()

    def new_conversation(self):
        if self.busy:
            raise RuntimeError("对话仍在运行。")
        self.router.close()
        self.conversation.new_conversation()
        self.router = self._new_router()
        self.pending_plan = self.last_result = None
        self.phase = ConversationPhase.IDLE
        self.changed.emit()

    def shutdown(self):
        if self.busy:
            raise RuntimeError("请先停止任务，再关闭对话。")
        self.router.close()
