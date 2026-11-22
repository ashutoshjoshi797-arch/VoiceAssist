"""Chat model providers and a factory that builds them from settings."""
from __future__ import annotations

from .. import secrets_store
from .base import AuthError, ChatProvider, LLMError, LocalEngineUnavailable, ModelMissing
from .cloud import AnthropicProvider, GeminiProvider, OpenAIProvider
from .local import RECOMMENDED_MODELS, LocalOpenAICompatProvider, OllamaProvider

__all__ = [
    "AuthError", "ChatProvider", "LLMError", "LocalEngineUnavailable", "ModelMissing",
    "RECOMMENDED_MODELS", "build_cloud", "build_local",
]


def build_cloud(settings) -> ChatProvider:
    on = settings.get("online")
    provider = on["provider"]
    model = on["models"].get(provider)
    key = secrets_store.get_key(provider)
    if not key:
        raise AuthError(f"No API key configured for {provider}.", hint="Add it in Settings → Online, or set the environment variable.")
    kw = dict(max_tokens=int(on.get("max_output_tokens", 1024)), timeout=float(on.get("request_timeout_s", 30)))
    if provider == "anthropic":
        return AnthropicProvider(model, key, **kw)
    if provider == "openai":
        return OpenAIProvider(model, key, **kw)
    if provider == "gemini":
        return GeminiProvider(model, key, **kw)
    raise LLMError(f"Unknown provider {provider}")


def build_local(settings) -> ChatProvider:
    lo = settings.get("local")
    if lo["engine"] == "openai_compat":
        return LocalOpenAICompatProvider(lo["model"], lo["openai_compat_url"], allow_lan=bool(lo.get("allow_lan")))
    return OllamaProvider(
        lo["model"], base_url=lo["ollama_url"], num_ctx=int(lo.get("num_ctx", 4096)),
        temperature=float(lo.get("temperature", 0.7)), keep_alive=str(lo.get("keep_alive", "30m")),
        disable_thinking=bool(lo.get("disable_thinking", True)), allow_lan=bool(lo.get("allow_lan")),
    )
