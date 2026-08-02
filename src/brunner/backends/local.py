from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brunner.artifacts import collect_local_artifacts
from brunner.backends.base import (
    BackendCapacity,
    BackendHandle,
    BackendSnapshot,
    WorkloadSpec,
    backend_registry_key,
)
from brunner.definition import ArtifactPolicy
from brunner.errors import BackendRequestError
from brunner.io import write_json_atomic


def _now() -> str:
    return datetime.now(UTC).isoformat()


LAUNCH_GRACE_SECONDS = 60.0


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LocalBackend:
    name = "local"

    def __init__(self, *, max_parallel: int | None = None) -> None:
        self.max_parallel = max_parallel or max(1, os.cpu_count() or 1)
        self._handles: dict[tuple[str, str], BackendHandle] = {}

    @staticmethod
    def _root(trial: Path) -> Path:
        return trial / "backend/local"

    @classmethod
    def _state_path(cls, trial: Path) -> Path:
        return cls._root(trial) / "state.json"

    @classmethod
    def _launcher_path(cls, trial: Path) -> Path:
        # Kept separate from state.json: the worker owns that file and would
        # otherwise race the launcher's write.
        return cls._root(trial) / "launcher.json"

    @classmethod
    def _launcher(cls, trial: Path) -> dict[str, Any]:
        path = cls._launcher_path(trial)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _watch_launcher(
        cls,
        worker: subprocess.Popen[bytes],
        trial: Path,
    ) -> None:
        return_code = worker.wait()
        launcher = cls._launcher(trial)
        launcher.update(
            {
                "exit_code": return_code,
                "exited_at": _now(),
            }
        )
        write_json_atomic(cls._launcher_path(trial), launcher)

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        workload.validate()
        root = self._root(workload.trial)
        root.mkdir(parents=True, exist_ok=True)
        state_path = self._state_path(workload.trial)
        handle = BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=workload.workload_id,
            trial=workload.trial.resolve(),
            metadata={"state_path": str(state_path)},
        )
        if state_path.is_file():
            state = json.loads(state_path.read_text())
            if state.get("workload_id") != workload.workload_id:
                raise BackendRequestError(
                    "local backend state belongs to another workload"
                )
            self._handles[
                backend_registry_key(workload.workload_id, workload.trial)
            ] = handle
            return handle

        stdout_path = root / "stdout.log"
        stderr_path = root / "stderr.log"
        write_json_atomic(
            state_path,
            {
                "schema_version": "1.0",
                "backend": self.name,
                "workload_id": workload.workload_id,
                "phase": "pending",
                "submitted_at": _now(),
                "command": list(workload.command),
            },
        )
        command = (
            sys.executable,
            "-m",
            "brunner.backends.worker",
            "--state",
            str(state_path),
            "--stdout",
            str(stdout_path),
            "--stderr",
            str(stderr_path),
            "--cwd",
            str(workload.trial / "workspace"),
            "--timeout",
            str(workload.timeout_seconds),
            "--environment-json",
            json.dumps(workload.environment),
            "--",
            *workload.command,
        )
        worker_log = root / "worker.log"
        try:
            # Never DEVNULL: a worker that dies before it can record its own
            # state (import failure, OOM) leaves this log as the only evidence.
            with worker_log.open("wb") as worker_output:
                worker = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=worker_output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as error:
            state = json.loads(state_path.read_text())
            state.update(
                {
                    "phase": "failed",
                    "reason": type(error).__name__,
                    "message": str(error),
                    "finished_at": _now(),
                }
            )
            write_json_atomic(state_path, state)
            raise BackendRequestError(
                f"could not start local workload: {error}"
            ) from error
        write_json_atomic(
            self._launcher_path(workload.trial),
            {
                "schema_version": "1.0",
                "launcher_pid": worker.pid,
                "started_at": _now(),
                "log": str(worker_log),
            },
        )
        threading.Thread(
            target=self._watch_launcher,
            args=(worker, workload.trial),
            daemon=True,
        ).start()
        self._handles[
            backend_registry_key(workload.workload_id, workload.trial)
        ] = handle
        return handle

    def _startup_failure(
        self,
        trial: Path,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Detect a worker that will never reach a terminal state.

        The worker records ``worker_pid`` only after the interpreter has booted
        and imported brunner. If it dies before that, the state file still says
        ``pending`` with no PID, and without this check the trial stays pending
        forever.
        """
        worker_pid = state.get("worker_pid")
        if isinstance(worker_pid, int):
            if _is_alive(worker_pid):
                return None
            return {
                "reason": "WorkerLost",
                "message": (
                    "local backend worker exited without recording "
                    "terminal state"
                ),
            }
        launcher = self._launcher(trial)
        exit_code = launcher.get("exit_code")
        if isinstance(exit_code, int):
            return {
                "reason": "WorkerStartFailed",
                "message": (
                    f"local backend worker exited with code {exit_code} "
                    "before it started the workload; see "
                    f"{self._root(trial) / 'worker.log'}"
                ),
                "exit_code": exit_code,
            }
        launcher_pid = launcher.get("launcher_pid")
        if isinstance(launcher_pid, int) and not _is_alive(launcher_pid):
            return {
                "reason": "WorkerStartFailed",
                "message": (
                    "local backend worker vanished before it started the "
                    f"workload; see {self._root(trial) / 'worker.log'}"
                ),
            }
        if launcher_pid is None:
            # The launcher record is written just after the worker starts, so
            # a missing one is only conclusive once that window has passed.
            if self._submitted_seconds_ago(state) > LAUNCH_GRACE_SECONDS:
                return {
                    "reason": "WorkerMissing",
                    "message": (
                        "local backend state exists but no worker was ever "
                        "launched for it"
                    ),
                }
        return None

    @staticmethod
    def _submitted_seconds_ago(state: dict[str, Any]) -> float:
        submitted_at = state.get("submitted_at")
        if not submitted_at:
            return float("inf")
        try:
            submitted = datetime.fromisoformat(str(submitted_at))
        except ValueError:
            return float("inf")
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=UTC)
        return (datetime.now(UTC) - submitted).total_seconds()

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        state_path = self._state_path(handle.trial)
        if not state_path.is_file():
            return BackendSnapshot(
                phase="unknown",
                reason="StateMissing",
                message=f"local state does not exist: {state_path}",
            )
        state = json.loads(state_path.read_text())
        phase = str(state.get("phase", "unknown"))
        if phase in {"pending", "running"}:
            failure = self._startup_failure(handle.trial, state)
            if failure is not None:
                phase = "failed"
                state.update(
                    {
                        "phase": phase,
                        **failure,
                        "finished_at": _now(),
                    }
                )
                write_json_atomic(state_path, state)
        return BackendSnapshot(
            phase=phase,
            reason=state.get("reason"),
            message=state.get("message"),
            exit_code=state.get("exit_code"),
            started_at=state.get("started_at"),
            finished_at=state.get("finished_at"),
            details={
                "worker_pid": state.get("worker_pid"),
                "workload_pid": state.get("workload_pid"),
            },
        )

    def logs(self, handle: BackendHandle) -> str:
        root = self._root(handle.trial)
        sections = []
        for label, path in (
            ("stdout", root / "stdout.log"),
            ("stderr", root / "stderr.log"),
        ):
            if path.is_file():
                sections.append(f"== {label} ==\n{path.read_text(errors='replace')}")
        return "\n".join(sections)

    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        return collect_local_artifacts(
            handle.trial,
            destination,
            policy,
            included_groups=included_groups,
        )

    def cleanup(self, handle: BackendHandle) -> None:
        state_path = self._state_path(handle.trial)
        if not state_path.is_file():
            return
        state = json.loads(state_path.read_text())
        if state.get("phase") in {"pending", "running"}:
            launcher = self._launcher(handle.trial)
            candidates = (
                state.get("workload_pid"),
                state.get("worker_pid"),
                launcher.get("launcher_pid"),
            )
            for pid in candidates:
                if not isinstance(pid, int) or not _is_alive(pid):
                    continue
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        state.update(
            {
                "phase": "cleaned",
                "finished_at": state.get("finished_at") or _now(),
            }
        )
        write_json_atomic(state_path, state)

    def capacity(self) -> BackendCapacity:
        snapshots = [
            self.inspect(handle)
            for handle in self._handles.values()
        ]
        running = sum(item.phase == "running" for item in snapshots)
        pending = sum(item.phase == "pending" for item in snapshots)
        available = max(0, self.max_parallel - running - pending)
        return BackendCapacity(
            limit=self.max_parallel,
            running=running,
            pending=pending,
            available=available,
        )
