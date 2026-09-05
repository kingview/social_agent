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
    background = "#fff0f1" if error else ("#eaf0ff" if side == "right" else "#ffffff")
    border = "#efbcc5" if error else ("#d8e2fc" if side == "right" else "#e3e8f1")
    label_color = "#c64760" if error else ("#2859ed" if side == "left" else "#617398")
    bubble = (
        f'<td width="72%" bgcolor="{background}" '
        f'style="border:1px solid {border}; padding:10px 12px;">'
        f'<div align="left"><span style="color:{label_color}; font-weight:700;">'
        f'{_html(label)}</span><br><span style="color:#34415c;">{body_html}</span></div></td>'
    )
    spacer = '<td width="28%"></td>'
    cells = f"{spacer}{bubble}" if side == "right" else f"{bubble}{spacer}"
    return (
        '<table width="100%" cellspacing="0" cellpadding="0" '
        f'data-message-side="{side}"><tr>{cells}</tr></table><br>'
    )


from .workspace_theme import WORKSPACE_STYLE

STYLESHEET = WORKSPACE_STYLE


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"
