from __future__ import annotations

import json

import pytest

from social_ops_agent import ConversationalPlanner, PlanningError, SelectedSession


DOUYIN_SESSION = SelectedSession(
    session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
    platform="douyin",
    profile_name="抖音账号 01",
)
TELEGRAM_SESSION = SelectedSession(
    session_ref="sess_telegram_abcdefghijklmnopqrstuvwx",
    platform="telegram",
    profile_name="Telegram 账号 01",
)


def test_plans_keyword_search_and_one_hundred_downloads() -> None:
    planner = ConversationalPlanner()

    plan = planner.create_plan(
        "通过关键词“web3”在抖音上搜索并下载前100个帖子",
        DOUYIN_SESSION,
    )

    assert plan.platform == "douyin"
    assert plan.query == "web3"
    assert plan.limit == 100
    assert plan.download is True
    assert plan.download_batch_size == 20
    assert plan.tool_call_budget == 6
    assert plan.requires_confirmation is False


def test_follow_up_can_adjust_previous_plan() -> None:
    planner = ConversationalPlanner()
    initial = planner.create_plan("在抖音搜索关键词“web3”并下载前100个帖子", DOUYIN_SESSION)

    changed = planner.create_plan("改成前50个", DOUYIN_SESSION, initial)

    assert changed.query == "web3"
    assert changed.limit == 50
    assert changed.download is True
    assert changed.tool_call_budget == 4


def test_rejects_platform_that_does_not_match_selected_session() -> None:
    planner = ConversationalPlanner()
    with pytest.raises(PlanningError, match="session_ref"):
        planner.create_plan("在小红书搜索关键词“web3”", DOUYIN_SESSION)


def test_rejects_external_write_actions() -> None:
    planner = ConversationalPlanner()
    with pytest.raises(PlanningError, match="不执行点赞"):
        planner.create_plan("搜索 web3 并给前10条帖子点赞", DOUYIN_SESSION)


def test_plans_watermark_processing_as_an_explicit_extra_tool_step() -> None:
    planner = ConversationalPlanner()

    plan = planner.create_plan(
        "在抖音搜索关键词“web3”并下载前100个帖子，有水印就去水印",
        DOUYIN_SESSION,
    )

    assert plan.remove_watermark is True
    assert plan.download is True
    assert plan.tool_call_budget == 11


def test_plans_one_random_douyin_post_from_recommendation_feed() -> None:
    planner = ConversationalPlanner()

    plan = planner.create_plan("随机下载一个抖音帖子", DOUYIN_SESSION)

    assert plan.source == "timeline"
    assert plan.query is None
    assert plan.limit == 1
    assert plan.download is True
    assert plan.tool_call_budget == 2


def test_model_fallback_sends_configured_bearer_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str | None] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": '{"limit": 1}'}}]}
            ).encode()

    def fake_urlopen(request, *, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = str(timeout)
        return Response()

    monkeypatch.setattr("social_ops_agent.planner.urlopen", fake_urlopen)
    planner = ConversationalPlanner(
        ollama_base_url="https://models.example.test/v1",
        ollama_model="remote-model",
        api_key="remote-secret",
    )

    assert planner._ollama_draft("测试", DOUYIN_SESSION, None) == {"limit": 1}
    assert captured["authorization"] == "Bearer remote-secret"


def test_plans_telegram_channel_browse_and_download() -> None:
    plan = ConversationalPlanner().create_plan(
        "从 https://t.me/weme_download 下载前20条频道消息的图片、视频和文本",
        TELEGRAM_SESSION,
    )

    assert plan.platform == "telegram"
    assert plan.source == "url"
    assert plan.view == "posts"
    assert str(plan.start_url) == "https://t.me/weme_download"
    assert plan.limit == 20
    assert plan.download is True
