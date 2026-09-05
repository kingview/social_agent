"""Authoritative task history and execution journal, independent of any UI.

SQLite transactions also cover the MCP subprocess. Only allowlisted resume
targets/results are stored, never raw arguments, cookies or approval tokens.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .policy import requested_write_actions
from .resume_evidence import recover_legacy_calls, evidence_summary, resume_input, resume_output


CONTEXT_LIMIT = 20_000


def _object(value):
    """Legacy history may contain nulls or incomplete documents."""
    try:
        result = json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _documents(record):
    legacy = _object(record['legacy_json'])
    plan = _object(record['plan_json']) or legacy.get('plan') or {}
    result = _object(record['execution_json']) or legacy.get('result') or {}
    return legacy, plan if isinstance(plan, dict) else {}, result if isinstance(result, dict) else {}


def _steps(steps):
    result = []
    for step in (steps if isinstance(steps, list) else [])[:12]:
        if isinstance(step, str):
            result.append(step[:500])
        elif isinstance(step, dict):
            result.append({key: value[:500] if isinstance(value, str) else value
                           for key, value in step.items()
                           if key in {'step_id','title','label','tool','status','units','completed_units','error'}
                           and isinstance(value, (str, int, float, bool))})
    return result


def _bounded_context(payload):
    """Bound valid JSON; remove whole evidence records, never shorten URL batches."""
    if len(json.dumps(payload, ensure_ascii=False)) <= CONTEXT_LIMIT:
        return payload
    payload['context_truncated'] = True
    payload['context_notice'] = '部分上下文因长度限制被省略；不得猜测省略的下载目标或据此新增操作授权。'
    rows = payload.get('tasks') or payload.get('recent_turns') or [payload.get('selected_resume_task', {})]
    # Journal-backed execution targets remain atomic: an oversized batch is
    # omitted and marked, rather than silently changing the requested batch.
    for row in rows:
        evidence = row.get('resume_evidence', [])
        while evidence and len(json.dumps(payload, ensure_ascii=False)) > CONTEXT_LIMIT:
            evidence.pop()
            row['resume_evidence_truncated'] = True
    if len(json.dumps(payload, ensure_ascii=False)) <= CONTEXT_LIMIT:
        return payload
    for row in rows:
        if isinstance(row.get('plan'), dict):
            row['plan'] = {key: row['plan'][key] for key in ('summary', 'steps', 'step_tools', 'resume_turn_id') if key in row['plan']}
        if isinstance(row.get('result'), dict):
            row['result'] = {key: row['result'][key] for key in ('summary', 'error', 'steps', 'publish_state') if key in row['result']}
    for size in (500, 200, 80):
        def shorten(value):
            if isinstance(value, str):
                return value[:size]
            if isinstance(value, dict):
                return {key: shorten(child) for key, child in value.items()}
            if isinstance(value, list):
                return [shorten(child) for child in value]
            return value
        # Evidence was already exhausted above; identity keys must stay exact.
        for row in rows:
            for key in tuple(row):
                if key not in {'turn_id', 'resume_turn_id', 'resume_evidence'}:
                    row[key] = shorten(row[key])
        if len(json.dumps(payload, ensure_ascii=False)) <= CONTEXT_LIMIT:
            return payload
    # At most twenty lineage records, and only allowlisted fields, reach here.
    # Keep original and selected tasks instead of retaining enormous middles.
    while len(rows) > 2 and len(json.dumps(payload, ensure_ascii=False)) > CONTEXT_LIMIT:
        rows.pop(1)
    if len(json.dumps(payload, ensure_ascii=False)) > CONTEXT_LIMIT:
        return {'context_truncated': True, 'context_notice': payload['context_notice']}
    return payload


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    message: str
    resume_task_id: str | None = None
    publish_attempted: bool = False
    legacy_interrupted: bool = False


class TaskStore:
    def __init__(self, state_root: Path) -> None:
        self.path = state_root / "tasks.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file() and self._schema_current():
            self.path.chmod(0o600)
            return
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                message TEXT NOT NULL, resume_task_id TEXT, plan_json TEXT,
                legacy_json TEXT, run_id TEXT, state TEXT NOT NULL DEFAULT 'planning',
                execution_json TEXT, publish_attempted INTEGER NOT NULL DEFAULT 0,
                publish_state TEXT NOT NULL DEFAULT 'not_attempted')""")
            db.execute("CREATE INDEX IF NOT EXISTS task_conversation ON tasks(conversation_id)")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            for name, declaration in (('created_at',"TEXT NOT NULL DEFAULT ''"),
                                      ('updated_at',"TEXT NOT NULL DEFAULT ''"),
                                      ('revision','INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {declaration}')
            db.execute("UPDATE tasks SET created_at=COALESCE(CASE WHEN json_valid(legacy_json) THEN json_extract(legacy_json,'$.created_at') END,strftime('%Y-%m-%dT%H:%M:%fZ','now')),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE created_at=''")
            db.execute('''CREATE TRIGGER IF NOT EXISTS task_insert_clock AFTER INSERT ON tasks
                WHEN NEW.created_at='' BEGIN
                UPDATE tasks SET created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=NEW.task_id; END''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS task_update_clock AFTER UPDATE ON tasks
                WHEN NEW.revision=OLD.revision BEGIN
                UPDATE tasks SET revision=OLD.revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=NEW.task_id; END''')
            # Earlier GUI imports inferred 'attempted' merely from a tool call,
            # including requests rejected BEFORE reserve_publish. A real core
            # reservation atomically changes BOTH fields (state becomes unknown).
            # Repair only this impossible combination; never clear unknown,
            # failed, published, or pre-journal legacy attempts.
            db.execute("""UPDATE tasks SET publish_attempted=0
                WHERE run_id IS NOT NULL AND publish_attempted=1 AND publish_state='not_attempted'""")
        self.path.chmod(0o600)

    def _schema_current(self):
        with self._db(write=False) as db:
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            if not {'created_at', 'updated_at', 'revision', 'publish_state', 'run_id'} <= columns:
                return False
            triggers = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            if not {'task_insert_clock', 'task_update_clock'} <= triggers:
                return False
            return db.execute("""SELECT 1 FROM tasks WHERE created_at='' OR
                (run_id IS NOT NULL AND publish_attempted=1 AND publish_state='not_attempted') LIMIT 1""").fetchone() is None

    @contextmanager
    def _db(self, *, write=True):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            # Serializes read/check/write operations across backend and MCP.
            if not write:
                db.execute('PRAGMA query_only=ON')
            db.execute("BEGIN IMMEDIATE" if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_task(self, conversation_id: str, message: str, task_id: str | None = None) -> str:
        task_id = task_id or f"turn-{uuid.uuid4().hex}"
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is not None:
                if row["conversation_id"] != conversation_id or row["message"] != message:
                    raise ValueError("Task does not belong to this conversation and original user message")
            else:
                db.execute("INSERT INTO tasks(task_id,conversation_id,message) VALUES(?,?,?)",
                           (task_id, conversation_id, message))
        return task_id

    def import_turn(self, conversation_id: str, turn: dict) -> None:
        """Idempotent legacy migration/display mirroring; never erase core facts."""
        task_id = self.ensure_task(conversation_id, turn["user_message"], turn["turn_id"])
        plan = turn.get("plan") or {}
        result = turn.get("result") or {}
        attempted = bool(turn.get("publish_attempted") or
                         "mcp__social__publish_x_post" in result.get("tool_calls", []) or
                         result.get("publish_state") in {"published", "failed", "unknown"})
        with self._db() as db:
            db.execute("""UPDATE tasks SET legacy_json=?,
                resume_task_id=CASE WHEN plan_json IS NULL THEN ? ELSE resume_task_id END,
                publish_attempted=CASE WHEN run_id IS NULL THEN MAX(publish_attempted,?)
                                      ELSE publish_attempted END WHERE task_id=?""",
                (json.dumps(turn, ensure_ascii=False), plan.get("resume_turn_id"), attempted, task_id))

    def history(self, conversation_id: str, *, exclude: str | None = None) -> tuple[TaskRecord, ...]:
        with self._db(write=False) as db:
            rows = db.execute("""SELECT task_id,message,resume_task_id,publish_attempted,run_id,
                CASE WHEN json_valid(execution_json) THEN json_extract(execution_json,'$.publish_state') END AS legacy_publish_state,
                CASE WHEN json_valid(legacy_json) THEN json_extract(legacy_json,'$.error_stage') END AS legacy_error_stage
                FROM tasks WHERE conversation_id=?""", (conversation_id,)).fetchall()
        return tuple(TaskRecord(
            row["task_id"], row["message"], row["resume_task_id"],
            bool(row["publish_attempted"]) or (not row["run_id"] and
                row['legacy_publish_state'] in {"published", "failed", "unknown"}),
            not row["run_id"] and row['legacy_error_stage'] == "interrupted",
        ) for row in rows if row["task_id"] != exclude)

    def dashboard(self, *, query='', states=None, limit=500, offset=0, summary=False) -> list[dict]:
        from .task_dashboard import query_tasks
        return query_tasks(self,query=query,states=states,limit=limit,offset=offset,summary=summary)

    def dashboard_task(self, task_id):
        from .task_dashboard import query_tasks
        rows = query_tasks(self,task_id=task_id,limit=1)
        if not rows:
            raise ValueError('任务不存在')
        return rows[0]

    def selected_task_context(self, task_id, conversation_id):
        """Read the exact selected task for Harness, without minting new authority."""
        with self._db(write=False) as db:
            row = db.execute('SELECT * FROM tasks WHERE task_id=? AND conversation_id=?',
                             (task_id,conversation_id)).fetchone()
        if row is None:
            raise ValueError('任务不属于当前对话')
        legacy, plan, result = _documents(row)
        plan_view = {key: plan[key][:300] for key in ('platform','session_ref','resume_turn_id') if isinstance(plan.get(key), str)}
        plan_view['step_tools'] = [tool[:100] for tool in (plan.get('step_tools') or [])[:12] if isinstance(tool, str)] if isinstance(plan.get('step_tools'), list) else []
        plan_view['step_units'] = [unit for unit in (plan.get('step_units') or [])[:12] if type(unit) is int and 0 <= unit <= 1_000_000] if isinstance(plan.get('step_units'), list) else []
        plan_view.update(summary=str(plan.get('summary') or '')[:1000],
                         objective=str(plan.get('objective') or '')[:2000], steps=_steps(plan.get('steps')))
        result_view = {key: result[key] for key in ('completed_steps','total_steps') if type(result.get(key)) is int and 0 <= result[key] <= 1_000_000}
        result_view.update(completion_status=str(result.get('completion_status') or '')[:100], cancelled=result.get('cancelled') is True)
        result_view.update(summary=str(result.get('summary') or '')[:2000],
                           error=str(result.get('error') or legacy.get('error') or '')[:2000],
                           steps=_steps(result.get('steps')), publish_state=row['publish_state'] if row['run_id'] else result.get('publish_state'))
        return json.dumps(_bounded_context({'selected_resume_task':{
            'turn_id':task_id,'original_user_message':row['message'][:1500],
            'plan':plan_view, 'result':result_view,
            'publish_attempted':bool(row['publish_attempted']),
            'resume_evidence':self._resume_evidence(row),
        }, 'context_only': True}),ensure_ascii=False)

    def model_context(self, conversation_id: str, *, exclude: str | None = None) -> str | None:
        """A bounded presentation, never passed to authorization validation."""
        with self._db(write=False) as db:
            records = db.execute("SELECT * FROM tasks WHERE conversation_id=? AND (? IS NULL OR task_id!=?) ORDER BY rowid DESC LIMIT 40",
                                 (conversation_id,exclude,exclude)).fetchall()
        rows = []
        for record in records:
            if record["task_id"] == exclude:
                continue
            legacy, plan, result = _documents(record)
            row = {
                "turn_id": record["task_id"], "user_message": record["message"][:1500],
                "requested_write_actions": list(requested_write_actions(record["message"])),
                "status": record["state"] if record["run_id"] else legacy.get("status", record["state"]),
                "platform": str(plan.get("platform") or legacy.get("platform") or '')[:300],
                "session_ref": str(plan.get("session_ref") or legacy.get("session_ref") or '')[:300],
                "plan_summary": str(plan.get("summary") or plan.get("objective") or "")[:1000],
                "result_summary": str(result.get("summary") or "")[:2000],
                "error_stage": "execution" if result.get("error") else legacy.get("error_stage"),
                "error": str(result.get("error") or legacy.get("error") or "")[:2000],
                "resume_turn_id": record["resume_task_id"],
                "publish_attempted": bool(record["publish_attempted"]),
                "publish_state": record["publish_state"] if record["run_id"] else result.get("publish_state"),
            }
            if len(rows) < 8:
                row['resume_evidence'] = self._resume_evidence(record)
            candidate = [row, *rows]
            if rows and len(json.dumps({"recent_turns": candidate}, ensure_ascii=False)) > 20000:
                break
            rows = candidate
        return json.dumps(_bounded_context({"recent_turns": rows}), ensure_ascii=False) if rows else None

    def _resume_evidence(self, record) -> list[dict]:
        _, _, report = _documents(record)
        calls = report.get('calls') or {}
        if not isinstance(calls, dict):
            calls = {}
        recovered = recover_legacy_calls(self.path.parent / 'harness-sessions' / 'execute',
                                         record['conversation_id'], calls)
        safe = []
        for item in evidence_summary(recovered):
            tool = item.get('tool')
            entry = {'tool': tool, 'status': str(item.get('status') or '')[:100]}
            original_input = item.get('resume_input')
            safe_input = resume_input(tool, original_input)
            if safe_input:
                entry['resume_input'] = safe_input
            output = item.get('resume_output')
            if isinstance(output, dict):
                if tool == 'browse_posts' and isinstance(output.get('post_urls'), list):
                    safe_output = resume_output(tool, {'posts': [{'url': value} for value in output['post_urls']]})
                elif tool == 'download_media':
                    safe_output = resume_output(tool, {**output, 'artifacts': [{'path': value} for value in output.get('artifact_paths', [])]})
                else:
                    safe_output = {}
                if safe_output:
                    entry['resume_output'] = safe_output
            if entry.get('resume_input') or entry.get('resume_output'):
                safe.append(entry)
        return safe

    def resume_context(self, conversation_id: str, task_id: str | None) -> dict | None:
        if not task_id:
            return None
        chain, visited = [], set()
        while task_id and task_id not in visited and len(chain) < 20:
            visited.add(task_id)
            with self._db(write=False) as db:
                row = db.execute('SELECT * FROM tasks WHERE task_id=? AND conversation_id=?',
                                 (task_id, conversation_id)).fetchone()
            if row is None:
                raise ValueError('Cannot resume a task outside this conversation')
            legacy, plan, report = _documents(row)
            chain.append({'turn_id': task_id, 'user_message': row['message'][:1500],
                          'state': row['state'] if row['run_id'] else legacy.get('status', row['state']),
                          'steps': _steps(report.get('steps') or plan.get('steps')),
                          'resume_evidence': self._resume_evidence(row)})
            task_id = row['resume_task_id'] or plan.get('resume_turn_id')
        return _bounded_context({'tasks': list(reversed(chain))})

    def planning_failed(self, task_id: str, error: str) -> None:
        with self._db() as db:
            db.execute("UPDATE tasks SET state='failed',execution_json=? WHERE task_id=? AND run_id IS NULL",
                       (json.dumps({"error": error[:20000]}, ensure_ascii=False), task_id))

    def recover_interrupted(self, task_id: str) -> None:
        """Recover an unfinished desktop run without clearing any write marker."""
        with self._db() as db:
            row = db.execute("SELECT execution_json FROM tasks WHERE task_id=? AND state='executing'",
                             (task_id,)).fetchone()
            if row is None:
                return
            report = json.loads(row["execution_json"] or "{}")
            state = "partial" if report.get("completed_steps", 0) else "failed"
            report.update(completion_status=state, error="应用在该任务完成前退出；执行中断。")
            db.execute("UPDATE tasks SET state=?,execution_json=? WHERE task_id=?",
                       (state, json.dumps(report, ensure_ascii=False), task_id))

    def set_plan(self, task_id: str, plan: dict) -> None:
        clean = {**plan, "media_context": None}
        with self._db() as db:
            updated = db.execute("""UPDATE tasks SET plan_json=?,resume_task_id=?,state='planned'
                WHERE task_id=? AND run_id IS NULL""",
                (json.dumps(clean, ensure_ascii=False), plan.get("resume_turn_id"), task_id))
            if updated.rowcount != 1:
                raise ValueError("Task already executed; create a new turn to retry")

    def start(self, task_id: str, run_id: str) -> None:
        with self._db() as db:
            updated = db.execute("UPDATE tasks SET run_id=?,state='executing' WHERE task_id=? AND run_id IS NULL",
                                 (run_id, task_id))
            if updated.rowcount != 1:
                raise ValueError("Task already executed; create a new turn to retry")

    def checkpoint(self, task_id: str, run_id: str, report: dict, *, state: str = "executing") -> None:
        with self._db() as db:
            row = db.execute("SELECT publish_attempted,publish_state FROM tasks WHERE task_id=? AND run_id=?",
                             (task_id, run_id)).fetchone()
            if row is None:
                raise ValueError("Unknown task execution")
            report = dict(report)
            if row["publish_attempted"] or report.get("publish_state") != "not_requested":
                report["publish_state"] = row["publish_state"]
            db.execute("UPDATE tasks SET execution_json=?,state=? WHERE task_id=? AND run_id=?",
                       (json.dumps(report, ensure_ascii=False), state, task_id, run_id))

    def validate_publish_inputs(self, task_id: str, run_id: str, *, has_media: bool) -> None:
        """Validate planned prerequisites against durable tool results, not model claims.

        Rejected preparation must not consume the one-shot publication grant.
        The planner decides text-only vs media; old plans conservatively retain
        their download/repair/attachment requirement, including resumed tasks.
        """
        with self._db(write=False) as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone()
            if row is None:
                raise ValueError("Unknown publication task execution")
            plan = json.loads(row["plan_json"] or "{}")
            tools = plan.get("step_tools") or []
            if "publish_x_post" in tools:
                report = json.loads(row["execution_json"] or "{}")
                statuses = {s["step_id"]: s for s in report.get("steps", [])}
                incomplete = []
                for index, tool in enumerate(tools[:tools.index("publish_x_post")]):
                    if tool != "local_reasoning" and statuses.get(f"step-{index+1}", {}).get("status") != "completed":
                        incomplete.append(str(index + 1))
                if incomplete:
                    raise ValueError("发布前置步骤尚未完成（第 " + "、".join(incomplete) + " 步）；请先完成下载/分析等步骤，未执行发布。")
            required = plan.get("publish_media_required")
            source = row
            visited = set()
            while required is None:
                if any(t in {"download_media", "process_watermark"} for t in tools) or plan.get("attachments"):
                    required = True
                    break
                parent = source["resume_task_id"]
                if not parent or parent in visited:
                    break
                visited.add(parent)
                source = db.execute("SELECT * FROM tasks WHERE task_id=? AND conversation_id=?",
                                    (parent, row["conversation_id"])).fetchone()
                if source is None:
                    break
                plan = json.loads(source["plan_json"] or "{}")
                tools = plan.get("step_tools") or []
                required = plan.get("publish_media_required")
            if required and not has_media:
                raise ValueError("该任务要求携带媒体发布，但 media_paths 为空；未执行发布，也不会自动改为纯文字。")

    def reserve_publish(self, task_id: str, run_id: str) -> None:
        """Must commit BEFORE forwarding an external write, even with no GUI."""
        with self._db() as db:
            current = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if current is None:
                raise ValueError("Unknown publication task")
            rows = db.execute("SELECT * FROM tasks WHERE conversation_id=?", (current["conversation_id"],)).fetchall()
            by_id = {row["task_id"]: row for row in rows}
            root = current
            visited = set()
            while not requested_write_actions(root["message"]):
                if root["task_id"] in visited or root["resume_task_id"] not in by_id:
                    raise ValueError("No original publication request in task lineage")
                visited.add(root["task_id"])
                root = by_id[root["resume_task_id"]]
            lineage = {root["task_id"]}
            while True:
                expanded = lineage | {row["task_id"] for row in rows if row["resume_task_id"] in lineage}
                if expanded == lineage:
                    break
                lineage = expanded
            if any(row["publish_attempted"] for row in rows if row["task_id"] in lineage):
                raise ValueError("Publication already attempted in this task lineage")
            updated = db.execute("""UPDATE tasks SET publish_attempted=1,publish_state='unknown'
                WHERE task_id=? AND run_id=? AND state='executing' AND publish_attempted=0""", (task_id, run_id))
            if updated.rowcount != 1:
                raise ValueError("Publication already attempted or task execution is not active")

    def publish_result(self, task_id: str, run_id: str, state: str) -> None:
        state = state if state in {"published", "failed", "unknown"} else "unknown"
        with self._db() as db:
            db.execute("UPDATE tasks SET publish_state=? WHERE task_id=? AND run_id=? AND publish_attempted=1",
                       (state, task_id, run_id))

    def execution(self, task_id: str) -> dict | None:
        with self._db(write=False) as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or not row["run_id"]:
            return None
        report = json.loads(row["execution_json"] or "{}")
        if row["publish_attempted"] or report.get("publish_state") != "not_requested":
            report["publish_state"] = row["publish_state"]
        return {**report, "task_id": task_id, "run_id": row["run_id"], "state": row["state"],
                "publish_attempted": bool(row["publish_attempted"])}
