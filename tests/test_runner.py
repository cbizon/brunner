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
from brunner.runner import (
    process_group_alive,
    run_attempt,
    run_staged_trial,
    run_trial,
)
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


def _write_python_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n" + body)
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
printf '%s\n' '{"type":"item.started","item":{"id":"tool-1","type":"command_execution","command":"python simulate.py"}}'
sleep 0.02
printf '%s\n' '{"type":"item.completed","item":{"id":"tool-1","type":"command_execution","command":"python simulate.py","status":"completed","exit_code":0}}'
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
    timing = json.loads((trial / "timing/accounting.json").read_text())
    assert state["status"] == "complete"
    assert validated.run_status["completed_units"] == ["uppercase"]
    assert usage["total_tokens"] == 5
    assert usage["logical_input_tokens"] == 3
    assert usage["provider"] == "codex"
    assert timing["summary"]["wall_seconds"] >= 0
    assert timing["summary"]["unclassified_seconds"] == 0
    assert timing["summary"]["foreground_tool_seconds"] > 0


def test_runner_waits_for_provider_subscription_reset(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("subscription", "codex", "fake-model", None),
    )
    binary = tmp_path / "codex"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import sys
import time

marker = pathlib.Path(".retried")
if not marker.exists():
    marker.write_text("1")
    print(json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "resetsAt": time.time() + 0.05,
            "overageDisabledReason": "org_level_disabled",
        },
    }), flush=True)
    print(json.dumps({"type": "turn.failed"}), flush=True)
    raise SystemExit(1)

arguments = sys.argv[1:]
final = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
submission = pathlib.Path("submission")
submission.mkdir(exist_ok=True)
(submission / "result.txt").write_text("HELLO\n")
(submission / "manifest.json").write_text(
    '{"schema_version":"1.0","output":"result.txt"}\n'
)
response = {
    "status": "complete",
    "submission_manifest": "submission/manifest.json",
    "completed_units": ["uppercase"],
    "limitations": [],
}
(submission / "run-status.json").write_text(json.dumps(response))
final.write_text(json.dumps(response))
print(json.dumps({"type": "turn.completed"}), flush=True)
""",
    )

    started = time.monotonic()
    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(provider="codex", model="fake-model"),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=5,
            finalization_seconds=1,
            retry_initial_seconds=2,
            retry_max_seconds=2,
            provider_exit_grace_seconds=0.05,
        ),
    )
    elapsed = time.monotonic() - started
    timing = json.loads((trial / "timing/accounting.json").read_text())

    assert state["status"] == "complete"
    assert len(state["attempts"]) == 2
    assert state["attempts"][0]["wait_category"] == "subscription_wait"
    assert elapsed < 1
    assert timing["summary"]["subscription_wait_seconds"] > 0


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


def test_soft_deadline_waits_for_declared_external_activity(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    combined_events = tmp_path / "combined.jsonl"
    combined_stderr = tmp_path / "combined.stderr.log"
    activity_log = tmp_path / "activity.jsonl"
    script = r"""
import json
import os
import time

path = os.environ["ACTIVITY_LOG"]
def emit(phase):
    with open(path, "a") as stream:
        stream.write(json.dumps({
            "event": "activity",
            "phase": phase,
            "category": "external_wait",
            "activity_id": "simulation",
            "source": "benchmark",
            "epoch_seconds": time.time(),
        }) + "\n")
        stream.flush()

emit("start")
time.sleep(0.12)
emit("end")
time.sleep(0.02)
print(json.dumps({"type": "turn.completed"}), flush=True)
"""

    started = time.monotonic()
    outcome = run_attempt(
        adapter=CodexAdapter(),
        command=(sys.executable, "-c", script),
        workspace=tmp_path,
        environment={
            **os.environ,
            "ACTIVITY_LOG": str(activity_log),
        },
        prompt="",
        attempt_events=events,
        attempt_stderr=stderr,
        combined_events=combined_events,
        combined_stderr=combined_stderr,
        soft_deadline_epoch=time.time() + 0.03,
        deadline_epoch=time.time() + 1,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.05,
        external_activity_path=activity_log,
    )

    assert time.monotonic() - started >= 0.12
    assert outcome["return_code"] == 0
    assert outcome["soft_deadline_activity_seen"] is True
    assert outcome["active_work_terminated"] is False


def test_soft_deadline_waits_for_provider_tool_without_timing_recorder(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    combined_events = tmp_path / "combined.jsonl"
    combined_stderr = tmp_path / "combined.stderr.log"
    script = r"""
import json
import time

item = {
    "id": "tool-1",
    "type": "command_execution",
    "command": "python simulate.py",
}
print(json.dumps({"type": "item.started", "item": item}), flush=True)
time.sleep(0.12)
print(json.dumps({"type": "item.completed", "item": item}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
"""

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
        soft_deadline_epoch=time.time() + 0.03,
        deadline_epoch=time.time() + 1,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.02,
    )

    assert time.monotonic() - started >= 0.12
    assert outcome["return_code"] == 0
    assert outcome["soft_deadline_activity_seen"] is True
    assert outcome["active_work_terminated"] is False


def test_terminal_event_waits_for_declared_background_work(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    combined_events = tmp_path / "combined.jsonl"
    combined_stderr = tmp_path / "combined.stderr.log"
    activity_log = tmp_path / "activity.jsonl"
    script = r"""
import json
import os
import time

path = os.environ["ACTIVITY_LOG"]
def emit(phase):
    with open(path, "a") as stream:
        stream.write(json.dumps({
            "event": "activity",
            "phase": phase,
            "category": "background_job",
            "activity_id": "simulation",
            "source": "benchmark",
            "epoch_seconds": time.time(),
        }) + "\n")
        stream.flush()

emit("start")
print(json.dumps({"type": "turn.completed"}), flush=True)
time.sleep(0.12)
emit("end")
"""

    started = time.monotonic()
    outcome = run_attempt(
        adapter=CodexAdapter(),
        command=(sys.executable, "-c", script),
        workspace=tmp_path,
        environment={
            **os.environ,
            "ACTIVITY_LOG": str(activity_log),
        },
        prompt="",
        attempt_events=events,
        attempt_stderr=stderr,
        combined_events=combined_events,
        combined_stderr=combined_stderr,
        deadline_epoch=time.time() + 1,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.02,
        external_activity_path=activity_log,
    )

    assert time.monotonic() - started >= 0.12
    assert outcome["return_code"] == 0
    assert outcome["lingering_processes_terminated"] is False
    assert outcome["active_work_terminated"] is False


def test_orphaned_process_group_is_reaped_before_return(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    combined_events = tmp_path / "combined.jsonl"
    combined_stderr = tmp_path / "combined.stderr.log"
    process_group_path = tmp_path / "process-group"
    script = r"""
import os
import pathlib
import subprocess
import sys

pathlib.Path(os.environ["PROCESS_GROUP_PATH"]).write_text(
    str(os.getpgrp())
)
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
)
"""

    started = time.monotonic()
    outcome = run_attempt(
        adapter=CodexAdapter(),
        command=(sys.executable, "-c", script),
        workspace=tmp_path,
        environment={
            **os.environ,
            "PROCESS_GROUP_PATH": str(process_group_path),
        },
        prompt="",
        attempt_events=events,
        attempt_stderr=stderr,
        combined_events=combined_events,
        combined_stderr=combined_stderr,
        deadline_epoch=time.time() + 2,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.02,
    )

    process_group_id = int(process_group_path.read_text())
    assert time.monotonic() - started < 1
    assert outcome["return_code"] != 0
    assert outcome["forced_termination_reason"] == "orphaned_process_group"
    assert outcome["lingering_processes_terminated"] is True
    assert process_group_alive(process_group_id) is False


def test_runner_restores_agent_control_plane_mutation(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("tamper", "codex", "fake-model", None),
    )
    metadata_path = trial / "metadata/manifest.json"
    agent_run_path = trial / "metadata/agent-run.json"
    backend_rogue_path = trial / "backend/rogue.json"
    original_metadata = metadata_path.read_bytes()
    original_agent_run = agent_run_path.read_bytes()
    binary = tmp_path / "codex"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib

pathlib.Path("../metadata/manifest.json").write_text("{}")
pathlib.Path("../metadata/agent-run.json").unlink()
pathlib.Path("../backend/rogue.json").write_text("{}")
print(json.dumps({"type": "turn.completed"}), flush=True)
""",
    )

    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(provider="codex", model="fake-model"),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=5,
            finalization_seconds=1,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
            provider_exit_grace_seconds=0.05,
        ),
    )

    assert state["status"] == "provider_error"
    assert "metadata/manifest.json" in state["failure"]
    assert metadata_path.read_bytes() == original_metadata
    assert agent_run_path.read_bytes() == original_agent_run
    assert not backend_rogue_path.exists()
