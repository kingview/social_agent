from __future__ import annotations

import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .agent_runtime import RuntimePlan
from .diagnostics import record_exception
from .conversation_controller import ConversationController
from .desktop_support import default_output_root, _html, _human_size, _chat_message_html
from .contracts import AgentExecutionResult, AgentProgress, DynamicAgentPlan
from .model_settings_dialog import ModelSettingsDialog, UniformComboBox
from .multimodal import SUPPORTED_SUFFIXES
from .plugin_desktop import PluginManagerDialog
from .plugins import PluginError, PluginInvoker, PluginManager
from .planner import SelectedSession
from .session_store import SessionRecord, SessionStore, default_session_registry_path
from .settings import LLMSettings, LLMSettingsError, LLMSettingsStore


APP_NAME = "Social Agent"
AUTO_SESSION_REF = "__auto_browser_session__"
PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "x": "X / Twitter",
    "telegram": "Telegram Web",
}


class ConversationPane(QWidget):
    new_conversation_requested = Signal()
    conversation_changed = Signal()

    def __init__(
        self,
        *,
        registry_path: Path | None = None,
        output_root: Path | None = None,
        plugin_root: Path | None = None,
        llm_settings_store: LLMSettingsStore | None = None,
        conversation_id: str | None = None,
        create_new: bool = False,
        managed: bool = False,
    ) -> None:
        super().__init__()
        self._managed = managed
        self._global_busy = False
        self._registry_path = (registry_path or default_session_registry_path()).expanduser().resolve()
        self._registry = SessionStore(self._registry_path)
        self._output_root = (output_root or default_output_root()).expanduser().resolve()
        self._plugin_manager = PluginManager(plugin_root)
        self._llm_settings_store = llm_settings_store or LLMSettingsStore()
        self._settings_load_error: str | None = None
        try:
            self._llm_settings = self._llm_settings_store.load_metadata()
        except LLMSettingsError as exc:
            record_exception("agent", "gui.settings_load", exc)
            self._llm_settings = LLMSettings.from_env()
            self._settings_load_error = str(exc)
        self.controller = ConversationController(output_root=self._output_root,
            registry_path=self._registry_path, plugin_root=self._plugin_manager.root,
            settings=self._llm_settings, conversation_id=conversation_id, create_new=create_new,
            parent=self)
        self._last_progress_message = ""
        self._attachment_paths: list[Path] = []
        self._session_manager_process = None
        self._session_manager_ready_dir = None
        self._session_manager_started_at = 0.0
        self._session_manager_timer = QTimer(self)
        self._session_manager_timer.setInterval(500)
        self._session_manager_timer.timeout.connect(self._poll_session_manager)

        self.setWindowTitle("Social Agent · 社媒任务助手")
        self.resize(1_020, 760)
        self.setMinimumSize(780, 620)
        self.setAcceptDrops(True)
        self._build_ui()
        self.controller.changed.connect(self.conversation_changed)
        self.controller.plan_ready.connect(self._plan_succeeded)
        self.controller.planning_status.connect(self._planning_status)
        self.controller.planning_failed.connect(self._plan_failed)
        self.controller.planning_finished.connect(self._planning_finished)
        self.controller.execution_started.connect(self._execution_started)
        self.controller.execution_progress.connect(self._execution_progress)
        self.controller.execution_succeeded.connect(self._execution_succeeded)
        self.controller.execution_failed.connect(self._execution_failed)
        self.controller.execution_finished.connect(self._execution_finished)
        self._refresh_plugin_state()

    def _build_ui(self) -> None:
        self.setObjectName("root")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        eyebrow = QLabel("LOCAL TOOL ORCHESTRATOR")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Social Agent")
        title.setObjectName("title")
        subtitle = QLabel("所有自然语言命令由 DeepSeek Harness 理解并编排；发送后自动执行。")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.new_chat_button = QPushButton("新会话")
        self.new_chat_button.setObjectName("secondaryButton")
        self.new_chat_button.clicked.connect(self.new_conversation)
        self.new_chat_button.setVisible(not self._managed)
        self.plugins_button = QPushButton("Tool 插件")
        self.plugins_button.setObjectName("secondaryButton")
        self.plugins_button.clicked.connect(self.manage_plugins)
        self.model_button = QPushButton()
        self.model_button.setObjectName("secondaryButton")
        self.model_button.clicked.connect(self.manage_model_settings)
        self._refresh_model_button()
        header.addWidget(self.model_button)
        header.addWidget(self.plugins_button)
        header.addWidget(self.new_chat_button)
        layout.addLayout(header)

        session_card = QFrame()
        session_card.setObjectName("card")
        session_layout = QHBoxLayout(session_card)
        session_layout.setContentsMargins(16, 12, 16, 12)
        session_label = QLabel("执行会话")
        session_label.setObjectName("fieldLabel")
        self.session_combo = UniformComboBox()
        self.session_combo.setObjectName("control")
        self.manage_sessions_button = QPushButton("管理浏览器窗口")
        self.manage_sessions_button.setObjectName("secondaryButton")
        self.manage_sessions_button.clicked.connect(self.manage_sessions)
        session_layout.addWidget(session_label)
        session_layout.addWidget(self.session_combo, 1)
        session_layout.addWidget(self.manage_sessions_button)
        layout.addWidget(session_card)

        self.chat = QTextBrowser()
        self.chat.setObjectName("chat")
        self.chat.setOpenExternalLinks(False)
        self.chat.setPlaceholderText("例如：通过关键词“web3”在抖音上搜索并下载前100个帖子")
        layout.addWidget(self.chat, 1)

        self.progress_frame = QFrame()
        self.progress_frame.setObjectName("progressFrame")
        progress_layout = QVBoxLayout(self.progress_frame)
        progress_layout.setContentsMargins(14, 10, 14, 10)
        progress_top = QHBoxLayout()
        self.progress_label = QLabel("等待执行")
        self.progress_label.setObjectName("progressLabel")
        self.progress_value = QLabel("0%")
        self.progress_value.setObjectName("progressValue")
        progress_top.addWidget(self.progress_label)
        progress_top.addStretch()
        progress_top.addWidget(self.progress_value)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        progress_layout.addLayout(progress_top)
        progress_layout.addWidget(self.progress_bar)
        self.progress_frame.hide()
        layout.addWidget(self.progress_frame)

        input_frame = QFrame()
        input_frame.setObjectName("inputFrame")
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(14, 12, 14, 12)
        self.message_input = QPlainTextEdit()
        self.message_input.setObjectName("messageInput")
        self.message_input.setPlaceholderText("描述任务；后续也可以继续补充或调整要求…")
        self.message_input.setMaximumHeight(100)
        input_layout.addWidget(self.message_input)

        self.attachment_list = QListWidget()
        self.attachment_list.setObjectName("attachmentList")
        self.attachment_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.attachment_list.setMaximumHeight(88)
        self.attachment_list.hide()
        input_layout.addWidget(self.attachment_list)

        action_row = QHBoxLayout()
        hint = QLabel("浏览、下载与本地处理 · X 发布须单独确认 · 不自动登录")
        hint.setObjectName("hint")
        self.attach_button = QPushButton("＋ 图片 / 视频 / 音频")
        self.attach_button.setObjectName("secondaryButton")
        self.attach_button.clicked.connect(self.choose_attachments)
        self.remove_attachment_button = QPushButton("移除附件")
        self.remove_attachment_button.setObjectName("secondaryButton")
        self.remove_attachment_button.clicked.connect(self.remove_selected_attachment)
        self.remove_attachment_button.hide()
        self.cancel_button = QPushButton("停止任务")
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.clicked.connect(self.cancel_execution)
        self.cancel_button.hide()
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primaryButton")
        self.send_button.clicked.connect(self.send_message)
        action_row.addWidget(self.attach_button)
        action_row.addWidget(self.remove_attachment_button)
        action_row.addWidget(hint)
        action_row.addStretch()
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.send_button)
        input_layout.addLayout(action_row)
        layout.addWidget(input_frame)

        if self.controller.conversation.turns:
            self._restore_conversation()
        else:
            self._append_agent(
                "可以直接添加图片、视频或音频并描述需求；需要浏览社媒时，Agent 会根据任务自动选择已经在比特浏览器中登录的窗口。"
                "图片使用 Harness 原生多模态；视频音频先提取内容，再与文字和会话历史一起理解。"
            )
        if self._settings_load_error:
            self._append_agent(
                f"模型设置读取失败，已暂时使用环境默认值：{self._settings_load_error}",
                error=True,
            )

    def choose_attachments(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(
            self,
            "添加图片、视频或音频",
            "",
            (
                "媒体文件 (*.png *.jpg *.jpeg *.webp *.gif *.mp4 *.mov *.mkv *.webm "
                "*.avi *.m4v *.mp3 *.m4a *.wav *.flac *.aac *.ogg *.opus)"
            ),
        )
        self._add_attachment_paths([Path(name) for name in names])

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._add_attachment_paths(
            [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        )
        event.acceptProposedAction()

    def remove_selected_attachment(self) -> None:
        row = self.attachment_list.currentRow()
        if 0 <= row < len(self._attachment_paths):
            self._attachment_paths.pop(row)
            self._refresh_attachments()

    def clear_attachments(self) -> None:
        self._attachment_paths.clear()
        self._refresh_attachments()

    def _add_attachment_paths(self, paths: list[Path]) -> None:
        if self.controller.busy:
            return
        known = {path.expanduser().resolve() for path in self._attachment_paths}
        rejected: list[str] = []
        for raw in paths:
            path = raw.expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                rejected.append(path.name)
                continue
            if path not in known:
                self._attachment_paths.append(path)
                known.add(path)
        if len(self._attachment_paths) > 8:
            self._attachment_paths = self._attachment_paths[:8]
            QMessageBox.information(self, "附件数量限制", "每条消息最多添加 8 个附件。")
        if rejected:
            QMessageBox.information(
                self,
                "不支持的附件",
                "以下文件不是支持的图片、视频或音频：\n" + "\n".join(rejected),
            )
        self._refresh_attachments()

    def _refresh_attachments(self) -> None:
        self.attachment_list.clear()
        labels = {"image": "图片", "video": "视频", "audio": "音频"}
        for path in self._attachment_paths:
            suffix = path.suffix.lower()
            kind = (
                "image"
                if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                else "video"
                if suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
                else "audio"
            )
            size = path.stat().st_size if path.exists() else 0
            self.attachment_list.addItem(
                f"{labels[kind]} · {path.name} · {_human_size(size)}"
            )
        visible = bool(self._attachment_paths)
        self.attachment_list.setVisible(visible)
        self.remove_attachment_button.setVisible(visible)

    def send_message(self) -> None:
        if self.controller.busy:
            return
        record = self._selected_record()
        records = self._registry.list()
        message = self.message_input.toPlainText().strip()
        attachments = list(self._attachment_paths)
        if not message and attachments:
            message = "请理解并分析这些附件，根据附件内容完成合适的本地任务。"
        if not message:
            return
        if not records and not attachments:
            QMessageBox.warning(
                self,
                "缺少输入",
                "请添加图片、视频或音频；如需浏览社媒平台，还要先注册登录会话。",
            )
            return
        if not self._ensure_llm_secret():
            return
        self._append_user(message, attachments=attachments)
        self._last_progress_message = ""
        self.message_input.clear()
        self._set_planning(True)
        session = (
            SelectedSession(
                session_ref=record.session_ref,
                platform=record.platform,
                profile_name=record.profile_name,
            )
            if record is not None
            else None
        )
        available_sessions = tuple(
            SelectedSession(
                session_ref=item.session_ref,
                platform=item.platform,
                profile_name=item.profile_name,
            )
            for item in records
        )
        self.controller.start_planning(message, session=session,
            available_sessions=available_sessions, attachment_paths=attachments)

    def _ensure_llm_secret(self) -> bool:
        try:
            hydrated = self._llm_settings_store.with_secret(self._llm_settings)
        except LLMSettingsError as exc:
            record_exception("agent", "gui.settings_secret", exc,
                state_root=self._output_root / ".social-agent-state", conversation_id=self.controller.conversation_id)
            QMessageBox.warning(
                self,
                "无法读取模型密钥",
                f"{exc}\n\n请在顶部“LLM”设置中重新保存 API Key。",
            )
            return False
        if hydrated != self._llm_settings:
            self.controller.configure(hydrated)
            self._llm_settings = hydrated
        return True

    def _plan_succeeded(self, plan: RuntimePlan) -> None:
        self.clear_attachments()
        if isinstance(plan, DynamicAgentPlan):
            selected_sessions = plan.authorized_browser_sessions()
            if selected_sessions:
                selected = "、".join(
                    f"{PLATFORM_LABELS.get(item.platform, item.platform)} · "
                    f"{item.profile_name or item.session_ref}"
                    for item in selected_sessions
                )
                mode = "自动选择" if self.session_combo.currentData() == AUTO_SESSION_REF else "使用"
                self._append_agent(f"已{mode}比特浏览器窗口：{selected}")
            steps = "\n".join(
                f"{index}. {step}" for index, step in enumerate(plan.steps, start=1)
            )
            self._append_agent(f"执行计划（共 {len(plan.steps)} 步）：\n{steps}")
        self.execute_plan()

    def _plan_failed(self, message: str) -> None:
        self._append_agent(f"无法处理消息：{message}", error=True)
        self.conversation_changed.emit()

    def _planning_status(self, message: str) -> None:
        self.progress_label.setText(message)
        self._append_progress_message(message)
        self.conversation_changed.emit()

    def _planning_finished(self) -> None:
        if self.controller.execution_worker is None:
            self._set_planning(False)

    def execute_plan(self) -> None:
        self.controller.start_execution()

    def _execution_started(self) -> None:
        self.send_button.setEnabled(False)
        self.session_combo.setEnabled(False)
        self.manage_sessions_button.setEnabled(False)
        self.plugins_button.setEnabled(False)
        self.model_button.setEnabled(False)
        self.message_input.setEnabled(False)
        self.attach_button.setEnabled(False)
        self.remove_attachment_button.setEnabled(False)
        self.cancel_button.show()
        self.progress_frame.show()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_value.show()
        self._append_agent("开始执行。")

    def cancel_execution(self) -> None:
        if self.controller.cancel():
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("已请求停止；固定下载将在当前批次后停止…")

    def _execution_progress(self, event: AgentProgress) -> None:
        percent = int(event.completed / max(event.total, 1) * 100)
        self.progress_bar.setValue(max(0, min(percent, 100)))
        self.progress_value.setText(f"{percent}%")
        self.progress_label.setText(event.message)
        self._append_progress_message(event.message, percent=percent)
        self.conversation_changed.emit()

    def _execution_succeeded(self, result: AgentExecutionResult) -> None:
        details = result.summary
        if result.tool_calls:
            details += "\nTool：" + "、".join(result.tool_calls)
        if result.output_directories:
            details += f"\n保存目录：{result.output_directories[0]}"
        if result.warnings:
            details += "\n运行信息：" + "；".join(result.warnings)
        self._append_agent(details, error=result.completion_status != "completed")
        self.conversation_changed.emit()

    def _execution_failed(self, message: str) -> None:
        self._append_agent(f"执行失败：{message}", error=True)
        self.conversation_changed.emit()

    def _execution_finished(self) -> None:
        if self.controller.plan_worker is None:
            self._set_planning(False)
        self.cancel_button.hide()
        self.cancel_button.setEnabled(True)

    def manage_sessions(self) -> None:
        if self._global_busy or self.controller.busy:
            return
        if (
            self._session_manager_process is not None
            and self._session_manager_process.poll() is None
        ):
            return
        self.manage_sessions_button.setEnabled(False)
        self.manage_sessions_button.setText("正在打开管理窗口…")
        try:
            self._session_manager_ready_dir = tempfile.TemporaryDirectory(
                prefix="social-agent-gui-ready-"
            )
            invoker = PluginInvoker(
                self._plugin_manager,
                session_registry=self._registry_path,
                output_root=self._output_root,
                state_root=self._output_root / ".social-agent-state",
                llm_base_url=self._llm_settings.base_url,
                llm_model=self._llm_settings.model,
                llm_api_key=self._llm_settings.api_key,
            )
            self._session_manager_process = invoker.launch_gui(
                "com.socialagent.social-content",
                ["--manage-sessions-only"],
                ready_file=Path(self._session_manager_ready_dir.name) / "ready",
            )
        except (PluginError, OSError) as exc:
            record_exception("agent", "gui.session_manager_launch", exc,
                state_root=self._output_root / ".social-agent-state", conversation_id=self.controller.conversation_id,
                plugin_id="com.socialagent.social-content")
            self._reset_session_manager_launch()
            QMessageBox.information(
                self,
                "需要社媒浏览与下载插件",
                f"{exc}\n\n请先点击顶部“Tool 插件”安装社媒浏览与下载插件。",
            )
            return
        self._session_manager_started_at = time.monotonic()
        self._session_manager_timer.start()

    def _poll_session_manager(self) -> None:
        process = self._session_manager_process
        if process is not None and process.poll() is None:
            if self._session_manager_ready_dir is not None:
                ready_path = Path(self._session_manager_ready_dir.name) / "ready"
                try:
                    ready = ready_path.read_text(encoding="utf-8").strip() == str(process.pid)
                except OSError:
                    ready = False
                if ready:
                    self.manage_sessions_button.setText("管理窗口已打开")
                elif time.monotonic() - self._session_manager_started_at >= 15:
                    self.manage_sessions_button.setText("管理窗口启动较慢，请稍候…")
            return
        self._reset_session_manager_launch()
        if process is not None and process.returncode:
            stderr = b""
            try:
                _, stderr = process.communicate(timeout=1)
            except Exception as exc:
                record_exception("agent", "gui.session_manager_stderr", exc,
                    state_root=self._output_root / ".social-agent-state")
            detail = stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 1_200:
                detail = detail[-1_200:]
            error_id = record_exception("agent", "gui.session_manager_exit",
                RuntimeError(f"Session manager exited with code {process.returncode}: {detail}"),
                state_root=self._output_root / ".social-agent-state", conversation_id=self.controller.conversation_id,
                plugin_id="com.socialagent.social-content")
            message = "会话管理界面未能启动。"
            if error_id:
                message += f"\n详细原因已保存到日志。错误编号：{error_id}"
            QMessageBox.critical(self, "无法打开会话管理", message)
        self._refresh_sessions()

    def _reset_session_manager_launch(self) -> None:
        self._session_manager_timer.stop()
        self._session_manager_process = None
        if self._session_manager_ready_dir is not None:
            self._session_manager_ready_dir.cleanup()
            self._session_manager_ready_dir = None
        self.manage_sessions_button.setEnabled(
            not self._global_busy and not self.controller.busy
        )
        self.manage_sessions_button.setText("管理浏览器窗口")

    def manage_plugins(self) -> None:
        if self._global_busy:
            QMessageBox.information(self, "任务处理中", "其他对话正在运行，结束后才能修改共享插件。")
            return
        dialog = PluginManagerDialog(self._plugin_manager, self)
        dialog.plugins_changed.connect(self._refresh_plugin_state)
        dialog.exec()
        self._refresh_plugin_state()

    def manage_model_settings(self) -> None:
        if self.controller.busy:
            return
        dialog = ModelSettingsDialog(
            self._llm_settings_store,
            self._llm_settings,
            self,
        )
        if not dialog.exec() or dialog.selected_settings is None:
            return
        selected = dialog.selected_settings
        self.controller.configure(selected)
        self._llm_settings = selected
        self._refresh_model_button()
        self._append_agent(
            f"模型来源已切换为 {selected.display_name}。后续规划、执行和媒体分析都会使用此配置。"
        )

    def _refresh_model_button(self) -> None:
        self.model_button.setText(f"LLM · {self._llm_settings.display_name}")

    def _refresh_plugin_state(self) -> None:
        enabled = self._plugin_manager.list(enabled_only=True)
        self.plugins_button.setText(f"Tool 插件 · {len(enabled)}")
        self._refresh_sessions()

    def _refresh_sessions(self) -> None:
        selected = self.session_combo.currentData() if self.session_combo.count() else None
        self.session_combo.clear()
        records = self._registry.list()
        if not records:
            self.session_combo.addItem("尚未注册登录会话", None)
            return
        self.session_combo.addItem("根据任务自动选择窗口", AUTO_SESSION_REF)
        for record in records:
            self.session_combo.addItem(
                f"{PLATFORM_LABELS.get(record.platform, record.platform)} · {record.profile_name}",
                record.session_ref,
            )
            if record.session_ref == selected:
                self.session_combo.setCurrentIndex(self.session_combo.count() - 1)
        if selected == AUTO_SESSION_REF or selected is None:
            self.session_combo.setCurrentIndex(0)

    def _selected_record(self) -> SessionRecord | None:
        session_ref = self.session_combo.currentData()
        if not session_ref or session_ref == AUTO_SESSION_REF:
            return None
        return self._registry.get(str(session_ref))

    def new_conversation(self) -> None:
        if self._managed:
            self.new_conversation_requested.emit()
            return
        if self.controller.busy:
            QMessageBox.information(self, "任务处理中", "请先等待当前任务结束或请求停止。")
            return
        self.chat.clear()
        self.controller.new_conversation()
        self.clear_attachments()
        self.progress_frame.hide()
        self._append_agent("新会话已开始。请选择执行会话并描述任务。")

    @property
    def view_state(self):
        return self.controller.view_state

    @property
    def management_window_open(self):
        return self._session_manager_process is not None

    def set_shared_busy(self, busy):
        self._global_busy = busy
        self.plugins_button.setEnabled(not busy)
        self.manage_sessions_button.setEnabled(not busy and not self.management_window_open)

    def refresh_shared_state(self):
        if not self.controller.busy:
            self._refresh_plugin_state()

    def shutdown(self):
        self.controller.shutdown()
        self._reset_session_manager_launch()

    def closeEvent(self, event):
        if self.controller.busy:
            QMessageBox.information(self, "对话仍在运行", "请先停止任务，再关闭对话。")
            event.ignore()
            return
        self.shutdown()
        event.accept()

    def open_last_output(self) -> None:
        if self.controller.last_result is not None and self.controller.last_result.output_directories:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.controller.last_result.output_directories[0]))

    def _set_planning(self, planning: bool) -> None:
        self.send_button.setEnabled(not planning)
        self.session_combo.setEnabled(not planning)
        self.manage_sessions_button.setEnabled(
            not planning and self._session_manager_process is None
        )
        self.plugins_button.setEnabled(not planning)
        self.model_button.setEnabled(not planning)
        self.message_input.setEnabled(not planning)
        self.attach_button.setEnabled(not planning)
        self.remove_attachment_button.setEnabled(not planning)
        self.send_button.setText("处理中…" if planning else "发送")
        if planning:
            self.progress_frame.show()
            self.progress_label.setText("正在理解任务…")
            self.progress_bar.setRange(0, 0)
            self.progress_value.hide()
        elif self.controller.execution_worker is None:
            self.progress_frame.hide()
            self.progress_bar.setRange(0, 100)
            self.progress_value.show()
        self.conversation_changed.emit()

    def _append_user(self, message: str, *, attachments: list[Path] | None = None) -> None:
        attached = ""
        if attachments:
            names = "、".join(_html(path.name) for path in attachments)
            attached = f"<br><span class='attachment'>附件：{names}</span>"
        body = _html(message).replace(chr(10), "<br>") + attached
        self.chat.append(_chat_message_html("你", body, side="right"))

    def _append_agent(self, message: str, *, error: bool = False) -> None:
        self.chat.append(
            _chat_message_html(
                "Agent",
                _html(message).replace(chr(10), "<br>"),
                side="left",
                error=error,
            )
        )

    def _append_progress_message(self, message: str, *, percent: int | None = None) -> None:
        normalized = message.strip()
        if not normalized:
            return
        display = (
            f"总进度 {max(0, min(percent, 100))}% · {normalized}"
            if percent is not None
            else f"处理中 · {normalized}"
        )
        if display == self._last_progress_message:
            return
        self._last_progress_message = display
        self._append_agent(display)

    def _restore_conversation(self) -> None:
        for turn in self.controller.conversation.turns:
            attachments = [Path(name) for name in turn.attachment_names]
            self._append_user(turn.user_message, attachments=attachments)
            if isinstance(turn.result, dict):
                summary = str(turn.result.get("summary") or "任务已完成。")
                self._append_agent(summary)
            elif turn.error:
                prefix = (
                    "已取消"
                    if turn.status == "cancelled"
                    else "上次任务中断"
                    if turn.error_stage == "interrupted"
                    else "执行失败"
                )
                self._append_agent(
                    f"{prefix}：{turn.error}",
                    error=turn.status != "cancelled",
                )
