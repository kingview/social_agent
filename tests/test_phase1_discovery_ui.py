import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication,QPlainTextEdit

from social_ops_agent.material_service import MaterialService
from social_ops_agent.material_ui.discovery_form import DiscoveryForm
from social_ops_agent.material_ui.tool_controller import ToolController
from social_ops_agent.material_ui.strategy_editor import StrategyDialog,StrategyEditor
from social_ops_agent.material_settings import StrategyRule
from social_ops_agent import material_windows as windows


@pytest.fixture
def form(tmp_path):
    app=QApplication.instance() or QApplication([])
    field=QPlainTextEdit()
    widget=DiscoveryForm(MaterialService(tmp_path/'out',tmp_path/'state'),field)
    yield widget
    widget.close(); field.close(); app.processEvents()


def choose(control,value): control.setCurrentIndex(control.findData(value))


def test_defaults_and_telegram_mode(form):
    options=form.options()
    assert options['platform']=='xiaohongshu' and options['source']=='timeline'
    assert options['days']==30 and options['browser_engine']=='standard'
    assert form.input.isHidden()
    choose(form.platform,'telegram')
    assert not form.source.isEnabled() and form.browser.isHidden()
    assert form.options()['execution_mode']=='automation'
    assert form.options()['sort']=='latest'
    assert form.options()['source']=='url'


def test_custom_date_validation_and_zero_threshold(form):
    choose(form.period,'custom')
    form.start_date.setDate(QDate(2026,9,1)); form.end_date.setDate(QDate(2026,9,2))
    options=form.options()
    assert options['days'] is None
    assert options['start_date'].startswith('2026-09-01T00:00:00')
    assert options['end_date'].startswith('2026-09-02T23:59:59.999999')
    form.start_date.setDate(QDate(2026,9,3))
    with pytest.raises(ValueError,match='晚于'): form.options()
    choose(form.period,30); choose(form.metric_mode,True)
    with pytest.raises(ValueError,match='阈值'): form.options()
    form.metric_value.setValue(0)
    assert form.options()['minimum_likes']==0
    choose(form.platform,'telegram')
    assert form.options()['minimum_views']==0
    assert 'minimum_likes' not in form.options()


def test_bit_requires_explicit_available_window(form,monkeypatch):
    monkeypatch.setattr(form,'refresh_windows',lambda:None)
    choose(form.browser,'bitbrowser')
    assert form.mode.currentData()=='rpa'
    with pytest.raises(ValueError,match='窗口'): form.options()
    rows=[dict(name='busy',session_ref='busy',status='占用中',available=False),
          dict(name='ok',session_ref='okay',status='可用',available=True)]
    form.windows_ready(('xiaohongshu',rows))
    assert form.window.currentData() is None
    assert not form.window.model().item(1).isEnabled()
    choose(form.window,'okay')
    assert form.options()['session_ref']=='okay'
    choose(form.browser,'standard')
    assert 'session_ref' not in form.options()
    assert form.options()['execution_mode']=='automation'


def test_controller_profile_link_and_validation():
    service=Mock(); runner=Mock()
    controller=ToolController(service,runner,'discover',None)
    for source,raw in [('search',''),('user','one\ntwo'),('url','')]:
        with pytest.raises(ValueError): controller.start(raw,{'source':source})
    service.create.assert_not_called()
    controller.start('https://www.xiaohongshu.com/user/profile/id',{'source':'user'})
    assert service.create.call_args.args[2]['source']=='url'
    assert 'user_key' not in service.create.call_args.args[2]
    controller.start('leftover hidden text',{'source':'timeline'})
    assert service.create.call_args.args[1]==['timeline']


def test_window_probe_reachable_busy_and_invalid_api(tmp_path,monkeypatch):
    records=[SimpleNamespace(platform='xiaohongshu',api_url='http://127.0.0.1:54345',profile_id='one',profile_name='窗口',session_ref='ref'),
             SimpleNamespace(platform='xiaohongshu',api_url='http://example.com',profile_id='two')]
    monkeypatch.setattr(windows.SessionStore,'list',lambda _:records)
    client=Mock(); client.__enter__=Mock(return_value=client); client.__exit__=Mock(return_value=None)
    client.post.return_value.json.return_value={'success':True}
    factory=Mock(return_value=client)
    result=windows.window_availability(tmp_path/'sessions', 'xiaohongshu',client_factory=factory,temporary_root=tmp_path)
    assert len(result)==1 and result[0]['available']
    assert client.post.call_args.args[0].endswith('/health')
    probed=[]
    monkeypatch.setattr(windows,'lock_busy',lambda path:probed.append(path) or True)
    result=windows.window_availability(tmp_path/'sessions','xiaohongshu',client_factory=factory,temporary_root=tmp_path)
    assert result[0]['status']=='占用中'
    expected=hashlib.sha256(b'loopback:54345|one').hexdigest()+'.lock'
    assert probed[0].name==expected
    client.post.return_value.json.return_value=[]
    assert not windows.window_availability(tmp_path/'sessions','xiaohongshu',client_factory=factory)[0]['available']


def test_strategy_structured_round_trip(form):
    rule=StrategyRule(name='科技',weights={'quality':1,'topic':2},required={'language':['zh']},preferred={'topic':['机器人','科技']})
    dialog=StrategyDialog(rule)
    assert dialog.value()==rule
    dialog.dimensions['topic'][2].setText('科技，机器人,科技;AI')
    assert dialog.value().preferred['topic']==['科技','机器人','AI']
    dialog.name.setText(' ')
    with pytest.raises(ValueError): dialog.value()
    editor=StrategyEditor([rule]); editor.remove()
    assert editor.values()==[] and rule.name=='科技'
    editor.close(); dialog.close()


def test_partial_discovery_remains_retryable_with_results(tmp_path):
    from social_ops_agent.material_jobs import MaterialRunner
    from social_ops_agent.task_center import TaskCenter
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    job=service.create('discover',['https://t.me/example_channel'])
    result=dict(completed=False,posts=[{'url':'https://t.me/example_channel/1'}],requested=10,
                count=1,found=3,filtered_out=2,warnings=['达到最长运行时间'])
    runner=MaterialRunner(service.jobs,lambda *args:result)
    try:
        row=runner.run(job)
        assert row['state']=='部分完成' and row['completed']==0
        center=TaskCenter(service,runner)
        detail=center.describe(center.get(job))
        assert '有效链接 1/10' in detail and '达到最长运行时间' in detail
        assert 'retry' in center.get(job)['actions']
    finally: runner.close()
