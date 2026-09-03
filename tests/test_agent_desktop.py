from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from social_ops_agent.desktop import AUTO_SESSION_REF, ExecutionWorker, MainWindow, _chat_message_html
from social_ops_agent.contracts import (
    AgentExecutionResult,
    AgentPlan,
    AgentProgress,
    BrowserSessionBinding,
    DynamicAgentPlan,
)
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
    assert window.session_combo.itemText(0) == "根据任务自动选择窗口"
    assert window.session_combo.currentData() == AUTO_SESSION_REF
    assert window.session_combo.itemText(1).startswith("抖音")
    assert window.manage_sessions_button.text() == "管理浏览器窗口"
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
    window.execute_plan = lambda: executions.append(window.controller.pending_plan)  # type: ignore[method-assign]
    plan = AgentPlan(
        objective="搜索帖子",
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        source="search",
        query="web3",
    )
    window.controller.accept_plan(plan)
    assert executions == [plan]
    window.close()
    app.processEvents()


@pytest.mark.parametrize("publish,legacy_confirmation", [(True, False), (True, True), (False, False)])
def test_plan_starts_without_extra_publish_confirmation(
    tmp_path, monkeypatch, publish, legacy_confirmation
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=tmp_path / "sessions.json",
        output_root=tmp_path / "downloads",
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(
            tmp_path / "llm.json", credentials=MemoryCredentials()
        ),
    )
    started = []
    # Exercise the GUI handoff, but never start Harness, browser work or publishing.
    monkeypatch.setattr(ExecutionWorker, "start", lambda worker: started.append(worker))

    def unexpected_dialog(*_args, **_kwargs):
        pytest.fail("Sending a task must not open an extra publish confirmation")

    monkeypatch.setattr("social_ops_agent.desktop.QMessageBox.warning", unexpected_dialog)
    monkeypatch.setattr("social_ops_agent.desktop.QMessageBox.question", unexpected_dialog)
    session_ref = "sess_x_abcdefghijklmnopqrstuvwx"
    objective = "生成文案并发到 X" if publish else "生成本地文案草稿"
    plan = DynamicAgentPlan(
        objective=objective,
        platform="x",
        session_ref=session_ref,
        browser_sessions=[BrowserSessionBinding(
            platform="x", session_ref=session_ref, profile_name="测试 X 窗口"
        )],
        summary=objective,
        steps=["生成文案", "发布到 X"] if publish else ["生成文案"],
        write_actions=["publish_x"] if publish else [],
        requires_confirmation=legacy_confirmation,
    )
    turn_id = window.controller.conversation.begin_turn(plan.objective)
    window.controller.active_turn_id = turn_id
    try:
        window.controller.accept_plan(plan)
        assert window.controller.pending_plan is None
        turn = window.controller.conversation.turns[-1]
        persisted = json.loads(window.controller.conversation.path.read_text())["turns"][-1]
        assert len(started) == 1
        assert started[0]._plan == plan
        assert window.controller.execution_worker is started[0]
        assert turn.status == persisted["status"] == "executing"
        assert window.controller.active_turn_id == turn_id
        assert "已取消本次 X 发布" not in window.chat.toPlainText()
        assert "开始执行" in window.chat.toPlainText()
        # Duplicate callbacks must not start a second execution.
        window.execute_plan()
        assert len(started) == 1
    finally:
        window.controller.finish_execution()
        window.controller.active_turn_id = None  # Isolated fixture never ran the worker.
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

    assert window.controller.conversation_id == conversation_id
    assert window.session_combo.currentData() == AUTO_SESSION_REF
    assert "第一条已经下载" in window.chat.toPlainText()
    window.close()
    app.processEvents()


def test_execution_progress_is_rendered_in_agent_chat_by_completed_steps(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=tmp_path / "sessions.json",
        output_root=tmp_path / "downloads",
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(
            tmp_path / "llm.json", credentials=MemoryCredentials()
        ),
    )

    window.controller.report_execution(
        AgentProgress(
            stage="step",
            completed=2,
            total=5,
            message="第 3/5 步：分析内容（正在分析图片、视频和文本）",
        )
    )

    assert window.progress_bar.value() == 40
    assert window.progress_value.text() == "40%"
    assert "总进度 40%" in window.chat.toPlainText()
    assert "第 3/5 步" in window.chat.toPlainText()
    window.close()
    app.processEvents()


def test_gui_persists_partial_completion_and_publish_attempt(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=tmp_path / "sessions.json", output_root=tmp_path / "downloads",
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(tmp_path / "llm.json", credentials=MemoryCredentials()),
    )
    turn_id = window.controller.conversation.begin_turn("继续并发布到X")
    window.controller.active_turn_id = turn_id
    window.controller.report_execution(AgentProgress(stage="publishing", completed=4, total=5, message="发布中"))
    # Display-only progress cannot create an authoritative publication marker.
    assert json.loads(window.controller.conversation.path.read_text())["turns"][-1]["publish_attempted"] is False
    window.controller.accept_result(AgentExecutionResult(
        runtime="deepseek_harness",
        plan=DynamicAgentPlan(objective="发布", summary="发布", steps=["发布"]),
        summary="任务未全部完成：X 发布结果不明", completion_status="partial",
        completed_steps=4, total_steps=5, publish_state="unknown",
    ))
    persisted = json.loads(window.controller.conversation.path.read_text())["turns"][-1]
    assert persisted["status"] == "partial"
    assert persisted["result"]["publish_state"] == "unknown"
    assert window.progress_bar.value() == 80
    assert "任务未全部完成" in window.chat.toPlainText()
    window.close()
    app.processEvents()


def test_registration_button_waits_for_child_window_ready(tmp_path, monkeypatch) -> None:
    import time

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=tmp_path / "sessions.json",
        output_root=tmp_path / "downloads",
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(tmp_path / "llm.json", credentials=MemoryCredentials()),
    )

    class FakeProcess:
        pid = 12345
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()
    launches = []

    def launch_gui(_self, _plugin_id, _args, *, ready_file):
        launches.append(ready_file)
        return process

    monkeypatch.setattr("social_ops_agent.desktop.PluginInvoker.launch_gui", launch_gui)
    window.manage_sessions()
    assert window.manage_sessions_button.text() == "正在打开管理窗口…"
    assert not window.manage_sessions_button.isEnabled()
    window.manage_sessions()
    assert len(launches) == 1
    window._poll_session_manager()
    assert window.manage_sessions_button.text() == "正在打开管理窗口…"

    # Another process's marker must not claim this window is ready.
    launches[0].write_text("99999", encoding="utf-8")
    window._session_manager_started_at = time.monotonic() - 16
    window._poll_session_manager()
    assert window.manage_sessions_button.text() == "管理窗口启动较慢，请稍候…"
    launches[0].write_text(str(process.pid), encoding="utf-8")
    window._poll_session_manager()
    assert window.manage_sessions_button.text() == "管理窗口已打开"
    window._set_planning(False)
    assert not window.manage_sessions_button.isEnabled()

    process.returncode = 0
    window._poll_session_manager()
    assert window.manage_sessions_button.text() == "管理浏览器窗口"
    assert window.manage_sessions_button.isEnabled()
    assert not launches[0].parent.exists()
    assert window._session_manager_process is None
    window.close()
    app.processEvents()


def test_registration_launch_failure_restores_button(tmp_path, monkeypatch) -> None:
    from social_ops_agent.plugins import PluginError

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        registry_path=tmp_path / "sessions.json",
        output_root=tmp_path / "downloads",
        plugin_root=tmp_path / "plugins",
        llm_settings_store=LLMSettingsStore(tmp_path / "llm.json", credentials=MemoryCredentials()),
    )

    def launch_gui(*_args, **_kwargs):
        raise PluginError("test launch failure")

    messages = []
    monkeypatch.setattr("social_ops_agent.desktop.PluginInvoker.launch_gui", launch_gui)
    monkeypatch.setattr("social_ops_agent.desktop.QMessageBox.information", lambda *args: messages.append(args))
    window.manage_sessions()
    assert messages
    assert window.manage_sessions_button.text() == "管理浏览器窗口"
    assert window.manage_sessions_button.isEnabled()
    assert window._session_manager_ready_dir is None
    assert not window._session_manager_timer.isActive()
    window.close()
    app.processEvents()
