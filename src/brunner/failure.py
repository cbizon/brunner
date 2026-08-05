from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


FAILURE_DOMAINS = frozenset(
    {
        "assessment",
        "backend",
        "candidate",
        "cleanup",
        "configuration",
        "evaluation",
        "integrity",
        "orchestrator",
        "provider",
        "reporting",
    }
)

FAILURE_DISPOSITIONS = frozenset(
    {
        "attention",
        "candidate_failed",
        "retry",
        "terminal",
        "wait",
    }
)


def failure_record(
    *,
    operation: str,
    domain: str,
    reason: str,
    message: str,
    disposition: str,
    retryable: bool,
    cleanup_required: bool = False,
    error_type: str | None = None,
    resource: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not operation.strip():
        raise ValueError("failure operation cannot be empty")
    if domain not in FAILURE_DOMAINS:
        raise ValueError(f"unknown failure domain: {domain}")
    if not reason.strip():
        raise ValueError("failure reason cannot be empty")
    if disposition not in FAILURE_DISPOSITIONS:
        raise ValueError(f"unknown failure disposition: {disposition}")
    if disposition == "retry" and not retryable:
        raise ValueError("retry disposition requires retryable=true")
    if disposition != "retry" and retryable:
        raise ValueError("retryable failures must use retry disposition")

    record: dict[str, Any] = {
        "schema_version": "1.0",
        "operation": operation,
        "domain": domain,
        "reason": reason,
        "message": message,
        "disposition": disposition,
        "retryable": retryable,
        "cleanup_required": cleanup_required,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if error_type is not None:
        record["error_type"] = error_type
    if resource is not None:
        record["resource"] = resource
    if details:
        record["details"] = details
    return record


def failure_from_exception(
    error: BaseException,
    *,
    operation: str,
    domain: str,
    reason: str,
    disposition: str,
    retryable: bool,
    cleanup_required: bool = False,
    resource: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return failure_record(
        operation=operation,
        domain=domain,
        reason=reason,
        message=str(error),
        disposition=disposition,
        retryable=retryable,
        cleanup_required=cleanup_required,
        error_type=type(error).__name__,
        resource=resource,
        details=details,
    )


def attach_failure(
    target: dict[str, Any],
    failure: dict[str, Any],
    *,
    history_limit: int = 50,
) -> None:
    target["failure"] = failure
    history = target.setdefault("failures", [])
    if not isinstance(history, list):
        history = []
        target["failures"] = history
    history.append(failure)
    del history[:-history_limit]
