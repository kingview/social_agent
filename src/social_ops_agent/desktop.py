from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .agent_runtime import RuntimePlan, RuntimeRouter
from .contracts import AgentExecutionResult, AgentPlan, AgentProgress, DynamicAgentPlan
from .model_settings_dialog import ModelSettingsDialog
from .multimodal import SUPPORTED_SUFFIXES, MultimodalInputError, prepare_multimodal_input
from .plugin_desktop import PluginManagerDialog
from .plugins import PluginError, PluginInvoker, PluginManager
from .planner import PlanningError, SelectedSession
from .session_store import SessionRecord, SessionStore, default_session_registry_path
from .settings import LLMSettings, LLMSettingsError, LLMSettingsStore


APP_NAME = "Social Agent"
PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "x": "X / Twitter",
}


class PlanWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    status_changed = Signal(str)

    def __init__(
        self,
        *,
        message: str,
        session: SelectedSession | None,
        attachment_paths: list[Path],
        previous_plan: AgentPlan | None,
        context_summary: str | None,
        conversation_id: str,
        registry_path: Path,
        output_root: Path,
        plugin_root: Path,
        router: RuntimeRouter,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._message = message
        self._session = session
        self._attachment_paths = list(attachment_paths)
        self._previous_plan = previous_plan
        self._context_summary = context_summary
        self._conversation_id = conversation_id
        self._registry_path = registry_path
        self._output_root = output_root
        self._plugin_root = plugin_root
        self._router = router

    def run(self) -> None:
        try:
            if self._attachment_paths:
                self.status_changed.emit("正在解析附件；视频和音频可能需要几分钟…")
            prepared = prepare_multimodal_input(
                self._attachment_paths,
                message=self._message,
                conversation_id=self._conversation_id,
                output_root=self._output_root,
                registry_path=self._registry_path,
                plugin_root=self._plugin_root,
                settings=self._router.settings,
            )
            self.status_changed.emit("Harness 正在结合附件和会话上下文生成计划…")
            plan = self._router.propose(
                self._message,
                self._session,
                self._previous_plan,
                attachments=prepared.attachments,
                media_context=prepared.media_context,
                context_summary=self._context_summary,
            )
        except (PlanningError, MultimodalInputError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            detail = str(exc).strip()
            self.failed.emit(
                detail
                or "无法生成执行计划，请确认 Harness、Node 24 与配置的模型服务正在运行。"
            )
        else:
            self.succeeded.emit(plan)


class ExecutionWorker(QThread):
    progress_changed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        plan: RuntimePlan,
        registry_path: Path,
        output_root: Path,
        conversation_id: str,
        router: RuntimeRouter,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._plan = plan
        self._cancel_requested = False
        self._router = router

    def cancel_after_current_batch(self) -> None:
        self._cancel_requested = True
        self._router.cancel()

    def run(self) -> None:
        try:
            result = self._router.execute(
                self._plan,
                progress=self.progress_changed.emit,
            )
        except Exception as exc:
            if self._cancel_requested:
                self.succeeded.emit(
                    AgentExecutionResult(
                        runtime=(
                            "deepseek_harness"
                            if isinstance(self._plan, DynamicAgentPlan)
                            else "deterministic"
                        ),
                        plan=self._plan,
                        summary="任务已由用户停止。",
                        cancelled=True,
                        finish_reason="cancelled",
                    )
                )
                return
            message = str(exc).strip() or "Agent 执行失败。"
            self.failed.emit(message)
        else:
            self.succeeded.emit(result)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        registry_path: Path | None = None,
        output_root: Path | None = None,
        plugin_root: Path | None = None,
        llm_settings_store: LLMSettingsStore | None = None,
    ) -> None:
        super().__init__()
        self._registry_path = (registry_path or default_session_registry_path()).expanduser().resolve()
        self._registry = SessionStore(self._registry_path)
        self._output_root = (output_root or default_output_root()).expanduser().resolve()
        self._plugin_manager = PluginManager(plugin_root)
        self._llm_settings_store = llm_settings_store or LLMSettingsStore()
        self._settings_load_error: str | None = None
        try:
            self._llm_settings = self._llm_settings_store.load()
        except LLMSettingsError as exc:
            self._llm_settings = LLMSettings.from_env()
            self._settings_load_error = str(exc)
        self._conversation_id = f"conversation-{uuid.uuid4().hex}"
        self._router = self._new_router()
        self._plan_worker: PlanWorker | None = None
        self._execution_worker: ExecutionWorker | None = None
        self._pending_plan: RuntimePlan | None = None
        self._last_plan: AgentPlan | None = None
        self._last_result: AgentExecutionResult | None = None
        self._attachment_paths: list[Path] = []
        self._session_manager_process = None
        self._session_manager_timer = QTimer(self)
        self._session_manager_timer.setInterval(500)
        self._session_manager_timer.timeout.connect(self._poll_session_manager)

        self.setWindowTitle("Social Agent · 社媒任务助手")
        self.resize(1_020, 760)
        self.setMinimumSize(780, 620)
        self.setAcceptDrops(True)
        self._build_ui()
        self._refresh_plugin_state()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        eyebrow = QLabel("LOCAL TOOL ORCHESTRATOR")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Social Agent")
        title.setObjectName("title")
        subtitle = QLabel("固定 Workflow + DeepSeek Harness 动态编排；计划确认后才执行。")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.new_chat_button = QPushButton("新会话")
        self.new_chat_button.setObjectName("secondaryButton")
        self.new_chat_button.clicked.connect(self.new_conversation)
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
        self.session_combo = QComboBox()
        self.session_combo.setObjectName("control")
        self.manage_sessions_button = QPushButton("管理比特浏览器会话")
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
        self.message_input.setPlaceholderText("描述任务；后续也可以说“改成前50条”继续调整计划…")
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
        self.execute_button = QPushButton("确认并执行计划")
        self.execute_button.setObjectName("executeButton")
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self.execute_plan)
        self.plan_button = QPushButton("生成计划  →")
        self.plan_button.setObjectName("primaryButton")
        self.plan_button.clicked.connect(self.request_plan)
        action_row.addWidget(self.attach_button)
        action_row.addWidget(self.remove_attachment_button)
        action_row.addWidget(hint)
        action_row.addStretch()
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.execute_button)
        action_row.addWidget(self.plan_button)
        input_layout.addLayout(action_row)
        layout.addWidget(input_frame)

        self._append_agent(
            "可以直接添加图片、视频或音频并描述需求；需要浏览社媒时，再选择一个已经在比特浏览器中手动登录的会话。"
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
        if self._plan_worker is not None or self._execution_worker is not None:
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

    def request_plan(self) -> None:
        record = self._selected_record()
        message = self.message_input.toPlainText().strip()
        attachments = list(self._attachment_paths)
        if not message and attachments:
            message = "请理解并分析这些附件，根据附件内容完成合适的本地任务。"
        if not message:
            return
        if record is None and not attachments:
            QMessageBox.warning(
                self,
                "缺少输入",
                "请添加图片、视频或音频；如需浏览社媒平台，还要选择登录会话。",
            )
            return
        self._append_user(message, attachments=attachments)
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
        worker = PlanWorker(
            message=message,
            session=session,
            attachment_paths=attachments,
            previous_plan=self._last_plan,
            context_summary=self._last_result.summary if self._last_result else None,
            conversation_id=self._conversation_id,
            registry_path=self._registry_path,
            output_root=self._output_root,
            plugin_root=self._plugin_manager.root,
            router=self._router,
            parent=self,
        )
        worker.status_changed.connect(self._planning_status)
        worker.succeeded.connect(self._plan_succeeded)
        worker.failed.connect(self._plan_failed)
        worker.finished.connect(self._planning_finished)
        self._plan_worker = worker
        worker.start()

    def _plan_succeeded(self, plan: RuntimePlan) -> None:
        self.clear_attachments()
        self._pending_plan = plan
        self.execute_button.setEnabled(True)
        if isinstance(plan, DynamicAgentPlan):
            steps = "\n".join(f"{index}. {step}" for index, step in enumerate(plan.steps, 1))
            publish_warning = ""
            if plan.write_actions == ["publish_x"]:
                publish_warning = (
                    "\n⚠ 此计划包含外部写操作：将通过所选账号公开发布一条 X 帖子；"
                    "发布请求不会自动重试。"
                )
                self.execute_button.setText("确认并自动发布到 X")
            else:
                self.execute_button.setText("确认并执行计划")
            self._append_agent(
                f"Harness 动态计划已生成：{plan.summary}\n{steps}\n"
                f"点击确认后，Harness 才能调用白名单 MCP Tools。{publish_warning}"
            )
            return
        self.execute_button.setText("确认并执行计划")
        self._last_plan = plan
        if plan.remove_watermark:
            action = "浏览、下载并在检测到高置信度静态水印时生成去水印副本"
        else:
            action = "浏览并下载" if plan.download else "仅浏览"
        target = plan.query or plan.user_key or str(plan.start_url or "推荐流")
        batches = (
            (plan.limit + plan.download_batch_size - 1) // plan.download_batch_size
            if plan.download
            else 0
        )
        calls = 1 + batches + (batches if plan.remove_watermark else 0)
        self._append_agent(
            f"计划已生成：{PLATFORM_LABELS[plan.platform.value]} · {action} · “{target}” · "
            f"最多 {plan.limit} 条 · 预计最多 {calls} 次 Tool 调用。\n"
            "点击“确认并执行计划”后开始；在此之前不会访问平台或下载文件。"
        )

    def _plan_failed(self, message: str) -> None:
        self._append_agent(f"无法生成计划：{message}", error=True)

    def _planning_status(self, message: str) -> None:
        self.plan_button.setText(message)

    def _planning_finished(self) -> None:
        self._plan_worker = None
        self._set_planning(False)

    def execute_plan(self) -> None:
        if self._pending_plan is None or self._execution_worker is not None:
            return
        plan = self._pending_plan
        if isinstance(plan, DynamicAgentPlan) and plan.write_actions == ["publish_x"]:
            answer = QMessageBox.warning(
                self,
                "确认自动发布到 X",
                "此计划会通过当前比特浏览器账号公开发布一条 X 帖子。\n\n"
                "发布内容可能由 Agent 根据前序分析生成；提交后不会自动撤回或重试。\n"
                "是否确认执行整套计划并允许本次发布？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
        self._pending_plan = None
        self.execute_button.setEnabled(False)
        self.plan_button.setEnabled(False)
        self.session_combo.setEnabled(False)
        self.manage_sessions_button.setEnabled(False)
        self.plugins_button.setEnabled(False)
        self.model_button.setEnabled(False)
        self.message_input.setEnabled(False)
        self.attach_button.setEnabled(False)
        self.remove_attachment_button.setEnabled(False)
        self.cancel_button.show()
        self.progress_frame.show()
        self.progress_bar.setValue(0)
        self._append_agent("计划已确认，开始执行。")
        worker = ExecutionWorker(
            plan=plan,
            registry_path=self._registry_path,
            output_root=self._output_root,
            conversation_id=self._conversation_id,
            router=self._router,
            parent=self,
        )
        worker.progress_changed.connect(self._execution_progress)
        worker.succeeded.connect(self._execution_succeeded)
        worker.failed.connect(self._execution_failed)
        worker.finished.connect(self._execution_finished)
        self._execution_worker = worker
        worker.start()

    def cancel_execution(self) -> None:
        if self._execution_worker is not None:
            self._execution_worker.cancel_after_current_batch()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("已请求停止；固定下载将在当前批次后停止…")

    def _execution_progress(self, event: AgentProgress) -> None:
        percent = int(event.completed / max(event.total, 1) * 100)
        self.progress_bar.setValue(max(0, min(percent, 100)))
        self.progress_value.setText(f"{percent}%")
        self.progress_label.setText(event.message)

    def _execution_succeeded(self, result: AgentExecutionResult) -> None:
        self._last_result = result
        details = result.summary
        if result.tool_calls:
            details += "\nTool：" + "、".join(result.tool_calls)
        if result.output_directories:
            details += f"\n保存目录：{result.output_directories[0]}"
        if result.warnings:
            details += "\n提醒：" + "；".join(result.warnings)
        self._append_agent(details)

    def _execution_failed(self, message: str) -> None:
        self._append_agent(f"执行失败：{message}", error=True)

    def _execution_finished(self) -> None:
        self._execution_worker = None
        self.plan_button.setEnabled(True)
        self.session_combo.setEnabled(True)
        self.manage_sessions_button.setEnabled(True)
        self.plugins_button.setEnabled(True)
        self.model_button.setEnabled(True)
        self.message_input.setEnabled(True)
        self.attach_button.setEnabled(True)
        self.remove_attachment_button.setEnabled(True)
        self.cancel_button.hide()
        self.cancel_button.setEnabled(True)
        self.execute_button.setText("确认并执行计划")

    def manage_sessions(self) -> None:
        if (
            self._session_manager_process is not None
            and self._session_manager_process.poll() is None
        ):
            return
        try:
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
            )
        except PluginError as exc:
            QMessageBox.information(
                self,
                "需要社媒浏览与下载插件",
                f"{exc}\n\n请先点击顶部“Tool 插件”安装社媒浏览与下载插件。",
            )
            return
        self.manage_sessions_button.setEnabled(False)
        self.manage_sessions_button.setText("会话管理已打开")
        self._session_manager_timer.start()

    def _poll_session_manager(self) -> None:
        process = self._session_manager_process
        if process is not None and process.poll() is None:
            return
        self._session_manager_timer.stop()
        self._session_manager_process = None
        self.manage_sessions_button.setEnabled(True)
        self.manage_sessions_button.setText("管理比特浏览器会话")
        if process is not None and process.returncode:
            stderr = b""
            try:
                _, stderr = process.communicate(timeout=1)
            except Exception:
                pass
            detail = stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 1_200:
                detail = detail[-1_200:]
            message = "会话管理界面未能启动。"
            if detail:
                message += f"\n\n{detail}"
            QMessageBox.critical(self, "无法打开会话管理", message)
        self._refresh_sessions()

    def manage_plugins(self) -> None:
        dialog = PluginManagerDialog(self._plugin_manager, self)
        dialog.plugins_changed.connect(self._refresh_plugin_state)
        dialog.exec()
        self._refresh_plugin_state()

    def manage_model_settings(self) -> None:
        if self._plan_worker is not None or self._execution_worker is not None:
            return
        dialog = ModelSettingsDialog(
            self._llm_settings_store,
            self._llm_settings,
            self,
        )
        if not dialog.exec() or dialog.selected_settings is None:
            return
        selected = dialog.selected_settings
        self._router.close()
        self._llm_settings = selected
        self._router = self._new_router()
        self._pending_plan = None
        self.execute_button.setEnabled(False)
        self.execute_button.setText("确认并执行计划")
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
        for record in records:
            self.session_combo.addItem(
                f"{PLATFORM_LABELS.get(record.platform, record.platform)} · {record.profile_name}",
                record.session_ref,
            )
            if record.session_ref == selected:
                self.session_combo.setCurrentIndex(self.session_combo.count() - 1)

    def _selected_record(self) -> SessionRecord | None:
        session_ref = self.session_combo.currentData()
        if not session_ref:
            return None
        return self._registry.get(str(session_ref))

    def new_conversation(self) -> None:
        if self._plan_worker is not None or self._execution_worker is not None:
            QMessageBox.information(self, "任务处理中", "请先等待当前任务结束或请求停止。")
            return
        self.chat.clear()
        self._pending_plan = None
        self._last_plan = None
        self._last_result = None
        self._router.close()
        self._conversation_id = f"conversation-{uuid.uuid4().hex}"
        self._router = self._new_router()
        self.clear_attachments()
        self.execute_button.setEnabled(False)
        self.execute_button.setText("确认并执行计划")
        self.progress_frame.hide()
        self._append_agent("新会话已开始。请选择执行会话并描述任务。")

    def _new_router(self) -> RuntimeRouter:
        return RuntimeRouter(
            registry_path=self._registry_path,
            output_root=self._output_root,
            conversation_id=self._conversation_id,
            settings=self._llm_settings,
        )

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._router.close()
        super().closeEvent(event)

    def open_last_output(self) -> None:
        if self._last_result is not None and self._last_result.output_directories:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_result.output_directories[0]))

    def _set_planning(self, planning: bool) -> None:
        self.plan_button.setEnabled(not planning)
        self.session_combo.setEnabled(not planning)
        self.manage_sessions_button.setEnabled(not planning)
        self.plugins_button.setEnabled(not planning)
        self.model_button.setEnabled(not planning)
        self.message_input.setEnabled(not planning)
        self.attach_button.setEnabled(not planning)
        self.remove_attachment_button.setEnabled(not planning)
        self.plan_button.setText("正在生成计划…" if planning else "生成计划  →")

    def _append_user(self, message: str, *, attachments: list[Path] | None = None) -> None:
        attached = ""
        if attachments:
            names = "、".join(_html(path.name) for path in attachments)
            attached = f"<br><span class='attachment'>附件：{names}</span>"
        self.chat.append(f"<div class='user'><b>你</b><br>{_html(message)}{attached}</div>")

    def _append_agent(self, message: str, *, error: bool = False) -> None:
        css_class = "error" if error else "agent"
        self.chat.append(
            f"<div class='{css_class}'><b>Agent</b><br>{_html(message).replace(chr(10), '<br>')}</div>"
        )


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


STYLESHEET = """
QWidget#root { background: #111318; color: #f1f2f4; }
QLabel#eyebrow { color: #d8ff52; font-size: 11px; font-weight: 800; letter-spacing: 2px; }
QLabel#title { font-size: 34px; font-weight: 800; }
QLabel#subtitle, QLabel#hint { color: #9297a3; }
QLabel#fieldLabel, QLabel#progressLabel { color: #cfd2d8; font-weight: 700; }
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
QPushButton#primaryButton, QPushButton#executeButton { background: #d8ff52; color: #15170c; border: none; }
QPushButton#executeButton { background: #b7d943; }
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Social Agent desktop client")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLESHEET)
    window = MainWindow(output_root=args.output_root)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
