"""Chat model providers: the product's cloud AI (via Supabase) and the on-device engines."""
from __future__ import annotations

from .base import AuthError, ChatProvider, LLMError, LocalEngineUnavailable, ModelMissing
from .local import RECOMMENDED_MODELS, LocalOpenAICompatProvider, OllamaProvider

__all__ = [
    "AuthError", "ChatProvider", "LLMError", "LocalEngineUnavailable", "ModelMissing",
    "RECOMMENDED_MODELS", "build_cloud", "build_local",
]


def build_cloud(settings, accounts, user_id) -> ChatProvider:
    from ..cloud import VoiceAssistCloudProvider
    if accounts is None or not accounts.configured:
        raise AuthError("This copy of VoiceAssist isn't connected to the VoiceAssist cloud.")
    on = settings.get("online")
    return VoiceAssistCloudProvider(accounts, user_id, max_tokens=int(on.get("max_output_tokens", 1024)),
                                    timeout=float(on.get("request_timeout_s", 30)))


def build_local(settings) -> ChatProvider:
    lo = settings.get("local")
    if lo["engine"] == "openai_compat":
        return LocalOpenAICompatProvider(lo["model"], lo["openai_compat_url"], allow_lan=bool(lo.get("allow_lan")))
    return OllamaProvider(
        lo["model"], base_url=lo["ollama_url"], num_ctx=int(lo.get("num_ctx", 4096)),
        temperature=float(lo.get("temperature", 0.7)), keep_alive=str(lo.get("keep_alive", "30m")),
        disable_thinking=bool(lo.get("disable_thinking", True)), allow_lan=bool(lo.get("allow_lan")),
    )
