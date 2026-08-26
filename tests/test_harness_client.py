from __future__ import annotations

import sys
from pathlib import Path

from social_ops_agent.harness_client import HarnessJsonRpcClient


def test_jsonrpc_client_collects_response_and_tool_calls(tmp_path: Path) -> None:
    runtime = Path(__file__).parent / "fixtures" / "fake_harness_runtime.py"
    client = HarnessJsonRpcClient(
        launch_args=[sys.executable, str(runtime)],
        cwd=tmp_path,
        env={},
        timeout_seconds=5,
    )
    client.start(provider="fake", model="fake", max_tokens=256)
    try:
        result = client.run_turn(session_id="session-1", prompt="hello")
    finally:
        client.close()

    assert result.final_response == "fake runtime complete"
    assert result.finish_reason == "completed"
    assert result.tool_calls == ["mcp__social__browse_posts"]
