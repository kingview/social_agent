"""A layout-owned conversation strip, independent of macOS QTabWidget corners."""
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QHBoxLayout, QSizePolicy, QStackedWidget, QStyleFactory,
    QTabBar, QVBoxLayout, QWidget,
)


class TabCloseButton(QAbstractButton):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip("关闭对话")
        self.setAccessibleName("关闭对话")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse() or self.hasFocus():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#3a424e"))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        painter.setPen(QPen(QColor("#e6e9ee" if self.underMouse() else "#949daa"), 1.5))
        painter.drawLine(QPointF(8, 8), QPointF(16, 16))
        painter.drawLine(QPointF(16, 8), QPointF(8, 16))


class ConversationTabBar(QTabBar):
    HEIGHT = 44
    WIDTH = 248

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("conversationTabBar")
        # A private style instance avoids platform-specific centered tab layout
        # and close-button placement. Never transfer ownership of the app style.
        style = QStyleFactory.create("Fusion")
        style.setParent(self)
        self.setStyle(style)
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setMovable(True)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setMinimumWidth(0)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName("对话标签")

    def tabSizeHint(self, index):
        return QSize(self.WIDTH, self.HEIGHT)

    def minimumTabSizeHint(self, index):
        return self.tabSizeHint(index)

    def tabInserted(self, index):
        super().tabInserted(index)
        button = TabCloseButton(self)
        button.clicked.connect(lambda: self._request_close(button))
        self.setTabButton(index, QTabBar.ButtonPosition.RightSide, button)

    def _request_close(self, button):
        for index in range(self.count()):
            if self.tabButton(index, QTabBar.ButtonPosition.RightSide) is button:
                self.tabCloseRequested.emit(index)
                break

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index in range(self.count()):
            rect = QRectF(self.tabRect(index)).adjusted(0.5, 0.5, -8.5, -0.5)
            if not rect.intersects(QRectF(self.rect())):
                continue
            selected = index == self.currentIndex()
            painter.setPen(QPen(QColor("#657d3c" if selected else "#343b46"), 1))
            painter.setBrush(QColor("#272f24" if selected else "#1b2028"))
            painter.drawRoundedRect(rect, 10, 10)
            info = self.tabData(index) or {}
            status = info.get("status", "")
            color = ("#eaa06f" if status in {"等待窗口", "正在停止", "未完成"} else
                     "#ef919d" if status == "失败" else "#d8ff52" if selected else "#9ea8b6")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color if status and status != "就绪" else "#d8ff52" if selected else "#66717e"))
            painter.drawEllipse(QPointF(rect.left() + 15, rect.center().y()), 3, 3)
            close = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
            text_right = close.geometry().left() - 8 if close else rect.right() - 12
            font = self.font()
            font.setPixelSize(12)
            painter.setFont(font)
            badge = status.replace("执行中 ", "").replace("等待窗口", "等待") if status != "就绪" else ""
            if badge:
                width = painter.fontMetrics().horizontalAdvance(badge) + 12
                badge_rect = QRectF(text_right - width, rect.center().y() - 11, width, 22)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#363c31" if selected else "#2b323c"))
                painter.drawRoundedRect(badge_rect, 5, 5)
                painter.setPen(QColor(color))
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)
                text_right = badge_rect.left() - 8
            font.setPixelSize(13)
            font.setBold(selected)
            painter.setFont(font)
            painter.setPen(QColor("#f0f4e8" if selected else "#b9c1cc"))
            text_rect = QRectF(rect.left() + 27, rect.top(), max(0, text_right - rect.left() - 27), rect.height())
            title = info.get("title", self.tabText(index))
            elided = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(text_rect.width()))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)
            if selected and self.hasFocus() and self.window().testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange):
                painter.setPen(QPen(QColor("#d8ff52"), 1, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 7, 7)


class ConversationTabs(QWidget):
    """QTabBar + stack with reserved action space; preserves the workspace tab API."""
    currentChanged = Signal(int)
    tabCloseRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("conversationTabs")
        self._pages = []
        self._bar = ConversationTabBar(self)
        self._stack = QStackedWidget(self)
        self.toolbar = QWidget(self)
        self.toolbar.setObjectName("conversationToolbar")
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(34, 12, 34, 12)
        row.setSpacing(16)
        row.addWidget(self._bar, 1)
        self.controls = QWidget(self.toolbar)
        self.controls.setObjectName("conversationControls")
        self.controls.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.action_layout = QHBoxLayout(self.controls)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(8)
        row.addWidget(self.controls, 0, Qt.AlignmentFlag.AlignVCenter)
        self.toolbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self._stack, 1)
        self._bar.currentChanged.connect(self._select)
        self._bar.tabMoved.connect(self._move)
        self._bar.tabCloseRequested.connect(self.tabCloseRequested)

    def tabBar(self):
        return self._bar

    def count(self):
        return len(self._pages)

    def widget(self, index):
        return self._pages[index] if 0 <= index < self.count() else None

    def indexOf(self, widget):
        return self._pages.index(widget) if widget in self._pages else -1

    def currentWidget(self):
        return self.widget(self._bar.currentIndex())

    def currentIndex(self):
        return self._bar.currentIndex()

    def setCurrentWidget(self, widget):
        self.setCurrentIndex(self.indexOf(widget))

    def setCurrentIndex(self, index):
        self._bar.setCurrentIndex(index)

    def addTab(self, widget, title):
        self._pages.append(widget)
        self._stack.addWidget(widget)
        return self._bar.addTab(title)

    def removeTab(self, index):
        widget = self.widget(index)
        if widget is None:
            return
        with QSignalBlocker(self._bar):
            self._pages.pop(index)
            self._stack.removeWidget(widget)
            self._bar.removeTab(index)
        self._select(self._bar.currentIndex())

    def update_conversation(self, index, state):
        self._bar.setTabText(index, f"{state.title} · {state.status}")
        self._bar.setTabData(index, {"title": state.title, "status": state.status})
        self._bar.setTabToolTip(index, f"对话 {state.conversation_id[-6:]}\n{state.title}\n{state.status}")
        close = self._bar.tabButton(index, QTabBar.ButtonPosition.RightSide)
        if close is not None:
            close.setAccessibleName(f"关闭对话：{state.title}")
        self._bar.update()

    def tabText(self, index):
        return self._bar.tabText(index)

    def _select(self, index):
        widget = self.widget(index)
        if widget is not None:
            self._stack.setCurrentWidget(widget)
        self.currentChanged.emit(index)

    def _move(self, source, destination):
        self._pages.insert(destination, self._pages.pop(source))
        self._select(self._bar.currentIndex())
