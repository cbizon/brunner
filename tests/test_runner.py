from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from brunner.contract import load_output_contract
from brunner.definition import (
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
    RuntimeDefaults,
)
from brunner.providers import CodexAdapter, ProviderSettings
from brunner.runner import run_attempt, run_staged_trial, run_trial
from brunner.submission import validate_submission
from brunner.trial import TrialIdentity, create_trial


ROOT = Path(__file__).parents[1]
EXAMPLE_ROOT = ROOT / "examples/text_benchmark"


def definition() -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="text-uppercase",
        version="1.0.0",
        root=EXAMPLE_ROOT,
        contract_path=EXAMPLE_ROOT / "output-contract.json",
        challenge=ChallengeDefinition(root=EXAMPLE_ROOT / "challenge"),
        evaluation=EvaluationDefinition(command=(sys.executable, "-c", "pass")),
    )


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


def test_run_attempt_terminates_process_after_terminal_result(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    combined_events = tmp_path / "combined.jsonl"
    combined_stderr = tmp_path / "combined.stderr.log"
    script = (
        "import json,time;"
        "print(json.dumps({'type':'turn.completed'}),flush=True);"
        "time.sleep(60)"
    )

    started = time.monotonic()
    outcome = run_attempt(
        adapter=CodexAdapter(),
        command=(sys.executable, "-c", script),
        workspace=tmp_path,
        environment=os.environ.copy(),
        prompt="",
        attempt_events=events,
        attempt_stderr=stderr,
        combined_events=combined_events,
        combined_stderr=combined_stderr,
        deadline_epoch=time.time() + 30,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.05,
    )

    assert time.monotonic() - started < 5
    assert outcome["return_code"] == 0
    assert outcome["terminal_result_seen"] is True
    assert outcome["lingering_processes_terminated"] is True


def test_durable_runner_produces_contract_valid_submission(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    identity = TrialIdentity(
        test_id="text-run",
        provider="codex",
        model="fake-model",
        effort="high",
    )
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        identity,
    )
    binary = tmp_path / "codex"
    _write_executable(
        binary,
        r"""
final=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--output-last-message" ]; then final="$argument"; fi
  previous="$argument"
done
mkdir -p submission
tr '[:lower:]' '[:upper:]' < input.txt > submission/result.txt
printf '%s\n' '{"schema_version":"1.0","output":"result.txt"}' > submission/manifest.json
printf '%s\n' '{"status":"complete","submission_manifest":"submission/manifest.json","completed_units":["uppercase"],"limitations":[]}' > submission/run-status.json
cp submission/run-status.json "$final"
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}'
""",
    )
    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(
            provider="codex",
            model="fake-model",
            effort="high",
        ),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=10,
            finalization_seconds=1,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
            provider_exit_grace_seconds=0.05,
        ),
    )

    validated = validate_submission(trial / "workspace", contract)
    usage = json.loads((trial / "usage/usage.json").read_text())
    assert state["status"] == "complete"
    assert validated.run_status["completed_units"] == ["uppercase"]
    assert usage["total_tokens"] == 5


def test_staged_runner_does_not_require_benchmark_package(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("staged", "codex", "fake-model", None),
    )
    binary = tmp_path / "codex"
    _write_executable(
        binary,
        r"""
final=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--output-last-message" ]; then final="$argument"; fi
  previous="$argument"
done
mkdir -p submission
tr '[:lower:]' '[:upper:]' < input.txt > submission/result.txt
printf '%s\n' '{"schema_version":"1.0","output":"result.txt"}' > submission/manifest.json
printf '%s\n' '{"status":"complete","submission_manifest":"submission/manifest.json","completed_units":["uppercase"],"limitations":[]}' > submission/run-status.json
cp submission/run-status.json "$final"
printf '%s\n' '{"type":"turn.completed"}'
""",
    )

    state = run_staged_trial(
        trial,
        ProviderSettings(provider="codex", model="fake-model"),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=10,
            finalization_seconds=1,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
            provider_exit_grace_seconds=0.05,
        ),
    )

    assert state["status"] == "complete"
