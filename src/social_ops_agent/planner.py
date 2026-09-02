from __future__ import annotations

import json
import re
from dataclasses import dataclass
from math import ceil
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from .contracts import (
    AgentMediaFormat,
    AgentPlan,
    AgentPlatform,
    AgentSource,
    AgentView,
)
from .policy import DEFAULT_EXECUTION_POLICY, ExecutionPolicyError
from .settings import LLMSettings


class PlanningError(ValueError):
    pass


class PlanningPolicyError(PlanningError):
    pass


def validate_planning_policy(message: str) -> None:
    """Reject platform side effects before any deterministic or LLM planning."""
    try:
        DEFAULT_EXECUTION_POLICY.validate_message(message)
    except ExecutionPolicyError as exc:
        raise PlanningPolicyError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SelectedSession:
    session_ref: str
    platform: str
    profile_name: str = ""


class ConversationalPlanner:
    """Deterministic-first planner with an optional local Ollama JSON fallback."""

    def __init__(
        self,
        *,
        ollama_base_url: str | None = None,
        ollama_model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        settings = LLMSettings.from_env()
        self._ollama_base_url = (
            ollama_base_url
            or settings.base_url
        ).rstrip("/")
        self._ollama_model = (
            ollama_model
            or settings.model
        )
        self._api_key = api_key or settings.api_key
        self._timeout_seconds = timeout_seconds

    def create_plan(
        self,
        message: str,
        session: SelectedSession,
        previous_plan: AgentPlan | None = None,
    ) -> AgentPlan:
        cleaned = " ".join(message.split()).strip()
        if not cleaned:
            raise PlanningError("请输入希望 Agent 执行的任务。")
        validate_planning_policy(cleaned)
        try:
            draft = _deterministic_draft(cleaned, session, previous_plan)
        except PlanningPolicyError:
            raise
        except PlanningError as deterministic_error:
            try:
                draft = self._ollama_draft(cleaned, session, previous_plan)
            except (OSError, ValueError, ValidationError, URLError):
                raise deterministic_error
        return _validated_plan(cleaned, session, draft)

    def _ollama_draft(
        self,
        message: str,
        session: SelectedSession,
        previous_plan: AgentPlan | None,
    ) -> dict[str, Any]:
        previous = previous_plan.model_dump(mode="json", exclude={"session_ref"}) if previous_plan else None
        system = """你是本地社媒只读 Agent 的计划器。只输出 JSON，不执行操作。
允许平台：douyin、xiaohongshu、x、telegram。
允许 source：search、user、timeline、url。
允许 view：top、latest、media、posts、replies、users。
字段：platform,source,view,query,user_key,start_url,limit,download,remove_watermark,media_format。
limit 必须是 1..100；media_format 只能 best、video、audio。
不要输出 cookie、密码、代理、登录步骤、点赞、评论、关注或发布操作。"""
        payload = {
            "model": self._ollama_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "selected_platform": session.platform,
                            "previous_plan": previous,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = Request(
            f"{self._ollama_base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        return _json_object(str(content))


def _deterministic_draft(
    message: str,
    session: SelectedSession,
    previous_plan: AgentPlan | None,
) -> dict[str, Any]:
    platform = _detect_platform(message) or AgentPlatform(session.platform)
    source = AgentSource.SEARCH
    start_url = _first_https_url(message)
    user_key = None
    query = _quoted_text(message) or _keyword_text(message)

    if start_url:
        source = AgentSource.URL
        platform = _platform_from_url(start_url) or platform
    elif (
        "时间线" in message
        or "推荐流" in message
        or "首页" in message
        or ("随机" in message and not query)
    ):
        source = AgentSource.TIMELINE
    elif "用户主页" in message or "作者主页" in message:
        source = AgentSource.USER
        user_match = re.search(r"(?:用户|作者|账号)\s*[@：:]?\s*([A-Za-z0-9_.-]{2,100})", message)
        user_key = user_match.group(1) if user_match else None

    if previous_plan is not None:
        if not query and source is AgentSource.SEARCH:
            query = previous_plan.query
        if source is AgentSource.USER and not user_key:
            user_key = previous_plan.user_key
        if source is AgentSource.URL and not start_url:
            start_url = str(previous_plan.start_url) if previous_plan.start_url else None

    if source is AgentSource.SEARCH and not query:
        raise PlanningError("没有识别到搜索关键词。可以这样说：在抖音搜索关键词“web3”并下载前100个帖子。")
    if source is AgentSource.USER and not user_key:
        raise PlanningError("没有识别到用户 ID。请写明用户主页对应的 ID。")

    requested_limit = _limit(message)
    limit = (
        requested_limit
        or (1 if source is AgentSource.TIMELINE and "随机" in message else None)
        or (previous_plan.limit if previous_plan else 20)
    )
    download = "下载" in message or (previous_plan.download if previous_plan and "改成" in message else False)
    remove_watermark = any(word in message for word in ("去水印", "移除水印", "去掉水印")) or (
        previous_plan.remove_watermark if previous_plan and "改成" in message else False
    )
    if remove_watermark:
        download = True
    media_format = _media_format(message)
    view = _view(platform, source, message)
    if previous_plan and not any(word in message for word in ("热门", "最新", "视频", "用户")):
        view = previous_plan.view
    return {
        "platform": platform.value,
        "source": source.value,
        "view": view.value,
        "query": query,
        "user_key": user_key,
        "start_url": start_url,
        "limit": limit,
        "download": download,
        "remove_watermark": remove_watermark,
        "media_format": media_format.value,
    }


def _validated_plan(
    objective: str,
    session: SelectedSession,
    draft: dict[str, Any],
) -> AgentPlan:
    draft = dict(draft)
    proposed_platform = str(draft.get("platform") or session.platform)
    if proposed_platform != session.platform:
        raise PlanningError(
            "任务平台与当前 session_ref 不一致。请在界面选择该平台对应的登录会话。"
        )
    draft["objective"] = objective
    draft["session_ref"] = session.session_ref
    draft["platform"] = session.platform
    limit = max(1, min(int(draft.get("limit") or 20), 100))
    batch_size = 20
    draft["limit"] = limit
    draft["download_batch_size"] = batch_size
    batches = ceil(limit / batch_size) if draft.get("download") else 0
    draft["tool_call_budget"] = min(
        20,
        1 + batches + (batches if draft.get("remove_watermark") else 0),
    )
    draft["max_scrolls"] = min(50, max(8, ceil(limit / 4)))
    draft["requires_confirmation"] = False
    allowed = set(AgentPlan.model_fields)
    try:
        return AgentPlan.model_validate({key: value for key, value in draft.items() if key in allowed})
    except ValidationError as exc:
        raise PlanningError(f"任务计划不完整：{exc.errors()[0]['msg']}") from exc


def _detect_platform(message: str) -> AgentPlatform | None:
    lowered = message.lower()
    if "抖音" in message or "douyin" in lowered:
        return AgentPlatform.DOUYIN
    if "小红书" in message or "xiaohongshu" in lowered or "xhs" in lowered:
        return AgentPlatform.XIAOHONGSHU
    if "twitter" in lowered or re.search(r"(?:^|\s)x(?:平台|上|\s|$)", lowered):
        return AgentPlatform.X
    if "telegram" in lowered or "电报" in message or "tg频道" in lowered:
        return AgentPlatform.TELEGRAM
    return None


def _first_https_url(message: str) -> str | None:
    match = re.search(r"https://[^\s，。]+", message)
    return match.group(0).rstrip(")]}>，。") if match else None


def _platform_from_url(url: str) -> AgentPlatform | None:
    if "douyin.com" in url:
        return AgentPlatform.DOUYIN
    if "xiaohongshu.com" in url or "xhslink.com" in url:
        return AgentPlatform.XIAOHONGSHU
    if "x.com" in url or "twitter.com" in url:
        return AgentPlatform.X
    if "t.me/" in url or "telegram.me/" in url or "web.telegram.org" in url:
        return AgentPlatform.TELEGRAM
    return None


def _quoted_text(message: str) -> str | None:
    match = re.search(r"[“\"']([^”\"']{1,300})[”\"']", message)
    return match.group(1).strip() if match else None


def _keyword_text(message: str) -> str | None:
    match = re.search(r"关键词\s*[:：]?\s*([^\s，并，。]{1,300})", message)
    if match:
        return match.group(1).strip()
    match = re.search(r"搜索\s+(.+?)(?:\s*(?:并|然后|前\s*\d+|帖子)|$)", message)
    return match.group(1).strip(" ，。") if match else None


def _limit(message: str) -> int | None:
    if re.search(r"首\s*(?:个|条|篇)?\s*(?:帖子|结果|内容)?", message):
        return 1
    count_token = r"(\d{1,3}|[零一二两三四五六七八九十百]{1,5})"
    for pattern in (
        rf"(?:前|第)\s*{count_token}\s*(?:个|条|篇)?\s*(?:帖子|结果|内容)?",
        rf"{count_token}\s*(?:个|条|篇)\s*(?:帖子|结果|内容)",
    ):
        match = re.search(pattern, message)
        if match:
            parsed = _count_value(match.group(1))
            if parsed is not None:
                return max(1, min(parsed, 100))
    return None


def requested_download_limit(message: str) -> int | None:
    """Return an explicit post-download count for policy enforcement.

    Harness remains responsible for understanding the whole request. This
    narrow parser only turns an unambiguous quantity following “下载” into a
    hard upper bound so a model cannot expand “下载第一条” into a bulk download.
    """
    download_at = message.find("下载")
    if download_at < 0:
        return None
    tail = message[download_at:]
    parsed = _limit(tail)
    if parsed is not None:
        return parsed
    count_token = r"(\d{1,3}|[零一二两三四五六七八九十百]{1,5})"
    match = re.search(rf"下载(?:搜索结果)?(?:中|里的?|的)?\s*{count_token}\s*(?:个|条|篇)", tail)
    if not match:
        return None
    value = _count_value(match.group(1))
    return None if value is None else max(1, min(value, 100))


def _count_value(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if token == "百":
        return 100
    if "百" in token:
        hundreds, remainder = token.split("百", 1)
        if hundreds not in digits or digits[hundreds] == 0:
            return None
        suffix = _count_value(remainder) if remainder else 0
        return None if suffix is None else digits[hundreds] * 100 + suffix
    if token == "十":
        return 10
    if "十" in token:
        tens, ones = token.split("十", 1)
        tens_value = 1 if not tens else digits.get(tens)
        ones_value = 0 if not ones else digits.get(ones)
        if tens_value is None or ones_value is None:
            return None
        return tens_value * 10 + ones_value
    if len(token) == 1:
        return digits.get(token)
    return None


def _media_format(message: str) -> AgentMediaFormat:
    if "仅音频" in message or "只下载音频" in message:
        return AgentMediaFormat.AUDIO
    if "仅视频" in message or "只下载视频" in message:
        return AgentMediaFormat.VIDEO
    return AgentMediaFormat.BEST


def _view(platform: AgentPlatform, source: AgentSource, message: str) -> AgentView:
    if source is AgentSource.USER:
        if platform is AgentPlatform.X and "回复" in message:
            return AgentView.REPLIES
        if platform is AgentPlatform.X and "媒体" in message:
            return AgentView.MEDIA
        return AgentView.POSTS
    if source is AgentSource.TIMELINE:
        return AgentView.LATEST if platform is AgentPlatform.X else AgentView.TOP
    if source is AgentSource.URL:
        if platform is AgentPlatform.X:
            return AgentView.LATEST
        if platform is AgentPlatform.TELEGRAM:
            return AgentView.POSTS
        return AgentView.TOP
    if platform is AgentPlatform.DOUYIN and "用户" in message:
        return AgentView.USERS
    if "最新" in message and platform is not AgentPlatform.DOUYIN:
        return AgentView.LATEST
    if "视频" in message or "媒体" in message:
        return AgentView.MEDIA
    return AgentView.TOP


def _json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model did not return JSON")
    result = json.loads(content[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("model JSON is not an object")
    return result
