from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brunner import BRUNNER_RUNTIME_PROTOCOL
from brunner.artifacts import (
    CHUNK_BYTES,
    artifact_metadata,
    enforce_inventory_size,
    finalize_artifact_collection,
    prepare_partial_artifacts,
)
from brunner.backends.base import (
    BackendCapacity,
    BackendHandle,
    BackendSnapshot,
    WorkloadSpec,
    native_resource_name,
    trial_resource_id,
    validate_secret_environment,
    workload_sha256,
)
from brunner.backends.squid import (
    MANAGED_PROXY_LABELS,
    MANAGED_PROXY_NAME,
    MANAGED_PROXY_PORT,
    managed_proxy_sha256,
    proxy_url_from_service,
    render_managed_proxy_resources,
)
from brunner.definition import ArtifactPolicy
from brunner.errors import (
    ArtifactTransferError,
    BackendConfigurationError,
    BackendError,
    BackendConnectivityError,
    BackendRequestError,
    IntegrityError,
)
from brunner.io import write_json_atomic
from brunner.staging import load_stage_report


CONNECTIVITY_FRAGMENTS = (
    "bad gateway",
    "connection closed",
    "unable to connect to the server",
    "connection refused",
    "connection reset by peer",
    "context deadline exceeded",
    "dial tcp",
    "gateway timeout",
    "http/2: client connection lost",
    "http2: client connection lost",
    "i/o timeout",
    "internal server error",
    "no route to host",
    "no such host",
    "network is unreachable",
    "proxyconnect tcp",
    "server is currently unable to handle the request",
    "stream error",
    "temporary failure in name resolution",
    "tls handshake timeout",
    "too many requests",
    "unexpected eof",
    "service unavailable",
)
REACHABLE_REQUEST_FRAGMENTS = (
    "already exists",
    "cannot be changed",
    "exceeded quota",
    "forbidden",
    "immutable",
    "invalid",
    "not found",
    "the server has asked for the client to provide credentials",
    "unauthorized",
)
STAGED_ANNOTATION = "dev.brunner/staged"
CHALLENGE_SHA256_ANNOTATION = "dev.brunner/challenge-sha256"
WORKLOAD_SHA256_ANNOTATION = "dev.brunner/workload-sha256"
RUNTIME_PROTOCOL_ANNOTATION = "dev.brunner/runtime-protocol"
EGRESS_PROXY_SHA256_ANNOTATION = "dev.brunner/egress-proxy-sha256"
REFERENCE_MANIFEST_SHA256_ANNOTATION = (
    "dev.brunner/reference-manifest-sha256"
)
PIPELINE_ROLE = "pipeline"
HELPER_ROLES = ("trial-stager", "artifact-reader")
PROXY_ENVIRONMENT = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
NON_RETRYABLE_JOB_FAILURES = frozenset({"DeadlineExceeded"})
NON_RETRYABLE_CONTAINER_FAILURES = frozenset(
    {
        "CreateContainerConfigError",
        "CreateContainerError",
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "RunContainerError",
        "StartError",
    }
)
RETRYABLE_CONTAINER_FAILURES = frozenset(
    {
        "ContainerStatusUnknown",
        "Evicted",
        "NodeLost",
        "OOMKilled",
        "Shutdown",
    }
)
TERMINATION_LOG_ENV = "BRUNNER_TERMINATION_LOG"


class ReaderMountError(BackendRequestError):
    def __init__(self, message: str, *, node: str | None) -> None:
        super().__init__(message)
        self.node = node


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class KubernetesProfile:
    namespace: str = "default"
    agent_image: str | None = None
    artifact_reader_image: str | None = None
    reference_claim_name: str | None = None
    storage_size: str = "20Gi"
    storage_class_name: str | None = None
    service_account_name: str | None = None
    image_pull_secrets: tuple[str, ...] = ()
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: tuple[dict[str, Any], ...] = ()
    secret_environment: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )
    nonsecret_environment: dict[str, str] = field(default_factory=dict)
    proxy_image: str | None = None
    proxy_cpu_request: str = "100m"
    proxy_cpu_limit: str = "1"
    proxy_memory_request: str = "256Mi"
    proxy_memory_limit: str = "1Gi"
    dns_namespace: str = "kube-system"
    dns_pod_selector: dict[str, str] = field(
        default_factory=lambda: {"k8s-app": "kube-dns"}
    )
    unsafe_disable_network_policy_for_tests: bool = False
    job_backoff_limit: int = 6
    require_image_digests: bool = True
    preflight_enabled: bool = True
    max_parallel: int | None = None
    staging_timeout_seconds: float = 10 * 60
    reader_timeout_seconds: float = 10 * 60
    reader_attempts: int = 3
    retain_failed_storage: bool = True
    command_timeout_seconds: float = 120
    artifact_chunk_bytes: int = CHUNK_BYTES
    artifact_chunk_attempts: int = 5
    artifact_chunk_retry_seconds: float = 1

    def __post_init__(self) -> None:
        validate_secret_environment(
            self.secret_environment,
            owner="Kubernetes profile",
        )
        if self.artifact_chunk_bytes < 1:
            raise ValueError(
                "Kubernetes artifact_chunk_bytes must be positive"
            )
        if (
            self.reference_claim_name is not None
            and not self.reference_claim_name.strip()
        ):
            raise ValueError(
                "Kubernetes reference_claim_name cannot be empty"
            )
        if self.proxy_image is not None and not self.proxy_image.strip():
            raise ValueError("Kubernetes proxy_image cannot be empty")
        if not self.dns_namespace.strip():
            raise ValueError("Kubernetes dns_namespace cannot be empty")
        if not self.dns_pod_selector:
            raise ValueError(
                "Kubernetes dns_pod_selector cannot be empty"
            )
        for name, value in (
            ("proxy_cpu_request", self.proxy_cpu_request),
            ("proxy_cpu_limit", self.proxy_cpu_limit),
            ("proxy_memory_request", self.proxy_memory_request),
            ("proxy_memory_limit", self.proxy_memory_limit),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Kubernetes {name} cannot be empty")
        if PROXY_ENVIRONMENT & (
            set(self.nonsecret_environment)
            | set(self.secret_environment)
        ):
            names = sorted(
                PROXY_ENVIRONMENT
                & (
                    set(self.nonsecret_environment)
                    | set(self.secret_environment)
                )
            )
            raise ValueError(
                "Kubernetes proxy environment is managed by Brunner: "
                + ", ".join(names)
            )
        if self.job_backoff_limit < 0:
            raise ValueError(
                "Kubernetes job_backoff_limit cannot be negative"
            )
        if self.artifact_chunk_attempts < 1:
            raise ValueError(
                "Kubernetes artifact_chunk_attempts must be positive"
            )
        if self.artifact_chunk_retry_seconds < 0:
            raise ValueError(
                "Kubernetes artifact_chunk_retry_seconds must not be negative"
            )


def _effective_secret_environment(
    workload: WorkloadSpec,
    profile: KubernetesProfile,
) -> dict[str, tuple[str, str]]:
    effective = dict(profile.secret_environment)
    conflicts = {
        name: (effective[name], reference)
        for name, reference in workload.secret_environment.items()
        if name in effective and effective[name] != reference
    }
    if conflicts:
        raise BackendConfigurationError(
            "workload secret environment conflicts with shared Kubernetes "
            f"profile credentials: {conflicts}"
        )
    effective.update(workload.secret_environment)
    nonsecret_conflicts = sorted(
        set(effective) & set(profile.nonsecret_environment)
    )
    if nonsecret_conflicts:
        raise BackendConfigurationError(
            "Kubernetes environment names cannot be both secret and "
            "non-secret: " + ", ".join(nonsecret_conflicts)
        )
    managed = sorted(PROXY_ENVIRONMENT & set(effective))
    if managed:
        raise BackendConfigurationError(
            "Kubernetes proxy environment is managed by Brunner: "
            + ", ".join(managed)
        )
    if TERMINATION_LOG_ENV in effective:
        raise BackendConfigurationError(
            f"{TERMINATION_LOG_ENV} is reserved by Brunner"
        )
    return effective


def _image_is_immutable(image: str) -> bool:
    return re.search(r"@sha256:[0-9a-fA-F]{64}$", image) is not None


QUANTITY_FACTORS = {
    "n": Decimal("0.000000001"),
    "u": Decimal("0.000001"),
    "m": Decimal("0.001"),
    "k": Decimal("1000"),
    "K": Decimal("1000"),
    "M": Decimal("1000000"),
    "G": Decimal("1000000000"),
    "T": Decimal("1000000000000"),
    "P": Decimal("1000000000000000"),
    "E": Decimal("1000000000000000000"),
    "Ki": Decimal(1024),
    "Mi": Decimal(1024**2),
    "Gi": Decimal(1024**3),
    "Ti": Decimal(1024**4),
    "Pi": Decimal(1024**5),
    "Ei": Decimal(1024**6),
}


def _quantity(value: str | int | float | None) -> Decimal:
    if value is None:
        return Decimal(0)
    text = str(value).strip()
    match = re.fullmatch(
        r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))([A-Za-z]+)?",
        text,
    )
    if match is None:
        raise BackendRequestError(
            f"unsupported Kubernetes resource quantity: {value!r}"
        )
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as error:
        raise BackendRequestError(
            f"invalid Kubernetes resource quantity: {value!r}"
        ) from error
    suffix = match.group(2) or ""
    if suffix and suffix not in QUANTITY_FACTORS:
        raise BackendRequestError(
            f"unsupported Kubernetes resource quantity suffix: {value!r}"
        )
    return number * QUANTITY_FACTORS.get(suffix, Decimal(1))


def _resource_requirements(
    job: dict[str, Any],
    pvc: dict[str, Any],
) -> dict[str, Decimal]:
    pod_spec = job["spec"]["template"]["spec"]
    regular = pod_spec.get("containers", ())
    init = pod_spec.get("initContainers", ())
    resource_names = {
        name
        for container in (*regular, *init)
        for kind in ("requests", "limits")
        for name in container.get("resources", {}).get(kind, {})
    }
    requirements: dict[str, Decimal] = {
        "pods": Decimal(1),
        "count/jobs.batch": Decimal(1),
        "persistentvolumeclaims": Decimal(1),
        "requests.storage": _quantity(
            pvc["spec"]["resources"]["requests"]["storage"]
        ),
    }
    for kind in ("requests", "limits"):
        for resource in resource_names:
            regular_total = sum(
                (
                    _quantity(
                        container.get("resources", {})
                        .get(kind, {})
                        .get(resource)
                    )
                    for container in regular
                ),
                Decimal(0),
            )
            init_max = max(
                (
                    _quantity(
                        container.get("resources", {})
                        .get(kind, {})
                        .get(resource)
                    )
                    for container in init
                ),
                default=Decimal(0),
            )
            effective = max(regular_total, init_max)
            if effective:
                requirements[f"{kind}.{resource}"] = effective
    return requirements


def _network_policy_name(workload: WorkloadSpec, suffix: str) -> str:
    return native_resource_name(
        workload.workload_id,
        workload.resource_id,
        suffix=suffix,
    )


def _selector_matches_labels(
    selector: dict[str, Any],
    labels: dict[str, str],
) -> bool:
    match_labels = selector.get("matchLabels", {})
    if not isinstance(match_labels, dict):
        raise ValueError("matchLabels must be an object")
    if any(
        labels.get(str(key)) != str(value)
        for key, value in match_labels.items()
    ):
        return False
    expressions = selector.get("matchExpressions", [])
    if not isinstance(expressions, list):
        raise ValueError("matchExpressions must be an array")
    for expression in expressions:
        if not isinstance(expression, dict):
            raise ValueError("matchExpressions entries must be objects")
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values", [])
        if not isinstance(key, str) or not isinstance(operator, str):
            raise ValueError("label selector expression is incomplete")
        if not isinstance(values, list):
            raise ValueError("label selector values must be an array")
        normalized = {str(value) for value in values}
        present = key in labels
        if operator == "In" and (
            not present or labels[key] not in normalized
        ):
            return False
        if (
            operator == "NotIn"
            and present
            and labels[key] in normalized
        ):
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
        if operator not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            raise ValueError(
                f"unsupported label selector operator {operator!r}"
            )
    return True


def render_network_policies(
    workload: WorkloadSpec,
    profile: KubernetesProfile,
    labels: dict[str, str],
) -> tuple[dict[str, Any], ...]:
    if profile.unsafe_disable_network_policy_for_tests:
        return ()
    workload_name = str(labels["dev.brunner/workload"])
    pipeline_egress: list[dict[str, Any]] = [
        {
            "to": [
                {
                    "podSelector": {
                        "matchLabels": dict(MANAGED_PROXY_LABELS)
                    },
                }
            ],
            "ports": [
                {"protocol": "TCP", "port": MANAGED_PROXY_PORT},
            ],
        }
    ]
    common = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
    }
    return (
        {
            **common,
            "metadata": {
                "name": _network_policy_name(workload, "-network"),
                "namespace": profile.namespace,
                "labels": labels,
            },
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        "dev.brunner/workload": workload_name,
                        "dev.brunner/role": PIPELINE_ROLE,
                    }
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": pipeline_egress,
            },
        },
        {
            **common,
            "metadata": {
                "name": _network_policy_name(workload, "-helpers"),
                "namespace": profile.namespace,
                "labels": labels,
            },
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        "dev.brunner/workload": workload_name,
                    },
                    "matchExpressions": [
                        {
                            "key": "dev.brunner/role",
                            "operator": "In",
                            "values": list(HELPER_ROLES),
                        }
                    ],
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        },
    )
def render_pvc(
    name: str,
    profile: KubernetesProfile,
    labels: dict[str, str],
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {
            "requests": {"storage": profile.storage_size},
        },
    }
    if profile.storage_class_name is not None:
        spec["storageClassName"] = profile.storage_class_name
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": profile.namespace,
            "labels": labels,
        },
        "spec": spec,
    }


def _pod_spec_common(
    profile: KubernetesProfile,
    *,
    claim_name: str,
    container: dict[str, Any],
    claim_read_only: bool = False,
    excluded_nodes: tuple[str, ...] = (),
) -> dict[str, Any]:
    claim: dict[str, Any] = {"claimName": claim_name}
    if claim_read_only:
        claim["readOnly"] = True
    container["securityContext"] = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    container.setdefault("volumeMounts", []).append(
        {"name": "tmp", "mountPath": "/tmp"}
    )
    spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 30,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": [
            {
                "name": "trial",
                "persistentVolumeClaim": claim,
            },
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    if profile.service_account_name:
        spec["serviceAccountName"] = profile.service_account_name
    if profile.image_pull_secrets:
        spec["imagePullSecrets"] = [
            {"name": name} for name in profile.image_pull_secrets
        ]
    if profile.node_selector:
        spec["nodeSelector"] = dict(profile.node_selector)
    if profile.tolerations:
        spec["tolerations"] = list(profile.tolerations)
    if excluded_nodes:
        spec["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "NotIn",
                                    "values": list(excluded_nodes),
                                }
                            ]
                        }
                    ]
                }
            }
        }
    return spec


def render_helper_pod(
    name: str,
    claim_name: str,
    image: str,
    profile: KubernetesProfile,
    labels: dict[str, str],
    *,
    trial_read_only: bool = False,
    excluded_nodes: tuple[str, ...] = (),
) -> dict[str, Any]:
    trial_mount: dict[str, Any] = {
        "name": "trial",
        "mountPath": "/brunner/trial",
    }
    if trial_read_only:
        trial_mount["readOnly"] = True
    container = {
        "name": "helper",
        "image": image,
        "command": ["sh", "-c", "trap : TERM INT; sleep 86400 & wait"],
        # Never inherit an image WORKDIR hidden by the trial PVC mount.
        "workingDir": "/tmp",
        "volumeMounts": [trial_mount],
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": profile.namespace,
            "labels": labels,
        },
        "spec": _pod_spec_common(
            profile,
            claim_name=claim_name,
            container=container,
            claim_read_only=trial_read_only,
            excluded_nodes=excluded_nodes,
        ),
    }


def render_job(
    name: str,
    claim_name: str,
    workload: WorkloadSpec,
    profile: KubernetesProfile,
    labels: dict[str, str],
    *,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    image = workload.image or profile.agent_image
    if not image:
        raise BackendRequestError(
            "Kubernetes workloads require an agent image"
        )
    secret_environment = _effective_secret_environment(workload, profile)
    if TERMINATION_LOG_ENV in profile.nonsecret_environment:
        raise BackendRequestError(
            f"{TERMINATION_LOG_ENV} is reserved by Brunner"
        )
    environment = [
        {"name": key, "value": value}
        for key, value in sorted(profile.nonsecret_environment.items())
    ]
    if (
        not profile.unsafe_disable_network_policy_for_tests
        and proxy_url is None
    ):
        raise BackendRequestError(
            "Kubernetes workloads require the managed proxy ClusterIP"
        )
    if proxy_url is not None:
        no_proxy = "localhost,127.0.0.1,::1"
        environment.extend(
            {
                "name": name,
                "value": value,
            }
            for name, value in (
                ("HTTP_PROXY", proxy_url),
                ("HTTPS_PROXY", proxy_url),
                ("NO_PROXY", no_proxy),
                ("http_proxy", proxy_url),
                ("https_proxy", proxy_url),
                ("no_proxy", no_proxy),
            )
        )
    environment.extend(
        {
            "name": name,
            "valueFrom": {
                "secretKeyRef": {
                    "name": reference[0],
                    "key": reference[1],
                }
            },
        }
        for name, reference in sorted(secret_environment.items())
    )
    environment.append(
        {
            "name": TERMINATION_LOG_ENV,
            "value": "/dev/termination-log",
        }
    )
    resources: dict[str, dict[str, str]] = {}
    requests = {}
    limits = {}
    cpu_request = workload.cpu_request or workload.cpu
    memory_request = workload.memory_request or workload.memory
    ephemeral_storage_request = (
        workload.ephemeral_storage_request or workload.storage
    )
    if cpu_request:
        requests["cpu"] = cpu_request
    if memory_request:
        requests["memory"] = memory_request
    if ephemeral_storage_request:
        requests["ephemeral-storage"] = ephemeral_storage_request
    cpu_limit = workload.cpu_limit or workload.cpu
    memory_limit = workload.memory_limit or workload.memory
    ephemeral_storage_limit = (
        workload.ephemeral_storage_limit or workload.storage
    )
    if cpu_limit:
        limits["cpu"] = cpu_limit
    if memory_limit:
        limits["memory"] = memory_limit
    if ephemeral_storage_limit:
        limits["ephemeral-storage"] = ephemeral_storage_limit
    if workload.gpu:
        requests["nvidia.com/gpu"] = str(workload.gpu)
        limits["nvidia.com/gpu"] = str(workload.gpu)
    if requests:
        resources["requests"] = requests
    if limits:
        resources["limits"] = limits
    container: dict[str, Any] = {
        "name": "agent",
        "image": image,
        "command": list(workload.command),
        "workingDir": "/brunner/trial/workspace",
        "env": environment,
        "volumeMounts": [
            {"name": "trial", "mountPath": "/brunner/trial"}
        ],
    }
    if resources:
        container["resources"] = resources
    pod_spec = _pod_spec_common(
        profile,
        claim_name=claim_name,
        container=container,
    )
    active_deadline_seconds = workload.timeout_seconds
    if workload.evaluation is not None:
        evaluation = workload.evaluation
        evaluation_spec = {
            "schema_version": "2.0",
            "runtime_protocol": evaluation.runtime_protocol,
            "benchmark_id": evaluation.benchmark_id,
            "benchmark_version": evaluation.benchmark_version,
            "contract_sha256": evaluation.contract_sha256,
            "command": list(evaluation.command),
            "results_path": evaluation.results_path,
            "primary_report": evaluation.primary_report,
            "timeout_seconds": evaluation.timeout_seconds,
            "reference_manifest_path": (
                evaluation.reference_manifest_path
            ),
            "reference_manifest_sha256": (
                evaluation.reference_manifest_sha256
            ),
            "reference_validate_command": list(
                evaluation.reference_validate_command
            ),
        }
        evaluator_requests = {}
        evaluator_limits = {}
        if evaluation.cpu_request:
            evaluator_requests["cpu"] = evaluation.cpu_request
        if evaluation.memory_request:
            evaluator_requests["memory"] = evaluation.memory_request
        if evaluation.ephemeral_storage_request:
            evaluator_requests["ephemeral-storage"] = (
                evaluation.ephemeral_storage_request
            )
        if evaluation.cpu_limit:
            evaluator_limits["cpu"] = evaluation.cpu_limit
        if evaluation.memory_limit:
            evaluator_limits["memory"] = evaluation.memory_limit
        if evaluation.ephemeral_storage_limit:
            evaluator_limits["ephemeral-storage"] = (
                evaluation.ephemeral_storage_limit
            )
        evaluator_resources = {}
        if evaluator_requests:
            evaluator_resources["requests"] = evaluator_requests
        if evaluator_limits:
            evaluator_resources["limits"] = evaluator_limits
        evaluator = {
            "name": "evaluator",
            "image": evaluation.image,
            "command": [
                "python",
                "-m",
                "brunner.evaluation_cli",
                "/brunner/trial",
            ],
            "workingDir": "/tmp",
            "env": [
                {
                    "name": "BRUNNER_EVALUATION_SPEC",
                    "value": json.dumps(
                        evaluation_spec,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
                {
                    "name": TERMINATION_LOG_ENV,
                    "value": "/dev/termination-log",
                },
                {
                    "name": "PYTHONSAFEPATH",
                    "value": "1",
                },
                {
                    "name": "PYTHONNOUSERSITE",
                    "value": "1",
                },
            ],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "readOnlyRootFilesystem": True,
            },
            "volumeMounts": [
                {"name": "trial", "mountPath": "/brunner/trial"},
                {"name": "evaluator-tmp", "mountPath": "/tmp"},
            ],
        }
        if evaluator_resources:
            evaluator["resources"] = evaluator_resources
        if evaluation.reference_manifest_path is not None:
            if not profile.reference_claim_name:
                raise BackendRequestError(
                    "Kubernetes evaluation requires reference_claim_name "
                    "when the benchmark defines a reference bundle"
                )
            evaluator["volumeMounts"].append(
                {
                    "name": "reference",
                    "mountPath": "/brunner/reference",
                    "readOnly": True,
                }
            )
            pod_spec["volumes"].append(
                {
                    "name": "reference",
                    "persistentVolumeClaim": {
                        "claimName": profile.reference_claim_name,
                        "readOnly": True,
                    },
                }
            )
        for mount in container["volumeMounts"]:
            if mount["name"] == "tmp":
                mount["name"] = "agent-tmp"
        for volume in pod_spec["volumes"]:
            if volume["name"] == "tmp":
                volume["name"] = "agent-tmp"
        pod_spec["volumes"].append(
            {"name": "evaluator-tmp", "emptyDir": {}}
        )
        pod_spec["initContainers"] = [container]
        pod_spec["containers"] = [evaluator]
        active_deadline_seconds += evaluation.timeout_seconds
    pod_spec["activeDeadlineSeconds"] = math.ceil(active_deadline_seconds)
    pod_labels = {
        **labels,
        "dev.brunner/role": PIPELINE_ROLE,
    }
    annotations = {
        WORKLOAD_SHA256_ANNOTATION: workload_sha256(workload),
        RUNTIME_PROTOCOL_ANNOTATION: BRUNNER_RUNTIME_PROTOCOL,
    }
    if not profile.unsafe_disable_network_policy_for_tests:
        if not profile.proxy_image:
            raise BackendRequestError(
                "Kubernetes workloads require proxy_image for Brunner's "
                "managed Squid egress proxy"
            )
        annotations[EGRESS_PROXY_SHA256_ANNOTATION] = (
            managed_proxy_sha256(profile.proxy_image)
        )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": profile.namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "backoffLimit": profile.job_backoff_limit,
            "template": {
                "metadata": {
                    "labels": pod_labels,
                    "annotations": annotations,
                },
                "spec": pod_spec,
            },
        },
    }


class KubernetesBackend:
    name = "kubernetes"
    agent_isolation = "container"
    trusted_evaluation = "kubernetes"

    def __init__(
        self,
        profile: KubernetesProfile,
        *,
        kubectl: str = "kubectl",
    ) -> None:
        self.profile = profile
        self.kubectl = kubectl
        self._preflight_complete = False
        self._secret_preflight_complete = False
        self._proxy_url: str | None = None

    def prepare_workload(self, workload: WorkloadSpec) -> WorkloadSpec:
        image = workload.image or self.profile.agent_image
        return (
            workload
            if image == workload.image
            else replace(workload, image=image)
        )

    def _error(
        self,
        arguments: tuple[str, ...],
        return_code: int,
        stdout: bytes,
        stderr: bytes,
    ) -> BackendError:
        message = (stderr or stdout).decode(errors="replace").strip()
        lowered = message.lower()
        if any(item in lowered for item in CONNECTIVITY_FRAGMENTS):
            error_type = BackendConnectivityError
        elif any(item in lowered for item in REACHABLE_REQUEST_FRAGMENTS):
            error_type = BackendRequestError
        elif self._probe_backend_reachable():
            error_type = BackendRequestError
        else:
            error_type = BackendConnectivityError
        return error_type(
            f"{self.kubectl} {' '.join(arguments)} exited "
            f"{return_code}: {message}"
        )

    def _probe_backend_reachable(self) -> bool:
        try:
            result = subprocess.run(
                (self.kubectl, "get", "--raw=/readyz"),
                capture_output=True,
                check=False,
                timeout=min(10, self.profile.command_timeout_seconds),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode == 0:
            return True
        message = (result.stderr or result.stdout).decode(
            errors="replace"
        ).lower()
        return any(
            item in message
            for item in (
                "forbidden",
                "unauthorized",
                "the server has asked for the client to provide credentials",
            )
        )

    def _run_bytes(
        self,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        command = (self.kubectl, *arguments)
        try:
            result = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=self.profile.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise BackendConnectivityError(
                f"kubectl command timed out: {' '.join(arguments)}"
            ) from error
        except OSError as error:
            raise BackendConnectivityError(
                f"cannot execute kubectl: {error}"
            ) from error
        if check and result.returncode:
            raise self._error(
                tuple(arguments),
                result.returncode,
                result.stdout,
                result.stderr,
            )
        return result

    def _run(
        self,
        *arguments: str,
        input_value: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run_bytes(
            *arguments,
            input_bytes=(
                input_value.encode() if input_value is not None else None
            ),
            check=check,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode(errors="replace"),
            result.stderr.decode(errors="replace"),
        )

    def _apply(self, resource: dict[str, Any]) -> None:
        self._run(
            "apply",
            "-f",
            "-",
            input_value=json.dumps(resource),
        )

    def _delete(self, kind: str, name: str) -> None:
        self._run(
            "delete",
            kind,
            name,
            "-n",
            self.profile.namespace,
            "--ignore-not-found=true",
            "--wait=false",
        )

    def _delete_and_wait(self, kind: str, name: str) -> None:
        self._run(
            "delete",
            kind,
            name,
            "-n",
            self.profile.namespace,
            "--ignore-not-found=true",
            "--wait=true",
            f"--timeout={math.ceil(self.profile.command_timeout_seconds)}s",
        )

    def _delete_helper_pods(
        self,
        workload_names: tuple[str, ...],
        role: str,
    ) -> None:
        deleted: set[str] = set()
        for workload_name in dict.fromkeys(workload_names):
            value = self._get(
                "pods",
                labels=(
                    f"dev.brunner/workload={workload_name},"
                    f"dev.brunner/role={role}"
                ),
            ) or {"items": []}
            for pod in value.get("items", ()):
                name = pod.get("metadata", {}).get("name")
                if not isinstance(name, str) or not name or name in deleted:
                    continue
                self._delete_and_wait("pod", name)
                deleted.add(name)

    def _get(
        self,
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
        check: bool = True,
    ) -> dict[str, Any] | None:
        arguments = ["get", kind]
        if name:
            arguments.append(name)
        arguments.extend(("-n", self.profile.namespace))
        if labels:
            arguments.extend(("-l", labels))
        arguments.extend(("-o", "json"))
        result = self._run(*arguments, check=False)
        if result.returncode:
            if "notfound" in result.stderr.lower() or (
                "not found" in result.stderr.lower()
            ):
                return None
            raise self._error(
                tuple(arguments),
                result.returncode,
                result.stdout.encode(),
                result.stderr.encode(),
            )
        return json.loads(result.stdout)

    def _events(
        self,
        name: str,
        uid: str | None,
        *,
        required: bool = False,
        warnings: list[str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        selectors = [f"involvedObject.name={name}"]
        if uid:
            selectors.append(f"involvedObject.uid={uid}")
        result = self._run(
            "get",
            "events",
            "-n",
            self.profile.namespace,
            "--field-selector",
            ",".join(selectors),
            "-o",
            "json",
            check=False,
        )
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            if warnings is not None:
                warnings.append(
                    f"Kubernetes events unavailable for {name}: {message}"
                )
            if required:
                raise self._error(
                    (
                        "get",
                        "events",
                        "-n",
                        self.profile.namespace,
                        "--field-selector",
                        ",".join(selectors),
                    ),
                    result.returncode,
                    result.stdout.encode(),
                    result.stderr.encode(),
                )
            return ()
        try:
            events = json.loads(result.stdout).get("items", ())
        except (AttributeError, json.JSONDecodeError):
            return ()
        normalized = []
        for event in events:
            if not isinstance(event, dict):
                continue
            series = event.get("series")
            if not isinstance(series, dict):
                series = {}
            metadata = event.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            reason = str(event.get("reason") or "").strip()
            message = str(event.get("message") or "").strip()
            timestamp = str(
                event.get("eventTime")
                or series.get("lastObservedTime")
                or event.get("lastTimestamp")
                or metadata.get("creationTimestamp")
                or ""
            )
            involved = event.get("involvedObject")
            if not isinstance(involved, dict):
                involved = {}
            source = event.get("source")
            if not isinstance(source, dict):
                source = {}
            normalized.append(
                (
                    timestamp,
                    {
                        "type": event.get("type"),
                        "reason": reason or None,
                        "message": message or None,
                        "count": event.get("count"),
                        "timestamp": timestamp or None,
                        "first_timestamp": event.get("firstTimestamp"),
                        "last_timestamp": event.get("lastTimestamp"),
                        "reporting_component": event.get(
                            "reportingComponent"
                        ),
                        "reporting_instance": event.get(
                            "reportingInstance"
                        ),
                        "source_component": source.get("component"),
                        "source_host": source.get("host"),
                        "involved_kind": involved.get("kind"),
                        "involved_name": involved.get("name"),
                    },
                )
            )
        return tuple(
            value
            for _, value in sorted(
                normalized,
                key=lambda item: item[0],
            )
        )

    def _warning_events(
        self,
        name: str,
        uid: str | None,
    ) -> tuple[str, ...]:
        warnings = []
        for event in self._events(name, uid):
            if event.get("type") != "Warning":
                continue
            detail = ": ".join(
                str(value)
                for value in (event.get("reason"), event.get("message"))
                if value
            )
            if detail:
                warnings.append(detail)
        return tuple(warnings)

    def _wait_for_pod(self, name: str, timeout_seconds: float) -> None:
        result = self._run(
            "wait",
            f"pod/{name}",
            "-n",
            self.profile.namespace,
            "--for=condition=Ready",
            f"--timeout={math.ceil(timeout_seconds)}s",
            check=False,
        )
        if result.returncode:
            raise self._error(
                (
                    "wait",
                    f"pod/{name}",
                    "-n",
                    self.profile.namespace,
                ),
                result.returncode,
                result.stdout.encode(),
                result.stderr.encode(),
            )

    def _remote_protocol(self, pod_name: str) -> dict[str, str]:
        result = self._run(
            "exec",
            "-n",
            self.profile.namespace,
            pod_name,
            "--",
            "python",
            "-m",
            "brunner.backends.remote",
            "protocol",
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise BackendRequestError(
                f"helper image in Pod {pod_name} returned invalid Brunner "
                "runtime identity"
            ) from error
        if (
            not isinstance(value, dict)
            or value.get("protocol") != BRUNNER_RUNTIME_PROTOCOL
        ):
            raise BackendRequestError(
                f"helper image in Pod {pod_name} has incompatible Brunner "
                f"runtime protocol: {value!r}"
            )
        return {
            "protocol": str(value["protocol"]),
            "version": str(value.get("version") or ""),
        }

    @staticmethod
    def _stage_report(workload: WorkloadSpec) -> dict[str, Any]:
        report = load_stage_report(workload.trial / "workspace")
        return {
            **report.to_dict(),
            "file_inventory": report.file_inventory,
        }

    def _validate_images(self, workload: WorkloadSpec) -> None:
        if (
            not self.profile.unsafe_disable_network_policy_for_tests
            and not self.profile.proxy_image
        ):
            raise BackendRequestError(
                "Kubernetes campaigns require proxy_image for Brunner's "
                "managed Squid egress proxy"
            )
        if not self.profile.require_image_digests:
            return
        images = {
            "agent": workload.image or self.profile.agent_image,
            "artifact reader": self.profile.artifact_reader_image,
        }
        if not self.profile.unsafe_disable_network_policy_for_tests:
            images["egress proxy"] = self.profile.proxy_image
        if workload.evaluation is not None:
            images["evaluator"] = workload.evaluation.image
        mutable = [
            f"{label}={image!r}"
            for label, image in images.items()
            if image is not None and not _image_is_immutable(image)
        ]
        if mutable:
            raise BackendRequestError(
                "Kubernetes production workloads require immutable image "
                "digests; use image@sha256:<digest> or explicitly set "
                "require_image_digests=False for tests: "
                + ", ".join(mutable)
            )

    def _check_permission(
        self,
        verb: str,
        resource: str,
    ) -> None:
        result = self._run(
            "auth",
            "can-i",
            verb,
            resource,
            "-n",
            self.profile.namespace,
            check=False,
        )
        if result.returncode:
            raise self._error(
                (
                    "auth",
                    "can-i",
                    verb,
                    resource,
                    "-n",
                    self.profile.namespace,
                ),
                result.returncode,
                result.stdout.encode(),
                result.stderr.encode(),
            )
        if result.stdout.strip().lower() != "yes":
            message = (result.stderr or result.stdout).strip()
            raise BackendRequestError(
                "Kubernetes preflight permission denied: "
                f"{verb} {resource} in namespace "
                f"{self.profile.namespace}: {message}"
            )

    def _ensure_preflight(self, workload: WorkloadSpec) -> None:
        _effective_secret_environment(workload, self.profile)
        if not self.profile.preflight_enabled:
            self._ensure_managed_proxy()
            return
        if not self.profile.artifact_reader_image:
            raise BackendRequestError(
                "Kubernetes campaigns require artifact_reader_image so "
                "terminal and failed trials can be collected"
            )
        self._validate_images(workload)
        self._validate_reference_claim(workload)
        if self._preflight_complete:
            return
        result = self._run(
            "version",
            "--output=json",
            check=False,
        )
        if result.returncode:
            raise self._error(
                ("version", "--output=json"),
                result.returncode,
                result.stdout.encode(),
                result.stderr.encode(),
            )
        permissions = [
            ("create", "jobs.batch"),
            ("get", "jobs.batch"),
            ("list", "jobs.batch"),
            ("delete", "jobs.batch"),
            ("create", "persistentvolumeclaims"),
            ("get", "persistentvolumeclaims"),
            ("delete", "persistentvolumeclaims"),
            ("patch", "persistentvolumeclaims"),
            ("create", "pods"),
            ("get", "pods"),
            ("list", "pods"),
            ("delete", "pods"),
            ("create", "pods/exec"),
            ("get", "pods/log"),
            ("get", "events"),
            ("list", "events"),
            ("get", "resourcequotas"),
            ("list", "resourcequotas"),
        ]
        if not self.profile.unsafe_disable_network_policy_for_tests:
            permissions.extend(
                (
                    ("create", "networkpolicies.networking.k8s.io"),
                    ("get", "networkpolicies.networking.k8s.io"),
                    ("list", "networkpolicies.networking.k8s.io"),
                    ("patch", "networkpolicies.networking.k8s.io"),
                    ("delete", "networkpolicies.networking.k8s.io"),
                    ("create", "configmaps"),
                    ("get", "configmaps"),
                    ("patch", "configmaps"),
                    ("create", "services"),
                    ("get", "services"),
                    ("patch", "services"),
                    ("create", "deployments.apps"),
                    ("get", "deployments.apps"),
                    ("patch", "deployments.apps"),
                )
            )
        for verb, resource in permissions:
            self._check_permission(verb, resource)
        self._ensure_managed_proxy()
        self._preflight_complete = True

    def _ensure_workload_secrets(self, workload: WorkloadSpec) -> None:
        secret_environment = _effective_secret_environment(
            workload,
            self.profile,
        )
        if not secret_environment:
            return
        if (
            self.profile.preflight_enabled
            and not self._secret_preflight_complete
        ):
            for verb in ("get", "create", "update"):
                self._check_permission(verb, "secrets")
            self._secret_preflight_complete = True

        references: dict[str, dict[str, str]] = {}
        sources: dict[tuple[str, str], str] = {}
        for environment_name, (secret_name, secret_key) in sorted(
            secret_environment.items()
        ):
            source_key = (secret_name, secret_key)
            previous_source = sources.setdefault(
                source_key,
                environment_name,
            )
            if previous_source != environment_name:
                raise BackendConfigurationError(
                    "multiple agent environment variables reference the same "
                    "Kubernetes Secret key and cannot be provisioned "
                    f"unambiguously: {secret_name}/{secret_key}"
                )
            references.setdefault(secret_name, {})[
                secret_key
            ] = environment_name

        for secret_name, keys in sorted(references.items()):
            existing = self._get("secret", secret_name)
            existing_data: dict[str, Any] = {}
            if existing is not None:
                value = existing.get("data", {})
                if not isinstance(value, dict):
                    raise BackendConfigurationError(
                        f"Kubernetes Secret {secret_name} has malformed data"
                    )
                existing_data = value
            missing = {
                secret_key: environment_name
                for secret_key, environment_name in keys.items()
                if secret_key not in existing_data
            }
            if not missing:
                continue
            unavailable = sorted(
                environment_name
                for environment_name in missing.values()
                if not os.environ.get(environment_name)
            )
            if unavailable:
                missing_keys = ", ".join(sorted(missing))
                raise BackendConfigurationError(
                    f"Kubernetes Secret {secret_name} is absent or missing "
                    f"keys [{missing_keys}], and the orchestrator environment "
                    "does not provide non-empty variables: "
                    + ", ".join(unavailable)
                )
            encoded_missing = {
                secret_key: base64.b64encode(
                    os.environ[environment_name].encode()
                ).decode()
                for secret_key, environment_name in sorted(missing.items())
            }
            if existing is None:
                resource = {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": secret_name,
                        "namespace": self.profile.namespace,
                    },
                    "type": "Opaque",
                    "data": encoded_missing,
                }
                operation = "create"
            else:
                resource = dict(existing)
                resource["data"] = {
                    **existing_data,
                    **encoded_missing,
                }
                metadata = dict(resource.get("metadata", {}))
                metadata.pop("managedFields", None)
                resource["metadata"] = metadata
                operation = "replace"
            self._run(
                operation,
                "-f",
                "-",
                input_value=json.dumps(resource),
            )

    def _ensure_managed_proxy(self) -> None:
        if self.profile.unsafe_disable_network_policy_for_tests:
            self._proxy_url = None
            return
        if self._proxy_url is not None:
            return
        image = self.profile.proxy_image
        if not image:
            raise BackendRequestError(
                "Kubernetes campaigns require proxy_image for Brunner's "
                "managed Squid egress proxy"
            )
        for resource in render_managed_proxy_resources(
            namespace=self.profile.namespace,
            image=image,
            image_pull_secrets=self.profile.image_pull_secrets,
            dns_namespace=self.profile.dns_namespace,
            dns_pod_selector=self.profile.dns_pod_selector,
            cpu_request=self.profile.proxy_cpu_request,
            cpu_limit=self.profile.proxy_cpu_limit,
            memory_request=self.profile.proxy_memory_request,
            memory_limit=self.profile.proxy_memory_limit,
        ):
            self._apply(resource)
        rollout = self._run(
            "rollout",
            "status",
            f"deployment/{MANAGED_PROXY_NAME}",
            "-n",
            self.profile.namespace,
            f"--timeout={math.ceil(self.profile.command_timeout_seconds)}s",
            check=False,
        )
        if rollout.returncode:
            raise self._error(
                (
                    "rollout",
                    "status",
                    f"deployment/{MANAGED_PROXY_NAME}",
                    "-n",
                    self.profile.namespace,
                ),
                rollout.returncode,
                rollout.stdout.encode(),
                rollout.stderr.encode(),
            )
        service = self._get("service", MANAGED_PROXY_NAME)
        if service is None:
            raise BackendRequestError(
                f"managed proxy Service disappeared: {MANAGED_PROXY_NAME}"
            )
        try:
            self._proxy_url = proxy_url_from_service(service)
        except ValueError as error:
            raise BackendRequestError(str(error)) from error

    def _validate_exclusive_workload_networking(
        self,
        workload: WorkloadSpec,
        labels: dict[str, str],
    ) -> None:
        if self.profile.unsafe_disable_network_policy_for_tests:
            return
        value = self._get("networkpolicies") or {"items": []}
        expected_names = {
            _network_policy_name(workload, "-network"),
            _network_policy_name(workload, "-helpers"),
        }
        workload_name = str(labels["dev.brunner/workload"])
        role_labels = {
            PIPELINE_ROLE: {
                **labels,
                "dev.brunner/role": PIPELINE_ROLE,
            },
            "trial-stager": {
                **labels,
                "dev.brunner/role": "trial-stager",
            },
            "artifact-reader": {
                "app.kubernetes.io/name": "brunner",
                "dev.brunner/workload": workload_name,
                "dev.brunner/role": "artifact-reader",
            },
        }
        conflicts: list[str] = []
        for policy in value.get("items", ()):
            if not isinstance(policy, dict):
                raise BackendRequestError(
                    "Kubernetes returned a malformed NetworkPolicy list"
                )
            metadata = policy.get("metadata")
            spec = policy.get("spec")
            if not isinstance(metadata, dict) or not isinstance(
                spec, dict
            ):
                raise BackendRequestError(
                    "Kubernetes returned a malformed NetworkPolicy"
                )
            name = metadata.get("name")
            if not isinstance(name, str) or not name:
                raise BackendRequestError(
                    "Kubernetes returned a NetworkPolicy without a name"
                )
            if name in expected_names:
                continue
            selector = spec.get("podSelector")
            if not isinstance(selector, dict):
                raise BackendRequestError(
                    f"NetworkPolicy {name} has a malformed podSelector"
                )
            try:
                matching_roles = [
                    role
                    for role, candidate_labels in role_labels.items()
                    if _selector_matches_labels(
                        selector,
                        candidate_labels,
                    )
                ]
            except ValueError as error:
                raise BackendRequestError(
                    f"cannot evaluate NetworkPolicy {name}: {error}"
                ) from error
            policy_types = spec.get("policyTypes", [])
            if not isinstance(policy_types, list):
                raise BackendRequestError(
                    f"NetworkPolicy {name} has malformed policyTypes"
                )
            controls_ingress = "Ingress" in policy_types or not policy_types
            controls_egress = "Egress" in policy_types or (
                not policy_types and "egress" in spec
            )
            ingress = spec.get("ingress", [])
            if not isinstance(ingress, list):
                raise BackendRequestError(
                    f"NetworkPolicy {name} has malformed ingress rules"
                )
            egress = spec.get("egress", [])
            if not isinstance(egress, list):
                raise BackendRequestError(
                    f"NetworkPolicy {name} has malformed egress rules"
                )
            if matching_roles and controls_ingress and ingress:
                conflicts.append(
                    f"{name} (ingress: {', '.join(matching_roles)})"
                )
            if matching_roles and controls_egress and egress:
                conflicts.append(
                    f"{name} (egress: {', '.join(matching_roles)})"
                )
        if conflicts:
            raise BackendRequestError(
                "Brunner cannot guarantee exclusive workload networking "
                "because other NetworkPolicies with nonempty rules select "
                "pipeline or helper Pods: " + ", ".join(sorted(conflicts))
            )

    def _stage_trial(
        self,
        workload: WorkloadSpec,
        claim_name: str,
        image: str,
        labels: dict[str, str],
    ) -> None:
        workload_name = str(labels["dev.brunner/workload"])
        pod_name = native_resource_name(
            workload.workload_id,
            workload.resource_id,
            suffix="-stage",
        )
        # Stagers created before role labels were introduced are still
        # discoverable by their deterministic name.
        self._delete_and_wait("pod", pod_name)
        helper_labels = {
            **labels,
            "dev.brunner/role": "trial-stager",
        }
        self._delete_helper_pods(
            (workload_name,),
            "trial-stager",
        )
        self._apply(
            render_helper_pod(
                pod_name,
                claim_name,
                image,
                self.profile,
                helper_labels,
            )
        )
        primary_error: Exception | None = None
        try:
            self._wait_for_pod(
                pod_name,
                self.profile.staging_timeout_seconds,
            )
            self._remote_protocol(pod_name)
            self._run(
                "exec",
                "-n",
                self.profile.namespace,
                pod_name,
                "--",
                "python",
                "-m",
                "brunner.backends.remote",
                "clear",
                "/brunner/trial",
            )
            self._run(
                "cp",
                str(workload.trial.resolve()) + "/.",
                (
                    f"{self.profile.namespace}/{pod_name}:"
                    "/brunner/trial"
                ),
            )
            stage_report = self._stage_report(workload)
            encoded = base64.urlsafe_b64encode(
                json.dumps(
                    stage_report,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).decode()
            self._run(
                "exec",
                "-n",
                self.profile.namespace,
                pod_name,
                "--",
                "python",
                "-m",
                "brunner.backends.remote",
                "verify-stage",
                "/brunner/trial",
                encoded,
            )
        except Exception as error:
            primary_error = error
        try:
            self._delete_and_wait("pod", pod_name)
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                f"trial stager cleanup also failed: {cleanup_error}"
            )
        if primary_error is not None:
            raise primary_error

    @staticmethod
    def _state_path(trial: Path) -> Path:
        return trial / "backend/kubernetes.json"

    def _submission_handle(
        self,
        workload: WorkloadSpec,
        *,
        job_name: str,
        claim_name: str,
        submitted_at: str | None = None,
    ) -> BackendHandle:
        egress_proxy_sha256 = (
            managed_proxy_sha256(self.profile.proxy_image)
            if self.profile.proxy_image
            else None
        )
        return BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=job_name,
            trial=workload.trial.resolve(),
            metadata={
                "claim_name": claim_name,
                "namespace": self.profile.namespace,
                "submitted_at": submitted_at or _now(),
                "resource_id": workload.resource_id,
                "workload_sha256": workload.sha256,
                "challenge_sha256": self._stage_report(workload)[
                    "challenge_sha256"
                ],
                "runtime_protocol": BRUNNER_RUNTIME_PROTOCOL,
                "egress_proxy_sha256": egress_proxy_sha256,
                "evaluation_results_path": (
                    workload.evaluation.results_path
                    if workload.evaluation is not None
                    else "evaluation/results.json"
                ),
            },
        )

    @staticmethod
    def _validate_remote_submission(
        *,
        job: dict[str, Any],
        pvc: dict[str, Any] | None,
        job_name: str,
        claim_name: str,
        workload_name: str | None = None,
        workload_sha256_value: str,
        challenge_sha256: str,
        egress_proxy_sha256: str | None,
    ) -> None:
        labels = job.get("metadata", {}).get("labels", {})
        if labels.get("dev.brunner/workload") != (
            workload_name or job_name
        ):
            raise BackendRequestError(
                f"existing Kubernetes Job {job_name} is not owned by Brunner"
            )
        if pvc is None:
            raise BackendRequestError(
                f"existing Kubernetes Job {job_name} has no PVC {claim_name}"
            )
        job_annotations = job.get("metadata", {}).get("annotations", {})
        expected_job_annotations = {
            WORKLOAD_SHA256_ANNOTATION: workload_sha256_value,
            RUNTIME_PROTOCOL_ANNOTATION: BRUNNER_RUNTIME_PROTOCOL,
        }
        if egress_proxy_sha256 is not None:
            expected_job_annotations[EGRESS_PROXY_SHA256_ANNOTATION] = (
                egress_proxy_sha256
            )
        job_mismatches = {
            key: {
                "expected": expected,
                "actual": job_annotations.get(key),
            }
            for key, expected in expected_job_annotations.items()
            if job_annotations.get(key) != expected
        }
        if job_mismatches:
            raise BackendRequestError(
                f"existing Kubernetes Job {job_name} identity mismatch: "
                f"{job_mismatches}"
            )
        pvc_annotations = pvc.get("metadata", {}).get("annotations", {})
        expected_pvc_annotations = {
            STAGED_ANNOTATION: "true",
            CHALLENGE_SHA256_ANNOTATION: challenge_sha256,
            WORKLOAD_SHA256_ANNOTATION: workload_sha256_value,
            RUNTIME_PROTOCOL_ANNOTATION: BRUNNER_RUNTIME_PROTOCOL,
        }
        pvc_mismatches = {
            key: {
                "expected": expected,
                "actual": pvc_annotations.get(key),
            }
            for key, expected in expected_pvc_annotations.items()
            if pvc_annotations.get(key) != expected
        }
        if pvc_mismatches:
            raise BackendRequestError(
                f"existing Kubernetes PVC {claim_name} identity mismatch: "
                f"{pvc_mismatches}"
            )
        volumes = (
            job.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("volumes", ())
        )
        if not any(
            volume.get("persistentVolumeClaim", {}).get("claimName")
            == claim_name
            for volume in volumes
            if isinstance(volume, dict)
        ):
            raise BackendRequestError(
                f"existing Kubernetes Job {job_name} does not mount "
                f"expected PVC {claim_name}"
            )

    def _persist_submission_handle(
        self,
        state_path: Path,
        handle: BackendHandle,
    ) -> BackendHandle:
        write_json_atomic(
            state_path,
            {
                "schema_version": "1.0",
                **handle.to_dict(),
            },
        )
        return handle

    def _validate_reference_claim(self, workload: WorkloadSpec) -> None:
        evaluation = workload.evaluation
        if (
            evaluation is None
            or evaluation.reference_manifest_path is None
        ):
            return
        claim_name = self.profile.reference_claim_name
        if not claim_name:
            raise BackendRequestError(
                "Kubernetes evaluation requires reference_claim_name "
                "when the benchmark defines a reference bundle"
            )
        claim = self._get("pvc", claim_name)
        if claim is None:
            raise BackendRequestError(
                f"trusted reference PVC does not exist: {claim_name}"
            )
        access_modes = set(
            claim.get("status", {}).get("accessModes")
            or claim.get("spec", {}).get("accessModes")
            or ()
        )
        if "ReadWriteMany" not in access_modes:
            raise BackendRequestError(
                f"trusted reference PVC {claim_name} must support "
                "ReadWriteMany"
            )
        annotation = (
            claim.get("metadata", {})
            .get("annotations", {})
            .get(REFERENCE_MANIFEST_SHA256_ANNOTATION)
        )
        if annotation != evaluation.reference_manifest_sha256:
            raise BackendRequestError(
                f"trusted reference PVC {claim_name} manifest digest "
                f"mismatch: {annotation!r} != "
                f"{evaluation.reference_manifest_sha256!r}"
            )

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        workload = self.prepare_workload(workload)
        workload.validate()
        self._ensure_workload_secrets(workload)
        self._ensure_preflight(workload)
        image = workload.image
        if not image:
            raise BackendRequestError(
                "Kubernetes workloads require an agent image"
            )
        state_path = self._state_path(workload.trial)
        stage_report = self._stage_report(workload)
        workload_digest = workload.sha256
        job_name = native_resource_name(
            workload.workload_id,
            workload.resource_id,
        )
        claim_name = native_resource_name(
            workload.workload_id,
            workload.resource_id,
            suffix="-data",
        )
        labels = {
            "app.kubernetes.io/name": "brunner",
            "dev.brunner/workload": job_name,
            **workload.labels,
        }
        self._validate_exclusive_workload_networking(workload, labels)
        for policy in render_network_policies(
            workload,
            self.profile,
            labels,
        ):
            self._apply(policy)
        if state_path.is_file():
            state = json.loads(state_path.read_text())
            handle = BackendHandle(
                backend=self.name,
                workload_id=workload.workload_id,
                native_id=str(state["native_id"]),
                trial=workload.trial.resolve(),
                metadata=dict(state["metadata"]),
            )
            expected_handle = {
                "native_id": job_name,
                "workload_id": workload.workload_id,
                "claim_name": claim_name,
                "namespace": self.profile.namespace,
                "resource_id": workload.resource_id,
                "workload_sha256": workload_digest,
                "challenge_sha256": stage_report["challenge_sha256"],
                "runtime_protocol": BRUNNER_RUNTIME_PROTOCOL,
                "egress_proxy_sha256": (
                    managed_proxy_sha256(self.profile.proxy_image)
                    if self.profile.proxy_image
                    else None
                ),
            }
            actual_handle = {
                "native_id": handle.native_id,
                "workload_id": handle.workload_id,
                **{
                    key: handle.metadata.get(key)
                    for key in (
                        "claim_name",
                        "namespace",
                        "resource_id",
                        "workload_sha256",
                        "challenge_sha256",
                        "runtime_protocol",
                        "egress_proxy_sha256",
                    )
                },
            }
            mismatches = {
                key: {
                    "expected": expected,
                    "actual": actual_handle.get(key),
                }
                for key, expected in expected_handle.items()
                if actual_handle.get(key) != expected
            }
            if mismatches:
                raise BackendRequestError(
                    "persisted Kubernetes submission identity differs from "
                    f"the current workload: {mismatches}"
                )
            job = self._get("job", handle.native_id)
            pvc = self._get(
                "pvc",
                str(handle.metadata["claim_name"]),
            )
            if job is not None:
                self._validate_remote_submission(
                    job=job,
                    pvc=pvc,
                    job_name=handle.native_id,
                    claim_name=str(handle.metadata["claim_name"]),
                    workload_name=native_resource_name(
                        workload.workload_id,
                        workload.resource_id,
                    ),
                    workload_sha256_value=workload_digest,
                    challenge_sha256=str(
                        stage_report["challenge_sha256"]
                    ),
                    egress_proxy_sha256=expected_handle[
                        "egress_proxy_sha256"
                    ],
                )
            return handle

        job = self._get("job", job_name)
        pvc = self._get("pvc", claim_name)
        if job is not None:
            self._validate_remote_submission(
                job=job,
                pvc=pvc,
                job_name=job_name,
                claim_name=claim_name,
                workload_sha256_value=workload_digest,
                challenge_sha256=str(
                    stage_report["challenge_sha256"]
                ),
                egress_proxy_sha256=(
                    managed_proxy_sha256(self.profile.proxy_image)
                    if self.profile.proxy_image
                    else None
                ),
            )
            handle = self._submission_handle(
                workload,
                job_name=job_name,
                claim_name=claim_name,
                submitted_at=job.get("metadata", {}).get("creationTimestamp"),
            )
            return self._persist_submission_handle(state_path, handle)

        if pvc is None:
            self._apply(render_pvc(claim_name, self.profile, labels))
            staged = False
        else:
            pvc_labels = pvc.get("metadata", {}).get("labels", {})
            if pvc_labels.get("dev.brunner/workload") != job_name:
                raise BackendRequestError(
                    f"existing Kubernetes PVC {claim_name} is not owned "
                    "by this Brunner workload"
                )
            staged = (
                pvc.get("metadata", {}).get("annotations", {})
            )
            if staged.get(STAGED_ANNOTATION) == "true":
                expected_annotations = {
                    CHALLENGE_SHA256_ANNOTATION: stage_report[
                        "challenge_sha256"
                    ],
                    WORKLOAD_SHA256_ANNOTATION: workload_digest,
                    RUNTIME_PROTOCOL_ANNOTATION: (
                        BRUNNER_RUNTIME_PROTOCOL
                    ),
                }
                mismatches = {
                    key: {
                        "expected": expected,
                        "actual": staged.get(key),
                    }
                    for key, expected in expected_annotations.items()
                    if staged.get(key) != expected
                }
                if mismatches:
                    raise BackendRequestError(
                        f"existing staged Kubernetes PVC {claim_name} "
                        f"identity mismatch: {mismatches}"
                    )
                staged = True
            else:
                staged = False
        if not staged:
            self._stage_trial(workload, claim_name, image, labels)
            self._run(
                "annotate",
                "pvc",
                claim_name,
                "-n",
                self.profile.namespace,
                f"{STAGED_ANNOTATION}=true",
                (
                    f"{CHALLENGE_SHA256_ANNOTATION}="
                    f"{stage_report['challenge_sha256']}"
                ),
                f"{WORKLOAD_SHA256_ANNOTATION}={workload_digest}",
                (
                    f"{RUNTIME_PROTOCOL_ANNOTATION}="
                    f"{BRUNNER_RUNTIME_PROTOCOL}"
                ),
                "--overwrite",
            )
        self._validate_exclusive_workload_networking(workload, labels)
        self._apply(
            render_job(
                job_name,
                claim_name,
                workload,
                self.profile,
                labels,
                proxy_url=self._proxy_url,
            )
        )
        handle = self._submission_handle(
            workload,
            job_name=job_name,
            claim_name=claim_name,
        )
        return self._persist_submission_handle(state_path, handle)

    def restart(
        self,
        handle: BackendHandle,
        workload: WorkloadSpec,
        generation: int,
    ) -> BackendHandle:
        workload = self.prepare_workload(workload)
        workload.validate()
        self._ensure_workload_secrets(workload)
        self._ensure_preflight(workload)
        if generation < 1:
            raise BackendRequestError(
                "Kubernetes restart generation must be positive"
            )
        expected_proxy_sha256 = (
            managed_proxy_sha256(self.profile.proxy_image)
            if self.profile.proxy_image
            else None
        )
        if handle.metadata.get("egress_proxy_sha256") != (
            expected_proxy_sha256
        ):
            raise BackendRequestError(
                "cannot restart Kubernetes workload with a different "
                "managed egress proxy identity"
            )
        image = workload.image
        if not image:
            raise BackendRequestError(
                "Kubernetes workloads require an agent image"
            )
        workload_name = native_resource_name(
            workload.workload_id,
            workload.resource_id,
        )
        claim_name = str(handle.metadata["claim_name"])
        job_name = native_resource_name(
            workload.workload_id,
            workload.resource_id,
            suffix=f"-r{generation}",
        )
        labels = {
            "app.kubernetes.io/name": "brunner",
            "dev.brunner/workload": workload_name,
            "dev.brunner/restart-generation": str(generation),
            **workload.labels,
        }
        self._validate_exclusive_workload_networking(workload, labels)
        pvc = self._get("pvc", claim_name)
        if pvc is None:
            raise BackendRequestError(
                f"cannot restart Kubernetes workload without PVC {claim_name}"
            )
        pvc_labels = pvc.get("metadata", {}).get("labels", {})
        if pvc_labels.get("dev.brunner/workload") != workload_name:
            raise BackendRequestError(
                f"existing Kubernetes PVC {claim_name} is not owned "
                "by this Brunner workload"
            )
        stage_report = self._stage_report(workload)
        pvc_annotations = pvc.get("metadata", {}).get("annotations", {})
        expected_pvc_annotations = {
            STAGED_ANNOTATION: "true",
            CHALLENGE_SHA256_ANNOTATION: stage_report["challenge_sha256"],
            WORKLOAD_SHA256_ANNOTATION: workload.sha256,
            RUNTIME_PROTOCOL_ANNOTATION: BRUNNER_RUNTIME_PROTOCOL,
        }
        mismatches = {
            key: {
                "expected": expected,
                "actual": pvc_annotations.get(key),
            }
            for key, expected in expected_pvc_annotations.items()
            if pvc_annotations.get(key) != expected
        }
        if mismatches:
            raise BackendRequestError(
                f"cannot restart from unverified Kubernetes PVC "
                f"{claim_name}: {mismatches}"
            )
        for policy in render_network_policies(
            workload,
            self.profile,
            labels,
        ):
            self._apply(policy)
        job = self._get("job", job_name)
        if job is not None:
            self._validate_remote_submission(
                job=job,
                pvc=pvc,
                job_name=job_name,
                claim_name=claim_name,
                workload_name=workload_name,
                workload_sha256_value=workload.sha256,
                challenge_sha256=str(stage_report["challenge_sha256"]),
                egress_proxy_sha256=expected_proxy_sha256,
            )
            restarted = self._submission_handle(
                workload,
                job_name=job_name,
                claim_name=claim_name,
                submitted_at=job.get("metadata", {}).get("creationTimestamp"),
            )
            restarted.metadata["restart_generation"] = generation
            return self._persist_submission_handle(
                self._state_path(workload.trial),
                restarted,
            )

        self._delete_and_wait("job", handle.native_id)
        self._validate_exclusive_workload_networking(workload, labels)
        self._apply(
            render_job(
                job_name,
                claim_name,
                workload,
                self.profile,
                labels,
                proxy_url=self._proxy_url,
            )
        )
        restarted = self._submission_handle(
            workload,
            job_name=job_name,
            claim_name=claim_name,
        )
        restarted.metadata["restart_generation"] = generation
        return self._persist_submission_handle(
            self._state_path(workload.trial),
            restarted,
        )

    def _pods_for_handle(
        self,
        handle: BackendHandle,
    ) -> tuple[dict[str, Any], ...]:
        value = self._get(
            "pods",
            labels=f"job-name={handle.native_id}",
        )
        if not value:
            return ()
        items = [
            item
            for item in value.get("items", [])
            if isinstance(item, dict)
        ]
        return tuple(
            sorted(
                items,
                key=lambda item: str(
                    item.get("metadata", {}).get(
                        "creationTimestamp",
                        "",
                    )
                ),
            )
        )

    def _pod_for_handle(
        self,
        handle: BackendHandle,
    ) -> dict[str, Any] | None:
        pods = self._pods_for_handle(handle)
        return pods[-1] if pods else None

    @staticmethod
    def _pod_summary(pod: dict[str, Any]) -> dict[str, Any]:
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        return {
            "name": metadata.get("name"),
            "uid": metadata.get("uid"),
            "created_at": metadata.get("creationTimestamp"),
            "phase": status.get("phase"),
            "reason": status.get("reason"),
            "message": status.get("message"),
            "node": pod.get("spec", {}).get("nodeName"),
            "container_terminations": list(
                KubernetesBackend._terminated_containers(pod)
            ),
        }

    @staticmethod
    def _select_terminal_pod(
        pods: tuple[dict[str, Any], ...],
        phase: str,
    ) -> dict[str, Any] | None:
        if not pods:
            return None
        if phase == "succeeded":
            succeeded = [
                pod
                for pod in pods
                if pod.get("status", {}).get("phase") == "Succeeded"
            ]
            if succeeded:
                return succeeded[-1]
        return pods[-1]

    @staticmethod
    def _terminated_containers(
        pod: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], ...]:
        if pod is None:
            return ()
        status = pod.get("status", {})
        terminated_containers = []
        for key in ("initContainerStatuses", "containerStatuses"):
            for item in status.get(key, []):
                terminated = item.get("state", {}).get("terminated")
                if terminated:
                    terminated_containers.append(
                        {
                            "container": item.get("name"),
                            "container_type": (
                                "init"
                                if key == "initContainerStatuses"
                                else "main"
                            ),
                            **terminated,
                        }
                    )
        return tuple(terminated_containers)

    @staticmethod
    def _termination_summary(
        terminated: dict[str, Any] | None,
        key: str,
    ) -> dict[str, Any] | None:
        if terminated is None:
            return None
        message = terminated.get("message")
        if not isinstance(message, str) or not message.strip():
            return None
        try:
            value = json.loads(message)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        summary = value.get(key)
        return summary if isinstance(summary, dict) else None

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        claim_name = str(handle.metadata["claim_name"])
        pvc = self._get("pvc", claim_name, check=False)
        warnings = []
        if pvc and pvc.get("status", {}).get("phase") == "Pending":
            warnings.append(
                f"PVC {claim_name} is still Pending; check storage class, "
                "capacity, and volume binding events"
            )
            warnings.extend(
                f"PVC {claim_name}: {warning}"
                for warning in self._warning_events(
                    claim_name,
                    (
                        str(pvc.get("metadata", {}).get("uid"))
                        if pvc.get("metadata", {}).get("uid") is not None
                        else None
                    ),
                )
            )
        job = self._get("job", handle.native_id, check=False)
        if job is None:
            claim_phase = (
                pvc.get("status", {}).get("phase") if pvc else None
            )
            storage_present = pvc is not None
            return BackendSnapshot(
                phase="failed",
                reason=(
                    "JobMissing"
                    if storage_present
                    else "TrialStorageMissing"
                ),
                message=(
                    "workload Job is missing; the durable trial PVC can be "
                    "restarted"
                    if storage_present
                    else "workload Job and durable trial PVC are both missing"
                ),
                warnings=tuple(warnings),
                details={
                    "claim_phase": claim_phase,
                    "retryable_infrastructure": storage_present,
                },
            )
        job_status = job.get("status", {})
        conditions = {
            item.get("type"): item
            for item in job_status.get("conditions", [])
        }
        if "Complete" in conditions:
            phase = "succeeded"
        elif "Failed" in conditions:
            phase = "failed"
        elif job_status.get("active"):
            phase = "running"
        else:
            phase = "pending"
        pods = self._pods_for_handle(handle)
        pod = self._select_terminal_pod(pods, phase)
        terminations = self._terminated_containers(pod)
        agent_termination = next(
            (
                item
                for item in terminations
                if item.get("container") == "agent"
            ),
            None,
        )
        evaluator_termination = next(
            (
                item
                for item in terminations
                if item.get("container") == "evaluator"
            ),
            None,
        )
        brunner_pipeline = self._termination_summary(
            agent_termination,
            "brunner_pipeline",
        )
        brunner_evaluation = self._termination_summary(
            evaluator_termination,
            "brunner_evaluation",
        )
        failed_terminations = [
            item
            for item in terminations
            if (
                int(item.get("exitCode") or 0) != 0
                or int(item.get("signal") or 0) != 0
                or item.get("reason") not in {None, "Completed"}
            )
        ]
        terminated = (
            next(
                (
                    item
                    for item in failed_terminations
                    if item.get("container") == "evaluator"
                ),
                None,
            )
            or next(
                (
                    item
                    for item in failed_terminations
                    if item.get("container") == "agent"
                ),
                None,
            )
            or evaluator_termination
            or agent_termination
        )
        reason = None
        message = None
        exit_code = terminated.get("exitCode") if terminated else None
        termination_reason = terminated.get("reason") if terminated else None
        termination_signal = (
            int(terminated.get("signal") or 0) if terminated else 0
        )
        brunner_incomplete = bool(
            brunner_pipeline
            and brunner_pipeline.get("infrastructure_failure") is True
        )
        evaluation_failed = bool(
            brunner_evaluation
            and brunner_evaluation.get("status") == "failed"
        )
        container_failed = bool(
            terminated
            and (
                int(terminated.get("exitCode") or 0) != 0
                or termination_signal != 0
                or termination_reason not in {None, "Completed"}
                or brunner_incomplete
            )
        )
        if container_failed and phase in {"succeeded", "failed"}:
            phase = "failed"
            if termination_reason in RETRYABLE_CONTAINER_FAILURES:
                reason = termination_reason
            elif brunner_incomplete:
                reason = (
                    brunner_pipeline.get("infrastructure_reason")
                    or "AgentPipelineIncomplete"
                )
                message = (
                    brunner_pipeline.get("failure")
                    or "Brunner agent did not produce a terminal "
                    "provider result"
                )
            elif evaluation_failed:
                evaluation_failure = brunner_evaluation.get("failure")
                if not isinstance(evaluation_failure, dict):
                    evaluation_failure = {}
                reason = str(
                    evaluation_failure.get("reason")
                    or "EvaluatorFailed"
                )
                message = str(
                    evaluation_failure.get("message")
                    or "trusted evaluator did not complete successfully"
                )
            else:
                reason = termination_reason or "ContainerFailed"
                message = terminated.get("message")
            container_name = terminated.get("container")
            if container_name:
                warnings.append(
                    f"container {container_name} terminated before workload "
                    "completion; inspect preserved logs"
                )
        if phase == "failed" and reason is None:
            failed = conditions.get("Failed", {})
            reason = failed.get("reason") or "JobFailed"
            message = failed.get("message")
        pod_status = pod.get("status", {}) if pod else {}
        failed_condition = conditions.get("Failed", {})
        job_failure_reason = failed_condition.get("reason")
        pod_failure_reason = pod_status.get("reason")
        kubernetes_events: dict[str, list[dict[str, Any]]] = {
            "job": [],
            "pod": [],
        }
        if phase in {"succeeded", "failed"}:
            job_metadata = job.get("metadata", {})
            kubernetes_events["job"] = list(
                self._events(
                    str(job_metadata.get("name") or handle.native_id),
                    (
                        str(job_metadata["uid"])
                        if job_metadata.get("uid") is not None
                        else None
                    ),
                    warnings=warnings,
                )
            )
            for event_pod in pods:
                event_metadata = event_pod.get("metadata", {})
                kubernetes_events["pod"].extend(
                    self._events(
                        str(event_metadata.get("name") or ""),
                        (
                            str(event_metadata["uid"])
                            if event_metadata.get("uid") is not None
                            else None
                        ),
                        warnings=warnings,
                    )
                )
            for event in (
                *kubernetes_events["job"],
                *kubernetes_events["pod"],
            ):
                if event.get("type") != "Warning":
                    continue
                detail = ": ".join(
                    str(value)
                    for value in (
                        event.get("reason"),
                        event.get("message"),
                    )
                    if value
                )
                if detail and detail not in warnings:
                    warnings.append(detail)
        if (
            brunner_evaluation
            and brunner_evaluation.get("retryable_infrastructure") is False
        ):
            retryable_evidence = False
        elif brunner_incomplete:
            retryable_evidence = (
                brunner_pipeline.get("retryable_infrastructure") is True
            )
        else:
            retryable_evidence = (
                (
                    exit_code not in {None, 0}
                )
                or termination_signal != 0
                or reason in RETRYABLE_CONTAINER_FAILURES
                or pod_failure_reason
                in {
                    "Evicted",
                    "NodeLost",
                    "Shutdown",
                }
                or job_failure_reason == "BackoffLimitExceeded"
            )
        retryable_infrastructure = bool(
            phase == "failed"
            and job_failure_reason not in NON_RETRYABLE_JOB_FAILURES
            and reason not in NON_RETRYABLE_CONTAINER_FAILURES
            and retryable_evidence
        )
        return BackendSnapshot(
            phase=phase,
            reason=reason,
            message=message,
            exit_code=exit_code,
            node=(pod.get("spec", {}).get("nodeName") if pod else None),
            started_at=job_status.get("startTime"),
            finished_at=job_status.get("completionTime"),
            warnings=tuple(warnings),
            details={
                "pod_phase": pod_status.get("phase"),
                "claim_phase": (
                    pvc.get("status", {}).get("phase") if pvc else None
                ),
                "terminated_container": terminated,
                "container_terminations": list(terminations),
                "pods": [
                    self._pod_summary(item)
                    for item in pods
                ],
                "job_failure_reason": job_failure_reason,
                "pod_failure_reason": pod_failure_reason,
                "retryable_infrastructure": retryable_infrastructure,
                "brunner_pipeline": brunner_pipeline,
                "brunner_evaluation": brunner_evaluation,
                "kubernetes_events": kubernetes_events,
            },
        )

    def logs(self, handle: BackendHandle) -> str:
        pods = self._pods_for_handle(handle)
        targets = [
            f"pod/{pod.get('metadata', {}).get('name')}"
            for pod in pods
            if pod.get("metadata", {}).get("name")
        ]
        if not targets:
            targets = [f"job/{handle.native_id}"]
        output = []
        for target in targets:
            result = self._run(
                "logs",
                target,
                "-n",
                self.profile.namespace,
                "--all-containers=true",
                "--prefix=true",
                check=False,
            )
            if (
                result.returncode
                and "not found" not in result.stderr.lower()
            ):
                raise self._error(
                    ("logs", target),
                    result.returncode,
                    result.stdout.encode(),
                    result.stderr.encode(),
                )
            output.append(
                f"===== {target} =====\n"
                + result.stdout
                + result.stderr
            )
        return "\n".join(output)

    def _reader(
        self,
        handle: BackendHandle,
        attempt: int,
        excluded_nodes: tuple[str, ...],
    ) -> tuple[str, str | None]:
        image = self.profile.artifact_reader_image
        if not image:
            raise BackendRequestError(
                "Kubernetes artifact collection requires "
                "artifact_reader_image"
            )
        name = native_resource_name(
            handle.workload_id,
            handle.trial,
            suffix=f"-reader-{attempt}",
        )
        labels = {
            "app.kubernetes.io/name": "brunner",
            "dev.brunner/workload": native_resource_name(
                handle.workload_id,
                handle.trial,
            ),
            "dev.brunner/role": "artifact-reader",
        }
        self._apply(
            render_helper_pod(
                name,
                str(handle.metadata["claim_name"]),
                image,
                self.profile,
                labels,
                trial_read_only=True,
                excluded_nodes=excluded_nodes,
            )
        )
        try:
            self._wait_for_pod(
                name,
                self.profile.reader_timeout_seconds,
            )
            self._remote_protocol(name)
        except BackendRequestError as error:
            pod = self._get("pod", name, check=False)
            node = (
                pod.get("spec", {}).get("nodeName")
                if pod is not None
                else None
            )
            event_warnings = self._warning_events(
                name,
                (
                    str(pod.get("metadata", {}).get("uid"))
                    if pod
                    and pod.get("metadata", {}).get("uid") is not None
                    else None
                ),
            )
            try:
                self._delete_and_wait("pod", name)
            except BackendConnectivityError:
                raise
            except BackendError as cleanup_error:
                error.add_note(
                    f"artifact reader cleanup also failed: {cleanup_error}"
                )
            detail = str(error)
            if event_warnings:
                detail += "; Kubernetes warning: " + event_warnings[-1]
            raise ReaderMountError(detail, node=node) from error
        pod = self._get("pod", name)
        node = pod.get("spec", {}).get("nodeName") if pod else None
        return name, node

    def _remote_inventory(
        self,
        pod: str,
        policy: ArtifactPolicy,
        included_groups: frozenset[str],
        evaluation_results_path: str = "evaluation/results.json",
        included_globs: tuple[str, ...] | None = None,
    ) -> dict[str, dict[str, Any]]:
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "excluded_globs": list(policy.excluded_globs),
                    "groups": {
                        name: list(patterns)
                        for name, patterns in policy.groups.items()
                    },
                    "allow_symlinks": policy.allow_symlinks,
                    "collect_evaluated_artifacts": (
                        policy.collect_evaluated_artifacts
                    ),
                    "max_collection_bytes": policy.max_collection_bytes,
                    "failure_diagnostic_globs": list(
                        policy.failure_diagnostic_globs
                    ),
                    "max_diagnostic_collection_bytes": (
                        policy.max_diagnostic_collection_bytes
                    ),
                    "included_groups": sorted(included_groups),
                    "included_globs": (
                        list(included_globs)
                        if included_globs is not None
                        else None
                    ),
                },
                separators=(",", ":"),
            ).encode()
        ).decode()
        result = self._run_bytes(
            "exec",
            "-n",
            self.profile.namespace,
            pod,
            "--",
            "python",
            "-m",
            "brunner.backends.remote",
            "inventory",
            "/brunner/trial",
            encoded,
            evaluation_results_path,
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise IntegrityError(
                "remote artifact inventory is not valid JSON"
            ) from error
        if not isinstance(value, dict):
            raise IntegrityError(
                "remote artifact inventory is not an object"
            )
        for name, metadata in value.items():
            relative = Path(name)
            if (
                not isinstance(name, str)
                or not name
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                raise IntegrityError(
                    f"remote artifact inventory has unsafe path: {name!r}"
                )
            if not isinstance(metadata, dict):
                raise IntegrityError(
                    f"remote artifact metadata is not an object: {name}"
                )
        return value

    def _read_remote(
        self,
        pod: str,
        relative_path: str,
        offset: int,
        count: int,
    ) -> bytes:
        result = self._run_bytes(
            "exec",
            "-n",
            self.profile.namespace,
            pod,
            "--",
            "python",
            "-m",
            "brunner.backends.remote",
            "read",
            "/brunner/trial",
            relative_path,
            str(offset),
            str(count),
        )
        return result.stdout

    def _read_remote_with_retries(
        self,
        pod: str,
        relative_path: str,
        offset: int,
        count: int,
    ) -> bytes:
        failures = []
        for attempt in range(1, self.profile.artifact_chunk_attempts + 1):
            try:
                data = self._read_remote(
                    pod,
                    relative_path,
                    offset,
                    count,
                )
                if not data:
                    raise ArtifactTransferError(
                        f"remote artifact ended early: {relative_path}"
                    )
                return data
            except (
                ArtifactTransferError,
                BackendConnectivityError,
                BackendRequestError,
            ) as error:
                if (
                    isinstance(error, BackendConnectivityError)
                    and not self._probe_backend_reachable()
                ):
                    raise
                failures.append(f"attempt {attempt}: {error}")
                if attempt < self.profile.artifact_chunk_attempts:
                    time.sleep(
                        self.profile.artifact_chunk_retry_seconds
                    )
        raise ArtifactTransferError(
            "remote artifact chunk failed after retries: "
            f"{relative_path} offset={offset} count={count}; "
            + "; ".join(failures)
        )

    def _collect_from_reader(
        self,
        pod: str,
        destination: Path,
        policy: ArtifactPolicy,
        included_groups: frozenset[str],
        baseline_trial: Path | None = None,
        evaluation_results_path: str = "evaluation/results.json",
    ) -> dict[str, Any]:
        inventory_policy = replace(policy, max_collection_bytes=None)
        inventory = self._remote_inventory(
            pod,
            inventory_policy,
            included_groups,
            evaluation_results_path,
        )
        unchanged = self._unchanged_staged_files(
            baseline_trial,
            inventory,
        )
        transfer_inventory = {
            name: metadata
            for name, metadata in inventory.items()
            if name not in unchanged
        }
        collection_mode = "complete"
        omitted_files = 0
        omitted_bytes = 0
        try:
            transferred_bytes = enforce_inventory_size(
                transfer_inventory,
                policy.max_collection_bytes,
            )
        except IntegrityError:
            if evaluation_results_path in inventory:
                raise
            full_inventory = inventory
            inventory = self._remote_inventory(
                pod,
                inventory_policy,
                included_groups,
                evaluation_results_path,
                included_globs=policy.failure_diagnostic_globs,
            )
            unchanged = self._unchanged_staged_files(
                baseline_trial,
                inventory,
            )
            transfer_inventory = {
                name: metadata
                for name, metadata in inventory.items()
                if name not in unchanged
            }
            transferred_bytes = enforce_inventory_size(
                transfer_inventory,
                policy.max_diagnostic_collection_bytes,
            )
            collection_mode = "diagnostics"
            omitted_files = len(full_inventory) - len(inventory)
            omitted_bytes = sum(
                int(metadata.get("size", 0))
                for name, metadata in full_inventory.items()
                if name not in inventory
                and metadata.get("type") == "file"
            )
        partial, complete = prepare_partial_artifacts(
            destination,
            inventory,
            inventory_policy,
            included_groups,
        )
        for name in unchanged:
            if name in complete:
                continue
            assert baseline_trial is not None
            source = baseline_trial / name
            expected = inventory[name]
            if (
                expected.get("type") != "file"
                or not source.is_file()
                or source.is_symlink()
                or source.stat().st_size != int(expected["size"])
            ):
                raise IntegrityError(
                    f"staged baseline file is unavailable for reuse: {name}"
                )
            target = partial / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            try:
                target.hardlink_to(source)
            except OSError as error:
                raise IntegrityError(
                    "cannot reuse unchanged staged file without copying "
                    f"its bytes: {name}: {error}"
                ) from error
        for name, expected in inventory.items():
            if name in complete or name in unchanged:
                continue
            if expected.get("type") != "file":
                raise IntegrityError(
                    f"remote artifact has unsupported type: {name}"
                )
            target = partial / name
            target.parent.mkdir(parents=True, exist_ok=True)
            expected_size = int(expected["size"])
            if target.exists() and target.stat().st_size > expected_size:
                target.unlink()
            offset = target.stat().st_size if target.exists() else 0
            with target.open("ab" if offset else "wb") as stream:
                while offset < expected_size:
                    count = min(
                        self.profile.artifact_chunk_bytes,
                        expected_size - offset,
                    )
                    data = self._read_remote_with_retries(
                        pod,
                        name,
                        offset,
                        count,
                    )
                    stream.write(data)
                    stream.flush()
                    offset += len(data)
            actual = artifact_metadata(target)
            if actual is None or actual.to_dict() != expected:
                target.unlink(missing_ok=True)
                raise IntegrityError(
                    f"remote artifact checksum mismatch: {name}"
                )
        result = finalize_artifact_collection(
            partial,
            destination,
            inventory,
            inventory_policy,
            included_groups=included_groups,
        )
        result["reused_staged_files"] = len(unchanged)
        result["transferred_bytes"] = transferred_bytes
        result["collection_mode"] = collection_mode
        result["omitted_files"] = omitted_files
        result["omitted_bytes"] = omitted_bytes
        return result

    @staticmethod
    def _unchanged_staged_files(
        baseline_trial: Path | None,
        inventory: dict[str, dict[str, Any]],
    ) -> frozenset[str]:
        if baseline_trial is None:
            return frozenset()
        marker = baseline_trial / "workspace/.brunner-challenge.json"
        if not marker.is_file():
            return frozenset()
        try:
            value = json.loads(marker.read_text())
        except (json.JSONDecodeError, OSError):
            return frozenset()
        baseline = value.get("file_inventory")
        if not isinstance(baseline, dict):
            return frozenset()
        unchanged = set()
        for relative, metadata in baseline.items():
            if not isinstance(relative, str) or not isinstance(metadata, dict):
                continue
            relative_path = Path(relative)
            if (
                not relative
                or relative_path.is_absolute()
                or ".." in relative_path.parts
            ):
                continue
            name = f"workspace/{relative}"
            if inventory.get(name) == metadata:
                unchanged.add(name)
        return frozenset(unchanged)

    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        workload_name = native_resource_name(
            handle.workload_id,
            handle.trial,
        )
        self._delete_helper_pods(
            (workload_name, handle.native_id),
            "artifact-reader",
        )
        excluded_nodes: list[str] = []
        failures = []
        attempts = max(1, self.profile.reader_attempts)
        for attempt in range(1, attempts + 1):
            pod = None
            node = None
            result = None
            primary_error: Exception | None = None
            try:
                pod, node = self._reader(
                    handle,
                    attempt,
                    tuple(excluded_nodes),
                )
                result = self._collect_from_reader(
                    pod,
                    destination,
                    policy,
                    included_groups,
                    handle.trial,
                    str(
                        handle.metadata.get(
                            "evaluation_results_path",
                            "evaluation/results.json",
                        )
                    ),
                )
                state_path = self._state_path(handle.trial)
                if state_path.is_file():
                    state = json.loads(state_path.read_text())
                    state["artifacts_collected_at"] = _now()
                    write_json_atomic(state_path, state)
            except Exception as error:
                primary_error = error
            if pod is not None:
                try:
                    self._delete_and_wait("pod", pod)
                except Exception as cleanup_error:
                    if primary_error is None:
                        raise
                    primary_error.add_note(
                        "artifact reader cleanup also failed: "
                        f"{cleanup_error}"
                    )
            if primary_error is None:
                if result is None:
                    raise ArtifactTransferError(
                        "artifact reader returned no collection result"
                    )
                return result
            if isinstance(primary_error, BackendConnectivityError):
                raise primary_error
            if isinstance(primary_error, IntegrityError):
                raise primary_error
            if not isinstance(
                primary_error,
                (ArtifactTransferError, BackendRequestError),
            ):
                raise primary_error
            failures.append(f"attempt {attempt}: {primary_error}")
            if (
                isinstance(primary_error, ReaderMountError)
                and primary_error.node
                and primary_error.node not in excluded_nodes
            ):
                excluded_nodes.append(primary_error.node)
            elif node and node not in excluded_nodes:
                excluded_nodes.append(node)
        raise ArtifactTransferError(
            "artifact reader failed after retries; partial files and PVC "
            "were preserved: " + "; ".join(failures)
        )

    def cleanup(self, handle: BackendHandle) -> None:
        state_path = self._state_path(handle.trial)
        state = (
            json.loads(state_path.read_text())
            if state_path.is_file()
            else {}
        )
        retain_storage = False
        if (
            self.profile.retain_failed_storage
            and not state.get("artifacts_collected_at")
        ):
            retain_storage = self.inspect(handle).phase == "failed"
        workload_name = native_resource_name(
            handle.workload_id,
            handle.trial,
        )
        helper_workloads = (workload_name, handle.native_id)
        self._delete_and_wait(
            "pod",
            native_resource_name(
                handle.workload_id,
                handle.trial,
                suffix="-stage",
            ),
        )
        self._delete_helper_pods(helper_workloads, "trial-stager")
        self._delete_helper_pods(helper_workloads, "artifact-reader")
        self._delete_and_wait("job", handle.native_id)
        if not self.profile.unsafe_disable_network_policy_for_tests:
            resource_id = str(
                handle.metadata.get("resource_id")
                or trial_resource_id(handle.trial)
            )
            self._delete_and_wait(
                "networkpolicy",
                native_resource_name(
                    handle.workload_id,
                    resource_id,
                    suffix="-network",
                ),
            )
            self._delete_and_wait(
                "networkpolicy",
                native_resource_name(
                    handle.workload_id,
                    resource_id,
                    suffix="-helpers",
                ),
            )
        if retain_storage:
            state["storage_retained"] = True
            state["storage_retained_at"] = _now()
            if state_path.parent.is_dir():
                write_json_atomic(state_path, state)
            return
        self._delete_and_wait(
            "pvc",
            str(handle.metadata["claim_name"]),
        )

    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity:
        if workload is not None:
            workload = self.prepare_workload(workload)
            workload.validate()
            self._ensure_preflight(workload)
        value = self._get(
            "jobs",
            labels="app.kubernetes.io/name=brunner",
        ) or {"items": []}
        running = 0
        pending = 0
        for job in value.get("items", []):
            status = job.get("status", {})
            if status.get("active"):
                running += 1
            elif not status.get("succeeded") and not status.get("failed"):
                pending += 1
        profile_available = (
            None
            if self.profile.max_parallel is None
            else max(
                0,
                self.profile.max_parallel - running - pending,
            )
        )
        quota_available: int | None = None
        quota_limits: list[dict[str, Any]] = []
        requirements: dict[str, Decimal] = {}
        if workload is not None:
            labels = {
                "app.kubernetes.io/name": "brunner",
                "dev.brunner/workload": native_resource_name(
                    workload.workload_id,
                    workload.resource_id,
                ),
            }
            requirements = _resource_requirements(
                render_job(
                    "capacity-probe",
                    "capacity-probe-data",
                    workload,
                    self.profile,
                    labels,
                    proxy_url=self._proxy_url,
                ),
                render_pvc(
                    "capacity-probe-data",
                    self.profile,
                    labels,
                ),
            )
            if not self.profile.unsafe_disable_network_policy_for_tests:
                requirements[
                    "count/networkpolicies.networking.k8s.io"
                ] = Decimal(2)
            quotas = self._get("resourcequota") or {"items": []}
            for quota in quotas.get("items", ()):
                hard = quota.get("status", {}).get("hard", {})
                used = quota.get("status", {}).get("used", {})
                if not isinstance(hard, dict) or not isinstance(used, dict):
                    continue
                constrained = []
                for resource, requirement in requirements.items():
                    if resource not in hard or requirement <= 0:
                        continue
                    remaining = max(
                        Decimal(0),
                        _quantity(hard[resource])
                        - _quantity(used.get(resource, "0")),
                    )
                    capacity = int(remaining // requirement)
                    constrained.append(capacity)
                    quota_limits.append(
                        {
                            "quota": quota.get("metadata", {}).get("name"),
                            "resource": resource,
                            "hard": hard[resource],
                            "used": used.get(resource, "0"),
                            "required": str(requirement),
                            "available_workloads": capacity,
                        }
                    )
                if constrained:
                    current = min(constrained)
                    quota_available = (
                        current
                        if quota_available is None
                        else min(quota_available, current)
                    )
        available = profile_available
        if quota_available is not None:
            available = (
                quota_available
                if available is None
                else min(available, quota_available)
            )
        limit = self.profile.max_parallel
        if quota_available is not None:
            quota_limit = running + pending + quota_available
            limit = (
                quota_limit
                if limit is None
                else min(limit, quota_limit)
            )
        return BackendCapacity(
            limit=limit,
            running=running,
            pending=pending,
            available=available,
            details={
                "namespace": self.profile.namespace,
                "checked_at": _now(),
                "requirements": {
                    name: str(value)
                    for name, value in requirements.items()
                },
                "quota_limits": quota_limits,
            },
        )
