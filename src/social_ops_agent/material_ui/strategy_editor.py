"""Structured editing of strategy weights and hard/preferred content filters."""
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget,QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,
    QGridLayout,QLabel,QLineEdit,QCheckBox,QDoubleSpinBox,QListWidget,QScrollArea)

from ..material_settings import StrategyRule
from ..workspace_theme import MATERIAL_STYLE
from .controls import button,error


DIMENSIONS = [('quality','基础质量'),('topic','主题'),('language','语言'),('format','形式'),
              ('audience','受众'),('style','风格'),('timeliness','时效性'),('portrait','人物可见特征')]


def terms(value):
    return list(dict.fromkeys(t.strip() for t in re.split('[,，;；\n]',value) if t.strip()))


class StrategyDialog(QDialog):
    def __init__(self,rule=None,parent=None):
        super().__init__(parent)
        self.setStyleSheet(MATERIAL_STYLE)
        rule=rule or StrategyRule(name='新策略')
        self.setWindowTitle('编辑策略'); self.resize(740,660)
        layout=QVBoxLayout(self)
        form=QFormLayout()
        self.name=QLineEdit(rule.name)
        self.enabled=QCheckBox('启用'); self.enabled.setChecked(rule.enabled)
        self.minimum=QDoubleSpinBox(); self.minimum.setRange(0,100); self.minimum.setValue(rule.minimum_score)
        for control in (self.name,self.minimum): control.setMinimumHeight(38)
        form.addRow('策略名称',self.name); form.addRow('',self.enabled); form.addRow('建议使用分数',self.minimum)
        layout.addLayout(form)
        hint=QLabel('硬条件：每个填写的维度必须命中至少一项；偏好用于加权评分。多项用逗号分隔，留空表示不限。')
        hint.setWordWrap(True); layout.addWidget(hint)
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        body=QWidget(); grid=QGridLayout(body); scroll.setWidget(body); layout.addWidget(scroll,1)
        for column,title in enumerate(('维度','权重','硬条件','偏好')): grid.addWidget(QLabel(title),0,column)
        self.dimensions={}
        for row,(key,label) in enumerate(DIMENSIONS,1):
            weight=QDoubleSpinBox(); weight.setRange(0,100); weight.setValue(rule.weights.get(key,0))
            required=QLineEdit(', '.join(rule.required.get(key,[])))
            preferred=QLineEdit(', '.join(rule.preferred.get(key,[])))
            grid.addWidget(QLabel(label),row,0)
            for column,control in enumerate((weight,required,preferred),1):
                control.setMinimumHeight(38); grid.addWidget(control,row,column)
            if key=='quality':
                required.setEnabled(False); preferred.setEnabled(False)
                preferred.setPlaceholderText('使用分析产生的质量分')
            self.dimensions[key]=(weight,required,preferred)
        actions=QHBoxLayout(); layout.addLayout(actions)
        button('取消',self.reject,actions); button('保存策略',self.save,actions)

    def value(self):
        return StrategyRule(name=self.name.text().strip(),enabled=self.enabled.isChecked(),minimum_score=self.minimum.value(),
            weights={k:fields[0].value() for k,fields in self.dimensions.items() if fields[0].value()>0},
            required={k:terms(fields[1].text()) for k,fields in self.dimensions.items() if k!='quality' and terms(fields[1].text())},
            preferred={k:terms(fields[2].text()) for k,fields in self.dimensions.items() if k!='quality' and terms(fields[2].text())})

    def save(self):
        try: self.value()
        except ValueError as exc: error(self,exc); return
        self.accept()


class StrategyEditor(QWidget):
    def __init__(self,rules,parent=None):
        super().__init__(parent)
        self.rules=[r.model_copy(deep=True) for r in rules]
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.list=QListWidget(); layout.addWidget(self.list)
        actions=QHBoxLayout(); layout.addLayout(actions)
        button('添加策略',lambda:self.edit(new=True),actions)
        button('编辑所选',self.edit,actions); button('移除所选',self.remove,actions)
        self.list.itemDoubleClicked.connect(lambda *_:self.edit())
        self.refresh()

    def refresh(self,index=0):
        self.list.clear()
        for rule in self.rules:
            self.list.addItem(f'{rule.name} · {"已启用" if rule.enabled else "已停用"} · 建议分数 ≥ {rule.minimum_score:g}')
        if self.rules: self.list.setCurrentRow(min(index,len(self.rules)-1))

    def edit(self,*,new=False):
        index=self.list.currentRow()
        if index<0 and not new: return
        dialog=StrategyDialog(None if new else self.rules[index],self)
        if dialog.exec()!=QDialog.DialogCode.Accepted: return
        rule=dialog.value()
        if any(r.name==rule.name for i,r in enumerate(self.rules) if new or i!=index):
            error(self,ValueError('策略名称不能重复')); return
        if new: self.rules.append(rule); index=len(self.rules)-1
        else: self.rules[index]=rule
        self.refresh(index)

    def remove(self):
        index=self.list.currentRow()
        if index>=0:
            self.rules.pop(index); self.refresh(max(0,index-1))

    def values(self):
        return [r.model_dump() for r in self.rules]
