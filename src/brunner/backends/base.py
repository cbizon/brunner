from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from brunner import BRUNNER_RUNTIME_PROTOCOL
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
RESERVED_WORKLOAD_LABELS = frozenset(
    {
        "app.kubernetes.io/name",
        "dev.brunner/role",
        "dev.brunner/restart-generation",
        "dev.brunner/workload",
    }
)


def validate_secret_environment(
    secret_environment: dict[str, tuple[str, str]],
    *,
    owner: str,
) -> None:
    if not isinstance(secret_environment, dict):
        raise ValueError(f"{owner} secret environment must be a mapping")
    for environment_name, reference in secret_environment.items():
        if not isinstance(environment_name, str) or not environment_name:
            raise ValueError(
                f"{owner} secret environment names must be non-empty strings"
            )
        if (
            not isinstance(reference, tuple)
            or len(reference) != 2
            or any(
                not isinstance(value, str) or not value
                for value in reference
            )
        ):
            raise ValueError(
                f"{owner} secret environment reference for "
                f"{environment_name!r} must be a "
                "(secret_name, secret_key) tuple"
            )


def native_resource_name(
    workload_id: str,
    resource_identity: str | Path,
    *,
    suffix: str = "",
    max_length: int = 63,
) -> str:
    if isinstance(resource_identity, Path):
        resource_identity = trial_resource_id(resource_identity)
    identity = f"{workload_id}\0{resource_identity}".encode()
    digest = hashlib.sha256(identity).hexdigest()[:10]
    normalized = re.sub(
        r"[^a-z0-9-]+",
        "-",
        workload_id.lower(),
    ).strip("-") or "trial"
    reserved = len("brunner--") + len(digest) + len(suffix)
    prefix = normalized[: max(1, max_length - reserved)].rstrip("-")
    return f"brunner-{prefix}-{digest}{suffix}"


def trial_resource_id(trial: Path) -> str:
    manifest_path = trial / "metadata/manifest.json"
    if manifest_path.is_file():
        try:
            metadata = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            metadata = None
        if isinstance(metadata, dict):
            resource_id = metadata.get("resource_id")
            if isinstance(resource_id, str) and resource_id.strip():
                return resource_id
            legacy_identity = {
                key: metadata.get(key)
                for key in (
                    "test_id",
                    "benchmark_id",
                    "benchmark_version",
                    "created_at",
                )
            }
            if all(value is not None for value in legacy_identity.values()):
                encoded = json.dumps(
                    legacy_identity,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                return "legacy-" + hashlib.sha256(encoded).hexdigest()[:24]
    return "unmanaged-" + hashlib.sha256(
        str(trial.resolve()).encode()
    ).hexdigest()[:24]


@dataclass(frozen=True)
class TrustedEvaluationSpec:
    benchmark_id: str
    benchmark_version: str
    contract_sha256: str
    image: str
    command: tuple[str, ...]
    results_path: str
    timeout_seconds: float
    runtime_protocol: str = BRUNNER_RUNTIME_PROTOCOL
    primary_report: str | None = None
    reference_manifest_path: str | None = None
    reference_manifest_sha256: str | None = None
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
        if self.runtime_protocol != BRUNNER_RUNTIME_PROTOCOL:
            raise ValueError(
                "evaluation runtime protocol is incompatible with this "
                f"Brunner release: {self.runtime_protocol!r} != "
                f"{BRUNNER_RUNTIME_PROTOCOL!r}"
            )
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
        if (
            self.reference_manifest_path is None
            and self.reference_manifest_sha256 is not None
        ):
            raise ValueError(
                "evaluation reference manifest digest requires a manifest path"
            )
        if (
            self.reference_manifest_path is not None
            and (
                not isinstance(self.reference_manifest_sha256, str)
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    self.reference_manifest_sha256,
                )
                is None
            )
        ):
            raise ValueError(
                "evaluation reference_manifest_sha256 must be a SHA-256 "
                "digest when a reference bundle is configured"
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
    secret_environment: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )
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
        reserved_labels = sorted(
            RESERVED_WORKLOAD_LABELS & set(self.labels)
        )
        if reserved_labels:
            raise ValueError(
                "workload labels use Brunner-reserved names: "
                + ", ".join(reserved_labels)
            )
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            for key, value in self.labels.items()
        ):
            raise ValueError(
                "workload labels must have non-empty string names and "
                "string values"
            )
        if self.evaluation is not None:
            self.evaluation.validate()
        validate_secret_environment(
            self.secret_environment,
            owner="workload",
        )
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

    @property
    def resource_id(self) -> str:
        return trial_resource_id(self.trial)

    @property
    def sha256(self) -> str:
        return workload_sha256(self)


def workload_sha256(workload: WorkloadSpec) -> str:
    evaluation = workload.evaluation
    evaluation_value = (
        None
        if evaluation is None
        else {
            name: (
                list(value)
                if isinstance(value, tuple)
                else value
            )
            for name, value in vars(evaluation).items()
        }
    )
    value = {
        "schema_version": "1.0",
        "runtime_protocol": BRUNNER_RUNTIME_PROTOCOL,
        "resource_id": workload.resource_id,
        "workload_id": workload.workload_id,
        "command": list(workload.command),
        "timeout_seconds": workload.timeout_seconds,
        "image": workload.image,
        "cpu": workload.cpu,
        "memory": workload.memory,
        "gpu": workload.gpu,
        "storage": workload.storage,
        "labels": dict(sorted(workload.labels.items())),
        "cpu_request": workload.cpu_request,
        "cpu_limit": workload.cpu_limit,
        "memory_request": workload.memory_request,
        "memory_limit": workload.memory_limit,
        "ephemeral_storage_request": workload.ephemeral_storage_request,
        "ephemeral_storage_limit": workload.ephemeral_storage_limit,
        "evaluation": evaluation_value,
    }
    if workload.secret_environment:
        value["secret_environment"] = {
            name: list(reference)
            for name, reference in sorted(
                workload.secret_environment.items()
            )
        }
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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

    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity: ...
