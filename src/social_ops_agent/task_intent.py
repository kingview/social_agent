"""Validate Harness-selected continuation against user-authored conversation turns."""
from __future__ import annotations

from typing import Any

from .harness_client import HarnessError
from .policy import publishing_declined, requested_write_actions
from .task_store import TaskRecord


def resolve_write_actions(
    payload: dict[str, Any], message: str, history: tuple[TaskRecord, ...] = (),
) -> tuple[list[str], str | None]:
    direct = list(requested_write_actions(message))
    desired = payload.get("write_actions", direct)
    if desired not in ([], ["publish_x"]):
        raise HarnessError("规划 write_actions 只能为空或 publish_x。")
    resume_id = payload.get("resume_turn_id")
    if resume_id is not None and (not isinstance(resume_id, str) or not resume_id):
        raise HarnessError("resume_turn_id 必须是历史任务标识或 null。")
    # Prompt summaries are deliberately not accepted as an authority source.
    by_id = {row.task_id: row for row in history}
    if resume_id and resume_id not in by_id:
        raise HarnessError("无法在本会话中找到要继续的任务，请重新指定原始任务。")
    if not desired:
        return [], resume_id
    if publishing_declined(message):
        raise HarnessError("本轮明确要求不发布，规划不得包含 X 发布。")
    if direct:
        return desired, resume_id
    # The model resolves what 'continue/retry' means; the core only validates
    # provenance. Never grant writes from a model summary or downloaded content.
    if not resume_id:
        raise HarnessError("发布计划缺少本轮明确要求或历史任务引用。")
    source = by_id[resume_id]
    visited: set[str] = set()
    while not requested_write_actions(source.message):
        key = source.task_id
        if key in visited or publishing_declined(source.message):
            raise HarnessError("历史任务不包含可继承的发布要求。")
        visited.add(key)
        source = by_id.get(source.resume_task_id)
        if source is None:
            raise HarnessError("历史任务不包含可继承的发布要求，请明确说明发布目标。")
    # Only this task's lineage matters, including sibling retries. Publishing in
    # an unrelated later task must not block resuming this one.
    lineage = {source.task_id}
    while True:
        expanded = lineage | {row.task_id for row in history if row.resume_task_id in lineage}
        if expanded == lineage:
            break
        lineage = expanded
    if any(
        row.publish_attempted or row.legacy_interrupted
        for row in history if row.task_id in lineage
    ):
        raise HarnessError("历史任务可能已经提交过 X 发布；请先核对账号，不能仅凭重试指令再次发布。")
    return desired, resume_id
