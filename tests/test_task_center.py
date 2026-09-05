import json
from types import SimpleNamespace

import pytest

from social_ops_agent.material_service import MaterialService
from social_ops_agent.task_center import TaskCenter


@pytest.fixture
def center(tmp_path):
    service = MaterialService(tmp_path/'out', tmp_path/'state')
    submitted = []
    return TaskCenter(service, SimpleNamespace(submit=submitted.append)), submitted


def add_agent(center, task_id='turn-one', state='failed', **fields):
    center.agent_store.import_turn('conversation-one', {
        'turn_id': task_id, 'user_message': '分析图片', 'status': state,
        'plan': {'steps': ['下载', '分析', '输出']},
        'result': {'completed_steps': 1, 'total_steps': 3, 'summary': '分析中断'},
        **fields,
    })
    return 'agent:'+task_id


def test_summaries_paged_without_loading_full_records(center, monkeypatch):
    tasks, _ = center
    for index in range(6):
        tasks.service.jobs.create('analyze', [str(index)], {'large': 'x'*10000}, name=f'素材{index}')
    add_agent(tasks)
    monkeypatch.setattr(tasks.service.jobs, 'get', lambda _: pytest.fail('N+1 get'))
    monkeypatch.setattr(tasks.agent_store, 'dashboard_task', lambda _: pytest.fail('N+1 get'))
    first = tasks.list(limit=3)
    second = tasks.list(limit=3, offset=3)
    assert len(first) == len(second) == 3
    assert not {r['id'] for r in first}.intersection(r['id'] for r in second)
    assert all(not {'items','results','parameters'}.intersection(r) for r in first + second)
    assert len(tasks.list(limit=3, offset=6)) == 1
    assert len(tasks.list(tool='analyze', query='素材2')) == 1
    assert len(tasks.list(group='attention')) == 1
    assert tasks.list(tool='agent')[0]['id'] == 'agent:turn-one'


def test_material_controls_revalidate_and_route(center):
    tasks, submitted = center
    job = tasks.service.jobs.create('import', ['one', 'two'], {})
    assert tasks.get(job)['actions'] == ['pause', 'stop']
    tasks.command(job, 'pause')
    assert tasks.get(job)['state'] == '已暂停'
    assert 'resume' in tasks.get(job)['actions']
    tasks.command(job, 'resume')
    assert submitted == [job]
    with pytest.raises(ValueError, match='状态已变化'):
        tasks.command(job, 'resume')
    tasks.service.jobs.transition(job, '已完成')
    with pytest.raises(ValueError):
        tasks.command(job, 'stop')


def test_agent_actions_require_supported_live_adapter(center):
    tasks, _ = center
    key = add_agent(tasks)
    assert tasks.get(key)['actions'] == []
    with pytest.raises(ValueError): tasks.command(key, 'resume')
    calls = []
    tasks.agent_command = lambda row, command: calls.append((row['task_id'], command))
    tasks.agent_actions = lambda row: ['resume'] if row['state'] == '执行失败' else []
    tasks.command(key, 'resume')
    assert calls == [('turn-one','resume')]
    with pytest.raises(ValueError): tasks.command(key, 'pause')
    add_agent(tasks, state='succeeded')
    with pytest.raises(ValueError): tasks.command(key, 'resume')


def test_description_uses_item_progress_and_live_file_telemetry(center):
    tasks, _ = center
    job = tasks.service.jobs.create('download', list('abcde'), {})
    for index in range(2):
        tasks.service.jobs.checkpoint(job, index, {'status':'completed','result':{}})
    tasks.service.jobs.claim(job)
    row = tasks.get(job)
    telemetry = tasks.service.state_root/'transfer-progress'/f'{job}.json'
    telemetry.parent.mkdir()
    telemetry.write_text(json.dumps({'execution_id':job,'status':'downloading',
        'downloaded_bytes':1000,'total_bytes':2000,'speed_bps':3000,'filename':'test.mp4'}))
    text = tasks.describe(row)
    assert '2/5' in text and '40%' in text and 'test.mp4' in text and 'KiB/s' in text
    assert row['completed'] == 2
    telemetry.write_text('malformed')
    assert '40%' in tasks.describe(row)
    telemetry.write_text('x'*65537)
    assert '40%' in tasks.describe(row)


def test_only_existing_output_directories_are_exposed(center, tmp_path):
    tasks, _ = center
    job = tasks.service.jobs.create('analyze', ['a'], {})
    tasks.service.jobs.checkpoint(job, 0, {'status':'completed','result':{'report_path':str(tmp_path/'report.json')}})
    row = tasks.get(job)
    assert tasks.output_directory(row) == tmp_path
    assert json.loads(tasks.describe(row, technical=True))['id'] == job
    row['results']['0']['result'] = {'path':str(tmp_path/'missing'/'report.json')}
    assert tasks.output_directory(row) is None
