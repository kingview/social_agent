"""Phase-one discovery parameters, separate from execution and result views."""
from functools import partial
from PySide6.QtCore import QDate, Slot
from PySide6.QtWidgets import QWidget, QFormLayout, QDateEdit, QSpinBox, QDoubleSpinBox, QPushButton, QLabel

from ..diagnostics import record_exception
from ..material_windows import window_availability
from ..discovery_contract import DiscoveryInput,DISCOVERY_LIMITS,calendar_bounds,default_source,default_mode
from .background import BackgroundCall
from .controls import combo


class DiscoveryForm(QWidget):
    def __init__(self, service, input_widget, parent=None):
        super().__init__(parent)
        self.service, self.input = service, input_widget
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0,0,0,0)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.platform = combo([('小红书','xiaohongshu'),('抖音','douyin'),('Telegram 公开频道','telegram')])
        self.source = combo([('主页信息流','timeline'),('指定账号 ID / 主页','user'),('关键词搜索','search'),('公开频道 / 主页链接','url')])
        self.browser = combo([('标准浏览器','standard'),('比特浏览器','bitbrowser')])
        self.mode = combo([('高效模式（浏览器自动化）','automation'),('稳定模式（RPA）','rpa')])
        self.window = combo([('请选择一个空闲窗口',None)])
        self.refresh_windows_button = QPushButton('刷新窗口状态')
        self.window_status = QLabel('')
        self.window_status.setWordWrap(True)
        self.media_type = combo([('图片和视频','both'),('仅图片','image'),('仅视频','video')])
        self.sort = combo([('综合','top'),('最新发布','latest'),('点赞最多（当前结果）','likes')])
        self.period = combo([('最近30天',30),('最近7天',7),('最近24小时',1),('不限',None),('自定义','custom')])
        self.start_date = QDateEdit(QDate.currentDate().addDays(-30))
        self.end_date = QDateEdit(QDate.currentDate())
        for control in (self.start_date,self.end_date):
            control.setDisplayFormat('yyyy-MM-dd'); control.setCalendarPopup(True); control.setMinimumHeight(40)
        self.count = QSpinBox(); self.count.setRange(*DISCOVERY_LIMITS['max_items']); self.count.setValue(DiscoveryInput.model_fields['max_items'].default)
        self.metric_mode = combo([('不限',False),('大于',True)])
        self.metric_value = QSpinBox(); self.metric_value.setRange(-1,2_000_000_000); self.metric_value.setValue(-1)
        self.metric_value.setSpecialValueText('请输入非负整数')
        self.interval = QDoubleSpinBox(); self.interval.setRange(*DISCOVERY_LIMITS['access_interval_seconds']); self.interval.setValue(DiscoveryInput.model_fields['access_interval_seconds'].default); self.interval.setSuffix(' 秒')
        self.timeout = QSpinBox(); self.timeout.setRange(*DISCOVERY_LIMITS['timeout_seconds']); self.timeout.setValue(DiscoveryInput.model_fields['timeout_seconds'].default); self.timeout.setSuffix(' 秒')
        for title,control in [('平台',self.platform),('获取方式',self.source),('浏览器环境',self.browser),('执行模式',self.mode),
                ('比特窗口',self.window),('',self.refresh_windows_button),('',self.window_status),('内容类型',self.media_type),
                ('搜索结果排序',self.sort),('发布时间',self.period),('开始日期',self.start_date),('结束日期',self.end_date),
                ('公开互动数量',self.metric_mode),('数量阈值',self.metric_value),('有效链接数',self.count),
                ('访问间隔',self.interval),('最长运行时间',self.timeout)]:
            control.setMinimumHeight(38)
            self.form.addRow(title,control)
        self.probe = BackgroundCall(self, error_reporter=partial(record_exception,'agent','materials.windows',state_root=service.state_root))
        self.probe.succeeded.connect(self.windows_ready)
        self.probe.failed.connect(self.windows_failed)
        self.platform.currentIndexChanged.connect(self.platform_changed)
        self.browser.currentIndexChanged.connect(self.browser_changed)
        self.source.currentIndexChanged.connect(self.update_visibility)
        self.period.currentIndexChanged.connect(self.update_visibility)
        self.metric_mode.currentIndexChanged.connect(self.update_visibility)
        self.refresh_windows_button.clicked.connect(self.refresh_windows)
        self.platform_changed()

    def update_visibility(self, *_):
        telegram = self.platform.currentData()=='telegram'
        bit = not telegram and self.browser.currentData()=='bitbrowser'
        for control in (self.browser,self.mode): self.form.setRowVisible(control,not telegram)
        for control in (self.window,self.refresh_windows_button,self.window_status): self.form.setRowVisible(control,bit)
        for control in (self.start_date,self.end_date): self.form.setRowVisible(control,self.period.currentData()=='custom')
        self.form.setRowVisible(self.metric_value,self.metric_mode.currentData())
        self.form.setRowVisible(self.sort,self.source.currentData()=='search')
        self.input.setVisible(self.source.currentData()!='timeline')
        self.input.setPlaceholderText('https://t.me/公开频道名称' if telegram else
            '一个关键词或短语' if self.source.currentData()=='search' else '一个账号 ID 或 HTTPS 主页链接')

    def platform_changed(self, *_):
        telegram = self.platform.currentData()=='telegram'
        self.source.setEnabled(not telegram)
        self.source.setCurrentIndex(self.source.findData(default_source(self.platform.currentData())))
        self.browser.setCurrentIndex(0)
        self.window.clear(); self.window.addItem('请选择一个空闲窗口',None)
        self.input.clear()
        self.update_visibility()

    def browser_changed(self, *_):
        bit = self.browser.currentData()=='bitbrowser'
        self.mode.setCurrentIndex(self.mode.findData(default_mode(self.browser.currentData())))
        self.update_visibility()
        if bit: self.refresh_windows()

    def refresh_windows(self):
        if self.probe.busy: return
        platform, registry = self.platform.currentData(), self.service.registry_path
        self.refresh_windows_button.setEnabled(False)
        self.window_status.setText('正在检查比特浏览器及窗口占用…')
        self.probe.start(lambda:(platform,window_availability(registry,platform)))

    @Slot(object)
    def windows_ready(self, result):
        self.refresh_windows_button.setEnabled(True)
        platform, rows = result
        if platform!=self.platform.currentData(): return
        previous = self.window.currentData()
        self.window.clear(); self.window.addItem('请选择一个空闲窗口',None)
        for row in rows:
            self.window.addItem(f'{row["name"]} · {row["status"]}',row['session_ref'])
            self.window.model().item(self.window.count()-1).setEnabled(row['available'])
        valid = next((r for r in rows if r['session_ref']==previous and r['available']),None)
        if valid: self.window.setCurrentIndex(self.window.findData(previous))
        self.window_status.setText('窗口状态已更新；开始时会再次校验占用。' if any(r['available'] for r in rows) else '未找到可用空闲窗口，请启动比特浏览器并注册对应平台窗口。')

    @Slot(object)
    def windows_failed(self, exc):
        self.refresh_windows_button.setEnabled(True)
        self.window.clear(); self.window.addItem('未找到',None)
        self.window_status.setText('窗口状态读取失败，详情已写入日志。')

    def options(self):
        telegram = self.platform.currentData()=='telegram'
        bit = not telegram and self.browser.currentData()=='bitbrowser'
        if bit and (self.probe.busy or not self.window.currentData()):
            raise ValueError('请先选择一个已检查为空闲的比特浏览器窗口')
        if self.metric_mode.currentData() and self.metric_value.value()<0:
            raise ValueError('请输入大于等于 0 的互动数量阈值')
        options = dict(platform=self.platform.currentData(),source=self.source.currentData(),
            browser_engine='bitbrowser' if bit else 'standard',execution_mode='automation' if telegram else self.mode.currentData(),
            media_type=self.media_type.currentData(),sort='latest' if telegram else self.sort.currentData(),max_items=self.count.value(),
            days=self.period.currentData() if self.period.currentData()!='custom' else None,
            access_interval_seconds=self.interval.value(),timeout_seconds=self.timeout.value())
        if bit: options['session_ref']=self.window.currentData()
        if self.metric_mode.currentData():
            options['minimum_views' if telegram else 'minimum_likes']=self.metric_value.value()
        if self.period.currentData()=='custom':
            start,end=self.start_date.date().toPython(),self.end_date.date().toPython()
            options.update(calendar_bounds(start,end))
        return options
