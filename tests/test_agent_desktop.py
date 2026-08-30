from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from social_ops_agent.desktop import MainWindow
from social_ops_agent.model_settings_dialog import ModelSettingsDialog
from social_ops_agent.settings import LLMSettings, LLMSettingsStore


class MemoryCredentials:
    def get(self, account: str) -> str | None:
        return None

    def set(self, account: str, value: str) -> None:
        pass

    def delete(self, account: str) -> None:
        pass


def test_desktop_has_conversation_and_confirmation_controls(tmp_path: Path) -> None:
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
    assert window.plan_button.text().startswith("生成计划")
    assert window.execute_button.text() == "确认并执行计划"
    assert not window.execute_button.isEnabled()
    assert window.plugins_button.text() == "Tool 插件 · 0"
    assert window.model_button.text() == "LLM · 本地 Ollama · qwen3.5:9b"
    assert "图片 / 视频 / 音频" in window.attach_button.text()
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
