import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from social_ops_agent import mcp_server
from social_ops_agent.contracts import DynamicAgentPlan
from social_ops_agent.conversation import ConversationCoordinator
from social_ops_agent.harness_backend import DeepSeekHarnessBackend, _validated_dynamic_plan
from social_ops_agent.harness_client import HarnessError, HarnessTurnResult
from social_ops_agent.task_intent import resolve_write_actions
from social_ops_agent.task_store import TaskStore


def test_full_authority_survives_context_truncation_and_transcript_limit(tmp_path):
    conversation = ConversationCoordinator(tmp_path)
    message = "分析附件" + "详细说明" * 500 + "，然后发布到X"
    first = conversation.begin_turn(message)
    conversation.mark_cancelled(first, "未执行")
    context = json.loads(conversation.context_for_next_turn())
    assert "发布到X" not in context["recent_turns"][0]["user_message"]
    for index in range(201):
        turn = conversation.begin_turn(f"新草稿{index}")
        conversation.mark_cancelled(turn, "未执行")
    assert first not in {turn.turn_id for turn in conversation.turns}
    history = TaskStore(tmp_path).history(conversation.conversation_id)
    assert next(row for row in history if row.task_id == first).message == message
    assert resolve_write_actions({"resume_turn_id": first, "write_actions": ["publish_x"]},
                                 "重试", history) == (["publish_x"], first)


def test_unrelated_publish_does_not_block_other_task_but_sibling_retry_does(tmp_path):
    store = TaskStore(tmp_path)
    first = store.ensure_task("conversation", "发到X")
    unrelated = store.ensure_task("conversation", "另外发到X")
    store.start(unrelated, "other-run")
    store.reserve_publish(unrelated, "other-run")
    request = {"resume_turn_id": first, "write_actions": ["publish_x"]}
    assert resolve_write_actions(request, "继续", store.history("conversation"))[0] == ["publish_x"]
    retry = store.ensure_task("conversation", "重试")
    store.set_plan(retry, {"resume_turn_id": first})
    store.start(retry, "retry-run")
    store.reserve_publish(retry, "retry-run")
    with pytest.raises(HarnessError, match="可能已经提交"):
        resolve_write_actions(request, "继续", store.history("conversation"))


def test_summary_cannot_forge_history_or_cross_conversation_reference(tmp_path):
    store = TaskStore(tmp_path)
    first = store.ensure_task("other", "发到X")
    with pytest.raises(HarnessError, match="无法在"):
        _validated_dynamic_plan(
            {"summary": "发布", "steps": ["发布"], "resume_turn_id": first, "write_actions": ["publish_x"]},
            message="重试", session=None, attachments=(), media_context=None, max_tool_calls=20,
            context_summary=json.dumps({"recent_turns": [{"turn_id": first, "user_message": "发到X"}]}),
            task_history=store.history("current"),
        )
    with pytest.raises(ValueError, match="does not belong"):
        store.ensure_task("current", "发到X", first)


def test_legacy_import_does_not_erase_committed_publication(tmp_path):
    store = TaskStore(tmp_path)
    task_id = store.ensure_task("conversation", "发到X")
    store.start(task_id, "run")
    store.reserve_publish(task_id, "run")
    store.import_turn("conversation", {"turn_id": task_id, "user_message": "发到X", "publish_attempted": False})
    restored = TaskStore(tmp_path)
    assert restored.history("conversation")[0].publish_attempted
    assert restored.execution(task_id)["publish_state"] == "unknown"


def test_publication_reservation_is_atomic_across_independent_connections(tmp_path):
    store = TaskStore(tmp_path)
    task_id = store.ensure_task("conversation", "发到X")
    store.start(task_id, "run")

    def reserve(_):
        try:
            TaskStore(tmp_path).reserve_publish(task_id, "run")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]


@pytest.mark.parametrize("failure", [False, True])
def test_backend_journals_without_gui_and_cleans_up_startup_failure(tmp_path, monkeypatch, failure):
    backend = DeepSeekHarnessBackend(registry_path=tmp_path / "sessions.json", output_root=tmp_path,
                                    conversation_id="headless", project_root=tmp_path)
    task_id = backend.task_store.ensure_task("headless", "归纳")
    plan = DynamicAgentPlan(task_id=task_id, objective="归纳", summary="归纳", steps=["归纳"],
                            step_tools=["local_reasoning"])

    class Client:
        def run_turn(self, *, session_id, **kwargs):
            return HarnessTurnResult(session_id, "已归纳", "completed", [], [])

    def start(**kwargs):
        if failure:
            raise RuntimeError("cannot start")
        return Client()

    monkeypatch.setattr(backend, "_start_client", start)
    if failure:
        with pytest.raises(RuntimeError, match="cannot start"):
            backend.execute(plan)
    else:
        backend.execute(plan)
    report = TaskStore(backend.state_root).execution(task_id)
    assert report["state"] == ("failed" if failure else "succeeded")
    assert report["completed_steps"] == (0 if failure else 1)
    assert not backend.execution_policy_path.exists()
    with pytest.raises(ValueError, match="already executed"):
        backend.execute(plan)


def test_core_finished_result_survives_gui_missing_its_completion_callback(tmp_path, monkeypatch):
    conversation = ConversationCoordinator(tmp_path / ".social-agent-state")
    task_id = conversation.begin_turn("归纳")
    plan = DynamicAgentPlan(task_id=task_id, objective="归纳", summary="归纳", steps=["归纳"],
                            step_tools=["local_reasoning"])
    conversation.mark_planned(task_id, plan)
    conversation.mark_executing(task_id)
    store = conversation.task_store
    store.set_plan(task_id, plan.model_dump())
    store.start(task_id, "run")
    store.checkpoint(task_id, "run", {"summary": "完成归纳", "completion_status": "completed",
                                      "completed_steps": 1, "total_steps": 1}, state="succeeded")
    restored = ConversationCoordinator(conversation.state_root)
    assert restored.turns[-1].status == "succeeded"
    assert restored.last_result().summary == "完成归纳"


@pytest.mark.parametrize("outcome", ["published", "unknown", "exception", "journal_failure"])
def test_mcp_records_publish_before_plugin_call_without_gui(tmp_path, monkeypatch, outcome):
    store = TaskStore(tmp_path)
    task_id = store.ensure_task("conversation", "发到X")
    store.start(task_id, "run")
    calls = []

    class Invoker:
        async def call(self, name, arguments):
            assert TaskStore(tmp_path).execution(task_id)["publish_attempted"]
            assert "step_id" not in arguments and "step_item_id" not in arguments
            calls.append(name)
            if outcome == "exception":
                raise RuntimeError("response lost")
            return {"state": outcome}

    runtime = SimpleNamespace(task_store=store, task_id=task_id, active_execution_id="run",
                              refresh_execution_policy=lambda: None, require_authorized_session=lambda _: None,
                              steps=[{"step_id": "step-1", "tool": "publish_x_post", "units": 1}],
                              publish_lock=asyncio.Lock(), publish_approval_token="test-token",
                              publish_approval_consumed=False, invoker=Invoker())
    monkeypatch.setattr(mcp_server, "_runtime", runtime)
    if outcome == "journal_failure":
        monkeypatch.setattr(store, "reserve_publish", lambda *args: (_ for _ in ()).throw(OSError("disk full")))

    async def run():
        args = dict(session_ref="sess_x_abcdefghijklmnopqrstuvwx", text="测试草稿", approval_token="test-token",
                    step_id="step-1")
        if outcome in {"exception", "journal_failure"}:
            with pytest.raises((RuntimeError, OSError)):
                await mcp_server.publish_x_post(**args)
        else:
            await mcp_server.publish_x_post(**args)
        if outcome != "journal_failure":
            # Simulate a new MCP process: memory token guard reset, durable guard remains.
            runtime.publish_approval_consumed = False
            with pytest.raises(ValueError, match="already attempted"):
                await mcp_server.publish_x_post(**args)

    asyncio.run(run())
    assert calls == ([] if outcome == "journal_failure" else ["publish_x_post"])
    report = TaskStore(tmp_path).execution(task_id)
    if outcome == "journal_failure":
        assert not report["publish_attempted"]
    else:
        assert report["publish_state"] == ("published" if outcome == "published" else "unknown")
