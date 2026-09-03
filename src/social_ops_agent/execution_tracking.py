"""Match observable tool results to planned steps, never calls to percentages."""
from __future__ import annotations

import json
from typing import Any

from .contracts import AgentProgress, DynamicAgentPlan
from .step_binding import resolve_step
from .tool_results import result_payload, successful_result


class ExecutionTracker:
    def __init__(self, plan: DynamicAgentPlan) -> None:
        self.plan = plan
        self.tools = plan.step_tools or ["unverified"] * len(plan.steps)
        self.steps = plan.execution_steps()
        self.completed_items: set[tuple[int, str]] = set()
        self.completed: set[int] = set()
        self.calls: dict[str, tuple[str, tuple[int, str] | None]] = {}
        self.call_records: dict[str, dict] = {}
        self.handled_results: set[str] = set()
        self.tool_calls: list[str] = []
        self.publish_state = "not_attempted" if plan.write_actions else "not_requested"

    def called(self, data: dict[str, Any]) -> AgentProgress:
        name = str(data.get("name") or "Tool")
        short = name.removeprefix("mcp__social__")
        call_id = str(data.get("callId") or f"call-{len(self.tool_calls)}")
        if call_id in self.calls or call_id in self.handled_results:
            return self.progress("等待当前工具返回结果。")
        self.tool_calls.append(name)
        arguments = data.get("arguments", {})
        try:
            # Native Harness events carry serialized JSON; test/programmatic
            # clients may already supply a dictionary. Do not infer a step from
            # malformed JSON, an array, or a scalar.
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            if any(arguments.get(key) is not None and not isinstance(arguments[key], str)
                   for key in ("step_id", "step_item_id")):
                raise ValueError("Step references must be strings")
            binding = resolve_step(self.steps, short, arguments.get("step_id"), arguments.get("step_item_id"))
        except (ValueError, AttributeError):
            binding = None
        index = binding[0] if binding is not None else None
        self.calls[call_id] = (short, binding)
        self.call_records[call_id] = {"tool": short, "step_id": self.steps[index]["step_id"] if index is not None else None,
                                      "step_item_id": binding[1] if binding else None, "status": "running"}
        if short == "publish_x_post":
            self.publish_state = "unknown"
        message = (
            f"第 {index + 1}/{len(self.tools)} 步：{self.plan.steps[index]}（正在调用 {short}）"
            if index is not None else f"正在调用辅助工具 {short}（不增加已完成步骤）"
        )
        return self.progress(message, stage="publishing" if short == "publish_x_post" else "step")

    def returned(self, data: dict[str, Any]) -> AgentProgress:
        message = data.get("message", {})
        blocks = message.get("content", []) if isinstance(message, dict) else []
        updates = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool-result":
                continue
            call_id = str(block.get("toolCallId", ""))
            if call_id in self.handled_results or call_id not in self.calls:
                continue
            self.handled_results.add(call_id)
            short, binding = self.calls.pop(call_id)
            index = binding[0] if binding is not None else None
            payload = result_payload(block.get("content"))
            successful = not block.get("isError") and successful_result(short, payload)
            self.call_records[call_id]["status"] = "succeeded" if successful else "failed"
            if short == "publish_x_post":
                state = payload.get("state") if payload else None
                self.publish_state = (
                    state if not block.get("isError") and isinstance(state, str) and state in {"published", "failed", "unknown"}
                    else "unknown"
                )
            if index is not None:
                if successful:
                    self.completed_items.add(binding)
                    if sum(i == index for i, _ in self.completed_items) == self.steps[index]["units"]:
                        self.completed.add(index)
                updates.append(
                    f"第 {index + 1} 步{'完成' if index in self.completed else '尚未完成'}：{self.plan.steps[index]}"
                )
        return self.progress("；".join(updates) or "工具返回，正在核对执行结果。")

    def finish(self, *, normal_end: bool) -> AgentProgress:
        # A text-only reasoning stage is verifiable at the end only when all
        # preceding tool stages completed. Missing tool evidence is not success.
        for index, tool in enumerate(self.tools):
            if normal_end and tool == "local_reasoning" and all(i in self.completed for i in range(index)):
                self.completed.add(index)
        complete = len(self.completed) == len(self.tools) and normal_end
        message = (
            f"全部 {len(self.tools)} 步执行完成。" if complete
            else f"任务未全部完成：已完成 {len(self.completed)}/{len(self.tools)} 步。"
        )
        return self.progress(message, stage="done" if complete else "incomplete")

    def reconcile_publication(self, state: str) -> None:
        """Core MCP's durable submission boundary outranks a tool-call event."""
        self.publish_state = state
        if state != "published":
            self.completed.difference_update(i for i, tool in enumerate(self.tools) if tool == "publish_x_post")
            self.completed_items = {(i, item) for i, item in self.completed_items if self.tools[i] != "publish_x_post"}

    def progress(self, message: str, *, stage: str = "step") -> AgentProgress:
        return AgentProgress(stage=stage, completed=len(self.completed), total=len(self.tools), message=message)

    def unfinished(self) -> list[str]:
        return [f"第 {i + 1} 步 {step}" for i, step in enumerate(self.plan.steps) if i not in self.completed]

    def report(self) -> dict:
        return {"completed_steps": len(self.completed), "total_steps": len(self.steps),
                "publish_state": self.publish_state, "tool_calls": list(self.tool_calls),
                "calls": dict(self.call_records),
                "steps": [{**step, "status": "completed" if i in self.completed else "pending",
                           "completed_units": sum(n == i for n, _ in self.completed_items)}
                          for i, step in enumerate(self.steps)]}
