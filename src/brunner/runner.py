from __future__ import annotations

import json
import os
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
from brunner.trial import load_trial_identity
from brunner.usage import read_json_records


PROVIDER_FINAL_STATUSES = frozenset({"complete", "partial", "failed"})
PIPELINE_TERMINAL_STATUSES = frozenset(
    {*PROVIDER_FINAL_STATUSES, "provider_error", "timeout"}
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


def continuation_prompt(contract: OutputContract) -> str:
    return (
        "Continue the benchmark from the persistent workspace and provider "
        "session. Inspect existing work, complete every required output in "
        f"{contract.submission_manifest}, validate it against the schemas, "
        f"and update {contract.run_status_path}. Do not stop after planning."
    )


def finalization_prompt(contract: OutputContract) -> str:
    return (
        "The benchmark deadline is approaching. Stop starting long-running "
        "work. Preserve all useful outputs already produced and write the best "
        f"valid {contract.submission_manifest} possible. Always write "
        f"{contract.run_status_path}. Report complete only when all required "
        "outputs are valid, partial when useful valid work exists, or failed "
        "otherwise. Finish now."
    )


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def pump_stream(
    source: IO[str],
    attempt_output: IO[str],
    combined_output: IO[str],
    on_line: Callable[[str], None] | None = None,
) -> None:
    for line in source:
        attempt_output.write(line)
        attempt_output.flush()
        combined_output.write(line)
        combined_output.flush()
        if on_line is not None:
            on_line(line)


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
) -> dict[str, Any]:
    terminal_seen = threading.Event()
    terminal_succeeded = threading.Event()
    observed_response: dict[str, Any] | None = None
    observation_lock = threading.Lock()

    def inspect_line(line: str) -> None:
        nonlocal observed_response
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(record, dict):
            return
        observation = adapter.observe_record(record)
        with observation_lock:
            if observation.final_response is not None:
                observed_response = observation.final_response
        if observation.terminal:
            if observation.succeeded:
                terminal_succeeded.set()
            terminal_seen.set()

    with (
        attempt_events.open("w") as attempt_stdout,
        attempt_stderr.open("w") as attempt_error,
        combined_events.open("a") as combined_stdout,
        combined_stderr.open("a") as combined_error,
    ):
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
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=pump_stream,
            args=(
                process.stdout,
                attempt_stdout,
                combined_stdout,
                inspect_line,
            ),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=pump_stream,
            args=(
                process.stderr,
                attempt_error,
                combined_error,
            ),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except BrokenPipeError:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass

        terminal_seen_at = None
        lingering_processes_terminated = False
        while process.poll() is None:
            if terminal_seen.is_set():
                if terminal_seen_at is None:
                    terminal_seen_at = time.monotonic()
                elif (
                    time.monotonic() - terminal_seen_at
                    >= terminal_exit_grace_seconds
                ):
                    terminate_process(process)
                    lingering_processes_terminated = True
                    break
            if stop_requested.is_set() or time.time() >= deadline_epoch:
                terminate_process(process)
                break
            wait_seconds = 1.0
            if terminal_seen_at is not None:
                remaining = terminal_exit_grace_seconds - (
                    time.monotonic() - terminal_seen_at
                )
                wait_seconds = min(wait_seconds, max(0.0, remaining))
            stop_requested.wait(wait_seconds)
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)

    provider_return_code = (
        process.returncode if process.returncode is not None else 124
    )
    if terminal_succeeded.is_set():
        return_code = 0
    elif terminal_seen.is_set():
        return_code = provider_return_code or 1
    else:
        return_code = provider_return_code
    return {
        "return_code": return_code,
        "provider_return_code": provider_return_code,
        "terminal_result_seen": terminal_seen.is_set(),
        "terminal_result_succeeded": terminal_succeeded.is_set(),
        "lingering_processes_terminated": lingering_processes_terminated,
        "observed_response": observed_response,
    }


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
    final_paths: tuple[Path, ...],
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
    for path in final_paths:
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        response = _valid_response(value, contract)
        if response is not None:
            return response
    return None


def write_terminal_artifacts(
    trial: Path,
    state: dict[str, Any],
    adapter: ProviderAdapter,
) -> None:
    created_epoch = float(state.get("created_epoch", time.time()))
    deadline_epoch = float(state["deadline_epoch"])
    last_attempt = state["attempts"][-1] if state["attempts"] else {}
    write_json_atomic(
        trial / "timing/goal.json",
        {
            "schema_version": "1.0",
            "started_at": state["created_at"],
            "ended_at": state.get("completed_at", utc_now()),
            "elapsed_seconds": max(0.0, time.time() - created_epoch),
            "timeout_seconds": max(0.0, deadline_epoch - created_epoch),
            "status": state["status"],
            "return_code": last_attempt.get("return_code"),
            "attempt_count": len(state["attempts"]),
            "failure": state.get("failure"),
            "finalization_started_at": state.get("finalization_started_at"),
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
    for path in (transcript, attempts_root, provider_home):
        path.mkdir(parents=True, exist_ok=True)
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

    run_environment = os.environ.copy()
    run_environment.update(environment or {})
    run_environment.update(settings.extra_environment)
    run_environment["HOME"] = str(provider_home)
    run_environment["CODEX_HOME"] = str(provider_home / "codex")
    Path(run_environment["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
    stop_requested = stop_requested or threading.Event()
    retry_seconds = runtime.retry_initial_seconds
    work_deadline = state["deadline_epoch"] - runtime.finalization_seconds
    combined_events = transcript / "events.jsonl"
    combined_stderr = transcript / "stderr.log"

    while time.time() < state["deadline_epoch"] and not stop_requested.is_set():
        finalizing = bool(state.get("finalization_started"))
        attempt_deadline = state["deadline_epoch"] if finalizing else work_deadline
        if time.time() >= attempt_deadline:
            if finalizing:
                break
            state["finalization_started"] = True
            state["finalization_started_at"] = utc_now()
            state["status"] = "finalizing"
            state.pop("next_retry_seconds", None)
            write_json_atomic(state_path, state)
            retry_seconds = runtime.retry_initial_seconds
            continue

        attempt_number = len(state["attempts"]) + 1
        attempt_prefix = attempts_root / f"{attempt_number:04d}"
        attempt_events = attempt_prefix.with_suffix(".events.jsonl")
        attempt_stderr = attempt_prefix.with_suffix(".stderr.log")
        resume_session = bool(state["session_started"])
        context = ProviderRunContext(
            workspace=workspace,
            transcript_dir=transcript,
            final_schema_path=workspace / "schema/final-response.schema.json",
            final_output_path=transcript / "final.json",
            persist_session=True,
            resume_session=resume_session,
            session_id=state["session_id"],
            executable=executable,
        )
        provider_command = adapter.build_command(settings, context)
        attempt = {
            "number": attempt_number,
            "status": "running",
            "mode": (
                "finalize"
                if finalizing
                else ("resume" if resume_session else "initial")
            ),
            "started_at": utc_now(),
            "events": str(attempt_events.relative_to(trial)),
            "stderr": str(attempt_stderr.relative_to(trial)),
        }
        state["attempts"].append(attempt)
        state["status"] = "finalizing" if finalizing else "running"
        write_json_atomic(state_path, state)
        if finalizing:
            prompt = finalization_prompt(contract)
        elif resume_session:
            prompt = continuation_prompt(contract)
        else:
            prompt = (
                workspace / configuration.rendered_prompt
            ).read_text()

        attempt_environment = {
            **run_environment,
            **provider_command.environment,
        }
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
            deadline_epoch=attempt_deadline,
            stop_requested=stop_requested,
            terminal_exit_grace_seconds=runtime.provider_exit_grace_seconds,
        )
        attempt.update(
            {
                key: value
                for key, value in outcome.items()
                if key != "observed_response"
            }
        )
        attempt["ended_at"] = utc_now()
        return_code = int(outcome["return_code"])
        attempt["status"] = "complete" if return_code == 0 else "failed"
        if attempt_events.stat().st_size:
            state["session_started"] = True

        records = read_json_records(attempt_events)
        stderr = (
            attempt_stderr.read_text(errors="replace")
            if attempt_stderr.is_file()
            else ""
        )
        failure = adapter.classify_failure(records, stderr)
        if failure is not None:
            attempt["failure"] = failure.summary
            attempt["api_status"] = failure.api_status
            attempt["failure_reason"] = failure.reason

        final_response = None
        if return_code == 0:
            final_response = load_final_response(
                contract,
                observed_response=outcome["observed_response"],
                attempt_events=attempt_events,
                final_paths=(
                    transcript / "final.json",
                    workspace / contract.run_status_path,
                ),
            )
            if final_response is None:
                attempt["status"] = "failed"
                attempt["failure"] = (
                    "provider returned no valid structured final response"
                )
            else:
                attempt["status"] = final_response["status"]
                attempt["provider_status"] = final_response["status"]
                write_json_atomic(transcript / "final.json", final_response)

        if final_response is not None:
            state["status"] = str(final_response["status"])
            state["completed_at"] = utc_now()
            state["final_response"] = final_response
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state

        if failure is not None and failure.terminal:
            state["status"] = "provider_error"
            state["failure"] = failure.summary
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state

        if resume_session and adapter.resume_is_unavailable(stderr):
            state["session_started"] = False
        if stop_requested.is_set():
            state["status"] = "interrupted"
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state
        if time.time() >= attempt_deadline:
            continue
        state["status"] = "retrying"
        state["next_retry_seconds"] = retry_seconds
        write_json_atomic(state_path, state)
        wait_seconds = min(
            retry_seconds,
            max(0.0, attempt_deadline - time.time()),
        )
        if stop_requested.wait(wait_seconds):
            state["status"] = "interrupted"
            state["completed_at"] = utc_now()
            write_json_atomic(state_path, state)
            write_terminal_artifacts(trial, state, adapter)
            return state
        retry_seconds = min(retry_seconds * 2, runtime.retry_max_seconds)

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
