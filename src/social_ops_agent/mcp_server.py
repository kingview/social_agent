from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .execution_policy_channel import read_execution_policy
from .plugins import PluginInvoker, PluginManager
from .policy import DEFAULT_EXECUTION_POLICY
from .settings import LLMSettings
from .step_binding import resolve_step
from .task_store import TaskStore


_STANDARD_TYPED_TOOLS = {
    "analyze_content",
    "browse_posts",
    "browser_operate",
    "download_media",
    "generate_post_copy",
    "process_watermark",
    "publish_x_post",
}


SERVER_INSTRUCTIONS = """Local Tool plugin bridge for Social Agent.
Only use opaque session_ref values authorized for the current execution. Never request raw cookies, passwords,
verification codes, proxy credentials, or browser fingerprints. Installed Tool
plugins must not like, comment, follow, repost, message, purchase, or log into an
account. X publishing is allowed only through publish_x_post with the one-time
approval token issued for the user's current or explicitly resumed task. Original downloaded media must be preserved."""

mcp = FastMCP("social-agent-tools", instructions=SERVER_INSTRUCTIONS)


class PluginToolRuntime:
    def __init__(self) -> None:
        self.output_root = _required_path("SOCIAL_AGENT_OUTPUT_ROOT")
        self.state_root = _required_path("SOCIAL_AGENT_STATE_ROOT")
        self.task_store = TaskStore(self.state_root)
        self.session_registry = _required_path("SOCIAL_AGENT_SESSION_REGISTRY", file=True)
        self.policy = DEFAULT_EXECUTION_POLICY
        self.download_budget_remaining_bytes = self.policy.max_total_download_mb * 1024 * 1024
        raw_policy_path = os.getenv("SOCIAL_AGENT_EXECUTION_POLICY_PATH", "").strip()
        self.execution_policy_path = (
            Path(raw_policy_path).expanduser().resolve() if raw_policy_path else None
        )
        self.active_execution_id: str | None = None
        self.task_id: str | None = None
        self.steps: list[dict] = []
        self.max_download_posts: int | None = None
        self.allowed_session_refs: set[str] = set()
        self.downloaded_post_urls: set[str] = set()
        self.download_lock = asyncio.Lock()
        self.publish_lock = asyncio.Lock()
        self.publish_approval_token = os.getenv("SOCIAL_AGENT_X_PUBLISH_APPROVAL_TOKEN", "")
        self.publish_approval_consumed = False
        settings = LLMSettings.from_env()
        self.manager = PluginManager()
        self.invoker = PluginInvoker(
            self.manager,
            session_registry=self.session_registry,
            output_root=self.output_root,
            state_root=self.state_root,
            llm_base_url=settings.base_url,
            llm_model=settings.model,
            llm_api_key=settings.api_key,
        )

    def refresh_execution_policy(self) -> None:
        policy = read_execution_policy(self.execution_policy_path)
        execution_id = policy.execution_id if policy is not None else None
        if execution_id != self.active_execution_id:
            self.active_execution_id = execution_id
            self.downloaded_post_urls.clear()
            self.download_budget_remaining_bytes = (
                self.policy.max_total_download_mb * 1024 * 1024
            )
        self.max_download_posts = policy.max_download_posts if policy is not None else None
        self.task_id = policy.task_id if policy else None
        self.steps = policy.steps if policy else []
        self.allowed_session_refs = (
            set(policy.allowed_session_refs) if policy is not None else set()
        )

    def require_authorized_session(self, session_ref: str) -> None:
        self.refresh_execution_policy()
        if not self.active_execution_id or session_ref not in self.allowed_session_refs:
            raise ValueError("session_ref is not authorized for the current execution")


_runtime: PluginToolRuntime | None = None


def runtime() -> PluginToolRuntime:
    global _runtime
    if _runtime is None:
        _runtime = PluginToolRuntime()
    return _runtime


@mcp.tool()
async def list_plugin_tools(live_schemas: bool = True) -> dict[str, Any]:
    """List enabled plugins using their live MCP schemas by default.

    Set live_schemas=false only for a fast installation inventory. The live MCP
    response is the authoritative description and input/output schema.
    """
    tool_runtime = runtime()
    if live_schemas:
        return {"plugins": await tool_runtime.invoker.live_catalog()}
    return {
        "plugins": [
            item for item in tool_runtime.manager.catalog() if item.get("enabled") is True
        ]
    }


@mcp.tool()
async def call_plugin_tool(tool_name: str, arguments: dict[str, Any],
                           step_id: str | None = None, step_item_id: str | None = None) -> dict[str, Any]:
    """Call a Tool declared by an installed and enabled plugin.

    Use this for capabilities added by future plugins after list_plugin_tools.
    Prefer the typed compatibility Tools below for standard social operations.
    """
    if tool_name in _STANDARD_TYPED_TOOLS:
        raise ValueError(
            f"{tool_name} is a standard Tool; call mcp__social__{tool_name} directly"
        )
    _check_step("call_plugin_tool", step_id, step_item_id)
    output = await runtime().invoker.call(tool_name, arguments)
    return output if isinstance(output, dict) else {"result": output}


async def _invoke_plugin_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    arguments = dict(arguments)
    _check_step(tool_name, arguments.pop("step_id", None), arguments.pop("step_item_id", None))
    output = await runtime().invoker.call(tool_name, arguments)
    return output if isinstance(output, dict) else {"result": output}


def _check_step(tool: str, step_id: str | None, step_item_id: str | None) -> None:
    tool_runtime = runtime()
    tool_runtime.refresh_execution_policy()
    steps = getattr(tool_runtime, "steps", [])
    binding = resolve_step(steps, tool, step_id, step_item_id)
    if binding is None and any(step["tool"] == tool for step in steps):
        raise ValueError("Repeated or batched planned tools require step_id and step_item_id")


@mcp.tool()
async def browse_posts(
    platform: str,
    session_ref: str,
    source: str = "search",
    view: str = "top",
    query: str | None = None,
    user_key: str | None = None,
    start_url: str | None = None,
    max_items: int = 20,
    max_scrolls: int = 8,
    step_id: str | None = None,
    step_item_id: str | None = None,
) -> dict[str, Any]:
    """Browse Douyin, Xiaohongshu, X, or Telegram Web through the social-content plugin."""
    runtime().require_authorized_session(session_ref)
    return await _invoke_plugin_tool(
        "browse_posts",
        {
            "platform": platform,
            "session_ref": session_ref,
            "source": source,
            "view": view,
            "query": query,
            "user_key": user_key,
            "start_url": start_url,
            "max_items": max_items,
            "max_scrolls": max_scrolls,
            "step_id": step_id,
            "step_item_id": step_item_id,
        },
    )


@mcp.tool()
async def browser_operate(
    session_ref: str,
    action: Literal[
        "observe",
        "navigate",
        "click",
        "input",
        "press",
        "scroll",
        "swipe_up",
        "swipe_down",
        "back",
        "forward",
        "reload",
        "wait",
    ],
    url: str | None = None,
    element_ref: str | None = None,
    selector: str | None = None,
    role: str | None = None,
    name: str | None = None,
    text: str | None = None,
    value: str | None = None,
    key: str | None = None,
    scroll_y: int = 900,
    timeout_seconds: float = 30.0,
    wait_after_ms: int = 600,
    max_elements: int = 40,
    text_excerpt_chars: int = 4_000,
    step_id: str | None = None,
    step_item_id: str | None = None,
) -> dict[str, Any]:
    """Perform one read-only BitBrowser operation.

    For input, provide exactly one target (normally element_ref from observe) and
    put the content in value. text is a target locator, not the input content.
    url is only valid for navigate.
    """
    runtime().require_authorized_session(session_ref)
    return await _invoke_plugin_tool("browser_operate", locals())


@mcp.tool()
async def download_media(
    urls: list[str],
    session_ref: str,
    media_format: str = "best",
    max_total_size_mb: int = 1000,
    telegram_scope: Literal["messages", "channel"] = "messages",
    telegram_max_messages: int = 2000,
    step_id: str | None = None,
    step_item_id: str | None = None,
) -> dict[str, Any]:
    """Download posts, or one complete Telegram channel with checkpoints."""
    tool_runtime = runtime()
    async with tool_runtime.download_lock:
        tool_runtime.refresh_execution_policy()
        tool_runtime.require_authorized_session(session_ref)
        _check_step("download_media", step_id, step_item_id)
        unique_urls = list(dict.fromkeys(urls))[: tool_runtime.policy.max_download_urls_per_call]
        if telegram_scope != "channel" and tool_runtime.max_download_posts is not None:
            remaining_posts = max(
                0,
                tool_runtime.max_download_posts - len(tool_runtime.downloaded_post_urls),
            )
            unique_urls = [
                url for url in unique_urls if url not in tool_runtime.downloaded_post_urls
            ][:remaining_posts]
            if not unique_urls:
                raise ValueError("the approved post download count is exhausted")
        remaining_mb = tool_runtime.download_budget_remaining_bytes // (1024 * 1024)
        if remaining_mb < 1:
            raise ValueError("the confirmed execution download budget is exhausted")
        output = await tool_runtime.invoker.call(
            "download_media",
            {
                "urls": unique_urls,
                "session_ref": session_ref,
                "media_format": media_format,
                "max_total_size_mb": min(max_total_size_mb, remaining_mb),
                "telegram_scope": telegram_scope,
                "telegram_max_messages": telegram_max_messages,
            },
        )
        if not isinstance(output, dict):
            raise ValueError("download plugin returned a non-object result")
        if telegram_scope != "channel":
            tool_runtime.downloaded_post_urls.update(unique_urls)
        used_bytes = sum(
            int(item.get("size_bytes") or 0)
            for item in output.get("artifacts", [])
            if isinstance(item, dict)
        )
        tool_runtime.download_budget_remaining_bytes = max(
            0, tool_runtime.download_budget_remaining_bytes - used_bytes
        )
        output["execution_download_budget_remaining_mb"] = round(
            tool_runtime.download_budget_remaining_bytes / (1024 * 1024), 2
        )
        output["execution_max_download_posts"] = tool_runtime.max_download_posts
        output["execution_download_posts_remaining"] = (
            None
            if tool_runtime.max_download_posts is None
            else max(
                0,
                tool_runtime.max_download_posts - len(tool_runtime.downloaded_post_urls),
            )
        )
        return output


@mcp.tool()
async def analyze_content(
    file_paths: list[str],
    post_text: str | None = None,
    source_url: str | None = None,
    language_hint: str | None = "zh",
    step_id: str | None = None,
    step_item_id: str | None = None,
) -> dict[str, Any]:
    """Analyze local images, videos, or audio using the analyzer plugin."""
    output = await _invoke_plugin_tool("analyze_content", locals())
    return _compact_analysis_output(output, runtime().output_root)


@mcp.tool()
async def process_watermark(
    file_paths: list[str],
    minimum_confidence: float = 0.72,
    repair_quality: str = "auto",
    step_id: str | None = None,
    step_item_id: str | None = None,
) -> dict[str, Any]:
    """Detect watermarks and create repaired copies; originals are preserved."""
    return await _invoke_plugin_tool("process_watermark", locals())


@mcp.tool()
async def generate_post_copy(
    analysis: dict[str, Any],
    platform: str = "generic",
    tone: str = "natural",
    objective: str | None = None,
    extra_instructions: str | None = None,
    variant_count: int = 3,
    max_characters: int = 300,
    step_id: str | None = None,
    step_item_id: str | None = None,
) -> dict[str, Any]:
    """Generate local social copy drafts grounded in an analysis result."""
    return await _invoke_plugin_tool("generate_post_copy", locals())


@mcp.tool()
async def publish_x_post(
    session_ref: str,
    text: str,
    approval_token: str,
    media_paths: list[str] | None = None,
    step_id: str | None = None,
    step_item_id: str | None = None,
) -> dict[str, Any]:
    """Publish exactly one X post authorized by the current task."""
    tool_runtime = runtime()
    async with tool_runtime.publish_lock:
        tool_runtime.require_authorized_session(session_ref)
        _check_step("publish_x_post", step_id, step_item_id)
        expected = tool_runtime.publish_approval_token
        if (
            not expected
            or tool_runtime.publish_approval_consumed
            or not hmac.compare_digest(approval_token, expected)
        ):
            raise ValueError("X publication approval is missing, invalid, expired, or already used")
        # Consume before forwarding because an ambiguous browser result must not be retried.
        if not tool_runtime.task_id or not tool_runtime.active_execution_id:
            raise ValueError("X publication requires a durable task execution")
        task_id, execution_id = tool_runtime.task_id, tool_runtime.active_execution_id
        tool_runtime.task_store.reserve_publish(task_id, execution_id)
        tool_runtime.publish_approval_consumed = True
        output = await tool_runtime.invoker.call(
            "publish_x_post",
            {
                "session_ref": session_ref,
                "text": text,
                "approval_token": approval_token,
                "media_paths": media_paths or [],
            },
        )
        output = output if isinstance(output, dict) else {"result": output}
        tool_runtime.task_store.publish_result(task_id, execution_id, output.get("state", "unknown"))
        return output


def _required_path(name: str, *, file: bool = False) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required")
    path = Path(raw).expanduser().resolve()
    if file:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _compact_analysis_output(output: dict[str, Any], output_root: Path) -> dict[str, Any]:
    """Persist the complete analysis while keeping the model context bounded."""
    encoded = json.dumps(output, ensure_ascii=False, indent=2)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    result_dir = output_root / "analysis-results"
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f"analysis-{digest[:16]}.json"
    if not result_path.is_file():
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(result_path)

    compact_keys = (
        "language",
        "summary",
        "topics",
        "entities",
        "claims",
        "image_summary",
        "video_summary",
        "transcript_summary",
        "sentiment",
        "commercial_intent",
        "safety_flags",
        "confidence",
        "needs_human_review",
        "warnings",
        "cache_hit",
        "pipeline_version",
        "model_versions",
    )
    compact = {key: output.get(key) for key in compact_keys if key in output}
    compact["tags"] = [
        {
            key: tag.get(key)
            for key in ("namespace", "label", "confidence")
            if key in tag
        }
        for tag in output.get("tags", [])[:50]
        if isinstance(tag, dict)
    ]
    compact["evidence_preview"] = [
        {
            **{
                key: evidence.get(key)
                for key in ("evidence_id", "kind", "timestamp_seconds", "confidence")
                if key in evidence
            },
            "text": str(evidence.get("text") or "")[:500],
        }
        for evidence in output.get("evidence", [])[:12]
        if isinstance(evidence, dict)
    ]
    compact["assets"] = [
        {
            key: asset.get(key)
            for key in (
                "artifact_sha256",
                "media_type",
                "modality",
                "width",
                "height",
                "duration_seconds",
                "sampled_frame_count",
                "warnings",
            )
            if key in asset
        }
        for asset in output.get("assets", [])[:20]
        if isinstance(asset, dict)
    ]
    compact["full_result_path"] = str(result_path)
    compact["full_result_sha256"] = digest
    compact["context_compacted"] = len(encoded) > 20_000
    for key in (
        "summary",
        "image_summary",
        "video_summary",
        "transcript_summary",
        "sentiment",
        "commercial_intent",
    ):
        if key in compact:
            compact[key] = str(compact[key] or "")[:4_000]
    if len(json.dumps(compact, ensure_ascii=False)) > 20_000:
        compact["claims"] = [str(item)[:500] for item in compact.get("claims", [])[:20]]
        compact["entities"] = list(compact.get("entities", []))[:20]
        compact["evidence_preview"] = compact["evidence_preview"][:5]
        compact["context_compacted"] = True
    return compact


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
