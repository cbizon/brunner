from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from brunner.definition import RuntimeDefaults
from brunner.failure import failure_from_exception
from brunner.io import write_json_atomic
from brunner.pipeline import summarize_pipeline_state
from brunner.providers import ProviderSettings
from brunner.runner import run_staged_trial
from brunner.trial import load_trial_identity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunner-agent")
    parser.add_argument("trial", type=Path)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--finalization-seconds", type=float)
    parser.add_argument("--provider-executable")
    return parser


def _exit_code(
    summary: dict[str, object],
    received_signal: int | None,
) -> int:
    if received_signal is not None:
        return 128 + received_signal
    if summary["provider_result_present"] is True:
        return 0
    if summary["status"] == "timeout":
        return 124
    if summary["status"] == "interrupted":
        return 75
    return 1


def _write_termination_summary(
    summary: dict[str, object],
) -> None:
    value = os.environ.get("BRUNNER_TERMINATION_LOG")
    if not value:
        return
    path = Path(value)
    try:
        path.write_text(
            json.dumps(
                {"brunner_pipeline": summary},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    except OSError as error:
        print(
            f"could not write Brunner termination summary to {path}: {error}",
            file=sys.stderr,
        )


def main() -> None:
    args = build_parser().parse_args()
    stop_requested = threading.Event()
    received_signal: int | None = None
    state: dict[str, object] | None = None

    def request_stop(
        signum: int,
        _frame: object,
    ) -> None:
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum
        stop_requested.set()

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        try:
            identity = load_trial_identity(args.trial)
            runtime = None
            if (
                args.timeout_seconds is not None
                or args.finalization_seconds is not None
            ):
                staged = json.loads(
                    (args.trial / "metadata/agent-run.json").read_text()
                )
                defaults = RuntimeDefaults(**staged["runtime"])
                runtime = replace(
                    defaults,
                    timeout_seconds=(
                        args.timeout_seconds
                        if args.timeout_seconds is not None
                        else defaults.timeout_seconds
                    ),
                    finalization_seconds=(
                        args.finalization_seconds
                        if args.finalization_seconds is not None
                        else defaults.finalization_seconds
                    ),
                )
            state = run_staged_trial(
                args.trial,
                ProviderSettings(
                    provider=identity.provider,
                    model=identity.model,
                    effort=identity.effort,
                ),
                runtime=runtime,
                executable=args.provider_executable,
                stop_requested=stop_requested,
            )
        except Exception as error:
            failure = failure_from_exception(
                error,
                operation="agent_harness",
                domain="orchestrator",
                reason="AgentHarnessFailed",
                disposition="terminal",
                retryable=False,
                resource="agent_runtime",
            )
            state = {}
            status_path = args.trial / "status.json"
            if status_path.is_file():
                try:
                    previous = json.loads(status_path.read_text())
                except (json.JSONDecodeError, OSError):
                    previous = None
                if isinstance(previous, dict):
                    state.update(previous)
            state.setdefault("schema_version", "2.0")
            state.setdefault("attempts", [])
            state["status"] = (
                "interrupted"
                if received_signal is not None
                else "provider_error"
            )
            state["failure"] = f"{type(error).__name__}: {error}"
            state["harness_failure"] = failure
            state["updated_at"] = datetime.now(UTC).isoformat()
            try:
                write_json_atomic(status_path, state)
            except OSError as persistence_error:
                print(
                    "could not persist Brunner agent harness failure: "
                    f"{persistence_error}",
                    file=sys.stderr,
                )
        if received_signal is not None:
            state["status"] = "interrupted"
            state["interruption"] = {
                "signal": received_signal,
                "signal_name": signal.Signals(received_signal).name,
                "received_at": datetime.now(UTC).isoformat(),
            }
            try:
                write_json_atomic(args.trial / "status.json", state)
            except OSError as error:
                print(
                    f"could not persist Brunner interruption: {error}",
                    file=sys.stderr,
                )
        summary = summarize_pipeline_state(state)
        exit_code = _exit_code(summary, received_signal)
        summary["process_exit_code"] = exit_code
        if isinstance(state.get("harness_failure"), dict):
            summary["harness_failure"] = state["harness_failure"]
        _write_termination_summary(summary)
        print(json.dumps(state, indent=2))
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
