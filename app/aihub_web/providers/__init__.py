"""Streaming adapters: each turns one provider's wire format into the same few events."""

from collections.abc import Mapping

import httpx2

from ..config import Config
from .anthropic import AnthropicProvider
from .base import (
    Event,
    Finish,
    GenRequest,
    Provider,
    ProviderError,
    Text,
    Thinking,
    Usage,
)
from .ollama import OllamaProvider
from .openrouter import OpenRouterProvider


def build_providers(
    cfg: Config, client: httpx2.AsyncClient, environ: Mapping[str, str]
) -> dict[str, Provider]:
    return {
        "ollama": OllamaProvider(cfg.ollama, client),
        "openrouter": OpenRouterProvider(cfg.openrouter, client, environ),
        "anthropic": AnthropicProvider(cfg.anthropic, client, environ),
    }


__all__ = [
    "Event",
    "Finish",
    "GenRequest",
    "Provider",
    "ProviderError",
    "Text",
    "Thinking",
    "Usage",
    "build_providers",
]
