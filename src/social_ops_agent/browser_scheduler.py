"""Cross-conversation, cross-process workflow leases for BitBrowser windows.

The core holds these through the whole workflow INCLUDING resource cleanup.
They are separate from the plugin's short operation locks: a plugin subprocess
must not deadlock trying to reacquire a lock already held by its own core.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Event
from urllib.parse import urlsplit

from .session_store import SessionStore
from .browser_queue import BrowserQueueTicket, open_lock
from .process_locks import _try_lock, _unlock


class BrowserWaitCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowResource:
    key: str
    label: str


def resources_for_plan(plan, registry_path: Path) -> list[WindowResource]:
    store = SessionStore(registry_path)
    resources = {}
    for binding in plan.authorized_browser_sessions():
        record = store.get(binding.session_ref)
        if record is None:
            raise ValueError("计划使用的浏览器窗口已被移除，请重新生成任务。")
        parts = urlsplit(record.api_url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("浏览器调度只允许本机比特浏览器 API。")
        # Aliases and platform-specific session refs for the SAME profile share one lease.
        identity = f"loopback:{parts.port or 80}|{record.profile_id}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        resources[key] = WindowResource(key, record.profile_name or binding.profile_name or "比特浏览器窗口")
    return sorted(resources.values(), key=lambda item: item.key)


class BrowserTaskScheduler:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(tempfile.gettempdir()) / "social-agent-workflow-leases"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def reserve(self, resources: list[WindowResource], *, conversation_id: str,
                execution_id: str, cancelled: Event, on_wait=None):
        resources = sorted({item.key: item for item in resources}.values(), key=lambda item: item.key)
        streams = []
        acquired = []
        previous_wait = None
        tickets = ExitStack()
        try:
            ticket = tickets.enter_context(BrowserQueueTicket(self.root, resources, conversation_id)) if resources else None
            for item in resources:
                stream = open_lock(self.root / f"{item.key}.lock")
                streams.append((item, stream))
            while True:
                if cancelled.is_set():
                    raise BrowserWaitCancelled("已取消等待浏览器窗口。")
                ahead = ticket.predecessor() if ticket else None
                if ahead is not None:
                    key, owner = ahead
                    wait_state = (key, owner)
                    if wait_state != previous_wait and on_wait:
                        on_wait(next(item.label for item in resources if item.key == key), owner)
                        previous_wait = wait_state
                    cancelled.wait(0.15)
                    continue
                busy = None
                for item, stream in streams:
                    if not _try_lock(stream):
                        busy = (item, stream)
                        break
                    acquired.append(stream)
                if busy is None:
                    break
                # All-or-none acquisition: never hold A while waiting for B.
                for stream in reversed(acquired):
                    _unlock(stream)
                acquired.clear()
                item, stream = busy
                try:
                    stream.seek(1)
                    owner = json.loads(stream.read(4096)).get("conversation_id", "")
                except (ValueError, OSError):
                    owner = ""
                wait_state = (item.key, owner)
                if wait_state != previous_wait and on_wait:
                    on_wait(item.label, owner)
                    previous_wait = wait_state
                cancelled.wait(0.15)
            if cancelled.is_set():
                raise BrowserWaitCancelled("已取消等待浏览器窗口。")
            owner = json.dumps({"conversation_id": conversation_id, "execution_id": execution_id,
                                "pid": os.getpid()}).encode()
            for _item, stream in streams:
                stream.seek(1)
                stream.write(owner)
                stream.truncate()
            yield
        finally:
            try:
                for stream in reversed(acquired):
                    _unlock(stream)
            finally:
                for _item, stream in streams:
                    stream.close()
                tickets.close()
