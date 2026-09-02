from __future__ import annotations

from pathlib import Path

from social_ops_agent.contracts import AgentExecutionResult, DynamicAgentPlan
from social_ops_agent.conversation import ConversationCoordinator


def _plan(objective: str = "下载第一条") -> DynamicAgentPlan:
    return DynamicAgentPlan(
        objective=objective,
        platform="xiaohongshu",
        session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
        summary="搜索并下载第一条",
        steps=["搜索", "下载第一条"],
        max_download_posts=1,
    )


def test_failed_turn_and_conversation_identity_survive_restart(tmp_path: Path) -> None:
    coordinator = ConversationCoordinator(tmp_path)
    conversation_id = coordinator.conversation_id
    turn_id = coordinator.begin_turn(
        "在小红书搜索 Microduck 并下载第一条",
        session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
        platform="xiaohongshu",
    )
    coordinator.mark_planned(turn_id, _plan())
    coordinator.mark_executing(turn_id)
    coordinator.mark_failed(
        turn_id,
        stage="execution",
        error="页面要求重新登录",
    )

    restored = ConversationCoordinator(tmp_path)

    assert restored.conversation_id == conversation_id
    assert restored.turns[-1].status == "failed"
    assert restored.last_session_ref() == "sess_xhs_abcdefghijklmnopqrstuvwx"
    context = restored.context_for_next_turn()
    assert context is not None
    assert "Microduck" in context
    assert "页面要求重新登录" in context


def test_inflight_turn_is_recovered_as_retryable_interruption(tmp_path: Path) -> None:
    coordinator = ConversationCoordinator(tmp_path)
    coordinator.begin_turn("下载第一条")

    restored = ConversationCoordinator(tmp_path)

    assert restored.turns[-1].status == "failed"
    assert restored.turns[-1].error_stage == "interrupted"
    assert "重试" in (restored.turns[-1].error or "")


def test_last_result_and_new_conversation_lifecycle(tmp_path: Path) -> None:
    coordinator = ConversationCoordinator(tmp_path)
    old_id = coordinator.conversation_id
    turn_id = coordinator.begin_turn("下载第一条")
    plan = _plan()
    coordinator.mark_planned(turn_id, plan)
    coordinator.mark_succeeded(
        turn_id,
        AgentExecutionResult(
            runtime="deepseek_harness",
            plan=plan,
            summary="已下载第一条帖子",
            output_directories=["/tmp/output"],
        ),
    )

    restored = ConversationCoordinator(tmp_path)
    assert restored.last_result() is not None
    assert restored.last_result().summary == "已下载第一条帖子"  # type: ignore[union-attr]

    new_id = restored.new_conversation()
    assert new_id != old_id
    assert restored.turns == ()
    assert ConversationCoordinator(tmp_path).conversation_id == new_id
