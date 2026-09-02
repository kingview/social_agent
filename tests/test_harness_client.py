from __future__ import annotations

import sys
from pathlib import Path

from social_ops_agent.harness_client import (
    HarnessJsonRpcClient,
    recover_logged_final_response,
)


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


def test_recovers_text_persisted_before_late_harness_error(tmp_path: Path) -> None:
    session_id = "session-with-late-error"
    session_dir = tmp_path / "cwd" / session_id
    session_dir.mkdir(parents=True)
    session_dir.joinpath("session.jsonl").write_text(
        "\n".join(
            [
                '{"type":"assistant/message","data":{"message":{"content":[{"type":"tool-call","name":"example"}]}}}',
                '{"type":"assistant/chunk","data":{"chunk":{"type":"block-end","block":{"type":"text","text":"任务已完成，文件已保存。"}}}}',
                '{"type":"turn/end","data":{"reason":{"kind":"error"}}}',
            ]
        ),
        encoding="utf-8",
    )

    assert recover_logged_final_response(tmp_path, session_id) == "任务已完成，文件已保存。"
