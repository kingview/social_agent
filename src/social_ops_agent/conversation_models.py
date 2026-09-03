from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TurnStatus = Literal[
    "planning",
    "planned",
    "executing",
    "succeeded",
    "failed",
    "partial",
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
    publish_attempted: bool = False
    created_at: str
    updated_at: str


class ConversationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    conversation_id: str = Field(pattern=r"^conversation-[a-f0-9]{32}$")
    created_at: str
    updated_at: str
    turns: list[ConversationTurn] = Field(default_factory=list, max_length=200)
