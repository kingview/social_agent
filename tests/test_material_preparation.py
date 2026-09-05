import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QTimer,QThreadPool
from PySide6.QtWidgets import QApplication

from social_ops_agent.material_preparation import PreparationControl,PreparationCancelled,expand_items
from social_ops_agent.material_service import MaterialService
from social_ops_agent.material_ui.tool_dialog import MaterialToolDialog


def test_scan_cancellation_does_not_create_history(tmp_path):
    source=tmp_path/'input'; source.mkdir()
    for i in range(10): (source/f'{i}.png').write_bytes(b'fixture')
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    control=PreparationControl()
    def progress(count):
        if count==3: control.cancel()
    with pytest.raises(PreparationCancelled):
        service.create('import',[str(source)],trusted_local=True,check_cancel=control.check,on_progress=progress,before_commit=control.seal)
    assert service.jobs.list()==[]


def test_limit_stops_lazy_scan_and_closes_iterator(tmp_path,monkeypatch):
    from social_ops_agent import material_preparation as preparation
    source=tmp_path/'input'; source.mkdir()
    for i in range(600): (source/f'{i}.png').write_bytes(b'fixture')
    visited=[]; closed=[]
    original=preparation.directory_files
    def wrapped(root,check):
        try:
            for item in original(root,check):
                visited.append(item)
                yield item
        finally: closed.append(True)
    monkeypatch.setattr(preparation,'directory_files',wrapped)
    with pytest.raises(ValueError,match='500'):
        expand_items('import',[str(source)],tmp_path,trusted_local=True)
    assert len(visited)==501 and closed==[True]


def test_scan_stable_deduplicated_and_symlinks_stay_inside_root(tmp_path):
    root=tmp_path/'inputs'; root.mkdir()
    outside=tmp_path/'outside.png'; outside.write_bytes(b'outside')
    for name in ('b.png','a.png','ignore.txt'): (root/name).write_bytes(b'fixture')
    (root/'external.png').symlink_to(outside)
    (root/'external-directory').symlink_to(tmp_path,target_is_directory=True)
    (root/'broken.png').symlink_to(tmp_path/'missing.png')
    result=expand_items('import',[str(root),str(root/'a.png')],tmp_path,trusted_local=True)
    assert result==[str(root/'a.png'),str(root/'b.png')]
    with pytest.raises(ValueError,match='输出目录'):
        expand_items('analyze',[str(outside)],tmp_path/'out')


def test_cancel_and_commit_have_one_atomic_boundary():
    cancelled=PreparationControl()
    assert cancelled.cancel()
    with pytest.raises(PreparationCancelled): cancelled.seal()
    committed=PreparationControl(); committed.seal()
    assert not committed.cancel()
    committed.check()


def test_closed_runner_does_not_leave_an_orphan_queued_task(tmp_path):
    from social_ops_agent.material_ui.tool_controller import ToolController
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    source=tmp_path/'input.png'; source.write_bytes(b'fixture')
    runner=Mock(); runner.submit.side_effect=RuntimeError('runner closed')
    controller=ToolController(service,runner,'import',None)
    with pytest.raises(RuntimeError,match='closed'):
        controller.start(str(source),{},preparation=PreparationControl())
    assert service.jobs.list()[0]['state']=='执行失败'
    assert 'runner closed' in service.jobs.list()[0]['error']


def spin(app,predicate):
    deadline=time.monotonic()+3
    while not predicate() and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.005)
    assert predicate()


@pytest.mark.parametrize('cancel,close',[(False,False),(True,False),(True,True)])
def test_dialog_prepares_off_thread_without_double_submit(tmp_path,monkeypatch,cancel,close):
    app=QApplication.instance() or QApplication([])
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    source=tmp_path/'input.png'; source.write_bytes(b'fixture')
    started=threading.Event(); release=threading.Event(); threads=[]
    original=service.create
    def create(*args,**kwargs):
        threads.append(threading.get_ident()); started.set()
        assert release.wait(3)
        return original(*args,**kwargs)
    monkeypatch.setattr(service,'create',create)
    runner=Mock()
    dialog=MaterialToolDialog(service,runner,'import')
    dialog.tasks.timer.stop()
    dialog.input.setPlainText(str(source))
    dialog.start_job(); dialog.start_job()
    spin(app,started.is_set)
    assert threads==[threads[0]] and threads[0]!=threading.get_ident()
    assert not dialog.input.isEnabled() and not dialog.start.isEnabled()
    tick=[]; QTimer.singleShot(0,lambda:tick.append(True)); spin(app,lambda:bool(tick))
    if cancel:
        if close: dialog.close()
        else: dialog.cancel_preparation()
    release.set()
    spin(app,lambda:not dialog.prepare_call.busy)
    if cancel:
        assert service.jobs.list()==[]
        runner.submit.assert_not_called()
        assert '取消' in dialog.notice.text()
    else:
        runner.submit.assert_called_once()
        job=service.jobs.list()[0]
        assert job['items']==[str(source)]
        service.jobs.command(job['id'],'stop')
    assert dialog.input.isEnabled() and dialog.start.isEnabled()
    dialog.close(); dialog.deleteLater(); app.processEvents()


def test_destroyed_view_cancels_scanning_before_history_write(tmp_path,monkeypatch):
    import shiboken6
    app=QApplication.instance() or QApplication([])
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    source=tmp_path/'input.png'; source.write_bytes(b'fixture')
    started=threading.Event(); release=threading.Event()
    original=service.create
    def create(*args,**kwargs):
        started.set(); release.wait(3)
        return original(*args,**kwargs)
    monkeypatch.setattr(service,'create',create)
    runner=Mock(); dialog=MaterialToolDialog(service,runner,'import')
    dialog.input.setPlainText(str(source)); dialog.start_job()
    spin(app,started.is_set)
    shiboken6.delete(dialog); release.set()
    assert QThreadPool.globalInstance().waitForDone(2000)
    app.processEvents()
    assert service.jobs.list()==[]
    runner.submit.assert_not_called()
