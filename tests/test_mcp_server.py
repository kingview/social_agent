from __future__ import annotations

import asyncio

import pytest

from social_ops_agent.mcp_server import call_plugin_tool, mcp


def test_mcp_exposes_typed_tools_and_the_plugin_bridge() -> None:
    assert set(mcp._tool_manager._tools) == {
        "list_plugin_tools",
        "call_plugin_tool",
        "browser_operate",
        "browse_posts",
        "download_media",
        "analyze_content",
        "process_watermark",
        "generate_post_copy",
        "publish_x_post",
    }


def test_plugin_bridge_rejects_standard_tools_before_starting_a_plugin() -> None:
    with pytest.raises(ValueError, match="call mcp__social__browse_posts directly"):
        asyncio.run(call_plugin_tool("browse_posts", {}))
