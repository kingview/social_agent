"""Desktop entry point and compatibility exports; conversation logic lives in its controller."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from .diagnostics import install_exception_hooks
from .conversation_pane import ConversationPane, APP_NAME, AUTO_SESSION_REF
from .conversation_workers import PlanWorker, ExecutionWorker
from .desktop_support import default_output_root, STYLESHEET, _chat_message_html
from .plugins import PluginInvoker

# Existing launchers can still construct a standalone view.
MainWindow = ConversationPane


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Social Agent desktop client")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    install_exception_hooks("agent", (args.output_root or default_output_root()) / ".social-agent-state")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLESHEET)
    from .conversation_workspace import ConversationWorkspace
    try:
        window = ConversationWorkspace(output_root=args.output_root)
    except RuntimeError as exc:
        QMessageBox.information(None, APP_NAME, str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
