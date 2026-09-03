"""Isolated MCP roundtrip driver, independent of pytest's Qt event loop."""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from mcp import StdioServerParameters
from social_ops_agent.diagnostics import diagnostic_context, record_exception
from social_ops_agent.plugin_host import PluginEndpoint, PluginHost, PluginHostError

root = Path(sys.argv[1])
os.environ["SOCIAL_AGENT_LOG_DIR"] = str(root / "logs")
endpoint = PluginEndpoint(plugin_id="test.diagnostics", version="1.0.0",
    parameters=StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).with_name("diagnostic_probe_mcp.py"))],
        env={"SOCIAL_AGENT_LOG_DIR": str(root / "logs")}),
    expected_tools=("diagnostic_probe",))
host = PluginHost()


async def invoke(number, mode):
    with diagnostic_context(replace=True, task_id=f"task-{number}", step_id=f"step-{number}",
        execution_id=f"execution-{number}", tool_call_id=f"call-{number}", trace_id=f"trace-{number}"):
        try:
            return await host.call(endpoint, "diagnostic_probe", {"mode": mode})
        except PluginHostError as cause:
            try:
                raise RuntimeError("upper-layer failure") from cause
            except RuntimeError as exc:
                return {"error_id": record_exception("test-core", "invoke", exc)}


async def verify():
    first = await invoke(1, "ok")
    failed, fallback, failed_again = await asyncio.gather(invoke(2, "fail"), invoke(3, "fallback"), invoke(4, "fail"))
    last = await invoke(5, "ok")
    assert first["pid"] == last["pid"] == fallback["pid"]
    for number, result in ((1, first), (3, fallback), (5, last)):
        assert result["context"]["task_id"] == f"task-{number}"
        assert result["context"]["step_id"] == f"step-{number}"
        assert result["context"]["tool_call_id"] == f"call-{number}"
    assert failed["error_id"] != failed_again["error_id"]
    validation = await invoke(6, {"private": "PRIVATE-VALIDATION-INPUT"})
    assert validation["error_id"]
    rows = [json.loads(line) for path in (root / "logs").glob("*.jsonl") for line in path.read_text().splitlines()]
    raw = json.dumps(rows)
    assert "private-fixture-cookie" not in raw and "PRIVATE-VALIDATION-INPUT" not in raw
    for number, failure in ((2, failed), (4, failed_again), (6, validation)):
        group = [row for row in rows if row["error_id"] == failure["error_id"]]
        assert sum(row["event"] == "exception" for row in group) == 1
        assert all(row["context"]["task_id"] == f"task-{number}" for row in group)
        assert all(row["context"]["tool_call_id"] == f"call-{number}" for row in group)
        assert {row["component"] for row in group} == {"test-plugin", "agent-plugin-host", "test-core"}
    assert any(row["stage"] == "probe.fallback" and row["context"]["task_id"] == "task-3" for row in rows)


try:
    asyncio.run(verify())
finally:
    host.close()
print("Diagnostic roundtrip: task isolation, warm process reuse, error IDs, fallback and validation privacy passed")
