"""Transcript persistence, independent of task execution and the selected GUI tab."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .conversation_models import ConversationSnapshot
from .state_io import write_json


class ConversationRepository:
    def __init__(self, state_root: Path):
        self.root = state_root / "conversations"

    def path(self, conversation_id: str) -> Path:
        if not re.fullmatch(r"conversation-[a-f0-9]{32}", conversation_id):
            raise ValueError("Invalid conversation ID")
        return self.root / f"{conversation_id}.json"

    def load(self, conversation_id: str) -> ConversationSnapshot | None:
        path = self.path(conversation_id)
        snapshot = self._read(path)
        return snapshot if snapshot and snapshot.conversation_id == conversation_id else None

    @staticmethod
    def _read(path: Path) -> ConversationSnapshot | None:
        try:
            return ConversationSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, snapshot: ConversationSnapshot) -> None:
        write_json(self.path(snapshot.conversation_id), snapshot.model_dump(mode="json"))

    def catalog(self) -> list[ConversationSnapshot]:
        records = []
        for path in self.root.glob("conversation-*.json"):
            snapshot = self._read(path)
            if snapshot and path.stem == snapshot.conversation_id:
                records.append(snapshot)
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def migrate_legacy(self) -> ConversationSnapshot | None:
        """Import active.json once. Never overwrite a newer per-ID transcript."""
        marker = self.root / "legacy-migration.json"
        if marker.exists():
            return None
        legacy = self._read(self.root / "active.json")
        if legacy is None:
            return None
        current = self.load(legacy.conversation_id)
        if current is None:
            self.save(legacy)
        write_json(marker, {"version": 1, "conversation_id": legacy.conversation_id})
        return current or legacy

    def default(self) -> ConversationSnapshot | None:
        migrated = self.migrate_legacy()
        selected = WorkspaceStore(self.root.parent).load().get("selected")
        if isinstance(selected, str):
            try:
                snapshot = self.load(selected)
                if snapshot:
                    return snapshot
            except ValueError:
                pass
        records = self.catalog()
        return records[0] if records else migrated


class WorkspaceStore:
    """Only tab membership/selection; progress updates never rewrite transcripts."""
    def __init__(self, state_root: Path):
        self.path = state_root / "conversations" / "workspace.json"
        self._last_saved: dict | None = None

    def load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict) or not isinstance(payload.get("open_conversations"), list):
            return {}
        return payload

    def save(self, conversation_ids: list[str], selected: str | None) -> None:
        payload = {"version": 1, "open_conversations": conversation_ids, "selected": selected}
        if payload != self._last_saved:
            write_json(self.path, payload)
            self._last_saved = payload
