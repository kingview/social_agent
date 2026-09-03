import json

import pytest

from social_ops_agent.agent_runtime import _harness_result
from social_ops_agent.contracts import DynamicAgentPlan
from social_ops_agent.conversation import ConversationCoordinator
from social_ops_agent.execution_tracking import ExecutionTracker
from social_ops_agent.harness_backend import DeepSeekHarnessBackend
from social_ops_agent.harness_client import HarnessTurnResult


def plan():
    return DynamicAgentPlan(
        objective="搜索下载分析生成文案并发布到X", summary="完整流程",
        steps=["搜索", "下载", "分析", "生成文案", "发布到X"],
        step_tools=["browse_posts", "download_media", "analyze_content", "generate_post_copy", "publish_x_post"],
        platform="x", session_ref="sess_x_abcdefghijklmnopqrstuvwx", write_actions=["publish_x"],
    )


def fixture_registry(tmp_path):
    (tmp_path / "sessions.json").write_text(json.dumps({"sessions": [{
        "session_ref": "sess_x_abcdefghijklmnopqrstuvwx", "platform": "x", "provider": "bitbrowser",
        "profile_id": "fixture-tracking-profile", "profile_name": "fixture X",
        "api_url": "http://127.0.0.1:54345", "created_at": "now", "updated_at": "now",
    }]}))


def result_event(call_id, payload, error=False):
    return {"type": "tool/result", "data": {"message": {"content": [{
        "type": "tool-result", "toolCallId": call_id, "isError": error,
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }]}}}


def events(publish_state):
    cases = [
        ("browse_posts", {"posts": [{"url": "test"}]}, False),
        ("download_media", {"items": [{"artifacts": ["video"]}]}, False),
        ("analyze_content", {"summary": "分析"}, False),
        ("generate_post_copy", {"error": "platform x not supported"}, True),
        ("generate_post_copy", {"variants": [{"body": "文案"}]}, False),
    ]
    if publish_state is not None:
        cases.append(("publish_x_post", {"state": publish_state}, False))
    for index, (tool, payload, error) in enumerate(cases):
        yield {"type": "tool/call", "data": {"name": f"mcp__social__{tool}", "callId": str(index)}}
        yield result_event(str(index), payload, error)


@pytest.mark.parametrize("publish_state,count", [(None, 4), ("failed", 4), ("unknown", 4), ("published", 5)])
def test_publish_and_retries_are_counted_by_successful_steps(publish_state, count):
    tracker = ExecutionTracker(plan())
    progress = []
    for event in events(publish_state):
        progress.append(tracker.called(event["data"]) if event["type"] == "tool/call" else tracker.returned(event["data"]))
    final = tracker.finish(normal_end=True)
    assert final.completed == count
    assert (final.stage == "done") == (publish_state == "published")
    assert progress[4].completed == 2  # Starting analysis: 2/5, not 3/5.
    assert progress[7].completed == 3  # Failed copy generation must not advance.
    assert progress[9].completed == 4  # Successful retry completes only the copy step.


def test_extra_copy_does_not_complete_publish_and_duplicate_results_are_ignored():
    tracker = ExecutionTracker(plan())
    for event in events(None):
        tracker.called(event["data"]) if event["type"] == "tool/call" else tracker.returned(event["data"])
    tracker.called({"name": "mcp__social__generate_post_copy", "callId": "extra"})
    tracker.returned(result_event("extra", {"variants": [{"body": "extra"}]})["data"])
    tracker.returned(result_event("extra", {"variants": [{"body": "extra"}]})["data"])
    assert tracker.finish(normal_end=True).completed == 4
    assert tracker.publish_state == "not_attempted"


@pytest.mark.parametrize("tool,payload", [
    ("browse_posts", {"posts": []}), ("download_media", {"items": [], "artifacts": []}),
    ("download_media", {"items": [{}], "completed": False}),
    ("publish_x_post", {"summary": "已发布"}),
])
def test_no_credit_for_empty_partial_or_unconfirmed_results(tool, payload):
    tracker = ExecutionTracker(plan())
    tracker.called({"name": f"mcp__social__{tool}", "callId": "one"})
    tracker.returned(result_event("one", payload)["data"])
    assert not tracker.completed


@pytest.mark.parametrize("state", [None, "failed", "unknown", "published"])
def test_runtime_and_conversation_preserve_actual_completion(tmp_path, monkeypatch, state):
    fixture_registry(tmp_path)
    backend = DeepSeekHarnessBackend(registry_path=tmp_path / "sessions.json", output_root=tmp_path,
                                    conversation_id="test", project_root=tmp_path)
    grants = []

    class Client:
        def run_turn(self, *, session_id, content_blocks, on_event):
            payload = json.loads(content_blocks[0]["text"])
            grants.append(bool(payload["x_publish_approval_token"]))
            for event in events(state):
                if event["type"] == "tool/call" and event["data"]["name"].endswith("publish_x_post"):
                    # A real core MCP reserves before calling the plugin, and
                    # writes the result before emitting the tool-result event.
                    from social_ops_agent.execution_policy_channel import read_execution_policy
                    grant = read_execution_policy(backend.execution_policy_path)
                    backend.task_store.reserve_publish(grant.task_id, grant.execution_id)
                    backend.task_store.publish_result(grant.task_id, grant.execution_id, state)
                on_event(event)
            return HarnessTurnResult(session_id, "模型声称全部完成", "completed", [], [])

    monkeypatch.setattr(backend, "_start_client", lambda **kwargs: Client())
    result = _harness_result(backend.execute(plan()))
    assert grants == [True]
    assert result.completed_steps == (5 if state == "published" else 4)
    assert result.completion_status == ("completed" if state == "published" else "partial")
    assert result.publish_state == (state or "not_attempted")
    if state != "published":
        assert result.summary.startswith("任务未全部完成")
    coordinator = ConversationCoordinator(tmp_path / "state")
    turn_id = coordinator.begin_turn("执行上次任务")
    coordinator.mark_planned(turn_id, plan())
    coordinator.mark_succeeded(turn_id, result)
    restored = ConversationCoordinator(coordinator.state_root)
    assert restored.turns[-1].status == ("succeeded" if state == "published" else "partial")
    assert not backend.execution_policy_path.exists()


def test_publish_call_without_result_remains_unknown():
    tracker = ExecutionTracker(plan())
    tracker.called({"name": "mcp__social__publish_x_post", "callId": "publish"})
    assert tracker.publish_state == "unknown"
    assert tracker.finish(normal_end=False).stage == "incomplete"


def test_repeated_tools_require_explicit_steps_and_results_can_arrive_out_of_order():
    tracker = ExecutionTracker(DynamicAgentPlan(objective="两份文案", summary="两份文案",
        steps=["第一份", "第二份"], step_tools=["generate_post_copy", "generate_post_copy"]))
    tracker.called({"name": "mcp__social__generate_post_copy", "callId": "legacy"})
    tracker.returned(result_event("legacy", {"variants": ["不确定归属"]})["data"])
    assert not tracker.completed
    for call_id, step in [("a", "step-1"), ("b", "step-2"), ("retry-a", "step-1")]:
        tracker.called({"name": "mcp__social__generate_post_copy", "callId": call_id,
                        "arguments": {"step_id": step}})
    tracker.returned(result_event("b", {"variants": ["第二份"]})["data"])
    assert tracker.completed == {1}
    tracker.returned(result_event("a", {"error": "failed"}, True)["data"])
    assert tracker.completed == {1}
    tracker.returned(result_event("retry-a", {"variants": ["第一份"]})["data"])
    assert tracker.completed == {0, 1}
    assert tracker.report()["calls"]["b"]["step_id"] == "step-2"


def test_batched_step_needs_all_distinct_units_and_retries_do_not_inflate_progress():
    tracker = ExecutionTracker(DynamicAgentPlan(objective="分两批下载", summary="分批下载",
        steps=["下载"], step_tools=["download_media"], step_units=[2]))
    for call_id, unit in [("first", "item-1"), ("duplicate", "item-1"), ("second", "item-2")]:
        tracker.called({"name": "mcp__social__download_media", "callId": call_id,
                        "arguments": {"step_id": "step-1", "step_item_id": unit}})
        tracker.returned(result_event(call_id, {"artifacts": ["file"]})["data"])
        assert tracker.progress("").completed == (1 if call_id == "second" else 0)


def test_invalid_step_binding_and_partial_batch_do_not_complete_a_step():
    tracker = ExecutionTracker(plan())
    tracker.called({"name": "mcp__social__download_media", "callId": "wrong",
                    "arguments": {"step_id": "step-5"}})
    tracker.returned(result_event("wrong", {"artifacts": ["file"]})["data"])
    tracker.called({"name": "mcp__social__download_media", "callId": "partial",
                    "arguments": {"step_id": "step-2"}})
    tracker.returned(result_event("partial", {"items": [{"artifacts": ["file"]}, {"error": "failed"}],
                                               "artifacts": ["file"]})["data"])
    assert not tracker.completed


@pytest.mark.parametrize("serialized", [True, False])
def test_native_arguments_replay_completes_four_steps_before_rejected_publish(serialized):
    tracker = ExecutionTracker(plan())
    stages = [("browse_posts", {"posts": ["post"]}), ("download_media", {"artifacts": ["file"]}),
              ("analyze_content", {"summary": "分析"}), ("generate_post_copy", {"variants": ["草稿"]})]
    for index, (tool, result) in enumerate(stages, 1):
        args = {"step_id": f"step-{index}", "step_item_id": "item-1"}
        tracker.called({"callId": f"call-{index}", "name": f"mcp__social__{tool}",
                        "arguments": json.dumps(args) if serialized else args})
        tracker.returned(result_event(f"call-{index}", result)["data"])
    tracker.called({"callId": "publish", "name": "mcp__social__publish_x_post",
                    "arguments": json.dumps({"step_id": "step-5", "step_item_id": "item-1"})})
    tracker.returned(result_event("publish", {"error": "approval missing"}, True)["data"])
    tracker.reconcile_publication("not_attempted")
    result = tracker.finish(normal_end=True)
    assert result.completed == 4 and result.total == 5
    assert tracker.publish_state == "not_attempted"
    assert all(row["step_id"] for row in tracker.call_records.values())


@pytest.mark.parametrize("raw", ['{"step_id":', '[]', 'null', '"text"', 7, [],
    '{"step_id":"step-2","step_item_id":[]}', {"step_id": {"id": "step-2"}}])
def test_malformed_native_arguments_cannot_receive_step_credit(raw):
    tracker = ExecutionTracker(plan())
    tracker.called({"callId": "bad", "name": "mcp__social__download_media", "arguments": raw})
    tracker.returned(result_event("bad", {"artifacts": ["file"]})["data"])
    assert not tracker.completed


def test_serialized_repeated_tool_arguments_remain_bound_to_their_own_steps():
    tracker = ExecutionTracker(DynamicAgentPlan(objective="两批文案", summary="两批文案",
        steps=["甲", "乙"], step_tools=["generate_post_copy", "generate_post_copy"]))
    for call, step in [("a", "step-1"), ("b", "step-2")]:
        tracker.called({"callId": call, "name": "mcp__social__generate_post_copy",
                        "arguments": json.dumps({"step_id": step})})
    tracker.returned(result_event("b", {"variants": ["乙"]})["data"])
    assert tracker.completed == {1}
    tracker.returned(result_event("a", {"variants": ["甲"]})["data"])
    assert tracker.completed == {0, 1}


def test_backend_and_gui_record_preflight_failure_as_not_attempted(tmp_path, monkeypatch):
    fixture_registry(tmp_path)
    coordinator = ConversationCoordinator(tmp_path / ".social-agent-state")
    execution_plan = plan()
    task_id = coordinator.begin_turn(execution_plan.objective)
    execution_plan = execution_plan.model_copy(update={"task_id": task_id})
    coordinator.mark_planned(task_id, execution_plan)
    backend = DeepSeekHarnessBackend(registry_path=tmp_path / "sessions.json", output_root=tmp_path,
        conversation_id=coordinator.conversation_id, project_root=tmp_path)

    class Client:
        def run_turn(self, *, session_id, on_event, **kwargs):
            for event in events(None):
                on_event(event)
            on_event({"type": "tool/call", "data": {"callId": "publish",
                "name": "mcp__social__publish_x_post", "arguments": '{"step_id":"step-5"}'}})
            # The core rejected the request before its reservation boundary.
            on_event(result_event("publish", {"error": "approval invalid"}, True))
            return HarnessTurnResult(session_id, "授权校验失败，未提交", "completed", [], [])

    monkeypatch.setattr(backend, "_start_client", lambda **kwargs: Client())
    result = _harness_result(backend.execute(execution_plan))
    assert result.completed_steps == 4 and result.completion_status == "partial"
    assert result.publish_state == "not_attempted"
    coordinator.mark_succeeded(task_id, result)
    restored = ConversationCoordinator(coordinator.state_root)
    assert not restored.turns[-1].publish_attempted
    assert restored.last_result().publish_state == "not_attempted"
    assert not restored.task_store.history(restored.conversation_id)[-1].publish_attempted
