import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget, QMainWindow
import pytest

from social_ops_agent import conversation_controller as module
from social_ops_agent.contracts import AgentProgress, AgentExecutionResult, DynamicAgentPlan
from social_ops_agent.settings import LLMSettings


class FakePlanWorker(QObject):
    status_changed = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, **kwargs):
        super().__init__(kwargs["parent"])
        self.arguments = kwargs
        self.started = False

    def start(self):
        self.started = True


class FakeExecutionWorker(QObject):
    progress_changed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, **kwargs):
        super().__init__(kwargs["parent"])
        self.arguments = kwargs
        self.cancelled = False

    def start(self):
        pass

    def cancel_after_current_batch(self):
        self.cancelled = True


@pytest.fixture
def controller(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(module, "PlanWorker", FakePlanWorker)
    monkeypatch.setattr(module, "ExecutionWorker", FakeExecutionWorker)
    controller = module.ConversationController(output_root=tmp_path, registry_path=tmp_path / "sessions.json",
        plugin_root=tmp_path / "plugins", settings=LLMSettings.from_env())
    yield controller
    controller.plan_worker = controller.execution_worker = controller.active_turn_id = None
    controller.shutdown()
    app.processEvents()


def start(controller):
    controller.start_planning("分析一个附件", session=None, available_sessions=(), attachment_paths=[])


def test_controller_runs_without_a_view_and_holds_busy_until_threads_finish(controller):
    assert not isinstance(controller, QWidget)
    start(controller)
    planner = controller.plan_worker
    assert controller.view_state.busy and planner.started
    controller.plan_ready.connect(lambda _plan: controller.start_execution())
    plan = DynamicAgentPlan(objective="分析", summary="分析", steps=["分析"])
    planner.succeeded.emit(plan)
    executor = controller.execution_worker
    assert executor is not None
    assert controller.conversation.turns[-1].status == "executing"
    executor.progress_changed.emit(AgentProgress(stage="waiting_browser", completed=0, total=1, message="等待窗口"))
    assert controller.view_state.status == "等待窗口"
    # Completing the execution before the planning thread's final signal is safe.
    executor.succeeded.emit(AgentExecutionResult(runtime="deepseek_harness", plan=plan, summary="完成"))
    executor.finished.emit()
    assert controller.busy
    planner.finished.emit()
    assert not controller.busy
    assert controller.view_state.status == "已完成"


def test_cancel_state_is_not_overwritten_by_late_progress(controller):
    controller.accept_plan(DynamicAgentPlan(objective="分析", summary="分析", steps=["分析"]))
    controller.start_execution()
    executor = controller.execution_worker
    assert controller.cancel() and executor.cancelled
    executor.progress_changed.emit(AgentProgress(stage="step", completed=0, total=1, message="延迟进度"))
    assert controller.view_state.status == "正在停止"
    with pytest.raises(RuntimeError):
        controller.shutdown()


def test_failed_plan_unblocks_after_finished_and_next_plan_gets_history(controller):
    start(controller)
    first = controller.plan_worker
    first.failed.emit("测试规划失败")
    assert controller.busy
    first.finished.emit()
    assert not controller.busy and controller.view_state.status == "失败"
    start(controller)
    assert "测试规划失败" in controller.plan_worker.arguments["context_summary"]


def test_busy_controller_does_not_replace_router_or_start_duplicate_plan(controller):
    start(controller)
    original = controller.plan_worker
    start(controller)
    assert controller.plan_worker is original
    with pytest.raises(RuntimeError):
        controller.configure(LLMSettings.from_env())
    with pytest.raises(RuntimeError):
        controller.new_conversation()


def test_workspace_panes_are_widgets_with_read_only_state(tmp_path):
    from social_ops_agent.conversation_workspace import ConversationWorkspace
    from social_ops_agent.settings import LLMSettingsStore
    from dataclasses import FrozenInstanceError
    app = QApplication.instance() or QApplication([])
    window = ConversationWorkspace(output_root=tmp_path, plugin_root=tmp_path / "plugins",
        registry_path=tmp_path / "registry.json", llm_settings_store=LLMSettingsStore(tmp_path / "llm.json"))
    try:
        pane = window.tabs.currentWidget()
        assert isinstance(pane, QWidget) and not isinstance(pane, QMainWindow)
        with pytest.raises(FrozenInstanceError):
            pane.view_state.busy = True
        original = window.workspace_path.stat().st_mtime_ns
        for _ in range(5):
            pane.controller.report_execution(AgentProgress(stage="step", completed=2, total=5, message="第 3 步"))
        assert window.workspace_path.stat().st_mtime_ns == original
        assert "40%" in window.tabs.tabText(0)
    finally:
        window.close()
        app.processEvents()
