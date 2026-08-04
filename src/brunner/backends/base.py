from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from brunner.definition import ArtifactPolicy


BACKEND_PHASES = frozenset(
    {
        "pending",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "cleaned",
    }
)
CONTAINER_ISOLATION = "container"


def native_resource_name(
    workload_id: str,
    trial: Path,
    *,
    suffix: str = "",
    max_length: int = 63,
) -> str:
    identity = (
        f"{workload_id}\0{trial.resolve()}".encode()
    )
    digest = hashlib.sha256(identity).hexdigest()[:10]
    normalized = re.sub(
        r"[^a-z0-9-]+",
        "-",
        workload_id.lower(),
    ).strip("-") or "trial"
    reserved = len("brunner--") + len(digest) + len(suffix)
    prefix = normalized[: max(1, max_length - reserved)].rstrip("-")
    return f"brunner-{prefix}-{digest}{suffix}"


def backend_registry_key(
    workload_id: str,
    trial: Path,
) -> tuple[str, str]:
    return workload_id, str(trial.resolve())


@dataclass(frozen=True)
class WorkloadSpec:
    workload_id: str
    trial: Path
    command: tuple[str, ...]
    timeout_seconds: float
    image: str | None = None
    cpu: str | None = None
    memory: str | None = None
    gpu: int = 0
    storage: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.workload_id.strip():
            raise ValueError("workload_id cannot be empty")
        if not self.trial.is_dir():
            raise ValueError(f"trial does not exist: {self.trial}")
        if not self.command:
            raise ValueError("workload command cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("workload timeout must be positive")
        if self.gpu < 0:
            raise ValueError("workload gpu count cannot be negative")


@dataclass(frozen=True)
class BackendHandle:
    backend: str
    workload_id: str
    native_id: str
    trial: Path
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "workload_id": self.workload_id,
            "native_id": self.native_id,
            "trial": str(self.trial),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class BackendSnapshot:
    phase: str
    reason: str | None = None
    message: str | None = None
    exit_code: int | None = None
    node: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.phase in {"succeeded", "failed", "cleaned"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "reason": self.reason,
            "message": self.message,
            "exit_code": self.exit_code,
            "node": self.node,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "warnings": list(self.warnings),
            "details": self.details,
        }


@dataclass(frozen=True)
class BackendCapacity:
    limit: int | None
    running: int
    pending: int
    available: int | None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "running": self.running,
            "pending": self.pending,
            "available": self.available,
            "details": self.details,
        }


class ExecutionBackend(Protocol):
    name: str
    agent_isolation: str

    def submit(self, workload: WorkloadSpec) -> BackendHandle: ...

    def restart(
        self,
        handle: BackendHandle,
        workload: WorkloadSpec,
        generation: int,
    ) -> BackendHandle: ...

    def inspect(self, handle: BackendHandle) -> BackendSnapshot: ...

    def logs(self, handle: BackendHandle) -> str: ...

    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]: ...

    def cleanup(self, handle: BackendHandle) -> None: ...

    def capacity(self) -> BackendCapacity: ...
