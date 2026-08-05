from __future__ import annotations

from typing import Any


PROVIDER_FINAL_STATUSES = frozenset({"complete", "partial", "failed"})
PIPELINE_TERMINAL_STATUSES = frozenset(
    {*PROVIDER_FINAL_STATUSES, "provider_error", "timeout"}
)


def summarize_pipeline_state(
    state: dict[str, Any] | None,
) -> dict[str, Any]:
    value = state or {}
    status = str(value.get("status") or "missing")
    final_response = value.get("final_response")
    provider_result_present = (
        status in PROVIDER_FINAL_STATUSES
        and isinstance(final_response, dict)
        and final_response.get("status") == status
    )
    attempts = value.get("attempts")
    last_attempt = (
        attempts[-1]
        if isinstance(attempts, list)
        and attempts
        and isinstance(attempts[-1], dict)
        else {}
    )
    terminal_result_seen = bool(last_attempt.get("terminal_result_seen"))
    interruption = value.get("interruption")
    if not isinstance(interruption, dict):
        interruption = {}

    infrastructure_reason = None
    retryable_infrastructure = False
    if status == "interrupted":
        infrastructure_reason = "AgentInterrupted"
        retryable_infrastructure = True
    elif status == "timeout":
        infrastructure_reason = "AgentTimeout"
    elif status == "provider_error":
        infrastructure_reason = "AgentProviderError"
    elif not provider_result_present:
        infrastructure_reason = (
            "AgentTerminalResultMissing"
            if status in PROVIDER_FINAL_STATUSES
            else "AgentPipelineIncomplete"
        )
        retryable_infrastructure = True

    return {
        "schema_version": "1.0",
        "status": status,
        "complete": status in PIPELINE_TERMINAL_STATUSES,
        "provider_result_present": provider_result_present,
        "terminal_result_seen": terminal_result_seen,
        "forced_termination_reason": last_attempt.get(
            "forced_termination_reason"
        ),
        "signal": interruption.get("signal"),
        "signal_name": interruption.get("signal_name"),
        "failure": value.get("failure"),
        "infrastructure_failure": not provider_result_present,
        "infrastructure_reason": infrastructure_reason,
        "retryable_infrastructure": retryable_infrastructure,
    }
