from __future__ import annotations

from social_ops_agent.mcp_server import mcp


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
    }
