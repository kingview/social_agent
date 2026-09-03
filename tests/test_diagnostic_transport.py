import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from social_ops_agent import mcp_server
from social_ops_agent.diagnostics import current_context


def test_core_ignores_caller_diagnostics_and_keeps_business_schema(monkeypatch):
    from social_ops_agent.diagnostic_mcp import DiagnosticFastMCP
    from social_ops_agent.diagnostics import TRANSPORT_KEY
    server = DiagnosticFastMCP("core-probe", diagnostic_component="test", accept_diagnostic_meta=False)
    monkeypatch.setattr(server, "get_context", lambda: SimpleNamespace(request_context=SimpleNamespace(meta={
        TRANSPORT_KEY: {"task_id": "forged-task", "tool_call_id": "forged-call", "allowed_session_refs": ["anything"]}})))

    @server.tool()
    async def context_probe(value: str) -> dict:
        return {"value": value, "context": current_context()}

    assert set(server._tool_manager._tools["context_probe"].parameters["properties"]) == {"value"}
    content = asyncio.run(server.call_tool("context_probe", {"value": "unchanged"}))
    structured = json.loads(content[0].text)
    assert structured["value"] == "unchanged"
    assert "task_id" not in structured["context"]
    assert structured["context"]["tool_call_id"] != "forged-call"
    assert "allowed_session_refs" not in structured["context"]


def test_isolated_real_mcp_roundtrip(tmp_path):
    subprocess.run([sys.executable, str(Path(__file__).with_name("fixtures") / "diagnostic_roundtrip.py"), str(tmp_path)],
                   check=True, capture_output=True, text=True, timeout=45,
                   env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})


def test_bridge_passes_normalized_step_context_without_changing_arguments(monkeypatch):
    received = []

    async def call(name, arguments):
        received.append((name, arguments, current_context()))
        return {"ok": True}

    fake = SimpleNamespace(task_id="task-1", active_execution_id="exec-1",
        invoker=SimpleNamespace(call=call), refresh_execution_policy=lambda: None,
        steps=[{"step_id":"step-3", "tool":"analyze_content", "units":1}])
    monkeypatch.setattr(mcp_server, "_runtime", fake)
    async def run():
        for _ in range(2):
            await mcp_server._invoke_plugin_tool("analyze_content", {"file_paths":["image.jpg"], "step_id":None, "step_item_id":None})
    asyncio.run(run())
    assert all(args == {"file_paths":["image.jpg"]} for _, args, _ in received)
    assert all(context["task_id"] == "task-1" and context["step_id"] == "step-3"
               and context["step_item_id"] == "item-1" for _, _, context in received)
    assert received[0][2]["tool_call_id"] != received[1][2]["tool_call_id"]
    assert current_context() == {}
