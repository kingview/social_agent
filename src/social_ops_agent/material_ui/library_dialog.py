"""MaterialLibraryDialog presentation widget."""
from __future__ import annotations

from functools import partial
from pathlib import Path
from PySide6.QtCore import Qt, QUrl, QSize, QTimer, Slot
from PySide6.QtGui import QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QMessageBox, QListWidget, QListWidgetItem, QAbstractItemView, QSpinBox
from ..diagnostics import record_exception
from ..workspace_theme import MATERIAL_STYLE
from .controls import button, combo, error
from .background import BackgroundCall
from .library_controller import LibraryController
from .selection_dialog import PagedSelectionDialog


class MaterialLibraryDialog(QDialog):
    def __init__(self,service,parent=None):
        super().__init__(parent)
        self.service=service
        self.controller = LibraryController(service, getattr(parent, 'runner', None))
        self._offset = 0
        self._selected_row = None
        self.rescore_call = BackgroundCall(self, error_reporter=partial(
            record_exception,'agent','materials.library.rescore',state_root=service.state_root))
        self.rescore_call.succeeded.connect(self._rescore_finished)
        self.rescore_call.failed.connect(self._rescore_failed)
        self.setStyleSheet(MATERIAL_STYLE)
        self.setWindowTitle('素材库与人工复核')
        self.resize(900,680)
        layout=QVBoxLayout(self)
        title=QLabel('素材库'); title.setObjectName('sectionTitle'); layout.addWidget(title)
        self.search=QLineEdit(); self.search.setPlaceholderText('搜索素材名、主题、标签、分析内容、人工分组或 ID'); layout.addWidget(self.search)
        filters=QHBoxLayout()
        self.analysis_filter=combo([('全部分析状态',None)]+[(s,s) for s in ('未分析','分析中','已分析','需复核','分析失败')])
        self.usage_filter=combo([('全部使用状态',None)]+[(s,s) for s in ('未使用','已占用','已使用','停用','已删除')])
        filters.addWidget(self.analysis_filter); filters.addWidget(self.usage_filter); layout.addLayout(filters)
        more_filters=QHBoxLayout()
        settings=service.settings()
        self.media_filter=combo([('全部媒体',None),('图片','image'),('视频','video')])
        self.theme_filter=combo([('全部主题',None)]+[(s,s) for s in settings.themes])
        self.strategy_filter=combo([('全部策略',None)]+[(r.name+' · 建议使用',r.name) for r in settings.strategies])
        self.quality_filter=QSpinBox(); self.quality_filter.setRange(-1,100); self.quality_filter.setValue(-1)
        self.quality_filter.setSpecialValueText('不限质量分'); self.quality_filter.setPrefix(''); self.quality_filter.setToolTip('基础质量分不低于此值；不限时包含未分析素材')
        for control in (self.media_filter,self.theme_filter,self.strategy_filter,self.quality_filter):
            control.setMinimumHeight(38); more_filters.addWidget(control)
        layout.addLayout(more_filters)
        self.list=QListWidget(); layout.addWidget(self.list)
        pages = QHBoxLayout()
        self.previous_page = button('上一页', lambda:self.change_page(-1), pages)
        self.page_label = QLabel(); pages.addWidget(self.page_label,1)
        self.next_page = button('下一页', lambda:self.change_page(1), pages)
        layout.addLayout(pages)
        preview_row=QHBoxLayout()
        self.preview=QLabel('选择素材预览'); self.preview.setFixedSize(200,160); self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_row.addWidget(self.preview)
        self.detail=QPlainTextEdit(); self.detail.setReadOnly(True); preview_row.addWidget(self.detail,1); layout.addLayout(preview_row)
        actions=QHBoxLayout()
        self.open_media=button('打开素材',self.open_selected,actions)
        self.usage=combo([(s,s) for s in ('未使用','已占用','已使用','停用','已删除')]); actions.addWidget(self.usage)
        self.save_usage=button('更新使用状态',self.update_usage,actions)
        layout.addLayout(actions)
        hint=QLabel('“已删除”为逻辑删除，文件仍保留，可改回“未使用”恢复。'); hint.setWordWrap(True); layout.addWidget(hint)
        self.group=QLineEdit(); self.group.setPlaceholderText('人工分组名称（可选，不进行自动身份识别）'); layout.addWidget(self.group)
        self.review_button=button('确认人工复核',self.review,layout)
        bottom=QHBoxLayout()
        self.rescore_button=button('按当前策略重新评分',self.rescore,bottom)
        button('入库问题记录',self.show_issues,bottom)
        layout.addLayout(bottom)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self.reset_page)
        self.search.textChanged.connect(lambda:self.search_timer.start())
        self.analysis_filter.currentIndexChanged.connect(self.reset_page)
        self.usage_filter.currentIndexChanged.connect(self.reset_page)
        for control in (self.media_filter,self.theme_filter,self.strategy_filter): control.currentIndexChanged.connect(self.reset_page)
        self.quality_filter.valueChanged.connect(self.reset_page)
        self.list.currentItemChanged.connect(self.selected)
        self.refresh()

    def reset_page(self,*_):
        self._offset = 0
        self.refresh()

    def change_page(self, direction):
        self._offset = max(0, self._offset + direction*100)
        self.refresh()

    def refresh(self,*_):
        current = self.list.currentItem()
        selected_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        rows = self.controller.list(query=self.search.text(), analysis_state=self.analysis_filter.currentData(), usage_state=self.usage_filter.currentData(),
            media_type=self.media_filter.currentData(),theme=self.theme_filter.currentData(),strategy=self.strategy_filter.currentData(),
            minimum_quality=self.quality_filter.value() if self.quality_filter.value()>=0 else None,limit=100,offset=self._offset)
        if not rows and self._offset:
            self._offset = max(0,self._offset-100)
            return self.refresh()
        self.list.blockSignals(True)
        self.list.clear()
        for row in rows:
            item=QListWidgetItem(f'{Path(row["source_path"]).name}\n{row["acquisition_state"]} · {row["intake_state"]} · {row["analysis_state"]} · {row["usage_state"]}')
            item.setData(Qt.ItemDataRole.UserRole,row['id']); self.list.addItem(item)
            if row['id']==selected_id: self.list.setCurrentItem(item)
        if self.list.count() and self.list.currentRow()<0: self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        paginated = self._offset>0 or len(rows)==100
        for control in (self.previous_page,self.next_page,self.page_label): control.setVisible(paginated)
        self.previous_page.setEnabled(self._offset>0)
        self.next_page.setEnabled(len(rows)==100)
        self.page_label.setText(f'第 {self._offset//100+1} 页')
        self.selected()

    def selected(self,*_):
        item=self.list.currentItem()
        row=self.controller.get(item.data(Qt.ItemDataRole.UserRole)) if item else None
        self._selected_row = row
        self.review_button.setEnabled(bool(row and row['analysis_state']=='需复核'))
        self.open_media.setEnabled(bool(row))
        self.save_usage.setEnabled(bool(row))
        self.preview.clear()
        if row:
            self.detail.setPlainText(self.controller.describe(row))
            self.group.setText(row['manual_subject_group'] or '')
            self.usage.setCurrentIndex(self.usage.findData(row['usage_state']))
            if row['media_type'].startswith('image/'):
                reader=QImageReader(row['path']); reader.setAutoTransform(True)
                size=reader.size()
                if size.isValid(): reader.setScaledSize(size.scaled(QSize(196,156),Qt.AspectRatioMode.KeepAspectRatio))
                image=reader.read()
                if not image.isNull(): self.preview.setPixmap(QPixmap.fromImage(image))
                else: self.preview.setText('预览不可用')
            else: self.preview.setText('视频素材\n点击“打开素材”播放')
        else:
            self.detail.clear()
            self.preview.setText('暂无匹配素材')

    def open_selected(self):
        item=self.list.currentItem()
        if item:
            row=self.controller.get(item.data(Qt.ItemDataRole.UserRole))
            if Path(row['path']).is_file(): QDesktopServices.openUrl(QUrl.fromLocalFile(row['path']))

    def update_usage(self):
        try:
            item=self.list.currentItem()
            if item:
                self.controller.set_usage(item.data(Qt.ItemDataRole.UserRole),self.usage.currentData())
                self.refresh()
        except Exception as exc: error(self,exc)

    def show_issues(self):
        dialog = PagedSelectionDialog(
            self.controller.issue_page, self, title='入库问题记录',
            searchable=False, confirm_label='重新检测所选项目',
            on_confirm=self.controller.retry_intake,
        )
        dialog.exec()

    def review(self):
        try:
            item=self.list.currentItem()
            if item:
                self.controller.review(item.data(Qt.ItemDataRole.UserRole),subject_group=self.group.text().strip() or None)
                self.refresh()
        except Exception as exc: error(self,exc)

    def rescore(self):
        if self.rescore_call.start(self.controller.rescore):
            self.rescore_button.setEnabled(False)
            self.rescore_button.setText('正在重新评分…')

    @Slot(object)
    def _rescore_finished(self,count):
        self.rescore_button.setEnabled(True)
        self.rescore_button.setText('按当前策略重新评分')
        self.refresh()
        if self.isVisible():
            QMessageBox.information(self,'重新评分',f'已更新 {count} 条素材的策略评分。')

    @Slot(object)
    def _rescore_failed(self,exc):
        self.rescore_button.setEnabled(True)
        self.rescore_button.setText('按当前策略重新评分')
        if self.isVisible():
            QMessageBox.warning(self,'操作未完成',str(exc))
