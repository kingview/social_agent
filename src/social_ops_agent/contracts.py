from __future__ import annotations

from enum import StrEnum
from math import ceil
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class AgentPlatform(StrEnum):
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    X = "x"
    TELEGRAM = "telegram"


class AgentSource(StrEnum):
    SEARCH = "search"
    USER = "user"
    TIMELINE = "timeline"
    URL = "url"


class AgentView(StrEnum):
    TOP = "top"
    LATEST = "latest"
    MEDIA = "media"
    POSTS = "posts"
    REPLIES = "replies"
    USERS = "users"


class AgentMediaFormat(StrEnum):
    BEST = "best"
    VIDEO = "video"
    AUDIO = "audio"


class AttachmentModality(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class AgentAttachment(BaseModel):
    """One user-selected file staged into the Agent-owned input directory."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4_096)
    display_name: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=200)
    modality: AttachmentModality
    size_bytes: int = Field(ge=1, le=1_073_741_824)


class AgentPlan(BaseModel):
    """Validated plan produced from a conversation turn.

    The model may propose values, but this schema owns all executable limits.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fixed"] = "fixed"
    objective: str = Field(min_length=1, max_length=2_000)
    platform: AgentPlatform
    session_ref: str = Field(
        pattern=r"^sess_(?:douyin|xhs|x|telegram)_[A-Za-z0-9_-]{20,80}$",
        max_length=96,
    )
    source: AgentSource = AgentSource.SEARCH
    view: AgentView = AgentView.TOP
    query: str | None = Field(default=None, min_length=1, max_length=300)
    user_key: str | None = Field(default=None, min_length=1, max_length=300)
    start_url: HttpUrl | None = None
    limit: int = Field(default=20, ge=1, le=100)
    download: bool = False
    remove_watermark: bool = False
    watermark_minimum_confidence: float = Field(default=0.72, ge=0.5, le=0.99)
    media_format: AgentMediaFormat = AgentMediaFormat.BEST
    download_batch_size: int = Field(default=20, ge=1, le=20)
    max_total_download_mb: int = Field(default=5_000, ge=100, le=20_000)
    max_scrolls: int = Field(default=30, ge=0, le=50)
    tool_call_budget: int = Field(default=10, ge=1, le=20)
    requires_confirmation: bool = False

    @model_validator(mode="after")
    def validate_executable_plan(self) -> AgentPlan:
        prefix = {
            AgentPlatform.DOUYIN: "sess_douyin_",
            AgentPlatform.XIAOHONGSHU: "sess_xhs_",
            AgentPlatform.X: "sess_x_",
            AgentPlatform.TELEGRAM: "sess_telegram_",
        }[self.platform]
        if not self.session_ref.startswith(prefix):
            raise ValueError("session_ref platform does not match plan platform")
        allowed_views = {
            AgentPlatform.X: {
                AgentSource.SEARCH: {AgentView.TOP, AgentView.LATEST, AgentView.MEDIA},
                AgentSource.USER: {AgentView.POSTS, AgentView.MEDIA, AgentView.REPLIES},
                AgentSource.TIMELINE: {AgentView.LATEST},
                AgentSource.URL: {AgentView.LATEST},
            },
            AgentPlatform.DOUYIN: {
                AgentSource.SEARCH: {AgentView.TOP, AgentView.MEDIA, AgentView.USERS},
                AgentSource.USER: {AgentView.POSTS},
                AgentSource.TIMELINE: {AgentView.TOP},
                AgentSource.URL: {AgentView.TOP},
            },
            AgentPlatform.XIAOHONGSHU: {
                AgentSource.SEARCH: {AgentView.TOP, AgentView.LATEST, AgentView.MEDIA},
                AgentSource.USER: {AgentView.POSTS},
                AgentSource.TIMELINE: {AgentView.TOP},
                AgentSource.URL: {AgentView.TOP},
            },
            AgentPlatform.TELEGRAM: {
                AgentSource.USER: {AgentView.POSTS},
                AgentSource.URL: {AgentView.POSTS},
            },
        }
        if self.view not in allowed_views[self.platform][self.source]:
            raise ValueError("view is not supported for this platform and source")
        if self.source is AgentSource.SEARCH and not self.query:
            raise ValueError("query is required for search")
        if self.source is AgentSource.USER and not self.user_key:
            raise ValueError("user_key is required for user browsing")
        if self.source is AgentSource.URL and self.start_url is None:
            raise ValueError("start_url is required for URL browsing")
        if self.remove_watermark and not self.download:
            raise ValueError("watermark removal requires downloading media first")
        if self.start_url is not None:
            host = (urlsplit(str(self.start_url)).hostname or "").lower()
            domains = {
                AgentPlatform.DOUYIN: ("douyin.com", "iesdouyin.com"),
                AgentPlatform.XIAOHONGSHU: ("xiaohongshu.com", "xhslink.com"),
                AgentPlatform.X: ("x.com", "twitter.com"),
                AgentPlatform.TELEGRAM: ("t.me", "telegram.me", "web.telegram.org"),
            }[self.platform]
            if not any(host == domain or host.endswith(f".{domain}") for domain in domains):
                raise ValueError("start_url platform does not match plan platform")
        batches = ceil(self.limit / self.download_batch_size) if self.download else 0
        required_calls = 1 + batches + (batches if self.remove_watermark else 0)
        if required_calls > self.tool_call_budget:
            raise ValueError("tool_call_budget is too small for this plan")
        return self


class AgentProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    completed: int = Field(ge=0)
    total: int = Field(ge=1)
    message: str


class BrowserSessionBinding(BaseModel):
    """One locally registered BitBrowser profile authorized for a plan."""

    model_config = ConfigDict(extra="forbid")

    session_ref: str = Field(
        pattern=r"^sess_(?:douyin|xhs|x|telegram)_[A-Za-z0-9_-]{20,80}$",
        max_length=96,
    )
    platform: Literal["douyin", "xiaohongshu", "x", "telegram"]
    profile_name: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def validate_platform_prefix(self) -> BrowserSessionBinding:
        prefix = {
            "douyin": "sess_douyin_",
            "xiaohongshu": "sess_xhs_",
            "x": "sess_x_",
            "telegram": "sess_telegram_",
        }[self.platform]
        if not self.session_ref.startswith(prefix):
            raise ValueError("session_ref platform does not match browser session platform")
        return self


class DynamicAgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dynamic_harness"] = "dynamic_harness"
    objective: str = Field(min_length=1, max_length=80_000)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    platform: Literal["douyin", "xiaohongshu", "x", "telegram"] | None = None
    session_ref: str | None = Field(
        default=None,
        pattern=r"^sess_(?:douyin|xhs|x|telegram)_[A-Za-z0-9_-]{20,80}$",
    )
    browser_sessions: list[BrowserSessionBinding] = Field(default_factory=list, max_length=12)
    summary: str = Field(min_length=1, max_length=2_000)
    steps: list[str] = Field(min_length=1, max_length=12)
    step_tools: list[Literal[
        "browse_posts", "browser_operate", "download_media", "analyze_content",
        "process_watermark", "generate_post_copy", "publish_x_post", "call_plugin_tool",
        "local_reasoning",
    ]] = Field(default_factory=list, max_length=12)
    step_units: list[int] = Field(default_factory=list, max_length=12)
    resume_turn_id: str | None = Field(default=None, max_length=100)
    attachments: list[AgentAttachment] = Field(default_factory=list, max_length=8)
    media_context: str | None = Field(default=None, max_length=80_000)
    max_download_posts: int | None = Field(default=None, ge=1, le=100)
    write_actions: list[Literal["publish_x"]] = Field(default_factory=list, max_length=1)
    max_tool_calls: int = Field(default=20, ge=1, le=200)
    requires_confirmation: bool = False

    @model_validator(mode="after")
    def validate_optional_browser_session(self) -> DynamicAgentPlan:
        if self.step_tools and len(self.step_tools) != len(self.steps):
            raise ValueError("step_tools must contain one tool for each step")
        if self.step_units and (len(self.step_units) != len(self.steps) or
                                any(type(n) is not int or not 1 <= n <= 100 for n in self.step_units)):
            raise ValueError("step_units must contain a positive unit count (1..100) for each step")
        if self.step_tools and any(tool in {"publish_x_post", "local_reasoning"} and count != 1
                                   for tool, count in zip(self.step_tools, self.step_units)):
            raise ValueError("publishing and reasoning steps must have exactly one unit")
        if self.step_tools and sum(count for tool, count in
                                  zip(self.step_tools, self.step_units or [1] * len(self.steps))
                                  if tool != "local_reasoning") > self.max_tool_calls:
            raise ValueError("step_units require more calls than max_tool_calls")
        if (self.platform is None) != (self.session_ref is None):
            raise ValueError("platform and session_ref must either both be present or both be absent")
        if self.platform is not None and self.session_ref is not None:
            prefix = {
                "douyin": "sess_douyin_",
                "xiaohongshu": "sess_xhs_",
                "x": "sess_x_",
                "telegram": "sess_telegram_",
            }[self.platform]
            if not self.session_ref.startswith(prefix):
                raise ValueError("session_ref platform does not match plan platform")
        if self.browser_sessions:
            refs = [item.session_ref for item in self.browser_sessions]
            if len(refs) != len(set(refs)):
                raise ValueError("browser sessions must be unique")
            primary = self.browser_sessions[0]
            if self.platform != primary.platform or self.session_ref != primary.session_ref:
                raise ValueError("primary browser session must match platform and session_ref")
        if self.write_actions:
            available_platforms = (
                {item.platform for item in self.browser_sessions}
                if self.browser_sessions
                else ({self.platform} if self.platform else set())
            )
            if "x" not in available_platforms:
                raise ValueError("X publishing requires an authorized X browser session")
        return self

    def execution_steps(self) -> list[dict]:
        return [
            {"step_id": f"step-{index + 1}", "title": title,
             "tool": (self.step_tools or ["unverified"] * len(self.steps))[index],
             "units": (self.step_units or [1] * len(self.steps))[index]}
            for index, title in enumerate(self.steps)
        ]

    def authorized_browser_sessions(self) -> list[BrowserSessionBinding]:
        if self.browser_sessions:
            return list(self.browser_sessions)
        if self.platform is None or self.session_ref is None:
            return []
        return [
            BrowserSessionBinding(
                platform=self.platform,
                session_ref=self.session_ref,
            )
        ]


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: AgentPlan
    discovered_urls: list[HttpUrl]
    downloaded_items: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    watermark_detected_count: int = Field(default=0, ge=0)
    watermark_processed_count: int = Field(default=0, ge=0)
    output_directories: list[str] = Field(default_factory=list)
    watermark_output_directories: list[str] = Field(default_factory=list)
    tool_calls_used: int = Field(ge=0)
    cancelled: bool = False
    warnings: list[str] = Field(default_factory=list)


class AgentExecutionResult(BaseModel):
    """Runtime-neutral result consumed by desktop and service frontends."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["deterministic", "deepseek_harness"]
    plan: AgentPlan | DynamicAgentPlan
    summary: str
    tool_calls: list[str] = Field(default_factory=list)
    tool_calls_used: int = Field(default=0, ge=0)
    output_directories: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, int | float | str | bool] = Field(default_factory=dict)
    cancelled: bool = False
    finish_reason: str | None = None
    completion_status: Literal["completed", "partial", "failed"] = "completed"
    completed_steps: int = Field(default=0, ge=0)
    total_steps: int = Field(default=0, ge=0)
    publish_state: Literal["not_requested", "not_attempted", "published", "failed", "unknown"] = "not_requested"


class RuntimeHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Literal["deterministic", "deepseek_harness"]
    available: bool
    detail: str
