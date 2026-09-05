import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTabBar, QToolButton, QWidget
import pytest

from social_ops_agent.conversation_controller import ConversationViewState
from social_ops_agent.conversation_tabs import ConversationTabs
from social_ops_agent.conversation_workspace import ConversationWorkspace
from social_ops_agent.desktop_support import STYLESHEET
from social_ops_agent.settings import LLMSettingsStore


@pytest.fixture
def workspace(tmp_path):
    app = QApplication.instance() or QApplication([])
    previous = app.styleSheet()
    app.setStyleSheet(STYLESHEET)
    window = ConversationWorkspace(output_root=tmp_path / "output", plugin_root=tmp_path / "plugins",
        registry_path=tmp_path / "registry.json", llm_settings_store=LLMSettingsStore(tmp_path / "llm.json"))
    yield window, app
    window.close()
    app.processEvents()
    app.setStyleSheet(previous)


@pytest.mark.parametrize("width,count", [(860, 1), (860, 10), (1180, 2), (1560, 12)])
def test_sidebar_controls_fit_and_conversations_scroll(workspace, width, count):
    window, app = workspace
    for _ in range(count - 1):
        window.open_conversation(create_new=True)
    window.resize(width, 880)
    window.show()
    window.tabs.setCurrentIndex(0)
    app.processEvents()
    assert not window.tabs.toolbar.isVisible()
    rail = window.new_button.parentWidget()
    for button in (window.new_button, window.history_button):
        top = button.mapTo(rail, QPoint(0, 0))
        bottom = button.mapTo(rail, button.rect().bottomRight())
        assert rail.rect().contains(top) and rail.rect().contains(bottom)
        assert button.height() >= 40
        assert not button.visibleRegion().isEmpty()
        assert button.visibleRegion().boundingRect().contains(button.rect())
    assert window.conversation_list.count() == count
    last = window.conversation_list.item(count - 1)
    window.conversation_list.scrollToItem(last)
    app.processEvents()
    assert window.conversation_list.viewport().rect().intersects(window.conversation_list.visualItemRect(last))
    window.conversation_list.itemClicked.emit(last)
    assert window.tabs.currentWidget().view_state.conversation_id == last.data(Qt.ItemDataRole.UserRole)


def test_close_button_is_on_right_and_follows_dragged_tab(workspace):
    window, app = workspace
    first = window.tabs.currentWidget()
    second = window.open_conversation(create_new=True)
    third = window.open_conversation(create_new=True)
    window.tabs.setCurrentWidget(first)
    window.show()
    app.processEvents()
    bar = window.tabs.tabBar()
    for index in range(3):
        close = bar.tabButton(index, QTabBar.ButtonPosition.RightSide)
        assert close is not None
        assert bar.tabButton(index, QTabBar.ButtonPosition.LeftSide) is None
        assert close.geometry().center().x() > bar.tabRect(index).center().x()
    bar.moveTab(0, 2)
    app.processEvents()
    assert window.tabs.currentWidget() is first
    assert window.tabs.widget(0) is second and window.tabs.widget(1) is third
    state = json.loads(window.workspace_path.read_text())
    assert state["selected"] == first.view_state.conversation_id
    assert state["open_conversations"][-1] == first.view_state.conversation_id
    QTest.mouseClick(bar.tabButton(2, QTabBar.ButtonPosition.RightSide), Qt.MouseButton.LeftButton)
    app.processEvents()
    assert window.tabs.indexOf(first) == -1 and window.tabs.count() == 2
    assert window.tabs.currentWidget().isVisible()


def test_keyboard_switch_updates_visible_page_and_selection(workspace):
    window, app = workspace
    first = window.tabs.currentWidget()
    second = window.open_conversation(create_new=True)
    window.tabs.setCurrentWidget(first)
    window.show()
    app.processEvents()
    bar = window.tabs.tabBar()
    bar.setFocus()
    QTest.keyClick(bar, Qt.Key.Key_Right)
    app.processEvents()
    assert window.tabs.currentWidget() is second
    assert second.isVisible() and not first.isVisible()


def test_status_changes_do_not_resize_tabs_or_drop_accessible_context():
    app = QApplication.instance() or QApplication([])
    tabs = ConversationTabs()
    index = tabs.addTab(QWidget(), "new")
    tabs.resize(600, 300)
    tabs.show()
    app.processEvents()
    for status in ("就绪", "执行中 40%", "等待窗口", "正在停止", "未完成"):
        tabs.update_conversation(index, ConversationViewState("conversation-test", "很长的对话标题" * 5, status, status != "就绪"))
        app.processEvents()
        assert tabs.tabBar().tabRect(index).width() == 248
        assert status in tabs.tabText(index)
        assert status in tabs.tabBar().tabToolTip(index)
    tabs.close()
