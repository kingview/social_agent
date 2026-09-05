"""MaterialToolbox presentation widget."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QFrame, QGridLayout
from ..material_service import TOOLS
from ..workspace_theme import MATERIAL_STYLE
from .controls import button
from .tool_dialog import MaterialToolDialog
from .library_dialog import MaterialLibraryDialog
from .settings_dialog import MaterialSettingsDialog


class MaterialToolbox(QWidget):
    def __init__(self,service,runner,parent=None, *, task_center=None):
        super().__init__(parent)
        self.service,self.runner=service,runner
        self.task_center = task_center
        self.setObjectName('materialToolbox')
        self.setStyleSheet(MATERIAL_STYLE)
        self.dialogs={}
        layout=QVBoxLayout(self)
        layout.setContentsMargins(32,28,32,28); layout.setSpacing(18)
        title=QLabel('每一项工具，都能独立完成工作。'); title.setObjectName('sectionTitle'); layout.addWidget(title)
        subtitle=QLabel('四个素材处理工具，与 Agent 共用任务、配置和结果。'); subtitle.setWordWrap(True); layout.addWidget(subtitle)
        self.search=QLineEdit(); self.search.setPlaceholderText('搜索工具名称或用途'); self.search.setMinimumHeight(40); layout.addWidget(self.search)
        self.search.textChanged.connect(self.filter_cards)
        self.buttons={}
        self.cards={}
        self.descriptions={}
        self.states={}
        grid=QGridLayout(); grid.setSpacing(18)
        descriptions={'discover':('↗','从频道、账号或关键词出发，筛选并导出有效帖子链接。'),
            'download':('↓','批量保存图片、视频和随附文字，保留已完成的下载结果。'),
            'import':('◇','检查完整性与画质，处理水印、去重，建立本地素材库。'),
            'analyze':('◎','理解媒体内容，生成标签、基础评分与策略适配结果。')}
        for index,(tool,label) in enumerate(TOOLS.items()):
            icon,description=descriptions[tool]
            card=QFrame(); card.setObjectName('toolCard'); card.setMinimumHeight(220)
            inner=QVBoxLayout(card); inner.setContentsMargins(22,22,22,20); inner.setSpacing(12)
            mark=QLabel(icon); mark.setObjectName('toolIcon'); mark.setFixedSize(44,44); mark.setAlignment(Qt.AlignmentFlag.AlignCenter); inner.addWidget(mark)
            name=QLabel(label); name.setObjectName('toolName'); inner.addWidget(name)
            about=QLabel(description); about.setWordWrap(True); inner.addWidget(about)
            inner.addStretch()
            state=QLabel('素材处理  ·  独立工具'); state.setObjectName('toolState'); inner.addWidget(state)
            self.states[tool]=state
            self.descriptions[tool]=description
            self.buttons[tool]=button('运行  →',lambda _=False,key=tool:self.open_tool(key),inner)
            self.buttons[tool].setObjectName('primaryButton')
            self.cards[tool]=card
            grid.addWidget(card,index//2,index%2)
        layout.addLayout(grid)
        bottom=QHBoxLayout()
        button('素材库 / 人工复核',lambda:MaterialLibraryDialog(service,self).exec(),bottom)
        button('素材工作流设置',lambda:MaterialSettingsDialog(service,self).exec(),bottom)
        layout.addLayout(bottom)
        layout.addStretch()
        self.status_timer=QTimer(self)
        self.status_timer.timeout.connect(self.refresh_availability)
        self.status_timer.start(5000)
        self.refresh_availability()

    def refresh_availability(self):
        from ..plugins import PluginManager, PluginError
        manager=PluginManager(self.service.plugin_root)
        required={'discover':'discover_public_materials','download':'download_public_material','import':'inspect_material','analyze':'analyze_content'}
        for key,tool in required.items():
            try:
                record=manager.find_tool(tool)
                label=f'插件已就绪  ·  v{record.manifest.version}'
            except PluginError:
                label='需安装或更新 Tool 插件'
            self.states[key].setText(label)
            dialog=self.dialogs.get(key)
            self.buttons[key].setText('切换窗口  ↗' if dialog and dialog.isVisible() else '运行  →')

    def filter_cards(self,text):
        for key,card in self.cards.items():
            card.setVisible(not text or text.casefold() in (TOOLS[key]+' '+self.descriptions[key]).casefold())

    def open_tool(self,tool):
        dialog=self.dialogs.get(tool)
        if dialog is None:
            dialog=MaterialToolDialog(self.service,self.runner,tool,self,task_center=self.task_center)
            self.dialogs[tool]=dialog
        dialog.show(); dialog.raise_(); dialog.activateWindow()
        self.buttons[tool].setText('切换窗口  ↗')
