"""Presentation keeps compatibility while routing state through the task facade."""
from __future__ import annotations

import os
import json
import threading
import time
from functools import partial
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtWidgets import QApplication, QWidget, QDialog

from social_ops_agent import material_desktop
from social_ops_agent.material_ui.background import BackgroundCall
from social_ops_agent.material_ui.library_controller import LibraryController
from social_ops_agent.material_ui.task_panel import MaterialTaskPanel
from social_ops_agent.material_ui.tool_controller import ToolController
from social_ops_agent.material_ui.selection_dialog import PagedSelectionDialog
from social_ops_agent.material_ui.selection_page import SelectionPage


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def task(index=0, *, kind='material', actions=('pause','stop')):
    return dict(id=f'task-{index:04d}', name=f'任务 {index}', kind=kind,
                tool='download' if kind=='material' else 'agent',
                state='执行中', completed=2, total=5, updated_at='revision-1',
                command=None, actions=list(actions), conversation_id='chat-1' if kind=='agent' else None,
                results={}, items=[], error=None)


class Center:
    def __init__(self, rows):
        self.rows=rows
        self.lists=[]
        self.gets=[]
        self.commands=[]
        self.descriptions=[]
        self.output=None

    def list(self, **options):
        self.lists.append(options)
        rows=self.rows
        if options['query']:
            rows=[row for row in rows if options['query'] in row['name']]
        return [{k:v for k,v in row.items() if k not in {'items','results','error'}}
                for row in rows[options['offset']:options['offset']+options['limit']]]

    def get(self, task_id):
        self.gets.append(task_id)
        return dict(next(row for row in self.rows if row['id']==task_id))

    def describe(self, row, *, technical=False):
        self.descriptions.append((row['id'],technical))
        return ('诊断：' if technical else '当前工作：')+row['id']

    def output_directory(self, row):
        return self.output

    def command(self, task_id, command):
        self.commands.append((task_id,command))


@pytest.fixture
def panel_factory(app,tmp_path):
    panels=[]
    def make(rows=None, **kwargs):
        center=Center(rows if rows is not None else [task()])
        # There is deliberately no service.jobs/database API on this fake.
        panel=MaterialTaskPanel(SimpleNamespace(state_root=tmp_path),object(),task_center=center,**kwargs)
        panel.timer.stop()
        panels.append(panel)
        return panel,center
    yield make
    for panel in panels:
        panel.close()
        panel.deleteLater()
    app.processEvents()


def test_public_imports_survive_widget_split():
    from social_ops_agent.material_ui.tool_dialog import MaterialToolDialog
    from social_ops_agent.material_ui.settings_dialog import MaterialSettingsDialog
    from social_ops_agent.material_ui.library_dialog import MaterialLibraryDialog
    from social_ops_agent.material_ui.toolbox import MaterialToolbox
    assert material_desktop.MaterialTaskPanel is MaterialTaskPanel
    assert material_desktop.MaterialToolDialog is MaterialToolDialog
    assert material_desktop.MaterialSettingsDialog is MaterialSettingsDialog
    assert material_desktop.MaterialLibraryDialog is MaterialLibraryDialog
    assert material_desktop.MaterialToolbox is MaterialToolbox
    assert callable(material_desktop.button) and callable(material_desktop.combo)


def test_task_panel_loads_bounded_summaries_and_only_selected_detail(panel_factory):
    panel,center=panel_factory([task(index) for index in range(205)])
    assert panel.list.count()==100
    assert center.gets==['task-0000']
    assert center.lists[0]['limit']==100 and center.lists[0]['offset']==0
    panel.refresh()
    assert center.gets==['task-0000']
    panel.list.setCurrentRow(2)
    assert center.gets==['task-0000','task-0002']
    panel.change_page(1)
    assert panel.list.count()==100 and panel.current()['id']=='task-0100'
    assert center.lists[-1]['offset']==100
    panel.change_page(1)
    assert panel.list.count()==5 and not panel.next_page.isEnabled()
    panel.search.setText('任务 0')
    assert center.lists[-1]['offset']==0 and panel.list.count()==1


def test_progress_actions_and_agent_commands_use_same_facade(panel_factory):
    row=task(kind='agent', actions=('stop',))
    panel,center=panel_factory([row])
    assert panel.progress.value()==40
    assert panel.stop.isEnabled()
    assert not panel.pause.isEnabled() and not panel.resume.isEnabled()
    assert not panel.open_conversation.isHidden()
    panel.command('pause')
    assert center.commands==[]
    panel.command('stop')
    assert center.commands==[(row['id'],'stop')]
    requested=[]
    panel.conversation_requested.connect(requested.append)
    panel.show_conversation()
    assert requested==['chat-1']


def test_read_only_agent_center_does_not_invent_controls(panel_factory):
    panel,center=panel_factory([task(kind='agent',actions=())])
    assert not panel.pause.isEnabled() and not panel.resume.isEnabled() and not panel.stop.isEnabled()
    panel.command('stop')
    assert center.commands==[]


def test_detail_cache_invalidates_on_revision_and_routes_technical_render(panel_factory):
    row=task()
    panel,center=panel_factory([row])
    panel.technical.setChecked(True)
    assert panel.detail.toPlainText().startswith('诊断：')
    assert len(center.gets)==1
    row['updated_at']='revision-2'
    row['completed']=3
    row['actions']=['retry']
    panel.refresh()
    assert len(center.gets)==2 and panel.progress.value()==60
    assert panel.resume.isEnabled() and not panel.pause.isEnabled()
    panel.command('resume')
    assert center.commands==[(row['id'],'retry')]


def test_empty_panel_hides_irrelevant_actions(panel_factory):
    panel,center=panel_factory([])
    assert panel.current() is None and panel.progress.value()==0
    assert panel.pause.isHidden() and panel.technical.isHidden()
    assert center.gets==[]


def test_output_opening_uses_resolved_facade_directory(panel_factory,tmp_path,monkeypatch):
    from social_ops_agent.material_ui import task_panel
    panel,center=panel_factory()
    center.output=tmp_path
    urls=[]
    monkeypatch.setattr(task_panel.QDesktopServices,'openUrl',urls.append)
    panel.selected()
    assert not panel.open_output.isHidden()
    panel.show_output()
    assert urls[0].toLocalFile()==str(tmp_path)


def test_library_controller_formats_rows_and_checks_runner_before_create():
    created=[]
    service=SimpleNamespace(create=lambda *args,**kwargs:created.append(args))
    controller=LibraryController(service)
    with pytest.raises(ValueError,match='工具箱'):
        controller.retry_intake(['/tmp/source.png'])
    assert created==[]
    row=dict(id='asset',metadata_json='{"theme":"科技"}',analysis_json='{"summary":"测试描述"}',
             features_json='{"quality":88}',scores_json='[{"status":"待配置"}]',
             source_path='/tmp/source.png',path='/tmp/library/asset.png')
    description=controller.describe(row)
    assert '科技' in description and '88' in description and '待配置' in description


def test_tool_controller_retains_url_validation_and_execution_snapshot():
    creates=[]
    submissions=[]
    def create(tool,items,options,**kwargs):
        creates.append((tool,items,options,kwargs))
        return 'job-1'
    controller=ToolController(SimpleNamespace(create=create),SimpleNamespace(submit=submissions.append),'download',None)
    with pytest.raises(ValueError,match='具体帖子'):
        controller.start('https://t.me/public_channel',{})
    assert not creates and not submissions
    original={'session_ref':'registered-window'}
    assert controller.start('https://t.me/public_channel/5\nhttps://t.me/public_channel/5',original)=='job-1'
    assert creates==[('download',['https://t.me/public_channel/5'],original,{'trusted_local':True})]
    assert original=={'session_ref':'registered-window'} and submissions==['job-1']


def test_tool_controller_lists_only_bounded_record_choices():
    calls=[]
    resource=dict(id='asset',source_path='/tmp/sample.png',analysis_state='未分析',usage_state='未使用')
    def listing(**kwargs):
        calls.append(kwargs)
        return [resource]
    controller=ToolController(SimpleNamespace(library=lambda:SimpleNamespace(list=listing)),None,'analyze',None)
    assert controller.records('library',query='sample')==[('resource:asset','sample.png · 未分析')]
    assert calls==[{'query':'sample','limit':101,'offset':0}]


def wait_for(app,predicate,timeout=3):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def test_background_service_call_returns_on_gui_thread(app):
    caller=BackgroundCall()
    owner_thread=threading.get_ident()
    started=threading.Event()
    release=threading.Event()
    results=[]
    def operation():
        started.set()
        release.wait(2)
        return threading.get_ident()
    caller.succeeded.connect(lambda worker_thread:results.append((worker_thread,threading.get_ident())))
    assert caller.start(operation)
    wait_for(app,started.is_set)
    assert caller.busy and not caller.start(lambda:None)
    release.set()
    wait_for(app,lambda:bool(results))
    assert results[0][0]!=owner_thread and results[0][1]==owner_thread
    assert not caller.busy


def test_background_call_does_not_access_destroyed_view(app):
    import shiboken6
    parent=QObject()
    caller=BackgroundCall(parent)
    started=threading.Event()
    release=threading.Event()
    finished=threading.Event()
    signals=[]
    def operation():
        started.set()
        release.wait(2)
        finished.set()
        return 1
    caller.succeeded.connect(signals.append)
    caller.start(operation)
    wait_for(app,started.is_set)
    shiboken6.delete(parent)
    release.set()
    wait_for(app,finished.is_set)
    QThreadPool.globalInstance().waitForDone(1000)
    app.processEvents()
    assert signals==[]


def test_background_failure_returns_exception_without_raising_on_gui(app):
    caller=BackgroundCall()
    errors=[]
    caller.failed.connect(errors.append)
    def fail():
        raise ValueError('可恢复的评分错误')
    caller.start(fail)
    wait_for(app,lambda:bool(errors))
    assert isinstance(errors[0],ValueError) and not caller.busy


def test_background_failure_is_logged_even_after_owning_view_is_destroyed(app,tmp_path):
    import shiboken6
    from social_ops_agent.diagnostics import record_exception,log_directory
    parent=QObject()
    caller=BackgroundCall(parent,error_reporter=partial(record_exception,'agent','materials.library.rescore',state_root=tmp_path))
    started=threading.Event()
    release=threading.Event()
    signals=[]
    def fail():
        started.set()
        release.wait(2)
        raise RuntimeError('rescore failed after close')
    caller.failed.connect(signals.append)
    caller.start(fail)
    wait_for(app,started.is_set)
    shiboken6.delete(parent)
    release.set()
    assert QThreadPool.globalInstance().waitForDone(1000)
    app.processEvents()
    rows=[json.loads(line) for path in log_directory(tmp_path).glob('agent-*.jsonl') for line in path.read_text().splitlines()]
    assert len(rows)==1 and rows[0]['stage']=='materials.library.rescore'
    assert rows[0]['exception']['message']=='rescore failed after close'
    assert rows[0]['exception']['stack'] and signals==[]


def test_record_pages_reach_old_library_entries_even_if_recent_page_has_no_eligible_assets():
    rows=[dict(id=str(i),source_path=f'/assets/{i}.png',analysis_state='已分析' if i<200 else '未分析',usage_state='未使用') for i in range(251)]
    calls=[]
    def listing(**kwargs):
        calls.append(kwargs)
        return rows[kwargs['offset']:kwargs['offset']+kwargs['limit']]
    controller=ToolController(SimpleNamespace(library=lambda:SimpleNamespace(list=listing)),None,'analyze',None)
    page=controller.record_page('library')
    assert page.entries==[] and page.next_cursor==100
    page=controller.record_page('library',cursor=page.next_cursor)
    assert page.entries==[] and page.next_cursor==200
    page=controller.record_page('library',cursor=page.next_cursor)
    assert len(page.entries)==51 and page.entries[-1][0]=='resource:250'
    assert page.next_cursor is None
    assert [call['offset'] for call in calls]==[0,100,200]
    assert all(call['limit']==101 for call in calls)


def test_download_record_pagination_reaches_older_tasks_and_large_single_job():
    rows=[]
    for i in range(135):
        row=task(i)
        row['results']={'0':{'result':{'artifacts':[{'path':f'/downloads/{i}-{j}.png'} for j in range(250 if i==0 else 1)]}}}
        rows.append(row)
    calls=[]
    class Downloads:
        def list(self,**kwargs):
            calls.append(kwargs)
            return [{'id':row['id']} for row in rows[kwargs['offset']:kwargs['offset']+kwargs['limit']]]
        def get(self,task_id):
            return next(row for row in rows if row['id']==task_id)
    controller=ToolController(None,None,'import',Downloads())
    cursor=None
    values=[]
    for _ in range(15):
        page=controller.record_page('downloads',cursor=cursor)
        assert len(page.entries)<=100
        values.extend(value for value,_ in page.entries)
        cursor=page.next_cursor
        if cursor is None: break
    assert cursor is None
    assert len(values)==384 and len(set(values))==384
    assert '/downloads/0-249.png' in values and '/downloads/134-0.png' in values
    assert all(call['limit']==50 for call in calls)
    assert any(call['offset']>=100 for call in calls)


def test_issue_pages_preserve_access_to_older_intake_failures():
    rows=[dict(source_path=f'/source/{i}.png',issues_json='["画质未通过"]') for i in range(235)]
    calls=[]
    def attempts(**kwargs):
        calls.append(kwargs)
        return rows[kwargs['offset']:kwargs['offset']+kwargs['limit']]
    controller=LibraryController(SimpleNamespace(library=lambda:SimpleNamespace(attempts=attempts)))
    first=controller.issue_page()
    second=controller.issue_page(cursor=first.next_cursor)
    third=controller.issue_page(cursor=second.next_cursor)
    assert len(first.entries)==len(second.entries)==100 and len(third.entries)==35
    assert third.entries[-1][0]=='/source/234.png' and third.next_cursor is None
    assert [call['offset'] for call in calls]==[0,100,200]


def test_selection_persists_across_pages_and_submission_includes_older_records(app,tmp_path):
    parent=QWidget()
    parent.service=SimpleNamespace(state_root=tmp_path)
    entries=[(f'/source/{i}.png',f'素材 {i}') for i in range(235)]
    def loader(*,cursor,limit,query):
        start=cursor or 0
        return SelectionPage(entries[start:start+limit],start+limit if start+limit<len(entries) else None)
    submitted=[]
    dialog=PagedSelectionDialog(loader,parent,title='分页选择',on_confirm=submitted.append)
    try:
        dialog.records.item(0).setSelected(True)
        dialog.next_page()
        dialog.records.item(5).setSelected(True)
        dialog.next_page()
        dialog.records.item(34).setSelected(True)
        assert not dialog.next.isEnabled()
        dialog.previous_page()
        assert dialog.records.item(5).isSelected()
        dialog.previous_page()
        assert dialog.records.item(0).isSelected()
        assert dialog.values()==['/source/0.png','/source/105.png','/source/234.png']
        dialog.confirm()
        assert submitted==[['/source/0.png','/source/105.png','/source/234.png']]
        assert dialog.result()==QDialog.DialogCode.Accepted
    finally:
        dialog.close()
        parent.close()
        dialog.deleteLater()
        parent.deleteLater()
        app.processEvents()
