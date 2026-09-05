"""MaterialToolDialog presentation widget."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QFileDialog, QMessageBox, QListWidget, QListWidgetItem, QSpinBox, QAbstractItemView, QFrame, QScrollArea
from ..material_service import TOOLS
from ..session_store import SessionStore
from ..workspace_theme import MATERIAL_STYLE
from .controls import button, combo, error
from .task_panel import MaterialTaskPanel
from .tool_controller import ToolController
from .selection_dialog import PagedSelectionDialog
from .discovery_form import DiscoveryForm


class MaterialToolDialog(QDialog):
    def __init__(self, service, runner, tool, parent=None, *, task_center=None):
        super().__init__(parent)
        self.service, self.runner, self.tool = service, runner, tool
        self.setStyleSheet(MATERIAL_STYLE)
        self.setWindowTitle(TOOLS[tool])
        self.resize(920, 740)
        self.setMinimumSize(760, 620)
        layout = QHBoxLayout(self)
        form_area = QScrollArea()
        form_area.setWidgetResizable(True)
        form_area.setFrameShape(QFrame.Shape.NoFrame)
        form_body = QWidget()
        left = QVBoxLayout(form_body)
        form_area.setWidget(form_body)
        title = QLabel(TOOLS[tool])
        title.setObjectName('sectionTitle')
        left.addWidget(title)
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        left.addLayout(self.form)
        self.input = QPlainTextEdit()
        self.input.setMinimumHeight(120)
        self.input.setPlaceholderText('每行一个 URL 或本地文件路径')
        self.source = None
        if tool == 'discover':
            self.discovery = DiscoveryForm(service,self.input,self)
            left.addWidget(self.discovery)
        elif tool == 'download':
            self.session = combo([('匿名直接下载', None)] + [(f'{s.platform} · {s.profile_name}',s.session_ref) for s in SessionStore(service.registry_path).list()])
            self.form.addRow('浏览器会话',self.session)
            button('导入 TXT / CSV 链接', self.choose_links, left)
        else:
            self.source = combo([('本地文件 / 目录','local'),('下载任务结果','downloads')] if tool == 'import' else [('素材库已入库记录','library'),('本地文件 / 目录','local')])
            self.form.addRow('来源',self.source)
            self.source.currentIndexChanged.connect(self.source_changed)
            row = QHBoxLayout()
            self.files_button = button('选择文件',self.choose_files,row)
            self.directory_button = button('选择目录',self.choose_directory,row)
            self.records_button = button('选择记录',self.choose_records,row)
            left.addLayout(row)
            if tool == 'import':
                self.theme = combo([('不指定',None)] + [(s,s) for s in service.settings().themes])
                self.form.addRow('主题',self.theme)
        left.addWidget(self.input, 1)
        self.start = button('开始任务',self.start_job,left)
        self.start.setObjectName('primaryButton')
        self.notice = QLabel('关闭此窗口可隐藏任务；文件检查点和结果会保留。')
        self.notice.setWordWrap(True)
        left.addWidget(self.notice)
        layout.addWidget(form_area, 1)
        self.tasks = MaterialTaskPanel(service,runner,self,tool=tool,task_center=task_center)
        self.controller = ToolController(service,runner,tool,self.tasks.task_center)
        layout.addWidget(self.tasks, 1)
        if tool in {'import','analyze'}:
            self.source_changed()

    def source_changed(self):
        self.input.clear()
        local = self.source.currentData() == 'local'
        self.input.setReadOnly(not local)
        self.files_button.setEnabled(local)
        self.directory_button.setEnabled(local)
        self.records_button.setEnabled(not local)
        self.files_button.setVisible(local)
        self.directory_button.setVisible(local)
        self.records_button.setVisible(not local)

    def choose_links(self):
        path,_ = QFileDialog.getOpenFileName(self,'导入链接','','链接文件 (*.txt *.csv)')
        if path:
            try:
                self.input.setPlainText(self.controller.read_links(path))
            except Exception as exc:
                error(self,exc)

    def choose_files(self):
        paths,_ = QFileDialog.getOpenFileNames(self,'选择图片或视频')
        if paths:
            self.input.setPlainText('\n'.join(paths))

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self,'选择素材目录')
        if path:
            self.input.setPlainText(path)

    def choose_records(self):
        source = self.source.currentData()
        dialog = PagedSelectionDialog(
            lambda **kwargs:self.controller.record_page(source,**kwargs),
            self, title='选择记录',
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.input.setPlainText('\n'.join(dialog.values()))

    def start_job(self):
        try:
            raw = self.input.toPlainText().strip()
            options = {}
            if self.tool == 'discover':
                options = self.discovery.options()
            elif self.tool == 'download':
                options['session_ref'] = self.session.currentData()
            else:
                if self.tool == 'import': options['theme'] = self.theme.currentData()
            job_id = self.controller.start(raw,options)
            self.tasks.refresh()
            self.notice.setText('任务已创建：' + job_id[:8] + '。配置已快照保存，修改表单只影响下一任务。')
        except Exception as exc:
            error(self,exc)

    def closeEvent(self,event):
        try:
            running = self.controller.active_tasks()
        except Exception as exc:
            error(self,exc)
            event.ignore()
            return
        if not running:
            event.accept(); return
        box = QMessageBox(self)
        box.setWindowTitle('任务仍在运行')
        box.setText('可以隐藏窗口继续运行，或停止此工具的任务后关闭。')
        hide = box.addButton('隐藏，继续运行',QMessageBox.ButtonRole.AcceptRole)
        stop = box.addButton('停止并关闭',QMessageBox.ButtonRole.DestructiveRole)
        box.addButton('取消',QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == stop:
            try:
                self.controller.stop(running)
            except Exception as exc:
                error(self,exc)
                event.ignore()
                return
        if box.clickedButton() in (hide,stop): event.accept()
        else: event.ignore()
