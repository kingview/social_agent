from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
from threading import Event

import pytest

from social_ops_agent.browser_scheduler import BrowserTaskScheduler, BrowserWaitCancelled, WindowResource, resources_for_plan
from social_ops_agent.contracts import DynamicAgentPlan
from social_ops_agent import harness_backend as harness


def hold(scheduler, keys, owner="one", cancelled=None, on_wait=None):
    return scheduler.reserve([WindowResource(key, key) for key in keys], conversation_id=owner,
                             execution_id=owner + "-execution", cancelled=cancelled or Event(), on_wait=on_wait)


def test_same_window_waits_other_window_runs_and_release_resumes(tmp_path):
    scheduler = BrowserTaskScheduler(tmp_path)
    waiting, entered = Event(), Event()
    def contender():
        with hold(BrowserTaskScheduler(tmp_path), ["one"], "second", on_wait=lambda *_: waiting.set()):
            entered.set()
    with ThreadPoolExecutor() as pool:
        with hold(scheduler, ["one"]):
            future = pool.submit(contender)
            assert waiting.wait(2) and not entered.is_set()
            with hold(scheduler, ["two"]):
                assert not entered.is_set()
        future.result(2)
    assert entered.is_set()


def test_multi_window_wait_never_holds_subset_and_can_cancel(tmp_path):
    scheduler = BrowserTaskScheduler(tmp_path)
    waiting, cancelled = Event(), Event()
    def contender():
        with hold(scheduler, ["a", "b"], cancelled=cancelled, on_wait=lambda *_: waiting.set()):
            raise AssertionError("cancelled waiter must not execute")
    with ThreadPoolExecutor() as pool:
        with hold(scheduler, ["b"]):
            future = pool.submit(contender)
            assert waiting.wait(2)
            # FIFO holds our place for A, but does not hold A's OS resource lock.
            from social_ops_agent.browser_queue import open_lock
            from social_ops_agent.process_locks import _try_lock, _unlock
            with open_lock(tmp_path / "a.lock") as probe:
                assert _try_lock(probe)
                _unlock(probe)
            with hold(scheduler, ["unrelated"]):
                cancelled.set()
            with pytest.raises(BrowserWaitCancelled):
                future.result(2)
        with hold(scheduler, ["a", "b"]):
            pass


def test_lock_releases_after_exception(tmp_path):
    scheduler = BrowserTaskScheduler(tmp_path)
    with pytest.raises(RuntimeError):
        with hold(scheduler, ["a", "b"]):
            raise RuntimeError("fixture failed")
    with hold(scheduler, ["b", "a"]):
        pass


def test_fifo_for_overlapping_sets_without_blocking_independent_windows(tmp_path):
    scheduler = BrowserTaskScheduler(tmp_path)
    queued = [Event(), Event(), Event()]
    order = []
    def run(index, keys):
        with hold(BrowserTaskScheduler(tmp_path), keys, str(index), on_wait=lambda *_: queued[index].set()):
            order.append(index)
    with ThreadPoolExecutor() as pool:
        with hold(scheduler, ["b"]):
            first = pool.submit(run, 0, ["a", "b"])
            assert queued[0].wait(2)
            second = pool.submit(run, 1, ["a"])
            assert queued[1].wait(2)
            third = pool.submit(run, 2, ["b"])
            assert queued[2].wait(2)
            with hold(scheduler, ["independent"]):
                assert order == []
        first.result(3)
        second.result(3)
        third.result(3)
    assert order[0] == 0  # Both later requests overlap the first request.
    assert sorted(order) == [0, 1, 2]
    assert json.loads((tmp_path / "queue.json").read_text())["tickets"] == []
    assert not list(tmp_path.glob("ticket-*.lock"))


def test_cancelled_waiter_leaves_queue_and_next_waiter_resumes(tmp_path):
    scheduler = BrowserTaskScheduler(tmp_path)
    cancelled, waiting = Event(), Event()
    entered = []
    def first():
        with hold(scheduler, ["a"], "cancel-me", cancelled=cancelled, on_wait=lambda *_: waiting.set()):
            entered.append("cancel-me")
    def second():
        with hold(scheduler, ["a"], "next"):
            entered.append("next")
    with ThreadPoolExecutor() as pool:
        with hold(scheduler, ["a"]):
            one = pool.submit(first)
            assert waiting.wait(2)
            two = pool.submit(second)
            cancelled.set()
            with pytest.raises(BrowserWaitCancelled):
                one.result(2)
        two.result(2)
    assert entered == ["next"]


def test_crashed_waiter_does_not_block_later_tickets(tmp_path):
    script = """
import sys
from pathlib import Path
from threading import Event
from social_ops_agent.browser_scheduler import BrowserTaskScheduler, WindowResource
with BrowserTaskScheduler(Path(sys.argv[1])).reserve([WindowResource('a', 'A')],
    conversation_id='crash-waiter', execution_id='crash-execution', cancelled=Event(),
    on_wait=lambda *args: print('waiting', flush=True)):
    print('unexpected', flush=True)
"""
    scheduler = BrowserTaskScheduler(tmp_path)
    with hold(scheduler, ["a"]):
        child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "waiting"
        finally:
            child.terminate()
            child.wait(timeout=5)
    with hold(scheduler, ["a"], "survivor"):
        rows = json.loads((tmp_path / "queue.json").read_text())["tickets"]
        assert [row["owner"] for row in rows] == ["survivor"]


def test_cross_process_lock_and_crash_recovery(tmp_path):
    script = """
import sys
from pathlib import Path
from threading import Event
from social_ops_agent.browser_scheduler import BrowserTaskScheduler, WindowResource
with BrowserTaskScheduler(Path(sys.argv[1])).reserve([WindowResource('profile', 'window')],
    conversation_id='child', execution_id='child-execution', cancelled=Event()):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        cancelled = Event()
        with pytest.raises(BrowserWaitCancelled):
            with hold(BrowserTaskScheduler(tmp_path), ["profile"], cancelled=cancelled,
                      on_wait=lambda *_: cancelled.set()):
                raise AssertionError("two processes entered same window")
    finally:
        child.terminate()
        child.wait(timeout=5)
    with hold(BrowserTaskScheduler(tmp_path), ["profile"]):
        pass


def registry(tmp_path):
    path = tmp_path / "sessions.json"
    refs = ["sess_xhs_abcdefghijklmnopqrstuvwx", "sess_x_abcdefghijklmnopqrstuvwx"]
    path.write_text(json.dumps({"sessions": [dict(session_ref=ref, platform=platform,
        provider="bitbrowser", profile_id="shared-profile", profile_name="shared window", api_url=api,
        created_at="now", updated_at="now") for ref, platform, api in zip(refs,
        ["xiaohongshu", "x"], ["http://127.0.0.1:54345", "http://localhost:54345/"])]}))
    return path, refs


def test_aliases_and_platform_refs_resolve_to_one_profile(tmp_path):
    path, refs = registry(tmp_path)
    plan = DynamicAgentPlan(objective="分析内容", summary="分析", steps=["分析"],
        platform="xiaohongshu", session_ref=refs[0], browser_sessions=[
        {"platform": platform, "session_ref": ref} for platform, ref in zip(["xiaohongshu", "x"], refs)])
    assert len(resources_for_plan(plan, path)) == 1
    path.write_text('{"sessions": []}')
    with pytest.raises(ValueError, match="已被移除"):
        resources_for_plan(plan, path)


def test_backend_holds_lease_through_cleanup_and_waits_without_model_calls(tmp_path, monkeypatch):
    path, refs = registry(tmp_path)
    backends = [harness.DeepSeekHarnessBackend(registry_path=path, output_root=tmp_path / "output",
                 conversation_id=f"conversation-{number}") for number in range(2)]
    for backend in backends:
        backend.browser_scheduler = BrowserTaskScheduler(tmp_path / "leases")
    plan = DynamicAgentPlan(objective="分析内容", summary="分析", steps=["分析"],
                            platform="xiaohongshu", session_ref=refs[0])
    cleanup_started, finish_cleanup, waiting, second_executed = Event(), Event(), Event(), Event()
    def execute_first(plan, *, execution_id, **_):
        (backends[0].state_root / "browser-resources" / execution_id).mkdir(parents=True)
        return harness.HarnessExecutionResult(plan=plan, response="first", session_id="first", tool_calls=[])
    def execute_second(plan, **_):
        second_executed.set()
        return harness.HarnessExecutionResult(plan=plan, response="second", session_id="second", tool_calls=[])
    def cleanup(*_):
        cleanup_started.set()
        assert finish_cleanup.wait(3)
        return {"closed_tabs": 1, "closed_windows": 1, "warnings": []}
    monkeypatch.setattr(backends[0], "_execute", execute_first)
    monkeypatch.setattr(backends[1], "_execute", execute_second)
    monkeypatch.setattr(harness, "cleanup_task_browsers", cleanup)
    with ThreadPoolExecutor() as pool:
        one = pool.submit(backends[0].execute, plan)
        assert cleanup_started.wait(2)
        two = pool.submit(backends[1].execute, plan, progress=lambda event:
                          waiting.set() if event.stage == "waiting_browser" else None)
        try:
            assert waiting.wait(2) and not second_executed.is_set()
        finally:
            finish_cleanup.set()
        one.result(3)
        two.result(3)
    assert second_executed.is_set()


def test_backend_cancel_wait_records_cancelled_not_failed(tmp_path):
    path, refs = registry(tmp_path)
    backend = harness.DeepSeekHarnessBackend(registry_path=path, output_root=tmp_path / "output", conversation_id="test")
    backend.browser_scheduler = BrowserTaskScheduler(tmp_path / "leases")
    plan = DynamicAgentPlan(objective="分析", summary="分析", steps=["分析"], platform="xiaohongshu", session_ref=refs[0])
    resources = resources_for_plan(plan, path)
    with backend.browser_scheduler.reserve(resources, conversation_id="other", execution_id="other", cancelled=Event()):
        result = backend.execute(plan, progress=lambda event: backend.cancel() if event.stage == "waiting_browser" else None)
    assert result.cancelled and not result.tool_calls
    assert backend.task_store.execution(result.plan.task_id)["state"] == "cancelled"


def test_cancelled_runtime_is_joined_before_window_handoff(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from contextlib import contextmanager
    backend = harness.DeepSeekHarnessBackend(registry_path=tmp_path / "sessions.json", output_root=tmp_path,
                                             conversation_id="cancel-runtime")
    order = []
    @contextmanager
    def reserve(*args, **kwargs):
        order.append("acquired")
        try:
            yield
        finally:
            order.append("released")
    monkeypatch.setattr(backend.browser_scheduler, "reserve", reserve)
    client = Mock()
    client.close.side_effect = lambda: order.append("runtime joined")
    def execute(*args, **kwargs):
        backend._active_client = client
        backend.cancel()
        raise RuntimeError("cancelled fixture")
    monkeypatch.setattr(backend, "_execute", execute)
    plan = DynamicAgentPlan(objective="分析本地内容", summary="分析", steps=["分析"])
    with pytest.raises(RuntimeError):
        backend.execute(plan)
    assert order == ["acquired", "runtime joined", "released"]
