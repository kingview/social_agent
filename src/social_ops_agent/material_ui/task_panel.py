"""Task presentation backed by the shared TaskCenter facade."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QPlainTextEdit, QMessageBox, QListWidget, QListWidgetItem, QProgressBar,
)

from ..diagnostics import record_exception
from ..workspace_theme import MATERIAL_STYLE
from .controls import button, combo, error


class MaterialTaskPanel(QWidget):
    """Render small task summaries and load only the selected task's details."""

    conversation_requested = Signal(str)
    PAGE_SIZE = 100

    def __init__(self, service, runner, parent=None, *, tool=None, task_center=None):
        super().__init__(parent)
        if task_center is None:
            from ..task_center import TaskCenter
            task_center = TaskCenter(service, runner)
        self.service, self.runner, self.tool = service, runner, tool
        self.task_center = task_center
        self._offset = 0
        self._signature = None
        self._rows = {}
        self._detail_row = None
        self._detail_revision = None
        self._refresh_error = None
        self.setObjectName("materialTasks")
        self.setStyleSheet(MATERIAL_STYLE)
        self.setMinimumWidth(280)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 20, 16, 16)
        heading = QLabel("任务管理")
        heading.setObjectName("taskHeading")
        layout.addWidget(heading)
        self.filter = combo([
            ("全部任务", None), ("进行中", "active"),
            ("待处理", "attention"), ("已结束", "ended"),
        ])
        self.filter.hide()
        segments = QHBoxLayout()
        segments.setSpacing(4)
        self.filter_buttons = []
        for index, title in enumerate(("全部", "进行中", "待处理", "已结束")):
            control = QPushButton(title)
            control.setObjectName("taskSegment")
            control.setCheckable(True)
            control.clicked.connect(
                lambda _=False, value=index: self.filter.setCurrentIndex(value)
            )
            segments.addWidget(control)
            self.filter_buttons.append(control)
        layout.addLayout(segments)
        self.search = QLineEdit()
        self.search.setPlaceholderText("按工具、名称或任务 ID 搜索")
        layout.addWidget(self.search)
        self.list = QListWidget()
        layout.addWidget(self.list, 2)
        pages = QHBoxLayout()
        self.previous_page = button("上一页", lambda: self.change_page(-1), pages)
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pages.addWidget(self.page_label, 1)
        self.next_page = button("下一页", lambda: self.change_page(1), pages)
        layout.addLayout(pages)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择任务查看每个项目的结果、问题和输出目录")
        layout.addWidget(self.detail, 2)
        self.technical = QPushButton("详细诊断数据")
        self.technical.setCheckable(True)
        self.technical.toggled.connect(self.selected)
        layout.addWidget(self.technical)
        row = QHBoxLayout()
        self.pause = button("暂停", lambda: self.command("pause"), row)
        self.resume = button("继续 / 重试", lambda: self.command("resume"), row)
        self.stop = button("停止", lambda: self.command("stop"), row)
        layout.addLayout(row)
        self.open_output = button("打开输出目录", self.show_output, layout)
        self.open_conversation = button("查看任务对话", self.show_conversation, layout)
        self.list.currentItemChanged.connect(self.selected)
        self.filter.currentIndexChanged.connect(self.reset_page)
        self.search.textChanged.connect(self.reset_page)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def current(self):
        item = self.list.currentItem()
        task_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if self._detail_row and self._detail_row["id"] == task_id:
            return self._detail_row
        return self._rows.get(task_id)

    def reset_page(self, *_):
        self._offset = 0
        self.refresh()

    def change_page(self, direction):
        self._offset = max(0, self._offset + direction * self.PAGE_SIZE)
        self.refresh()

    def refresh(self, *_):
        for index, control in enumerate(self.filter_buttons):
            control.setChecked(index == self.filter.currentIndex())
        try:
            rows = self.task_center.list(
                tool=self.tool, group=self.filter.currentData(),
                query=self.search.text().strip(), limit=self.PAGE_SIZE,
                offset=self._offset,
            )
            if not rows and self._offset:
                self._offset = max(0, self._offset - self.PAGE_SIZE)
                return self.refresh()
            self._refresh_error = None
        except Exception as exc:
            # A transient read failure should not create a modal dialog every second.
            if str(exc) != self._refresh_error:
                record_exception("agent", "materials.tasks.refresh", exc,
                                 state_root=self.service.state_root)
                self._refresh_error = str(exc)
            self.detail.setPlainText("暂时无法读取任务列表，稍后自动重试。")
            return
        selected = self.list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self._rows = {row["id"]: row for row in rows}
        signature = [
            (row["id"], row["state"], row["completed"], row["total"],
             row.get("updated_at"), row.get("command"),
             tuple(row.get("actions", ())))
            for row in rows
        ]
        if signature != self._signature:
            self._signature = signature
            self.list.blockSignals(True)
            self.list.clear()
            for row in rows:
                label = (
                    f'{row["name"]} · {row["state"]}\n'
                    f'{row["completed"]}/{row["total"]} · {row["id"][:8]}'
                )
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                self.list.addItem(item)
                if row["id"] == selected_id:
                    self.list.setCurrentItem(item)
            if self.list.currentRow() < 0 and self.list.count():
                self.list.setCurrentRow(0)
            self.list.blockSignals(False)
        paginated = self._offset > 0 or len(rows) == self.PAGE_SIZE
        for widget in (self.previous_page, self.next_page, self.page_label):
            widget.setVisible(paginated)
        self.previous_page.setEnabled(self._offset > 0)
        self.next_page.setEnabled(len(rows) == self.PAGE_SIZE)
        self.page_label.setText(f"第 {self._offset // self.PAGE_SIZE + 1} 页")
        self.selected()

    def selected(self, *_):
        item = self.list.currentItem()
        task_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        summary = self._rows.get(task_id)
        if summary:
            revision = (
                task_id, summary.get("updated_at"), summary["state"],
                summary["completed"], summary["total"], summary.get("command"),
                tuple(summary.get("actions", ())),
            )
            if revision != self._detail_revision:
                try:
                    self._detail_row = self.task_center.get(task_id)
                    self._detail_revision = revision
                except (KeyError, ValueError):
                    self._detail_row = None
            row = self._detail_row
        else:
            row = self._detail_row = None
            self._detail_revision = None
        controls = (
            self.pause, self.resume, self.stop, self.open_output,
            self.open_conversation, self.technical,
        )
        for control in controls:
            control.setVisible(bool(row))
            control.setEnabled(bool(row))
        if not row:
            self.progress.setValue(0)
            self.detail.clear()
            return
        actions = set(row.get("actions", ()))
        self.pause.setEnabled("pause" in actions)
        self.resume.setEnabled(bool(actions.intersection({"resume", "retry"})))
        self.stop.setEnabled("stop" in actions)
        self.open_conversation.setVisible(bool(row.get("conversation_id")))
        self.open_output.setVisible(self.task_center.output_directory(row) is not None)
        self.progress.setValue(int(100 * row["completed"] / max(1, row["total"])))
        description = self.task_center.describe(row, technical=self.technical.isChecked())
        if description != self.detail.toPlainText():
            scroll = self.detail.verticalScrollBar().value()
            self.detail.setPlainText(description)
            self.detail.verticalScrollBar().setValue(scroll)

    def command(self, command):
        row = self.current()
        if not row:
            return
        actions = set(row.get("actions", ()))
        if command == "resume" and command not in actions and "retry" in actions:
            command = "retry"
        if command not in actions:
            return
        try:
            self.task_center.command(row["id"], command)
            self._detail_revision = None
            self.refresh()
        except Exception as exc:
            error(self, exc)

    def show_conversation(self):
        row = self.current()
        if row and row.get("conversation_id"):
            self.conversation_requested.emit(row["conversation_id"])

    def show_output(self):
        row = self.current()
        path = self.task_center.output_directory(row) if row else None
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "输出目录", "当前没有可打开的输出文件。")
