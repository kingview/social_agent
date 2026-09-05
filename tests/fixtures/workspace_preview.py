"""Isolated visual fixture: never calls a model, plugin, or a user browser."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from social_ops_agent.conversation_controller import ConversationPhase
from social_ops_agent.conversation_workspace import ConversationWorkspace
from social_ops_agent.desktop_support import STYLESHEET
from social_ops_agent.settings import LLMSettingsStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tabs", type=int, default=3)
    parser.add_argument("--width", type=int, default=1180)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args()
    app = QApplication([])
    app.setApplicationName("SocialAgent UI Preview")
    app.setStyleSheet(STYLESHEET)
    with TemporaryDirectory(prefix="social-agent-topbar-preview-") as directory:
        root = Path(directory)
        window = ConversationWorkspace(output_root=root, plugin_root=root / "plugins",
            registry_path=root / "registry.json", llm_settings_store=LLMSettingsStore(root / "llm.json"))
        for i in range(args.tabs):
            pane = window.tabs.widget(0) if i == 0 else window.open_conversation(create_new=True)
            if i == 0:
                task = pane.controller.conversation.begin_turn("小红书搜索 microduck")
                pane.controller.conversation.mark_failed(task, stage="planning", error="界面测试，不执行任务")
                pane.controller.phase = ConversationPhase.EXECUTING
                pane.controller.percent = 40
            elif i == 1:
                pane.controller.phase = ConversationPhase.WAITING
        window.refresh()
        window.setWindowTitle("SocialAgent · 顶栏隔离验证")
        window.resize(args.width, 840)
        window.tabs.setCurrentIndex(0)
        window.show()
        app.processEvents()
        bar, header = window.tabs.tabBar(), window.tabs.toolbar
        print(json.dumps({"scale": window.devicePixelRatioF(), "width": window.width(),
            "header_height": header.height(), "bar_height": bar.height(),
            "button_height": window.new_button.height(), "first_tab_x": bar.tabRect(0).x()}), flush=True)
        if args.screenshot:
            assert window.grab().save(str(args.screenshot))
        if args.native:
            QTimer.singleShot(120000, app.quit)
            app.exec()
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
