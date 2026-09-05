"""Shared controls; widgets never execute a material operation directly."""
from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QMessageBox
from ..diagnostics import record_exception
from ..model_settings_dialog import UniformComboBox


def button(text, callback, layout):
    control = QPushButton(text)
    control.setMinimumHeight(38)
    control.clicked.connect(callback)
    layout.addWidget(control)
    return control


def combo(items):
    control = UniformComboBox()
    control.setMinimumHeight(40)
    for label, data in items:
        control.addItem(label, data)
    return control


def error(parent, exc):
    record_exception('agent', 'materials.gui', exc, state_root=parent.service.state_root)
    QMessageBox.warning(parent, '操作未完成', str(exc))
