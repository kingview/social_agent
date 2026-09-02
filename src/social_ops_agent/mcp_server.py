from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .execution_policy_channel import read_execution_policy
from .plugins import PluginInvoker, PluginManager
from .policy import DEFAULT_EXECUTION_POLICY
from .settings import LLMSettings


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
Only use the selected opaque session_ref. Never request raw cookies, passwords,
verification codes, proxy credentials, or browser fingerprints. Installed Tool
plugins must not like, comment, follow, repost, message, purchase, or log into an
account. X publishing is allowed only through publish_x_post with the one-time
approval token issued after GUI confirmation. Original downloaded media must be preserved."""

mcp = FastMCP("social-agent-tools", instructions=SERVER_INSTRUCTIONS)


class PluginToolRuntime:
    def __init__(self) -> None:
        self.output_root = _required_path("SOCIAL_AGENT_OUTPUT_ROOT")
        self.state_root = _required_path("SOCIAL_AGENT_STATE_ROOT")
        self.session_registry = _required_path("SOCIAL_AGENT_SESSION_REGISTRY", file=True)
        self.policy = DEFAULT_EXECUTION_POLICY
        self.download_budget_remaining_bytes = self.policy.max_total_download_mb * 1024 * 1024
        raw_policy_path = os.getenv("SOCIAL_AGENT_EXECUTION_POLICY_PATH", "").strip()
        self.execution_policy_path = (
            Path(raw_policy_path).expanduser().resolve() if raw_policy_path else None
        )
        self.active_execution_id: str | None = None
        self.max_download_posts: int | None = None
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
async def call_plugin_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a Tool declared by an installed and enabled plugin.

    Use this for capabilities added by future plugins after list_plugin_tools.
    Prefer the typed compatibility Tools below for standard social operations.
    """
    if tool_name in _STANDARD_TYPED_TOOLS:
        raise ValueError(
            f"{tool_name} is a standard Tool; call mcp__social__{tool_name} directly"
        )
    return await _invoke_plugin_tool(tool_name, arguments)


async def _invoke_plugin_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    output = await runtime().invoker.call(tool_name, arguments)
    return output if isinstance(output, dict) else {"result": output}


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
) -> dict[str, Any]:
    """Browse Douyin, Xiaohongshu, X, or Telegram Web through the social-content plugin."""
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
) -> dict[str, Any]:
    """Perform one read-only BitBrowser operation.

    For input, provide exactly one target (normally element_ref from observe) and
    put the content in value. text is a target locator, not the input content.
    url is only valid for navigate.
    """
    return await _invoke_plugin_tool("browser_operate", locals())


@mcp.tool()
async def download_media(
    urls: list[str],
    session_ref: str,
    media_format: str = "best",
    max_total_size_mb: int = 1000,
    telegram_scope: Literal["messages", "channel"] = "messages",
    telegram_max_messages: int = 2000,
) -> dict[str, Any]:
    """Download posts, or one complete Telegram channel with checkpoints."""
    tool_runtime = runtime()
    async with tool_runtime.download_lock:
        tool_runtime.refresh_execution_policy()
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
) -> dict[str, Any]:
    """Analyze local images, videos, or audio using the analyzer plugin."""
    return await _invoke_plugin_tool("analyze_content", locals())


@mcp.tool()
async def process_watermark(
    file_paths: list[str],
    minimum_confidence: float = 0.72,
    repair_quality: str = "auto",
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
) -> dict[str, Any]:
    """Generate local social copy drafts grounded in an analysis result."""
    return await _invoke_plugin_tool("generate_post_copy", locals())


@mcp.tool()
async def publish_x_post(
    session_ref: str,
    text: str,
    approval_token: str,
    media_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Publish exactly one X post approved in the current GUI execution plan."""
    tool_runtime = runtime()
    async with tool_runtime.publish_lock:
        expected = tool_runtime.publish_approval_token
        if (
            not expected
            or tool_runtime.publish_approval_consumed
            or not hmac.compare_digest(approval_token, expected)
        ):
            raise ValueError("X publication approval is missing, invalid, expired, or already used")
        # Consume before forwarding because an ambiguous browser result must not be retried.
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
        return output if isinstance(output, dict) else {"result": output}


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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
