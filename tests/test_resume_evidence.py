import asyncio
import json

import pytest

from social_ops_agent.contracts import DynamicAgentPlan
from social_ops_agent.execution_tracking import ExecutionTracker
from social_ops_agent.harness_backend import DeepSeekHarnessBackend
from social_ops_agent.harness_client import HarnessTurnResult
from social_ops_agent.harness_prompts import planning_persona, execution_persona
from social_ops_agent.resume_evidence import resume_input
from social_ops_agent.task_store import TaskStore
from social_ops_agent import mcp_server


URLS = [f'https://t.me/example_channel/{n}' for n in range(100, 93, -1)]


def test_pending_download_targets_survive_restart_without_secrets(tmp_path):
    store = TaskStore(tmp_path)
    task = store.ensure_task('conv', '下载10条')
    store.start(task, 'run')
    tracker = ExecutionTracker(DynamicAgentPlan(objective='下载10条', summary='下载',
        steps=['下载'], step_tools=['download_media']))
    tracker.called({'name': 'mcp__social__download_media', 'callId': 'original-call',
        'arguments': json.dumps({'urls': URLS, 'media_format': 'best', 'max_total_size_mb': 5000,
                                'session_ref': 'SECRET', 'cookie': 'SECRET', 'approval_token': 'SECRET'})})
    store.checkpoint(task, 'run', tracker.report())
    restored = TaskStore(tmp_path).resume_context('conv', task)
    evidence = restored['tasks'][0]['resume_evidence'][0]
    assert evidence['status'] == 'running'
    assert evidence['resume_input']['urls'] == URLS
    assert 'SECRET' not in json.dumps(restored)
    assert len(evidence['resume_input']['urls']) == 7  # Never inflate to requested 10.


def test_recovery_uses_exact_call_id_and_conversation_not_model_summary(tmp_path):
    store = TaskStore(tmp_path)
    original = store.ensure_task('conv', '下载10条')
    store.start(original, 'old-run')
    store.checkpoint(original, 'old-run', {'calls': {
        'download-1': {'tool': 'download_media', 'status': 'running'},
        'browse-1': {'tool': 'browse_posts', 'status': 'succeeded'}}})
    retry = store.ensure_task('conv', '继续')
    store.set_plan(retry, {'resume_turn_id': original})
    def call(call_id, urls):
        return {'type': 'tool/call', 'data': {'callId': call_id,
            'name': 'mcp__social__download_media', 'arguments': json.dumps({'urls': urls})}}
    path = tmp_path / 'harness-sessions/execute/cwd/conv-execute-old/session.jsonl'
    path.parent.mkdir(parents=True)
    result = {'type': 'tool/result', 'data': {'message': {'content': [
        {'type': 'tool-result', 'toolCallId': 'browse-1', 'content': [
            {'type': 'text', 'text': json.dumps({'posts': [{'url': URLS[0], 'text': 'PRIVATE_BODY'}]})}]}]}}}
    path.write_text('\n'.join(json.dumps(e) for e in [call('download-1', URLS),
                    call('not-this-task', ['https://t.me/other/2']), result]) + '\n{truncated')
    other = tmp_path / 'harness-sessions/execute/cwd/other-execute-old/session.jsonl'
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps(call('download-1', ['https://t.me/other/3'])))
    context = TaskStore(tmp_path).resume_context('conv', retry)
    assert len(context['tasks']) == 2
    evidence = context['tasks'][0]['resume_evidence']
    assert evidence[0]['resume_input']['urls'] == URLS
    assert evidence[1]['resume_output']['post_urls'] == [URLS[0]]
    assert 'PRIVATE_BODY' not in json.dumps(context)
    assert 'https://t.me/other' not in json.dumps(context)
    with pytest.raises(ValueError, match='outside'):
        store.resume_context('other', original)


def test_browse_results_preserve_urls_not_page_body():
    tracker = ExecutionTracker(DynamicAgentPlan(objective='搜索', summary='搜索',
        steps=['搜索'], step_tools=['browse_posts']))
    tracker.called({'name':'mcp__social__browse_posts', 'callId':'b', 'arguments':{}})
    tracker.returned({'message': {'content':[{'type':'tool-result', 'toolCallId':'b',
        'content': {'posts':[{'url':URLS[0], 'text':'SECRET_BODY'}]}}]}})
    row = tracker.report()['calls']['b']
    assert row['resume_output']['post_urls'] == [URLS[0]]
    assert 'SECRET_BODY' not in json.dumps(row)


def test_credentials_and_unexpected_urls_are_not_saved():
    assert not resume_input('download_media', {'urls':['https://user:secret@t.me/channel/1']})
    assert not resume_input('download_media', {'urls':['https://unrelated.example/private']})
    assert resume_input('download_media', {'urls':[URLS[0]+'?cookie=SECRET']}) == {'urls':[URLS[0]]}
    assert not resume_input('publish_x_post', {'approval_token':'SECRET', 'text':'PRIVATE'})


def test_execution_on_fresh_client_receives_original_targets(tmp_path):
    registry = tmp_path / 'registry.json'
    registry.write_text('{"sessions": []}')
    backend = DeepSeekHarnessBackend(registry_path=registry, output_root=tmp_path/'out',
                                    conversation_id='conv', project_root=tmp_path)
    task = backend.task_store.ensure_task('conv', '下载10条')
    backend.task_store.start(task, 'original')
    backend.task_store.checkpoint(task, 'original', {'calls': {'d': {
        'tool':'download_media', 'status':'running', 'resume_input': {'urls':URLS}}}})
    class Client:
        def run_turn(self, *, session_id, content_blocks, on_event):
            self.prompt = json.loads(content_blocks[0]['text'])
            return HarnessTurnResult(session_id=session_id, final_response='尚未完成',
                                     finish_reason='stop', events=[], tool_calls=[])
    client = Client()
    backend._clients['execute'] = client
    backend.execute(DynamicAgentPlan(objective='继续', summary='继续下载', steps=['下载'],
                                    step_tools=['download_media'], resume_turn_id=task))
    assert client.prompt['resumed_task_context']['tasks'][0]['resume_evidence'][0]['resume_input']['urls'] == URLS
    assert client.prompt['recent_conversation_context']


def test_browse_schema_is_explicit_and_telegram_rejects_search():
    tool = mcp_server.mcp._tool_manager.get_tool('browse_posts')
    assert tool.parameters['properties']['source']['enum'] == ['search', 'user', 'timeline', 'url']
    with pytest.raises(ValueError, match='Telegram browse_posts'):
        asyncio.run(mcp_server.browse_posts(platform='telegram', session_ref='unused', source='search', view='posts'))
    assert 'source="resume"' in planning_persona()
    assert 'resumed_task_context' in execution_persona()
