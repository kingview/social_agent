from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .contracts import AgentAttachment, AttachmentModality
from .plugins import PluginError, PluginInvoker, PluginManager
from .settings import LLMSettings


IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus"}
SUPPORTED_SUFFIXES = set(IMAGE_TYPES) | VIDEO_SUFFIXES | AUDIO_SUFFIXES
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 1024 * 1024 * 1024


class MultimodalInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedMultimodalInput:
    attachments: tuple[AgentAttachment, ...]
    media_context: str | None = None


def prepare_multimodal_input(
    paths: list[Path],
    *,
    message: str,
    conversation_id: str,
    output_root: Path,
    registry_path: Path,
    plugin_root: Path | None = None,
    settings: LLMSettings | None = None,
) -> PreparedMultimodalInput:
    if not paths:
        return PreparedMultimodalInput(attachments=())
    if len(paths) > MAX_ATTACHMENTS:
        raise MultimodalInputError(f"每条消息最多添加 {MAX_ATTACHMENTS} 个附件。")

    staged = _stage_files(paths, output_root=output_root, conversation_id=conversation_id)
    analyzable = [item.path for item in staged if item.modality is not AttachmentModality.IMAGE]
    if not analyzable:
        return PreparedMultimodalInput(attachments=tuple(staged))

    active_settings = settings or LLMSettings.from_env()
    invoker = PluginInvoker(
        PluginManager(plugin_root),
        session_registry=registry_path,
        output_root=output_root,
        state_root=output_root / ".social-agent-state",
        llm_base_url=active_settings.base_url,
        llm_model=active_settings.model,
        llm_api_key=active_settings.api_key,
    )
    try:
        analysis = asyncio.run(
            invoker.call(
                "analyze_content",
                {
                    "file_paths": analyzable,
                    "post_text": message or None,
                    "language_hint": "zh",
                },
            )
        )
    except PluginError as exc:
        raise MultimodalInputError(
            "视频和音频理解需要启用“媒体内容分析与修复”Tool 插件。"
            f"\n{exc}"
        ) from exc
    return PreparedMultimodalInput(
        attachments=tuple(staged),
        media_context=_bounded_analysis_context(analysis),
    )


def _stage_files(
    paths: list[Path],
    *,
    output_root: Path,
    conversation_id: str,
) -> list[AgentAttachment]:
    target_root = (
        output_root.expanduser().resolve()
        / ".social-agent-inputs"
        / _safe_component(conversation_id)
        / uuid.uuid4().hex
    )
    target_root.mkdir(parents=True, exist_ok=False)
    staged: list[AgentAttachment] = []
    try:
        for raw in paths:
            source = raw.expanduser().resolve(strict=True)
            if not source.is_file():
                raise MultimodalInputError(f"附件不是文件：{source.name}")
            modality, media_type = _classify(source)
            size = source.stat().st_size
            if size <= 0:
                raise MultimodalInputError(f"附件为空：{source.name}")
            if size > MAX_ATTACHMENT_BYTES:
                raise MultimodalInputError(f"附件超过 1 GB：{source.name}")
            target = target_root / f"{len(staged) + 1:02d}-{_safe_filename(source.name)}"
            shutil.copy2(source, target)
            staged.append(
                AgentAttachment(
                    path=str(target),
                    display_name=source.name,
                    media_type=media_type,
                    modality=modality,
                    size_bytes=size,
                )
            )
    except Exception:
        shutil.rmtree(target_root, ignore_errors=True)
        raise
    return staged


def _classify(path: Path) -> tuple[AttachmentModality, str]:
    suffix = path.suffix.lower()
    if suffix in IMAGE_TYPES:
        return AttachmentModality.IMAGE, IMAGE_TYPES[suffix]
    if suffix in VIDEO_SUFFIXES:
        return AttachmentModality.VIDEO, mimetypes.guess_type(path.name)[0] or "video/mp4"
    if suffix in AUDIO_SUFFIXES:
        return AttachmentModality.AUDIO, mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    raise MultimodalInputError(
        f"不支持的附件格式：{path.name}。支持图片、常见视频和音频格式。"
    )


def _bounded_analysis_context(value: object) -> str:
    if not isinstance(value, dict):
        raise MultimodalInputError("媒体 Tool 返回了无法识别的分析结果。")
    selected = {
        key: value.get(key)
        for key in (
            "language",
            "summary",
            "tags",
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
            "evidence",
            "assets",
            "warnings",
        )
        if key in value
    }
    payload = json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
    if len(payload) <= 70_000:
        return payload
    return payload[:69_900] + "\n[媒体分析上下文已截断]"


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return cleaned[:120] or "conversation"


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value).strip(". ")
    return cleaned[:240] or "attachment"
