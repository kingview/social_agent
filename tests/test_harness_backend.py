from __future__ import annotations

import pytest

from social_ops_agent.harness_backend import DynamicAgentPlan, _json_object, requires_dynamic_harness
from social_ops_agent.harness_client import HarnessError
from social_ops_agent.planner import PlanningPolicyError, validate_planning_policy


def test_dynamic_plan_contract_binds_platform_and_opaque_session() -> None:
    plan = DynamicAgentPlan(
        objective="分析抖音 web3 内容",
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        summary="搜索、下载、分析",
        steps=["搜索帖子", "下载媒体", "分析并总结"],
    )

    assert plan.requires_confirmation is True
    assert plan.mode == "dynamic_harness"
    assert plan.max_tool_calls == 20


def test_dynamic_marker_routes_complex_tasks_to_harness() -> None:
    assert requires_dynamic_harness("搜索 Web3 并分析内容、生成文案")
    assert requires_dynamic_harness("打开抖音，点击搜索框输入 Web3 后翻页")
    assert not requires_dynamic_harness("搜索 Web3 并下载前20个帖子")


def test_planning_policy_runs_before_any_llm_planner() -> None:
    with pytest.raises(PlanningPolicyError, match="不执行"):
        validate_planning_policy("分析热门帖子然后自动发布")


def test_json_object_accepts_fenced_model_output() -> None:
    assert _json_object("```json\n{\"summary\":\"ok\",\"steps\":[\"one\"]}\n```")["summary"] == "ok"


def test_json_object_wraps_malformed_model_output_in_harness_error() -> None:
    with pytest.raises(HarnessError, match="invalid JSON"):
        _json_object('{"summary":"ok","steps":[broken]}')
