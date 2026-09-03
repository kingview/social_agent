from pathlib import Path
from PySide6.QtCore import QStandardPaths

def default_output_root() -> Path:
    downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    base = Path(downloads) if downloads else Path.home() / "Downloads"
    return base / "SocialAgent"


def _html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _chat_message_html(
    label: str,
    body_html: str,
    *,
    side: str,
    error: bool = False,
) -> str:
    """Render a Qt-rich-text chat row with a stable left/right column."""
    if side not in {"left", "right"}:
        raise ValueError("chat message side must be left or right")
    background = "#362126" if error else ("#28303b" if side == "right" else "#1b1e25")
    border = "#7c3e49" if error else ("#465365" if side == "right" else "#343945")
    label_color = "#ff9ca8" if error else ("#d8ff52" if side == "left" else "#b8c7dc")
    bubble = (
        f'<td width="72%" bgcolor="{background}" '
        f'style="border:1px solid {border}; padding:10px 12px;">'
        f'<div align="left"><span style="color:{label_color}; font-weight:700;">'
        f'{_html(label)}</span><br><span style="color:#eef0f3;">{body_html}</span></div></td>'
    )
    spacer = '<td width="28%"></td>'
    cells = f"{spacer}{bubble}" if side == "right" else f"{bubble}{spacer}"
    return (
        '<table width="100%" cellspacing="0" cellpadding="0" '
        f'data-message-side="{side}"><tr>{cells}</tr></table><br>'
    )


STYLESHEET = """
QMainWindow#conversationWorkspace { background: #111318; }
QTabWidget#conversationTabs, QTabWidget#conversationTabs QTabBar, QWidget#conversationControls { background: #111318; }
QTabWidget#conversationTabs::pane { border: none; background: #111318; }
QTabWidget#conversationTabs QTabBar::tab { background: #242832; color: #cfd2d8; padding: 12px 16px; min-width: 140px; }
QTabWidget#conversationTabs QTabBar::tab:selected { background: #111318; color: #d8ff52; }
QWidget#root { background: #111318; color: #f1f2f4; }
QLabel#eyebrow { color: #d8ff52; font-size: 11px; font-weight: 800; letter-spacing: 2px; }
QLabel#title { font-size: 34px; font-weight: 800; color: #f1f2f4; }
QLabel#subtitle, QLabel#hint { color: #9297a3; }
QLabel#fieldLabel, QLabel#progressLabel, QLabel#progressValue { color: #cfd2d8; font-weight: 700; }
QFrame#card, QFrame#inputFrame, QFrame#progressFrame { background: #1b1e25; border: 1px solid #30343e; border-radius: 12px; }
QTextBrowser#chat { background: #15171d; border: 1px solid #30343e; border-radius: 14px; padding: 14px; color: #e7e8eb; font-size: 14px; }
QPlainTextEdit#messageInput { background: transparent; border: none; color: #f4f5f6; font-size: 15px; }
QListWidget#attachmentList { background: #14171d; border: 1px solid #343945; border-radius: 8px; color: #dfe2e8; padding: 5px; }
QComboBox#control, QLineEdit#control { min-height: 44px; max-height: 44px; padding: 0 12px; background: #111318; border: 1px solid #383d48; border-radius: 8px; color: #e9eaed; font-size: 14px; }
QComboBox#control QLineEdit { min-height: 40px; max-height: 40px; padding: 0; background: transparent; border: none; color: #e9eaed; }
QComboBox#control::drop-down { width: 42px; border: none; }
QComboBox#control:disabled, QLineEdit#control:disabled { background: #17191f; color: #686d77; }
QListView#comboPopup { background: #1b1e25; color: #e9eaed; border: 1px solid #454b58; border-radius: 8px; padding: 4px 0; outline: none; }
QListView#comboPopup::item { min-height: 42px; padding: 0 14px; }
QListView#comboPopup::item:hover { background: #303641; }
QListView#comboPopup::item:selected { background: #3a4250; color: #d8ff52; }
QPushButton { min-height: 38px; padding: 0 15px; border-radius: 9px; font-weight: 700; }
QPushButton#primaryButton { background: #d8ff52; color: #15170c; border: none; }
QPushButton#secondaryButton { background: #242832; color: #d8dae0; border: 1px solid #3a3f4b; }
QPushButton:disabled { background: #292c33; color: #686d77; }
QProgressBar { min-height: 7px; max-height: 7px; border: none; border-radius: 3px; background: #30343d; }
QProgressBar::chunk { background: #d8ff52; border-radius: 3px; }
"""


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"
