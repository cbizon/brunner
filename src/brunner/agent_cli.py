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
    stop_requested = threading.Event()
    received_signal: int | None = None

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
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if received_signal is not None:
        state["interruption"] = {
            "signal": received_signal,
            "signal_name": signal.Signals(received_signal).name,
            "received_at": datetime.now(UTC).isoformat(),
        }
        write_json_atomic(args.trial / "status.json", state)
    summary = summarize_pipeline_state(state)
    exit_code = _exit_code(summary, received_signal)
    summary["process_exit_code"] = exit_code
    _write_termination_summary(summary)
    print(json.dumps(state, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
