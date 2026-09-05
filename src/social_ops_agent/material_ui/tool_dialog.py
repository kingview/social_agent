"""MaterialToolDialog presentation widget."""
from __future__ import annotations

from functools import partial
from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QFileDialog, QMessageBox, QPushButton, QAbstractSpinBox, QComboBox, QFrame, QScrollArea
from ..diagnostics import record_exception
from ..material_preparation import PreparationControl, PreparationCancelled
from ..material_service import TOOLS
from ..session_store import SessionStore
from ..workspace_theme import MATERIAL_STYLE
from .controls import button, combo, error
from .task_panel import MaterialTaskPanel
from .tool_controller import ToolController
from .selection_dialog import PagedSelectionDialog
from .discovery_form import DiscoveryForm
from .background import BackgroundCall


def report_preparation_error(state_root,exc):
    if not isinstance(exc,PreparationCancelled):
        record_exception('agent','materials.prepare',exc,state_root=state_root)


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
        self.form_body = form_body
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
        self.cancel_prepare = button('取消准备',self.cancel_preparation,left)
        self.cancel_prepare.hide()
        self.notice = QLabel('关闭此窗口可隐藏任务；文件检查点和结果会保留。')
        self.notice.setWordWrap(True)
        left.addWidget(self.notice)
        layout.addWidget(form_area, 1)
        self.tasks = MaterialTaskPanel(service,runner,self,tool=tool,task_center=task_center)
        self.controller = ToolController(service,runner,tool,self.tasks.task_center)
        self.preparation = None
        self.prepare_call = BackgroundCall(self,error_reporter=partial(report_preparation_error,service.state_root))
        self.prepare_call.succeeded.connect(self.prepared)
        self.prepare_call.failed.connect(self.preparation_failed)
        self.prepare_timer = QTimer(self)
        self.prepare_timer.setInterval(150)
        self.prepare_timer.timeout.connect(self.preparation_progress)
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
        if self.prepare_call.busy:
            return
        try:
            raw = self.input.toPlainText().strip()
            options = {}
            if self.tool == 'discover':
                options = self.discovery.options()
            elif self.tool == 'download':
                options['session_ref'] = self.session.currentData()
            else:
                if self.tool == 'import': options['theme'] = self.theme.currentData()
            preparation = PreparationControl()
            operation = partial(self.controller.start,raw,options,preparation=preparation)
            self.preparation = preparation
            self._preparing_controls = [(control,control.isEnabled()) for control in self.form_body.findChildren(QWidget)
                if isinstance(control,(QPushButton,QComboBox,QLineEdit,QPlainTextEdit,QAbstractSpinBox)) and control is not self.cancel_prepare]
            for control,_ in self._preparing_controls: control.setEnabled(False)
            self.cancel_prepare.setEnabled(True); self.cancel_prepare.show()
            self.notice.setText('正在后台检查输入，准备创建任务…')
            self.start.setText('准备中…')
            # Only thread-safe control data is captured, not the view itself.
            self._cancel_on_destroy = lambda *_:preparation.cancel()
            self.destroyed.connect(self._cancel_on_destroy)
            self.prepare_call.start(operation)
            self.prepare_timer.start()
        except Exception as exc:
            error(self,exc)

    def preparation_progress(self):
        if self.preparation and self.cancel_prepare.isEnabled():
            self.notice.setText(f'正在后台准备，已找到 {self.preparation.count} 个有效输入…')

    def cancel_preparation(self):
        if self.preparation:
            cancelled = self.preparation.cancel()
            self.cancel_prepare.setEnabled(False)
            self.notice.setText('正在取消准备…' if cancelled else '已进入创建阶段；创建后可从任务管理停止。')

    def finish_preparation(self):
        self.prepare_timer.stop()
        self.destroyed.disconnect(self._cancel_on_destroy)
        self.cancel_prepare.hide()
        self.start.setText('开始任务')
        for control,enabled in self._preparing_controls: control.setEnabled(enabled)
        self.preparation = None

    @Slot(object)
    def prepared(self,job_id):
        self.finish_preparation()
        self.tasks.refresh()
        self.notice.setText('任务已创建：' + job_id[:8] + '。配置已快照保存，修改表单只影响下一任务。')

    @Slot(object)
    def preparation_failed(self,exc):
        self.finish_preparation()
        self.notice.setText(str(exc) if isinstance(exc,PreparationCancelled) else '任务创建失败，详情已写入日志。')
        if self.isVisible() and not isinstance(exc,PreparationCancelled):
            QMessageBox.warning(self,'任务未创建',str(exc))

    def closeEvent(self,event):
        if self.prepare_call.busy:
            self.cancel_preparation()
            event.accept()
            return
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
