from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


ACTIVITY_LOG_ENV = "BRUNNER_ACTIVITY_LOG"
EXTERNAL_ACTIVITY_CATEGORIES = frozenset(
    {"background_job", "external_wait"}
)
EXCLUSIVE_CATEGORIES = (
    "subscription_wait",
    "runner_retry_wait",
    "external_wait",
    "foreground_tool",
    "agent_active",
)


def epoch_to_iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


@dataclass
class TimingRecorder:
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _sequence: int = 0

    def emit(
        self,
        event: str,
        *,
        epoch_seconds: float | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        recorded_epoch = (
            time.time() if epoch_seconds is None else epoch_seconds
        )
        with self._lock:
            self._sequence += 1
            value = {
                "schema_version": "1.0",
                "sequence": self._sequence,
                "event": event,
                "recorded_at": epoch_to_iso(recorded_epoch),
                "epoch_seconds": recorded_epoch,
                **{
                    key: item
                    for key, item in details.items()
                    if item is not None
                },
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
            return value


def _append_external_event(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, sort_keys=True) + "\n"
    with path.open("a") as stream:
        try:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.write(line)
                stream.flush()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except ImportError:
            stream.write(line)
            stream.flush()


def record_activity(
    phase: str,
    category: str,
    activity_id: str,
    *,
    label: str | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    if phase not in {"start", "end"}:
        raise ValueError("activity phase must be 'start' or 'end'")
    if category not in EXTERNAL_ACTIVITY_CATEGORIES:
        raise ValueError(
            f"unsupported external activity category: {category}"
        )
    if not activity_id.strip():
        raise ValueError("activity_id must not be empty")
    selected = log_path
    if selected is None:
        environment_path = os.environ.get(ACTIVITY_LOG_ENV)
        if not environment_path:
            raise RuntimeError(f"{ACTIVITY_LOG_ENV} is not set")
        selected = Path(environment_path)
    epoch = time.time()
    value = {
        "schema_version": "1.0",
        "event": "activity",
        "phase": phase,
        "category": category,
        "activity_id": activity_id,
        "label": label,
        "source": "benchmark",
        "recorded_at": epoch_to_iso(epoch),
        "epoch_seconds": epoch,
        "pid": os.getpid(),
    }
    _append_external_event(selected, value)
    return value


@contextmanager
def activity(
    category: str,
    activity_id: str,
    *,
    label: str | None = None,
    log_path: Path | None = None,
) -> Iterator[None]:
    record_activity(
        "start",
        category,
        activity_id,
        label=label,
        log_path=log_path,
    )
    try:
        yield
    finally:
        record_activity(
            "end",
            category,
            activity_id,
            label=label,
            log_path=log_path,
        )


def read_timing_events(*paths: Path) -> list[dict[str, Any]]:
    records = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(
                value.get("epoch_seconds"),
                (int, float),
            ):
                records.append(value)
    return sorted(
        records,
        key=lambda value: (
            float(value["epoch_seconds"]),
            int(value.get("sequence", 0)),
        ),
    )


def active_activity_keys(path: Path) -> set[tuple[str, str, str]]:
    active: set[tuple[str, str, str]] = set()
    for event in read_timing_events(path):
        if event.get("event") != "activity":
            continue
        category = event.get("category")
        activity_id = event.get("activity_id")
        source = event.get("source", "benchmark")
        if not all(
            isinstance(item, str)
            for item in (source, category, activity_id)
        ):
            continue
        key = (source, category, activity_id)
        if event.get("phase") == "start":
            active.add(key)
        elif event.get("phase") == "end":
            active.discard(key)
    return active


def _iso_to_epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _clip_interval(
    start: float,
    end: float,
    wall_start: float,
    wall_end: float,
) -> tuple[float, float] | None:
    selected_start = max(start, wall_start)
    selected_end = min(end, wall_end)
    if selected_end <= selected_start:
        return None
    return selected_start, selected_end


def _merge_intervals(
    intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    if not intervals:
        return []
    selected = sorted(intervals)
    merged = [selected[0]]
    for start, end in selected[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _duration(intervals: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _pair_activity_events(
    events: list[dict[str, Any]],
    *,
    wall_start: float,
    wall_end: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    open_events: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    intervals = []
    limitations = []
    for event in events:
        if event.get("event") != "activity":
            continue
        category = event.get("category")
        activity_id = event.get("activity_id")
        source = str(event.get("source", "runner"))
        if not isinstance(category, str) or not isinstance(activity_id, str):
            continue
        key = (source, category, activity_id)
        if event.get("phase") == "start":
            open_events.setdefault(key, []).append(event)
            continue
        if event.get("phase") != "end" or not open_events.get(key):
            limitations.append(
                f"unmatched activity end: {source}/{category}/{activity_id}"
            )
            continue
        start_event = open_events[key].pop(0)
        clipped = _clip_interval(
            float(start_event["epoch_seconds"]),
            float(event["epoch_seconds"]),
            wall_start,
            wall_end,
        )
        if clipped is None:
            continue
        start, end = clipped
        intervals.append(
            {
                "category": category,
                "activity_id": activity_id,
                "label": start_event.get("label") or event.get("label"),
                "source": source,
                "attempt": start_event.get("attempt"),
                "started_at": epoch_to_iso(start),
                "ended_at": epoch_to_iso(end),
                "duration_seconds": end - start,
                "complete": True,
                "_start": start,
                "_end": end,
            }
        )
    for (source, category, activity_id), starts in open_events.items():
        for start_event in starts:
            clipped = _clip_interval(
                float(start_event["epoch_seconds"]),
                wall_end,
                wall_start,
                wall_end,
            )
            if clipped is None:
                continue
            start, end = clipped
            intervals.append(
                {
                    "category": category,
                    "activity_id": activity_id,
                    "label": start_event.get("label"),
                    "source": source,
                    "attempt": start_event.get("attempt"),
                    "started_at": epoch_to_iso(start),
                    "ended_at": epoch_to_iso(end),
                    "duration_seconds": end - start,
                    "complete": False,
                    "_start": start,
                    "_end": end,
                }
            )
            limitations.append(
                f"unmatched activity start: {source}/{category}/{activity_id}"
            )
    return intervals, limitations


def _attempt_intervals(
    state: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    wall_start: float,
    wall_end: float,
) -> list[dict[str, Any]]:
    starts = {
        int(event["attempt"]): event
        for event in events
        if event.get("event") == "attempt_started"
        and isinstance(event.get("attempt"), int)
    }
    ends = {
        int(event["attempt"]): event
        for event in events
        if event.get("event") == "attempt_ended"
        and isinstance(event.get("attempt"), int)
    }
    result = []
    for attempt in state.get("attempts", []):
        if not isinstance(attempt, dict):
            continue
        number = attempt.get("number")
        start = (
            float(starts[number]["epoch_seconds"])
            if isinstance(number, int) and number in starts
            else _iso_to_epoch(attempt.get("started_at"))
        )
        end = (
            float(ends[number]["epoch_seconds"])
            if isinstance(number, int) and number in ends
            else _iso_to_epoch(attempt.get("ended_at"))
        )
        if start is None or end is None:
            continue
        clipped = _clip_interval(start, end, wall_start, wall_end)
        if clipped is None:
            continue
        start, end = clipped
        result.append(
            {
                "category": "agent_session",
                "source": "runner",
                "attempt": number,
                "mode": attempt.get("mode"),
                "started_at": epoch_to_iso(start),
                "ended_at": epoch_to_iso(end),
                "duration_seconds": end - start,
                "complete": True,
                "_start": start,
                "_end": end,
            }
        )
    return result


def _exclusive_partition(
    wall_start: float,
    wall_end: float,
    category_intervals: dict[str, list[tuple[float, float]]],
    *,
    timeline_available: bool,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    boundaries = {wall_start, wall_end}
    for intervals in category_intervals.values():
        for start, end in intervals:
            boundaries.update((start, end))
    ordered = sorted(boundaries)
    totals = {
        category: 0.0
        for category in (
            *EXCLUSIVE_CATEGORIES,
            "runner_overhead",
            "unclassified",
        )
    }
    segments = []
    for start, end in zip(ordered, ordered[1:]):
        if end <= start:
            continue
        category = next(
            (
                candidate
                for candidate in EXCLUSIVE_CATEGORIES
                if any(
                    interval_start < end and interval_end > start
                    for interval_start, interval_end in category_intervals.get(
                        candidate,
                        [],
                    )
                )
            ),
            "runner_overhead" if timeline_available else "unclassified",
        )
        totals[category] += end - start
        if segments and segments[-1]["category"] == category:
            segments[-1]["_end"] = end
            segments[-1]["ended_at"] = epoch_to_iso(end)
            segments[-1]["duration_seconds"] += end - start
        else:
            segments.append(
                {
                    "category": category,
                    "started_at": epoch_to_iso(start),
                    "ended_at": epoch_to_iso(end),
                    "duration_seconds": end - start,
                    "_start": start,
                    "_end": end,
                }
            )
    return totals, segments


def build_time_accounting(
    state: dict[str, Any],
    *,
    events_path: Path,
    external_events_path: Path,
) -> dict[str, Any]:
    wall_start = float(state.get("created_epoch", time.time()))
    wall_end = (
        _iso_to_epoch(state.get("completed_at"))
        or _iso_to_epoch(state.get("updated_at"))
        or time.time()
    )
    wall_end = max(wall_start, wall_end)
    events = read_timing_events(events_path, external_events_path)
    timeline_available = events_path.is_file()
    activities, limitations = _pair_activity_events(
        events,
        wall_start=wall_start,
        wall_end=wall_end,
    )
    attempts = _attempt_intervals(
        state,
        events,
        wall_start=wall_start,
        wall_end=wall_end,
    )
    category_intervals: dict[str, list[tuple[float, float]]] = {
        category: [] for category in EXCLUSIVE_CATEGORIES
    }
    if timeline_available:
        category_intervals["agent_active"] = [
            (item["_start"], item["_end"]) for item in attempts
        ]
    for item in activities:
        category = item["category"]
        if category in category_intervals:
            category_intervals[category].append(
                (item["_start"], item["_end"])
            )
    totals, exclusive_intervals = _exclusive_partition(
        wall_start,
        wall_end,
        category_intervals,
        timeline_available=timeline_available,
    )
    background_intervals = [
        (item["_start"], item["_end"])
        for item in activities
        if item["category"] == "background_job"
    ]
    if not timeline_available:
        limitations.append(
            "Runner receipt timestamps are unavailable; attempt time is not "
            "classified as agent-active or tool time."
        )
    limitations.extend(
        [
            (
                "agent_active_seconds includes provider/model processing, "
                "orchestration, and provider latency between observed tool "
                "events; those components cannot be separated generically."
            ),
            (
                "Simulation runtime and idle waiting are separated only when "
                "the benchmark emits background_job or external_wait activity "
                "events."
            ),
            (
                "Provider event times are local receipt timestamps and may "
                "lag the provider-side event."
            ),
            (
                "background_job_seconds may overlap other categories and is "
                "not added to the exclusive wall-time partition."
            ),
        ]
    )
    public_intervals = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in (*attempts, *activities)
    ]
    public_exclusive = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in exclusive_intervals
    ]
    summary = {
        "wall_seconds": wall_end - wall_start,
        **{
            f"{category}_seconds": totals[category]
            for category in (
                *EXCLUSIVE_CATEGORIES,
                "runner_overhead",
                "unclassified",
            )
        },
        "background_job_seconds": _duration(background_intervals),
    }
    return {
        "schema_version": "1.0",
        "started_at": epoch_to_iso(wall_start),
        "ended_at": epoch_to_iso(wall_end),
        "summary": summary,
        "partition_is_exclusive": True,
        "background_job_may_overlap": True,
        "intervals": public_intervals,
        "exclusive_intervals": public_exclusive,
        "limitations": list(dict.fromkeys(limitations)),
    }
