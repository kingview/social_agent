import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from social_ops_agent import desktop, plugin_cli, conversation_pane
from social_ops_agent.settings import LLMSettingsError
from social_ops_agent.plugins import PluginError


def stage_names(root):
    return {json.loads(line)["stage"] for path in root.glob("*.jsonl") for line in path.read_text().splitlines()}


def test_keychain_and_session_manager_launch_failures_are_persisted(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("SOCIAL_AGENT_LOG_DIR", str(logs))
    monkeypatch.setattr(desktop.QMessageBox, "warning", lambda *args: None)
    monkeypatch.setattr(desktop.QMessageBox, "information", lambda *args: None)

    def broken_secret(settings):
        raise LLMSettingsError("keychain unavailable")

    window = SimpleNamespace(_llm_settings_store=SimpleNamespace(with_secret=broken_secret),
        _llm_settings=SimpleNamespace(base_url="http://localhost/v1", model="test", api_key=""),
        _output_root=tmp_path, controller=SimpleNamespace(conversation_id="conversation-1", busy=False),
        _global_busy=False, _plugin_manager=None,
        _registry_path=tmp_path / "registry.json", _session_manager_process=None,
        manage_sessions_button=SimpleNamespace(setEnabled=lambda v: None, setText=lambda v: None))
    assert desktop.MainWindow._ensure_llm_secret(window) is False

    def broken_launch(*args, **kwargs):
        raise PluginError("plugin unavailable")

    monkeypatch.setattr(conversation_pane, "PluginInvoker", broken_launch)
    window._reset_session_manager_launch = lambda: window._session_manager_ready_dir.cleanup()
    desktop.MainWindow.manage_sessions(window)
    assert stage_names(logs) == {"gui.settings_secret", "gui.session_manager_launch"}


def test_session_manager_process_failure_logs_details_not_stack_in_dialog(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("SOCIAL_AGENT_LOG_DIR", str(logs))
    messages = []
    monkeypatch.setattr(desktop.QMessageBox, "critical", lambda *args: messages.append(args[-1]))
    window = SimpleNamespace(_output_root=tmp_path, controller=SimpleNamespace(conversation_id="conversation-1"),
        _session_manager_process=SimpleNamespace(poll=lambda: 1, returncode=1,
            communicate=lambda **kwargs: (b"", b"Traceback: test startup detail")),
        _reset_session_manager_launch=lambda: None, _refresh_sessions=lambda: None)
    desktop.MainWindow._poll_session_manager(window)
    assert stage_names(logs) == {"gui.session_manager_exit"}
    assert "test startup detail" in next(logs.glob("*.jsonl")).read_text()
    assert "test startup detail" not in messages[0] and "err_" in messages[0]


def test_plugin_cli_error_is_persisted(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("SOCIAL_AGENT_LOG_DIR", str(logs))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    assert plugin_cli.main(["--root", str(tmp_path / "plugins"), "install", str(tmp_path / "missing.socialtool")]) == 1
    assert stage_names(logs) == {"plugin_cli.install"}
