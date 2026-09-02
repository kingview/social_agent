from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import AgentExecutionResult, AgentPlan, DynamicAgentPlan


TurnStatus = Literal[
    "planning",
    "planned",
    "executing",
    "succeeded",
    "failed",
    "cancelled",
]


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=100)
    user_message: str = Field(min_length=1, max_length=80_000)
    attachment_names: list[str] = Field(default_factory=list, max_length=8)
    session_ref: str | None = Field(default=None, max_length=100)
    platform: str | None = Field(default=None, max_length=40)
    status: TurnStatus = "planning"
    plan: dict | None = None
    result: dict | None = None
    error_stage: Literal["planning", "execution", "interrupted"] | None = None
    error: str | None = Field(default=None, max_length=20_000)
    created_at: str
    updated_at: str


class ConversationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    conversation_id: str = Field(pattern=r"^conversation-[a-f0-9]{32}$")
    created_at: str
    updated_at: str
    turns: list[ConversationTurn] = Field(default_factory=list, max_length=200)


class ConversationCoordinator:
    """Single durable owner of GUI turns and Harness conversation identity."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root.expanduser().resolve()
        self.path = self.state_root / "conversations" / "active.json"
        self.snapshot = self._load() or self._fresh_snapshot()
        if self._recover_interrupted_turn():
            self._save()
        elif not self.path.is_file():
            self._save()

    @property
    def conversation_id(self) -> str:
        return self.snapshot.conversation_id

    @property
    def turns(self) -> tuple[ConversationTurn, ...]:
        return tuple(self.snapshot.turns)

    def new_conversation(self) -> str:
        self.snapshot = self._fresh_snapshot()
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
        self._save()
        return turn_id

    def mark_planned(self, turn_id: str, plan: AgentPlan | DynamicAgentPlan) -> None:
        turn = self._turn(turn_id)
        turn.status = "planned"
        turn.plan = _plan_payload(plan)
        turn.error_stage = None
        turn.error = None
        self._touch(turn)

    def mark_executing(self, turn_id: str) -> None:
        turn = self._turn(turn_id)
        turn.status = "executing"
        self._touch(turn)

    def mark_succeeded(self, turn_id: str, result: AgentExecutionResult) -> None:
        turn = self._turn(turn_id)
        turn.status = "cancelled" if result.cancelled else "succeeded"
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
        if not self.snapshot.turns:
            return None
        rows: list[dict[str, object]] = []
        for turn in reversed(self.snapshot.turns):
            plan_summary = None
            if isinstance(turn.plan, dict):
                plan_summary = turn.plan.get("summary") or turn.plan.get("objective")
            result_summary = None
            if isinstance(turn.result, dict):
                result_summary = turn.result.get("summary")
            row = {
                "user_message": _excerpt(turn.user_message, 1_500),
                "status": turn.status,
                "plan_summary": _excerpt(plan_summary, 1_000),
                "result_summary": _excerpt(result_summary, 2_000),
                "error_stage": turn.error_stage,
                "error": _excerpt(turn.error, 2_000),
            }
            candidate = [row, *rows]
            encoded = json.dumps(
                {"recent_turns": candidate},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(encoded) > 20_000 and rows:
                break
            rows = candidate
        return json.dumps(
            {"recent_turns": rows},
            ensure_ascii=False,
            separators=(",", ":"),
        )

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
            turn.status = "failed"
            turn.error_stage = "interrupted"
            turn.error = "应用在该任务完成前退出；可以发送“重试”继续上一任务。"
            turn.updated_at = _timestamp()
            changed = True
        return changed

    def _turn(self, turn_id: str) -> ConversationTurn:
        for turn in reversed(self.snapshot.turns):
            if turn.turn_id == turn_id:
                return turn
        raise KeyError(f"unknown conversation turn: {turn_id}")

    def _touch(self, turn: ConversationTurn) -> None:
        turn.updated_at = _timestamp()
        self._save()

    def _load(self) -> ConversationSnapshot | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return ConversationSnapshot.model_validate(payload)
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValidationError):
            return None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot.updated_at = _timestamp()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                self.snapshot.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

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


def _excerpt(value: object, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


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
