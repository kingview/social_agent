"""Offline test plugin; no browser, network/model calls or user files."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from social_ops_agent.diagnostic_mcp import DiagnosticFastMCP
from social_ops_agent.diagnostics import current_context, logged, record_exception

mcp = DiagnosticFastMCP("diagnostic-probe", diagnostic_component="test-plugin")


@mcp.tool()
@logged("test-plugin", "probe.business")
async def diagnostic_probe(mode: str = "ok") -> dict:
    def work():
        if mode == "fail":
            try:
                raise ValueError("Cookie: session=private-fixture-cookie")
            except ValueError as exc:
                raise RuntimeError("test failure") from exc
        if mode == "fallback":
            try:
                raise ValueError("test fallback")
            except ValueError as exc:
                record_exception("test-plugin", "probe.fallback", exc)
        return {"context": current_context(), "pid": os.getpid()}
    return await asyncio.to_thread(work)


if __name__ == "__main__":
    mcp.run(transport="stdio")
