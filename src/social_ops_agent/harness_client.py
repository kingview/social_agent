from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


JsonObject = dict[str, Any]


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HarnessTurnResult:
    session_id: str
    final_response: str
    finish_reason: str | None
    events: list[JsonObject]
    tool_calls: list[str]


class HarnessJsonRpcClient:
    """Small synchronous client for DeepSeek Harness' stdio JSON-RPC runtime."""

    def __init__(
        self,
        *,
        launch_args: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float = 1_800,
    ) -> None:
        self._launch_args = list(launch_args)
        self._cwd = cwd.resolve()
        self._env = dict(env)
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._responses: dict[str, queue.Queue[object]] = {}
        self._notifications: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=200)
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    def start(self, *, provider: str, model: str, max_tokens: int) -> None:
        if self._process is not None:
            return
        environment = os.environ.copy()
        environment.update(self._env)
        self._process = subprocess.Popen(
            self._launch_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self._cwd,
            env=environment,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="social-agent-harness-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="social-agent-harness-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        self._request(
            "initialize",
            {
                "cwd": str(self._cwd),
                "provider": provider,
                "model": model,
                "maxTokens": max_tokens,
            },
            timeout_seconds=120,
        )

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._request("shutdown", None, timeout_seconds=2)
            except Exception:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self._process = None

    def cancel(self) -> None:
        """Cancel the active turn by terminating its isolated runtime process."""
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def run_turn(
        self,
        *,
        session_id: str,
        prompt: str | None = None,
        content_blocks: list[JsonObject] | None = None,
        on_event: Callable[[JsonObject], None] | None = None,
    ) -> HarnessTurnResult:
        if content_blocks is None:
            if prompt is None:
                raise HarnessError("Harness turn requires prompt text or content blocks")
            content_blocks = [{"type": "text", "text": prompt}]
        elif prompt is not None:
            raise HarnessError("pass either prompt or content_blocks, not both")
        if not content_blocks:
            raise HarnessError("Harness turn content blocks cannot be empty")
        response = self._request(
            "session/prompt",
            {
                "sessionId": session_id,
                "contentBlocks": content_blocks,
            },
        )
        if not isinstance(response, dict) or not isinstance(response.get("messageId"), str):
            raise HarnessError("Harness session/prompt returned an invalid response")
        message_id = response["messageId"]
        deadline = time.monotonic() + self._timeout_seconds
        received = False
        events: list[JsonObject] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError(self._diagnostic("Harness turn timed out"))
            try:
                item = self._notifications.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                self._require_running()
                continue
            if isinstance(item, BaseException):
                raise HarnessError(self._diagnostic(str(item))) from item
            if not isinstance(item, dict):
                continue
            method = item.get("method")
            params = item.get("params")
            if not isinstance(params, dict) or params.get("sessionId") != session_id:
                continue
            if method == "session.event":
                event = params.get("event")
                if not isinstance(event, dict):
                    continue
                if _is_inbox_receipt(event, message_id):
                    received = True
                if received:
                    events.append(event)
                    if on_event is not None:
                        on_event(event)
            elif method == "session.status" and received and params.get("status") == "idle":
                break
        return HarnessTurnResult(
            session_id=session_id,
            final_response=_final_response(events),
            finish_reason=_finish_reason(events),
            events=events,
            tool_calls=[
                str(event.get("data", {}).get("name"))
                for event in events
                if event.get("type") == "tool/call" and isinstance(event.get("data"), dict)
            ],
        )

    def _request(
        self,
        method: str,
        params: JsonObject | None,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        self._require_running()
        request_id = uuid.uuid4().hex
        waiter: queue.Queue[object] = queue.Queue(maxsize=1)
        with self._lock:
            self._responses[request_id] = waiter
        message: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        try:
            result = waiter.get(timeout=timeout_seconds or self._timeout_seconds)
        except queue.Empty as exc:
            with self._lock:
                self._responses.pop(request_id, None)
            raise HarnessError(self._diagnostic(f"Harness request {method} timed out")) from exc
        if isinstance(result, BaseException):
            raise HarnessError(self._diagnostic(str(result))) from result
        if isinstance(result, dict) and "__error__" in result:
            raise HarnessError(self._diagnostic(f"Harness JSON-RPC error: {result['__error__']}"))
        return result

    def _write(self, message: JsonObject) -> None:
        process = self._require_running()
        if process.stdin is None:
            raise HarnessError("Harness stdin is unavailable")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            process.stdin.write(payload)
            process.stdin.flush()

    def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if isinstance(message_id, (str, int)):
                    with self._lock:
                        waiter = self._responses.pop(str(message_id), None)
                    if waiter is not None:
                        if isinstance(message.get("error"), dict):
                            waiter.put({"__error__": message["error"]})
                        else:
                            waiter.put(message.get("result"))
                elif isinstance(message.get("method"), str):
                    self._notifications.put(message)
        except BaseException as exc:
            self._fail_waiters(exc)
        finally:
            self._fail_waiters(HarnessError("Harness runtime stdout closed"))

    def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr.append(line.rstrip())

    def _fail_waiters(self, error: BaseException) -> None:
        with self._lock:
            waiters = list(self._responses.values())
            self._responses.clear()
        for waiter in waiters:
            waiter.put(error)
        self._notifications.put(error)

    def _require_running(self) -> subprocess.Popen[str]:
        process = self._process
        if process is None:
            raise HarnessError("Harness runtime is not started")
        if process.poll() is not None:
            raise HarnessError(self._diagnostic(f"Harness runtime exited with code {process.returncode}"))
        return process

    def _diagnostic(self, message: str) -> str:
        if not self._stderr:
            return message
        return message + "\nHarness stderr:\n" + "\n".join(self._stderr)


def _is_inbox_receipt(event: JsonObject, message_id: str) -> bool:
    if event.get("type") != "agent/inbox/spliced":
        return False
    data = event.get("data")
    inserted = data.get("inserted") if isinstance(data, dict) else None
    return isinstance(inserted, list) and any(
        isinstance(item, dict) and item.get("id") == message_id for item in inserted
    )


def _final_response(events: list[JsonObject]) -> str:
    for event in reversed(events):
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        text = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text.strip():
            return text
    # Some providers emit a complete block-end and then fail during final
    # context accounting/compaction before Harness appends assistant/message.
    for event in reversed(events):
        if event.get("type") != "assistant/chunk":
            continue
        data = event.get("data")
        chunk = data.get("chunk") if isinstance(data, dict) else None
        block = chunk.get("block") if isinstance(chunk, dict) else None
        if (
            isinstance(chunk, dict)
            and chunk.get("type") == "block-end"
            and isinstance(block, dict)
            and block.get("type") == "text"
        ):
            text = str(block.get("text") or "")
            if text.strip():
                return text
    return ""


def recover_logged_final_response(session_root: Path, session_id: str) -> str:
    """Recover a final assistant message persisted before a late Harness error."""
    events = _read_session_events(session_root, session_id)
    return _final_response(events)


def recover_logged_turn_error(session_root: Path, session_id: str) -> str:
    events = _read_session_events(session_root, session_id)
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        error = reason.get("error") if isinstance(reason, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ""


def _read_session_events(session_root: Path, session_id: str) -> list[JsonObject]:
    candidates = [
        path
        for path in session_root.rglob("session.jsonl")
        if path.parent.name == session_id
    ]
    if not candidates:
        return []
    session_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    events: list[JsonObject] = []
    try:
        for line in session_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    except OSError:
        return []
    return events


def _finish_reason(events: list[JsonObject]) -> str | None:
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        kind = reason.get("kind") if isinstance(reason, dict) else None
        return str(kind) if kind is not None else None
    return None
