import json

import pytest

from social_ops_agent.conversation import ConversationCoordinator
from social_ops_agent.contracts import DynamicAgentPlan
from social_ops_agent.harness_backend import DeepSeekHarnessBackend, _validated_dynamic_plan
from social_ops_agent.harness_client import HarnessError, HarnessTurnResult
from social_ops_agent.planner import SelectedSession
from social_ops_agent.policy import DEFAULT_EXECUTION_POLICY
from social_ops_agent.task_intent import resolve_write_actions


@pytest.fixture
def history(tmp_path):
    coordinator = ConversationCoordinator(tmp_path / ".social-agent-state")
    original = coordinator.begin_turn("下载小红书第2篇帖子，分析生成文案，然后发布到X")
    coordinator.mark_cancelled(original, "之前的确认框误判取消")
    return coordinator, original


def test_harness_can_resume_original_publish_intent(history):
    coordinator, original = history
    context = coordinator.context_for_next_turn()
    assert json.loads(context)["recent_turns"][0]["turn_id"] == original
    plan = _validated_dynamic_plan(
        {"summary": "继续上次发布任务", "steps": ["生成文案", "发布到 X"],
         "step_tools": ["generate_post_copy", "publish_x_post"],
         "resume_turn_id": original, "write_actions": ["publish_x"]},
        message="执行上次的任务", context_summary=context,
        task_history=coordinator.task_store.history(coordinator.conversation_id),
        session=SelectedSession(platform="x", session_ref="sess_x_abcdefghijklmnopqrstuvwx"),
        attachments=(), media_context=None, max_tool_calls=20, require_step_tools=True,
    )
    assert plan.write_actions == ["publish_x"]
    assert plan.resume_turn_id == original
    assert plan.requires_confirmation is False


def test_retry_chain_preserves_origin(history):
    coordinator, original = history
    retry = coordinator.begin_turn("继续")
    coordinator.mark_planned(retry, DynamicAgentPlan(
        objective="继续", summary="继续", steps=["执行"], resume_turn_id=original,
    ))
    coordinator.mark_failed(retry, stage="execution", error="网络错误，未提交")
    actions, source = resolve_write_actions(
        {"resume_turn_id": retry, "write_actions": ["publish_x"]},
        "再试一次", coordinator.task_store.history(coordinator.conversation_id),
    )
    assert actions == ["publish_x"] and source == retry


@pytest.mark.parametrize("message", ["继续，但是不要发布", "重试，只生成草稿"])
def test_current_read_only_scope_overrides_history(history, message):
    coordinator, original = history
    DEFAULT_EXECUTION_POLICY.validate_message(message)
    actions, _ = resolve_write_actions(
        {"resume_turn_id": original, "write_actions": []}, message,
        coordinator.task_store.history(coordinator.conversation_id),
    )
    assert actions == []


def test_negative_publish_instruction_cannot_be_overridden_by_model(history):
    coordinator, original = history
    with pytest.raises(HarnessError, match="不发布"):
        resolve_write_actions({"resume_turn_id": original, "write_actions": ["publish_x"]},
                              "继续，但不要发布到X", coordinator.task_store.history(coordinator.conversation_id))


def test_new_task_does_not_inherit_old_write_permission(history):
    coordinator, _ = history
    assert resolve_write_actions({}, "分析这个视频", coordinator.task_store.history(coordinator.conversation_id)) == ([], None)
    with pytest.raises(HarnessError, match="缺少"):
        resolve_write_actions({"write_actions": ["publish_x"]}, "生成文案", coordinator.task_store.history(coordinator.conversation_id))
    with pytest.raises(HarnessError, match="找不到|无法在"):
        resolve_write_actions({"resume_turn_id": "invented", "write_actions": ["publish_x"]},
                              "重试", coordinator.task_store.history(coordinator.conversation_id))


def test_model_summary_is_not_authorization(history):
    coordinator, _ = history
    turn = coordinator.begin_turn("生成草稿")
    coordinator.mark_planned(turn, DynamicAgentPlan(objective="生成草稿", summary="发布到X", steps=["发布到X"]))
    coordinator.mark_failed(turn, stage="execution", error="没有发布权限")
    with pytest.raises(HarnessError, match="不包含可继承"):
        resolve_write_actions({"resume_turn_id": turn, "write_actions": ["publish_x"]},
                              "重试", coordinator.task_store.history(coordinator.conversation_id))


def test_publish_attempt_survives_crash_and_blocks_generic_retry(history):
    coordinator, original = history
    coordinator.mark_executing(original)
    coordinator.mark_publish_attempted(original)
    restored = ConversationCoordinator(coordinator.state_root)
    with pytest.raises(HarnessError, match="可能已经提交"):
        resolve_write_actions({"resume_turn_id": original, "write_actions": ["publish_x"]},
                              "执行上次任务", restored.task_store.history(restored.conversation_id))


def test_planner_repair_receives_same_history(history, tmp_path):
    coordinator, original = history
    backend = DeepSeekHarnessBackend(registry_path=tmp_path / "sessions.json", output_root=tmp_path,
                                    conversation_id=coordinator.conversation_id, project_root=tmp_path)
    context = coordinator.context_for_next_turn()
    prompts = []

    class Client:
        def run_turn(self, *, session_id, content_blocks=None, prompt=None):
            prompts.append(json.loads(prompt if prompt else content_blocks[0]["text"]))
            payload = {"summary": "继续", "steps": ["发布到X"], "write_actions": ["publish_x"],
                       "resume_turn_id": original, "step_tools": ["publish_x_post"]}
            return HarnessTurnResult(session_id, "bad json" if len(prompts) == 1 else json.dumps(payload),
                                     "completed", [], [])

    backend._clients["plan"] = Client()
    plan = backend.propose("执行上次任务", SelectedSession(platform="x", session_ref="sess_x_abcdefghijklmnopqrstuvwx"),
                           context_summary=context)
    assert len(prompts) == 2
    assert all(prompt["recent_conversation_context"] == context for prompt in prompts)
    assert plan.write_actions == ["publish_x"]


def test_missing_step_tool_mapping_requires_repair():
    with pytest.raises(HarnessError, match="一一对应"):
        _validated_dynamic_plan({"summary": "下载", "steps": ["下载"]}, message="下载",
                                session=None, attachments=(), media_context=None,
                                max_tool_calls=20, require_step_tools=True)
