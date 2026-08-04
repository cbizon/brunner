from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from brunner.artifacts import collect_local_artifacts
from brunner.backends.base import (
    BackendCapacity,
    BackendHandle,
    BackendSnapshot,
    WorkloadSpec,
    backend_registry_key,
    native_resource_name,
)
from brunner.definition import ArtifactPolicy
from brunner.errors import (
    BackendError,
    BackendConnectivityError,
    BackendRequestError,
)
from brunner.io import write_json_atomic


CONNECTIVITY_FRAGMENTS = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "connection refused",
)

class ContainerBackend:
    name = "container"
    agent_isolation = "container"

    def __init__(
        self,
        *,
        runtime: str = "docker",
        max_parallel: int | None = None,
        command_timeout_seconds: float = 120,
    ) -> None:
        self.runtime = runtime
        self.max_parallel = max_parallel
        self.command_timeout_seconds = command_timeout_seconds
        self._handles: dict[tuple[str, str], BackendHandle] = {}

    def _runtime_error(
        self,
        arguments: tuple[str, ...],
        message: str,
    ) -> BackendError:
        lowered = message.lower()
        error_type = (
            BackendConnectivityError
            if any(item in lowered for item in CONNECTIVITY_FRAGMENTS)
            else BackendRequestError
        )
        return error_type(
            f"{self.runtime} {' '.join(arguments)} failed: {message}"
        )

    def _run(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                (self.runtime, *arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise BackendConnectivityError(
                f"container runtime command timed out: {arguments}"
            ) from error
        except OSError as error:
            raise BackendConnectivityError(
                f"cannot execute container runtime {self.runtime!r}: {error}"
            ) from error
        if check and result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise self._runtime_error(tuple(arguments), message)
        return result

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        workload.validate()
        if not workload.image:
            raise BackendRequestError(
                "container workloads require an image"
            )
        name = native_resource_name(
            workload.workload_id,
            workload.trial,
        )
        state_path = workload.trial / "backend/container.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text())
            handle = BackendHandle(
                backend=self.name,
                workload_id=workload.workload_id,
                native_id=str(state["native_id"]),
                trial=workload.trial.resolve(),
                metadata={"name": str(state["name"])},
            )
            self._handles[
                backend_registry_key(workload.workload_id, workload.trial)
            ] = handle
            return handle
        existing = self._run(
            "inspect",
            "--format",
            "{{json .}}",
            name,
            check=False,
        )
        if existing.returncode == 0:
            try:
                inspected = json.loads(existing.stdout)
                native_id = str(inspected["Id"])
                labels = inspected["Config"]["Labels"]
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise BackendRequestError(
                    f"cannot adopt existing container {name}: "
                    "runtime returned invalid metadata"
                ) from error
            if labels.get("dev.brunner.workload") != workload.workload_id:
                raise BackendRequestError(
                    f"existing container {name} is not owned by this "
                    "Brunner workload"
                )
            handle = BackendHandle(
                backend=self.name,
                workload_id=workload.workload_id,
                native_id=native_id,
                trial=workload.trial.resolve(),
                metadata={"name": name},
            )
            write_json_atomic(
                state_path,
                {
                    "schema_version": "1.0",
                    **handle.to_dict(),
                    "name": name,
                },
            )
            self._handles[
                backend_registry_key(workload.workload_id, workload.trial)
            ] = handle
            return handle
        existing_message = (existing.stderr or existing.stdout).strip()
        if "no such" not in existing_message.lower():
            raise self._runtime_error(
                ("inspect", name),
                existing_message,
            )
        arguments = [
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"dev.brunner.workload={workload.workload_id}",
            "--mount",
            (
                "type=bind,src="
                f"{workload.trial.resolve()},dst=/brunner/trial"
            ),
            "--workdir",
            "/brunner/trial/workspace",
        ]
        for key, value in sorted(workload.environment.items()):
            arguments.extend(("--env", f"{key}={value}"))
        if workload.cpu:
            arguments.extend(("--cpus", workload.cpu))
        if workload.memory:
            arguments.extend(("--memory", workload.memory))
        if workload.gpu:
            arguments.extend(("--gpus", str(workload.gpu)))
        arguments.extend(
            (
                workload.image,
                "timeout",
                str(workload.timeout_seconds),
                *workload.command,
            )
        )
        result = self._run(*arguments)
        native_id = result.stdout.strip()
        handle = BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=native_id,
            trial=workload.trial.resolve(),
            metadata={"name": name},
        )
        write_json_atomic(
            state_path,
            {
                "schema_version": "1.0",
                **handle.to_dict(),
                "name": name,
            },
        )
        self._handles[
            backend_registry_key(workload.workload_id, workload.trial)
        ] = handle
        return handle

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        result = self._run(
            "inspect",
            "--format",
            "{{json .State}}",
            handle.native_id,
            check=False,
        )
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            if "no such object" in message.lower():
                return BackendSnapshot(
                    phase="unknown",
                    reason="ContainerMissing",
                    message=message,
                )
            raise self._runtime_error(
                ("inspect", handle.native_id),
                message,
            )
        state = json.loads(result.stdout)
        if state.get("Running"):
            phase = "running"
        elif state.get("Status") == "created":
            phase = "pending"
        elif int(state.get("ExitCode", 1)) == 0:
            phase = "succeeded"
        else:
            phase = "failed"
        return BackendSnapshot(
            phase=phase,
            reason=state.get("Error") or None,
            exit_code=state.get("ExitCode"),
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt"),
            details={"container_status": state.get("Status")},
        )

    def logs(self, handle: BackendHandle) -> str:
        result = self._run(
            "logs",
            handle.native_id,
            check=False,
        )
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            if "no such" not in message.lower():
                raise self._runtime_error(
                    ("logs", handle.native_id),
                    message,
                )
        return result.stdout + result.stderr

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
        result = self._run(
            "rm",
            "--force",
            handle.native_id,
            check=False,
        )
        message = (result.stderr or result.stdout).lower()
        if result.returncode and "no such" not in message:
            raise self._runtime_error(
                ("rm", "--force", handle.native_id),
                message.strip(),
            )

    def capacity(self) -> BackendCapacity:
        result = self._run(
            "ps",
            "--filter",
            "label=dev.brunner.workload",
            "--format",
            "{{.ID}}",
        )
        running = len([line for line in result.stdout.splitlines() if line])
        available = (
            None
            if self.max_parallel is None
            else max(0, self.max_parallel - running)
        )
        return BackendCapacity(
            limit=self.max_parallel,
            running=running,
            pending=0,
            available=available,
            details={"runtime": self.runtime},
        )
