from __future__ import annotations

import json
import uuid
from typing import Any

from brunner.errors import ConfigurationError
from brunner.providers.base import (
    ProviderCommand,
    ProviderFailure,
    ProviderObservation,
    ProviderRunContext,
    ProviderSettings,
    error_text,
    response_from_record,
    validate_effort,
)
from brunner.providers.codex import (
    RETRYABLE_RESUME_ERRORS,
    TERMINAL_ERROR_FRAGMENTS,
    TERMINAL_HTTP_STATUSES,
)
from brunner.usage import normalized_usage, sum_usage


CLAUDE_EFFORTS = ("low", "medium", "high", "max")
CLAUDE_DISALLOWED_TOOLS = ("WebSearch", "WebFetch")


class ClaudeAdapter:
    name = "claude"

    def validate_settings(
        self,
        settings: ProviderSettings,
    ) -> ProviderSettings:
        if settings.provider != self.name:
            raise ConfigurationError("Claude adapter requires provider='claude'")
        return validate_effort(
            settings,
            provider_efforts=CLAUDE_EFFORTS,
        )

    def new_session_id(self) -> str | None:
        return str(uuid.uuid4())

    def build_command(
        self,
        settings: ProviderSettings,
        context: ProviderRunContext,
    ) -> ProviderCommand:
        settings = self.validate_settings(settings)
        if context.persist_session and context.session_id is None:
            raise ConfigurationError(
                "persistent Claude sessions require a session id"
            )
        executable = context.executable or "claude"
        schema = json.dumps(
            json.loads(context.final_schema_path.read_text()),
            separators=(",", ":"),
        )
        command = [
            executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--disallowedTools",
            ",".join(CLAUDE_DISALLOWED_TOOLS),
            "--no-chrome",
            "--disable-slash-commands",
            "--model",
            settings.model,
        ]
        if settings.effort is not None:
            command.extend(("--effort", settings.effort))
        command.extend(("--json-schema", schema))
        if context.persist_session:
            flag = "--resume" if context.resume_session else "--session-id"
            command.extend((flag, str(context.session_id)))
        else:
            command.append("--no-session-persistence")
        return ProviderCommand(
            command=tuple(command),
            environment=dict(settings.extra_environment),
        )

    def observe_record(
        self,
        record: dict[str, Any],
    ) -> ProviderObservation:
        final_response = response_from_record(record)
        if record.get("type") != "result":
            return ProviderObservation(final_response=final_response)
        succeeded = (
            record.get("is_error") is not True
            and record.get("api_error_status") is None
        )
        return ProviderObservation(
            terminal=True,
            succeeded=succeeded,
            final_response=final_response,
        )

    def parse_usage(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, int]:
        values = [
            normalized_usage(record["usage"])
            for record in records
            if record.get("type") == "result"
            and isinstance(record.get("usage"), dict)
        ]
        if not values:
            raise ValueError("no Claude usage found")
        return sum_usage(values)

    def classify_failure(
        self,
        records: list[dict[str, Any]],
        stderr: str,
    ) -> ProviderFailure | None:
        summary = stderr.strip()
        terminal = False
        api_status = None
        reason = None
        for record in reversed(records):
            status = record.get("api_error_status")
            if isinstance(status, int) and api_status is None:
                api_status = status
            rate_limit = record.get("rate_limit_info")
            if isinstance(rate_limit, dict) and reason is None:
                value = rate_limit.get("overageDisabledReason")
                if isinstance(value, str):
                    reason = value
            text = error_text(record)
            lowered = text.lower()
            is_error = (
                record.get("is_error") is True
                or bool(record.get("error"))
                or record.get("type") in {"error", "rate_limit_event"}
                or status is not None
            )
            if is_error and text and not summary:
                summary = text
            if is_error and (
                status in TERMINAL_HTTP_STATUSES
                or reason == "org_level_disabled_until"
                or any(fragment in lowered for fragment in TERMINAL_ERROR_FRAGMENTS)
            ):
                terminal = True
        if not summary and not terminal:
            return None
        return ProviderFailure(
            summary=summary[-2000:] or "Claude request failed",
            terminal=terminal,
            api_status=api_status,
            reason=reason,
        )

    def resume_is_unavailable(self, stderr: str) -> bool:
        lowered = stderr.lower()
        return any(fragment in lowered for fragment in RETRYABLE_RESUME_ERRORS)
