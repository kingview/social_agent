import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from social_ops_agent.conversation import ConversationCoordinator
from social_ops_agent.conversation_workspace import ConversationWorkspace
from social_ops_agent.contracts import AgentProgress, AgentExecutionResult, DynamicAgentPlan
from social_ops_agent.settings import LLMSettingsStore


@pytest.fixture
def workspace(tmp_path):
    app = QApplication.instance() or QApplication([])
    options = dict(output_root=tmp_path / "output", plugin_root=tmp_path / "plugins",
        registry_path=tmp_path / "sessions.json",
        llm_settings_store=LLMSettingsStore(tmp_path / "llm.json"))
    window = ConversationWorkspace(**options)
    yield window, app, options
    for pane in window.panes()+list(window.background_panes.values()):
        pane.controller.plan_worker = pane.controller.execution_worker = pane.controller.active_turn_id = None
    window.close()
    app.processEvents()


@pytest.fixture
def history_menus(monkeypatch):
    from social_ops_agent import conversation_workspace as module
    menus = []

    class RecordingMenu(module.QMenu):
        def exec(self, *_args):
            menus.append(self)

    monkeypatch.setattr(module, "QMenu", RecordingMenu)
    return menus


def test_tabs_keep_independent_context_drafts_and_callbacks(workspace):
    window, app, _ = workspace
    one = window.tabs.currentWidget()
    one.message_input.setPlainText("对话一的草稿")
    one.controller.active_turn_id = one.controller.conversation.begin_turn("对话一分析机器人")
    one._set_planning(True)
    # Starting a second chat is allowed while the first chat is working.
    one.new_conversation()
    two = window.tabs.currentWidget()
    assert two is not one and two.controller.router is not one.controller.router
    assert one.controller.conversation_id != two.controller.conversation_id
    assert two.message_input.isEnabled() and two.send_button.isEnabled()
    two.message_input.setPlainText("对话二的草稿")
    two.controller.active_turn_id = two.controller.conversation.begin_turn("对话二分析风景")
    one.controller.report_execution(AgentProgress(stage="waiting_browser", completed=0, total=5, message="对话一等待窗口"))
    assert "对话一等待窗口" in one.chat.toPlainText()
    assert "对话一等待窗口" not in two.chat.toPlainText()
    plan = DynamicAgentPlan(objective="对话一分析机器人", summary="分析", steps=["分析"])
    one.controller.accept_result(AgentExecutionResult(runtime="deepseek_harness", plan=plan, summary="机器人分析完成"))
    assert "机器人分析完成" in one.chat.toPlainText()
    assert "机器人分析完成" not in two.chat.toPlainText()
    assert one.message_input.toPlainText() == "对话一的草稿"
    assert two.message_input.toPlainText() == "对话二的草稿"
    assert "机器人" not in two.controller.conversation.context_for_next_turn()
    assert "风景" not in one.controller.conversation.context_for_next_turn()
    window.tabs.setCurrentWidget(one)
    assert one.message_input.toPlainText() == "对话一的草稿"


def test_close_and_history_reopen_keeps_transcript(workspace):
    window, app, _ = workspace
    one = window.tabs.currentWidget()
    key = one.controller.conversation_id
    turn = one.controller.conversation.begin_turn("历史任务一")
    one.controller.conversation.mark_failed(turn, stage="execution", error="测试失败")
    two = window.open_conversation(create_new=True)
    window.close_conversation(window.tabs.indexOf(one))
    assert window.tabs.count() == 1
    restored = window.open_conversation(key)
    assert restored.controller.conversation.turns[0].user_message == "历史任务一"
    assert "历史任务一" in restored.chat.toPlainText()
    assert two.controller.conversation.turns == ()
    assert window.open_conversation(key) is restored and window.tabs.count() == 2


def test_history_hides_empty_tabs_and_unsent_drafts(workspace, history_menus):
    window, app, _ = workspace
    first = window.tabs.currentWidget()
    first_id = first.controller.conversation_id
    draft = window.open_conversation(create_new=True)
    draft.message_input.setPlainText("仅输入，尚未发送")
    window.close_conversation(window.tabs.indexOf(first))
    # Existing saved empty records stay readable for workspace restoration.
    assert first_id in {item.conversation_id for item in ConversationCoordinator.catalog(window.state_root)}
    window._show_history()
    actions = history_menus[-1].actions()
    assert len(actions) == 1
    assert actions[0].text() == "暂无历史对话" and not actions[0].isEnabled()
    assert draft.message_input.toPlainText() == "仅输入，尚未发送"


@pytest.mark.parametrize("status", ["planning", "failed", "cancelled"])
def test_history_includes_sent_commands_even_if_not_completed(workspace, history_menus, status):
    window, app, _ = workspace
    sent = window.tabs.currentWidget()
    conversation = sent.controller.conversation
    # Filter by submitted turns, not the title or execution outcome.
    turn = conversation.begin_turn("新对话")
    if status == "failed":
        conversation.mark_failed(turn, stage="planning", error="测试失败")
    elif status == "cancelled":
        conversation.mark_cancelled(turn, "测试取消")
    window.open_conversation(create_new=True)
    window.close_conversation(window.tabs.indexOf(sent))
    window._show_history()
    actions = history_menus[-1].actions()
    assert [action.text() for action in actions] == [f"新对话 · {conversation.conversation_id[-6:]}"]
    actions[0].trigger()
    restored = window.tabs.currentWidget()
    assert restored.controller.conversation_id == conversation.conversation_id
    assert restored.controller.conversation.turns[0].user_message == "新对话"


def test_restart_restores_tabs_and_selection(workspace):
    window, app, options = workspace
    one = window.tabs.currentWidget()
    one_id = one.controller.conversation_id
    two = window.open_conversation(create_new=True)
    two_id = two.controller.conversation_id
    window.tabs.setCurrentWidget(one)
    window.close()
    restored = ConversationWorkspace(**options)
    try:
        assert {pane.controller.conversation_id for pane in restored.panes()} == {one_id, two_id}
        assert restored.tabs.currentWidget().controller.conversation_id == one_id
    finally:
        restored.close()


def test_second_workspace_cannot_recover_running_conversation(workspace):
    window, _app, options = workspace
    one = window.tabs.currentWidget()
    one.controller.active_turn_id = one.controller.conversation.begin_turn("仍在运行的任务")
    with pytest.raises(RuntimeError, match="已打开"):
        ConversationWorkspace(**options)
    assert one.controller.conversation.turns[-1].status == "planning"


def test_cancel_is_scoped_and_shared_plugin_edits_disabled(workspace, monkeypatch):
    window, app, _ = workspace
    one = window.tabs.currentWidget()
    two = window.open_conversation(create_new=True)
    stopped = []
    one.controller.execution_worker = SimpleNamespace(cancel_after_current_batch=lambda: stopped.append("one"))
    two.controller.execution_worker = SimpleNamespace(cancel_after_current_batch=lambda: stopped.append("two"))
    window.refresh()
    assert not one.plugins_button.isEnabled() and not two.plugins_button.isEnabled()
    assert window.new_button.isEnabled()
    two.cancel_execution()
    assert stopped == ["two"]
    notices = []
    from social_ops_agent import conversation_workspace as module
    monkeypatch.setattr(module.QMessageBox, "information", lambda *args: notices.append(args))
    window.close_conversation(0)
    assert window.tabs.count() == 1 and not notices
    assert one.controller.conversation_id in window.background_panes
    assert window.open_conversation(one.controller.conversation_id) is one
    assert window.tabs.count() == 2


def test_conversation_files_do_not_overwrite_or_recover_each_other(tmp_path):
    one = ConversationCoordinator(tmp_path)
    turn_one = one.begin_turn("第一个对话")
    two = ConversationCoordinator(tmp_path, create_new=True)
    turn_two = two.begin_turn("第二个对话")
    two.mark_failed(turn_two, stage="planning", error="测试")
    assert one.turns[0].status == "planning"
    data = json.loads(one.path.read_text())
    assert data["turns"][0]["status"] == "planning"
    one.mark_failed(turn_one, stage="planning", error="独立测试")
    loaded_one = ConversationCoordinator(tmp_path, conversation_id=one.conversation_id)
    loaded_two = ConversationCoordinator(tmp_path, conversation_id=two.conversation_id)
    assert loaded_one.turns[0].user_message == "第一个对话"
    assert loaded_two.turns[0].user_message == "第二个对话"
    assert len(ConversationCoordinator.catalog(tmp_path)) == 2


def test_legacy_active_snapshot_is_migrated_without_losing_history(tmp_path):
    old = ConversationCoordinator(tmp_path)
    key = old.conversation_id
    turn = old.begin_turn("旧对话内容")
    old.mark_failed(turn, stage="planning", error="旧错误")
    (old.path.parent / "active.json").write_text(old.path.read_text(), encoding="utf-8")
    old.path.unlink()  # Fixture has only the legacy active.json.
    migrated = ConversationCoordinator(tmp_path)
    assert migrated.conversation_id == key and migrated.path.name == key + ".json"
    assert migrated.turns[0].user_message == "旧对话内容"
