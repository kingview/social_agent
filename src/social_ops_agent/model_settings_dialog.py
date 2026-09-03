from __future__ import annotations

from .diagnostics import record_exception

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .settings import (
    LLMProvider,
    LLMSettings,
    LLMSettingsError,
    LLMSettingsStore,
    PROVIDER_DEFAULTS,
    PROVIDER_LABELS,
)


class ModelProbeWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: LLMSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings

    def run(self) -> None:
        try:
            models = self.settings.list_models()
        except Exception as exc:
            record_exception("agent", "model_settings_dialog.handled", exc)
            self.failed.emit(str(exc).strip() or type(exc).__name__)
        else:
            self.succeeded.emit(models)


class ComboItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):  # type: ignore[no-untyped-def]
        size = super().sizeHint(option, index)
        size.setHeight(42)
        return size


class UniformComboBox(QComboBox):
    """Use a predictable cross-platform popup instead of the compact macOS menu."""

    POPUP_ROW_HEIGHT = 42
    MAX_VISIBLE_ROWS = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        view = QListView(self)
        view.setObjectName("comboPopup")
        view.setUniformItemSizes(True)
        view.setItemDelegate(ComboItemDelegate(view))
        self.setView(view)
        self.setMaxVisibleItems(self.MAX_VISIBLE_ROWS)

    def showPopup(self) -> None:
        visible_rows = max(1, min(self.count(), self.MAX_VISIBLE_ROWS))
        view = self.view()
        view.setMinimumWidth(self.width())
        view.setMinimumHeight(visible_rows * self.POPUP_ROW_HEIGHT + 8)
        super().showPopup()


class ModelSettingsDialog(QDialog):
    """Configure one model source without exposing stored credentials."""

    def __init__(
        self,
        store: LLMSettingsStore,
        current: LLMSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.current = current
        self.selected_settings: LLMSettings | None = None
        self._worker: ModelProbeWorker | None = None
        self._last_provider = current.provider

        self.setObjectName("root")
        self.setWindowTitle("LLM 模型来源")
        self.resize(660, 470)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(15)

        title = QLabel("设置 Social Agent 使用的模型")
        title.setObjectName("dialogTitle")
        description = QLabel(
            "所选来源会同时用于 Harness 规划、执行、附件理解和 Tool 插件。"
        )
        description.setObjectName("subtitle")
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(13)
        self.provider_combo = UniformComboBox()
        self.provider_combo.setObjectName("control")
        for provider in LLMProvider:
            self.provider_combo.addItem(PROVIDER_LABELS[provider], provider.value)
        self.endpoint_input = QLineEdit()
        self.endpoint_input.setObjectName("control")
        self.endpoint_input.setPlaceholderText("例如 https://api.example.com/v1")
        self.model_combo = UniformComboBox()
        self.model_combo.setObjectName("control")
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.key_input = QLineEdit()
        self.key_input.setObjectName("control")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setClearButtonEnabled(True)
        for control in (
            self.provider_combo,
            self.endpoint_input,
            self.model_combo,
            self.key_input,
        ):
            control.setMinimumHeight(46)
            control.setMaximumHeight(46)
            control.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        form.addRow("模型来源", self.provider_combo)
        form.addRow("API 根地址", self.endpoint_input)
        form.addRow("模型 ID", self.model_combo)
        form.addRow("API Key", self.key_input)
        layout.addLayout(form)

        self.security_hint = QLabel()
        self.security_hint.setObjectName("hint")
        self.security_hint.setWordWrap(True)
        layout.addWidget(self.security_hint)

        self.status_label = QLabel("尚未测试连接")
        self.status_label.setObjectName("hint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch()

        actions = QHBoxLayout()
        self.test_button = QPushButton("测试并读取模型")
        self.test_button.setObjectName("secondaryButton")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("secondaryButton")
        self.save_button = QPushButton("保存并使用")
        self.save_button.setObjectName("primaryButton")
        actions.addWidget(self.test_button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

        index = self.provider_combo.findData(current.provider.value)
        self.provider_combo.setCurrentIndex(max(index, 0))
        self.endpoint_input.setText(current.base_url)
        self.model_combo.addItem(current.model)
        self.model_combo.setCurrentText(current.model)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.test_button.clicked.connect(self.test_connection)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.save)
        self._refresh_key_state()

    def _provider(self) -> LLMProvider:
        return LLMProvider(str(self.provider_combo.currentData()))

    def _provider_changed(self) -> None:
        provider = self._provider()
        if provider is not self._last_provider:
            endpoint, model = PROVIDER_DEFAULTS[provider]
            self.endpoint_input.setText(endpoint)
            self.model_combo.clear()
            if provider is LLMProvider.OPENAI:
                self.model_combo.addItems(["gpt-5.4-mini", "gpt-5.4"])
            elif model:
                self.model_combo.addItem(model)
            self.model_combo.setCurrentText(model)
            self.key_input.clear()
            self._last_provider = provider
        self.status_label.setText("来源已更改，建议先测试连接。")
        self._refresh_key_state()

    def _refresh_key_state(self) -> None:
        provider = self._provider()
        local = provider is LLMProvider.OLLAMA
        self.key_input.setEnabled(not local)
        if local:
            self.key_input.setPlaceholderText("本地 Ollama 不需要 API Key")
            self.security_hint.setText(
                "Ollama 在本机运行；请先确保 Ollama 服务和所选模型已经启动。"
            )
            return
        try:
            stored = bool(self.store.api_key(provider))
        except LLMSettingsError as exc:
            record_exception("agent", "model_settings_dialog.handled", exc)
            stored = False
            self.status_label.setText(str(exc))
        self.key_input.setPlaceholderText(
            "已安全保存；留空则继续使用" if stored else "输入服务商提供的 API Key"
        )
        if provider is LLMProvider.OPENAI:
            self.security_hint.setText(
                "OpenAI API 需要平台 API Key，不能直接复用 ChatGPT 订阅或网页登录。"
                "密钥只保存在 macOS 钥匙串或 Windows 凭据管理器中。"
            )
        else:
            self.security_hint.setText(
                "适用于 vLLM、LiteLLM 及其他兼容 OpenAI Chat Completions 的服务。"
                "密钥只保存在系统凭据管理器中。"
            )

    def _settings(self) -> LLMSettings:
        provider = self._provider()
        entered = self.key_input.text().strip()
        api_key = entered
        if not api_key:
            api_key = self.store.api_key(provider) or ""
        return LLMSettings.create(
            provider=provider,
            base_url=self.endpoint_input.text(),
            model=self.model_combo.currentText(),
            api_key=api_key,
        )

    def test_connection(self) -> None:
        if self._worker is not None:
            return
        try:
            settings = self._settings()
        except LLMSettingsError as exc:
            record_exception("agent", "model_settings_dialog.handled", exc)
            QMessageBox.warning(self, "模型设置不完整", str(exc))
            return
        self._set_busy(True)
        self.status_label.setText("正在连接模型端点并读取模型列表…")
        worker = ModelProbeWorker(settings, self)
        worker.succeeded.connect(self._probe_succeeded)
        worker.failed.connect(self._probe_failed)
        worker.finished.connect(self._probe_finished)
        self._worker = worker
        worker.start()

    def _probe_succeeded(self, models: object) -> None:
        rows = [str(item) for item in models] if isinstance(models, list) else []
        current = self.model_combo.currentText().strip()
        self.model_combo.clear()
        self.model_combo.addItems(rows)
        if current:
            self.model_combo.setCurrentText(current)
        if rows and current not in rows:
            self.status_label.setText(
                f"连接成功，共发现 {len(rows)} 个模型；当前模型 ID 不在返回列表中。"
            )
        else:
            self.status_label.setText(f"连接成功，共发现 {len(rows)} 个模型。")

    def _probe_failed(self, message: str) -> None:
        self.status_label.setText(f"连接失败：{message}")

    def _probe_finished(self) -> None:
        self._worker = None
        self._set_busy(False)
        self._refresh_key_state()

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.provider_combo,
            self.endpoint_input,
            self.model_combo,
            self.key_input,
            self.test_button,
            self.cancel_button,
            self.save_button,
        ):
            widget.setEnabled(not busy)

    def save(self) -> None:
        try:
            settings = self._settings()
            self.store.save(settings)
        except LLMSettingsError as exc:
            record_exception("agent", "model_settings_dialog.handled", exc)
            QMessageBox.warning(self, "无法保存模型设置", str(exc))
            return
        self.selected_settings = settings
        self.accept()

    def reject(self) -> None:
        if self._worker is not None:
            QMessageBox.information(self, "正在测试", "请等待当前连接测试结束。")
            return
        super().reject()
