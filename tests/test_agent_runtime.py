from __future__ import annotations

from pathlib import Path

import pytest

from social_ops_agent.agent_runtime import RuntimeRouter, _fixed_result
from social_ops_agent.contracts import AgentPlan, AgentRunResult
from social_ops_agent.harness_backend import DynamicAgentPlan
from social_ops_agent.planner import PlanningError, PlanningPolicyError, SelectedSession
from social_ops_agent.policy import ExecutionPolicy, ExecutionPolicyError
from social_ops_agent.settings import LLMSettings


SESSION = SelectedSession(
    session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
    platform="douyin",
    profile_name="test",
)


def _fixed_plan(**updates) -> AgentPlan:
    plan = AgentPlan(
        objective="下载 web3",
        platform="douyin",
        session_ref=SESSION.session_ref,
        query="web3",
    )
    return plan.model_copy(update=updates)


def _dynamic_plan() -> DynamicAgentPlan:
    return DynamicAgentPlan(
        objective="分析 web3",
        platform="douyin",
        session_ref=SESSION.session_ref,
        summary="分析热门内容",
        steps=["搜索", "分析"],
    )


class FakeRuntime:
    def __init__(self, plan=None, error: Exception | None = None) -> None:
        self.plan = plan
        self.error = error
        self.proposals = 0

    def propose(self, message, session, previous_plan=None):
        self.proposals += 1
        if self.error is not None:
            raise self.error
        return self.plan


def _router(tmp_path: Path) -> RuntimeRouter:
    return RuntimeRouter(
        registry_path=tmp_path / "sessions.json",
        output_root=tmp_path / "output",
        conversation_id="test-conversation",
    )


def test_router_selects_harness_for_dynamic_task(tmp_path: Path) -> None:
    router = _router(tmp_path)
    fixed = FakeRuntime(plan=_fixed_plan())
    harness = FakeRuntime(plan=_dynamic_plan())
    router._deterministic = lambda: fixed  # type: ignore[method-assign]
    router._harness = lambda: harness  # type: ignore[method-assign]

    plan = router.propose("搜索 web3 并分析、生成文案", SESSION)

    assert isinstance(plan, DynamicAgentPlan)
    assert harness.proposals == 1
    assert fixed.proposals == 0


def test_router_falls_back_to_harness_when_fixed_parser_cannot_plan(tmp_path: Path) -> None:
    router = _router(tmp_path)
    fixed = FakeRuntime(error=PlanningError("not fixed"))
    harness = FakeRuntime(plan=_dynamic_plan())
    router._deterministic = lambda: fixed  # type: ignore[method-assign]
    router._harness = lambda: harness  # type: ignore[method-assign]

    assert isinstance(router.propose("完成一个非固定任务", SESSION), DynamicAgentPlan)
    assert fixed.proposals == 1
    assert harness.proposals == 1


def test_policy_rejection_happens_before_runtime_selection(tmp_path: Path) -> None:
    router = _router(tmp_path)
    with pytest.raises(PlanningPolicyError, match="不执行"):
        router.propose("分析完成后自动发布", SESSION)


def test_cancel_before_worker_mounts_runtime_is_not_lost(tmp_path: Path) -> None:
    router = _router(tmp_path)
    router.cancel()

    with pytest.raises(RuntimeError, match="cancelled before"):
        router.execute(_fixed_plan())


def test_execution_policy_owns_cross_runtime_limits() -> None:
    policy = ExecutionPolicy(max_total_download_mb=5_000)
    with pytest.raises(ExecutionPolicyError, match="容量"):
        policy.validate_plan(_fixed_plan(max_total_download_mb=6_000))


def test_fixed_native_result_is_normalized_for_frontends() -> None:
    plan = _fixed_plan()
    normalized = _fixed_result(
        AgentRunResult(
            plan=plan,
            discovered_urls=["https://www.douyin.com/video/1"],
            downloaded_items=1,
            artifact_count=2,
            output_directories=["/tmp/output"],
            tool_calls_used=2,
        )
    )

    assert normalized.runtime == "deterministic"
    assert normalized.metrics["artifact_count"] == 2
    assert normalized.output_directories == ["/tmp/output"]


def test_generic_llm_environment_takes_precedence_and_legacy_remains_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOCIAL_AGENT_OLLAMA_BASE_URL", "http://legacy/v1")
    monkeypatch.setenv("SOCIAL_AGENT_OLLAMA_MODEL", "legacy-model")
    monkeypatch.setenv("SOCIAL_AGENT_LLM_BASE_URL", "http://generic/v1/")
    monkeypatch.setenv("SOCIAL_AGENT_LLM_MODEL", "generic-model")
    monkeypatch.setenv("SOCIAL_AGENT_LLM_API_KEY", "secret")

    settings = LLMSettings.from_env()

    assert settings.base_url == "http://generic/v1"
    assert settings.model == "generic-model"
    assert settings.api_key == "secret"
