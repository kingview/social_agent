from __future__ import annotations

import json

import pytest

from social_ops_agent.harness_backend import (
    DeepSeekHarnessBackend,
    DynamicAgentPlan,
    _json_object,
    _validated_dynamic_plan,
    requires_dynamic_harness,
)
from social_ops_agent.harness_client import HarnessError, HarnessTurnResult
from social_ops_agent.planner import PlanningPolicyError, validate_planning_policy
from social_ops_agent.settings import LLMProvider, LLMSettings


def test_dynamic_plan_contract_binds_platform_and_opaque_session() -> None:
    plan = DynamicAgentPlan(
        objective="分析抖音 web3 内容",
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        summary="搜索、下载、分析",
        steps=["搜索帖子", "下载媒体", "分析并总结"],
    )

    assert plan.requires_confirmation is False
    assert plan.mode == "dynamic_harness"
    assert plan.max_tool_calls == 20


def test_dynamic_plan_allows_local_media_without_browser_session() -> None:
    plan = DynamicAgentPlan(
        objective="分析附件",
        summary="分析本地附件",
        steps=["理解内容", "生成摘要"],
    )

    assert plan.platform is None
    assert plan.session_ref is None


def test_every_natural_language_command_routes_to_harness() -> None:
    assert requires_dynamic_harness("搜索 Web3 并分析内容、生成文案")
    assert requires_dynamic_harness("打开抖音，点击搜索框输入 Web3 后翻页")
    assert requires_dynamic_harness("搜索 Web3 并下载前20个帖子")
    assert requires_dynamic_harness("生成一条文案并发布到X")
    assert requires_dynamic_harness("下载 Telegram 频道的所有图片和视频")


def test_planning_policy_runs_before_any_llm_planner() -> None:
    with pytest.raises(PlanningPolicyError, match="不支持其他平台写操作"):
        validate_planning_policy("分析热门帖子然后自动发布")


def test_json_object_accepts_fenced_model_output() -> None:
    assert _json_object("```json\n{\"summary\":\"ok\",\"steps\":[\"one\"]}\n```")["summary"] == "ok"


def test_json_object_wraps_malformed_model_output_in_harness_error() -> None:
    with pytest.raises(HarnessError, match="invalid JSON"):
        _json_object('{"summary":"ok","steps":[broken]}')


def test_model_authored_extra_fields_are_not_part_of_plan_contract() -> None:
    plan = _validated_dynamic_plan(
        {
            "summary": "下载并分析",
            "steps": ["下载", "分析"],
            "extra_reminder": "模型自由生成的提醒不进入计划契约",
        },
        message="分析本地内容",
        session=None,
        attachments=(),
        media_context=None,
        max_tool_calls=20,
    )

    assert "extra_reminder" not in plan.model_dump()


def test_harness_plan_preserves_first_post_as_a_hard_download_limit() -> None:
    plan = _validated_dynamic_plan(
        {
            "summary": "搜索并下载第一条",
            "steps": ["搜索", "下载第一条"],
            "max_download_posts": 20,
        },
        message="在小红书上搜索美女，并下载第一条",
        session=type(
            "Session",
            (),
            {
                "platform": "xiaohongshu",
                "session_ref": "sess_xhs_abcdefghijklmnopqrstuvwx",
            },
        )(),
        attachments=(),
        media_context=None,
        max_tool_calls=20,
    )

    assert plan.max_download_posts == 1


def test_x_publish_plan_still_requires_one_time_confirmation() -> None:
    plan = _validated_dynamic_plan(
        {"summary": "生成并发布", "steps": ["生成文案", "发布到 X"]},
        message="生成文案并发布到X",
        session=type(
            "Session",
            (),
            {
                "platform": "x",
                "session_ref": "sess_x_abcdefghijklmnopqrstuvwx",
            },
        )(),
        attachments=(),
        media_context=None,
        max_tool_calls=20,
    )

    assert plan.write_actions == ["publish_x"]
    assert plan.requires_confirmation is True


def test_full_telegram_channel_plan_uses_normal_budget_after_tool_refactor() -> None:
    plan = _validated_dynamic_plan(
        {"summary": "全量下载频道", "steps": ["分批浏览", "下载媒体和文本"]},
        message="下载这个 Telegram 频道的所有图片、视频和文本",
        session=type(
            "Session",
            (),
            {
                "platform": "telegram",
                "session_ref": "sess_telegram_abcdefghijklmnopqrstuvwx",
            },
        )(),
        attachments=(),
        media_context=None,
        max_tool_calls=200,
    )

    assert plan.max_tool_calls == 20


def test_execution_harness_stays_alive_across_retry_turns_and_policy_rotates(
    tmp_path,
) -> None:
    registry_path = tmp_path / "sessions.json"
    registry_path.write_text('{"sessions":[]}', encoding="utf-8")
    backend = DeepSeekHarnessBackend(
        registry_path=registry_path,
        output_root=tmp_path / "output",
        conversation_id="conversation-with-retry",
        project_root=tmp_path,
        settings=LLMSettings.create(
            provider=LLMProvider.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3.5:9b",
        ),
    )

    class FakeClient:
        def __init__(self) -> None:
            self.turns: list[tuple[str, dict]] = []

        def run_turn(self, *, session_id, content_blocks, on_event):
            policy = json.loads(
                backend.execution_policy_path.read_text(encoding="utf-8")
            )
            self.turns.append((session_id, policy))
            return HarnessTurnResult(
                session_id=session_id,
                final_response="执行完成",
                finish_reason="stop",
                events=[],
                tool_calls=[],
            )

    client = FakeClient()
    backend._clients["execute"] = client  # type: ignore[assignment]
    plan = DynamicAgentPlan(
        objective="下载第一条",
        platform="xiaohongshu",
        session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
        summary="搜索并下载第一条",
        steps=["搜索", "下载第一条"],
        max_download_posts=1,
    )

    backend.execute(plan)
    backend.execute(plan.model_copy(update={"objective": "重试"}))

    assert len(client.turns) == 2
    assert backend._clients["execute"] is client
    assert client.turns[0][0] == client.turns[1][0]
    assert client.turns[0][0].startswith("conversation-with-retry-execute-")
    assert client.turns[0][1]["execution_id"] != client.turns[1][1]["execution_id"]
    assert [turn[1]["max_download_posts"] for turn in client.turns] == [1, 1]
    assert not backend.execution_policy_path.exists()


def test_empty_execution_response_is_reported_as_failure_and_resets_session(
    tmp_path,
) -> None:
    registry_path = tmp_path / "sessions.json"
    registry_path.write_text('{"sessions":[]}', encoding="utf-8")
    backend = DeepSeekHarnessBackend(
        registry_path=registry_path,
        output_root=tmp_path / "output",
        conversation_id="conversation-empty-response",
        project_root=tmp_path,
        settings=LLMSettings.create(
            provider=LLMProvider.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3.5:9b",
        ),
    )

    class EmptyClient:
        def __init__(self) -> None:
            self.closed = False

        def run_turn(self, *, session_id, content_blocks, on_event):
            return HarnessTurnResult(
                session_id=session_id,
                final_response="",
                finish_reason="stop",
                events=[],
                tool_calls=[],
            )

        def close(self) -> None:
            self.closed = True

    client = EmptyClient()
    backend._clients["execute"] = client  # type: ignore[assignment]
    plan = DynamicAgentPlan(
        objective="重试",
        summary="重试上一任务",
        steps=["重新执行"],
    )

    with pytest.raises(HarnessError, match="without an assistant response"):
        backend.execute(plan)

    assert client.closed is True
    assert "execute" not in backend._clients
    assert backend._execute_generation == 1
    assert not backend.execution_policy_path.exists()
