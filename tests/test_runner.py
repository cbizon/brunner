from __future__ import annotations

import json
import os
import subprocess
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
    continuation_prompt,
    finalization_prompt,
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


def test_success_event_without_ready_output_does_not_terminate_work(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    combined_events = tmp_path / "combined.jsonl"
    combined_stderr = tmp_path / "combined.stderr.log"
    marker = tmp_path / "background-complete"
    script = (
        "import json,pathlib,time;"
        "print(json.dumps({'type':'turn.completed'}),flush=True);"
        "time.sleep(0.12);"
        f"pathlib.Path({str(marker)!r}).write_text('complete')"
    )

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
        deadline_epoch=time.time() + 2,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.01,
        terminal_success_ready=lambda: False,
    )

    assert marker.read_text() == "complete"
    assert outcome["return_code"] == 0
    assert outcome["terminal_result_seen"] is True
    assert outcome["terminal_exit_ready"] is False
    assert outcome["lingering_processes_terminated"] is False


def test_model_substitution_is_terminal_provider_error(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            "model-substitution",
            "claude",
            "claude-fable-5",
            None,
        ),
    )
    binary = tmp_path / "claude"
    _write_python_executable(
        binary,
        r"""
import json
import time

print(json.dumps({
    "type": "system",
    "subtype": "init",
    "model": "claude-fable-5",
}), flush=True)
print(json.dumps({
    "type": "assistant",
    "message": {
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "substituted"}],
    },
}), flush=True)
time.sleep(60)
""",
    )

    started = time.monotonic()
    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(
            provider="claude",
            model="claude-fable-5",
        ),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=10,
            finalization_seconds=1,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
            provider_exit_grace_seconds=0.02,
        ),
    )

    assert time.monotonic() - started < 2
    assert state["status"] == "provider_error"
    assert state["model_mismatch"] == {
        "requested_model": "claude-fable-5",
        "observed_model": "claude-opus-5",
        "source": "assistant.message.model",
        "provider_event_index": 2,
    }
    assert "substituted model 'claude-opus-5'" in state["failure"]
    attempt = state["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["forced_termination_reason"] == "model_mismatch"
    assert attempt["observed_models"] == [
        {
            "model": "claude-opus-5",
            "source": "assistant.message.model",
            "provider_event_index": 2,
        }
    ]


def test_claude_harness_configuration_failure_is_not_retried(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            "claude-harness-failure",
            "claude",
            "claude-test",
            None,
        ),
    )
    binary = tmp_path / "claude"
    _write_executable(
        binary,
        """
count_file=.attempt-count
count=0
if [ -f "$count_file" ]; then
    count=$(cat "$count_file")
fi
count=$((count + 1))
printf '%s' "$count" > "$count_file"
echo 'unshare: unshare failed: Operation not permitted' >&2
exit 1
""",
    )

    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(
            provider="claude",
            model="claude-test",
        ),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=5,
            finalization_seconds=1,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
            max_attempts=10,
            provider_exit_grace_seconds=0.05,
        ),
    )

    assert state["status"] == "provider_error"
    assert len(state["attempts"]) == 1
    assert state["attempts"][0]["failure_reason"] == (
        "harness_configuration_error"
    )
    assert (trial / "workspace/.attempt-count").read_text() == "1"


def test_nonfinal_success_cannot_consume_finalization_window(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            "reserved-finalization",
            "codex",
            "fake-model",
            None,
        ),
    )
    binary = tmp_path / "codex"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import sys
import time

counter_path = pathlib.Path(".attempt-count")
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count))
if count == 1:
    print(json.dumps({"type": "turn.completed"}), flush=True)
    time.sleep(60)

arguments = sys.argv[1:]
final = pathlib.Path(
    arguments[arguments.index("--output-last-message") + 1]
)
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

    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(provider="codex", model="fake-model"),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=1,
            finalization_seconds=0.5,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
            provider_exit_grace_seconds=0.02,
        ),
    )

    assert state["status"] == "complete"
    assert [attempt["mode"] for attempt in state["attempts"]] == [
        "initial",
        "finalize",
    ]
    assert state["attempts"][0]["forced_termination_reason"] == (
        "soft_deadline"
    )


def test_prompt_delivery_cannot_block_the_hard_deadline(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    combined_events = tmp_path / "combined.jsonl"
    combined_stderr = tmp_path / "combined.stderr.log"

    started = time.monotonic()
    outcome = run_attempt(
        adapter=CodexAdapter(),
        command=(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ),
        workspace=tmp_path,
        environment=os.environ.copy(),
        prompt="x" * 2_000_000,
        attempt_events=events,
        attempt_stderr=stderr,
        combined_events=combined_events,
        combined_stderr=combined_stderr,
        deadline_epoch=time.time() + 0.1,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.02,
    )

    assert time.monotonic() - started < 2
    assert outcome["return_code"] != 0
    assert outcome["forced_termination_reason"] == "hard_deadline"


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


def test_provider_launch_failure_is_persisted(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("missing-provider", "codex", "fake-model", None),
    )
    missing_executable = tmp_path / "missing-codex"

    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(provider="codex", model="fake-model"),
        executable=str(missing_executable),
        runtime=RuntimeDefaults(
            timeout_seconds=5,
            finalization_seconds=1,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.02,
            provider_exit_grace_seconds=0.05,
        ),
    )

    persisted = json.loads((trial / "status.json").read_text())
    assert state["status"] == "provider_error"
    assert persisted == state
    assert state["attempts"][0]["return_code"] == 127
    assert str(missing_executable) in state["attempts"][0]["launch_error"]
    assert "provider launch failed" in state["failure"]


def test_codex_runner_waits_for_provider_subscription_reset(
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


def test_claude_runner_waits_for_captured_subscription_reset(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            "claude-subscription",
            "claude",
            "claude-opus-5",
            None,
        ),
    )
    binary = tmp_path / "claude"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import time

marker = pathlib.Path(".retried")
if not marker.exists():
    marker.write_text("1")
    reset = time.time() + 0.2
    message = "You've hit your limit · resets shortly (UTC)"
    print(json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "resetsAt": reset,
            "rateLimitType": "five_hour",
            "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled",
            "isUsingOverage": False,
        },
        "uuid": "rate-limit-event",
        "session_id": "captured-session",
    }), flush=True)
    print(json.dumps({
        "type": "assistant",
        "message": {
            "model": "<synthetic>",
            "role": "assistant",
            "content": [{"type": "text", "text": message}],
        },
        "parent_tool_use_id": None,
        "session_id": "captured-session",
        "error": "rate_limit",
    }), flush=True)
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 429,
        "result": message,
        "session_id": "captured-session",
        "usage": {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        },
    }), flush=True)
    raise SystemExit(1)

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
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": json.dumps(response),
    "session_id": "captured-session",
    "usage": {
        "input_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 1,
    },
}), flush=True)
""",
    )

    started = time.monotonic()
    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(
            provider="claude",
            model="claude-opus-5",
        ),
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
    limited = state["attempts"][0]
    assert limited["failure"] == (
        "You've hit your limit · resets shortly (UTC)"
    )
    assert limited["api_status"] == 429
    assert limited["failure_reason"] == "org_level_disabled"
    assert limited["wait_category"] == "subscription_wait"
    assert limited["retry_at_epoch"] is not None
    assert limited["model_mismatch"] is None
    assert elapsed < 1
    assert timing["summary"]["subscription_wait_seconds"] > 0


def test_claude_resume_repairs_missing_structured_final_response(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            "claude-structured-repair",
            "claude",
            "claude-opus-5",
            None,
        ),
    )
    binary = tmp_path / "claude"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import sys
import time

counter_path = pathlib.Path(".attempt-count")
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count))
prompt = sys.stdin.read()
with pathlib.Path(".prompts.jsonl").open("a") as stream:
    stream.write(json.dumps(prompt) + "\n")

if count == 1:
    message = "You've hit your limit"
    print(json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "resetsAt": time.time() + 0.05,
            "overageDisabledReason": "org_level_disabled",
        },
    }), flush=True)
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 429,
        "result": message,
        "usage": {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        },
    }), flush=True)
    raise SystemExit(1)

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
record = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": (
        "The benchmark is complete and all outputs are valid."
        if count == 2
        else json.dumps(response)
    ),
    "usage": {
        "input_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 1,
    },
}
if count == 3:
    record["structured_output"] = response
print(json.dumps(record), flush=True)
""",
    )

    started = time.monotonic()
    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(
            provider="claude",
            model="claude-opus-5",
        ),
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
    prompts = [
        json.loads(line)
        for line in (
            trial / "workspace/.prompts.jsonl"
        ).read_text().splitlines()
    ]

    assert state["status"] == "complete"
    assert len(state["attempts"]) == 3
    assert state["attempts"][0]["wait_category"] == "subscription_wait"
    assert state["attempts"][1]["output_repair_reason"] == (
        "missing_structured_final_response"
    )
    assert elapsed < 1
    assert len(prompts) == 3
    assert all("Return only the exact JSON object" in prompt for prompt in prompts)
    assert "previous attempt was rejected" not in prompts[1].lower()
    assert "previous attempt was rejected" in prompts[2].lower()


def test_continuation_and_finalization_prompts_require_json_handoff() -> None:
    contract = load_output_contract(definition().contract_path)

    prompts = (
        continuation_prompt(contract),
        finalization_prompt(contract),
        continuation_prompt(
            contract,
            repair_reason="missing_structured_final_response",
        ),
    )

    assert all("Return only the exact JSON object" in prompt for prompt in prompts)
    assert all("must not be wrapped in prose" in prompt for prompt in prompts)
    assert "previous attempt was rejected" in prompts[-1].lower()


def test_missing_resumed_session_retries_as_fresh_session(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("session-reset", "codex", "fake-model", None),
    )
    binary = tmp_path / "codex"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import sys

counter_path = pathlib.Path(".attempt-count")
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count))
resume = "resume" in sys.argv[1:]
with pathlib.Path(".modes").open("a") as stream:
    stream.write(("resume" if resume else "initial") + "\n")

if count == 1:
    print(json.dumps({
        "type": "error",
        "error": "temporary session creation failure",
    }), flush=True)
    raise SystemExit(1)
if count == 2:
    print(json.dumps({
        "type": "error",
        "error": "No session found for requested identifier",
    }), flush=True)
    raise SystemExit(1)

arguments = sys.argv[1:]
final = pathlib.Path(
    arguments[arguments.index("--output-last-message") + 1]
)
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

    state = run_trial(
        benchmark,
        contract,
        trial,
        ProviderSettings(provider="codex", model="fake-model"),
        executable=str(binary),
        runtime=RuntimeDefaults(
            timeout_seconds=5,
            finalization_seconds=1,
            retry_initial_seconds=0.1,
            retry_max_seconds=0.2,
            provider_exit_grace_seconds=0.05,
        ),
    )

    modes = (trial / "workspace/.modes").read_text().splitlines()
    assert state["status"] == "complete"
    assert modes == ["initial", "resume", "initial"]
    assert state["attempts"][1]["session_reset"] is True


def test_stale_canonical_final_response_is_ignored(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("stale-final", "codex", "fake-model", None),
    )
    stale_response = {
        "status": "complete",
        "submission_manifest": "submission/manifest.json",
        "completed_units": ["uppercase"],
        "limitations": [],
    }
    (trial / "transcript/final.json").write_text(
        json.dumps(stale_response)
    )
    binary = tmp_path / "codex"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import sys

counter_path = pathlib.Path(".attempt-count")
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count))
if count == 1:
    print(json.dumps({"type": "turn.completed"}), flush=True)
    raise SystemExit

arguments = sys.argv[1:]
final = pathlib.Path(
    arguments[arguments.index("--output-last-message") + 1]
)
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

    assert state["status"] == "complete"
    assert len(state["attempts"]) == 2
    assert "no valid current structured" in state["attempts"][0]["failure"]


def test_final_response_requires_successful_terminal_event(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("missing-terminal", "codex", "fake-model", None),
    )
    binary = tmp_path / "codex"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import sys

counter_path = pathlib.Path(".attempt-count")
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count))
arguments = sys.argv[1:]
final = pathlib.Path(
    arguments[arguments.index("--output-last-message") + 1]
)
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
if count > 1:
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

    assert state["status"] == "complete"
    assert len(state["attempts"]) == 2
    assert state["attempts"][0]["failure"] == (
        "provider exited without a successful terminal event"
    )


def test_complete_response_requires_valid_matching_submission(
    tmp_path: Path,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "tests",
        TrialIdentity("invalid-submission", "codex", "fake-model", None),
    )
    binary = tmp_path / "codex"
    _write_python_executable(
        binary,
        r"""
import json
import pathlib
import sys

counter_path = pathlib.Path(".attempt-count")
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count))
arguments = sys.argv[1:]
final = pathlib.Path(
    arguments[arguments.index("--output-last-message") + 1]
)
submission = pathlib.Path("submission")
submission.mkdir(exist_ok=True)
response = {
    "status": "complete",
    "submission_manifest": "submission/manifest.json",
    "completed_units": ["uppercase"],
    "limitations": [],
}
(submission / "run-status.json").write_text(json.dumps(response))
final.write_text(json.dumps(response))
if count > 1:
    (submission / "result.txt").write_text("HELLO\n")
    (submission / "manifest.json").write_text(
        '{"schema_version":"1.0","output":"result.txt"}\n'
    )
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

    assert state["status"] == "complete"
    assert len(state["attempts"]) == 2
    assert "manifest" in (
        state["attempts"][0]["submission_validation_error"]
    )


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
    late_output_path = tmp_path / "late-output"
    script = r"""
import os
import pathlib
import subprocess
import sys

pathlib.Path(os.environ["PROCESS_GROUP_PATH"]).write_text(
    str(os.getpgrp())
)
subprocess.Popen(
    [
        sys.executable,
        "-c",
        (
            "import os,pathlib,time;"
            "time.sleep(0.2);"
            "pathlib.Path(os.environ['LATE_OUTPUT_PATH'])"
            ".write_text('late')"
        ),
    ],
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
            "LATE_OUTPUT_PATH": str(late_output_path),
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
    time.sleep(0.3)
    assert not late_output_path.exists()


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


def _leaked_activity(
    path: Path,
    *,
    epoch: float,
    guard_pid: int | None = None,
) -> None:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "event": "activity",
        "phase": "start",
        "category": "background_job",
        "activity_id": "sim-1",
        "source": "benchmark",
        "epoch_seconds": epoch,
    }
    if guard_pid is not None:
        value["guard_pid"] = guard_pid
    path.write_text(json.dumps(value) + "\n")


def _soft_deadline_attempt(
    tmp_path: Path,
    activity_log: Path,
    **kwargs: object,
) -> dict[str, object]:
    return run_attempt(
        adapter=CodexAdapter(),
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        workspace=tmp_path,
        environment=os.environ.copy(),
        prompt="",
        attempt_events=tmp_path / "events.jsonl",
        attempt_stderr=tmp_path / "stderr.log",
        combined_events=tmp_path / "combined.jsonl",
        combined_stderr=tmp_path / "combined.stderr.log",
        deadline_epoch=time.time() + 20,
        soft_deadline_epoch=time.time() - 1,
        stop_requested=threading.Event(),
        terminal_exit_grace_seconds=0.2,
        external_activity_path=activity_log,
        **kwargs,
    )


def test_leaked_activity_from_earlier_attempt_does_not_block_deadline(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    _leaked_activity(log, epoch=time.time() - 100)

    outcome = _soft_deadline_attempt(
        tmp_path,
        log,
        attempt_start_epoch=time.time() - 10,
    )

    assert outcome["forced_termination_reason"] == "soft_deadline"


def test_leaked_activity_with_dead_guard_does_not_block_deadline(
    tmp_path: Path,
) -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    log = tmp_path / "activity.jsonl"
    _leaked_activity(log, epoch=time.time() - 100, guard_pid=finished.pid)

    outcome = _soft_deadline_attempt(tmp_path, log)

    assert outcome["forced_termination_reason"] == "soft_deadline"
    assert outcome["stale_activity_intervals"]


def test_leaked_activity_past_maximum_does_not_block_deadline(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    _leaked_activity(log, epoch=time.time() - 100)

    outcome = _soft_deadline_attempt(
        tmp_path,
        log,
        max_activity_interval_seconds=50,
    )

    assert outcome["forced_termination_reason"] == "soft_deadline"


def test_live_guarded_activity_still_defers_soft_deadline(
    tmp_path: Path,
) -> None:
    log = tmp_path / "activity.jsonl"
    _leaked_activity(log, epoch=time.time() - 100, guard_pid=os.getpid())

    started = time.monotonic()
    outcome = _soft_deadline_attempt(tmp_path, log)

    # Declared work held by a live process must still survive the soft
    # deadline, up to the hard deadline.
    assert outcome["forced_termination_reason"] == "hard_deadline"
    assert time.monotonic() - started > 5


def test_trial_stops_after_max_attempts(tmp_path: Path) -> None:
    contract = load_output_contract(EXAMPLE_ROOT / "output-contract.json")
    trial = create_trial(
        definition(),
        contract,
        tmp_path / "tests",
        TrialIdentity("attempt-cap", "codex", "model-a", None),
    )
    failing = tmp_path / "failing-provider"
    _write_executable(failing, "exit 3\n")

    state = run_trial(
        definition(),
        contract,
        trial,
        ProviderSettings(provider="codex", model="model-a"),
        runtime=RuntimeDefaults(
            timeout_seconds=120,
            finalization_seconds=60,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            max_attempts=3,
        ),
        executable=str(failing),
    )

    assert state["status"] == "provider_error"
    assert "3 attempts" in state["failure"]
    assert len(state["attempts"]) == 3
