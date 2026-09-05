from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ExecutionPolicyGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1, max_length=100)
    max_download_posts: int | None = Field(default=None, ge=1, le=500)
    allowed_session_refs: list[str] = Field(default_factory=list, max_length=12)
    task_id: str | None = None
    steps: list[dict] = Field(default_factory=list)


class ExecutionPolicyChannel:
    """Atomic, short-lived policy handoff from the core to the persistent MCP."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def grant(
        self,
        execution_id: str,
        *,
        max_download_posts: int | None,
        allowed_session_refs: list[str] | None = None,
        task_id: str | None = None,
        steps: list[dict] | None = None,
    ) -> None:
        policy = ExecutionPolicyGrant(
            execution_id=execution_id,
            max_download_posts=max_download_posts,
            allowed_session_refs=list(dict.fromkeys(allowed_session_refs or ())),
            task_id=task_id,
            steps=steps or [],
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                policy.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def revoke(self, execution_id: str) -> None:
        policy = read_execution_policy(self.path)
        if policy is not None and policy.execution_id == execution_id:
            self.path.unlink(missing_ok=True)


def read_execution_policy(path: Path | None) -> ExecutionPolicyGrant | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ExecutionPolicyGrant.model_validate(payload)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValidationError):
        return None
