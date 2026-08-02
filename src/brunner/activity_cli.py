from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Sequence

from brunner.timing import EXTERNAL_ACTIVITY_CATEGORIES, record_activity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunner-activity")
    parser.add_argument("--log", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "end"):
        selected = subparsers.add_parser(command)
        selected.add_argument(
            "category",
            choices=sorted(EXTERNAL_ACTIVITY_CATEGORIES),
        )
        selected.add_argument("activity_id")
        selected.add_argument("--label")
    run = subparsers.add_parser("run")
    run.add_argument(
        "category",
        choices=sorted(EXTERNAL_ACTIVITY_CATEGORIES),
    )
    run.add_argument("activity_id")
    run.add_argument("--label")
    run.add_argument("program", nargs=argparse.REMAINDER)
    return parser


def execute(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"start", "end"}:
        record_activity(
            args.command,
            args.category,
            args.activity_id,
            label=args.label,
            log_path=args.log,
        )
        return 0
    program = list(args.program)
    if program[:1] == ["--"]:
        program = program[1:]
    if not program:
        raise SystemExit("brunner-activity run requires a command after --")
    record_activity(
        "start",
        args.category,
        args.activity_id,
        label=args.label,
        log_path=args.log,
        guard_pid=True,
    )
    try:
        return subprocess.run(program, check=False).returncode
    finally:
        record_activity(
            "end",
            args.category,
            args.activity_id,
            label=args.label,
            log_path=args.log,
        )


def main() -> None:
    raise SystemExit(execute())


if __name__ == "__main__":
    main()
