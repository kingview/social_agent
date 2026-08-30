from __future__ import annotations

import base64
from pathlib import Path

import pytest

from social_ops_agent.contracts import AgentAttachment, DynamicAgentPlan
from social_ops_agent.harness_backend import _content_blocks
from social_ops_agent.multimodal import MultimodalInputError, prepare_multimodal_input


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_image_is_staged_without_media_plugin_and_encoded_for_harness(tmp_path: Path) -> None:
    source = tmp_path / "sample.png"
    source.write_bytes(PNG_1X1)

    prepared = prepare_multimodal_input(
        [source],
        message="分析图片",
        conversation_id="conversation-test",
        output_root=tmp_path / "output",
        registry_path=tmp_path / "sessions.json",
        plugin_root=tmp_path / "plugins",
    )

    assert prepared.media_context is None
    assert prepared.attachments[0].modality == "image"
    assert Path(prepared.attachments[0].path).read_bytes() == PNG_1X1
    blocks = _content_blocks("prompt", prepared.attachments)
    assert blocks[0] == {"type": "text", "text": "prompt"}
    assert blocks[1]["type"] == "image"
    assert base64.b64decode(blocks[1]["data"]) == PNG_1X1


def test_unsupported_attachment_is_rejected_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("no", encoding="utf-8")

    with pytest.raises(MultimodalInputError, match="不支持"):
        prepare_multimodal_input(
            [source],
            message="分析",
            conversation_id="conversation-test",
            output_root=tmp_path / "output",
            registry_path=tmp_path / "sessions.json",
            plugin_root=tmp_path / "plugins",
        )


def test_dynamic_media_plan_does_not_require_browser_session(tmp_path: Path) -> None:
    image = tmp_path / "sample.png"
    image.write_bytes(PNG_1X1)
    plan = DynamicAgentPlan(
        objective="分析图片",
        summary="分析本地图片",
        steps=["识别图片内容", "给出摘要"],
        attachments=[
            AgentAttachment(
                path=str(image),
                display_name=image.name,
                media_type="image/png",
                modality="image",
                size_bytes=image.stat().st_size,
            )
        ],
    )

    assert plan.platform is None
    assert plan.session_ref is None
