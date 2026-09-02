"""Authoritative task history and execution journal, independent of any UI.

SQLite transactions also cover the MCP subprocess. No tool arguments, cookies or
one-shot approval tokens are written here. active.json remains a display cache.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .policy import requested_write_actions


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
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                message TEXT NOT NULL, resume_task_id TEXT, plan_json TEXT,
                legacy_json TEXT, run_id TEXT, state TEXT NOT NULL DEFAULT 'planning',
                execution_json TEXT, publish_attempted INTEGER NOT NULL DEFAULT 0,
                publish_state TEXT NOT NULL DEFAULT 'not_attempted')""")
            db.execute("CREATE INDEX IF NOT EXISTS task_conversation ON tasks(conversation_id)")
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            # Serializes read/check/write operations across backend and MCP.
            db.execute("BEGIN IMMEDIATE")
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
                publish_attempted=MAX(publish_attempted,?) WHERE task_id=?""",
                (json.dumps(turn, ensure_ascii=False), plan.get("resume_turn_id"), attempted, task_id))

    def history(self, conversation_id: str, *, exclude: str | None = None) -> tuple[TaskRecord, ...]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM tasks WHERE conversation_id=?", (conversation_id,)).fetchall()
        return tuple(TaskRecord(
            row["task_id"], row["message"], row["resume_task_id"],
            bool(row["publish_attempted"]) or json.loads(row["execution_json"] or "{}").get("publish_state") in {"published", "failed", "unknown"},
            not row["run_id"] and json.loads(row["legacy_json"] or "{}").get("error_stage") == "interrupted",
        ) for row in rows if row["task_id"] != exclude)

    def model_context(self, conversation_id: str, *, exclude: str | None = None) -> str | None:
        """A bounded presentation, never passed to authorization validation."""
        with self._db() as db:
            records = db.execute("SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid DESC",
                                 (conversation_id,)).fetchall()
        rows = []
        for record in records:
            if record["task_id"] == exclude:
                continue
            legacy = json.loads(record["legacy_json"] or "{}")
            plan = json.loads(record["plan_json"] or "null") or legacy.get("plan") or {}
            result = json.loads(record["execution_json"] or "null") or legacy.get("result") or {}
            row = {
                "turn_id": record["task_id"], "user_message": record["message"][:1500],
                "requested_write_actions": list(requested_write_actions(record["message"])),
                "status": record["state"] if record["run_id"] else legacy.get("status", record["state"]),
                "platform": plan.get("platform") or legacy.get("platform"),
                "session_ref": plan.get("session_ref") or legacy.get("session_ref"),
                "plan_summary": str(plan.get("summary") or plan.get("objective") or "")[:1000],
                "result_summary": str(result.get("summary") or "")[:2000],
                "error_stage": "execution" if result.get("error") else legacy.get("error_stage"),
                "error": str(result.get("error") or legacy.get("error") or "")[:2000],
                "resume_turn_id": record["resume_task_id"],
                "publish_attempted": bool(record["publish_attempted"]),
                "publish_state": record["publish_state"] if record["publish_attempted"] else result.get("publish_state"),
            }
            candidate = [row, *rows]
            if rows and len(json.dumps({"recent_turns": candidate}, ensure_ascii=False)) > 20000:
                break
            rows = candidate
        return json.dumps({"recent_turns": rows}, ensure_ascii=False) if rows else None

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
            if row["publish_attempted"] and report.get("publish_state") in {"not_attempted", "not_requested"}:
                report["publish_state"] = row["publish_state"]
            db.execute("UPDATE tasks SET execution_json=?,state=? WHERE task_id=? AND run_id=?",
                       (json.dumps(report, ensure_ascii=False), state, task_id, run_id))

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
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or not row["run_id"]:
            return None
        report = json.loads(row["execution_json"] or "{}")
        if row["publish_attempted"]:
            report["publish_state"] = row["publish_state"]
        return {**report, "task_id": task_id, "run_id": row["run_id"], "state": row["state"],
                "publish_attempted": bool(row["publish_attempted"])}
