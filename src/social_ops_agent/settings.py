from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import keyring
from keyring.errors import KeyringError


SETTINGS_VERSION = 1
KEYRING_SERVICE = "com.socialagent.llm"


class LLMSettingsError(ValueError):
    pass


class LLMProvider(StrEnum):
    OLLAMA = "ollama"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"


PROVIDER_LABELS = {
    LLMProvider.OLLAMA: "本地 Ollama",
    LLMProvider.OPENAI: "OpenAI API",
    LLMProvider.OPENAI_COMPATIBLE: "其他 OpenAI-compatible",
}


PROVIDER_DEFAULTS = {
    LLMProvider.OLLAMA: ("http://127.0.0.1:11434/v1", "qwen3.5:9b"),
    LLMProvider.OPENAI: ("https://api.openai.com/v1", "gpt-5.4-mini"),
    LLMProvider.OPENAI_COMPATIBLE: ("http://127.0.0.1:8000/v1", ""),
}


class CredentialStore(Protocol):
    def get(self, account: str) -> str | None: ...

    def set(self, account: str, value: str) -> None: ...

    def delete(self, account: str) -> None: ...


class SystemCredentialStore:
    """Store remote model credentials in Keychain/Credential Manager."""

    def get(self, account: str) -> str | None:
        try:
            return keyring.get_password(KEYRING_SERVICE, account)
        except KeyringError as exc:
            raise LLMSettingsError(f"无法读取系统钥匙串：{exc}") from exc

    def set(self, account: str, value: str) -> None:
        try:
            keyring.set_password(KEYRING_SERVICE, account, value)
        except KeyringError as exc:
            raise LLMSettingsError(f"无法写入系统钥匙串：{exc}") from exc

    def delete(self, account: str) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as exc:
            raise LLMSettingsError(f"无法更新系统钥匙串：{exc}") from exc


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """One selected OpenAI-compatible endpoint used across Agent and plugins."""

    provider: LLMProvider
    base_url: str
    model: str
    api_key: str

    @property
    def provider_id(self) -> str:
        return "social-openai-compatible"

    @property
    def display_name(self) -> str:
        return f"{PROVIDER_LABELS[self.provider]} · {self.model}"

    @classmethod
    def create(
        cls,
        *,
        provider: LLMProvider | str,
        base_url: str,
        model: str,
        api_key: str = "",
    ) -> LLMSettings:
        selected = LLMProvider(provider)
        endpoint = _validate_base_url(base_url, require_https=selected is LLMProvider.OPENAI)
        model_id = model.strip()
        if not model_id or len(model_id) > 200:
            raise LLMSettingsError("请输入有效的模型 ID。")
        secret = api_key.strip()
        if selected is LLMProvider.OPENAI and not secret:
            raise LLMSettingsError("OpenAI API 来源必须配置 API Key。")
        if selected is LLMProvider.OLLAMA and not secret:
            secret = "local-model"
        return cls(provider=selected, base_url=endpoint, model=model_id, api_key=secret)

    @classmethod
    def from_env(cls) -> LLMSettings:
        base_url = (
            os.getenv("SOCIAL_AGENT_LLM_BASE_URL")
            or os.getenv("SOCIAL_AGENT_OLLAMA_BASE_URL")
            or PROVIDER_DEFAULTS[LLMProvider.OLLAMA][0]
        )
        model = (
            os.getenv("SOCIAL_AGENT_LLM_MODEL")
            or os.getenv("SOCIAL_AGENT_OLLAMA_MODEL")
            or PROVIDER_DEFAULTS[LLMProvider.OLLAMA][1]
        )
        provider = _provider_for_endpoint(base_url)
        api_key = os.getenv("SOCIAL_AGENT_LLM_API_KEY") or os.getenv(
            "SOCIAL_AGENT_OLLAMA_API_KEY"
        )
        if not api_key and provider is LLMProvider.OLLAMA:
            api_key = "local-model"
        return cls.create(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key or "",
        )

    def health(self, *, timeout_seconds: float = 8.0) -> tuple[bool, str]:
        try:
            models = self.list_models(timeout_seconds=timeout_seconds)
        except LLMSettingsError as exc:
            return False, str(exc)
        if models and self.model not in models:
            return False, f"端点连接成功，但没有找到模型 {self.model}。"
        return True, f"模型端点已就绪：{self.display_name}"

    def list_models(self, *, timeout_seconds: float = 8.0) -> list[str]:
        request = Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                if not 200 <= int(response.status) < 300:
                    raise LLMSettingsError(f"模型端点返回 HTTP {response.status}。")
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise LLMSettingsError("模型端点拒绝了 API Key，请检查密钥和账号权限。") from exc
            raise LLMSettingsError(f"模型端点返回 HTTP {exc.code}。") from exc
        except (OSError, URLError, ValueError) as exc:
            raise LLMSettingsError(f"模型端点不可用：{exc}") from exc
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        return sorted(
            {
                str(item.get("id") or "").strip()
                for item in rows
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
        )


class LLMSettingsStore:
    """Persist non-secret settings; keep API keys in the OS credential store."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        credentials: CredentialStore | None = None,
    ) -> None:
        self.path = (path or default_llm_settings_path()).expanduser().resolve()
        self.credentials = credentials or SystemCredentialStore()

    def load(self) -> LLMSettings:
        if _has_llm_environment_override() or not self.path.is_file():
            return LLMSettings.from_env()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            provider = LLMProvider(str(payload["provider"]))
            api_key = self.api_key(provider)
            return LLMSettings.create(
                provider=provider,
                base_url=str(payload["base_url"]),
                model=str(payload["model"]),
                api_key=api_key or "",
            )
        except LLMSettingsError:
            raise
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMSettingsError("本地 LLM 设置文件损坏，请重新保存模型设置。") from exc

    def api_key(self, provider: LLMProvider | str) -> str | None:
        selected = LLMProvider(provider)
        if selected is LLMProvider.OLLAMA:
            return "local-model"
        return self.credentials.get(selected.value)

    def save(self, settings: LLMSettings) -> None:
        validated = LLMSettings.create(
            provider=settings.provider,
            base_url=settings.base_url,
            model=settings.model,
            api_key=settings.api_key,
        )
        if validated.provider is not LLMProvider.OLLAMA and validated.api_key:
            self.credentials.set(validated.provider.value, validated.api_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = {
            "version": SETTINGS_VERSION,
            "provider": validated.provider.value,
            "base_url": validated.base_url,
            "model": validated.model,
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def default_llm_settings_path() -> Path:
    configured = os.getenv("SOCIAL_AGENT_LLM_SETTINGS_PATH")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "SocialAgent"
    elif os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SocialAgent"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "social-agent"
    return base / "llm-settings.json"


def _provider_for_endpoint(value: str) -> LLMProvider:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host == "api.openai.com":
        return LLMProvider.OPENAI
    if host in {"127.0.0.1", "localhost", "::1"} and parsed.port == 11434:
        return LLMProvider.OLLAMA
    return LLMProvider.OPENAI_COMPATIBLE


def _validate_base_url(value: str, *, require_https: bool) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise LLMSettingsError("模型地址必须是有效的 HTTP(S) API 根地址。")
    if require_https and parsed.scheme != "https":
        raise LLMSettingsError("OpenAI API 地址必须使用 HTTPS。")
    return candidate


def _has_llm_environment_override() -> bool:
    return any(
        os.getenv(name)
        for name in (
            "SOCIAL_AGENT_LLM_BASE_URL",
            "SOCIAL_AGENT_LLM_MODEL",
            "SOCIAL_AGENT_LLM_API_KEY",
            "SOCIAL_AGENT_OLLAMA_BASE_URL",
            "SOCIAL_AGENT_OLLAMA_MODEL",
            "SOCIAL_AGENT_OLLAMA_API_KEY",
        )
    )
