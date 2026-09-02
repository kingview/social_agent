from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from social_ops_agent import mcp_server
from social_ops_agent.mcp_server import call_plugin_tool, download_media, mcp
from social_ops_agent.policy import ExecutionPolicy


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
    download_schema = mcp._tool_manager._tools["download_media"].parameters
    assert download_schema["properties"]["telegram_scope"]["default"] == "messages"
    assert download_schema["properties"]["telegram_max_messages"]["default"] == 2000


def test_plugin_bridge_rejects_standard_tools_before_starting_a_plugin() -> None:
    with pytest.raises(ValueError, match="call mcp__social__browse_posts directly"):
        asyncio.run(call_plugin_tool("browse_posts", {}))


def test_download_bridge_hard_caps_first_post_even_when_model_passes_twenty_urls() -> None:
    class FakeInvoker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call(self, name: str, arguments: dict) -> dict:
            self.calls.append((name, arguments))
            return {"items": [{"url": url} for url in arguments["urls"]], "artifacts": []}

    async def run() -> None:
        invoker = FakeInvoker()
        fake_runtime = SimpleNamespace(
            invoker=invoker,
            policy=ExecutionPolicy(),
            download_lock=asyncio.Lock(),
            download_budget_remaining_bytes=5_000 * 1024 * 1024,
            max_download_posts=1,
            downloaded_post_urls=set(),
        )
        fake_runtime.refresh_execution_policy = lambda: None
        previous = mcp_server._runtime
        mcp_server._runtime = fake_runtime
        try:
            result = await download_media(
                urls=[f"https://www.xiaohongshu.com/explore/{index}" for index in range(20)],
                session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
            )
            assert invoker.calls[0][1]["urls"] == [
                "https://www.xiaohongshu.com/explore/0"
            ]
            assert result["execution_download_posts_remaining"] == 0
            with pytest.raises(ValueError, match="count is exhausted"):
                await download_media(
                    urls=["https://www.xiaohongshu.com/explore/another"],
                    session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
                )
        finally:
            mcp_server._runtime = previous

    asyncio.run(run())


def test_mcp_runtime_refreshes_limits_without_restarting_the_process(tmp_path) -> None:
    policy_path = tmp_path / "execution-policy.json"
    tool_runtime = object.__new__(mcp_server.PluginToolRuntime)
    tool_runtime.execution_policy_path = policy_path
    tool_runtime.policy = ExecutionPolicy(max_total_download_mb=120)
    tool_runtime.active_execution_id = None
    tool_runtime.max_download_posts = None
    tool_runtime.downloaded_post_urls = {"https://old.example/post"}
    tool_runtime.download_budget_remaining_bytes = 1

    policy_path.write_text(
        json.dumps({"execution_id": "turn-one", "max_download_posts": 1}),
        encoding="utf-8",
    )
    tool_runtime.refresh_execution_policy()
    assert tool_runtime.active_execution_id == "turn-one"
    assert tool_runtime.max_download_posts == 1
    assert tool_runtime.downloaded_post_urls == set()
    assert tool_runtime.download_budget_remaining_bytes == 120 * 1024 * 1024

    tool_runtime.downloaded_post_urls.add("https://current.example/post")
    tool_runtime.download_budget_remaining_bytes = 42
    tool_runtime.refresh_execution_policy()
    assert tool_runtime.downloaded_post_urls == {"https://current.example/post"}
    assert tool_runtime.download_budget_remaining_bytes == 42

    policy_path.write_text(
        json.dumps({"execution_id": "turn-two", "max_download_posts": 3}),
        encoding="utf-8",
    )
    tool_runtime.refresh_execution_policy()
    assert tool_runtime.active_execution_id == "turn-two"
    assert tool_runtime.max_download_posts == 3
    assert tool_runtime.downloaded_post_urls == set()
    assert tool_runtime.download_budget_remaining_bytes == 120 * 1024 * 1024
