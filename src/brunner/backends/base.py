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


@dataclass(frozen=True)
class TrustedEvaluationSpec:
    benchmark_id: str
    benchmark_version: str
    contract_sha256: str
    image: str
    command: tuple[str, ...]
    results_path: str
    timeout_seconds: float
    primary_report: str | None = None
    reference_manifest_path: str | None = None
    reference_validate_command: tuple[str, ...] = ()
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    ephemeral_storage_request: str | None = None
    ephemeral_storage_limit: str | None = None

    def validate(self) -> None:
        if (
            not isinstance(self.benchmark_id, str)
            or not self.benchmark_id.strip()
        ):
            raise ValueError("evaluation benchmark_id cannot be empty")
        if (
            not isinstance(self.benchmark_version, str)
            or not self.benchmark_version.strip()
        ):
            raise ValueError("evaluation benchmark_version cannot be empty")
        if (
            not isinstance(self.contract_sha256, str)
            or not self.contract_sha256.strip()
        ):
            raise ValueError("evaluation contract_sha256 cannot be empty")
        if not isinstance(self.image, str) or not self.image.strip():
            raise ValueError("evaluation image cannot be empty")
        if not self.command or any(
            not isinstance(argument, str) or not argument.strip()
            for argument in self.command
        ):
            raise ValueError("evaluation command cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("evaluation timeout must be positive")
        for name, value in (
            ("results_path", self.results_path),
            ("primary_report", self.primary_report),
            ("reference_manifest_path", self.reference_manifest_path),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"evaluation {name} must be a safe relative path"
                )
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"evaluation {name} must be a safe relative path"
                )
        if any(
            not isinstance(argument, str) or not argument.strip()
            for argument in self.reference_validate_command
        ):
            raise ValueError(
                "evaluation reference_validate_command arguments must be "
                "non-empty strings"
            )
        for name, value in (
            ("cpu_request", self.cpu_request),
            ("cpu_limit", self.cpu_limit),
            ("memory_request", self.memory_request),
            ("memory_limit", self.memory_limit),
            ("ephemeral_storage_request", self.ephemeral_storage_request),
            ("ephemeral_storage_limit", self.ephemeral_storage_limit),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"evaluation {name} cannot be empty")


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
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    ephemeral_storage_request: str | None = None
    ephemeral_storage_limit: str | None = None
    evaluation: TrustedEvaluationSpec | None = None

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
        if self.evaluation is not None:
            self.evaluation.validate()
        for name, value in (
            ("cpu", self.cpu),
            ("memory", self.memory),
            ("storage", self.storage),
            ("cpu_request", self.cpu_request),
            ("cpu_limit", self.cpu_limit),
            ("memory_request", self.memory_request),
            ("memory_limit", self.memory_limit),
            ("ephemeral_storage_request", self.ephemeral_storage_request),
            ("ephemeral_storage_limit", self.ephemeral_storage_limit),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"workload {name} cannot be empty")


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
    trusted_evaluation: str

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
