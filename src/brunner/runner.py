from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Callable

from brunner.contract import (
    OutputContract,
    load_output_contract,
    render_final_response_handoff,
    validate_final_response,
)
from brunner.definition import BenchmarkDefinition, RuntimeDefaults
from brunner.errors import ContractError
from brunner.io import write_json_atomic
from brunner.providers import (
    ProviderAdapter,
    ProviderRunContext,
    ProviderSettings,
    get_provider,
)
from brunner.submission import validate_submission
from brunner.trial import load_trial_identity
from brunner.timing import (
    ACTIVITY_LOG_ENV,
    DEFAULT_MAX_ACTIVITY_INTERVAL_SECONDS,
    ActivityTracker,
    TimingRecorder,
    build_time_accounting,
    epoch_to_iso,
)
from brunner.usage import read_json_records


PROVIDER_FINAL_STATUSES = frozenset({"complete", "partial", "failed"})
PIPELINE_TERMINAL_STATUSES = frozenset(
    {*PROVIDER_FINAL_STATUSES, "provider_error", "timeout"}
)
STREAM_DRAIN_SECONDS = 5.0
STREAM_CLOSE_SECONDS = 2.0
PROTECTED_CONTROL_PATHS = (
    "metadata",
    "backend",
    "evaluation",
    "assessments",
    "usage",
    "status.json",
)


@dataclass(frozen=True)
class AgentRunConfiguration:
    benchmark_id: str
    benchmark_version: str
    rendered_prompt: str
    runtime: RuntimeDefaults


def load_agent_run_configuration(
    trial: Path,
    contract: OutputContract,
) -> AgentRunConfiguration:
    value = json.loads((trial / "metadata/agent-run.json").read_text())
    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    expected = {
        "benchmark_id": metadata["benchmark_id"],
        "benchmark_version": metadata["benchmark_version"],
        "contract_sha256": metadata["contract_sha256"],
    }
    mismatches = {
        key: {"expected": wanted, "actual": value.get(key)}
        for key, wanted in expected.items()
        if value.get(key) != wanted
    }
    if value.get("contract_sha256") != contract.sha256:
        mismatches["loaded_contract_sha256"] = {
            "expected": value.get("contract_sha256"),
            "actual": contract.sha256,
        }
    if mismatches:
        raise RuntimeError(
            f"staged agent configuration changed: {mismatches}"
        )
    rendered_prompt = str(value["rendered_prompt"])
    prompt_path = Path(rendered_prompt)
    if (
        not rendered_prompt
        or prompt_path.is_absolute()
        or ".." in prompt_path.parts
    ):
        raise RuntimeError(
            f"invalid staged prompt path: {rendered_prompt!r}"
        )
    runtime = RuntimeDefaults(**value["runtime"])
    runtime.validate()
    return AgentRunConfiguration(
        benchmark_id=str(value["benchmark_id"]),
        benchmark_version=str(value["benchmark_version"]),
        rendered_prompt=rendered_prompt,
        runtime=runtime,
    )


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _output_repair_feedback(reason: str | None) -> str:
    if reason == "missing_structured_final_response":
        return (
            "The previous attempt was rejected because it returned prose or "
            "no valid structured final response. Workspace files alone do not "
            "complete the run."
        )
    if reason == "submission_validation_failed":
        return (
            "The previous attempt was rejected because its final response did "
            "not exactly match a valid submission. Repair the submission and "
            "run-status document before responding."
        )
    return ""


def continuation_prompt(
    contract: OutputContract,
    *,
    repair_reason: str | None = None,
) -> str:
    parts = [
        "Continue the benchmark from the persistent workspace and provider "
        "session. Inspect existing work, complete every required output in "
        f"{contract.submission_manifest}, validate it against the schemas, "
        f"and update {contract.run_status_path}. Do not stop after planning.",
    ]
    feedback = _output_repair_feedback(repair_reason)
    if feedback:
        parts.append(feedback)
    parts.append(render_final_response_handoff(contract))
    return " ".join(parts)


def finalization_prompt(
    contract: OutputContract,
    *,
    repair_reason: str | None = None,
) -> str:
    parts = [
        "The benchmark deadline is approaching. Stop starting long-running "
        "work. Preserve all useful outputs already produced and write the best "
        f"valid {contract.submission_manifest} possible. Always write "
        f"{contract.run_status_path}. Report complete only when all required "
        "outputs are valid, partial when useful valid work exists, or failed "
        "otherwise. Finish now.",
    ]
    feedback = _output_repair_feedback(repair_reason)
    if feedback:
        parts.append(feedback)
    parts.append(render_final_response_handoff(contract))
    return " ".join(parts)


def process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process(
    process: subprocess.Popen[str],
    *,
    term_wait_seconds: float = 5,
    kill_wait_seconds: float = 2,
) -> bool:
    process_group_id = process.pid
    process.poll()
    if not process_group_alive(process_group_id):
        return False
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError:
        if process.poll() is not None:
            return False
        process.terminate()
    term_deadline = time.monotonic() + term_wait_seconds
    while time.monotonic() < term_deadline:
        process.poll()
        if not process_group_alive(process_group_id):
            break
        time.sleep(0.05)
    process.poll()
    if process_group_alive(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
        kill_deadline = time.monotonic() + kill_wait_seconds
        while time.monotonic() < kill_deadline:
            process.poll()
            if not process_group_alive(process_group_id):
                break
            time.sleep(0.05)
    if process.poll() is None:
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
    return True


class StreamSink:
    """Serialize writes to the attempt and combined logs.

    A pump thread can outlive ``run_attempt`` when a grandchild inherited the
    pipe, so writes have to be refused once the files close rather than raising
    inside a daemon thread and silently dropping the rest of the stream.
    """

    def __init__(
        self,
        attempt_output: IO[str],
        combined_output: IO[str],
    ) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._attempt = attempt_output
        self._combined = combined_output
        self.dropped_lines = 0

    def write(self, line: str) -> None:
        with self._lock:
            if self._closed:
                self.dropped_lines += 1
                return
            try:
                self._attempt.write(line)
                self._attempt.flush()
                self._combined.write(line)
                self._combined.flush()
            except (OSError, ValueError):
                self._closed = True
                self.dropped_lines += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True


def pump_stream(
    source: IO[str],
    sink: StreamSink,
    on_line: Callable[[str], None] | None = None,
) -> None:
    try:
        for line in source:
            sink.write(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - never kill the pump
                    pass
    except (OSError, ValueError):
        return


def snapshot_control_plane(
    trial: Path,
    *,
    reject_symlinks: bool = True,
) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative in PROTECTED_CONTROL_PATHS:
        root = trial / relative
        if root.is_symlink():
            if reject_symlinks:
                raise RuntimeError(
                    f"runner-owned path is a symlink: {root}"
                )
            snapshot[relative] = (
                b"symlink\0" + os.readlink(root).encode()
            )
            continue
        if root.is_file():
            snapshot[relative] = root.read_bytes()
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                if reject_symlinks:
                    raise RuntimeError(
                        f"runner-owned path contains a symlink: {path}"
                    )
                snapshot[path.relative_to(trial).as_posix()] = (
                    b"symlink\0" + os.readlink(path).encode()
                )
                continue
            if path.is_file():
                snapshot[path.relative_to(trial).as_posix()] = (
                    path.read_bytes()
                )
    return snapshot


def restore_control_plane(
    trial: Path,
    expected: dict[str, bytes],
) -> list[str]:
    observed = snapshot_control_plane(
        trial,
        reject_symlinks=False,
    )
    changed = sorted(
        name
        for name in set(expected) | set(observed)
        if expected.get(name) != observed.get(name)
    )
    if not changed:
        return []
    for relative in PROTECTED_CONTROL_PATHS:
        path = trial / relative
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        if Path(relative).suffix != ".json":
            path.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        path = trial / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".restore.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
    return changed


def run_attempt(
    *,
    adapter: ProviderAdapter,
    command: tuple[str, ...],
    workspace: Path,
    environment: dict[str, str],
    prompt: str,
    attempt_events: Path,
    attempt_stderr: Path,
    combined_events: Path,
    combined_stderr: Path,
    deadline_epoch: float,
    stop_requested: threading.Event,
    terminal_exit_grace_seconds: float,
    soft_deadline_epoch: float | None = None,
    external_activity_path: Path | None = None,
    attempt_number: int = 1,
    timing_recorder: TimingRecorder | None = None,
    terminal_success_ready: Callable[[], bool] | None = None,
    requested_model: str | None = None,
    attempt_start_epoch: float | None = None,
    max_activity_interval_seconds: float | None = (
        DEFAULT_MAX_ACTIVITY_INTERVAL_SECONDS
    ),
    submission_poll_seconds: float = 2.0,
) -> dict[str, Any]:
    terminal_seen = threading.Event()
    terminal_succeeded = threading.Event()
    terminal_exit_ready = threading.Event()
    observed_response: dict[str, Any] | None = None
    observation_lock = threading.Lock()
    model_lock = threading.Lock()
    activity_lock = threading.Lock()
    observed_models: list[dict[str, Any]] = []
    observed_model_keys: set[tuple[str, str]] = set()
    model_mismatch: dict[str, Any] | None = None
    active_provider_activities: set[str] = set()
    provider_event_index = 0
    prompt_delivery_errors: list[str] = []
    prompt_delivery_done = threading.Event()

    def inspect_line(line: str) -> None:
        nonlocal model_mismatch, observed_response, provider_event_index
        recorded_epoch = time.time()
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(record, dict):
            return
        provider_event_index += 1
        if timing_recorder is not None:
            item = record.get("item")
            timing_recorder.emit(
                "provider_event_received",
                epoch_seconds=recorded_epoch,
                attempt=attempt_number,
                provider_event_index=provider_event_index,
                provider_event_type=record.get("type"),
                provider_item_type=(
                    item.get("type") if isinstance(item, dict) else None
                ),
            )
        for model_observation in adapter.model_observations(record):
            key = (
                model_observation.model,
                model_observation.source,
            )
            matches = bool(
                requested_model is None
                or adapter.models_match(
                    requested_model,
                    model_observation.model,
                )
            )
            with model_lock:
                if key not in observed_model_keys:
                    observed_model_keys.add(key)
                    observed_models.append(
                        {
                            "model": model_observation.model,
                            "source": model_observation.source,
                            "provider_event_index": provider_event_index,
                        }
                    )
                if not matches and model_mismatch is None:
                    model_mismatch = {
                        "requested_model": requested_model,
                        "observed_model": model_observation.model,
                        "source": model_observation.source,
                        "provider_event_index": provider_event_index,
                    }
            if timing_recorder is not None:
                timing_recorder.emit(
                    "provider_model_observed",
                    epoch_seconds=recorded_epoch,
                    attempt=attempt_number,
                    provider_event_index=provider_event_index,
                    requested_model=requested_model,
                    observed_model=model_observation.model,
                    source=model_observation.source,
                    matches=matches,
                )
        for activity in adapter.activity_observations(record):
            with activity_lock:
                if activity.phase == "start":
                    active_provider_activities.add(
                        activity.activity_id
                    )
                    provider_activity_started[activity.activity_id] = (
                        recorded_epoch
                    )
                elif activity.phase == "end":
                    active_provider_activities.discard(
                        activity.activity_id
                    )
                    provider_activity_started.pop(
                        activity.activity_id,
                        None,
                    )
            if timing_recorder is not None:
                timing_recorder.emit(
                    "activity",
                    epoch_seconds=recorded_epoch,
                    phase=activity.phase,
                    category=activity.category,
                    activity_id=activity.activity_id,
                    label=activity.label,
                    source="provider",
                    attempt=attempt_number,
                )
        observation = adapter.observe_record(record)
        with observation_lock:
            if observation.final_response is not None:
                observed_response = observation.final_response
        if observation.terminal:
            if observation.succeeded:
                terminal_succeeded.set()
                if terminal_success_ready is None:
                    terminal_exit_ready.set()
            else:
                terminal_exit_ready.set()
            terminal_seen.set()

    activity_tracker = (
        ActivityTracker(
            external_activity_path,
            since_epoch=attempt_start_epoch,
            max_interval_seconds=max_activity_interval_seconds,
        )
        if external_activity_path is not None
        else None
    )
    provider_activity_started: dict[str, float] = {}

    def active_work() -> bool:
        with activity_lock:
            if active_provider_activities:
                if max_activity_interval_seconds is None:
                    return True
                now = time.time()
                expired = {
                    activity_id
                    for activity_id in active_provider_activities
                    if now - provider_activity_started.get(activity_id, now)
                    > max_activity_interval_seconds
                }
                # A provider that never emits the matching tool-end event must
                # not hold the attempt open for the rest of the trial.
                active_provider_activities.difference_update(expired)
                if active_provider_activities:
                    return True
        return bool(
            activity_tracker is not None and activity_tracker.active()
        )

    with (
        attempt_events.open("w") as attempt_stdout,
        attempt_stderr.open("w") as attempt_error,
        combined_events.open("a") as combined_stdout,
        combined_stderr.open("a") as combined_error,
    ):
        try:
            process = subprocess.Popen(
                list(command),
                cwd=workspace,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            launch_error = f"{type(error).__name__}: {error}"
            message = f"provider launch failed: {launch_error}\n"
            attempt_error.write(message)
            attempt_error.flush()
            combined_error.write(message)
            combined_error.flush()
            return {
                "return_code": 127,
                "provider_return_code": 127,
                "terminal_result_seen": False,
                "terminal_result_succeeded": False,
                "terminal_exit_ready": False,
                "lingering_processes_terminated": False,
                "forced_termination_reason": None,
                "active_work_terminated": False,
                "soft_deadline_activity_seen": False,
                "launch_error": launch_error,
                "prompt_delivery_error": None,
                "stream_pump_incomplete": False,
                "dropped_output_lines": 0,
                "stale_activity_intervals": [],
                "requested_model": requested_model,
                "observed_models": [],
                "model_mismatch": None,
                "observed_response": None,
            }
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_sink = StreamSink(attempt_stdout, combined_stdout)
        stderr_sink = StreamSink(attempt_error, combined_error)
        stdout_thread = threading.Thread(
            target=pump_stream,
            args=(
                process.stdout,
                stdout_sink,
                inspect_line,
            ),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=pump_stream,
            args=(
                process.stderr,
                stderr_sink,
            ),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        def deliver_prompt() -> None:
            try:
                process.stdin.write(prompt)
                process.stdin.flush()
            except (OSError, ValueError) as error:
                prompt_delivery_errors.append(
                    f"{type(error).__name__}: {error}"
                )
            finally:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
                prompt_delivery_done.set()

        prompt_thread = threading.Thread(
            target=deliver_prompt,
            daemon=True,
        )
        prompt_thread.start()

        terminal_idle_since = None
        leader_exited_idle_since = None
        soft_deadline_activity_seen = False
        soft_deadline_idle_since = None
        lingering_processes_terminated = False
        forced_termination_reason = None
        active_work_terminated = False
        next_submission_poll = 0.0
        while process_group_alive(process.pid):
            work_is_active = active_work()
            now_epoch = time.time()
            now_monotonic = time.monotonic()
            with model_lock:
                mismatched_model = model_mismatch is not None
            if mismatched_model:
                active_work_terminated = work_is_active
                terminate_process(process)
                forced_termination_reason = "model_mismatch"
                break
            if (
                terminal_seen.is_set()
                and terminal_succeeded.is_set()
                and not terminal_exit_ready.is_set()
                and terminal_success_ready is not None
                and now_monotonic >= next_submission_poll
            ):
                # Revalidating the submission rehashes every artifact, so it
                # must not run on every 100ms tick.
                next_submission_poll = (
                    now_monotonic + submission_poll_seconds
                )
                if terminal_success_ready():
                    terminal_exit_ready.set()
            if (
                prompt_delivery_errors
                and process.poll() is None
                and not terminal_seen.is_set()
            ):
                terminate_process(process)
                forced_termination_reason = "prompt_delivery_error"
                break
            if terminal_exit_ready.is_set():
                if work_is_active:
                    terminal_idle_since = None
                elif terminal_idle_since is None:
                    terminal_idle_since = now_monotonic
                elif (
                    now_monotonic - terminal_idle_since
                    >= terminal_exit_grace_seconds
                ):
                    terminate_process(process)
                    lingering_processes_terminated = True
                    forced_termination_reason = "terminal_exit_grace"
                    break
            if process.poll() is not None and not terminal_exit_ready.is_set():
                if work_is_active:
                    leader_exited_idle_since = None
                elif leader_exited_idle_since is None:
                    leader_exited_idle_since = now_monotonic
                elif (
                    now_monotonic - leader_exited_idle_since
                    >= terminal_exit_grace_seconds
                ):
                    terminate_process(process)
                    lingering_processes_terminated = True
                    forced_termination_reason = "orphaned_process_group"
                    break
            if stop_requested.is_set():
                active_work_terminated = work_is_active
                terminate_process(process)
                forced_termination_reason = "stop_requested"
                break
            if now_epoch >= deadline_epoch:
                active_work_terminated = work_is_active
                terminate_process(process)
                forced_termination_reason = "hard_deadline"
                break
            if (
                soft_deadline_epoch is not None
                and now_epoch >= soft_deadline_epoch
                and not terminal_exit_ready.is_set()
            ):
                if work_is_active:
                    soft_deadline_activity_seen = True
                    soft_deadline_idle_since = None
                else:
                    if soft_deadline_idle_since is None:
                        soft_deadline_idle_since = now_monotonic
                    if (
                        now_monotonic - soft_deadline_idle_since
                        >= terminal_exit_grace_seconds
                    ):
                        terminate_process(process)
                        forced_termination_reason = "soft_deadline"
                        break
            wait_seconds = 0.1
            if terminal_idle_since is not None:
                remaining = terminal_exit_grace_seconds - (
                    now_monotonic - terminal_idle_since
                )
                wait_seconds = min(wait_seconds, max(0.0, remaining))
            if soft_deadline_idle_since is not None:
                remaining = terminal_exit_grace_seconds - (
                    now_monotonic - soft_deadline_idle_since
                )
                wait_seconds = min(wait_seconds, max(0.0, remaining))
            stop_requested.wait(wait_seconds)
        if process.poll() is None:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        prompt_thread.join(timeout=1)
        if not prompt_delivery_done.is_set() and not prompt_delivery_errors:
            prompt_delivery_errors.append(
                "prompt delivery thread did not stop after provider exit"
            )
        stdout_thread.join(timeout=STREAM_DRAIN_SECONDS)
        stderr_thread.join(timeout=STREAM_DRAIN_SECONDS)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            # A grandchild that inherited the pipe (and its own session) can
            # keep the read blocked forever. Force EOF so the pumps exit
            # instead of leaving daemon threads writing into files we are
            # about to close.
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            stdout_thread.join(timeout=STREAM_CLOSE_SECONDS)
            stderr_thread.join(timeout=STREAM_CLOSE_SECONDS)
        stream_pump_incomplete = (
            stdout_thread.is_alive() or stderr_thread.is_alive()
        )
        stdout_sink.close()
        stderr_sink.close()
        dropped_output_lines = (
            stdout_sink.dropped_lines + stderr_sink.dropped_lines
        )

    provider_return_code = (
        process.returncode if process.returncode is not None else 124
    )
    with model_lock:
        final_observed_models = list(observed_models)
        final_model_mismatch = (
            dict(model_mismatch) if model_mismatch is not None else None
        )
    if final_model_mismatch is not None:
        return_code = provider_return_code or 1
    elif terminal_succeeded.is_set() and not active_work_terminated:
        return_code = 0
    elif terminal_seen.is_set():
        return_code = provider_return_code or 1
    elif forced_termination_reason is not None:
        return_code = provider_return_code or 124
    else:
        return_code = provider_return_code
    return {
        "return_code": return_code,
        "provider_return_code": provider_return_code,
        "terminal_result_seen": terminal_seen.is_set(),
        "terminal_result_succeeded": terminal_succeeded.is_set(),
        "terminal_exit_ready": terminal_exit_ready.is_set(),
        "lingering_processes_terminated": lingering_processes_terminated,
        "forced_termination_reason": forced_termination_reason,
        "active_work_terminated": active_work_terminated,
        "soft_deadline_activity_seen": soft_deadline_activity_seen,
        "launch_error": None,
        "stream_pump_incomplete": stream_pump_incomplete,
        "dropped_output_lines": dropped_output_lines,
        "stale_activity_intervals": (
            activity_tracker.stale_intervals()
            if activity_tracker is not None
            else []
        ),
        "prompt_delivery_error": (
            prompt_delivery_errors[0] if prompt_delivery_errors else None
        ),
        "requested_model": requested_model,
        "observed_models": final_observed_models,
        "model_mismatch": final_model_mismatch,
        "observed_response": observed_response,
    }


def model_mismatch_message(mismatch: dict[str, Any]) -> str:
    return (
        f"provider substituted model {mismatch.get('observed_model')!r} "
        f"for requested model {mismatch.get('requested_model')!r} "
        f"at {mismatch.get('source') or 'an unknown event source'}"
    )


def load_state(
    path: Path,
    *,
    configuration: AgentRunConfiguration,
    contract: OutputContract,
    settings: ProviderSettings,
    test_id: str,
    runtime: RuntimeDefaults,
    adapter: ProviderAdapter,
) -> dict[str, Any]:
    if path.is_file():
        state = json.loads(path.read_text())
        expected = {
            "test_id": test_id,
            "provider": settings.provider,
            "model": settings.model,
            "effort": settings.effort,
            "benchmark_id": configuration.benchmark_id,
            "benchmark_version": configuration.benchmark_version,
            "contract_sha256": contract.sha256,
        }
        mismatches = {
            key: {"expected": value, "actual": state.get(key)}
            for key, value in expected.items()
            if state.get(key) != value
        }
        if mismatches:
            raise RuntimeError(f"persistent trial identity changed: {mismatches}")
        if state["status"] in PIPELINE_TERMINAL_STATUSES:
            return state
        for attempt in state["attempts"]:
            if attempt["status"] == "running":
                attempt["status"] = "interrupted"
                attempt["ended_at"] = utc_now()
        return state
    now = time.time()
    return {
        "schema_version": "1.0",
        "test_id": test_id,
        "provider": settings.provider,
        "model": settings.model,
        "effort": settings.effort,
        "benchmark_id": configuration.benchmark_id,
        "benchmark_version": configuration.benchmark_version,
        "contract_sha256": contract.sha256,
        "status": "pending",
        "created_at": utc_now(),
        "created_epoch": now,
        "deadline_epoch": now + runtime.timeout_seconds,
        "session_id": adapter.new_session_id(),
        "session_started": False,
        "finalization_started": False,
        "attempts": [],
    }


def _valid_response(
    value: object,
    contract: OutputContract,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        validate_final_response(
            value,
            contract,
            label="provider final response",
        )
    except ContractError:
        return None
    return value



def load_final_response(
    contract: OutputContract,
    *,
    observed_response: object,
    attempt_events: Path,
    final_output_path: Path,
) -> dict[str, Any] | None:
    response = _valid_response(observed_response, contract)
    if response is not None:
        return response
    for record in reversed(read_json_records(attempt_events)):
        for key in ("structured_output", "result"):
            response = _valid_response(record.get(key), contract)
            if response is not None:
                return response
            value = record.get(key)
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    continue
                response = _valid_response(decoded, contract)
                if response is not None:
                    return response
    if final_output_path.is_file():
        try:
            value = json.loads(final_output_path.read_text())
        except json.JSONDecodeError:
            pass
        else:
            response = _valid_response(value, contract)
            if response is not None:
                return response
    return None


def submission_validation_error(
    workspace: Path,
    contract: OutputContract,
    response: dict[str, Any],
) -> str | None:
    if response["status"] == "failed":
        return None
    try:
        submission = validate_submission(workspace, contract)
        if submission.run_status != response:
            raise ContractError(
                "run status does not match the current provider final "
                "response"
            )
    except (ContractError, OSError) as error:
        return str(error)
    return None


def write_terminal_artifacts(
    trial: Path,
    state: dict[str, Any],
    adapter: ProviderAdapter,
) -> None:
    created_epoch = float(state.get("created_epoch", time.time()))
    deadline_epoch = float(state["deadline_epoch"])
    last_attempt = state["attempts"][-1] if state["attempts"] else {}
    external_events = (
        trial / "workspace/.brunner/activity-events.jsonl"
    )
    legacy_external_events = trial / "timing/external-events.jsonl"
    if not external_events.is_file() and legacy_external_events.is_file():
        external_events = legacy_external_events
    accounting = build_time_accounting(
        state,
        events_path=trial / "timing/events.jsonl",
        external_events_path=external_events,
    )
    write_json_atomic(trial / "timing/accounting.json", accounting)
    write_json_atomic(
        trial / "timing/goal.json",
        {
            "schema_version": "2.0",
            "started_at": state["created_at"],
            "ended_at": state.get("completed_at", utc_now()),
            "elapsed_seconds": accounting["summary"]["wall_seconds"],
            "timeout_seconds": max(0.0, deadline_epoch - created_epoch),
            "status": state["status"],
            "return_code": last_attempt.get("return_code"),
            "attempt_count": len(state["attempts"]),
            "failure": state.get("failure"),
            "finalization_started_at": state.get("finalization_started_at"),
            "accounting_path": "timing/accounting.json",
            "accounting": accounting["summary"],
        },
    )
    records = read_json_records(trial / "transcript/events.jsonl")
    if not records:
        return
    try:
        usage = adapter.parse_usage(records)
    except ValueError as error:
        usage = {"parse_error": str(error)}
    write_json_atomic(trial / "usage/usage.json", usage)


def _run_configured_trial(
    configuration: AgentRunConfiguration,
    contract: OutputContract,
    trial: Path,
    settings: ProviderSettings,
    *,
    runtime: RuntimeDefaults | None = None,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
    stop_requested: threading.Event | None = None,
) -> dict[str, Any]:
    runtime = runtime or configuration.runtime
    runtime.validate()
    adapter = get_provider(settings.provider)
    settings = adapter.validate_settings(settings)
    identity = load_trial_identity(trial)
    expected_identity = (
        identity.provider,
        identity.model,
        identity.effort,
    )
    actual_identity = (
        settings.provider,
        settings.model,
        settings.effort,
    )
    if expected_identity != actual_identity:
        raise RuntimeError(
            f"trial provider identity changed: {expected_identity} != "
            f"{actual_identity}"
        )

    workspace = trial / "workspace"
    transcript = trial / "transcript"
    attempts_root = transcript / "attempts"
    provider_home = trial / "provider-home"
    timing_root = trial / "timing"
    for path in (transcript, attempts_root, provider_home, timing_root):
        path.mkdir(parents=True, exist_ok=True)
    timing_events = timing_root / "events.jsonl"
    external_timing_events = (
        workspace / ".brunner/activity-events.jsonl"
    )
    external_timing_events.parent.mkdir(parents=True, exist_ok=True)
    timing_recorder = TimingRecorder(timing_events)
    state_path = trial / "status.json"
    state = load_state(
        state_path,
        configuration=configuration,
        contract=contract,
        settings=settings,
        test_id=identity.test_id,
        runtime=runtime,
        adapter=adapter,
    )
    if state["status"] in PIPELINE_TERMINAL_STATUSES:
        write_terminal_artifacts(trial, state, adapter)
        return state
    (transcript / "final.json").unlink(missing_ok=True)

    run_environment = os.environ.copy()
    run_environment.update(environment or {})
    run_environment.update(settings.extra_environment)
    run_environment["HOME"] = str(provider_home)
    run_environment["CODEX_HOME"] = str(provider_home / "codex")
    run_environment[ACTIVITY_LOG_ENV] = str(external_timing_events)
    Path(run_environment["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
    stop_requested = stop_requested or threading.Event()
    retry_seconds = runtime.retry_initial_seconds
    work_deadline = state["deadline_epoch"] - runtime.finalization_seconds
    combined_events = transcript / "events.jsonl"
    combined_stderr = transcript / "stderr.log"
    timing_recorder.emit("runner_started", status=state["status"])

    while time.time() < state["deadline_epoch"] and not stop_requested.is_set():
        finalizing = bool(state.get("finalization_started"))
        phase_deadline = state["deadline_epoch"] if finalizing else work_deadline
        if time.time() >= phase_deadline:
            if finalizing:
                break
            state["finalization_started"] = True
            state["finalization_started_at"] = utc_now()
            state["status"] = "finalizing"
            state.pop("next_retry_seconds", None)
            timing_recorder.emit("finalization_started")
            write_json_atomic(state_path, state)
            retry_seconds = runtime.retry_initial_seconds
            continue

        attempt_number = len(state["attempts"]) + 1
        if attempt_number > runtime.max_attempts:
            # A provider that fails immediately would otherwise retry for the
            # whole trial window and bury the original failure.
            state["status"] = "provider_error"
            state["failure"] = (
                f"provider did not succeed within {runtime.max_attempts} "
                "attempts"
            )
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state
        attempt_prefix = attempts_root / f"{attempt_number:04d}"
        attempt_events = attempt_prefix.with_suffix(".events.jsonl")
        attempt_stderr = attempt_prefix.with_suffix(".stderr.log")
        attempt_final = attempt_prefix.with_suffix(".final.json")
        previous_attempt = state["attempts"][-1] if state["attempts"] else {}
        repair_reason = previous_attempt.get("output_repair_reason")
        if not isinstance(repair_reason, str):
            repair_reason = None
        resume_session = bool(state["session_started"])
        context = ProviderRunContext(
            workspace=workspace,
            transcript_dir=transcript,
            final_schema_path=workspace / "schema/final-response.schema.json",
            final_output_path=attempt_final,
            persist_session=True,
            resume_session=resume_session,
            session_id=state["session_id"],
            executable=executable,
        )
        provider_command = adapter.build_command(settings, context)
        attempt_started_epoch = time.time()
        attempt = {
            "number": attempt_number,
            "status": "running",
            "mode": (
                "finalize"
                if finalizing
                else ("resume" if resume_session else "initial")
            ),
            "started_at": epoch_to_iso(attempt_started_epoch),
            "events": str(attempt_events.relative_to(trial)),
            "stderr": str(attempt_stderr.relative_to(trial)),
            "final_output": str(attempt_final.relative_to(trial)),
        }
        state["attempts"].append(attempt)
        state["status"] = "finalizing" if finalizing else "running"
        write_json_atomic(state_path, state)
        timing_recorder.emit(
            "attempt_started",
            epoch_seconds=attempt_started_epoch,
            attempt=attempt_number,
            mode=attempt["mode"],
        )
        if finalizing:
            prompt = finalization_prompt(
                contract,
                repair_reason=repair_reason,
            )
        elif resume_session:
            prompt = continuation_prompt(
                contract,
                repair_reason=repair_reason,
            )
        else:
            prompt = (
                workspace / configuration.rendered_prompt
            ).read_text()

        attempt_environment = {
            **run_environment,
            **provider_command.environment,
        }
        protected_control_plane = snapshot_control_plane(trial)

        def terminal_success_ready() -> bool:
            try:
                response = load_final_response(
                    contract,
                    observed_response=None,
                    attempt_events=attempt_events,
                    final_output_path=attempt_final,
                )
            except OSError:
                return False
            return bool(
                response is not None
                and submission_validation_error(
                    workspace,
                    contract,
                    response,
                )
                is None
            )

        outcome = run_attempt(
            adapter=adapter,
            command=provider_command.command,
            workspace=workspace,
            environment=attempt_environment,
            prompt=prompt,
            attempt_events=attempt_events,
            attempt_stderr=attempt_stderr,
            combined_events=combined_events,
            combined_stderr=combined_stderr,
            deadline_epoch=state["deadline_epoch"],
            stop_requested=stop_requested,
            terminal_exit_grace_seconds=runtime.provider_exit_grace_seconds,
            soft_deadline_epoch=(None if finalizing else work_deadline),
            external_activity_path=external_timing_events,
            attempt_number=attempt_number,
            timing_recorder=timing_recorder,
            terminal_success_ready=terminal_success_ready,
            requested_model=settings.model,
            attempt_start_epoch=attempt_started_epoch,
            max_activity_interval_seconds=(
                runtime.max_activity_interval_seconds
            ),
            submission_poll_seconds=runtime.submission_poll_seconds,
        )
        for stale in outcome.get("stale_activity_intervals", ()):
            timing_recorder.emit(
                "activity_interval_stale",
                attempt=attempt_number,
                **stale,
            )
        control_plane_changes = restore_control_plane(
            trial,
            protected_control_plane,
        )
        if control_plane_changes:
            attempt.update(
                {
                    key: value
                    for key, value in outcome.items()
                    if key != "observed_response"
                }
            )
            attempt_ended = timing_recorder.emit(
                "attempt_ended",
                attempt=attempt_number,
                return_code=1,
            )
            attempt["ended_at"] = attempt_ended["recorded_at"]
            attempt["status"] = "failed"
            attempt["control_plane_changes"] = control_plane_changes
            attempt["failure"] = (
                "agent modified runner-owned control files: "
                + ", ".join(control_plane_changes)
            )
            state["status"] = "provider_error"
            state["failure"] = attempt["failure"]
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state
        attempt.update(
            {
                key: value
                for key, value in outcome.items()
                if key != "observed_response"
            }
        )
        attempt_ended = timing_recorder.emit(
            "attempt_ended",
            attempt=attempt_number,
            return_code=outcome["return_code"],
        )
        attempt["ended_at"] = attempt_ended["recorded_at"]
        return_code = int(outcome["return_code"])
        attempt["status"] = "complete" if return_code == 0 else "failed"

        records = read_json_records(attempt_events)
        stderr = (
            attempt_stderr.read_text(errors="replace")
            if attempt_stderr.is_file()
            else ""
        )
        resume_unavailable = (
            resume_session
            and adapter.resume_is_unavailable(records, stderr)
        )
        if resume_unavailable:
            state["session_started"] = False
            attempt["session_reset"] = True
        elif records:
            state["session_started"] = True

        launch_error = outcome.get("launch_error")
        if launch_error is not None:
            attempt["status"] = "failed"
            attempt["failure"] = f"provider launch failed: {launch_error}"
            state["status"] = "provider_error"
            state["failure"] = attempt["failure"]
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state

        model_mismatch = outcome.get("model_mismatch")
        if isinstance(model_mismatch, dict):
            attempt["status"] = "failed"
            attempt["failure"] = model_mismatch_message(model_mismatch)
            state["status"] = "provider_error"
            state["failure"] = attempt["failure"]
            state["model_mismatch"] = model_mismatch
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state

        failure = adapter.classify_failure(records, stderr)
        if failure is not None:
            attempt["failure"] = failure.summary
            attempt["api_status"] = failure.api_status
            attempt["failure_reason"] = failure.reason
            attempt["wait_category"] = failure.wait_category
            attempt["retry_at_epoch"] = failure.retry_at_epoch

        final_response = None
        if return_code == 0 and outcome["terminal_result_succeeded"]:
            final_response = load_final_response(
                contract,
                observed_response=outcome["observed_response"],
                attempt_events=attempt_events,
                final_output_path=attempt_final,
            )
            if final_response is None:
                attempt["status"] = "failed"
                attempt["output_repair_reason"] = (
                    "missing_structured_final_response"
                )
                attempt["failure"] = (
                    "provider returned no valid current structured final "
                    "response"
                )
            else:
                provider_status = str(final_response["status"])
                attempt["provider_status"] = provider_status
                validation_error = submission_validation_error(
                    workspace,
                    contract,
                    final_response,
                )
                if validation_error is not None:
                    attempt["status"] = "failed"
                    attempt["output_repair_reason"] = (
                        "submission_validation_failed"
                    )
                    attempt["submission_validation_error"] = validation_error
                    attempt["failure"] = (
                        "provider final response did not have a valid "
                        f"matching submission: {validation_error}"
                    )
                    final_response = None
                if final_response is not None:
                    attempt["status"] = provider_status
                    write_json_atomic(
                        transcript / "final.json",
                        final_response,
                    )
        elif return_code == 0:
            attempt["status"] = "failed"
            attempt["failure"] = (
                "provider exited without a successful terminal event"
            )

        if final_response is not None:
            state["status"] = str(final_response["status"])
            state["completed_at"] = utc_now()
            state["final_response"] = final_response
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state

        if (
            failure is not None
            and failure.terminal
            and not resume_unavailable
        ):
            state["status"] = "provider_error"
            state["failure"] = failure.summary
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state

        if stop_requested.is_set():
            state["status"] = "interrupted"
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state
        if time.time() >= phase_deadline:
            continue
        state["status"] = "retrying"
        output_repair_required = isinstance(
            attempt.get("output_repair_reason"),
            str,
        )
        wait_category = (
            failure.wait_category
            if failure is not None and failure.wait_category
            else "runner_retry_wait"
        )
        now = time.time()
        requested_wait = (
            0.0
            if resume_unavailable or output_repair_required
            else retry_seconds
        )
        if (
            failure is not None
            and wait_category == "subscription_wait"
            and failure.retry_at_epoch is not None
        ):
            requested_wait = max(0.0, failure.retry_at_epoch - now)
        wait_seconds = min(
            requested_wait,
            max(0.0, phase_deadline - time.time()),
        )
        state["next_retry_seconds"] = wait_seconds
        state["next_retry_category"] = wait_category
        write_json_atomic(state_path, state)
        retry_activity_id = f"retry-{attempt_number}"
        timing_recorder.emit(
            "activity",
            phase="start",
            category=wait_category,
            activity_id=retry_activity_id,
            label=(
                "provider subscription boundary"
                if wait_category == "subscription_wait"
                else "runner retry backoff"
            ),
            source="runner",
            attempt=attempt_number,
        )
        if stop_requested.wait(wait_seconds):
            timing_recorder.emit(
                "activity",
                phase="end",
                category=wait_category,
                activity_id=retry_activity_id,
                source="runner",
                attempt=attempt_number,
            )
            state["status"] = "interrupted"
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state
        timing_recorder.emit(
            "activity",
            phase="end",
            category=wait_category,
            activity_id=retry_activity_id,
            source="runner",
            attempt=attempt_number,
        )
        if resume_unavailable or output_repair_required:
            retry_seconds = runtime.retry_initial_seconds
        elif wait_category == "runner_retry_wait":
            retry_seconds = min(
                retry_seconds * 2,
                runtime.retry_max_seconds,
            )
        else:
            retry_seconds = runtime.retry_initial_seconds

    state["status"] = "timeout" if not stop_requested.is_set() else "interrupted"
    if state["status"] == "timeout":
        state["failure"] = (
            "agent did not produce a final response before the finalization "
            "window ended"
        )
    state["completed_at"] = utc_now()
    write_json_atomic(state_path, state)
    write_terminal_artifacts(trial, state, adapter)
    return state


def run_trial(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    trial: Path,
    settings: ProviderSettings,
    *,
    runtime: RuntimeDefaults | None = None,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
    stop_requested: threading.Event | None = None,
) -> dict[str, Any]:
    definition.validate()
    return _run_configured_trial(
        AgentRunConfiguration(
            benchmark_id=definition.benchmark_id,
            benchmark_version=definition.version,
            rendered_prompt=definition.challenge.rendered_prompt,
            runtime=definition.runtime,
        ),
        contract,
        trial,
        settings,
        runtime=runtime,
        executable=executable,
        environment=environment,
        stop_requested=stop_requested,
    )


def run_staged_trial(
    trial: Path,
    settings: ProviderSettings,
    *,
    runtime: RuntimeDefaults | None = None,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
    stop_requested: threading.Event | None = None,
) -> dict[str, Any]:
    trial = trial.resolve()
    contract = load_output_contract(
        trial / "workspace/schema/output-contract.json"
    )
    configuration = load_agent_run_configuration(trial, contract)
    return _run_configured_trial(
        configuration,
        contract,
        trial,
        settings,
        runtime=runtime,
        executable=executable,
        environment=environment,
        stop_requested=stop_requested,
    )
