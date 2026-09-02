from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
        fake_runtime.require_authorized_session = lambda session_ref: None
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
        json.dumps(
            {
                "execution_id": "turn-one",
                "max_download_posts": 1,
                "allowed_session_refs": ["sess_xhs_abcdefghijklmnopqrstuvwx"],
            }
        ),
        encoding="utf-8",
    )
    tool_runtime.refresh_execution_policy()
    assert tool_runtime.active_execution_id == "turn-one"
    assert tool_runtime.max_download_posts == 1
    assert tool_runtime.allowed_session_refs == {"sess_xhs_abcdefghijklmnopqrstuvwx"}
    tool_runtime.require_authorized_session("sess_xhs_abcdefghijklmnopqrstuvwx")
    with pytest.raises(ValueError, match="not authorized"):
        tool_runtime.require_authorized_session("sess_xhs_not_authorized_1234567890")
    assert tool_runtime.downloaded_post_urls == set()
    assert tool_runtime.download_budget_remaining_bytes == 120 * 1024 * 1024

    tool_runtime.downloaded_post_urls.add("https://current.example/post")
    tool_runtime.download_budget_remaining_bytes = 42
    tool_runtime.refresh_execution_policy()
    assert tool_runtime.downloaded_post_urls == {"https://current.example/post"}
    assert tool_runtime.download_budget_remaining_bytes == 42

    policy_path.write_text(
        json.dumps(
            {
                "execution_id": "turn-two",
                "max_download_posts": 3,
                "allowed_session_refs": [],
            }
        ),
        encoding="utf-8",
    )
    tool_runtime.refresh_execution_policy()
    assert tool_runtime.active_execution_id == "turn-two"
    assert tool_runtime.max_download_posts == 3
    assert tool_runtime.downloaded_post_urls == set()
    assert tool_runtime.download_budget_remaining_bytes == 120 * 1024 * 1024


def test_analysis_bridge_persists_full_result_and_bounds_harness_context(tmp_path) -> None:
    output = {
        "language": "zh",
        "summary": "内容摘要",
        "claims": ["结论" * 1_000 for _ in range(100)],
        "entities": [{"name": f"实体-{index}"} for index in range(100)],
        "tags": [
            {
                "namespace": "topic",
                "label": "机器人",
                "confidence": 0.9,
                "evidence_refs": [f"evidence-{index}" for index in range(1_000)],
            }
        ],
        "evidence": [
            {
                "evidence_id": f"evidence-{index}",
                "kind": "ocr",
                "text": "画面文字" * 1_000,
                "confidence": 0.8,
            }
            for index in range(100)
        ],
        "assets": [{"modality": "video", "transcript": ["很长的逐字稿"] * 1_000}],
    }

    compact = mcp_server._compact_analysis_output(output, tmp_path)

    full_path = tmp_path / "analysis-results" / Path(compact["full_result_path"]).name
    assert json.loads(full_path.read_text(encoding="utf-8")) == output
    assert "evidence_refs" not in compact["tags"][0]
    assert "transcript" not in compact["assets"][0]
    assert len(json.dumps(compact, ensure_ascii=False)) < 25_000
    assert compact["context_compacted"] is True


def test_step_metadata_is_checked_but_not_forwarded_to_existing_plugins(monkeypatch):
    calls = []

    class Invoker:
        async def call(self, name, arguments):
            calls.append((name, arguments))
            return {"variants": ["copy"]}

    runtime = SimpleNamespace(invoker=Invoker(), refresh_execution_policy=lambda: None,
                              steps=[{"step_id": f"step-{i}", "tool": "generate_post_copy", "units": 1}
                                     for i in [1, 2]])
    monkeypatch.setattr(mcp_server, "_runtime", runtime)

    async def run():
        with pytest.raises(ValueError, match="require step_id"):
            await mcp_server.generate_post_copy(analysis={"summary": "test"})
        with pytest.raises(ValueError, match="does not match"):
            await mcp_server.generate_post_copy(analysis={}, step_id="invented")
        await mcp_server.generate_post_copy(analysis={"summary": "test"}, step_id="step-2", step_item_id="item-1")

    asyncio.run(run())
    assert len(calls) == 1
    assert "step_id" not in calls[0][1] and "step_item_id" not in calls[0][1]
    for name in ("browse_posts", "browser_operate", "download_media", "analyze_content",
                 "process_watermark", "generate_post_copy", "publish_x_post", "call_plugin_tool"):
        schema = mcp._tool_manager._tools[name].parameters
        assert "step_id" in schema["properties"] and "step_item_id" in schema["properties"]
