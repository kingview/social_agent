"""Qt-independent data returned by bounded selection queries."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SelectionPage:
    entries: list[tuple[str, str]]
    next_cursor: object = None
