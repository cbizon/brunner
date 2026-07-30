from brunner.providers.base import (
    ProviderAdapter,
    ProviderCommand,
    ProviderFailure,
    ProviderObservation,
    ProviderRunContext,
    ProviderSettings,
)
from brunner.providers.claude import ClaudeAdapter
from brunner.providers.codex import CodexAdapter


PROVIDERS: dict[str, ProviderAdapter] = {
    "codex": CodexAdapter(),
    "claude": ClaudeAdapter(),
}


def get_provider(name: str) -> ProviderAdapter:
    try:
        return PROVIDERS[name]
    except KeyError as error:
        raise ValueError(f"unsupported provider: {name}") from error


__all__ = [
    "ClaudeAdapter",
    "CodexAdapter",
    "ProviderAdapter",
    "ProviderCommand",
    "ProviderFailure",
    "ProviderObservation",
    "ProviderRunContext",
    "ProviderSettings",
    "get_provider",
]
