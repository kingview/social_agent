"""FIFO admission for overlapping window sets, shared by all local Agent processes.

Each ticket has an OS-locked liveness file. Process death releases it, so stale
queue entries can be reaped without timeouts that might evict a slow live task.
The metadata guard is held only for short queue transactions, never while waiting.
"""
from contextlib import contextmanager
import json
import os
from threading import Event
import uuid

from .state_io import write_json
from .process_locks import _try_lock, _unlock


def open_lock(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b", buffering=0)
    if os.fstat(descriptor).st_size == 0:
        stream.write(b" ")
    return stream


class BrowserQueueTicket:
    def __init__(self, root, resources, conversation_id):
        self.root = root
        self.keys = {item.key for item in resources}
        self.owner = conversation_id
        self.token = uuid.uuid4().hex
        self.path = root / f"ticket-{self.token}.lock"
        self.stream = None

    @contextmanager
    def _guard(self):
        with open_lock(self.root / "queue.lock") as guard:
            while not _try_lock(guard):
                Event().wait(0.02)
            try:
                yield
            finally:
                _unlock(guard)

    def _read(self):
        try:
            payload = json.loads((self.root / "queue.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        # Corruption must fail closed instead of silently allowing queue-jumping.
        if not isinstance(payload, dict) or not isinstance(payload.get("tickets"), list):
            raise ValueError("浏览器等待队列损坏，请检查日志。")
        return payload["tickets"]

    def _save(self, rows):
        write_json(self.root / "queue.json", {"version": 1, "tickets": rows})

    def __enter__(self):
        self.stream = open_lock(self.path)
        try:
            if not _try_lock(self.stream):
                raise RuntimeError("Unable to own browser queue ticket")
            with self._guard():
                rows = self._live(self._read())
                rows.append({"token": self.token, "keys": sorted(self.keys), "owner": self.owner})
                self._save(rows)
            return self
        except BaseException:
            self.stream.close()
            self.path.unlink(missing_ok=True)
            raise

    def _live(self, rows):
        live = []
        for row in rows:
            token = row["token"]
            if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
                raise ValueError("Invalid browser queue ticket")
            if token == self.token:
                live.append(row)
                continue
            path = self.root / f"ticket-{token}.lock"
            with open_lock(path) as probe:
                if not _try_lock(probe):
                    live.append(row)
                    continue
                _unlock(probe)
            path.unlink(missing_ok=True)
        return live

    def predecessor(self):
        with self._guard():
            rows = self._read()
            live = self._live(rows)
            if live != rows:
                self._save(live)
            for row in live:
                if row["token"] == self.token:
                    return None
                overlap = self.keys.intersection(row["keys"])
                if overlap:
                    return min(overlap), row["owner"]
            raise RuntimeError("Browser queue ticket disappeared")

    def __exit__(self, *args):
        try:
            with self._guard():
                self._save([row for row in self._read() if row["token"] != self.token])
        finally:
            self.stream.close()
            self.path.unlink(missing_ok=True)
