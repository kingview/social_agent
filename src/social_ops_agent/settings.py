from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Provider-neutral settings for an OpenAI-compatible model endpoint."""

    base_url: str
    model: str
    api_key: str
    provider_id: str = "local-openai-compatible"

    @classmethod
    def from_env(cls) -> LLMSettings:
        return cls(
            base_url=(
                os.getenv("SOCIAL_AGENT_LLM_BASE_URL")
                or os.getenv("SOCIAL_AGENT_OLLAMA_BASE_URL")
                or "http://127.0.0.1:11434/v1"
            ).rstrip("/"),
            model=(
                os.getenv("SOCIAL_AGENT_LLM_MODEL")
                or os.getenv("SOCIAL_AGENT_OLLAMA_MODEL")
                or "qwen3.5:9b"
            ),
            api_key=(
                os.getenv("SOCIAL_AGENT_LLM_API_KEY")
                or os.getenv("SOCIAL_AGENT_OLLAMA_API_KEY")
                or "local-model"
            ),
            provider_id="local-openai-compatible",
        )

    def health(self, *, timeout_seconds: float = 2.0) -> tuple[bool, str]:
        request = Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                if not 200 <= int(response.status) < 300:
                    return False, f"模型端点返回 HTTP {response.status}"
        except (OSError, URLError) as exc:
            return False, f"模型端点不可用：{exc}"
        return True, f"模型端点已就绪：{self.model}"
