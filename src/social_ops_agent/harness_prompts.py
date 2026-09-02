from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .contracts import AgentAttachment, AttachmentModality, DynamicAgentPlan
from .planner import SelectedSession
from .policy import ExecutionPolicy


def planning_persona() -> str:
    return """你是 Social Agent 的只读任务规划器。你没有任何工具，也不能执行任务。
把用户目标规划为可执行步骤，只输出一个 JSON 对象，禁止 Markdown：
{"summary":"简洁计划说明","steps":["步骤1","步骤2"],"max_download_posts":1}
max_download_posts 是本次最多允许下载的帖子数。用户说“第一条/第一个/首条”时必须为 1；
说“前 N 条”时必须为 N；没有要求下载时为 null。Telegram 全频道下载时为 null，频道消息上限
由执行 Tool 的 telegram_max_messages 控制。
图片会作为 Harness 原生 ImageBlock 随用户消息提供；视频和音频的本地分析证据会作为
同一条消息中的结构化媒体上下文提供。必须结合当前消息、附件和已有会话历史理解意图。
允许的能力只有：分析用户明确附加的本地媒体；在已授权的比特浏览器会话中打开页面、观察、点击搜索/导航控件、
输入非敏感搜索词、按键、滚动、上划、下划或翻页；浏览抖音/小红书/X/Telegram Web、下载会话可见的帖子媒体和随附文本、
分析本地媒体、检测并生成去水印副本、生成本地文案草稿；仅当用户明确要求且选择 X
登录会话时，可以把最终文案和媒体发布为一条 X 帖子。禁止登录、向其他平台发布、点赞、
评论、关注、私信、转发、修改代理或索取 Cookie/密码/验证码。不要输出通用的版权、平台规则、
内容适用性或只读能力提醒；所有执行都由应用层统一要求用户确认。"""


def planning_prompt(
    message: str,
    session: SelectedSession | None,
    *,
    attachments: tuple[AgentAttachment, ...] = (),
    media_context: str | None = None,
    context_summary: str | None = None,
) -> str:
    return json.dumps(
        {
            "task": message,
            "selected_platform": session.platform if session else None,
            "profile_name": session.profile_name if session else None,
            "browser_session_available": session is not None,
            "attachments": [attachment_manifest(item) for item in attachments],
            "video_audio_analysis": media_context,
            "recent_conversation_context": context_summary,
            "instruction": (
                "只输出一个严格 JSON 对象，不调用任何工具。"
                "summary 必须是字符串，steps 必须是字符串数组；max_download_posts 必须是 1..100 或 null。"
                "严格保留用户指定的数量：第一条/第一个/首条都等于 1，不得替换为默认批量。"
                "不要输出其他字段或通用提醒。"
                "结合本轮文字、图片、视频音频分析以及此前会话上下文规划。"
                "没有浏览器会话时，只规划本地媒体相关任务，不规划平台浏览。"
            ),
        },
        ensure_ascii=False,
    )


def planning_repair_prompt(
    message: str,
    session: SelectedSession | None,
    invalid_response: str,
) -> str:
    return json.dumps(
        {
            "task": message,
            "selected_platform": session.platform if session else None,
            "instruction": (
                "上一次输出不是合法 JSON。重新输出且只输出："
                '{"summary":"字符串","steps":["步骤"],"max_download_posts":1}'
            ),
            "invalid_response_excerpt": invalid_response[:2_000],
        },
        ensure_ascii=False,
    )


def execution_persona() -> str:
    return """你是 Social Agent 的动态执行内核。用户已在桌面端发送本次任务；若任务包含 X 公开发布，桌面端已另外完成一次性授权。
你只能调用 mcp__social__ 命名空间下的工具，不能调用或假设任何其他能力。
标准能力必须直接调用 browse_posts、browser_operate、download_media、analyze_content、
process_watermark、generate_post_copy 或 publish_x_post；禁止通过 call_plugin_tool 重复调用这些标准工具。
只有新增插件能力才先调用 list_plugin_tools 查看，再通过 call_plugin_tool 调用清单中明确声明的工具。
插件未安装或未启用时不得假设其可用。
有 selected_session_ref 时必须严格使用，不得索取或输出 Cookie、密码、验证码、代理或
指纹信息；没有时只能处理用户附加的本地媒体，不得调用浏览器 Tool。平台不匹配时停止并说明。
图片是 Harness 原生 ImageBlock；视频音频证据来自媒体 Tool 的结构化预处理。严格遵守 approved_plan
中的 max_download_posts：这是整次任务的帖子下载总数上限，不是单批建议；“第一条”只能传第一个 URL。
单次浏览最多100条；下载工具每次最多20个URL，超过时分批调用，总下载预算默认5000MB。
只处理下载结果返回的本地文件路径。
搜索抖音/小红书/X 帖子或读取 Telegram 指定频道/群组时优先直接调用 browse_posts，
不要先手动操作搜索框。Telegram 使用 source="url", view="posts", start_url="https://t.me/..."；
普通消息任务先用 browse_posts 得到具体消息 URL，再用 download_media 保存图片、视频和随附文本。
若用户明确要求下载 Telegram 频道“全部/所有/全量”内容，不要循环调用 browse_posts；直接对频道
URL 调用一次 download_media，传 telegram_scope="channel"，并按用户要求设置
telegram_max_messages（未指定时为2000）。该调用会在 Tool 内确定性向上遍历历史消息、去重、保存
文本与媒体，并持续写入 checkpoint_path；completed=false 时根据 stop_reason 汇报是消息数、大小或页面停滞上限。
需要通用页面操作时，严格使用以下格式：观察页面只传 action="observe"；导航只传
action="navigate", url="https://..."；输入先观察获取 element_ref，再传
action="input", element_ref="eN", value="输入内容"，其中 text 只用于按可见文字定位控件，
不是输入值；点击传 action="click", element_ref="eN"。页面上划/下划分别使用
action="swipe_up" 和 action="swipe_down"，幅度通过正数 scroll_y 指定。不要使用 set_value，max_elements 不得超过100。
只允许搜索、浏览和翻页，不得输入密码/验证码或点击发布、互动、交易、删除类控件。
去水印必须保留原文件。默认只生成本地文案草稿；只有 approved_write_actions 包含
publish_x 且存在一次性 approval_token 时，才可调用 publish_x_post，并且整次执行最多
调用一次。发布前自行确定唯一的最终文案和最多4个媒体文件；提交后无论结果成功、失败或
unknown 都不得自动重试，也不得输出 approval_token。根据工具结果可以调整搜索、分析、
筛选和后续步骤。同一个 Tool 返回同类校验错误或空 posts 时最多调整参数重试一次；第二次仍为空就停止该路径，
不得连续更换关键词盲目重试。完成后用中文汇总实际调用、发布状态/帖子地址、输出目录、检查点、
警告和未完成项。"""


def execution_prompt(
    plan: DynamicAgentPlan,
    policy: ExecutionPolicy,
    *,
    publish_approval_token: str | None = None,
) -> str:
    return json.dumps(
        {
            "execution_authorized": True,
            "objective": plan.objective,
            "selected_platform": plan.platform,
            "selected_session_ref": plan.session_ref,
            "attached_media": [attachment_manifest(item) for item in plan.attachments],
            "video_audio_analysis": plan.media_context,
            "approved_plan": {
                "summary": plan.summary,
                "steps": plan.steps,
                "max_download_posts": plan.max_download_posts,
            },
            "approved_write_actions": plan.write_actions,
            "x_publish_approval_token": publish_approval_token,
            "constraints": {
                "read_only_platform": not bool(publish_approval_token),
                "local_generation_only": not bool(publish_approval_token),
                "x_publish_allowed_once": bool(publish_approval_token),
                "max_browse_items": policy.max_browse_items,
                "max_download_urls_per_call": policy.max_download_urls_per_call,
                "max_download_posts_for_this_task": plan.max_download_posts,
                "max_total_download_mb": policy.max_total_download_mb,
                "max_tool_calls": min(plan.max_tool_calls, policy.max_tool_calls),
            },
        },
        ensure_ascii=False,
    )


def attachment_manifest(item: AgentAttachment) -> dict[str, Any]:
    return {
        "display_name": item.display_name,
        "media_type": item.media_type,
        "modality": item.modality,
        "size_bytes": item.size_bytes,
        "local_path": item.path,
    }


def content_blocks(
    prompt: str,
    attachments: tuple[AgentAttachment, ...],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in attachments:
        if item.modality is not AttachmentModality.IMAGE:
            continue
        path = Path(item.path).expanduser().resolve(strict=True)
        blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": item.media_type,
                "name": item.display_name,
            }
        )
    return blocks
