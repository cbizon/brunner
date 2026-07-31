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
        try:
            worker = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
        threading.Thread(target=worker.wait, daemon=True).start()
        self._handles[
            backend_registry_key(workload.workload_id, workload.trial)
        ] = handle
        return handle

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
        worker_pid = state.get("worker_pid") or state.get("launcher_pid")
        if phase in {"pending", "running"} and isinstance(worker_pid, int):
            if not _is_alive(worker_pid):
                phase = "failed"
                state.update(
                    {
                        "phase": phase,
                        "reason": "WorkerLost",
                        "message": "local backend worker exited without "
                        "recording terminal state",
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
            for key in ("workload_pid", "worker_pid", "launcher_pid"):
                pid = state.get(key)
                if not isinstance(pid, int) or not _is_alive(pid):
                    continue
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
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
