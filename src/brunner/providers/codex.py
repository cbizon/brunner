from __future__ import annotations

import json
import os
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
from brunner.usage import normalize_codex_usage, sum_usage


CODEX_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "enable_mcp_apps",
    "in_app_browser",
    "remote_plugin",
)
RETRYABLE_RESUME_ERRORS = (
    "no session",
    "session not found",
    "no saved",
    "unknown session",
    "could not find session",
)
TERMINAL_ERROR_FRAGMENTS = (
    "usage credits are required",
    "credit balance is too low",
    "invalid api key",
    "authentication failed",
    "oauth token has expired",
    "not authorized to use",
    "does not exist or you do not have access",
    "model_not_found",
)
TERMINAL_HTTP_STATUSES = frozenset({400, 401, 403, 404})
CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "computer_tool_call",
        "file_change",
        "mcp_tool_call",
        "web_search",
    }
)


class CodexAdapter:
    name = "codex"

    def validate_settings(
        self,
        settings: ProviderSettings,
    ) -> ProviderSettings:
        if settings.provider != self.name:
            raise ConfigurationError("Codex adapter requires provider='codex'")
        settings = validate_effort(settings)
        if settings.provider_id and not settings.base_url:
            raise ConfigurationError(
                "custom Codex providers require a base URL"
            )
        return settings

    def new_session_id(self) -> str | None:
        return None

    def build_command(
        self,
        settings: ProviderSettings,
        context: ProviderRunContext,
    ) -> ProviderCommand:
        settings = self.validate_settings(settings)
        executable = context.executable or "codex"
        command = [executable, "exec"]
        if context.resume_session:
            command.append("resume")
        command.extend(
            [
                "--json",
                "--ignore-user-config",
                "--strict-config",
                "-c",
                'web_search="disabled"',
                "-c",
                "allow_login_shell=false",
                "-c",
                (
                    "shell_environment_policy.set.PATH="
                    f"{json.dumps(os.environ['PATH'])}"
                ),
                "--skip-git-repo-check",
                *[
                    argument
                    for feature in CODEX_DISABLED_FEATURES
                    for argument in ("--disable", feature)
                ],
                "--output-schema",
                str(context.final_schema_path),
                "--output-last-message",
                str(context.final_output_path),
                "--model",
                settings.model,
            ]
        )
        if context.read_only:
            command.extend(("--sandbox", "read-only"))
        else:
            command.extend(("--sandbox", "workspace-write"))
        if settings.effort is not None:
            command.extend(
                ("-c", f"model_reasoning_effort={json.dumps(settings.effort)}")
            )
        if settings.provider_id:
            provider_key = f"model_providers.{settings.provider_id}"
            command.extend(
                (
                    "-c",
                    f"model_provider={json.dumps(settings.provider_id)}",
                    "-c",
                    f"{provider_key}.name={json.dumps(settings.provider_name)}",
                    "-c",
                    f"{provider_key}.base_url={json.dumps(settings.base_url)}",
                    "-c",
                    (
                        f"{provider_key}.env_key="
                        f"{json.dumps(settings.environment_key)}"
                    ),
                    "-c",
                    f"{provider_key}.supports_websockets=false",
                )
            )
        if not context.persist_session:
            command.append("--ephemeral")
        if context.resume_session:
            command.extend(("--last", "-"))
        else:
            command.extend(("--cd", str(context.workspace), "-"))
        return ProviderCommand(
            command=tuple(command),
            environment=dict(settings.extra_environment),
        )

    def observe_record(
        self,
        record: dict[str, Any],
    ) -> ProviderObservation:
        event_type = record.get("type")
        if event_type == "turn.completed":
            return ProviderObservation(
                terminal=True,
                succeeded=True,
                final_response=response_from_record(record),
            )
        if event_type == "turn.failed":
            return ProviderObservation(terminal=True, succeeded=False)
        return ProviderObservation(final_response=response_from_record(record))

    def model_observations(
        self,
        record: dict[str, Any],
    ) -> tuple[ProviderModelObservation, ...]:
        return ()

    def models_match(
        self,
        requested: str,
        observed: str,
    ) -> bool:
        return requested.strip().lower() == observed.strip().lower()

    def activity_observations(
        self,
        record: dict[str, Any],
    ) -> tuple[ProviderActivity, ...]:
        event_type = record.get("type")
        if event_type not in {"item.started", "item.completed"}:
            return ()
        item = record.get("item")
        if not isinstance(item, dict) or item.get("type") not in (
            CODEX_TOOL_ITEM_TYPES
        ):
            return ()
        activity_id = item.get("id")
        if not isinstance(activity_id, str):
            return ()
        label = item.get("command") or item.get("type")
        return (
            ProviderActivity(
                activity_id=activity_id,
                phase="start" if event_type == "item.started" else "end",
                label=str(label),
            ),
        )

    def parse_usage(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        per_turn = []
        cumulative = []
        for record in records:
            payload = record.get("payload", record)
            if not isinstance(payload, dict):
                continue
            usage = payload.get("usage")
            if isinstance(usage, dict):
                selected = normalize_codex_usage(usage)
                if (
                    record.get("type") in {"turn.completed", "turn_completed"}
                    or payload.get("type")
                    in {"turn.completed", "turn_completed"}
                ):
                    per_turn.append(selected)
                else:
                    cumulative.append(selected)
            total_usage = payload.get("total_token_usage")
            if isinstance(total_usage, dict):
                cumulative.append(normalize_codex_usage(total_usage))
            info = payload.get("info")
            if isinstance(info, dict):
                total_usage = info.get("total_token_usage")
                if isinstance(total_usage, dict):
                    cumulative.append(normalize_codex_usage(total_usage))
        selected = per_turn if per_turn else cumulative[-1:]
        if not selected:
            raise ValueError("no Codex usage found")
        return sum_usage(selected, provider=self.name)

    def classify_failure(
        self,
        records: list[dict[str, Any]],
        stderr: str,
    ) -> ProviderFailure | None:
        summary = stderr.strip()
        terminal = False
        api_status = None
        reason = None
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
                or record.get("type") in {"error", "turn.failed"}
                or (
                    record.get("type") == "rate_limit_event"
                    and rejected_rate_limit
                )
                or status is not None
            )
            if is_error and text and not summary:
                summary = text
            if is_error and (
                status in TERMINAL_HTTP_STATUSES
                or any(fragment in lowered for fragment in TERMINAL_ERROR_FRAGMENTS)
            ):
                terminal = True
        if not summary and not terminal:
            return None
        return ProviderFailure(
            summary=summary[-2000:] or "Codex request failed",
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
