"""Reusable bounded record picker with selection preserved across pages."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QVBoxLayout,
)

from .controls import button, error
from .selection_page import SelectionPage


class PagedSelectionDialog(QDialog):
    PAGE_SIZE = 100

    def __init__(self, loader, parent, *, title, searchable=True,
                 confirm_label='选用所选记录', on_confirm=None):
        super().__init__(parent)
        self.service = parent.service
        self.loader, self.on_confirm = loader, on_confirm
        self._cursors = [None]
        self._page = 0
        self._next_cursor = None
        self._chosen = {}
        self.setWindowTitle(title)
        self.resize(720, 500)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索素材名或路径')
        self.search.setVisible(searchable)
        layout.addWidget(self.search)
        self.records = QListWidget()
        self.records.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.records.itemSelectionChanged.connect(self.remember_selection)
        layout.addWidget(self.records, 1)
        pages = QHBoxLayout()
        self.previous = button('上一页', self.previous_page, pages)
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pages.addWidget(self.page_label, 1)
        self.next = button('下一页', self.next_page, pages)
        layout.addLayout(pages)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        button('清空选择', self.clear_selection, actions)
        self.confirm_button = button(confirm_label, self.confirm, actions)
        self.confirm_button.setObjectName('primaryButton')
        layout.addLayout(actions)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(180)
        self.debounce.timeout.connect(self.reset_pages)
        self.search.textChanged.connect(lambda:self.debounce.start())
        self.load_page()

    def values(self):
        return list(self._chosen)

    def remember_selection(self):
        visible = {self.records.item(i).data(Qt.ItemDataRole.UserRole)
                   for i in range(self.records.count())}
        selected = {item.data(Qt.ItemDataRole.UserRole) for item in self.records.selectedItems()}
        for value in visible-selected:
            self._chosen.pop(value, None)
        for item in self.records.selectedItems():
            self._chosen.setdefault(item.data(Qt.ItemDataRole.UserRole), item.text())
        self.update_hint()

    def update_hint(self):
        message = f'已选 {len(self._chosen)} 条，翻页或搜索不会清空选择。每页最多显示 100 条。'
        if not self.records.count() and self._next_cursor is not None:
            message += '\n本页没有匹配的可用记录，可继续下一页。'
        self.hint.setText(message)
        self.confirm_button.setEnabled(bool(self._chosen))

    def clear_selection(self):
        self._chosen.clear()
        self.records.clearSelection()
        self.update_hint()

    def load_page(self):
        try:
            page = self.loader(cursor=self._cursors[self._page],
                               limit=self.PAGE_SIZE, query=self.search.text())
        except Exception as exc:
            error(self, exc)
            return
        self._next_cursor = page.next_cursor
        self.records.blockSignals(True)
        self.records.clear()
        for value, label in page.entries:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip(value)
            self.records.addItem(item)
            item.setSelected(value in self._chosen)
        self.records.blockSignals(False)
        self.previous.setEnabled(self._page > 0)
        self.next.setEnabled(self._next_cursor is not None)
        self.page_label.setText(f'第 {self._page+1} 页')
        self.update_hint()

    def reset_pages(self):
        self._page = 0
        self._cursors = [None]
        self.load_page()

    def next_page(self):
        if self._next_cursor is None:
            return
        self._cursors = self._cursors[:self._page+1]+[self._next_cursor]
        self._page += 1
        self.load_page()

    def previous_page(self):
        if self._page:
            self._page -= 1
            self.load_page()

    def confirm(self):
        if not self._chosen:
            return
        try:
            if len(self._chosen) > 500:
                raise ValueError('每批最多 500 条，请减少选择后重试')
            if self.on_confirm is not None:
                self.on_confirm(self.values())
        except Exception as exc:
            error(self, exc)
            return
        self.accept()
