"""MaterialSettingsDialog presentation widget."""
from __future__ import annotations

import json
from PySide6.QtWidgets import QDialog, QVBoxLayout, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QSpinBox
from ..material_settings import MaterialSettings
from ..workspace_theme import MATERIAL_STYLE
from .controls import button, error


class MaterialSettingsDialog(QDialog):
    def __init__(self,service,parent=None):
        super().__init__(parent)
        self.service = service
        self.setStyleSheet(MATERIAL_STYLE)
        self.setWindowTitle('素材工作流设置')
        self.resize(700,620)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('配置仅影响新任务；现有聊天的远端 LLM 设置不变。'))
        form = QFormLayout()
        self.fields = {}
        settings = service.settings()
        for key,label in [('library_root','素材库目录'),('download_root','下载目录'),('local_base_url','本机模型 API'),('local_model','本机模型及版本')]:
            field = QLineEdit(str(getattr(settings,key))); field.setMinimumHeight(40)
            form.addRow(label,field); self.fields[key]=field
        self.concurrency = QSpinBox(); self.concurrency.setRange(1,8); self.concurrency.setValue(settings.max_concurrency)
        form.addRow('并行任务（重启生效）',self.concurrency)
        layout.addLayout(form)
        self.rules = QPlainTextEdit()
        self.rules.setPlainText(json.dumps({k:getattr(settings,k) for k in ('themes','analysis_dimensions','tag_rules')}
            | {'strategies':[r.model_dump() for r in settings.strategies]},ensure_ascii=False,indent=2))
        layout.addWidget(QLabel('主题、分析维度、标签规则与策略组（JSON；保存时校验）'))
        layout.addWidget(self.rules)
        button('保存',self.save,layout)

    def save(self):
        try:
            data = self.service.settings().model_dump()
            data.update({k:v.text().strip() for k,v in self.fields.items()})
            rules = json.loads(self.rules.toPlainText())
            if not isinstance(rules,dict) or set(rules)-{'themes','analysis_dimensions','tag_rules','strategies'}:
                raise ValueError('规则仅接受 themes、analysis_dimensions、tag_rules、strategies')
            data.update(rules,max_concurrency=self.concurrency.value())
            MaterialSettings.model_validate(data).save(self.service.state_root)
            self.accept()
        except Exception as exc:
            error(self,exc)
