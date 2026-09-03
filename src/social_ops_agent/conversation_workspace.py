"""Multiple independent conversation views; no worker callbacks target the active tab."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QLockFile
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QMenu, QMessageBox, QPushButton, QTabWidget, QWidget

from .conversation import ConversationCoordinator
from .conversation_pane import ConversationPane
from .desktop_support import default_output_root
from .conversation_repository import WorkspaceStore, ConversationRepository
from .diagnostics import record_exception


class ConversationWorkspace(QMainWindow):
    def __init__(self, *, output_root: Path | None = None, **pane_options):
        super().__init__()
        self.setObjectName("conversationWorkspace")
        self.output_root = (output_root or default_output_root()).expanduser().resolve()
        self.state_root = self.output_root / ".social-agent-state"
        self.workspace_store = WorkspaceStore(self.state_root)
        self.workspace_path = self.workspace_store.path
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._workspace_lock = QLockFile(str(self.state_root / "desktop-workspace.lock"))
        self._workspace_lock.setStaleLockTime(0)
        if not self._workspace_lock.tryLock(0):
            raise RuntimeError("此数据目录的 SocialAgent 已打开，请在现有窗口中点击“新对话”。")
        self.pane_options = pane_options
        self._restoring = True
        self.tabs = QTabWidget()
        self.tabs.setObjectName("conversationTabs")
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_conversation)
        self.tabs.currentChanged.connect(self._selected)
        self.tabs.tabBar().tabMoved.connect(self._save)
        self.setCentralWidget(self.tabs)
        controls = QWidget()
        controls.setObjectName("conversationControls")
        row = QHBoxLayout(controls)
        row.setContentsMargins(6, 2, 6, 2)
        self.new_button = QPushButton("＋ 新对话")
        self.history_button = QPushButton("历史对话")
        for button in (self.new_button, self.history_button):
            button.setObjectName("secondaryButton")
            row.addWidget(button)
        self.new_button.clicked.connect(lambda: self.open_conversation(create_new=True))
        self.history_button.clicked.connect(self._show_history)
        self.tabs.setCornerWidget(controls, Qt.Corner.TopRightCorner)
        self.setWindowTitle("Social Agent · 多对话任务助手")
        self.resize(1180, 880)
        self.setMinimumSize(860, 710)
        ConversationRepository(self.state_root).migrate_legacy()
        state = self.workspace_store.load()
        available = {item.conversation_id for item in ConversationCoordinator.catalog(self.state_root)}
        for conversation_id in state.get("open_conversations", []):
            if isinstance(conversation_id, str) and conversation_id in available:
                self.open_conversation(conversation_id)
        if not self.tabs.count():
            self.open_conversation()
        selected = state.get("selected")
        for index in range(self.tabs.count()):
            if self.tabs.widget(index).view_state.conversation_id == selected:
                self.tabs.setCurrentIndex(index)
        self._restoring = False
        self._save()

    def panes(self):
        return [self.tabs.widget(index) for index in range(self.tabs.count())]

    @staticmethod
    def busy(pane) -> bool:
        return pane.view_state.busy

    def open_conversation(self, conversation_id=None, *, create_new=False):
        if conversation_id:
            for pane in self.panes():
                if pane.view_state.conversation_id == conversation_id:
                    self.tabs.setCurrentWidget(pane)
                    return pane
        pane = ConversationPane(output_root=self.output_root, conversation_id=conversation_id,
                          create_new=create_new, managed=True, **self.pane_options)
        pane.new_conversation_requested.connect(lambda: self.open_conversation(create_new=True))
        pane.conversation_changed.connect(self.refresh)
        self.tabs.addTab(pane, "新对话")
        self.tabs.setCurrentWidget(pane)
        self.refresh()
        return pane

    def refresh(self):
        panes = self.panes()
        any_busy = any(self.busy(pane) for pane in panes)
        for index, pane in enumerate(panes):
            state = pane.view_state
            self.tabs.setTabText(index, f"{state.title} · {state.status}")
            self.tabs.setTabToolTip(index, f"对话 {state.conversation_id[-6:]}\n{state.title}\n{state.status}")
            pane.set_shared_busy(any_busy)
        self._save()

    def _selected(self, _index):
        pane = self.tabs.currentWidget()
        if pane is not None and not self.busy(pane):
            pane.refresh_shared_state()
        self.refresh()

    def close_conversation(self, index):
        pane = self.tabs.widget(index)
        if pane is None:
            return
        if self.busy(pane):
            QMessageBox.information(self, "对话仍在运行", "切换标签页不会中断任务。请先停止此对话的任务，再关闭标签页。")
            return
        if pane.management_window_open:
            QMessageBox.information(self, "管理窗口已打开", "请先关闭此对话打开的浏览器管理窗口。")
            return
        pane.shutdown()
        self.tabs.removeTab(index)
        pane.deleteLater()
        if not self.tabs.count():
            self.open_conversation(create_new=True)
        self.refresh()

    def _show_history(self):
        menu = QMenu(self)
        for snapshot in ConversationCoordinator.catalog(self.state_root):
            title = snapshot.turns[0].user_message.replace("\n", " ")[:32] if snapshot.turns else "新对话"
            action = menu.addAction(f"{title} · {snapshot.conversation_id[-6:]}")
            action.triggered.connect(lambda _checked=False, key=snapshot.conversation_id: self.open_conversation(key))
        if menu.isEmpty():
            menu.addAction("暂无历史对话").setEnabled(False)
        menu.exec(self.history_button.mapToGlobal(self.history_button.rect().bottomLeft()))

    def _save(self, *_args):
        if self._restoring:
            return
        current = self.tabs.currentWidget()
        try:
            self.workspace_store.save([pane.view_state.conversation_id for pane in self.panes()],
                                      current.view_state.conversation_id if current else None)
        except OSError as exc:
            record_exception("agent", "gui.workspace_save", exc, state_root=self.state_root)

    def closeEvent(self, event):
        if any(self.busy(pane) for pane in self.panes()):
            QMessageBox.information(self, "仍有对话正在运行", "请先停止各对话中的任务，或等待任务完成后退出。")
            event.ignore()
            return
        self._save()
        for pane in self.panes():
            pane.shutdown()
        self._workspace_lock.unlock()
        event.accept()
