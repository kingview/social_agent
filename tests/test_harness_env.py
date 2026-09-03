import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("authorized", [True, False])
def test_production_harness_config_forwards_only_explicit_task_token(tmp_path, authorized):
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node", path="/opt/homebrew/opt/node@24/bin") or shutil.which("node")
    if not node or not (root / "harness/node_modules/@deepseek-ai/dsh-mcp-client").is_dir():
        pytest.skip("Install Harness dependencies and Node 22.19+/24+ for transport integration tests")
    env = {
        **os.environ,
        "SOCIAL_AGENT_PYTHON": sys.executable,
        "SOCIAL_AGENT_MCP_ARGS": json.dumps([str(root / "tests/fixtures/env_probe_mcp.py")]),
        "SOCIAL_AGENT_PROJECT_ROOT": str(root),
        "SOCIAL_AGENT_SESSION_REGISTRY": str(tmp_path / "sessions.json"),
        "SOCIAL_AGENT_OUTPUT_ROOT": str(tmp_path / "output"),
        "SOCIAL_AGENT_STATE_ROOT": str(tmp_path / "state"),
        "SOCIAL_AGENT_EXECUTION_POLICY_PATH": str(tmp_path / "policy.json"),
        "SOCIAL_AGENT_PLUGIN_ROOT": str(tmp_path / "plugins"),
        "SOCIAL_AGENT_LLM_API_KEY": "test-only",
        "SOCIAL_AGENT_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "SOCIAL_AGENT_LLM_MODEL": "test-only",
        "SOCIAL_AGENT_PYTHONPATH": str(root / "src"),
        "SOCIAL_AGENT_UNRELATED_TOKEN": "must-not-reach-child",
    }
    env.pop("SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN", None)
    if authorized:
        env["SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN"] = "test-grant-sentinel"
    result = subprocess.run([node, str(root / "tests/fixtures/harness_env_probe.mjs"),
                             str(root / "harness/cordis-execute.yml")],
                            env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout.strip())
    assert probe == {"token_present": authorized, "token_matches": authorized,
                     "policy_present": True, "unrelated_token_present": False}


@pytest.mark.parametrize("token", [None, "new-task-grant"])
def test_backend_overrides_stale_host_grant_instead_of_inheriting_it(tmp_path, monkeypatch, token):
    from social_ops_agent import harness_backend

    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs["env"])

        def start(self, **kwargs):
            pass

    monkeypatch.setenv("SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN", "stale-host-grant")
    monkeypatch.setattr(harness_backend, "HarnessJsonRpcClient", Client)
    monkeypatch.setattr(harness_backend, "_node_executable", lambda: tmp_path / "node")
    monkeypatch.setattr(harness_backend, "_runtime_script", lambda _: tmp_path / "runtime.mjs")
    backend = harness_backend.DeepSeekHarnessBackend(registry_path=tmp_path / "sessions.json",
        output_root=tmp_path, conversation_id="test", project_root=tmp_path)
    monkeypatch.setattr(backend, "is_available", lambda _: (True, "test"))
    backend._start_client(mode="execute", publish_approval_token=token)
    assert captured["SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN"] == (token or "")
