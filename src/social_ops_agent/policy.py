from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


class ExecutionPolicyError(ValueError):
    pass


_X_PUBLISH_INTENT = re.compile(
    r"(?:发(?:布|帖|送)?\s*(?:一条|这个|这条|该条)?\s*(?:到|至|在)?\s*(?:X|Twitter|推特)|"
    r"(?:在|到)\s*(?:X|Twitter|推特)\s*(?:上)?(?:发|发布|发帖)|"
    r"post\s+(?:it\s+)?to\s+(?:x|twitter))",
    re.IGNORECASE,
)


def requested_write_actions(message: str) -> tuple[str, ...]:
    return ("publish_x",) if _X_PUBLISH_INTENT.search(message) else ()


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    max_browse_items: int = 100
    max_download_urls_per_call: int = 20
    max_total_download_mb: int = 5_000
    max_tool_calls: int = 200

    def validate_message(self, message: str) -> None:
        forbidden = ("点赞", "评论", "关注", "转发", "私信", "自动登录")
        if any(word in message for word in forbidden):
            raise ExecutionPolicyError(
                "当前 Agent 不执行点赞、评论、关注、转发、私信或自动登录。"
            )
        write_actions = requested_write_actions(message)
        generic_publish = any(word in message for word in ("发布", "发帖", "发送到"))
        if generic_publish and not write_actions:
            raise ExecutionPolicyError("当前只支持经过确认后自动发布到 X，不支持其他平台写操作。")

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
        write_actions = tuple(getattr(plan, "write_actions", ()))
        if write_actions and write_actions != ("publish_x",):
            raise ExecutionPolicyError("计划包含未授权的平台写操作。")


DEFAULT_EXECUTION_POLICY = ExecutionPolicy()
