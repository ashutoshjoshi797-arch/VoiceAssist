"""Cloud chat providers: Anthropic Claude, OpenAI, Google Gemini — streaming over HTTPS.

Implemented with the standard library so the app has no native dependencies for online mode.
Base URLs can be overridden via environment variables (used by the tests to point at a fake server):
  VOICEASSIST_ANTHROPIC_BASE, VOICEASSIST_OPENAI_BASE, VOICEASSIST_GEMINI_BASE
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from typing import Iterator

from .. import netutil
from .base import AuthError, ChatProvider, LLMError


def _base(var: str, default: str) -> str:
    return os.environ.get(var, default).rstrip("/")


def _raise_for_status(e: netutil.HTTPStatusError, label: str):
    msg = e.body
    try:
        j = json.loads(e.body)
        err = j.get("error", j)
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        pass
    if e.status in (401, 403):
        raise AuthError(f"{label} rejected the API key ({e.status}).", hint="Check the key in Settings → Online.") from e
    if e.status == 404:
        raise LLMError(f"{label}: model not found — {msg}", hint="Pick a different model in Settings → Online.") from e
    if e.status == 429:
        raise LLMError(f"{label} rate limit or quota reached — {msg}") from e
    raise LLMError(f"{label} error {e.status}: {msg}") from e


def merge_roles(messages: list[dict]) -> list[dict]:
    """Ensure strictly alternating roles starting with 'user' (required by Anthropic/Gemini)."""
    out: list[dict] = []
    for m in messages:
        role = "assistant" if m["role"] == "assistant" else "user"
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


class AnthropicProvider(ChatProvider):
    kind, name, label = "cloud", "anthropic", "Claude (Anthropic)"

    def __init__(self, model: str, api_key: str, max_tokens: int = 1024, timeout: float = 30):
        super().__init__(model)
        self.api_key, self.max_tokens, self.timeout = api_key, max_tokens, timeout
        self.base = _base("VOICEASSIST_ANTHROPIC_BASE", "https://api.anthropic.com")

    def stream_chat(self, system, messages, cancel_event: threading.Event) -> Iterator[str]:
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": merge_roles(messages),
            "stream": True,
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        try:
            s = netutil.open_request(
                "POST", f"{self.base}/v1/messages", headers=headers, json_body=body,
                timeout=self.timeout, cancel_event=cancel_event,
            )
        except netutil.HTTPStatusError as e:
            _raise_for_status(e, "Claude")
        completed = False
        for event, data in s.iter_sse():
            if event == "error":
                raise LLMError(f"Claude stream error: {data[:300]}")
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if j.get("type") == "content_block_delta":
                d = j.get("delta", {})
                if d.get("type") == "text_delta" and d.get("text"):
                    yield d["text"]
            elif j.get("type") == "message_stop":
                completed = True
                break
        if not completed:
            raise netutil.NetworkError("Claude stream ended before the answer was complete")


class OpenAIProvider(ChatProvider):
    kind, name, label = "cloud", "openai", "OpenAI"

    def __init__(self, model: str, api_key: str | None, max_tokens: int = 1024, timeout: float = 30,
                 base_url: str | None = None, local: bool = False, allow_lan: bool = False):
        super().__init__(model)
        self.api_key, self.max_tokens, self.timeout = api_key, max_tokens, timeout
        self.base = (base_url or _base("VOICEASSIST_OPENAI_BASE", "https://api.openai.com/v1")).rstrip("/")
        self._local, self._allow_lan = local, allow_lan
        if local:
            self.kind, self.name, self.label = "local", "openai_compat", "Local server (OpenAI-compatible)"

    def stream_chat(self, system, messages, cancel_event: threading.Event) -> Iterator[str]:
        msgs = [{"role": "system", "content": system}] + merge_roles(messages)
        body = {"model": self.model, "messages": msgs, "stream": True}
        if self._local:
            body["max_tokens"] = self.max_tokens
        else:
            body["max_completion_tokens"] = self.max_tokens
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            s = netutil.open_request(
                "POST", f"{self.base}/chat/completions", headers=headers, json_body=body,
                timeout=self.timeout, cancel_event=cancel_event,
                local_only=self._local, allow_lan=self._allow_lan,
            )
        except netutil.HTTPStatusError as e:
            _raise_for_status(e, self.label)
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


class GeminiProvider(ChatProvider):
    kind, name, label = "cloud", "gemini", "Gemini (Google)"

    def __init__(self, model: str, api_key: str, max_tokens: int = 1024, timeout: float = 30):
        super().__init__(model)
        self.api_key, self.max_tokens, self.timeout = api_key, max_tokens, timeout
        self.base = _base("VOICEASSIST_GEMINI_BASE", "https://generativelanguage.googleapis.com")

    def stream_chat(self, system, messages, cancel_event: threading.Event) -> Iterator[str]:
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in merge_roles(messages)
        ]
        # Note: newer Gemini models reject temperature/top_p/top_k, so none are sent.
        body = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        model = urllib.parse.quote(self.model, safe="")
        url = f"{self.base}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        try:
            s = netutil.open_request(
                "POST", url, headers={"x-goog-api-key": self.api_key}, json_body=body,
                timeout=self.timeout, cancel_event=cancel_event,
            )
        except netutil.HTTPStatusError as e:
            _raise_for_status(e, "Gemini")
        completed = False
        for _event, data in s.iter_sse():
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if "error" in j:
                raise LLMError(f"Gemini stream error: {j['error']}")
            for cand in j.get("candidates", []):
                if cand.get("finishReason"):
                    completed = True
                for part in (cand.get("content") or {}).get("parts", []):
                    if part.get("text") and not part.get("thought"):
                        yield part["text"]
        if not completed:
            raise netutil.NetworkError("Gemini stream ended before the answer was complete")
