from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from brunner.evaluation import (
    evaluation_spec_from_dict,
    execute_evaluation,
)
from brunner import BRUNNER_RUNTIME_PROTOCOL


SPEC_ENV = "BRUNNER_EVALUATION_SPEC"
TERMINATION_LOG_ENV = "BRUNNER_TERMINATION_LOG"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunner-evaluator")
    parser.add_argument("trial", type=Path)
    return parser


def _write_termination_summary(summary: dict[str, Any]) -> None:
    value = os.environ.get(TERMINATION_LOG_ENV)
    if not value:
        return
    try:
        Path(value).write_text(
            json.dumps(
                {"brunner_evaluation": summary},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    except OSError as error:
        print(
            f"could not write evaluator termination summary: {error}",
            file=sys.stderr,
        )


def main() -> None:
    args = build_parser().parse_args()
    summary: dict[str, Any]
    try:
        encoded = os.environ.get(SPEC_ENV)
        if not encoded:
            raise RuntimeError(
                f"missing trusted evaluator environment variable {SPEC_ENV}"
            )
        value = json.loads(encoded)
        if not isinstance(value, dict):
            raise TypeError("trusted evaluation specification is not an object")
        spec = evaluation_spec_from_dict(value)
        if spec.runtime_protocol != BRUNNER_RUNTIME_PROTOCOL:
            raise RuntimeError(
                "trusted evaluator runtime protocol mismatch: "
                f"{spec.runtime_protocol!r} != "
                f"{BRUNNER_RUNTIME_PROTOCOL!r}"
            )
        result = execute_evaluation(
            spec,
            args.trial,
            reference_root=(
                Path("/brunner/reference")
                if spec.reference_manifest_path is not None
                else None
            ),
        )
        failure = result.get("failure")
        summary = {
            "status": result["status"],
            "failure": failure if isinstance(failure, dict) else None,
            "candidate_failure": bool(
                isinstance(failure, dict)
                and failure.get("domain") == "candidate"
            ),
            "retryable_infrastructure": False,
        }
        exit_code = (
            0
            if result["status"] == "complete" or summary["candidate_failure"]
            else 2
        )
    except Exception as error:
        summary = {
            "status": "failed",
            "failure": {
                "domain": "evaluation",
                "reason": type(error).__name__,
                "message": str(error),
            },
            "candidate_failure": False,
            "retryable_infrastructure": False,
        }
        exit_code = 2
        print(f"trusted evaluator bootstrap failed: {error}", file=sys.stderr)
    summary["process_exit_code"] = exit_code
    _write_termination_summary(summary)
    print(json.dumps(summary, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
