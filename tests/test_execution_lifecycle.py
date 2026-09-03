from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from social_ops_agent.contracts import DynamicAgentPlan
from social_ops_agent.execution_lifecycle import ExecutionLifecycle
from social_ops_agent.browser_scheduler import BrowserWaitCancelled


def scope(order, **overrides):
    @contextmanager
    def reserve(*args, **kwargs):
        order.append("acquired")
        try:
            yield
        finally:
            order.append("released")
    policy = SimpleNamespace(grant=Mock(side_effect=lambda *a, **k: order.append("granted")),
        revoke=Mock(side_effect=lambda *a: order.append("revoked")))
    options = dict(scheduler=SimpleNamespace(reserve=reserve), policy_channel=policy,
        plan=DynamicAgentPlan(objective="分析", summary="分析", steps=["分析"]),
        execution_id="execution-test", conversation_id="conversation-test", cancelled=Event(),
        finish_runtime=lambda failed: order.append("joined"), report_error=lambda exc: order.append("logged"))
    options.update(overrides)
    return ExecutionLifecycle(**options), policy


def test_runtime_teardown_happens_once_before_cleanup_and_window_handoff():
    order = []
    lifecycle, policy = scope(order)
    with lifecycle:
        lifecycle.acquire(lambda: [], on_wait=None)
        order.append("executed")
        lifecycle.stop_runtime()
        order.append("cleaned")
    assert order == ["acquired", "granted", "executed", "revoked", "joined", "cleaned", "released"]
    policy.revoke.assert_called_once()


def test_original_error_preserved_when_teardown_also_fails():
    order = []
    def broken_join(failed):
        assert failed
        order.append("join attempted")
        raise OSError("teardown failure")
    lifecycle, _ = scope(order, finish_runtime=broken_join)
    with pytest.raises(RuntimeError, match="original failure"):
        with lifecycle:
            lifecycle.acquire(lambda: [], on_wait=None)
            raise RuntimeError("original failure")
    assert order[-4:] == ["revoked", "join attempted", "logged", "released"]


def test_failed_revoke_still_joins_runtime_and_releases():
    order = []
    lifecycle, policy = scope(order)
    policy.revoke.side_effect = OSError("cannot revoke")
    with pytest.raises(OSError, match="cannot revoke"):
        with lifecycle:
            lifecycle.acquire(lambda: [], on_wait=None)
    assert order == ["acquired", "granted", "logged", "joined", "released"]


def test_registry_change_does_not_grant_or_execute():
    order = []
    lifecycle, policy = scope(order)
    resources = iter([["old"], ["new"]])
    with pytest.raises(ValueError, match="注册信息已变化"):
        with lifecycle:
            lifecycle.acquire(lambda: next(resources), on_wait=None)
    policy.grant.assert_not_called()
    policy.revoke.assert_not_called()
    assert order == ["acquired", "joined", "released"]


def test_cancelled_acquisition_never_grants_and_resets_flag():
    order, cancelled = [], Event()
    cancelled.set()
    lifecycle, policy = scope(order, cancelled=cancelled)
    with pytest.raises(BrowserWaitCancelled):
        with lifecycle:
            lifecycle.acquire(lambda: [], on_wait=None)
    assert not cancelled.is_set()
    policy.grant.assert_not_called()
    assert order == ["acquired", "joined", "released"]


def test_failed_grant_is_revoked_even_when_enter_incomplete():
    order = []
    lifecycle, policy = scope(order)
    policy.grant.side_effect = OSError("partial write")
    with pytest.raises(OSError, match="partial write"):
        with lifecycle:
            lifecycle.acquire(lambda: [], on_wait=None)
    policy.revoke.assert_called_once()
    assert order == ["acquired", "revoked", "joined", "released"]
