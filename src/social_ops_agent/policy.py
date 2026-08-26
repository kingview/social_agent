from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ExecutionPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    max_browse_items: int = 100
    max_download_urls_per_call: int = 20
    max_total_download_mb: int = 5_000
    max_tool_calls: int = 20

    def validate_message(self, message: str) -> None:
        forbidden = ("点赞", "评论", "关注", "转发", "发布", "私信", "自动登录")
        if any(word in message for word in forbidden):
            raise ExecutionPolicyError(
                "当前 Agent 只允许浏览和下载，也支持本地分析和生成草稿；"
                "不执行点赞、评论、关注、发布、私信或自动登录。"
            )

    def validate_plan(self, plan: Any) -> None:
        limit = getattr(plan, "limit", None)
        if limit is not None and int(limit) > self.max_browse_items:
            raise ExecutionPolicyError("计划浏览数量超过执行策略上限。")
        batch_size = getattr(plan, "download_batch_size", None)
        if batch_size is not None and int(batch_size) > self.max_download_urls_per_call:
            raise ExecutionPolicyError("计划下载批次超过执行策略上限。")
        download_mb = getattr(plan, "max_total_download_mb", self.max_total_download_mb)
        if int(download_mb) > self.max_total_download_mb:
            raise ExecutionPolicyError("计划下载容量超过执行策略上限。")
        tool_calls = getattr(
            plan,
            "max_tool_calls",
            getattr(plan, "tool_call_budget", self.max_tool_calls),
        )
        if int(tool_calls) > self.max_tool_calls:
            raise ExecutionPolicyError("计划 Tool 调用次数超过执行策略上限。")


DEFAULT_EXECUTION_POLICY = ExecutionPolicy()
