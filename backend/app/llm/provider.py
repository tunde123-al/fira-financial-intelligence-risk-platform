"""Provider-agnostic LLM interface.

The application depends only on `LLMProvider`. Concrete providers talk to the
vendors' documented HTTP APIs with the standard library, so adding or swapping a
provider needs no SDK. All configuration comes from environment variables
(LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, LLM_BASE_URL).

`NullProvider` is the default: FIRA is fully functional without an LLM — the
report narrative is then rendered deterministically from evidence.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.http import require_http_url


class LLMUnavailable(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1500,
                 temperature: float = 0.0) -> LLMResponse: ...


def _post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: int, retries: int = 2) -> dict[str, Any]:
    require_http_url(url)
    data = json.dumps(body).encode()
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})  # noqa: S310 (scheme validated)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310  # nosec B310
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = LLMError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
            if e.code not in (429, 500, 502, 503, 504, 529):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = LLMError(f"connection error: {e}")
        time.sleep(min(2 ** attempt, 8))
    raise last or LLMError("unknown LLM error")


class NullProvider:
    name = "none"
    model = "none"

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1500,
                 temperature: float = 0.0) -> LLMResponse:
        raise LLMUnavailable("no LLM provider configured (LLM_PROVIDER=none)")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str, base_url: str | None = None, timeout: int = 60):
        self.model, self.api_key, self.timeout = model, api_key, timeout
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1500,
                 temperature: float = 0.0) -> LLMResponse:
        t0 = time.perf_counter()
        res = _post(f"{self.base_url}/v1/messages",
                    {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                    {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                     "messages": messages}, self.timeout)
        text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
        u = res.get("usage", {})
        return LLMResponse(text, res.get("model", self.model), int(u.get("input_tokens", 0)),
                           int(u.get("output_tokens", 0)), int((time.perf_counter() - t0) * 1000))


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str | None = None, timeout: int = 60):
        self.model, self.api_key, self.timeout = model, api_key, timeout
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1500,
                 temperature: float = 0.0) -> LLMResponse:
        t0 = time.perf_counter()
        res = _post(f"{self.base_url}/chat/completions", {"Authorization": f"Bearer {self.api_key}"},
                    {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                     "messages": [{"role": "system", "content": system}, *messages]}, self.timeout)
        text = res["choices"][0]["message"].get("content") or ""
        u = res.get("usage", {})
        return LLMResponse(text, res.get("model", self.model), int(u.get("prompt_tokens", 0)),
                           int(u.get("completion_tokens", 0)), int((time.perf_counter() - t0) * 1000))


class OllamaProvider:
    """Local models served by Ollama (no data leaves the machine)."""

    name = "ollama"

    def __init__(self, model: str, base_url: str | None = None, timeout: int = 120):
        self.model, self.timeout = model, timeout
        self.base_url = (base_url or "http://localhost:11434").rstrip("/")

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int = 1500,
                 temperature: float = 0.0) -> LLMResponse:
        t0 = time.perf_counter()
        res = _post(f"{self.base_url}/api/chat", {},
                    {"model": self.model, "stream": False, "format": "json",
                     "options": {"temperature": temperature, "num_predict": max_tokens},
                     "messages": [{"role": "system", "content": system}, *messages]}, self.timeout)
        return LLMResponse(res.get("message", {}).get("content", ""), self.model, int(res.get("prompt_eval_count", 0)),
                           int(res.get("eval_count", 0)), int((time.perf_counter() - t0) * 1000))


def build_provider(settings: Any) -> LLMProvider:
    p = settings.llm_provider
    if p == "none":
        return NullProvider()
    if not settings.llm_model:
        raise ValueError("LLM_MODEL must be set when LLM_PROVIDER is not 'none'")
    if p == "anthropic":
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required for the anthropic provider")
        return AnthropicProvider(settings.llm_model, settings.llm_api_key, settings.llm_base_url, settings.llm_timeout_s)
    if p == "openai":
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required for the openai provider")
        return OpenAIProvider(settings.llm_model, settings.llm_api_key, settings.llm_base_url, settings.llm_timeout_s)
    if p == "ollama":
        return OllamaProvider(settings.llm_model, settings.llm_base_url, settings.llm_timeout_s)
    raise ValueError(f"unknown LLM provider {p}")
