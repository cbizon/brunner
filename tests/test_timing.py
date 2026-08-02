from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brunner.activity_cli import execute as execute_activity
from brunner.timing import (
    ActivityTracker,
    build_time_accounting,
    epoch_to_iso,
    record_activity,
)


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )


def _event(
    epoch: float,
    event: str,
    **details: object,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "event": event,
        "epoch_seconds": epoch,
        "recorded_at": epoch_to_iso(epoch),
        **details,
    }


def test_time_accounting_partitions_wait_tool_and_agent_time(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "timing/events.jsonl"
    external_path = tmp_path / "timing/external-events.jsonl"
    _write_events(
        events_path,
        [
            _event(0, "runner_started"),
            _event(10, "attempt_started", attempt=1),
            _event(
                20,
                "activity",
                phase="start",
                category="foreground_tool",
                activity_id="tool-1",
                source="provider",
                attempt=1,
            ),
            _event(
                40,
                "activity",
                phase="end",
                category="foreground_tool",
                activity_id="tool-1",
                source="provider",
                attempt=1,
            ),
            _event(80, "attempt_ended", attempt=1),
            _event(
                80,
                "activity",
                phase="start",
                category="subscription_wait",
                activity_id="retry-1",
                source="runner",
            ),
            _event(
                90,
                "activity",
                phase="end",
                category="subscription_wait",
                activity_id="retry-1",
                source="runner",
            ),
        ],
    )
    _write_events(
        external_path,
        [
            _event(
                30,
                "activity",
                phase="start",
                category="background_job",
                activity_id="simulation",
                source="benchmark",
            ),
            _event(
                50,
                "activity",
                phase="start",
                category="external_wait",
                activity_id="simulation-wait",
                source="benchmark",
            ),
            _event(
                60,
                "activity",
                phase="end",
                category="external_wait",
                activity_id="simulation-wait",
                source="benchmark",
            ),
            _event(
                70,
                "activity",
                phase="end",
                category="background_job",
                activity_id="simulation",
                source="benchmark",
            ),
        ],
    )
    state = {
        "created_epoch": 0,
        "created_at": epoch_to_iso(0),
        "completed_at": epoch_to_iso(100),
        "attempts": [
            {
                "number": 1,
                "mode": "initial",
                "started_at": epoch_to_iso(10),
                "ended_at": epoch_to_iso(80),
            }
        ],
    }

    accounting = build_time_accounting(
        state,
        events_path=events_path,
        external_events_path=external_path,
    )

    assert accounting["summary"] == {
        "wall_seconds": 100,
        "subscription_wait_seconds": 10,
        "runner_retry_wait_seconds": 0,
        "external_wait_seconds": 10,
        "foreground_tool_seconds": 20,
        "agent_active_seconds": 40,
        "runner_overhead_seconds": 20,
        "unclassified_seconds": 0,
        "background_job_seconds": 40,
    }
    assert sum(
        accounting["summary"][f"{category}_seconds"]
        for category in (
            "subscription_wait",
            "runner_retry_wait",
            "external_wait",
            "foreground_tool",
            "agent_active",
            "runner_overhead",
            "unclassified",
        )
    ) == accounting["summary"]["wall_seconds"]


def test_external_activity_records_append_only_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    record_activity(
        "start",
        "background_job",
        "simulation-1",
        label="case a",
        log_path=path,
    )
    record_activity(
        "end",
        "background_job",
        "simulation-1",
        log_path=path,
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["phase"] for record in records] == ["start", "end"]
    assert all(record["source"] == "benchmark" for record in records)


def test_historical_trial_without_timeline_remains_unclassified(
    tmp_path: Path,
) -> None:
    state = {
        "created_epoch": 0,
        "created_at": epoch_to_iso(0),
        "completed_at": epoch_to_iso(10),
        "attempts": [
            {
                "number": 1,
                "mode": "initial",
                "started_at": epoch_to_iso(2),
                "ended_at": epoch_to_iso(8),
            }
        ],
    }

    accounting = build_time_accounting(
        state,
        events_path=tmp_path / "missing-runner-events.jsonl",
        external_events_path=tmp_path / "missing-external-events.jsonl",
    )

    assert accounting["summary"]["agent_active_seconds"] == 0
    assert accounting["summary"]["unclassified_seconds"] == 10


def test_external_activity_rejects_unknown_categories(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        record_activity(
            "start",
            "model_thinking",
            "unknown",
            log_path=tmp_path / "events.jsonl",
        )


def test_activity_cli_wraps_external_wait_command(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    return_code = execute_activity(
        [
            "--log",
            str(path),
            "run",
            "external_wait",
            "simulation",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert return_code == 0
    assert [record["phase"] for record in records] == ["start", "end"]


def _activity_line(
    phase: str,
    epoch: float,
    *,
    activity_id: str = "sim-1",
    guard_pid: int | None = None,
) -> str:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "event": "activity",
        "phase": phase,
        "category": "background_job",
        "activity_id": activity_id,
        "source": "benchmark",
        "epoch_seconds": epoch,
    }
    if guard_pid is not None:
        value["guard_pid"] = guard_pid
    return json.dumps(value) + "\n"


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_activity_tracker_pairs_start_and_end(tmp_path: Path) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(_activity_line("start", 100.0))
    tracker = ActivityTracker(log)

    assert tracker.active(now=110.0)

    log.write_text(
        _activity_line("start", 100.0) + _activity_line("end", 120.0)
    )
    assert not tracker.active(now=130.0)


def test_activity_tracker_reads_only_appended_bytes(tmp_path: Path) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(_activity_line("start", 100.0))
    tracker = ActivityTracker(log)
    tracker.active(now=110.0)
    first_offset = tracker._offset

    with log.open("a") as stream:
        stream.write(_activity_line("start", 105.0, activity_id="sim-2"))
    tracker.active(now=110.0)

    assert tracker._offset > first_offset
    assert len(tracker._open) == 2


def test_activity_tracker_ignores_starts_from_earlier_attempts(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(_activity_line("start", 100.0))
    tracker = ActivityTracker(log, since_epoch=200.0)

    assert not tracker.active(now=210.0)


def test_activity_tracker_releases_interval_with_dead_guard(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(_activity_line("start", 100.0, guard_pid=_dead_pid()))
    tracker = ActivityTracker(log)

    assert not tracker.active(now=110.0)
    assert tracker.stale_intervals()[0]["reason"].startswith("guard process")


def test_activity_tracker_keeps_interval_with_live_guard(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(_activity_line("start", 100.0, guard_pid=os.getpid()))
    tracker = ActivityTracker(log)

    assert tracker.active(now=110.0)


def test_activity_tracker_releases_interval_past_maximum(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(_activity_line("start", 100.0))
    tracker = ActivityTracker(log, max_interval_seconds=50)

    assert tracker.active(now=140.0)
    assert not tracker.active(now=200.0)


def test_activity_tracker_recovers_from_truncated_log(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(
        _activity_line("start", 100.0)
        + _activity_line("start", 101.0, activity_id="sim-2")
    )
    tracker = ActivityTracker(log)
    assert len(tracker.active(now=110.0)) == 2

    log.write_text(_activity_line("start", 300.0, activity_id="sim-9"))
    active = tracker.active(now=310.0)

    assert active == {("benchmark", "background_job", "sim-9")}


def test_activity_tracker_recovers_from_replaced_log(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    log.write_text(_activity_line("start", 100.0))
    tracker = ActivityTracker(log)
    assert tracker.active(now=110.0)

    # A workspace restored from a collected trial yields a different inode
    # with, potentially, identical length.
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(
        _activity_line("start", 300.0, activity_id="sim-9")
    )
    replacement.replace(log)
    active = tracker.active(now=310.0)

    assert active == {("benchmark", "background_job", "sim-9")}


def test_activity_run_wrapper_records_guard_pid(tmp_path: Path) -> None:
    log = tmp_path / "activity.jsonl"
    execute_activity(
        [
            "--log",
            str(log),
            "run",
            "background_job",
            "case-a",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )
    events = [json.loads(line) for line in log.read_text().splitlines()]

    assert events[0]["phase"] == "start"
    assert events[0]["guard_pid"] == os.getpid()


def test_bare_activity_start_records_no_guard_pid(tmp_path: Path) -> None:
    log = tmp_path / "activity.jsonl"
    execute_activity(
        ["--log", str(log), "start", "background_job", "case-a"]
    )
    event = json.loads(log.read_text().splitlines()[0])

    # The starting process exits immediately, so its PID proves nothing.
    assert "guard_pid" not in event
