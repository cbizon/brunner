from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from brunner.errors import ConfigurationError


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class ProviderSettings:
    provider: str
    model: str
    effort: str | None = None
    allowed_efforts: tuple[str, ...] | None = None
    provider_id: str | None = None
    provider_name: str = "OpenAI-compatible provider"
    base_url: str | None = None
    environment_key: str = "OPENAI_API_KEY"
    extra_environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRunContext:
    workspace: Path
    transcript_dir: Path
    final_schema_path: Path
    final_output_path: Path
    persist_session: bool
    resume_session: bool
    session_id: str | None
    executable: str | None = None
    read_only: bool = False


@dataclass(frozen=True)
class ProviderCommand:
    command: tuple[str, ...]
    environment: dict[str, str]
    prompt_on_stdin: bool = True


@dataclass(frozen=True)
class ProviderObservation:
    terminal: bool = False
    succeeded: bool = False
    final_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderActivity:
    activity_id: str
    phase: str
    category: str = "foreground_tool"
    label: str | None = None


@dataclass(frozen=True)
class ProviderFailure:
    summary: str
    terminal: bool
    api_status: int | None = None
    reason: str | None = None
    wait_category: str | None = None
    retry_at_epoch: float | None = None


class ProviderAdapter(Protocol):
    name: str

    def validate_settings(
        self,
        settings: ProviderSettings,
    ) -> ProviderSettings: ...

    def new_session_id(self) -> str | None: ...

    def build_command(
        self,
        settings: ProviderSettings,
        context: ProviderRunContext,
    ) -> ProviderCommand: ...

    def observe_record(
        self,
        record: dict[str, Any],
    ) -> ProviderObservation: ...

    def activity_observations(
        self,
        record: dict[str, Any],
    ) -> tuple[ProviderActivity, ...]: ...

    def parse_usage(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def classify_failure(
        self,
        records: list[dict[str, Any]],
        stderr: str,
    ) -> ProviderFailure | None: ...

    def resume_is_unavailable(self, stderr: str) -> bool: ...


def validate_effort(
    settings: ProviderSettings,
    *,
    provider_efforts: tuple[str, ...] = EFFORT_LEVELS,
) -> ProviderSettings:
    effort = settings.effort
    if effort is None:
        return settings
    normalized = effort.strip().lower()
    if normalized not in EFFORT_LEVELS:
        raise ConfigurationError(
            f"unsupported effort {effort!r}; choose from {EFFORT_LEVELS}"
        )
    allowed = settings.allowed_efforts or provider_efforts
    if normalized not in allowed:
        raise ConfigurationError(
            f"{settings.provider} model {settings.model!r} does not support "
            f"effort {normalized!r}; choose from {allowed}"
        )
    return ProviderSettings(
        **{
            **settings.__dict__,
            "effort": normalized,
        }
    )


def response_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("structured_output", "result"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return None


def record_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [
            text
            for item in value
            for text in record_strings(item)
        ]
    if isinstance(value, dict):
        return [
            text
            for item in value.values()
            for text in record_strings(item)
        ]
    return []


def error_text(record: dict[str, Any]) -> str:
    for key in ("result", "error"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            texts = [
                str(item["text"]).strip()
                for item in content
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ]
            if texts:
                return "\n".join(texts)
    return " ".join(record_strings(record)).strip()
