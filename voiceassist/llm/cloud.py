"""Helpers shared by chat providers."""
from __future__ import annotations


def merge_roles(messages: list[dict]) -> list[dict]:
    """Ensure strictly alternating roles starting with 'user'."""
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
