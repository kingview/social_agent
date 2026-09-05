"""Durable, item-checkpointed jobs shared by toolbox and Agent material tools."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .diagnostics import record_exception
from .browser_queue import open_lock
from .process_locks import _try_lock, _unlock
from .material_task_state import STATES, RESUMABLE, MaterialJobInterrupted, item_status, outcome


class MaterialJobs:
    def __init__(self, state_root: Path):
        self.root = Path(state_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'material-tasks.sqlite3'
        if self._schema_ready():
            return
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, tool TEXT, name TEXT, conversation_id TEXT,
                parameters_json TEXT, items_json TEXT, results_json TEXT,
                state TEXT, command TEXT, created_at TEXT, updated_at TEXT,
                error TEXT, cursor INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,
                    job_id TEXT, action TEXT, time TEXT, detail TEXT);''')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_tool_state ON jobs(tool,state,created_at)')

    def _schema_ready(self):
        """Opening an existing task repository must not reserve its writer lock."""
        if not self.path.is_file():
            return False
        with self.db(write=False) as db:
            names = {row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('jobs','events','jobs_tool_state')"
            )}
        return names == {'jobs', 'events', 'jobs_tool_state'}

    @contextmanager
    def db(self, *, write=True):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, tool, items, parameters, *, name=None, conversation_id=None):
        if not items:
            raise ValueError('没有可处理的项目')
        job_id, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        with self.db() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, tool, name or tool, conversation_id, json.dumps(parameters, ensure_ascii=False),
                 json.dumps(items, ensure_ascii=False), '{}', '待执行', '', now, now, '', 0))
        return job_id

    def get(self, job_id):
        with self.db(write=False) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        return self._decode(row)

    @staticmethod
    def _decode(row):
        item = dict(row)
        for field in ('parameters', 'items', 'results'):
            item[field] = json.loads(item.pop(field + '_json'))
        item['completed'] = sum(r.get('status') == 'completed' for r in item['results'].values())
        item['total'] = len(item['items'])
        return item

    def list(self, *, tool=None, state=None, states=None, query='', limit=None, offset=0, summary=False):
        clauses, params = [], []
        for key, value in (('tool',tool),('state',state)):
            if value is not None:
                clauses.append(key+'=?'); params.append(str(value))
        if states is not None:
            if not states:
                return []
            clauses.append('state IN ('+','.join('?' for _ in states)+')')
            params.extend(str(value) for value in states)
        if query:
            clauses.append('(instr(lower(name),lower(?))>0 OR instr(lower(id),lower(?))>0 OR instr(lower(tool),lower(?))>0)')
            params.extend([query]*3)
        columns = '''id,tool,name,conversation_id,state,command,created_at,updated_at,error,cursor,
            json_array_length(items_json) AS total,
            (SELECT count(*) FROM json_each(results_json) WHERE json_extract(value,'$.status')='completed') AS completed''' if summary else '*'
        sql = 'SELECT '+columns+' FROM jobs'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY created_at DESC,id DESC'
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'; params.extend([max(0,int(limit)),max(0,int(offset))])
        with self.db(write=False) as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) if summary else self._decode(row) for row in rows]

    def transition(self, job_id, state, *, error=''):
        if state not in STATES:
            raise ValueError('无效任务状态')
        now = datetime.now(timezone.utc).isoformat()
        with self.db() as db:
            row = db.execute('SELECT command FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            # A stop may supersede a pause between the worker's check and ACK.
            if state == '已暂停' and row['command'] == 'stop':
                state = '已停止'
            command = row['command'] if state in {'待执行', '执行中'} else ''
            db.execute(
                'UPDATE jobs SET state=?,command=?,error=?,updated_at=? WHERE id=?',
                (state, command, error, now, job_id),
            )
            db.execute('INSERT INTO events(job_id,action,time,detail) VALUES(?,?,?,?)', (job_id, state, now, error))

    def command(self, job_id, command):
        if command not in {'pause', 'stop', 'resume', 'retry'}:
            raise ValueError('无效任务操作')
        with self.db() as db:
            row = db.execute('SELECT state,command FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            if command in {'resume', 'retry'}:
                if row['state'] not in RESUMABLE:
                    raise ValueError('任务当前不可继续或重试')
                db.execute("UPDATE jobs SET command='',state='待执行',error='' WHERE id=?", (job_id,))
            elif row['state'] in {'执行中', '待执行'}:
                if row['command'] == 'stop' and command == 'pause':
                    raise ValueError('任务正在停止，不能改为暂停')
                if row['state'] == '待执行':
                    # No running handler needs to acknowledge a queued command.
                    db.execute("UPDATE jobs SET state=?,command='' WHERE id=?", (
                        '已暂停' if command == 'pause' else '已停止', job_id,
                    ))
                else:
                    db.execute('UPDATE jobs SET command=? WHERE id=?', (command, job_id))
            else:
                raise ValueError('任务当前不可暂停或停止')
            db.execute('UPDATE jobs SET updated_at=? WHERE id=?', (datetime.now(timezone.utc).isoformat(),job_id))
            db.execute('INSERT INTO events(job_id,action,time,detail) VALUES(?,?,?,?)',
                (job_id, command, datetime.now(timezone.utc).isoformat(), ''))

    def checkpoint(self, job_id, index, result):
        with self.db() as db:
            row = db.execute('SELECT results_json FROM jobs WHERE id=?', (job_id,)).fetchone()
            results = json.loads(row[0])
            results[str(index)] = result
            db.execute('UPDATE jobs SET results_json=?,cursor=?,updated_at=? WHERE id=?',
                (json.dumps(results, ensure_ascii=False), index+1, datetime.now(timezone.utc).isoformat(), job_id))

    def claim(self, job_id):
        with self.db() as db:
            return db.execute("UPDATE jobs SET state='执行中',error='',updated_at=? WHERE id=? AND state='待执行' AND command=''", (datetime.now(timezone.utc).isoformat(),job_id)).rowcount == 1

    def recover_interrupted(self, callback=None):
        recovered = []
        for row in self.list(states={'执行中','待执行'}):
            with self.execution_lock(row['id']) as acquired:
                if acquired:
                    current = self.get(row['id'])
                    if current['state'] not in {'执行中','待执行'}:
                        continue
                    if callback is not None:
                        callback(current)
                    with self.db() as db:
                        latest = db.execute('SELECT state,command FROM jobs WHERE id=?', (row['id'],)).fetchone()
                        if latest is None or latest['state'] not in {'执行中', '待执行'}:
                            continue
                        state = '已停止' if latest['command'] == 'stop' else '已暂停'
                        now = datetime.now(timezone.utc).isoformat()
                        detail = '应用退出导致中断；已完成结果保留，可继续'
                        db.execute(
                            "UPDATE jobs SET state=?,command='',error=?,updated_at=? WHERE id=?",
                            (state, detail, now, row['id']),
                        )
                        db.execute('INSERT INTO events(job_id,action,time,detail) VALUES(?,?,?,?)',
                                   (row['id'], '恢复中断任务', now, detail))
                        recovered.append(row['id'])
        return recovered

    def check_control(self, job_id):
        with self.db(write=False) as db:
            row = db.execute('SELECT state,command FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        if row['command']=='stop' or row['state']=='已停止':
            raise MaterialJobInterrupted('stop')
        if row['command']=='pause' or row['state']=='已暂停':
            raise MaterialJobInterrupted('pause')

    @contextmanager
    def execution_lock(self, job_id):
        # OS locks survive separate MCP/GUI processes and release on a crash.
        if not isinstance(job_id, str) or not job_id.isalnum():
            raise ValueError('无效任务 ID')
        directory = self.root / 'material-job-locks'
        directory.mkdir(parents=True, exist_ok=True)
        with open_lock(directory / (job_id + '.lock')) as stream:
            acquired = _try_lock(stream)
            try:
                yield acquired
            finally:
                if acquired:
                    _unlock(stream)


class MaterialRunner:
    def __init__(self, jobs: MaterialJobs, handler, *, concurrency=2):
        self.jobs, self.handler = jobs, handler
        self.pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='materials')
        self.lock = threading.Lock()
        self.active = set()
        self._pending = set()
        self._closed = False

    def submit(self, job_id):
        with self.lock:
            if self._closed:
                raise RuntimeError('素材任务执行器已关闭')
            if job_id in self.active:
                # Resume can race the previous worker's final active-set cleanup.
                # Keep the request for that worker to consume after releasing its
                # execution lock, instead of silently dropping the queued run.
                self._pending.add(job_id)
                return
            self.active.add(job_id)
        try:
            self.pool.submit(self._run, job_id)
        except BaseException:
            with self.lock:
                self.active.discard(job_id)
                self._pending.discard(job_id)
            raise

    def run(self, job_id):
        """Synchronous entry for MCP; same locking/lifecycle as background jobs."""
        with self.lock:
            if self._closed:
                raise RuntimeError('素材任务执行器已关闭')
            owned = job_id not in self.active
            if owned:
                self.active.add(job_id)
            else:
                self._pending.add(job_id)
        if not owned:
            return self.jobs.get(job_id)
        self._run(job_id)
        return self.jobs.get(job_id)

    def _run(self, job_id):
        while True:
            try:
                with self.jobs.execution_lock(job_id) as acquired:
                    if acquired:
                        self._run_locked(job_id)
                if not acquired and self._queued_behind_worker(job_id):
                    # Another process can still be releasing a paused job after
                    # this process accepted its resume. Wait only for queued
                    # work; a running job never earns a duplicate invocation.
                    time.sleep(.05)
                    continue
            except BaseException:
                with self.lock:
                    self.active.discard(job_id)
                    self._pending.discard(job_id)
                raise
            with self.lock:
                rerun = job_id in self._pending and not self._closed
                self._pending.discard(job_id)
                if not rerun:
                    self.active.discard(job_id)
            if not rerun:
                return

    def _queued_behind_worker(self, job_id):
        with self.lock:
            if self._closed:
                return False
        row = self.jobs.get(job_id)
        return row['state'] == '待执行' and not row['command']

    def _run_locked(self, job_id):
        try:
            row = self.jobs.get(job_id)
            if not self.jobs.claim(job_id):
                return
            for index, item in enumerate(row['items']):
                current = self.jobs.get(job_id)
                self.jobs.check_control(job_id)
                if current['results'].get(str(index), {}).get('status') == 'completed':
                    continue
                try:
                    result = self.handler(row['tool'], item, row['parameters'], job_id)
                    status = item_status(result)
                    self.jobs.checkpoint(job_id, index, {'status': status, 'result': result})
                except MaterialJobInterrupted:
                    raise
                except Exception as exc:
                    record_exception('agent', 'materials.item', exc, state_root=self.jobs.root, task_id=job_id)
                    self.jobs.checkpoint(job_id, index, {'status': 'failed', 'error': str(exc)})
                    if any(word in str(exc).lower() for word in ('captcha', '验证码', '登录失效', '人工验证', 'login required')):
                        self.jobs.transition(job_id, '待人工处理', error=str(exc))
                        return
            result = self.jobs.get(job_id)
            self.jobs.transition(job_id, outcome(result['results'],result['total']))
        except MaterialJobInterrupted as exc:
            self.jobs.transition(job_id, '已停止' if exc.command=='stop' else '已暂停')
        except Exception as exc:
            record_exception('agent', 'materials.job', exc, state_root=self.jobs.root, task_id=job_id)
            self.jobs.transition(job_id, '执行失败', error=str(exc))

    def close(self):
        with self.lock:
            self._closed = True
            self._pending.clear()
            active = list(self.active)
        for job_id in active:
            try:
                if self.jobs.get(job_id)['state'] in {'执行中','待执行'}:
                    self.jobs.command(job_id, 'pause')
            except ValueError:
                pass  # The worker may have committed completion meanwhile.
        self.pool.shutdown(wait=False)
