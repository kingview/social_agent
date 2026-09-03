"""Probe child environment through tools/list only. No external operations."""
import json
import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("env-probe")
description = json.dumps({
    "token_present": bool(os.getenv("SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN")),
    "token_matches": os.getenv("SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN") == "test-grant-sentinel",
    "policy_present": bool(os.getenv("SOCIAL_AGENT_EXECUTION_POLICY_PATH")),
    "unrelated_token_present": "SOCIAL_AGENT_UNRELATED_TOKEN" in os.environ,
})


@server.tool(description=description)
def probe_environment() -> dict:
    raise RuntimeError("This fixture must only be inspected, never called")


server.run(transport="stdio")
