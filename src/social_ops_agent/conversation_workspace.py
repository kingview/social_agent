"""Multiple independent conversation views; no worker callbacks target the active tab."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QMainWindow, QMenu, QMessageBox, QPushButton, QWidget, QHBoxLayout, QVBoxLayout, QStackedWidget, QSplitter, QFrame, QLabel, QListWidget, QListWidgetItem, QScrollArea, QDialog
from PySide6.QtCore import Qt

from .conversation import ConversationCoordinator
from .conversation_pane import ConversationPane
from .conversation_tabs import ConversationTabs
from .desktop_support import default_output_root
from .conversation_repository import WorkspaceStore, ConversationRepository
from .diagnostics import record_exception
from .material_service import MaterialService
from .material_jobs import MaterialRunner
from .material_desktop import MaterialTaskPanel, MaterialToolbox, MaterialSettingsDialog
from .task_center import TaskCenter
from .material_task_state import RESUMABLE
from .conversation_controller import ConversationPhase


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
        self.background_panes = {}
        self.tabs = ConversationTabs()
        self.tabs.tabCloseRequested.connect(self.close_conversation)
        self.tabs.currentChanged.connect(self._selected)
        self.tabs.tabBar().tabMoved.connect(self._save)
        self.material_service = MaterialService(self.output_root, self.state_root,
            registry_path=pane_options.get('registry_path'), plugin_root=pane_options.get('plugin_root'))
        self.material_service.recover_interrupted()
        self.material_runner = MaterialRunner(self.material_service.jobs, self.material_service.handle,
            concurrency=self.material_service.settings().max_concurrency)
        self.task_center = TaskCenter(self.material_service, self.material_runner,
            self.command_agent_task, agent_actions=self.agent_task_actions)
        shell = QWidget()
        shell.setObjectName('workspaceShell')
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        rail = QWidget()
        rail.setObjectName('workspaceRail')
        rail.setFixedWidth(194)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(15,12,15,8)
        navigation.setSpacing(8)
        brand=QLabel('◈  Social Agent'); brand.setObjectName('workspaceBrand'); navigation.addWidget(brand)
        self.workbench_button = QPushButton('工作台')
        self.toolbox_button = QPushButton('工具箱')
        for control in (self.workbench_button,self.toolbox_button):
            control.setCheckable(True)
            control.setFixedHeight(42)
            navigation.addWidget(control)
        self.workbench_button.setChecked(True)
        self.navigation_layout = navigation
        shell_layout.addWidget(rail)
        content=QVBoxLayout(); content.setContentsMargins(0,0,0,0); content.setSpacing(0)
        header=QFrame(); header.setObjectName('workspaceHeader')
        header_layout=QHBoxLayout(header); header_layout.setContentsMargins(26,14,24,14)
        self.heading=QLabel('工作台'); self.heading.setObjectName('workspaceHeading'); header_layout.addWidget(self.heading)
        header_layout.addStretch()
        self.notifications_button=QPushButton('任务动态'); self.notifications_button.setObjectName('secondaryButton')
        self.settings_button=QPushButton('系统设置'); self.settings_button.setObjectName('secondaryButton')
        header_layout.addWidget(self.notifications_button); header_layout.addWidget(self.settings_button)
        content.addWidget(header)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.tabs)
        self.toolbox = MaterialToolbox(self.material_service,self.material_runner,self,task_center=self.task_center)
        toolbox_scroll = QScrollArea()
        toolbox_scroll.setWidgetResizable(True)
        toolbox_scroll.setFrameShape(QFrame.Shape.NoFrame)
        toolbox_scroll.setWidget(self.toolbox)
        self.pages.addWidget(toolbox_scroll)
        self.task_panel = MaterialTaskPanel(self.material_service,self.material_runner,self,task_center=self.task_center)
        self.task_panel.conversation_requested.connect(self.show_task_conversation)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.pages)
        self.splitter.addWidget(self.task_panel)
        self.splitter.setStretchFactor(0,3)
        self.splitter.setStretchFactor(1,1)
        self.splitter.setSizes([760,300])
        content.addWidget(self.splitter,1)
        shell_layout.addLayout(content,1)
        self.workbench_button.clicked.connect(lambda:self.pages.setCurrentIndex(0))
        self.toolbox_button.clicked.connect(lambda:self.pages.setCurrentIndex(1))
        self.pages.currentChanged.connect(self.page_changed)
        self.settings_button.clicked.connect(self.show_settings)
        self.notifications_button.clicked.connect(self.show_attention)
        self.setCentralWidget(shell)
        self.new_button = QPushButton("＋ 新对话")
        self.history_button = QPushButton("历史对话")
        for button in (self.new_button, self.history_button):
            button.setObjectName("conversationAction")
            button.setFixedHeight(42)
            navigation.addWidget(button)
        self.tabs.toolbar.hide()
        caption=QLabel('打开的对话'); caption.setObjectName('railCaption'); navigation.addWidget(caption)
        self.conversation_list=QListWidget(); self.conversation_list.setObjectName('railConversations')
        self.conversation_list.itemClicked.connect(lambda item:self.show_task_conversation(item.data(Qt.ItemDataRole.UserRole)))
        self.conversation_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.conversation_list.customContextMenuRequested.connect(self.conversation_menu)
        navigation.addWidget(self.conversation_list,1)
        profile=QLabel('●  本地工作空间'); profile.setObjectName('railProfile'); navigation.addWidget(profile)
        self.new_button.clicked.connect(lambda: self.open_conversation(create_new=True))
        self.history_button.clicked.connect(self._show_history)
        self.setWindowTitle("Social Agent · 多对话任务助手")
        self.resize(1320, 880)
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

    def show_task_conversation(self, conversation_id):
        self.open_conversation(conversation_id)
        self.pages.setCurrentIndex(0)

    def _task_pane(self, row):
        return next((pane for pane in self.panes() + list(self.background_panes.values())
            if pane.view_state.conversation_id == row.get('conversation_id')), None)

    def agent_task_actions(self, row):
        pane = self._task_pane(row)
        if pane and pane.controller.busy:
            controller = pane.controller
            if (controller.active_turn_id == row['task_id'] and controller.execution_worker is not None
                    and controller.phase != ConversationPhase.CANCELLING):
                return ['stop']
            return []
        # Harness continuation is not a native resumable worker/pause operation.
        return ['resume'] if row['state'] in RESUMABLE and row.get('conversation_id') else []

    def command_agent_task(self, row, command):
        if command not in self.agent_task_actions(row):
            raise ValueError('任务状态已变化，当前无法执行该操作')
        if command == 'stop':
            self._task_pane(row).cancel_execution()
        elif command == 'resume':
            pane = self.open_conversation(row['conversation_id'])
            pane.resume_task(row['task_id'])

    def page_changed(self,index):
        self.heading.setText('工作台' if index==0 else '工具箱')
        self.workbench_button.setChecked(index==0)
        self.toolbox_button.setChecked(index==1)
        self.task_panel.setVisible(index==0 and self.width()>=1160)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'task_panel'):
            self.task_panel.setVisible(self.pages.currentIndex()==0 and event.size().width()>=1160)

    def show_attention(self):
        if self.width()<1160:
            dialog=QDialog(self)
            dialog.setWindowTitle('任务管理')
            dialog.resize(520,700)
            layout=QVBoxLayout(dialog)
            panel=MaterialTaskPanel(self.material_service,self.material_runner,dialog,task_center=self.task_center)
            panel.conversation_requested.connect(lambda key:(dialog.accept(),self.show_task_conversation(key)))
            layout.addWidget(panel)
            dialog.exec()
            dialog.deleteLater()
            return
        self.pages.setCurrentIndex(0)
        self.task_panel.filter.setCurrentIndex(2)
        self.task_panel.refresh()

    def show_settings(self):
        menu=QMenu(self)
        menu.addAction('素材工作流设置',lambda:MaterialSettingsDialog(self.material_service,self).exec())
        pane=self.tabs.currentWidget()
        if pane:
            menu.addAction('模型来源与远端 LLM',pane.manage_model_settings)
            menu.addAction('Tool 插件',pane.manage_plugins)
            menu.addAction('管理浏览器窗口',pane.manage_sessions)
        menu.exec(self.settings_button.mapToGlobal(self.settings_button.rect().bottomLeft()))

    def conversation_menu(self,point):
        item=self.conversation_list.itemAt(point)
        if not item:return
        key=item.data(Qt.ItemDataRole.UserRole)
        index=next((i for i,p in enumerate(self.panes()) if p.view_state.conversation_id==key),None)
        if index is None:return
        menu=QMenu(self); menu.addAction('关闭对话（运行任务转入后台）',lambda:self.close_conversation(index))
        menu.exec(self.conversation_list.mapToGlobal(point))

    @staticmethod
    def busy(pane) -> bool:
        return pane.view_state.busy

    def open_conversation(self, conversation_id=None, *, create_new=False):
        if hasattr(self,'pages'):
            self.pages.setCurrentIndex(0)
        if conversation_id:
            for pane in self.panes():
                if pane.view_state.conversation_id == conversation_id:
                    self.tabs.setCurrentWidget(pane)
                    return pane
            if conversation_id in self.background_panes:
                pane=self.background_panes.pop(conversation_id)
                self.tabs.addTab(pane,pane.view_state.title)
                self.tabs.setCurrentWidget(pane); self.refresh()
                return pane
        pane = ConversationPane(output_root=self.output_root, conversation_id=conversation_id,
                          create_new=create_new, managed=True, **self.pane_options)
        pane.new_conversation_requested.connect(lambda: self.open_conversation(create_new=True))
        pane.conversation_changed.connect(self.refresh)
        pane.material_tool_requested.connect(self.toolbox.open_tool)
        self.tabs.addTab(pane, "新对话")
        self.tabs.setCurrentWidget(pane)
        self.refresh()
        return pane

    def refresh(self):
        panes = self.panes()
        any_busy = any(self.busy(pane) for pane in panes+list(self.background_panes.values()))
        for index, pane in enumerate(panes):
            state = pane.view_state
            self.tabs.update_conversation(index, state)
            pane.set_shared_busy(any_busy)
        if hasattr(self,'conversation_list'):
            existing = {self.conversation_list.item(i).data(Qt.ItemDataRole.UserRole): self.conversation_list.item(i)
                        for i in range(self.conversation_list.count())}
            active_ids = {pane.view_state.conversation_id for pane in self.panes()}
            for key,item in list(existing.items()):
                if key not in active_ids:
                    self.conversation_list.takeItem(self.conversation_list.row(item))
            for index,pane in enumerate(panes):
                state=pane.view_state
                item=existing.get(state.conversation_id)
                if item is None:
                    item=QListWidgetItem()
                    self.conversation_list.insertItem(index,item)
                elif self.conversation_list.row(item) != index:
                    self.conversation_list.takeItem(self.conversation_list.row(item))
                    self.conversation_list.insertItem(index,item)
                item.setText(f'{state.title[:16]}\n{state.status}')
                item.setToolTip(state.title)
                item.setData(Qt.ItemDataRole.UserRole,state.conversation_id)
                if pane is self.tabs.currentWidget(): self.conversation_list.setCurrentItem(item)
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
            self.background_panes[pane.view_state.conversation_id]=pane
            self.tabs.removeTab(index)
            if not self.tabs.count():self.open_conversation(create_new=True)
            self.refresh()
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
            # Empty tabs are persisted for workspace restoration, not history.
            if not snapshot.turns:
                continue
            title = snapshot.turns[0].user_message.replace("\n", " ")[:32]
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
        if any(self.busy(pane) for pane in self.panes()+list(self.background_panes.values())):
            QMessageBox.information(self, "仍有对话正在运行", "请先停止各对话中的任务，或等待任务完成后退出。")
            event.ignore()
            return
        if self.material_runner.active:
            QMessageBox.information(self, '仍有素材任务正在运行', '请在任务管理中暂停或停止任务，等当前项目完成后退出；已完成结果会保留。')
            event.ignore()
            return
        self._save()
        for pane in self.panes()+list(self.background_panes.values()):
            pane.shutdown()
        self._workspace_lock.unlock()
        self.material_runner.close()
        event.accept()
