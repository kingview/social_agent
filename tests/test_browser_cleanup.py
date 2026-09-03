import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from social_ops_agent import browser_cleanup as cleanup
from social_ops_agent import harness_backend as harness


def test_no_browser_task_does_not_launch_plugin(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(cleanup.subprocess, "run", run)
    assert cleanup.cleanup_task_browsers(tmp_path, "execution_12345678")["closed_windows"] == 0
    run.assert_not_called()


def test_cleanup_launches_installed_hook_with_exact_task_scope(tmp_path, monkeypatch):
    directory = tmp_path / "browser-resources" / "execution_12345678"
    directory.mkdir(parents=True)
    (directory / "test.json").write_text("{}")
    runtime = SimpleNamespace(task_cleanup_module="social_content_crawler.browser_lifecycle")
    owner = SimpleNamespace(python=tmp_path / "python", manifest=SimpleNamespace(
        id="com.socialagent.social-content", permissions=["browser-session"], runtime=runtime))
    manager = Mock()
    manager.list.return_value = [owner]
    run = Mock(return_value=SimpleNamespace(stdout=json.dumps({"closed_tabs": 2, "closed_windows": 1, "warnings": []})))
    monkeypatch.setattr(cleanup.subprocess, "run", run)
    result = cleanup.cleanup_task_browsers(tmp_path, "execution_12345678", manager=manager)
    assert result["closed_windows"] == 1
    assert run.call_args.args[0] == [str(owner.python), "-m", runtime.task_cleanup_module,
                                    "--state-root", str(tmp_path), "--execution-id", "execution_12345678"]
    assert run.call_args.kwargs["timeout"] == 45
    run.side_effect = TimeoutError("test")
    monkeypatch.setattr(cleanup, "record_exception", Mock())
    result = cleanup.cleanup_task_browsers(tmp_path, "execution_12345678", manager=manager)
    assert result["warnings"]


@pytest.mark.parametrize("status,cancelled,should_clean", [
    ("completed", False, True), ("partial", False, False), ("failed", False, False),
    ("completed", True, False), ("exception", False, False),
])
def test_cleanup_only_after_verified_success(tmp_path, monkeypatch, status, cancelled, should_clean):
    backend = harness.DeepSeekHarnessBackend(registry_path=tmp_path / "sessions.json",
                                             output_root=tmp_path, conversation_id="cleanup-test")
    plan = harness.DynamicAgentPlan(objective="分析本地附件", summary="分析", steps=["分析"])
    def execute(plan, *, execution_id, **kwargs):
        directory = backend.state_root / "browser-resources" / execution_id
        directory.mkdir(parents=True)
        if status == "exception":
            raise RuntimeError("failed test task")
        return harness.HarnessExecutionResult(plan=plan, response="结果", session_id="test", tool_calls=[],
            completion_status=status, cancelled=cancelled, completed_steps=1, total_steps=1)
    monkeypatch.setattr(backend, "_execute", execute)
    cleaner = Mock(return_value={"closed_tabs": 1, "closed_windows": 0, "warnings": []})
    monkeypatch.setattr(harness, "cleanup_task_browsers", cleaner)
    events = []
    if status == "exception":
        with pytest.raises(RuntimeError):
            backend.execute(plan, progress=events.append)
    else:
        result = backend.execute(plan, progress=events.append)
        if should_clean:
            assert "已清理" in result.user_summary()
            assert events[-1].completed == events[-1].total == 1
    assert cleaner.called is should_clean
