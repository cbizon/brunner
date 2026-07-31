from __future__ import annotations

import argparse
import json
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
        runtime = RuntimeDefaults(
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
            retry_initial_seconds=defaults.retry_initial_seconds,
            retry_max_seconds=defaults.retry_max_seconds,
            provider_exit_grace_seconds=(
                defaults.provider_exit_grace_seconds
            ),
            backend_shutdown_grace_seconds=(
                defaults.backend_shutdown_grace_seconds
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
    )
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
