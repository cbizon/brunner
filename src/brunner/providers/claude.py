from __future__ import annotations

import json
import uuid
from typing import Any

from brunner.errors import ConfigurationError
from brunner.providers.base import (
    ProviderActivity,
    ProviderCommand,
    ProviderFailure,
    ProviderModelObservation,
    ProviderObservation,
    ProviderRunContext,
    ProviderSettings,
    error_text,
    record_strings,
    response_from_record,
    validate_effort,
)
from brunner.providers.codex import (
    RETRYABLE_RESUME_ERRORS,
    TERMINAL_ERROR_FRAGMENTS,
    TERMINAL_HTTP_STATUSES,
)
from brunner.usage import normalize_claude_usage, sum_usage


CLAUDE_EFFORTS = ("low", "medium", "high", "max")
CLAUDE_DISALLOWED_TOOLS = ("WebSearch", "WebFetch")
CLAUDE_READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
CLAUDE_TERMINAL_HARNESS_ERROR_FRAGMENTS = (
    "failed to create sandbox",
    "failed to initialize sandbox",
    "sandbox is not available",
    "sandbox is unavailable",
    "sandboxing is not supported",
    "unshare: unshare failed: operation not permitted",
)
CLAUDE_PERMISSION_DENIAL_FRAGMENTS = (
    "but you haven't granted it yet",
    "but you have not granted it yet",
)


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
            "--disallowedTools",
            ",".join(CLAUDE_DISALLOWED_TOOLS),
            "--no-chrome",
            "--disable-slash-commands",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--model",
            settings.model,
        ]
        if context.read_only:
            tools = ",".join(CLAUDE_READ_ONLY_TOOLS)
            command.extend(
                (
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    tools,
                    "--allowedTools",
                    tools,
                )
            )
        else:
            command.append("--dangerously-skip-permissions")
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

    def model_observations(
        self,
        record: dict[str, Any],
    ) -> tuple[ProviderModelObservation, ...]:
        if (
            record.get("type") != "assistant"
            or record.get("parent_tool_use_id") not in {None, ""}
        ):
            return ()
        message = record.get("message")
        if not isinstance(message, dict):
            return ()
        model = message.get("model")
        if not isinstance(model, str) or not model.strip():
            return ()
        normalized = model.strip()
        if normalized == "<synthetic>":
            return ()
        return (
            ProviderModelObservation(
                model=normalized,
                source="assistant.message.model",
            ),
        )

    def models_match(
        self,
        requested: str,
        observed: str,
    ) -> bool:
        requested_parts = _model_parts(requested)
        observed_parts = _model_parts(observed)
        if requested_parts and requested_parts[-1] == "latest":
            requested_parts = requested_parts[:-1]
        return bool(
            requested_parts
            and observed_parts[: len(requested_parts)] == requested_parts
        )

    def activity_observations(
        self,
        record: dict[str, Any],
    ) -> tuple[ProviderActivity, ...]:
        message = record.get("message")
        if not isinstance(message, dict):
            return ()
        content = message.get("content")
        if not isinstance(content, list):
            return ()
        observations = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if record.get("type") == "assistant" and item.get("type") == (
                "tool_use"
            ):
                activity_id = item.get("id")
                phase = "start"
                label = item.get("name")
            elif record.get("type") == "user" and item.get("type") == (
                "tool_result"
            ):
                activity_id = item.get("tool_use_id")
                phase = "end"
                label = None
            else:
                continue
            if isinstance(activity_id, str):
                observations.append(
                    ProviderActivity(
                        activity_id=activity_id,
                        phase=phase,
                        label=str(label) if label is not None else None,
                    )
                )
        return tuple(observations)

    def parse_usage(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        values = [
            normalize_claude_usage(record["usage"])
            for record in records
            if record.get("type") == "result"
            and isinstance(record.get("usage"), dict)
        ]
        if not values:
            raise ValueError("no Claude usage found")
        return sum_usage(values, provider=self.name)

    def classify_failure(
        self,
        records: list[dict[str, Any]],
        stderr: str,
    ) -> ProviderFailure | None:
        summary = stderr.strip()
        terminal = _is_terminal_harness_failure(stderr)
        api_status = None
        reason = "harness_configuration_error" if terminal else None
        wait_category = None
        retry_at_epoch = None
        for record in reversed(records):
            status = record.get("api_error_status")
            if isinstance(status, int) and api_status is None:
                api_status = status
            rate_limit = record.get("rate_limit_info")
            rejected_rate_limit = (
                isinstance(rate_limit, dict)
                and rate_limit.get("status") == "rejected"
            )
            if isinstance(rate_limit, dict):
                if reason is None:
                    value = rate_limit.get("overageDisabledReason")
                    if isinstance(value, str):
                        reason = value
                if retry_at_epoch is None:
                    reset = (
                        rate_limit.get("resetsAt")
                        or rate_limit.get("resetAt")
                        or rate_limit.get("resets_at")
                    )
                    if isinstance(reset, (int, float)):
                        retry_at_epoch = float(reset)
                if (
                    rejected_rate_limit
                    and isinstance(reason, str)
                    and reason.startswith("org_level_disabled")
                ):
                    wait_category = "subscription_wait"
            text = error_text(record)
            lowered = text.lower()
            is_error = (
                record.get("is_error") is True
                or bool(record.get("error"))
                or record.get("type") == "error"
                or (
                    record.get("type") == "rate_limit_event"
                    and rejected_rate_limit
                )
                or status is not None
            )
            if is_error and text and not summary:
                summary = text
            harness_failure = _is_terminal_harness_failure(text)
            if is_error and (
                status in TERMINAL_HTTP_STATUSES
                or any(fragment in lowered for fragment in TERMINAL_ERROR_FRAGMENTS)
                or harness_failure
            ):
                terminal = True
                if harness_failure:
                    reason = "harness_configuration_error"
        if not summary and not terminal:
            return None
        return ProviderFailure(
            summary=summary[-2000:] or "Claude request failed",
            terminal=terminal,
            api_status=api_status,
            reason=reason,
            wait_category=wait_category,
            retry_at_epoch=retry_at_epoch,
        )

    def resume_is_unavailable(
        self,
        records: list[dict[str, Any]],
        stderr: str,
    ) -> bool:
        lowered = "\n".join(
            (
                stderr,
                *(
                    text
                    for record in records
                    for text in record_strings(record)
                ),
            )
        ).lower()
        return any(fragment in lowered for fragment in RETRYABLE_RESUME_ERRORS)


def _model_parts(value: str) -> tuple[str, ...]:
    normalized = value.strip().lower().replace("_", "-")
    if normalized.startswith("claude-"):
        normalized = normalized.removeprefix("claude-")
    return tuple(part for part in normalized.split("-") if part)


def _is_terminal_harness_failure(value: str) -> bool:
    lowered = value.lower()
    return any(
        fragment in lowered
        for fragment in CLAUDE_TERMINAL_HARNESS_ERROR_FRAGMENTS
    ) or (
        "requested permissions to use" in lowered
        and any(
            fragment in lowered
            for fragment in CLAUDE_PERMISSION_DENIAL_FRAGMENTS
        )
    )
