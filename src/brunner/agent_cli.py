from __future__ import annotations

import argparse
import json
import signal
import threading
from dataclasses import replace
from pathlib import Path

from brunner.definition import RuntimeDefaults
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

    def request_stop(
        _signum: int,
        _frame: object,
    ) -> None:
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
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
