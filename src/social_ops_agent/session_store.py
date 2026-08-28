from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_ref: str
    platform: str
    provider: str
    profile_id: str
    profile_name: str
    api_url: str
    created_at: str
    updated_at: str


class SessionStore:
    """Read-only core view of session refs owned by the browser plugin."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def list(self) -> list[SessionRecord]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw = payload.get("sessions", []) if isinstance(payload, dict) else []
        records: list[SessionRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                records.append(SessionRecord(**item))
            except TypeError:
                continue
        return records

    def get(self, session_ref: str) -> SessionRecord | None:
        return next((item for item in self.list() if item.session_ref == session_ref), None)


def default_session_registry_path() -> Path:
    configured = os.getenv("POSTDROP_SESSION_REGISTRY")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "PostDrop" / "sessions.json"
