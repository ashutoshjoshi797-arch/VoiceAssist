"""Local chat models. Ollama is the primary engine; any OpenAI-compatible server on localhost
(llama.cpp `llama-server`, LM Studio, …) is also supported (LocalOpenAICompatProvider).

Every request here is checked to target loopback (see netutil.check_local_url), so local mode
cannot send conversation data off the machine.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Iterator

from .. import netutil
from .base import ChatProvider, LLMError, LocalEngineUnavailable, ModelMissing, strip_think
from .cloud import merge_roles

log = logging.getLogger(__name__)

RECOMMENDED_MODELS = [
    {"tag": "llama3.2:3b", "ram_gb": 4, "size_gb": 2.0, "note": "Recommended — good answers, fast (8 GB+ RAM)"},
    {"tag": "gemma3:4b", "ram_gb": 5, "size_gb": 3.3, "note": "Best quality, a bit slower (12 GB+ RAM)"},
    {"tag": "llama3.2:1b", "ram_gb": 2, "size_gb": 1.3, "note": "For computers with 4–8 GB RAM"},
    {"tag": "qwen2.5:1.5b", "ram_gb": 2, "size_gb": 1.0, "note": "Small, multilingual"},
    {"tag": "qwen2.5:0.5b", "ram_gb": 1, "size_gb": 0.4, "note": "Tiny and very fast, basic answers only"},
]


class OllamaProvider(ChatProvider):
    kind, name, label = "local", "ollama", "Ollama (on this computer)"

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434", num_ctx: int = 4096,
                 temperature: float = 0.7, keep_alive: str = "30m", disable_thinking: bool = True,
                 allow_lan: bool = False, timeout: float = 300):
        super().__init__(model)
        self.base = base_url.rstrip("/")
        self.num_ctx, self.temperature, self.keep_alive = num_ctx, temperature, keep_alive
        self.disable_thinking, self.allow_lan, self.timeout = disable_thinking, allow_lan, timeout

    # -- helpers --------------------------------------------------------------
    def _req(self, method, path, body=None, timeout=None, cancel_event=None):
        return netutil.open_request(
            method, f"{self.base}{path}", json_body=body, timeout=timeout or self.timeout,
            cancel_event=cancel_event, local_only=True, allow_lan=self.allow_lan,
        )

    def _unavailable(self, e) -> LocalEngineUnavailable:
        return LocalEngineUnavailable(
            f"Ollama is not running at {self.base}.",
            hint="Click \"Set up offline AI\" to install or start it (Settings → Local AI).",
        )

    def list_models(self) -> list[str]:
        try:
            j = json.loads(self._req("GET", "/api/tags", timeout=3).read_all() or b"{}")
        except (netutil.NetworkError, ConnectionError) as e:
            raise self._unavailable(e) from e
        return sorted(m.get("name") or m.get("model") for m in j.get("models", []))

    def is_running(self) -> bool:
        try:
            self._req("GET", "/api/version", timeout=2).read_all()
            return True
        except Exception:
            return False

    def has_model(self) -> bool:
        names = self.list_models()
        want = self.model if ":" in self.model else self.model + ":latest"
        return want in names or self.model in names

    def preload(self) -> None:
        """Load the model into memory so the first reply is fast."""
        if not self.has_model():
            raise ModelMissing(
                f"Local model '{self.model}' is not installed in Ollama.",
                hint="Click \"Set up offline AI\" to download it (Settings → Local AI).",
            )
        self._req("POST", "/api/generate", {"model": self.model, "prompt": "", "keep_alive": self.keep_alive},
                  timeout=600).read_all()

    # -- chat -----------------------------------------------------------------
    def stream_chat(self, system, messages, cancel_event: threading.Event) -> Iterator[str]:
        return strip_think(self._stream(system, messages, cancel_event))

    def _stream(self, system, messages, cancel_event) -> Iterator[str]:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + merge_roles(messages),
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": int(self.num_ctx), "temperature": float(self.temperature)},
        }
        if self.disable_thinking:
            body["think"] = False
        try:
            s = self._req("POST", "/api/chat", body, cancel_event=cancel_event)
        except netutil.HTTPStatusError as e:
            if e.status == 400 and "think" in e.body.lower() and body.pop("think", None) is not None:
                # Older Ollama or a model without a thinking switch: retry without it.
                s = self._req("POST", "/api/chat", body, cancel_event=cancel_event)
            elif e.status == 404 or "not found" in e.body.lower():
                raise ModelMissing(
                    f"Local model '{self.model}' is not installed in Ollama.",
                    hint="Click \"Set up offline AI\" to download it (Settings → Local AI).",
                ) from e
            else:
                raise LLMError(f"Ollama error {e.status}: {e.body[:300]}") from e
        except netutil.NetworkError as e:
            raise self._unavailable(e) from e
        completed = False
        for line in s.iter_lines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("error"):
                raise LLMError(f"Ollama: {j['error']}")
            piece = (j.get("message") or {}).get("content")
            if piece:
                yield piece
            if j.get("done"):
                completed = True
                break
        if not completed:
            raise LLMError("The local model stopped unexpectedly (Ollama may have crashed or run out of memory).",
                           hint="Try a smaller model or lower the context window in Settings → Local AI.")


class LocalOpenAICompatProvider(ChatProvider):
    """A model server on this computer that speaks the OpenAI chat format (llama-server, LM Studio)."""
    kind, name, label = "local", "openai_compat", "Local server (OpenAI-compatible)"

    def __init__(self, model: str, base_url: str, max_tokens: int = 1024, allow_lan: bool = False):
        super().__init__(model)
        self.base, self.max_tokens, self.timeout, self._allow_lan = base_url.rstrip("/"), max_tokens, 300, allow_lan

    def _stream(self, system, messages, cancel_event) -> Iterator[str]:
        body = {"model": self.model, "messages": [{"role": "system", "content": system}] + merge_roles(messages),
                "stream": True, "max_tokens": self.max_tokens}
        try:
            s = netutil.open_request("POST", f"{self.base}/chat/completions", json_body=body, timeout=self.timeout,
                                     cancel_event=cancel_event, local_only=True, allow_lan=self._allow_lan)
        except netutil.HTTPStatusError as e:
            raise LLMError(f"{self.label} error {e.status}: {e.body[:200]}") from e
        completed = False
        for _event, data in s.iter_sse():
            if data.strip() == "[DONE]":
                completed = True
                break
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if "error" in j:
                raise LLMError(f"{self.label} stream error: {j['error']}")
            for ch in j.get("choices", []):
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    yield piece
        if not completed:
            raise netutil.NetworkError(f"{self.label} stream ended before the answer was complete")

    def stream_chat(self, system, messages, cancel_event):
        try:
            yield from strip_think(self._stream(system, messages, cancel_event))
        except netutil.NetworkError as e:
            raise LocalEngineUnavailable(
                f"No local model server is answering at {self.base}.",
                hint="Start llama-server / LM Studio, or switch Settings → Local → Engine to Ollama.",
            ) from e

    def list_models(self) -> list[str]:
        try:
            j = netutil.request_json("GET", f"{self.base}/models", timeout=3, local_only=True, allow_lan=self._allow_lan)
            return sorted(m.get("id") for m in j.get("data", []))
        except Exception as e:
            raise LocalEngineUnavailable(f"No local model server at {self.base}.") from e

    def is_running(self) -> bool:
        try:
            self.list_models()
            return True
        except Exception:
            return False

    def preload(self) -> None:
        return None
