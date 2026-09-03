from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from .conversation_models import ConversationSnapshot, ConversationTurn, TurnStatus
from .conversation_repository import ConversationRepository

from .contracts import AgentExecutionResult, AgentPlan, DynamicAgentPlan
from .task_store import TaskStore


class ConversationCoordinator:
    """GUI transcript and compact model context; TaskStore owns execution facts."""

    def __init__(self, state_root: Path, *, conversation_id: str | None = None,
                 create_new: bool = False) -> None:
        self.state_root = state_root.expanduser().resolve()
        self.repository = ConversationRepository(self.state_root)
        self.task_store = TaskStore(self.state_root)
        loaded = None if create_new else (
            self.repository.load(conversation_id) if conversation_id is not None
            else self.repository.default()
        )
        if conversation_id is not None and loaded is None:
            raise ValueError("Conversation does not exist or its ID does not match")
        self.snapshot = loaded or self._fresh_snapshot()
        self.path = self.repository.path(self.conversation_id)
        for turn in self.snapshot.turns:
            self.task_store.import_turn(self.conversation_id, turn.model_dump(mode="json"))
            self._sync_execution(turn)
        if self._recover_interrupted_turn():
            self._save()
        elif not self.path.is_file():
            self._save()

    @classmethod
    def catalog(cls, state_root: Path) -> list[ConversationSnapshot]:
        return ConversationRepository(state_root).catalog()

    @property
    def conversation_id(self) -> str:
        return self.snapshot.conversation_id

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        return tuple(self.snapshot.turns)

    def new_conversation(self) -> str:
        self.snapshot = self._fresh_snapshot()
        self.path = self.repository.path(self.conversation_id)
        self._save()
        return self.snapshot.conversation_id

    def begin_turn(
        self,
        message: str,
        *,
        attachment_names: list[str] | None = None,
        session_ref: str | None = None,
        platform: str | None = None,
    ) -> str:
        self._recover_interrupted_turn()
        now = _timestamp()
        turn_id = f"turn-{uuid.uuid4().hex}"
        self.snapshot.turns.append(
            ConversationTurn(
                turn_id=turn_id,
                user_message=message,
                attachment_names=list(attachment_names or ()),
                session_ref=session_ref,
                platform=platform,
                created_at=now,
                updated_at=now,
            )
        )
        if len(self.snapshot.turns) > 200:
            self.snapshot.turns = self.snapshot.turns[-200:]
        self.task_store.ensure_task(self.conversation_id, message, turn_id)
        self._save()
        return turn_id

    def mark_planned(self, turn_id: str, plan: AgentPlan | DynamicAgentPlan) -> None:
        turn = self._turn(turn_id)
        turn.status = "planned"
        turn.plan = _plan_payload(plan)
        if isinstance(plan, DynamicAgentPlan):
            turn.session_ref = plan.session_ref
            turn.platform = plan.platform
        turn.error_stage = None
        turn.error = None
        self._touch(turn)

    def mark_executing(self, turn_id: str) -> None:
        turn = self._turn(turn_id)
        turn.status = "executing"
        self._touch(turn)

    def mark_publish_attempted(self, turn_id: str) -> None:
        turn = self._turn(turn_id)
        turn.publish_attempted = True
        self._touch(turn)

    def mark_succeeded(self, turn_id: str, result: AgentExecutionResult) -> None:
        turn = self._turn(turn_id)
        turn.status = (
            "cancelled" if result.cancelled
            else "succeeded" if result.completion_status == "completed"
            else result.completion_status
        )
        turn.result = _result_payload(result)
        turn.error_stage = None
        turn.error = None
        self._touch(turn)

    def mark_cancelled(self, turn_id: str, reason: str) -> None:
        turn = self._turn(turn_id)
        turn.status = "cancelled"
        turn.error_stage = None
        turn.error = reason.strip() or "任务已取消。"
        self._touch(turn)

    def mark_failed(
        self,
        turn_id: str,
        *,
        stage: Literal["planning", "execution", "interrupted"],
        error: str,
    ) -> None:
        turn = self._turn(turn_id)
        turn.status = "failed"
        turn.error_stage = stage
        turn.error = error.strip() or "任务失败。"
        self._touch(turn)

    def context_for_next_turn(self) -> str | None:
        return self.task_store.model_context(self.conversation_id)

    def last_result(self) -> AgentExecutionResult | None:
        for turn in reversed(self.snapshot.turns):
            if not isinstance(turn.result, dict):
                continue
            try:
                return AgentExecutionResult.model_validate(turn.result)
            except ValidationError:
                continue
        return None

    def last_session_ref(self) -> str | None:
        for turn in reversed(self.snapshot.turns):
            if turn.session_ref:
                return turn.session_ref
        return None

    def _recover_interrupted_turn(self) -> bool:
        changed = False
        for turn in self.snapshot.turns:
            if turn.status not in {"planning", "planned", "executing"}:
                continue
            self.task_store.recover_interrupted(turn.turn_id)
            turn.status = "failed"
            turn.error_stage = "interrupted"
            turn.error = "应用在该任务完成前退出；可以发送“重试”继续上一任务。"
            turn.updated_at = _timestamp()
            self.task_store.import_turn(self.conversation_id, turn.model_dump(mode="json"))
            changed = True
        return changed

    def _turn(self, turn_id: str) -> ConversationTurn:
        for turn in reversed(self.snapshot.turns):
            if turn.turn_id == turn_id:
                return turn
        raise KeyError(f"unknown conversation turn: {turn_id}")

    def _touch(self, turn: ConversationTurn) -> None:
        turn.updated_at = _timestamp()
        self.task_store.import_turn(self.conversation_id, turn.model_dump(mode="json"))
        self._sync_execution(turn)
        self._save()

    def _sync_execution(self, turn: ConversationTurn) -> None:
        report = self.task_store.execution(turn.turn_id)
        if not report:
            return
        turn.publish_attempted = report["publish_attempted"]
        if report["state"] == "executing" or not turn.plan:
            return
        turn.status = report["state"]
        if "summary" in report:
            turn.result = AgentExecutionResult(
                runtime="deepseek_harness", plan=turn.plan,
                **{key: value for key, value in report.items()
                   if key in {"summary", "warnings", "tool_calls", "completed_steps", "total_steps",
                              "completion_status", "publish_state", "cancelled", "finish_reason"}},
            ).model_dump(mode="json")
            turn.error_stage = None
            turn.error = None
        if report.get("error"):
            turn.error_stage = "execution"
            turn.error = report["error"]

    def _save(self) -> None:
        self.snapshot.updated_at = _timestamp()
        self.repository.save(self.snapshot)

    @staticmethod
    def _fresh_snapshot() -> ConversationSnapshot:
        now = _timestamp()
        return ConversationSnapshot(
            conversation_id=f"conversation-{uuid.uuid4().hex}",
            created_at=now,
            updated_at=now,
        )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _plan_payload(plan: AgentPlan | DynamicAgentPlan) -> dict:
    payload = plan.model_dump(mode="json")
    if "media_context" in payload:
        payload["media_context"] = None
    return payload


def _result_payload(result: AgentExecutionResult) -> dict:
    payload = result.model_dump(mode="json")
    payload["summary"] = str(payload.get("summary") or "")[:50_000]
    plan = payload.get("plan")
    if isinstance(plan, dict) and "media_context" in plan:
        plan["media_context"] = None
    return payload
