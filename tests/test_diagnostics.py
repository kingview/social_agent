import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field, ValidationError

from social_ops_agent import diagnostics as d


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_AGENT_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path / "logs"


def records(directory):
    return [json.loads(line) for p in sorted(directory.glob("*.jsonl*")) for line in p.read_text().splitlines()]


def test_chained_validation_failure_keeps_fields_and_stack_but_not_model_input(logs):
    class Payload(BaseModel):
        evidence_refs: list[str] = Field(max_length=50)

    try:
        try:
            Payload(evidence_refs=["PRIVATE-MODEL-INPUT"] * 173)
        except ValidationError as exc:
            raise RuntimeError("unexpected media analysis failure") from exc
    except RuntimeError as exc:
        d.record_exception("test", "analysis", exc, task_id="task-3", arguments="DO-NOT-LOG")
    record = records(logs)[0]
    assert record["context"] == {"task_id": "task-3"}
    assert record["exception"]["cause"]["validation_errors"] == [{"field": ["evidence_refs"], "type": "too_long"}]
    assert record["exception"]["cause"]["stack"][-1]["line"] > 0
    content = json.dumps(record)
    assert "PRIVATE-MODEL-INPUT" not in content and "DO-NOT-LOG" not in content
    assert "source" not in record["exception"]["stack"][-1]
    assert (next(logs.glob("*.jsonl")).stat().st_mode & 0o777) == 0o600


def test_exception_redaction_masks_credentials_and_signed_urls(logs, monkeypatch):
    monkeypatch.setenv("SOCIAL_AGENT_LLM_API_KEY", "random-secret-value")
    d.register_secrets("registered-key-value")
    message = ('random-secret-value registered-key-value\n'
        'Cookie: a=private-cookie; b=second-cookie\nAuthorization: Bearer header-value\n'
        '{"approval_token": "private-grant"}\n'
        '{"authorization": "Basic private-basic-auth"}\n'
        'socks5://username:socks-password@127.0.0.1:1080\n'
        'https://user:proxy-password@example.test/media?xsec_token=signed-value&other=private\n')
    d.record_exception("test", "redaction", ValueError(message))
    content = json.dumps(records(logs))
    for value in ["random-secret-value", "registered-key-value", "private-cookie", "second-cookie",
                  "header-value", "private-grant", "proxy-password", "signed-value", "socks-password", "private-basic-auth"]:
        assert value not in content
    assert "example.test/media" in content and "REDACTED" in content


def test_async_tool_and_thread_fallback_keep_context_and_reraise(logs):
    failure = RuntimeError("model failed")

    @d.logged("test", "analyze_content")
    async def call(request, context):
        def fallback():
            try:
                raise ValueError("ocr failed")
            except ValueError as exc:
                d.record_exception("test", "ocr.fallback", exc)
        await asyncio.to_thread(fallback)
        raise failure

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(call("SENSITIVE REQUEST", SimpleNamespace(trace_id="trace-1", agent_run_id="run-1")))
    assert caught.value is failure
    found = records(logs)
    assert {r["stage"] for r in found} == {"ocr.fallback", "analyze_content"}
    assert all(r["context"]["trace_id"] == "trace-1" for r in found)
    assert "SENSITIVE REQUEST" not in json.dumps(found)


def test_rotation_is_bounded_and_lines_remain_json(logs, monkeypatch):
    monkeypatch.setattr(d, "MAX_LOG_BYTES", 1200)
    for _ in range(25):
        d.record_exception("rotation", "test", RuntimeError("x" * 600))
    files = list(logs.glob("rotation-*.jsonl*"))
    assert len(files) <= d.BACKUP_COUNT + 1
    assert records(logs)


def test_log_storage_failure_cannot_replace_original_exception(logs, monkeypatch, capsys):
    logs.parent.joinpath("blocked").write_text("not a directory")
    monkeypatch.setenv("SOCIAL_AGENT_LOG_DIR", str(logs.parent / "blocked" / "child"))
    failure = ValueError("actual operation error")

    @d.logged("test", "broken-disk")
    def call():
        raise failure

    with pytest.raises(ValueError) as caught:
        call()
    assert caught.value is failure
    assert "unable to persist" in capsys.readouterr().err


def test_rotation_write_failure_does_not_dump_record_to_stderr(logs, monkeypatch, capsys):
    def unavailable(self):
        raise OSError("disk unavailable")
    monkeypatch.setattr(d._PrivateRotatingHandler, "_open", unavailable)
    d.record_exception("write-error", "test", ValueError("private business detail"))
    captured = capsys.readouterr()
    assert not captured.out
    assert "unable to persist" in captured.err
    assert "private business detail" not in captured.err


def test_cleanup_keeps_active_and_unrelated_files(logs, monkeypatch):
    logs.mkdir()
    monkeypatch.setattr(d, "RETAIN_PROCESS_LOGS", 2)
    for name in ("worker-11.jsonl", "worker-11.jsonl.1", "worker-12.jsonl", "worker-13.jsonl", "user.jsonl"):
        (logs / name).write_text("test")
    os.utime(logs / "worker-11.jsonl", (1, 1))
    os.utime(logs / "worker-12.jsonl", (2, 2))
    os.utime(logs / "worker-13.jsonl", (3, 3))
    def alive(pid, signal):
        if pid == 11:
            raise ProcessLookupError()
    monkeypatch.setattr(d.os, "kill", alive)
    d._prune_inactive_logs(logs, "worker")
    assert sorted(p.name for p in logs.iterdir()) == ["user.jsonl", "worker-12.jsonl", "worker-13.jsonl"]


def test_exception_hooks_cover_main_thread_and_async(logs, monkeypatch):
    import threading
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    d.install_exception_hooks("test")
    sys.excepthook(RuntimeError, RuntimeError("main"), None)
    threading.excepthook(SimpleNamespace(exc_value=RuntimeError("thread"), thread=SimpleNamespace(name="worker")))
    d.log_async_exception(None, {"exception": RuntimeError("async")})
    assert {r["stage"] for r in records(logs)} == {"unhandled.main", "unhandled.thread", "unhandled.async"}


def test_plugin_diagnostics_copies_match_canonical_source():
    root = Path(__file__).resolve().parents[2]
    for package in ("media_content_analyzer", "social_content_crawler"):
        target = root / "tools" / package / "src" / package
        if not target.is_dir():
            pytest.skip("Sibling Tool sources are only required for workspace parity checks")
        for name in ("diagnostics.py", "diagnostic_mcp.py"):
            assert (target / name).read_bytes() == Path(d.__file__).with_name(name).read_bytes()


def test_chained_errors_share_id_and_write_full_stack_only_once(logs):
    try:
        try:
            raise ValueError("root failure")
        except ValueError as cause:
            first = d.record_exception("tool", "source", cause, task_id="task-1")
            raise RuntimeError("wrapped failure") from cause
    except RuntimeError as wrapper:
        second = d.record_exception("gui", "summary", wrapper, task_id="task-1")
        d.record_exception("gui", "summary", wrapper, task_id="task-1")
    found = records(logs)
    assert first == second
    assert len(found) == 2
    assert [r["event"] for r in found].count("exception") == 1
    propagated = next(r for r in found if r["event"] == "exception_propagated")
    assert propagated["exception"] == {"type": "RuntimeError"}


def test_remote_error_links_are_validated_and_never_parsed_from_messages(logs):
    error = RuntimeError("err_" + "a" * 32)
    d.link_remote_error(error, {"error_id": "untrusted content"})
    first = d.record_exception("host", "invalid-meta", error)
    assert first != "err_" + "a" * 32
    linked = RuntimeError("remote failure")
    d.link_remote_error(linked, {"error_id": first})
    assert d.record_exception("host", "valid-meta", linked) == first
    assert records(logs)[-1]["event"] == "exception_propagated"


def test_transport_metadata_accepts_only_bounded_identifiers():
    assert d.transport_context({"task_id": "turn-1", "step_id": "step-2", "tool_call_id": "call-3",
        "api_key": "secret", "allowed_session_refs": ["sess"], "log_dir": "/tmp/other",
        "conversation_id": "x" * 200, "trace_id": {"nested": "data"}}) == {
        "task_id": "turn-1", "step_id": "step-2", "tool_call_id": "call-3"}


def test_failed_write_can_be_retried_without_losing_original_stack(logs, monkeypatch):
    original = d._PrivateRotatingHandler._open
    monkeypatch.setattr(d._PrivateRotatingHandler, "_open", lambda self: (_ for _ in ()).throw(OSError("full")))
    failure = ValueError("original")
    assert d.record_exception("retry-write", "source", failure) is None
    monkeypatch.setattr(d._PrivateRotatingHandler, "_open", original)
    assert d.record_exception("retry-write", "source", failure)
    assert records(logs)[0]["event"] == "exception"
