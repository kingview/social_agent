"""Stable step/item addressing shared by the MCP bridge and event tracker."""
from __future__ import annotations


def resolve_step(steps: list[dict], tool: str, step_id: str | None,
                 step_item_id: str | None) -> tuple[int, str] | None:
    if step_id is None:
        candidates = [i for i, step in enumerate(steps) if step["tool"] == tool]
        # Legacy clients can only infer a globally unique, single-unit step.
        # Never move a retry to the next unfinished occurrence of the same tool.
        if len(candidates) != 1 or steps[candidates[0]]["units"] != 1:
            return None
        index = candidates[0]
    else:
        index = next((i for i, step in enumerate(steps) if step["step_id"] == step_id), -1)
        if index < 0 or steps[index]["tool"] != tool:
            raise ValueError("step_id does not match this planned tool")
    units = steps[index]["units"]
    item = step_item_id or ("item-1" if units == 1 else None)
    if item not in {f"item-{n + 1}" for n in range(units)}:
        raise ValueError("step_item_id must identify a planned unit (item-1, item-2, ...)")
    return index, item
