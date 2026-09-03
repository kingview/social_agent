"""Trusted task lifecycle hook, outside the model's tool catalog."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from .diagnostics import record_exception
from .plugins import PluginManager


def cleanup_task_browsers(state_root: Path, execution_id: str, *, manager=None) -> dict:
    result = {"closed_tabs": 0, "closed_windows": 0, "warnings": []}
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", execution_id):
        raise ValueError("Invalid execution ID")
    directory = state_root / "browser-resources" / execution_id
    if not directory.is_dir() or not any(directory.glob("*.json")):
        return result
    try:
        manager = manager or PluginManager()
        # One owner for the shared BitBrowser resource ledger. Do not invoke
        # arbitrary plugins, LLM-provided modules, or browser tool arguments.
        owner = next((item for item in manager.list()
                      if item.manifest.id == "com.socialagent.social-content"
                      and "browser-session" in item.manifest.permissions
                      and item.manifest.runtime.task_cleanup_module), None)
        if owner is None:
            raise RuntimeError("Browser cleanup plugin is unavailable")
        completed = subprocess.run(
            [str(owner.python), "-m", owner.manifest.runtime.task_cleanup_module,
             "--state-root", str(state_root.resolve()), "--execution-id", execution_id],
            env={**os.environ, "SOCIAL_AGENT_STATE_ROOT": str(state_root.resolve()),
                 "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45,
            check=True,
        )
        payload = json.loads(completed.stdout)
        for name in ("closed_tabs", "closed_windows"):
            if type(payload.get(name)) is not int or payload[name] < 0:
                raise ValueError("Invalid browser cleanup counts")
            result[name] = payload[name]
        warnings = payload.get("warnings", [])
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            raise ValueError("Invalid browser cleanup warnings")
        result["warnings"] = warnings
    except Exception as exc:
        record_exception("agent", "browser_resources.cleanup", exc,
                         state_root=state_root, execution_id=execution_id)
        result["warnings"] = ["任务已完成，但部分浏览器资源未能自动清理，详情已写入日志。"]
    return result
