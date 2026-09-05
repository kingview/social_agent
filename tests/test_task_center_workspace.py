"""Workspace task commands stay bound to the selected conversation and task."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from social_ops_agent.contracts import DynamicAgentPlan
from social_ops_agent.conversation_controller import ConversationPhase
from social_ops_agent.conversation_workspace import ConversationWorkspace
from social_ops_agent.settings import LLMSettingsStore


class MemoryCredentials:
    def get(self, _account): return None
    def set(self, _account, _value): pass
    def delete(self, _account): pass


@pytest.fixture
def workspace(tmp_path):
    app=QApplication.instance() or QApplication([])
    window=ConversationWorkspace(
        output_root=tmp_path/'output',plugin_root=tmp_path/'plugins',
        registry_path=tmp_path/'sessions.json',
        llm_settings_store=LLMSettingsStore(tmp_path/'llm.json',credentials=MemoryCredentials()),
    )
    yield window,app
    for pane in window.panes()+list(window.background_panes.values()):
        pane.controller.plan_worker=None
        pane.controller.execution_worker=None
        pane.controller.active_turn_id=None
    window.close()
    app.processEvents()


def row_for(pane, task_id, state='执行失败'):
    return dict(id='agent:'+task_id,task_id=task_id,kind='agent',
                conversation_id=pane.controller.conversation_id,state=state)


def fail_task(pane, message='分析已下载的图片'):
    task_id=pane.controller.conversation.begin_turn(message)
    pane.controller.conversation.mark_failed(task_id,stage='execution',error='测试失败')
    return task_id


def test_workspace_injects_one_center_into_all_task_views(workspace):
    window,_app=workspace
    assert window.task_panel.task_center is window.task_center
    assert window.toolbox.task_center is window.task_center
    window.toolbox.open_tool('import')
    dialog=window.toolbox.dialogs['import']
    assert dialog.tasks.task_center is window.task_center
    dialog.close()


def test_stop_is_only_available_for_exact_live_execution(workspace):
    window,_app=workspace
    one=window.tabs.currentWidget()
    task_id=one.controller.conversation.begin_turn('任务一')
    other=window.open_conversation(create_new=True)
    other_id=other.controller.conversation.begin_turn('任务二')
    calls=[]
    one.controller.active_turn_id=task_id
    other.controller.active_turn_id=other_id
    one.controller.execution_worker=SimpleNamespace(cancel_after_current_batch=lambda:calls.append('one'))
    other.controller.execution_worker=SimpleNamespace(cancel_after_current_batch=lambda:calls.append('other'))
    one.controller.phase=other.controller.phase=ConversationPhase.EXECUTING
    row=row_for(one,task_id,'执行中')
    assert set(window.agent_task_actions(row))=={'stop'}
    assert not window.agent_task_actions(row_for(one,'wrong-task','执行中'))
    window.command_agent_task(row,'stop')
    assert calls==['one']
    assert one.controller.phase==ConversationPhase.CANCELLING
    assert other.controller.phase==ConversationPhase.EXECUTING
    assert not window.agent_task_actions(row)


def test_planning_does_not_claim_to_support_stop_or_pause(workspace):
    window,_app=workspace
    pane=window.tabs.currentWidget()
    task_id=pane.controller.conversation.begin_turn('等待规划')
    pane.controller.active_turn_id=task_id
    pane.controller.plan_worker=object()
    pane.controller.phase=ConversationPhase.PLANNING
    assert not window.agent_task_actions(row_for(pane,task_id,'执行中'))


@pytest.mark.parametrize('state',['执行失败','部分完成','已暂停','已停止'])
def test_resume_opens_exact_task_conversation_without_touching_other_chat(workspace,monkeypatch,state):
    window,_app=workspace
    one=window.tabs.currentWidget()
    task_id=fail_task(one)
    other=window.open_conversation(create_new=True)
    other.message_input.setPlainText('对话二的未发送草稿')
    resumes=[]
    monkeypatch.setattr(one,'resume_task',resumes.append)
    row=row_for(one,task_id,state)
    assert 'resume' in window.agent_task_actions(row)
    assert 'pause' not in window.agent_task_actions(row)
    window.command_agent_task(row,'resume')
    assert resumes==[task_id]
    assert window.tabs.currentWidget() is one
    assert other.message_input.toPlainText()=='对话二的未发送草稿'


def test_busy_conversation_cannot_resume_older_task(workspace):
    window,_app=workspace
    pane=window.tabs.currentWidget()
    task_id=fail_task(pane)
    pane.controller.active_turn_id=pane.controller.conversation.begin_turn('正在做另一个任务')
    assert 'resume' not in window.agent_task_actions(row_for(pane,task_id))
    with pytest.raises(ValueError):
        window.command_agent_task(row_for(pane,task_id),'resume')


def test_selected_resume_uses_harness_and_preserves_unsent_draft_and_attachments(workspace,tmp_path,monkeypatch):
    from social_ops_agent import conversation_controller
    window,_app=workspace
    pane=window.tabs.currentWidget()
    task_id=fail_task(pane)
    attachment=tmp_path/'next-task.png'
    attachment.write_bytes(b'not-read-by-this-isolated-test')
    pane.message_input.setPlainText('下一个任务的草稿')
    pane._attachment_paths=[attachment]
    pane._refresh_attachments()
    calls=[]
    monkeypatch.setattr(pane,'_ensure_llm_secret',lambda:True)
    monkeypatch.setattr(conversation_controller,'PlanWorker',StubPlanWorker)
    start_planning=pane.controller.start_planning
    def capture_planning(message,**kwargs):
        calls.append((message,kwargs))
        return start_planning(message,**kwargs)
    monkeypatch.setattr(pane.controller,'start_planning',capture_planning)
    executions=[]
    monkeypatch.setattr(pane,'execute_plan',lambda:executions.append(True))
    pane.resume_task(task_id)
    assert len(calls)==1
    assert calls[0][1]['resume_task_id']==task_id
    assert calls[0][1]['attachment_paths']==[]
    assert pane.message_input.toPlainText()=='下一个任务的草稿'
    assert pane._attachment_paths==[attachment]
    # A later planning callback must not clear media intended for the next task.
    pane._plan_succeeded(DynamicAgentPlan(objective='继续分析',summary='继续未完成的分析',
        steps=['分析图片'],resume_turn_id=task_id))
    assert executions==[True]
    assert pane.message_input.toPlainText()=='下一个任务的草稿'
    assert pane._attachment_paths==[attachment]


class StubPlanWorker(QObject):
    status_changed=Signal(str)
    succeeded=Signal(object)
    failed=Signal(str)
    finished=Signal()
    started=[]

    def __init__(self,**kwargs):
        super().__init__(kwargs.get('parent'))
        self.arguments=kwargs

    def start(self):
        self.started.append(self)


@pytest.mark.parametrize('wrong_target',[None,'turn-not-selected'])
def test_mismatched_harness_resume_plan_never_executes(workspace,monkeypatch,wrong_target):
    from social_ops_agent import conversation_controller
    window,_app=workspace
    pane=window.tabs.currentWidget()
    task_id=fail_task(pane)
    controller=pane.controller
    monkeypatch.setattr(conversation_controller,'PlanWorker',StubPlanWorker)
    executions=[]
    monkeypatch.setattr(pane,'execute_plan',lambda:executions.append(True))
    controller.start_planning('继续所选任务',session=None,available_sessions=(),attachment_paths=[],resume_task_id=task_id)
    controller.accept_plan(DynamicAgentPlan(objective='继续分析',summary='继续分析',steps=['分析'],resume_turn_id=wrong_target))
    assert executions==[]
    assert controller.pending_plan is None
    assert controller.phase==ConversationPhase.FAILED


def test_resume_rejects_task_owned_by_another_conversation(workspace,monkeypatch):
    from social_ops_agent import conversation_controller
    window,_app=workspace
    first=window.tabs.currentWidget()
    foreign_id=fail_task(first)
    other=window.open_conversation(create_new=True)
    monkeypatch.setattr(conversation_controller,'PlanWorker',StubPlanWorker)
    count=len(StubPlanWorker.started)
    with pytest.raises(ValueError):
        other.controller.start_planning('继续任务',session=None,available_sessions=(),attachment_paths=[],resume_task_id=foreign_id)
    assert len(StubPlanWorker.started)==count
    assert other.controller.active_turn_id is None
