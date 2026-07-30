from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from brunner.io import write_json_atomic


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _terminate(process: subprocess.Popen[bytes]) -> None:
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
        process.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--environment-json", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SystemExit("worker command cannot be empty")
    environment = os.environ.copy()
    environment.update(json.loads(args.environment_json))
    state = json.loads(args.state.read_text())
    state.update(
        {
            "phase": "running",
            "started_at": _now(),
            "worker_pid": os.getpid(),
        }
    )
    write_json_atomic(args.state, state)
    args.stdout.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with args.stdout.open("wb") as stdout, args.stderr.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=args.cwd,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            state.update(
                {
                    "phase": "failed",
                    "reason": type(error).__name__,
                    "message": str(error),
                    "finished_at": _now(),
                }
            )
            write_json_atomic(args.state, state)
            return 127
        state["workload_pid"] = process.pid
        write_json_atomic(args.state, state)
        try:
            return_code = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(process)
            return_code = 124
    state.update(
        {
            "phase": "failed" if return_code else "succeeded",
            "reason": "DeadlineExceeded" if timed_out else None,
            "message": (
                f"workload exceeded {args.timeout} seconds"
                if timed_out
                else None
            ),
            "exit_code": return_code,
            "finished_at": _now(),
        }
    )
    write_json_atomic(args.state, state)
    return return_code


if __name__ == "__main__":
    sys.exit(main())
