from __future__ import annotations

import json
from pathlib import Path

import pytest

from brunner.failure import failure_from_exception, failure_record
from brunner.io import write_json_atomic


def test_failure_record_captures_boundary_decision() -> None:
    failure = failure_from_exception(
        OSError("no space left on device"),
        operation="campaign_state_write",
        domain="orchestrator",
        reason="CampaignPersistenceFailed",
        disposition="attention",
        retryable=False,
        resource="orchestrator_filesystem",
    )

    assert failure["operation"] == "campaign_state_write"
    assert failure["domain"] == "orchestrator"
    assert failure["error_type"] == "OSError"
    assert failure["resource"] == "orchestrator_filesystem"
    assert failure["cleanup_required"] is False


def test_failure_record_rejects_inconsistent_retry_decision() -> None:
    with pytest.raises(ValueError, match="requires retryable"):
        failure_record(
            operation="backend_cleanup",
            domain="cleanup",
            reason="BackendCleanupFailed",
            message="temporary API failure",
            disposition="retry",
            retryable=False,
        )


def test_atomic_json_write_does_not_use_predictable_temp_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    predictable = tmp_path / "state.json.tmp"
    predictable.symlink_to(tmp_path / "outside.json")

    write_json_atomic(path, {"status": "complete"})

    assert json.loads(path.read_text()) == {"status": "complete"}
    assert predictable.is_symlink()
    assert not (tmp_path / "outside.json").exists()
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_json_serialization_failure_preserves_previous_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    write_json_atomic(path, {"status": "running"})

    with pytest.raises(TypeError):
        write_json_atomic(path, {"invalid": object()})

    assert json.loads(path.read_text()) == {"status": "running"}
