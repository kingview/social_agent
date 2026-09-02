from __future__ import annotations

import json
from pathlib import Path

import pytest

from social_ops_agent.settings import (
    LLMProvider,
    LLMSettings,
    LLMSettingsError,
    LLMSettingsStore,
)


class MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_calls = 0

    def get(self, account: str) -> str | None:
        self.get_calls += 1
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        self.values[account] = value

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


def test_remote_key_is_kept_out_of_settings_json(tmp_path: Path) -> None:
    credentials = MemoryCredentials()
    store = LLMSettingsStore(tmp_path / "llm.json", credentials=credentials)
    settings = LLMSettings.create(
        provider=LLMProvider.OPENAI,
        base_url="https://api.openai.com/v1/",
        model="gpt-5.4-mini",
        api_key="sk-private-value",
    )

    store.save(settings)

    payload = json.loads((tmp_path / "llm.json").read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.4-mini",
    }
    assert "sk-private-value" not in (tmp_path / "llm.json").read_text(encoding="utf-8")
    assert credentials.values["openai"] == "sk-private-value"
    assert store.load() == settings


def test_startup_metadata_load_does_not_open_credential_store(tmp_path: Path) -> None:
    credentials = MemoryCredentials()
    store = LLMSettingsStore(tmp_path / "llm.json", credentials=credentials)
    settings = LLMSettings.create(
        provider=LLMProvider.OPENAI,
        base_url="https://api.openai.com/v1",
        model="gpt-5.4-mini",
        api_key="sk-private-value",
    )
    store.save(settings)

    metadata = store.load_metadata()

    assert metadata.api_key == ""
    assert credentials.get_calls == 0
    assert store.with_secret(metadata) == settings
    assert credentials.get_calls == 1


def test_openai_requires_https_and_api_key() -> None:
    with pytest.raises(LLMSettingsError, match="HTTPS"):
        LLMSettings.create(
            provider=LLMProvider.OPENAI,
            base_url="http://api.openai.com/v1",
            model="gpt-5.4-mini",
            api_key="secret",
        )
    with pytest.raises(LLMSettingsError, match="API Key"):
        LLMSettings.create(
            provider=LLMProvider.OPENAI,
            base_url="https://api.openai.com/v1",
            model="gpt-5.4-mini",
        )


def test_ollama_default_does_not_require_a_secret() -> None:
    settings = LLMSettings.create(
        provider=LLMProvider.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        model="qwen3.5:9b",
    )

    assert settings.api_key == "local-model"
    assert settings.display_name == "本地 Ollama · qwen3.5:9b"
