"""Task projections and migration stay read-only and preserve execution facts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3

import pytest

from social_ops_agent import task_dashboard
from social_ops_agent.task_store import CONTEXT_LIMIT, TaskStore
from social_ops_agent.task_intent import resolve_write_actions


def core_task(store, *, name='下载素材', state='succeeded', report=None):
    task = store.ensure_task('conversation', name)
    store.set_plan(task, {'steps': ['搜索', '下载'], 'step_tools': ['browse_posts', 'download_media']})
    store.start(task, 'run-' + task)
    store.checkpoint(task, 'run-' + task, report or {'completed_steps': 2, 'total_steps': 2}, state=state)
    return task


@pytest.mark.parametrize('state,report,expected', [
    ('succeeded', {}, '已完成'), ('completed', {}, '已完成'),
    ('succeeded', {'completion_status': 'partial'}, '部分完成'),
    ('executing', {}, '执行中'), ('planned', {}, '待执行'),
    ('failed', {}, '执行失败'), ('interrupted', {}, '已暂停'),
    ('executing', {'cancelled': True}, '已停止'),
])
def test_dashboard_maps_actual_execution_states(tmp_path, state, report, expected):
    store = TaskStore(tmp_path)
    task = core_task(store, state=state, report=report)
    store.import_turn('conversation', {'turn_id': task, 'user_message': '下载素材',
                                      'status': 'failed', 'result': {'error': 'outdated GUI mirror'}})
    row = store.dashboard_task(task)
    assert row['state'] == expected
    assert row['raw_state'] == state
    assert row['items'] == ['搜索', '下载']


def test_summary_is_sql_paged_and_never_loads_full_json_in_python(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    for index in range(5):
        core_task(store, name=f'task-{index}', report={'completed_steps': 2, 'total_steps': 2,
                    'summary': 'x' * 1_000_000, 'error': 'e' * 10_000})
    all_rows = store.dashboard(summary=True)
    queries = []
    original_db = store._db

    @contextmanager
    def tracked_db(*, write=True):
        assert not write
        with original_db(write=False) as db:
            db.set_trace_callback(queries.append)
            yield db

    monkeypatch.setattr(store, '_db', tracked_db)
    monkeypatch.setattr(task_dashboard.json, 'loads', lambda *_args, **_kwargs: pytest.fail('summary must not parse payloads'))
    page = store.dashboard(summary=True, limit=2, offset=1)
    assert page == all_rows[1:3]
    assert any('LIMIT 2 OFFSET 1' in sql for sql in queries)
    assert not any(key in row for row in page for key in ('message','report','plan','results','items','execution_json','legacy_json'))
    assert all(len(row['error']) == 2000 for row in page)
    assert len(json.dumps(page)) < 6000


def test_legacy_projection_and_context_use_original_plan_and_result(tmp_path):
    store = TaskStore(tmp_path)
    task = store.ensure_task('conversation', '下载第一条', 'legacy-turn')
    legacy = {'status': 'completed', 'plan': {'steps': ['下载原帖'], 'summary': '原计划'},
              'result': {'summary': '原结果', 'completed_steps': 1, 'total_steps': 1,
                         'calls': {'download': {'tool': 'download_media', 'status': 'running',
                                  'resume_input': {'urls': ['https://t.me/public_channel/12']}}}}}
    with store._db() as db:
        db.execute("UPDATE tasks SET plan_json='null',execution_json='null',legacy_json=? WHERE task_id=?",
                   (json.dumps(legacy), task))
    row = store.dashboard_task(task)
    assert row['state'] == '已完成'
    assert row['results']['summary'] == '原结果'
    assert row['items'] == ['下载原帖']
    context = json.loads(store.selected_task_context(task, 'conversation'))['selected_resume_task']
    assert context['original_user_message'] == '下载第一条'
    assert context['plan']['summary'] == '原计划'
    assert context['result']['summary'] == '原结果'
    assert context['resume_evidence'][0]['resume_input']['urls'] == ['https://t.me/public_channel/12']
    assert store.resume_context('conversation', task)['tasks'][0]['steps'] == ['下载原帖']


def test_null_and_malformed_legacy_json_do_not_crash_dashboard(tmp_path):
    store = TaskStore(tmp_path)
    task = store.ensure_task('conversation', 'legacy')
    with store._db() as db:
        db.execute("UPDATE tasks SET legacy_json='{broken',plan_json='[]',execution_json='null' WHERE task_id=?", (task,))
    row = store.dashboard_task(task)
    assert row['items'] == []
    assert row['results'] == {}


def test_all_read_endpoints_and_reopen_work_during_writer_reservation(tmp_path):
    store = TaskStore(tmp_path)
    task = core_task(store)
    writer = sqlite3.connect(store.path)
    writer.execute('BEGIN IMMEDIATE')
    try:
        def read():
            reopened = TaskStore(tmp_path)
            assert reopened.history('conversation')
            assert reopened.model_context('conversation')
            assert reopened.execution(task)
            assert reopened.dashboard(summary=True)
            assert reopened.selected_task_context(task, 'conversation')
            assert reopened.resume_context('conversation', task)
            return True
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(read).result(timeout=1)
    finally:
        writer.rollback()
        writer.close()
    with store._db(write=False) as db:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute("UPDATE tasks SET publish_attempted=0")


def create_legacy_schema(path):
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,message TEXT NOT NULL,
            resume_task_id TEXT,plan_json TEXT,legacy_json TEXT,run_id TEXT,state TEXT NOT NULL DEFAULT 'planning',
            execution_json TEXT,publish_attempted INTEGER NOT NULL DEFAULT 0,
            publish_state TEXT NOT NULL DEFAULT 'not_attempted')''')
        db.execute('''INSERT INTO tasks(task_id,conversation_id,message,legacy_json,run_id,state,publish_attempted,publish_state)
            VALUES(?,?,?,?,?,?,?,?)''', ('legacy', 'conversation', '发到X',
            json.dumps({'created_at': '2026-01-02T03:04:05Z'}), 'run', 'executing', 1, 'unknown'))


def test_migration_rolls_back_schema_and_triggers_together(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.sqlite3'
    create_legacy_schema(path)
    original_connect = sqlite3.connect

    class FailAfterTriggers(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if 'UPDATE tasks SET publish_attempted=0' in sql:
                assert self.in_transaction
                raise sqlite3.OperationalError('simulated migration failure')
            return super().execute(sql, parameters)

        def executescript(self, _sql):
            pytest.fail('executescript implicitly commits the migration')

    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: original_connect(*args, factory=FailAfterTriggers, **kwargs))
    with pytest.raises(sqlite3.OperationalError, match='migration failure'):
        TaskStore(tmp_path)
    with original_connect(path) as db:
        assert 'created_at' not in {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        assert db.execute('SELECT publish_attempted,publish_state FROM tasks').fetchone() == (1, 'unknown')
    monkeypatch.setattr(sqlite3, 'connect', original_connect)
    store = TaskStore(tmp_path)
    row = store.dashboard_task('legacy')
    assert row['created_at'] == '2026-01-02T03:04:05Z'
    assert row['publish_attempted'] == 1 and row['publish_state'] == 'unknown'
    before = row['revision']
    store.checkpoint('legacy', 'run', {'summary': 'changed'})
    assert store.dashboard_task('legacy')['revision'] > before


def test_context_is_bounded_does_not_truncate_url_batches_or_grant_publish(tmp_path):
    store = TaskStore(tmp_path)
    task = core_task(store, name='只下载，不发布', state='failed')
    urls = ['https://t.me/public_channel/' + str(index) + ('a' * 3500) for index in range(20)]
    report = {'summary': 'result' * 20_000, 'steps': [{'step_id': 'step-1', 'label': 'label' * 5000}],
              'calls': {'download': {'tool': 'download_media', 'status': 'running',
                                    'resume_input': {'urls': urls, 'approval_token': 'SECRET'}}}}
    store.checkpoint(task, 'run-' + task, report, state='failed')
    before = store.execution(task)
    selected_text = store.selected_task_context(task, 'conversation')
    selected = json.loads(selected_text)
    chain = store.resume_context('conversation', task)
    assert len(selected_text) <= CONTEXT_LIMIT
    assert len(json.dumps(chain, ensure_ascii=False)) <= CONTEXT_LIMIT
    assert len(store.model_context('conversation')) <= CONTEXT_LIMIT
    assert selected['context_truncated'] and chain['context_truncated']
    assert selected['selected_resume_task']['resume_evidence'] == []
    assert 'SECRET' not in selected_text
    assert store.execution(task) == before
    assert resolve_write_actions({'resume_turn_id': task}, '重试', store.history('conversation'))[0] == []
    with pytest.raises(ValueError, match='不属于'):
        store.selected_task_context(task, 'other-conversation')


def test_context_reads_do_not_clear_real_publish_attempts(tmp_path):
    store = TaskStore(tmp_path)
    task = store.ensure_task('conversation', '发布到X')
    store.start(task, 'run')
    store.reserve_publish(task, 'run')
    store.checkpoint(task, 'run', {'summary': 'unknown'}, state='failed')
    before = store.execution(task)
    for _ in range(2):
        reopened = TaskStore(tmp_path)
        reopened.dashboard(summary=True)
        reopened.selected_task_context(task, 'conversation')
        reopened.resume_context('conversation', task)
        reopened.history('conversation')
    assert store.execution(task) == before
    with pytest.raises(ValueError, match='already attempted'):
        store.reserve_publish(task, 'run')
