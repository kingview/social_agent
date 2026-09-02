from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from social_ops_agent.desktop import MainWindow, _chat_message_html
from social_ops_agent.contracts import AgentExecutionResult, AgentPlan, DynamicAgentPlan
from social_ops_agent.conversation import ConversationCoordinator
from social_ops_agent.model_settings_dialog import ModelSettingsDialog
from social_ops_agent.settings import LLMSettings, LLMSettingsStore


class MemoryCredentials:
    def get(self, account: str) -> str | None:
        return None

    def set(self, account: str, value: str) -> None:
        pass

    def delete(self, account: str) -> None:
        pass


class StartupDenyCredentials(MemoryCredentials):
    def get(self, account: str) -> str | None:
        raise AssertionError(f"credential store must not be read during startup: {account}")


def test_chat_messages_render_agent_left_and_user_right() -> None:
    agent = _chat_message_html("Agent", "执行完成", side="left")
    user = _chat_message_html("你", "下载第一条", side="right")

    assert 'data-message-side="left"' in agent
    assert agent.index('width="72%"') < agent.index('width="28%"')
    assert 'data-message-side="right"' in user
    assert user.index('width="28%"') < user.index('width="72%"')


def test_desktop_has_single_send_control(tmp_path: Path) -> None:
    registry = tmp_path / "sessions.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": [
                    {
                        "session_ref": "sess_douyin_abcdefghijklmnopqrstuvwx",
                        "platform": "douyin",
                        "provider": "bitbrowser",
                        "profile_id": "profile-1",
                        "profile_name": "抖音账号 01",
                        "api_url": "http://127.0.0.1:54345",
                        "created_at": "2026-08-23T00:00:00+00:00",
                        "updated_at": "2026-08-23T00:00:00+00:00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=registry,
        output_root=tmp_path / "downloads",
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(
            tmp_path / "llm.json", credentials=MemoryCredentials()
        ),
    )

    assert "社媒任务助手" in window.windowTitle()
    assert window.session_combo.itemText(0).startswith("抖音")
    assert window.send_button.text() == "发送"
    assert window.send_button.isEnabled()
    assert not hasattr(window, "execute_button")
    assert window.plugins_button.text() == "Tool 插件 · 0"
    assert window.model_button.text() == "LLM · 本地 Ollama · qwen3.5:9b"
    assert "图片 / 视频 / 音频" in window.attach_button.text()

    window.session_combo.addItems(["小红书账号 02", "X 账号 03", "Telegram 账号 04"])
    window.session_combo.show()
    window.session_combo.showPopup()
    app.processEvents()
    assert window.session_combo.view().height() >= 4 * 42
    window.session_combo.hidePopup()

    executions = []
    window.execute_plan = lambda: executions.append(window._pending_plan)  # type: ignore[method-assign]
    plan = AgentPlan(
        objective="搜索帖子",
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        source="search",
        query="web3",
    )
    window._plan_succeeded(plan)
    assert executions == [plan]
    window.close()
    app.processEvents()


def test_model_settings_fields_are_equal_width_and_height(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    store = LLMSettingsStore(tmp_path / "llm.json", credentials=MemoryCredentials())
    dialog = ModelSettingsDialog(store, LLMSettings.from_env())
    dialog.show()
    app.processEvents()

    controls = (
        dialog.provider_combo,
        dialog.endpoint_input,
        dialog.model_combo,
        dialog.key_input,
    )
    assert {control.width() for control in controls} == {controls[0].width()}
    assert {control.height() for control in controls} == {46}
    dialog.provider_combo.showPopup()
    app.processEvents()
    assert dialog.provider_combo.view().height() >= 3 * 42
    dialog.provider_combo.hidePopup()
    dialog.close()
    app.processEvents()


def test_desktop_startup_does_not_read_remote_api_key(tmp_path: Path) -> None:
    settings_path = tmp_path / "llm.json"
    settings_path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-5.4-mini",
            }
        ),
        encoding="utf-8",
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=tmp_path / "sessions.json",
        output_root=tmp_path / "downloads",
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(
            settings_path,
            credentials=StartupDenyCredentials(),
        ),
    )

    assert window.model_button.text() == "LLM · OpenAI API · gpt-5.4-mini"
    window.close()
    app.processEvents()


def test_desktop_restores_active_conversation_and_last_session(tmp_path: Path) -> None:
    registry = tmp_path / "sessions.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": [
                    {
                        "session_ref": "sess_xhs_abcdefghijklmnopqrstuvwx",
                        "platform": "xiaohongshu",
                        "provider": "bitbrowser",
                        "profile_id": "profile-xhs",
                        "profile_name": "小红书账号",
                        "api_url": "http://127.0.0.1:54345",
                        "created_at": "2026-08-23T00:00:00+00:00",
                        "updated_at": "2026-08-23T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "downloads"
    conversation = ConversationCoordinator(output_root / ".social-agent-state")
    conversation_id = conversation.conversation_id
    turn_id = conversation.begin_turn(
        "下载第一条",
        session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
        platform="xiaohongshu",
    )
    plan = DynamicAgentPlan(
        objective="下载第一条",
        platform="xiaohongshu",
        session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
        summary="下载第一条",
        steps=["下载"],
        max_download_posts=1,
    )
    conversation.mark_planned(turn_id, plan)
    conversation.mark_succeeded(
        turn_id,
        AgentExecutionResult(
            runtime="deepseek_harness",
            plan=plan,
            summary="第一条已经下载",
        ),
    )

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=registry,
        output_root=output_root,
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(
            tmp_path / "llm.json", credentials=MemoryCredentials()
        ),
    )

    assert window._conversation_id == conversation_id
    assert window.session_combo.currentData() == "sess_xhs_abcdefghijklmnopqrstuvwx"
    assert "第一条已经下载" in window.chat.toPlainText()
    window.close()
    app.processEvents()
