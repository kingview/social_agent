"""Normalize MCP result envelopes and conservatively verify tool success."""
from __future__ import annotations

import json
from typing import Any


def result_payload(content: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6:
        return None
    if isinstance(content, str):
        try:
            return result_payload(json.loads(content), depth + 1)
        except (ValueError, TypeError):
            return None
    if isinstance(content, list):
        for block in content:
            value = result_payload(block, depth + 1)
            if value is not None:
                return value
        return None
    if not isinstance(content, dict) or content.get("isError"):
        return None
    if content.get("type") == "text":
        return result_payload(content.get("text"), depth + 1)
    if "structuredContent" in content:
        return result_payload(content["structuredContent"], depth + 1)
    if "content" in content:
        return result_payload(content["content"], depth + 1)
    return content


def successful_result(tool: str, payload: dict[str, Any] | None) -> bool:
    if not payload or payload.get("error") or payload.get("errors"):
        return False
    state = payload.get("state", payload.get("status"))
    if isinstance(state, str) and state in {"failed", "unknown", "error", "partial"}:
        return False
    if payload.get("completed") is False or payload.get("success") is False:
        return False
    if tool == "publish_x_post":
        return payload.get("state") == "published"
    if tool == "browse_posts":
        return bool(payload.get("posts"))
    if tool == "download_media":
        items = payload.get("items") or []
        if items and any(not isinstance(item, dict) or item.get("error") or
                         item.get("status") in {"failed", "partial", "error"} or
                         item.get("completed") is False for item in items):
            return False
        return bool(payload.get("artifacts") or items and all(item.get("artifacts") for item in items))
    if tool == "generate_post_copy":
        return bool(payload.get("variants"))
    return True
