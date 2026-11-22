"""Common interface for every chat model (cloud or local)."""
from __future__ import annotations

import threading
from typing import Iterator


class LLMError(Exception):
    """An error with a message that is safe and useful to show to the user."""

    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class AuthError(LLMError):
    pass


class LocalEngineUnavailable(LLMError):
    pass


class ModelMissing(LLMError):
    pass


class ChatProvider:
    #: "cloud" or "local"
    kind = "local"
    #: provider id, e.g. "anthropic", "ollama"
    name = "base"
    label = "Base"

    def __init__(self, model: str):
        self.model = model

    def stream_chat(
        self, system: str, messages: list[dict], cancel_event: threading.Event
    ) -> Iterator[str]:
        """Yield text deltas. messages = [{"role": "user"|"assistant", "content": str}, ...]"""
        raise NotImplementedError

    def describe(self) -> dict:
        return {"kind": self.kind, "provider": self.name, "label": self.label, "model": self.model}


def strip_think(chunks: Iterator[str]) -> Iterator[str]:
    """Remove <think>...</think> reasoning blocks some local models emit."""
    buf = ""
    in_think = False
    for c in chunks:
        buf += c
        out = ""
        while buf:
            if in_think:
                end = buf.find("</think>")
                if end < 0:
                    buf = buf[-8:]  # keep a tail in case the closing tag is split
                    break
                buf = buf[end + 8 :]
                in_think = False
            else:
                start = buf.find("<think>")
                if start < 0:
                    # hold back a possible partial "<think" at the end
                    keep = 0
                    for i in range(1, 7):
                        if buf.endswith("<think>"[:i]):
                            keep = i
                    out += buf[: len(buf) - keep]
                    buf = buf[len(buf) - keep :]
                    break
                out += buf[:start]
                buf = buf[start + 7 :]
                in_think = True
        if out:
            yield out
    if buf and not in_think:
        yield buf
