"""An LLM client that speaks OpenAI-compatible HTTP.

This is deliberately dependency-free and provider-agnostic. Every serious LLM
endpoint -- OpenAI, Groq, Together, vLLM, Ollama, LM Studio, and Nous Research's
Hermes running under any of them -- exposes the same `/chat/completions` shape,
so one implementation covers all of them.

The one thing this module will never do is decide anything. It returns text.
Interpretation, validation and execution belong to the caller.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


@dataclass
class LLMConfig:
    """How to reach a model."""

    base_url: str = ""             # e.g. http://localhost:8000/v1  or  https://api.openai.com/v1
    api_key: str = ""              # local servers usually ignore it
    model: str = ""                # e.g. hermes-3-llama-3.1-8b, gpt-4o-mini
    temperature: float = 0.0       # deterministic: this is a parser, not a brainstorm
    max_tokens: int = 400
    timeout: float = DEFAULT_TIMEOUT
    # A second, cheaper model can be used for routine replies
    fallback_model: str = ""

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or os.getenv("LLM_BASE_URL",
                                                    "http://localhost:8000/v1")).rstrip("/")
        self.api_key = self.api_key or os.getenv("LLM_API_KEY", "")
        self.model = self.model or os.getenv("LLM_MODEL", "")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def redacted(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "configured": self.configured,
            "api_key_set": bool(self.api_key),
        }


class LLMError(RuntimeError):
    """The model could not be reached, or returned something unusable."""


class LLMClient:
    """Minimal OpenAI-compatible chat client."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.cfg = config or LLMConfig()
        self.calls = 0
        self.failures = 0

    def complete(self, system: str, user: str, model: str | None = None,
                 temperature: float | None = None) -> str:
        """One chat completion. Raises LLMError on any failure."""
        if not self.cfg.base_url:
            raise LLMError("LLM base_url is not configured")

        payload = {
            "model": model or self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        url = f"{self.cfg.base_url}/chat/completions"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            self.failures += 1
            raise LLMError(f"HTTP {exc.code} from {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.failures += 1
            raise LLMError(f"cannot reach {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            self.failures += 1
            raise LLMError(f"malformed response from {url}") from exc

        self.calls += 1
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            self.failures += 1
            raise LLMError(f"no completion in response: {str(body)[:200]}") from exc
        if not isinstance(content, str) or not content.strip():
            self.failures += 1
            raise LLMError("model returned an empty completion")
        return content.strip()


__all__ = ["LLMClient", "LLMConfig", "LLMError"]
