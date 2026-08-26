from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import HttpUrl

from media_content_analyzer import (
    AnalyzeContentInput,
    ArtifactRef,
    ContentAnalysisOutput,
    GeneratePostCopyInput,
    ProcessWatermarkInput,
    build_local_copy_tool,
    build_local_tool,
    build_local_watermark_tool,
)
from media_content_analyzer.ports import ToolContext as AnalyzerToolContext
from social_content_crawler import (
    BrowsePostsInput,
    DownloadInput,
    InMemoryAuditSink,
    LocalRateLimiter,
    PublicHttpsUrlPolicy,
    SessionRegistry,
    SocialMediaDownloadTool,
    SocialPostBrowseTool,
    SocialPostBrowserBackend,
    YtDlpBackend,
)
from social_content_crawler.platforms import default_allowed_domains
from social_content_crawler.ports import ToolContext as CrawlerToolContext

from .policy import DEFAULT_EXECUTION_POLICY
from .settings import LLMSettings


SERVER_INSTRUCTIONS = """Local read/analysis tools for Social Agent.
Only use the selected opaque session_ref. Never request raw cookies, passwords,
verification codes, proxy credentials, or browser fingerprints. These tools do
not publish, like, comment, follow, repost, message, or log into an account."""

mcp = FastMCP("social-agent-tools", instructions=SERVER_INSTRUCTIONS)


class SocialToolRuntime:
    def __init__(self) -> None:
        self.output_root = _required_path("SOCIAL_AGENT_OUTPUT_ROOT")
        self.state_root = _required_path("SOCIAL_AGENT_STATE_ROOT")
        self.policy = DEFAULT_EXECUTION_POLICY
        self.settings = LLMSettings.from_env()
        self.download_budget_remaining_bytes = self.policy.max_total_download_mb * 1024 * 1024
        self.download_lock = asyncio.Lock()
        registry = SessionRegistry(_required_path("SOCIAL_AGENT_SESSION_REGISTRY", file=True))
        audit = InMemoryAuditSink()
        limiter = LocalRateLimiter()
        self.browse_tool = SocialPostBrowseTool(
            backend=SocialPostBrowserBackend(session_registry=registry),
            audit_sink=audit,
            rate_limiter=limiter,
        )
        self.download_tool = SocialMediaDownloadTool(
            backend=YtDlpBackend(session_registry=registry),
            audit_sink=audit,
            rate_limiter=limiter,
            url_policy=PublicHttpsUrlPolicy(),
            output_root=self.output_root,
            allowed_domains=default_allowed_domains(),
        )
        analyzer_state = self.state_root / "analysis"
        self.analyze_tool = build_local_tool(
            allowed_media_root=self.output_root,
            state_root=analyzer_state,
            model_base_url=self.settings.base_url,
            model_name=self.settings.model,
            model_api_key=self.settings.api_key,
        )
        self.copy_tool = build_local_copy_tool(
            state_root=self.state_root / "copy",
            model_base_url=self.settings.base_url,
            model_name=self.settings.model,
            model_api_key=self.settings.api_key,
        )
        self.watermark_tool = build_local_watermark_tool(
            allowed_media_root=self.output_root,
            state_root=self.state_root / "watermark",
            output_root=self.output_root / "watermark-processed",
        )

    @staticmethod
    def crawler_context() -> CrawlerToolContext:
        run_id = uuid.uuid4().hex
        return CrawlerToolContext(
            tenant_id="local-agent",
            trace_id=f"harness-{run_id}",
            actor_type="agent",
            actor_id="deepseek-harness",
            agent_run_id=run_id,
        )

    @staticmethod
    def analyzer_context() -> AnalyzerToolContext:
        run_id = uuid.uuid4().hex
        return AnalyzerToolContext(
            tenant_id="local-agent",
            trace_id=f"harness-{run_id}",
            actor_type="agent",
            actor_id="deepseek-harness",
            agent_run_id=run_id,
        )


_runtime: SocialToolRuntime | None = None


def runtime() -> SocialToolRuntime:
    global _runtime
    if _runtime is None:
        _runtime = SocialToolRuntime()
    return _runtime


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
    """Browse Douyin, Xiaohongshu, or X and return post URLs and metadata.

    Requires an opaque session_ref that is already registered locally for the
    same platform. This is a read-only browser operation. max_items is 1..100.
    """
    request = BrowsePostsInput(
        platform=platform,
        session_ref=session_ref,
        source=source,
        view=view,
        query=query,
        user_key=user_key,
        start_url=start_url,
        max_items=max_items,
        max_scrolls=max_scrolls,
    )
    output = await runtime().browse_tool.execute(request, runtime().crawler_context())
    return output.model_dump(mode="json")


@mcp.tool()
async def download_media(
    urls: list[str],
    session_ref: str,
    media_format: str = "best",
    max_total_size_mb: int = 1000,
) -> dict[str, Any]:
    """Download media from up to 20 HTTPS social post URLs into local storage.

    media_format is best, video, or audio. The opaque session_ref must match
    the URL platform. Original media is always preserved.
    """
    tool_runtime = runtime()
    async with tool_runtime.download_lock:
        remaining_mb = tool_runtime.download_budget_remaining_bytes // (1024 * 1024)
        if remaining_mb < 1:
            raise ValueError(
                "the confirmed execution download budget "
                f"({tool_runtime.policy.max_total_download_mb} MB) is exhausted"
            )
        request = DownloadInput(
            urls=[HttpUrl(url) for url in urls],
            session_ref=session_ref,
            media_format=media_format,
            max_items=min(len(urls), 20),
            max_total_size_mb=min(max_total_size_mb, remaining_mb),
        )
        output = await tool_runtime.download_tool.execute(
            request,
            tool_runtime.crawler_context(),
        )
        used_bytes = sum(artifact.size_bytes for artifact in output.artifacts)
        tool_runtime.download_budget_remaining_bytes = max(
            0,
            tool_runtime.download_budget_remaining_bytes - used_bytes,
        )
        payload = output.model_dump(mode="json")
        payload["execution_download_budget_remaining_mb"] = round(
            tool_runtime.download_budget_remaining_bytes / (1024 * 1024),
            2,
        )
        return payload


@mcp.tool()
async def analyze_content(
    file_paths: list[str],
    post_text: str | None = None,
    source_url: str | None = None,
    language_hint: str | None = "zh",
) -> dict[str, Any]:
    """Analyze up to 100 downloaded local images, videos, or audio files.

    Produces OCR/transcript evidence, summary, topics, tags, entities, safety
    flags, confidence, and per-asset details. Paths must be under the configured
    Social Agent output directory.
    """
    artifacts = [_artifact(path) for path in file_paths]
    request = AnalyzeContentInput(
        artifacts=artifacts,
        post_text=post_text,
        source_url=source_url,
        language_hint=language_hint,
    )
    output = await runtime().analyze_tool.execute(request, runtime().analyzer_context())
    return output.model_dump(mode="json")


@mcp.tool()
async def process_watermark(
    file_paths: list[str],
    minimum_confidence: float = 0.72,
    repair_quality: str = "auto",
) -> dict[str, Any]:
    """Detect watermarks and create repaired copies of downloaded videos.

    The original files are always preserved. This tool is available only after
    the desktop user has confirmed the overall execution plan.
    """
    request = ProcessWatermarkInput(
        artifacts=[_artifact(path) for path in file_paths],
        mode="remove_if_present",
        authorization_confirmed=True,
        minimum_confidence=minimum_confidence,
        repair_quality=repair_quality,
    )
    output = await runtime().watermark_tool.execute(request, runtime().analyzer_context())
    return output.model_dump(mode="json")


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
    """Generate draft social copy grounded in a completed analysis result.

    This only creates local drafts; it never publishes content. The analysis
    argument must be the structured result returned by analyze_content.
    """
    request = GeneratePostCopyInput(
        analysis=ContentAnalysisOutput.model_validate(analysis),
        platform=platform,
        tone=tone,
        objective=objective,
        extra_instructions=extra_instructions,
        variant_count=variant_count,
        max_characters=max_characters,
    )
    output = await runtime().copy_tool.execute(request, runtime().analyzer_context())
    return output.model_dump(mode="json")


def _artifact(raw_path: str) -> ArtifactRef:
    path = Path(raw_path).expanduser().resolve(strict=True)
    root = runtime().output_root.resolve()
    if path != root and root not in path.parents:
        raise ValueError("media path is outside the configured Social Agent output directory")
    if not path.is_file():
        raise ValueError("media path is not a file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return ArtifactRef(
        path=str(path),
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        media_type=media_type,
    )


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
