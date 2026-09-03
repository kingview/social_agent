from __future__ import annotations

from .diagnostics import record_exception

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .plugins import PluginManager, PluginRecord


class InstallPluginWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, manager: PluginManager, archive: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.archive = archive

    def run(self) -> None:
        try:
            record = self.manager.install(self.archive)
        except Exception as exc:
            record_exception("agent", "plugin_desktop.handled", exc)
            self.failed.emit(str(exc) or type(exc).__name__)
        else:
            self.succeeded.emit(record)


class PluginManagerDialog(QDialog):
    plugins_changed = Signal()

    def __init__(self, manager: PluginManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._worker: InstallPluginWorker | None = None
        self.setWindowTitle("Tool 插件管理")
        self.resize(760, 480)

        layout = QVBoxLayout(self)
        title = QLabel("Tool 插件")
        title.setObjectName("dialogTitle")
        copy = QLabel("插件安装在用户数据目录中，独立升级；Agent App 本体不再携带大型 AI 和浏览器依赖。")
        copy.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(copy)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["插件", "版本", "状态", "能力"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 80)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.install_button = QPushButton("安装 .socialtool")
        self.install_button.clicked.connect(self.install_plugin)
        self.toggle_button = QPushButton("禁用")
        self.toggle_button.clicked.connect(self.toggle_plugin)
        self.remove_button = QPushButton("卸载")
        self.remove_button.clicked.connect(self.remove_plugin)
        close_button = QPushButton("完成")
        close_button.clicked.connect(self.accept)
        actions.addWidget(self.install_button)
        actions.addWidget(self.toggle_button)
        actions.addWidget(self.remove_button)
        actions.addStretch()
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        for record in self.manager.list():
            tools = "、".join(item.name for item in record.manifest.tools)
            item = QTreeWidgetItem(
                [
                    record.manifest.name,
                    record.manifest.version,
                    "已启用" if record.enabled else "已禁用",
                    tools,
                ]
            )
            item.setData(0, 256, record.manifest.id)
            self.tree.addTopLevelItem(item)
        self._update_buttons()

    def selected(self) -> PluginRecord | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        plugin_id = items[0].data(0, 256)
        return self.manager.get(str(plugin_id)) if plugin_id else None

    def install_plugin(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "安装 Tool 插件",
            str(Path.home() / "Downloads"),
            "Social Agent Tool (*.socialtool)",
        )
        if not filename:
            return
        self._set_busy(True)
        worker = InstallPluginWorker(self.manager, Path(filename), self)
        worker.succeeded.connect(self._installed)
        worker.failed.connect(self._install_failed)
        worker.finished.connect(lambda: self._set_busy(False))
        self._worker = worker
        worker.start()

    def _installed(self, record: PluginRecord) -> None:
        self.refresh()
        self.plugins_changed.emit()
        QMessageBox.information(
            self,
            "安装完成",
            f"{record.manifest.name} {record.manifest.version} 已安装并启用。",
        )

    def _install_failed(self, message: str) -> None:
        QMessageBox.critical(self, "安装失败", message)

    def toggle_plugin(self) -> None:
        record = self.selected()
        if record is None:
            return
        self.manager.set_enabled(record.manifest.id, not record.enabled)
        self.refresh()
        self.plugins_changed.emit()

    def remove_plugin(self) -> None:
        record = self.selected()
        if record is None:
            return
        answer = QMessageBox.question(
            self,
            "卸载插件",
            f"确定卸载“{record.manifest.name}”吗？插件环境会被删除，下载和分析结果不会删除。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.manager.uninstall(record.manifest.id)
        self.refresh()
        self.plugins_changed.emit()

    def _update_buttons(self) -> None:
        record = self.selected()
        self.toggle_button.setEnabled(record is not None)
        self.remove_button.setEnabled(record is not None)
        self.toggle_button.setText("禁用" if record is not None and record.enabled else "启用")

    def _set_busy(self, busy: bool) -> None:
        self.install_button.setEnabled(not busy)
        self.toggle_button.setEnabled(not busy and self.selected() is not None)
        self.remove_button.setEnabled(not busy and self.selected() is not None)
        self.install_button.setText("正在安装依赖…" if busy else "安装 .socialtool")
