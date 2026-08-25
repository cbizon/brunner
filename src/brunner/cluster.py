from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from brunner.archive import (
    ARCHIVE_MANIFEST,
    load_campaign_archive,
)
from brunner.backends import KubernetesBackend, KubernetesProfile
from brunner.artifacts import artifact_metadata
from brunner.backends.squid import (
    MANAGED_PROXY_PORT,
    managed_proxy_labels,
)
from brunner.campaign import (
    CampaignEngine,
    CampaignPlan,
    _campaign_evaluation_spec,
    _evaluation_sha256,
    campaign_resource_name,
)
from brunner.contract import OutputContract
from brunner.definition import BenchmarkDefinition
from brunner.errors import (
    BackendConnectivityError,
    BackendRequestError,
    EvaluationPending,
    IntegrityError,
)
from brunner.hashing import sha256_file
from brunner.io import write_json_atomic


CONTROL_ROOT = Path("/brunner/control")
RESULTS_ROOT = Path("/brunner/results")
RESULT_MANIFEST = ARCHIVE_MANIFEST
RESULT_MANIFEST_SHA256_ANNOTATION = (
    "dev.brunner/result-manifest-sha256"
)
RESULT_MANIFEST_SIZE_ANNOTATION = "dev.brunner/result-manifest-size"
RESUME_ARCHIVE_MARKER = "resume-archive.json"
PREPARATION_MARKER_GRACE_SECONDS = 10
CAMPAIGN_SHA256_ANNOTATION = "dev.brunner/campaign-sha256"
CAMPAIGN_IMAGE_OVERRIDES_ENV = "BRUNNER_CAMPAIGN_IMAGE_OVERRIDES"
EVALUATION_IMAGE_OVERRIDE_ENV = "BRUNNER_EVALUATION_IMAGE_OVERRIDE"
TERMINAL_CAMPAIGN_STATES = frozenset({"complete", "attention_required"})
TERMINAL_CONTAINER_REASONS = frozenset(
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


class ControllerLockLost(BaseException):
    """The active controller may no longer mutate campaign resources."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "campaign"


def _immutable_image(image: str) -> bool:
    return re.search(r"@sha256:[0-9a-fA-F]{64}$", image) is not None


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _secret_environment(
    value: dict[str, dict[str, tuple[str, str]]],
) -> dict[str, dict[str, list[str]]]:
    return {
        provider: {
            name: list(reference)
            for name, reference in sorted(environment.items())
        }
        for provider, environment in sorted(value.items())
    }


@dataclass(frozen=True)
class ControllerProfile:
    image: str
    namespace: str = "bizon"
    control_storage_size: str = "20Gi"
    results_storage_size: str = "100Gi"
    storage_class_name: str | None = None
    resource_cache_claim_name: str | None = None
    image_pull_secrets: tuple[str, ...] = ()
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: tuple[dict[str, Any], ...] = ()
    poll_seconds: float = 5
    preparation_timeout_seconds: float = 60 * 60
    lock_duration_seconds: int = 60
    command_timeout_seconds: float = 120
    retrieval_chunk_bytes: int = 4 * 1024 * 1024
    max_published_trial_bytes: int | None = 10 * 1024 * 1024 * 1024
    controller_cpu_request: str = "250m"
    controller_cpu_limit: str = "2"
    controller_memory_request: str = "512Mi"
    controller_memory_limit: str = "4Gi"
    preparation_cpu_request: str = "250m"
    preparation_cpu_limit: str = "2"
    preparation_memory_request: str = "512Mi"
    preparation_memory_limit: str = "4Gi"
    assessment_cpu_request: str = "1"
    assessment_cpu_limit: str = "4"
    assessment_memory_request: str = "2Gi"
    assessment_memory_limit: str = "8Gi"
    reviewer_secret_environment: dict[
        str,
        dict[str, tuple[str, str]],
    ] = field(default_factory=dict)
    require_image_digest: bool = True

    def validate(self) -> None:
        if not self.image.strip():
            raise ValueError("controller image cannot be empty")
        if self.require_image_digest and not _immutable_image(self.image):
            raise ValueError(
                "controller image must use image@sha256:<digest>"
            )
        if not self.namespace.strip():
            raise ValueError("controller namespace cannot be empty")
        if (
            self.resource_cache_claim_name is not None
            and not self.resource_cache_claim_name.strip()
        ):
            raise ValueError(
                "controller resource_cache_claim_name cannot be empty"
            )
        if self.poll_seconds <= 0:
            raise ValueError("controller poll_seconds must be positive")
        if self.preparation_timeout_seconds <= 0:
            raise ValueError(
                "controller preparation_timeout_seconds must be positive"
            )
        if self.lock_duration_seconds < 15:
            raise ValueError(
                "controller lock_duration_seconds must be at least 15"
            )
        if self.command_timeout_seconds <= 0:
            raise ValueError(
                "controller command_timeout_seconds must be positive"
            )
        if self.retrieval_chunk_bytes < 1:
            raise ValueError(
                "controller retrieval_chunk_bytes must be positive"
            )
        if (
            self.max_published_trial_bytes is not None
            and self.max_published_trial_bytes < 1
        ):
            raise ValueError(
                "controller max_published_trial_bytes must be positive or None"
            )
        for name, value in (
            ("preparation_cpu_request", self.preparation_cpu_request),
            ("preparation_cpu_limit", self.preparation_cpu_limit),
            ("preparation_memory_request", self.preparation_memory_request),
            ("preparation_memory_limit", self.preparation_memory_limit),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"controller {name} cannot be empty")
        for provider, environment in (
            self.reviewer_secret_environment.items()
        ):
            if not provider.strip():
                raise ValueError("reviewer provider cannot be empty")
            for name, reference in environment.items():
                if (
                    not name
                    or not isinstance(reference, tuple)
                    or len(reference) != 2
                    or any(not item for item in reference)
                ):
                    raise ValueError(
                        "reviewer Secret mappings must be "
                        "ENV=(secret_name, secret_key)"
                    )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["image_pull_secrets"] = list(self.image_pull_secrets)
        value["tolerations"] = list(self.tolerations)
        value["reviewer_secret_environment"] = _secret_environment(
            self.reviewer_secret_environment
        )
        return value


@dataclass(frozen=True)
class ClusterCampaign:
    plan: CampaignPlan
    backend: KubernetesProfile
    controller: ControllerProfile

    def validate(self) -> None:
        self.plan.validate()
        self.controller.validate()
        if self.backend.namespace != self.controller.namespace:
            raise ValueError(
                "backend and controller must use the same Kubernetes namespace"
            )
        if self.backend.service_account_name is not None:
            raise ValueError(
                "backend service_account_name is controller-owned and must "
                "not be configured by a benchmark"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        backend = asdict(self.backend)
        backend["image_pull_secrets"] = list(
            self.backend.image_pull_secrets
        )
        backend["tolerations"] = list(self.backend.tolerations)
        backend["secret_environment"] = {
            name: list(reference)
            for name, reference in sorted(
                self.backend.secret_environment.items()
            )
        }
        return {
            "plan": self.plan.to_dict(),
            "backend": backend,
            "controller": self.controller.to_dict(),
        }

    @property
    def sha256(self) -> str:
        return _json_sha256(self.to_dict())


def campaign_image_overrides(
    campaign: ClusterCampaign,
) -> dict[str, str | None]:
    return {
        "plan_backend_image": campaign.plan.backend_image,
        "backend_agent_image": campaign.backend.agent_image,
        "backend_artifact_reader_image": (
            campaign.backend.artifact_reader_image
        ),
        "backend_proxy_image": campaign.backend.proxy_image,
        "controller_image": campaign.controller.image,
    }


def campaign_image_environment(
    campaign: ClusterCampaign,
) -> dict[str, str]:
    return {
        CAMPAIGN_IMAGE_OVERRIDES_ENV: json.dumps(
            campaign_image_overrides(campaign),
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def definition_image_environment(
    definition: BenchmarkDefinition,
) -> dict[str, str]:
    return {
        EVALUATION_IMAGE_OVERRIDE_ENV: definition.evaluation.image,
    }


def apply_definition_image_override(
    definition: BenchmarkDefinition,
    environment: dict[str, str] | None = None,
) -> BenchmarkDefinition:
    if environment is None:
        environment = dict(os.environ)
    image = environment.get(EVALUATION_IMAGE_OVERRIDE_ENV)
    if image is None:
        return definition
    if not image.strip():
        raise ValueError(
            f"{EVALUATION_IMAGE_OVERRIDE_ENV} cannot be empty"
        )
    return replace(
        definition,
        evaluation=replace(definition.evaluation, image=image),
    )


def apply_campaign_image_overrides(
    campaign: ClusterCampaign,
    environment: dict[str, str] | None = None,
) -> ClusterCampaign:
    if environment is None:
        environment = dict(os.environ)
    raw = environment.get(CAMPAIGN_IMAGE_OVERRIDES_ENV)
    if raw is None:
        return campaign
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{CAMPAIGN_IMAGE_OVERRIDES_ENV} is not valid JSON"
        ) from error
    expected = set(campaign_image_overrides(campaign))
    if not isinstance(overrides, dict) or set(overrides) != expected:
        raise ValueError(
            f"{CAMPAIGN_IMAGE_OVERRIDES_ENV} must contain exactly "
            f"{sorted(expected)}"
        )
    for name, value in overrides.items():
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ValueError(
                f"{CAMPAIGN_IMAGE_OVERRIDES_ENV}.{name} must be a "
                "non-empty string or null"
            )
    return replace(
        campaign,
        plan=replace(
            campaign.plan,
            backend_image=overrides["plan_backend_image"],
        ),
        backend=replace(
            campaign.backend,
            agent_image=overrides["backend_agent_image"],
            artifact_reader_image=overrides[
                "backend_artifact_reader_image"
            ],
            proxy_image=overrides["backend_proxy_image"],
        ),
        controller=replace(
            campaign.controller,
            image=overrides["controller_image"],
        ),
    )


@dataclass(frozen=True)
class CampaignResources:
    base: str
    namespace: str

    @property
    def service_account(self) -> str:
        return f"{self.base}-controller"

    @property
    def role(self) -> str:
        return f"{self.base}-controller"

    @property
    def lock_config_map(self) -> str:
        return f"{self.base}-lock"

    @property
    def status_config_map(self) -> str:
        return f"{self.base}-status"

    @property
    def control_claim(self) -> str:
        return f"{self.base}-control"

    @property
    def results_claim(self) -> str:
        return f"{self.base}-results"

    @property
    def preparation_job(self) -> str:
        return f"{self.base}-prepare"

    @property
    def deployment(self) -> str:
        return f"{self.base}-controller"

    @property
    def assessment_policy(self) -> str:
        return f"{self.base}-assessment"

    @property
    def proxy(self) -> str:
        return f"{self.base}-proxy"


def campaign_resources(
    definition: BenchmarkDefinition,
    campaign: ClusterCampaign,
) -> CampaignResources:
    campaign.validate()
    return CampaignResources(
        base=campaign_resource_name(
            definition.benchmark_id,
            campaign.plan.campaign_id,
        ),
        namespace=campaign.controller.namespace,
    )


class Kubectl:
    def __init__(
        self,
        namespace: str,
        *,
        executable: str = "kubectl",
        timeout_seconds: float = 120,
    ) -> None:
        self.namespace = namespace
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def run_bytes(
        self,
        *arguments: str,
        input_bytes: bytes | None = None,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = (self.executable, *arguments)
        try:
            result = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=timeout_seconds or self.timeout_seconds,
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
            message = (result.stderr or result.stdout).decode(
                errors="replace"
            ).strip()
            lowered = message.lower()
            error_type = (
                BackendConnectivityError
                if any(
                    fragment in lowered
                    for fragment in (
                        "connection refused",
                        "connection reset",
                        "context deadline exceeded",
                        "i/o timeout",
                        "no route to host",
                        "no such host",
                        "service unavailable",
                        "tls handshake timeout",
                        "unable to connect",
                        "unexpected eof",
                    )
                )
                else BackendRequestError
            )
            raise error_type(
                f"{self.executable} {' '.join(arguments)} exited "
                f"{result.returncode}: {message}"
            )
        return result

    def run(
        self,
        *arguments: str,
        input_value: str | None = None,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self.run_bytes(
            *arguments,
            input_bytes=(
                input_value.encode() if input_value is not None else None
            ),
            check=check,
            timeout_seconds=timeout_seconds,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode(errors="replace"),
            result.stderr.decode(errors="replace"),
        )

    def apply(self, resource: dict[str, Any]) -> None:
        self.run(
            "apply",
            "-f",
            "-",
            input_value=json.dumps(resource),
        )

    def replace(self, resource: dict[str, Any]) -> bool:
        result = self.run(
            "replace",
            "-f",
            "-",
            input_value=json.dumps(resource),
            check=False,
        )
        return result.returncode == 0

    def create(self, resource: dict[str, Any]) -> bool:
        result = self.run(
            "create",
            "-f",
            "-",
            input_value=json.dumps(resource),
            check=False,
        )
        return result.returncode == 0

    def get(
        self,
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
    ) -> dict[str, Any] | None:
        arguments = ["get", kind]
        if name:
            arguments.append(name)
        arguments.extend(("-n", self.namespace))
        if labels:
            arguments.extend(("-l", labels))
        arguments.extend(("-o", "json"))
        result = self.run(*arguments, check=False)
        if result.returncode:
            lowered = (result.stderr or result.stdout).lower()
            if "not found" in lowered or "notfound" in lowered:
                return None
            raise BackendRequestError(
                f"kubectl {' '.join(arguments)} exited "
                f"{result.returncode}: {result.stderr or result.stdout}"
            )
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise BackendRequestError("kubectl returned non-object JSON")
        return value

    def delete(
        self,
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
        wait: bool = False,
    ) -> None:
        arguments = ["delete", kind]
        if name:
            arguments.append(name)
        if labels:
            arguments.extend(("-l", labels))
        arguments.extend(
            (
                "-n",
                self.namespace,
                "--ignore-not-found=true",
                f"--wait={'true' if wait else 'false'}",
            )
        )
        self.run(*arguments)


def _labels(resources: CampaignResources) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "brunner",
        "dev.brunner/campaign": resources.base,
    }


def _pvc(
    resources: CampaignResources,
    *,
    name: str,
    size: str,
    storage_class_name: str | None,
    role: str,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteMany"],
        "resources": {"requests": {"storage": size}},
    }
    if storage_class_name is not None:
        spec["storageClassName"] = storage_class_name
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": resources.namespace,
            "labels": {**_labels(resources), "dev.brunner/role": role},
        },
        "spec": spec,
    }


def _pod_placement(
    profile: ControllerProfile,
    spec: dict[str, Any],
) -> None:
    if profile.image_pull_secrets:
        spec["imagePullSecrets"] = [
            {"name": name} for name in profile.image_pull_secrets
        ]
    if profile.node_selector:
        spec["nodeSelector"] = dict(profile.node_selector)
    if profile.tolerations:
        spec["tolerations"] = list(profile.tolerations)


def _security_context() -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }


def _pod_security_context() -> dict[str, Any]:
    return {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _controller_command(
    subcommand: str,
    *,
    benchmark_ref: str,
    campaign_ref: str,
    campaign_sha256: str,
) -> list[str]:
    return [
        "brunner",
        "--benchmark",
        benchmark_ref,
        subcommand,
        campaign_ref,
        "--campaign-sha256",
        campaign_sha256,
    ]


def render_cluster_resources(
    definition: BenchmarkDefinition,
    campaign: ClusterCampaign,
    *,
    benchmark_ref: str,
    campaign_ref: str,
) -> tuple[dict[str, Any], ...]:
    campaign.validate()
    resources = campaign_resources(definition, campaign)
    profile = campaign.controller
    labels = _labels(resources)
    pod_labels = {**labels, "dev.brunner/role": "controller"}
    role_rules = [
        {
            "apiGroups": [""],
            "resources": [
                "configmaps",
                "events",
                "persistentvolumeclaims",
                "pods",
                "pods/log",
                "services",
            ],
            "verbs": ["create", "delete", "get", "list", "patch", "update"],
        },
        {
            "apiGroups": [""],
            "resources": ["resourcequotas"],
            "verbs": ["get", "list"],
        },
        {
            "apiGroups": [""],
            "resources": ["pods/exec"],
            "verbs": ["create"],
        },
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["create", "delete", "get", "list", "patch", "update"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "verbs": ["create", "delete", "get", "list", "patch", "update"],
        },
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "verbs": ["create", "delete", "get", "list", "patch", "update"],
        },
    ]
    volumes = [
        {
            "name": "control",
            "persistentVolumeClaim": {
                "claimName": resources.control_claim,
            },
        },
        {
            "name": "results",
            "persistentVolumeClaim": {
                "claimName": resources.results_claim,
            },
        },
        {"name": "tmp", "emptyDir": {}},
    ]
    mounts = [
        {"name": "control", "mountPath": str(CONTROL_ROOT)},
        {"name": "results", "mountPath": str(RESULTS_ROOT)},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    prepare_volumes = list(volumes)
    prepare_mounts = list(mounts)
    runtime_environment = {
        **definition_image_environment(definition),
        **campaign_image_environment(campaign),
    }
    campaign_environment = [
        {"name": name, "value": value}
        for name, value in runtime_environment.items()
    ]
    prepare_environment = list(campaign_environment)
    if profile.resource_cache_claim_name is not None:
        prepare_volumes.append(
            {
                "name": "resource-cache",
                "persistentVolumeClaim": {
                    "claimName": profile.resource_cache_claim_name,
                },
            }
        )
        prepare_mounts.append(
            {
                "name": "resource-cache",
                "mountPath": "/brunner/resource-cache",
            }
        )
        prepare_environment.append(
            {
                "name": "BRUNNER_RESOURCE_CACHE",
                "value": "/brunner/resource-cache",
            }
        )
    prepare_pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "securityContext": _pod_security_context(),
        "containers": [
            {
                "name": "prepare",
                "image": profile.image,
                "command": _controller_command(
                    "controller-prepare",
                    benchmark_ref=benchmark_ref,
                    campaign_ref=campaign_ref,
                    campaign_sha256=campaign.sha256,
                ),
                "workingDir": "/tmp",
                "env": prepare_environment,
                "resources": {
                    "requests": {
                        "cpu": profile.preparation_cpu_request,
                        "memory": profile.preparation_memory_request,
                    },
                    "limits": {
                        "cpu": profile.preparation_cpu_limit,
                        "memory": profile.preparation_memory_limit,
                    },
                },
                "securityContext": _security_context(),
                "volumeMounts": prepare_mounts,
            }
        ],
        "volumes": prepare_volumes,
    }
    _pod_placement(profile, prepare_pod_spec)
    controller_container = {
        "name": "controller",
        "image": profile.image,
        "command": _controller_command(
            "controller-run",
            benchmark_ref=benchmark_ref,
            campaign_ref=campaign_ref,
            campaign_sha256=campaign.sha256,
        ),
        "workingDir": "/tmp",
        "env": [
            *campaign_environment,
            {
                "name": "BRUNNER_CONTROLLER_POD_NAME",
                "valueFrom": {
                    "fieldRef": {"fieldPath": "metadata.name"}
                },
            },
            {
                "name": "BRUNNER_CONTROLLER_POD_UID",
                "valueFrom": {
                    "fieldRef": {"fieldPath": "metadata.uid"}
                },
            },
        ],
        "resources": {
            "requests": {
                "cpu": profile.controller_cpu_request,
                "memory": profile.controller_memory_request,
            },
            "limits": {
                "cpu": profile.controller_cpu_limit,
                "memory": profile.controller_memory_limit,
            },
        },
        "securityContext": _security_context(),
        "volumeMounts": mounts,
    }
    controller_pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": True,
        "enableServiceLinks": False,
        "serviceAccountName": resources.service_account,
        "securityContext": _pod_security_context(),
        "terminationGracePeriodSeconds": 30,
        "containers": [controller_container],
        "volumes": volumes,
    }
    _pod_placement(profile, controller_pod_spec)
    return (
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": resources.service_account,
                "namespace": resources.namespace,
                "labels": labels,
            },
            "automountServiceAccountToken": True,
            "imagePullSecrets": [
                {"name": name} for name in profile.image_pull_secrets
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {
                "name": resources.role,
                "namespace": resources.namespace,
                "labels": labels,
            },
            "rules": role_rules,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": resources.role,
                "namespace": resources.namespace,
                "labels": labels,
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": resources.service_account,
                    "namespace": resources.namespace,
                }
            ],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": resources.role,
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": resources.status_config_map,
                "namespace": resources.namespace,
                "labels": labels,
            },
            "data": {
                "status.json": json.dumps(
                    {
                        "schema_version": "1.0",
                        "campaign_id": campaign.plan.campaign_id,
                        "campaign_sha256": campaign.sha256,
                        "status": "submitted",
                        "updated_at": _now(),
                    },
                    sort_keys=True,
                )
            },
        },
        _pvc(
            resources,
            name=resources.control_claim,
            size=profile.control_storage_size,
            storage_class_name=profile.storage_class_name,
            role="control",
        ),
        _pvc(
            resources,
            name=resources.results_claim,
            size=profile.results_storage_size,
            storage_class_name=profile.storage_class_name,
            role="results",
        ),
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": resources.preparation_job,
                "namespace": resources.namespace,
                "labels": {**labels, "dev.brunner/role": "preparer"},
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": math.ceil(
                    profile.preparation_timeout_seconds
                ),
                "template": {
                    "metadata": {
                        "labels": {
                            **labels,
                            "dev.brunner/role": "preparer",
                        }
                    },
                    "spec": prepare_pod_spec,
                },
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": resources.deployment,
                "namespace": resources.namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": pod_labels},
                "template": {
                    "metadata": {
                        "labels": pod_labels,
                        "annotations": {
                            "dev.brunner/campaign-sha256": campaign.sha256,
                        },
                    },
                    "spec": controller_pod_spec,
                },
            },
        },
    )


class ConfigMapLock:
    def __init__(
        self,
        client: Kubectl,
        resources: CampaignResources,
        *,
        holder: str,
        duration_seconds: int,
    ) -> None:
        self.client = client
        self.resources = resources
        self.holder = holder
        self.duration_seconds = duration_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._last_success = 0.0
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @staticmethod
    def _expired(state: dict[str, Any], now: datetime) -> bool:
        renew = state.get("renewed_at") or state.get("acquired_at")
        if not isinstance(renew, str):
            return True
        try:
            renewed_at = datetime.fromisoformat(renew.replace("Z", "+00:00"))
        except ValueError:
            return True
        if renewed_at.tzinfo is None:
            renewed_at = renewed_at.replace(tzinfo=UTC)
        duration = int(state.get("duration_seconds") or 0)
        return (now - renewed_at).total_seconds() > duration

    @staticmethod
    def _state(resource: dict[str, Any]) -> dict[str, Any]:
        raw = resource.get("data", {}).get("lock.json")
        if not isinstance(raw, str):
            raise IntegrityError(
                "controller lock ConfigMap does not contain lock.json"
            )
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as error:
            raise IntegrityError(
                "controller lock ConfigMap contains invalid lock.json"
            ) from error
        if not isinstance(state, dict):
            raise IntegrityError(
                "controller lock ConfigMap lock.json must be an object"
            )
        return state

    def _resource(
        self,
        state: dict[str, Any],
        *,
        resource_version: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "name": self.resources.lock_config_map,
            "namespace": self.resources.namespace,
            "labels": {
                **_labels(self.resources),
                "dev.brunner/role": "controller-lock",
            },
        }
        if resource_version is not None:
            metadata["resourceVersion"] = resource_version
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {
                "lock.json": json.dumps(
                    state,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        }

    def _try_acquire_or_renew(self) -> bool:
        lock = self.client.get(
            "configmap",
            self.resources.lock_config_map,
        )
        now = datetime.now(UTC)
        if lock is None:
            return self.client.create(
                self._resource(
                    {
                        "holder": self.holder,
                        "acquired_at": now.isoformat(),
                        "renewed_at": now.isoformat(),
                        "duration_seconds": self.duration_seconds,
                        "transitions": 0,
                    }
                )
            )
        metadata = dict(lock.get("metadata", {}))
        state = self._state(lock)
        current = state.get("holder")
        if current not in {None, "", self.holder} and not self._expired(
            state,
            now,
        ):
            return False
        transitions = int(state.get("transitions") or 0)
        if current not in {None, "", self.holder}:
            transitions += 1
        state.update(
            {
                "holder": self.holder,
                "renewed_at": now.isoformat(),
                "duration_seconds": self.duration_seconds,
                "transitions": transitions,
            }
        )
        if current != self.holder:
            state["acquired_at"] = now.isoformat()
        resource_version = metadata.get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            raise IntegrityError(
                "controller lock ConfigMap has no resourceVersion"
            )
        return self.client.replace(
            self._resource(
                state,
                resource_version=resource_version,
            )
        )

    def acquire(self) -> None:
        while not self._try_acquire_or_renew():
            time.sleep(min(5, self.duration_seconds / 3))
        self._last_success = time.monotonic()
        self._thread = threading.Thread(
            target=self._renew_loop,
            name="brunner-controller-lock",
            daemon=True,
        )
        self._thread.start()

    def _renew_loop(self) -> None:
        interval = max(2, self.duration_seconds / 3)
        while not self._stop.wait(interval):
            try:
                renewed = self._try_acquire_or_renew()
            except Exception as error:
                renewed = False
                self.last_error = str(error)
            if renewed:
                self._last_success = time.monotonic()
                self.last_error = None
            elif (
                time.monotonic() - self._last_success
                > self.duration_seconds
            ):
                self._lost.set()
                return

    def assert_held(self) -> None:
        if self._lost.is_set():
            raise ControllerLockLost(
                "controller lost its Kubernetes ConfigMap lock"
                + (f": {self.last_error}" if self.last_error else "")
            )

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _status_resource(
    resources: CampaignResources,
    status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": resources.status_config_map,
            "namespace": resources.namespace,
            "labels": _labels(resources),
        },
        "data": {
            "status.json": json.dumps(status, sort_keys=True),
        },
    }


def _campaign_status(
    campaign: ClusterCampaign,
    state: dict[str, Any],
    *,
    result_ready: bool,
    manifest_sha256: str | None = None,
    manifest_size: int | None = None,
) -> dict[str, Any]:
    phases: dict[str, int] = {}
    for trial in state.get("trials", ()):
        if not isinstance(trial, dict):
            continue
        phase = str(trial.get("phase") or "unknown")
        phases[phase] = phases.get(phase, 0) + 1
    return {
        "schema_version": "1.0",
        "campaign_id": campaign.plan.campaign_id,
        "campaign_sha256": campaign.sha256,
        "status": state.get("status"),
        "has_attention": bool(state.get("has_attention")),
        "trial_phases": phases,
        "result_ready": result_ready,
        "result_manifest_sha256": manifest_sha256,
        "result_manifest_size": manifest_size,
        "controller_pod": os.environ.get("BRUNNER_CONTROLLER_POD_NAME"),
        "updated_at": _now(),
    }


def _result_inventory(
    results_root: Path,
    *,
    fence: Callable[[], None],
) -> list[dict[str, Any]]:
    files = []
    for path in sorted(results_root.rglob("*")):
        fence()
        if path.is_symlink():
            raise IntegrityError(
                f"result bundle contains a symlink: {path}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(results_root).as_posix()
        if relative == RESULT_MANIFEST:
            continue
        size = path.stat().st_size
        digest = sha256_file(path)
        fence()
        files.append(
            {
                "path": relative,
                "size": size,
                "sha256": digest,
            }
        )
    return files


def _result_state_sha256(state: dict[str, Any]) -> str:
    stable = dict(state)
    stable.pop("updated_at", None)
    return _json_sha256(stable)


def publish_trial_results(
    source: Path,
    destination: Path,
    *,
    max_bytes: int | None,
    fence: Callable[[], None] | None = None,
) -> Path:
    fence = fence or (lambda: None)
    fence()
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or source.is_symlink():
        raise IntegrityError(f"collected trial is unsafe: {source}")
    collection_inventory_path = source.with_name(
        source.name + "-inventory.json"
    )
    collection_inventory = (
        json.loads(collection_inventory_path.read_text())
        if collection_inventory_path.is_file()
        else {}
    )
    marker = source / "workspace/.brunner-challenge.json"
    baseline = {}
    if marker.is_file() and not marker.is_symlink():
        value = json.loads(marker.read_text())
        if isinstance(value.get("file_inventory"), dict):
            baseline = {
                f"workspace/{relative}": metadata
                for relative, metadata in value["file_inventory"].items()
            }

    selected: dict[str, dict[str, Any]] = {}
    for path in sorted(source.rglob("*")):
        fence()
        if path.is_symlink():
            raise IntegrityError(
                f"published result contains a symlink: {path}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        relative_parts = Path(relative).parts
        if (
            len(relative_parts) >= 3
            and relative_parts[0] == "assessments"
            and relative_parts[2]
            in {"workspace", ".reviewer-provider-home"}
        ):
            continue
        if (
            relative in baseline
            and collection_inventory.get(relative) == baseline[relative]
        ):
            continue
        metadata = artifact_metadata(path)
        if metadata is not None:
            selected[relative] = metadata.to_dict()
    total = sum(
        int(metadata["size"])
        for metadata in selected.values()
        if metadata.get("type") == "file"
    )
    if max_bytes is not None and total > max_bytes:
        raise IntegrityError(
            "published trial result is "
            f"{total} bytes, exceeding the configured {max_bytes}-byte limit"
        )

    partial = destination.with_name(destination.name + ".partial")
    fence()
    partial.mkdir(parents=True, exist_ok=True)
    fence()
    for relative, expected in selected.items():
        fence()
        source_path = source / relative
        target = partial / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        expected_size = int(expected["size"])
        if target.is_file():
            observed = artifact_metadata(target)
            if observed is not None and observed.to_dict() == expected:
                continue
            if target.stat().st_size > expected_size:
                target.unlink()
        offset = target.stat().st_size if target.is_file() else 0
        with source_path.open("rb") as input_stream, target.open(
            "ab" if offset else "wb"
        ) as output_stream:
            input_stream.seek(offset)
            while offset < expected_size:
                fence()
                data = input_stream.read(
                    min(4 * 1024 * 1024, expected_size - offset)
                )
                if not data:
                    raise IntegrityError(
                        f"published result ended early: {relative}"
                    )
                output_stream.write(data)
                output_stream.flush()
                offset += len(data)
        observed = artifact_metadata(target)
        if observed is None or observed.to_dict() != expected:
            target.unlink(missing_ok=True)
            raise IntegrityError(
                f"published result checksum mismatch: {relative}"
            )
    for path in sorted(partial.rglob("*"), reverse=True):
        fence()
        if not path.is_file():
            continue
        relative = path.relative_to(partial).as_posix()
        if relative == "publication.json":
            continue
        if relative not in selected:
            path.unlink()
    fence()
    write_json_atomic(
        partial / "publication.json",
        {
            "schema_version": "1.0",
            "files": selected,
            "total_bytes": total,
            "published_at": _now(),
        },
    )
    fence()
    if destination.exists():
        fence()
        shutil.rmtree(destination)
        fence()
    fence()
    partial.replace(destination)
    fence()
    return destination


def finalize_result_bundle(
    results_root: Path,
    state: dict[str, Any],
    campaign: ClusterCampaign,
    *,
    fence: Callable[[], None] | None = None,
) -> dict[str, Any]:
    from brunner.dashboard import write_campaign_dashboard

    fence = fence or (lambda: None)
    fence()
    results_root.mkdir(parents=True, exist_ok=True)
    fence()
    write_json_atomic(results_root / "campaign.json", state)
    fence()
    write_campaign_dashboard(state, results_root / "index.html")
    fence()
    manifest = {
        "schema_version": "2.0",
        "campaign_id": campaign.plan.campaign_id,
        "benchmark_id": state.get("benchmark_id"),
        "benchmark_version": state.get("benchmark_version"),
        "contract_sha256": state.get("contract_sha256"),
        "evaluation_sha256": state.get("evaluation_sha256"),
        "challenge_sha256": state.get("challenge_sha256"),
        "campaign_sha256": campaign.sha256,
        "state_sha256": _result_state_sha256(state),
        "terminal": state.get("status") in TERMINAL_CAMPAIGN_STATES,
        "created_at": _now(),
        "files": _result_inventory(results_root, fence=fence),
    }
    manifest_path = results_root / RESULT_MANIFEST
    fence()
    write_json_atomic(manifest_path, manifest)
    fence()
    digest = sha256_file(manifest_path)
    size = manifest_path.stat().st_size
    fence()
    return {
        "manifest": manifest,
        "sha256": digest,
        "size": size,
    }


def _assessment_providers(
    definition: BenchmarkDefinition,
) -> frozenset[str]:
    return frozenset(
        assessment.reviewer.provider
        for assessment in definition.resolved_assessments()
        if assessment.reviewer is not None
    )


def _merge_assessment_output(
    source: Path,
    destination: Path,
    *,
    fence: Callable[[], None],
) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or source.is_symlink():
        raise IntegrityError(f"assessment output root is unsafe: {source}")
    if not destination.is_dir() or destination.is_symlink():
        raise IntegrityError(
            f"collected trial root is unsafe: {destination}"
        )
    for path in sorted(source.rglob("*")):
        fence()
        if path.is_symlink():
            raise IntegrityError(
                f"assessment output contains a symlink: {path}"
            )
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            fence()
            continue
        if not path.is_file():
            raise IntegrityError(
                f"assessment output contains an unsupported entry: {path}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".assessment-part")
        shutil.copy2(path, temporary)
        expected = artifact_metadata(path)
        observed = artifact_metadata(temporary)
        if (
            expected is None
            or observed is None
            or observed.to_dict() != expected.to_dict()
        ):
            temporary.unlink(missing_ok=True)
            raise IntegrityError(
                f"assessment output copy failed integrity check: {relative}"
            )
        temporary.replace(target)
        fence()


class KubernetesEvaluationFinalizer:
    def __init__(
        self,
        *,
        definition: BenchmarkDefinition,
        campaign: ClusterCampaign,
        resources: CampaignResources,
        client: Kubectl,
        benchmark_ref: str,
        campaign_ref: str,
        proxy_url: str | None,
        proxy_labels: dict[str, str],
        fence: Callable[[], None] | None = None,
    ) -> None:
        self.definition = definition
        self.campaign = campaign
        self.resources = resources
        self.client = client
        self.benchmark_ref = benchmark_ref
        self.campaign_ref = campaign_ref
        self.proxy_url = proxy_url
        self.proxy_labels = dict(proxy_labels)
        self.fence = fence or (lambda: None)

    def _job_name(self, trial: Path) -> str:
        identity = (
            f"{self.resources.base}\0{self.campaign.sha256}\0{trial.name}"
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
        prefix = _safe_slug(trial.name)[:28].rstrip("-")
        return f"brunner-assess-{prefix}-{digest}"

    def _network_policy(
        self,
        job_name: str,
        labels: dict[str, str],
    ) -> dict[str, Any]:
        egress = []
        if _assessment_providers(self.definition):
            if self.proxy_url is None:
                raise BackendRequestError(
                    "model assessments require the managed proxy"
                )
            egress.append(
                {
                    "to": [
                        {
                            "podSelector": {
                                "matchLabels": dict(self.proxy_labels)
                            }
                        }
                    ],
                    "ports": [
                        {
                            "protocol": "TCP",
                            "port": MANAGED_PROXY_PORT,
                        }
                    ],
                }
            )
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": job_name,
                "namespace": self.resources.namespace,
                "labels": labels,
            },
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": egress,
            },
        }

    def _output_relative(self, job_name: str) -> str:
        return f"assessment-output/{job_name}"

    def _job(self, trial: Path, job_name: str) -> dict[str, Any]:
        providers = _assessment_providers(self.definition)
        labels = {
            **_labels(self.resources),
            "dev.brunner/role": "assessment",
            "dev.brunner/assessment-job": job_name,
        }
        runtime_environment = {
            **definition_image_environment(self.definition),
            **campaign_image_environment(self.campaign),
        }
        environment: list[dict[str, Any]] = [
            {"name": name, "value": value}
            for name, value in runtime_environment.items()
        ]
        if providers:
            no_proxy = "localhost,127.0.0.1,::1"
            environment.extend(
                {
                    "name": name,
                    "value": value,
                }
                for name, value in (
                    ("HTTP_PROXY", str(self.proxy_url)),
                    ("HTTPS_PROXY", str(self.proxy_url)),
                    ("NO_PROXY", no_proxy),
                    ("http_proxy", str(self.proxy_url)),
                    ("https_proxy", str(self.proxy_url)),
                    ("no_proxy", no_proxy),
                )
            )
        for provider in sorted(providers):
            mappings = self.campaign.controller.reviewer_secret_environment.get(
                provider,
                {},
            )
            if not mappings:
                raise BackendRequestError(
                    f"no reviewer Secret mappings configured for {provider}"
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
                for name, reference in sorted(mappings.items())
            )
        relative = trial.relative_to(CONTROL_ROOT).as_posix()
        output_relative = self._output_relative(job_name)
        timeout = max(
            300,
            math.ceil(
                sum(
                    assessment.timeout_seconds
                    for assessment in self.definition.resolved_assessments()
                )
                + 300
            ),
        )
        pod_spec: dict[str, Any] = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "securityContext": _pod_security_context(),
            "containers": [
                {
                    "name": "assessment",
                    "image": self.campaign.controller.image,
                    "command": [
                        "brunner",
                        "--benchmark",
                        self.benchmark_ref,
                        "controller-finalize",
                        self.campaign_ref,
                        "--campaign-sha256",
                        self.campaign.sha256,
                        "--trial-relative",
                        relative,
                        "--output-relative",
                        output_relative,
                    ],
                    "workingDir": "/tmp",
                    "env": environment,
                    "resources": {
                        "requests": {
                            "cpu": (
                                self.campaign.controller
                                .assessment_cpu_request
                            ),
                            "memory": (
                                self.campaign.controller
                                .assessment_memory_request
                            ),
                        },
                        "limits": {
                            "cpu": (
                                self.campaign.controller
                                .assessment_cpu_limit
                            ),
                            "memory": (
                                self.campaign.controller
                                .assessment_memory_limit
                            ),
                        },
                    },
                    "securityContext": _security_context(),
                    "volumeMounts": [
                        {
                            "name": "control",
                            "mountPath": str(trial),
                            "subPath": relative,
                            "readOnly": True,
                        },
                        {
                            "name": "control",
                            "mountPath": str(
                                CONTROL_ROOT / output_relative
                            ),
                            "subPath": output_relative,
                        },
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "control",
                    "persistentVolumeClaim": {
                        "claimName": self.resources.control_claim,
                    },
                },
                {"name": "tmp", "emptyDir": {}},
            ],
        }
        _pod_placement(self.campaign.controller, pod_spec)
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.resources.namespace,
                "labels": labels,
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": timeout,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": pod_spec,
                },
            },
        }

    def __call__(self, trial: Path) -> dict[str, Any]:
        from brunner.evaluation import _validate_evaluation_result
        from brunner.report import write_run_report

        job_name = self._job_name(trial)
        job = self._job(trial, job_name)
        labels = dict(job["metadata"]["labels"])
        self.fence()
        self.client.apply(self._network_policy(job_name, labels))
        self.fence()
        existing = self.client.get("job", job_name)
        self.fence()
        if existing is None:
            output_root = CONTROL_ROOT / self._output_relative(job_name)
            if output_root.exists():
                shutil.rmtree(output_root)
            output_root.mkdir(parents=True)
            self.fence()
            self.client.apply(job)
            self.fence()
            raise EvaluationPending(
                f"assessment Job submitted: {job_name}"
            )
        conditions = {
            item.get("type"): item
            for item in existing.get("status", {}).get("conditions", ())
            if isinstance(item, dict)
        }
        if conditions.get("Failed", {}).get("status") == "True":
            failed = conditions["Failed"]
            raise BackendRequestError(
                "assessment Job failed: "
                + str(
                    failed.get("message")
                    or failed.get("reason")
                    or job_name
                )
            )
        if conditions.get("Complete", {}).get("status") != "True":
            pods = self.client.get(
                "pods",
                labels=f"job-name={job_name}",
            ) or {"items": []}
            self.fence()
            for pod in pods.get("items", ()):
                for status in pod.get("status", {}).get(
                    "containerStatuses",
                    (),
                ):
                    waiting = status.get("state", {}).get("waiting", {})
                    reason = waiting.get("reason")
                    if reason in TERMINAL_CONTAINER_REASONS:
                        raise BackendRequestError(
                            f"assessment container cannot start: {reason}: "
                            f"{waiting.get('message') or ''}"
                        )
            raise EvaluationPending(
                f"assessment Job is still running: {job_name}"
            )
        output_root = CONTROL_ROOT / self._output_relative(job_name)
        output_results = (
            output_root / self.definition.evaluation.results_path
        )
        if not output_results.is_file():
            raise IntegrityError(
                "assessment Job completed without output results: "
                f"{output_results}"
            )
        _validate_evaluation_result(json.loads(output_results.read_text()))
        self.fence()
        _merge_assessment_output(
            output_root,
            trial,
            fence=self.fence,
        )
        self.fence()
        results_path = trial / self.definition.evaluation.results_path
        if not results_path.is_file():
            raise IntegrityError(
                "assessment Job completed without evaluation results: "
                f"{results_path}"
            )
        result = _validate_evaluation_result(
            json.loads(results_path.read_text())
        )
        self.fence()
        report_path = write_run_report(
            trial,
            results_path.with_name("run-report.html"),
        )
        self.fence()
        result["report"] = {
            "status": "complete",
            "path": str(report_path.relative_to(trial)),
        }
        self.fence()
        write_json_atomic(results_path, result)
        self.fence()
        self.client.delete("job", job_name)
        self.fence()
        self.client.delete("networkpolicy", job_name)
        self.fence()
        shutil.rmtree(output_root)
        self.fence()
        return result


def verify_campaign_sha256(
    campaign: ClusterCampaign,
    expected: str,
) -> None:
    if campaign.sha256 != expected:
        raise IntegrityError(
            "campaign definition digest differs from submitted deployment: "
            f"{campaign.sha256} != {expected}"
        )


def prepare_cluster_campaign(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    campaign: ClusterCampaign,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    marker = CONTROL_ROOT / f"prepared-{campaign.sha256}.json"
    failure_marker = (
        CONTROL_ROOT / f"preparation-{campaign.sha256}.failed.json"
    )
    try:
        verify_campaign_sha256(campaign, expected_sha256)
        result_manifest = RESULTS_ROOT / RESULT_MANIFEST
        if result_manifest.is_file():
            existing = json.loads(result_manifest.read_text())
            if existing.get("campaign_sha256") != campaign.sha256:
                result_manifest.unlink()
                (RESULTS_ROOT / "campaign.json").unlink(missing_ok=True)
        resources = campaign_resources(definition, campaign)
        backend = KubernetesBackend(
            campaign.backend,
            managed_proxy_name=resources.proxy,
            managed_proxy_campaign_labels=_labels(resources),
            source_claim_name=resources.control_claim,
            source_root=CONTROL_ROOT,
        )
        engine = CampaignEngine(
            definition,
            contract,
            campaign.plan,
            backend,
            control_root=CONTROL_ROOT,
            results_root=RESULTS_ROOT,
            dashboard_path=CONTROL_ROOT / "index.html",
        )
        state = engine.initialize()
        failure_marker.unlink(missing_ok=True)
        write_json_atomic(
            marker,
            {
                "schema_version": "1.0",
                "campaign_id": campaign.plan.campaign_id,
                "campaign_sha256": campaign.sha256,
                "prepared_at": _now(),
            },
        )
        return state
    except Exception as error:
        marker.unlink(missing_ok=True)
        write_json_atomic(
            failure_marker,
            {
                "schema_version": "1.0",
                "campaign_id": campaign.plan.campaign_id,
                "campaign_sha256": campaign.sha256,
                "failed_at": _now(),
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                },
            },
        )
        raise


def finalize_cluster_trial(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    campaign: ClusterCampaign,
    *,
    expected_sha256: str,
    trial_relative: str,
    output_relative: str,
) -> dict[str, Any]:
    from brunner.evaluation import finalize_evaluation

    verify_campaign_sha256(campaign, expected_sha256)
    relative = Path(trial_relative)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:1] != ("collected",)
    ):
        raise ValueError("trial-relative must identify a collected trial")
    trial = (CONTROL_ROOT / relative).resolve()
    if not trial.is_relative_to(CONTROL_ROOT.resolve()) or not trial.is_dir():
        raise ValueError(f"collected trial does not exist: {trial}")
    output_path = Path(output_relative)
    if (
        output_path.is_absolute()
        or ".." in output_path.parts
        or output_path.parts[:1] != ("assessment-output",)
    ):
        raise ValueError(
            "output-relative must identify an assessment-output directory"
        )
    output_trial = (CONTROL_ROOT / output_path).resolve()
    if (
        not output_trial.is_relative_to(CONTROL_ROOT.resolve())
        or not output_trial.is_dir()
    ):
        raise ValueError(
            f"assessment output directory does not exist: {output_trial}"
        )
    return finalize_evaluation(
        definition,
        contract,
        trial,
        output_trial=output_trial,
    )


def _wait_for_preparation(
    campaign: ClusterCampaign,
    resources: CampaignResources,
    client: Kubectl,
) -> None:
    marker = CONTROL_ROOT / f"prepared-{campaign.sha256}.json"
    failure_marker = (
        CONTROL_ROOT / f"preparation-{campaign.sha256}.failed.json"
    )
    deadline = (
        time.monotonic()
        + campaign.controller.preparation_timeout_seconds
        + 30
    )
    completed_without_marker_since: float | None = None
    while True:
        if marker.is_file():
            return
        failure: str | None = None
        if failure_marker.is_file():
            try:
                value = json.loads(failure_marker.read_text())
                error = value.get("error", {})
                failure = str(
                    error.get("message")
                    if isinstance(error, dict)
                    else error
                )
            except (json.JSONDecodeError, OSError):
                failure = failure_marker.read_text(errors="replace")
        job = client.get("job", resources.preparation_job)
        if job is not None:
            conditions = {
                item.get("type"): item
                for item in job.get("status", {}).get("conditions", ())
                if isinstance(item, dict)
            }
            failed = conditions.get("Failed", {})
            if failed.get("status") == "True":
                failure = str(
                    failed.get("message")
                    or failed.get("reason")
                    or "preparation Job failed"
                )
            completed = conditions.get("Complete", {})
            if completed.get("status") == "True":
                now = time.monotonic()
                if completed_without_marker_since is None:
                    completed_without_marker_since = now
                elif (
                    now - completed_without_marker_since
                    >= PREPARATION_MARKER_GRACE_SECONDS
                    and failure is None
                ):
                    failure = (
                        "preparation Job completed without publishing "
                        f"{marker.name}"
                    )
            else:
                completed_without_marker_since = None
        if time.monotonic() >= deadline and failure is None:
            failure = (
                "preparation did not complete within "
                f"{campaign.controller.preparation_timeout_seconds} seconds"
            )
        if failure is not None:
            status = {
                "schema_version": "1.0",
                "campaign_id": campaign.plan.campaign_id,
                "campaign_sha256": campaign.sha256,
                "status": "attention_required",
                "has_attention": True,
                "preparation_status": "failed",
                "preparation_error": failure,
                "result_ready": False,
                "updated_at": _now(),
            }
            client.apply(_status_resource(resources, status))
            raise BackendRequestError(
                f"campaign preparation failed: {failure}"
            )
        client.apply(
            _status_resource(
                resources,
                {
                    "schema_version": "1.0",
                    "campaign_id": campaign.plan.campaign_id,
                    "campaign_sha256": campaign.sha256,
                    "status": "preparing",
                    "has_attention": False,
                    "preparation_status": "running",
                    "result_ready": False,
                    "updated_at": _now(),
                },
            )
        )
        time.sleep(campaign.controller.poll_seconds)


def _publish_manifest_identity(
    client: Kubectl,
    resources: CampaignResources,
    campaign: ClusterCampaign,
    bundle: dict[str, Any],
) -> None:
    client.run(
        "annotate",
        "pvc",
        resources.results_claim,
        "-n",
        resources.namespace,
        (
            f"{RESULT_MANIFEST_SHA256_ANNOTATION}="
            f"{bundle['sha256']}"
        ),
        (
            f"{RESULT_MANIFEST_SIZE_ANNOTATION}="
            f"{bundle['size']}"
        ),
        (
            f"{CAMPAIGN_SHA256_ANNOTATION}="
            f"{campaign.sha256}"
        ),
        "--overwrite",
    )


def run_cluster_controller(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    campaign: ClusterCampaign,
    *,
    benchmark_ref: str,
    campaign_ref: str,
    expected_sha256: str,
) -> None:
    verify_campaign_sha256(campaign, expected_sha256)
    resources = campaign_resources(definition, campaign)
    client = Kubectl(
        resources.namespace,
        timeout_seconds=campaign.controller.command_timeout_seconds,
    )
    _wait_for_preparation(campaign, resources, client)
    pod_name = os.environ.get("BRUNNER_CONTROLLER_POD_NAME", "unknown")
    pod_uid = os.environ.get("BRUNNER_CONTROLLER_POD_UID", "unknown")
    lock = ConfigMapLock(
        client,
        resources,
        holder=f"{pod_name}/{pod_uid}",
        duration_seconds=campaign.controller.lock_duration_seconds,
    )
    lock.acquire()
    backend = KubernetesBackend(
        campaign.backend,
        managed_proxy_name=resources.proxy,
        managed_proxy_campaign_labels=_labels(resources),
        source_claim_name=resources.control_claim,
        source_root=CONTROL_ROOT,
        fence=lock.assert_held,
    )
    backend._ensure_managed_proxy()
    finalizer = KubernetesEvaluationFinalizer(
        definition=definition,
        campaign=campaign,
        resources=resources,
        client=client,
        benchmark_ref=benchmark_ref,
        campaign_ref=campaign_ref,
        proxy_url=backend._proxy_url,
        proxy_labels=backend._proxy_labels,
        fence=lock.assert_held,
    )
    engine = CampaignEngine(
        definition,
        contract,
        campaign.plan,
        backend,
        control_root=CONTROL_ROOT,
        results_root=RESULTS_ROOT,
        evaluation_finalizer=finalizer,
        result_publisher=lambda source, entry: publish_trial_results(
            source,
            RESULTS_ROOT / "trials" / str(entry["test_id"]),
            max_bytes=campaign.controller.max_published_trial_bytes,
            fence=lock.assert_held,
        ),
        dashboard_path=CONTROL_ROOT / "index.html",
        fence=lock.assert_held,
    )
    try:
        while True:
            lock.assert_held()
            marker = RESULTS_ROOT / RESULT_MANIFEST
            control_state = CONTROL_ROOT / "campaign.json"
            existing_state = (
                json.loads(control_state.read_text())
                if control_state.is_file()
                else None
            )
            existing_manifest = (
                json.loads(marker.read_text())
                if marker.is_file()
                else None
            )
            if (
                isinstance(existing_state, dict)
                and existing_state.get("status")
                in TERMINAL_CAMPAIGN_STATES
                and isinstance(existing_manifest, dict)
                and existing_manifest.get("campaign_sha256")
                == campaign.sha256
                and existing_manifest.get("state_sha256")
                == _result_state_sha256(existing_state)
                and existing_manifest.get("terminal", True) is True
            ):
                manifest_sha256 = sha256_file(marker)
                manifest_size = marker.stat().st_size
                status = _campaign_status(
                    campaign,
                    existing_state,
                    result_ready=True,
                    manifest_sha256=manifest_sha256,
                    manifest_size=manifest_size,
                )
                lock.assert_held()
                client.apply(_status_resource(resources, status))
                lock.assert_held()
                time.sleep(campaign.controller.poll_seconds)
                continue
            state = engine.advance()
            lock.assert_held()
            state_sha256 = _result_state_sha256(state)
            marker = RESULTS_ROOT / RESULT_MANIFEST
            current_manifest = (
                json.loads(marker.read_text())
                if marker.is_file()
                else None
            )
            if (
                isinstance(current_manifest, dict)
                and current_manifest.get("campaign_sha256")
                == campaign.sha256
                and current_manifest.get("state_sha256")
                == state_sha256
            ):
                bundle = {
                    "manifest": current_manifest,
                    "sha256": sha256_file(marker),
                    "size": marker.stat().st_size,
                }
            else:
                bundle = finalize_result_bundle(
                    RESULTS_ROOT,
                    state,
                    campaign,
                    fence=lock.assert_held,
                )
                lock.assert_held()
                _publish_manifest_identity(
                    client,
                    resources,
                    campaign,
                    bundle,
                )
            lock.assert_held()
            client.apply(
                _status_resource(
                    resources,
                    _campaign_status(
                        campaign,
                        state,
                        result_ready=(
                            state.get("status") in TERMINAL_CAMPAIGN_STATES
                        ),
                        manifest_sha256=str(bundle["sha256"]),
                        manifest_size=int(bundle["size"]),
                    ),
                )
            )
            lock.assert_held()
            time.sleep(campaign.controller.poll_seconds)
    finally:
        lock.close()


class ClusterCampaignClient:
    def __init__(
        self,
        definition: BenchmarkDefinition,
        campaign: ClusterCampaign,
        *,
        benchmark_ref: str,
        campaign_ref: str,
        kubectl: str = "kubectl",
    ) -> None:
        campaign.validate()
        self.definition = definition
        self.campaign = campaign
        self.benchmark_ref = benchmark_ref
        self.campaign_ref = campaign_ref
        self.resources = campaign_resources(definition, campaign)
        self.client = Kubectl(
            self.resources.namespace,
            executable=kubectl,
            timeout_seconds=campaign.controller.command_timeout_seconds,
        )

    def _validate_archive_compatibility(
        self,
        archive: dict[str, Any],
    ) -> None:
        from brunner.contract import load_output_contract

        state = archive["state"]
        contract = load_output_contract(
            self.definition.contract_path,
            expected_benchmark_id=self.definition.benchmark_id,
        )
        expected_identity = {
            "benchmark_id": self.definition.benchmark_id,
            "benchmark_version": self.definition.version,
            "contract_sha256": contract.sha256,
            "evaluation_sha256": _evaluation_sha256(
                _campaign_evaluation_spec(
                    self.definition,
                    contract,
                    self.campaign.plan,
                )
            ),
            "backend": "kubernetes",
        }
        mismatches = {
            key: {"expected": value, "actual": state.get(key)}
            for key, value in expected_identity.items()
            if state.get(key) != value
        }
        if mismatches:
            raise IntegrityError(
                f"campaign archive identity is incompatible: {mismatches}"
            )

        entries = state.get("trials")
        if not isinstance(entries, list):
            raise IntegrityError(
                "archived campaign state has no valid trial list"
            )
        configured = {
            trial.test_id: trial.to_dict()
            for trial in self.campaign.plan.trials
        }
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise IntegrityError(
                    "archived campaign state has a malformed trial entry"
                )
            test_id_value = entry.get("test_id")
            if not isinstance(test_id_value, str) or not test_id_value:
                raise IntegrityError(
                    "archived campaign state has a trial without a valid "
                    "test_id"
                )
            test_id = test_id_value
            if test_id in seen:
                raise IntegrityError(
                    f"archived campaign state repeats trial {test_id!r}"
                )
            seen.add(test_id)
            expected_trial_path = str(CONTROL_ROOT / "trials" / test_id)
            expected_result_path = str(RESULTS_ROOT / "trials" / test_id)
            if entry.get("trial") != expected_trial_path:
                raise IntegrityError(
                    "campaign archive contains an unsafe trial path for "
                    f"{test_id}: {entry.get('trial')!r}"
                )
            if (
                entry.get("collected_trial") is not None
                and entry.get("collected_trial") != expected_result_path
            ):
                raise IntegrityError(
                    "campaign archive contains an unsafe result path for "
                    f"{test_id}: {entry.get('collected_trial')!r}"
                )
            current = configured.get(test_id)
            if current is None:
                continue
            identity_keys = set(current) - {"backend_image"}
            trial_mismatches = {
                key: {
                    "expected": current.get(key),
                    "actual": entry.get(key),
                }
                for key in identity_keys
                if entry.get(key) != current.get(key)
            }
            if trial_mismatches:
                raise IntegrityError(
                    "campaign archive trial identity changed for "
                    f"{test_id}: {trial_mismatches}"
                )
            metadata_relative = (
                f"trials/{test_id}/metadata/manifest.json"
            )
            metadata_record = archive["files"].get(metadata_relative)
            if metadata_record is None:
                if entry.get("phase") == "complete":
                    raise IntegrityError(
                        "campaign archive is missing completed trial "
                        f"metadata: {test_id}"
                    )
                continue
            try:
                metadata = json.loads(
                    (archive["root"] / metadata_relative).read_text()
                )
            except (json.JSONDecodeError, OSError) as error:
                raise IntegrityError(
                    "campaign archive contains unreadable trial metadata: "
                    f"{test_id}"
                ) from error
            if not isinstance(metadata, dict):
                raise IntegrityError(
                    "campaign archive trial metadata must be an object: "
                    f"{test_id}"
                )
            expected_metadata = {
                "test_id": test_id,
                "provider": entry.get("provider"),
                "model": entry.get("model"),
                "effort": entry.get("effort"),
                "benchmark_id": state.get("benchmark_id"),
                "benchmark_version": state.get("benchmark_version"),
                "contract_sha256": state.get("contract_sha256"),
                "challenge_sha256": entry.get("challenge_sha256"),
            }
            metadata_mismatches = {
                key: {
                    "expected": value,
                    "actual": metadata.get(key),
                }
                for key, value in expected_metadata.items()
                if metadata.get(key) != value
            }
            resource_id = metadata.get("resource_id")
            if metadata_mismatches or not isinstance(
                resource_id,
                str,
            ) or not resource_id:
                raise IntegrityError(
                    "campaign archive trial metadata is incompatible for "
                    f"{test_id}: mismatches={metadata_mismatches}, "
                    f"resource_id_valid={bool(resource_id)}"
                )

    def _validate_resume_archive(self, root: Path) -> dict[str, Any]:
        archive = load_campaign_archive(
            root,
            expected_campaign_id=self.campaign.plan.campaign_id,
            require_terminal=True,
            require_resumable=True,
        )
        self._validate_archive_compatibility(archive)
        return archive

    def submit(
        self,
        *,
        resume_from: Path | None = None,
    ) -> dict[str, Any]:
        archive = (
            self._validate_resume_archive(resume_from)
            if resume_from is not None
            else None
        )
        if (
            archive is not None
            and self.client.get("deployment", self.resources.deployment)
            is not None
        ):
            raise BackendRequestError(
                "cannot restore a campaign archive while its controller "
                "Deployment exists; retire the remote campaign first or "
                "submit without --resume-from"
            )
        rendered = render_cluster_resources(
            self.definition,
            self.campaign,
            benchmark_ref=self.benchmark_ref,
            campaign_ref=self.campaign_ref,
        )
        deployment = next(
            item for item in rendered if item["kind"] == "Deployment"
        )
        preparation = next(
            item
            for item in rendered
            if item["kind"] == "Job"
            and item["metadata"]["name"]
            == self.resources.preparation_job
        )
        for resource in rendered:
            if resource is deployment or resource is preparation:
                continue
            self.client.apply(resource)
        self.client.delete("deployment", self.resources.deployment, wait=True)
        self.client.delete(
            "job",
            labels=(
                f"dev.brunner/campaign={self.resources.base},"
                "dev.brunner/role=preparer"
            ),
            wait=True,
        )
        if archive is not None:
            self._restore_archive(archive)
        self.client.apply(preparation)
        self.client.apply(deployment)
        return {
            "campaign_id": self.campaign.plan.campaign_id,
            "campaign_sha256": self.campaign.sha256,
            "namespace": self.resources.namespace,
            "controller": self.resources.deployment,
            "control_claim": self.resources.control_claim,
            "results_claim": self.resources.results_claim,
            "resumed_from": (
                str(archive["root"]) if archive is not None else None
            ),
            "status": "submitted",
        }

    def status(self) -> dict[str, Any]:
        config_map = self.client.get(
            "configmap",
            self.resources.status_config_map,
        )
        if config_map is None:
            raise BackendRequestError(
                "campaign status does not exist; submit the campaign first"
            )
        raw = config_map.get("data", {}).get("status.json")
        if not isinstance(raw, str):
            raise IntegrityError("campaign status ConfigMap is malformed")
        status = json.loads(raw)
        deployment = self.client.get(
            "deployment",
            self.resources.deployment,
        )
        status["controller_available"] = bool(
            deployment
            and int(deployment.get("status", {}).get("availableReplicas") or 0)
            > 0
        )
        try:
            updated_at = datetime.fromisoformat(
                str(status["updated_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            status["status_stale"] = True
        else:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            stale_seconds = (
                datetime.now(UTC) - updated_at
            ).total_seconds()
            status["status_stale_seconds"] = max(0, stale_seconds)
            status["status_stale"] = stale_seconds > max(
                30,
                self.campaign.controller.poll_seconds * 4,
            )
        status["namespace"] = self.resources.namespace
        preparation = self.client.get(
            "job",
            self.resources.preparation_job,
        )
        if preparation is not None:
            conditions = {
                item.get("type"): item
                for item in preparation.get("status", {}).get(
                    "conditions",
                    (),
                )
                if isinstance(item, dict)
            }
            if conditions.get("Failed", {}).get("status") == "True":
                failed = conditions["Failed"]
                status["preparation_status"] = "failed"
                status["preparation_error"] = (
                    failed.get("message")
                    or failed.get("reason")
                    or "preparation Job failed"
                )
                status["has_attention"] = True
            elif conditions.get("Complete", {}).get("status") == "True":
                status["preparation_status"] = "complete"
            elif preparation.get("status", {}).get("active"):
                status["preparation_status"] = "running"
            else:
                status["preparation_status"] = "pending"
        return status

    def _transfer_pod(self, *, write: bool) -> dict[str, Any]:
        role = "archive-writer" if write else "archive-reader"
        labels = {
            **_labels(self.resources),
            "dev.brunner/role": role,
        }
        mounts = [
            {
                "name": "results",
                "mountPath": str(RESULTS_ROOT),
                "readOnly": not write,
            },
            {"name": "tmp", "mountPath": "/tmp"},
        ]
        volumes = [
            {
                "name": "results",
                "persistentVolumeClaim": {
                    "claimName": self.resources.results_claim,
                    "readOnly": not write,
                },
            },
            {"name": "tmp", "emptyDir": {}},
        ]
        if write:
            mounts.insert(
                0,
                {
                    "name": "control",
                    "mountPath": str(CONTROL_ROOT),
                },
            )
            volumes.insert(
                0,
                {
                    "name": "control",
                    "persistentVolumeClaim": {
                        "claimName": self.resources.control_claim,
                    },
                },
            )
        pod_spec: dict[str, Any] = {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "securityContext": _pod_security_context(),
            "containers": [
                {
                    "name": "reader",
                    "image": self.campaign.controller.image,
                    "command": [
                        "sh",
                        "-c",
                        "trap : TERM INT; sleep 86400 & wait",
                    ],
                    "workingDir": "/tmp",
                    "securityContext": _security_context(),
                    "volumeMounts": mounts,
                }
            ],
            "volumes": volumes,
        }
        _pod_placement(self.campaign.controller, pod_spec)
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{self.resources.base}-{role}",
                "namespace": self.resources.namespace,
                "labels": labels,
            },
            "spec": pod_spec,
        }

    def _transfer_network_policy(
        self,
        pod_name: str,
        *,
        role: str,
    ) -> dict[str, Any]:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": pod_name,
                "namespace": self.resources.namespace,
                "labels": _labels(self.resources),
            },
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        **_labels(self.resources),
                        "dev.brunner/role": role,
                    }
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }

    def _wait_transfer_pod(self, name: str) -> None:
        result = self.client.run(
            "wait",
            f"pod/{name}",
            "-n",
            self.resources.namespace,
            "--for=condition=Ready",
            "--timeout=300s",
            check=False,
            timeout_seconds=330,
        )
        if result.returncode:
            raise BackendRequestError(
                f"campaign archive transfer Pod did not become ready: "
                f"{result.stderr or result.stdout}"
            )

    def _remote_read(
        self,
        pod: str,
        root: Path,
        path: str,
        offset: int,
        count: int,
    ) -> bytes:
        result = self.client.run_bytes(
            "exec",
            "-n",
            self.resources.namespace,
            pod,
            "--",
            "python",
            "-m",
            "brunner.backends.remote",
            "read",
            str(root),
            path,
            str(offset),
            str(count),
        )
        return result.stdout

    def _read(
        self,
        pod: str,
        path: str,
        offset: int,
        count: int,
    ) -> bytes:
        return self._remote_read(
            pod,
            RESULTS_ROOT,
            path,
            offset,
            count,
        )

    def _remote_file_info(
        self,
        pod: str,
        root: Path,
        relative: str,
    ) -> dict[str, Any]:
        result = self.client.run(
            "exec",
            "-n",
            self.resources.namespace,
            pod,
            "--",
            "python",
            "-m",
            "brunner.backends.remote",
            "file-info",
            str(root),
            relative,
        )
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise IntegrityError(
                "archive transfer returned malformed file metadata"
            )
        return value

    def _write_remote_chunk(
        self,
        pod: str,
        root: Path,
        relative: str,
        *,
        offset: int,
        total_size: int,
        data: bytes,
    ) -> None:
        self.client.run_bytes(
            "exec",
            "-i",
            "-n",
            self.resources.namespace,
            pod,
            "--",
            "python",
            "-m",
            "brunner.backends.remote",
            "write-chunk",
            str(root),
            relative,
            str(offset),
            str(total_size),
            input_bytes=data,
        )

    def _commit_remote_file(
        self,
        pod: str,
        root: Path,
        relative: str,
        *,
        size: int,
        sha256: str,
    ) -> None:
        self.client.run(
            "exec",
            "-n",
            self.resources.namespace,
            pod,
            "--",
            "python",
            "-m",
            "brunner.backends.remote",
            "commit-file",
            str(root),
            relative,
            str(size),
            sha256,
        )

    def _upload_file(
        self,
        pod: str,
        root: Path,
        relative: str,
        source: Path,
        *,
        size: int,
        expected_sha256: str,
    ) -> None:
        existing = self._remote_file_info(pod, root, relative)
        if existing.get("exists") is True:
            if (
                existing.get("size") == size
                and existing.get("sha256") == expected_sha256
            ):
                return
            raise IntegrityError(
                "campaign archive restore refuses to overwrite changed "
                f"remote content: {relative}"
            )
        partial_relative = relative + ".brunner-part"
        partial = self._remote_file_info(
            pod,
            root,
            partial_relative,
        )
        offset = int(partial.get("size") or 0)
        if offset > size:
            offset = 0
        if offset:
            prefix_digest = hashlib.sha256()
            remaining = offset
            with source.open("rb") as stream:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    prefix_digest.update(chunk)
                    remaining -= len(chunk)
            if (
                remaining
                or partial.get("sha256") != prefix_digest.hexdigest()
            ):
                offset = 0
        with source.open("rb") as stream:
            stream.seek(offset)
            if size == 0 and offset == 0:
                self._write_remote_chunk(
                    pod,
                    root,
                    relative,
                    offset=0,
                    total_size=0,
                    data=b"",
                )
            while offset < size:
                data = stream.read(
                    min(
                        self.campaign.controller.retrieval_chunk_bytes,
                        size - offset,
                    )
                )
                if not data:
                    raise IntegrityError(
                        "campaign archive source ended early during restore: "
                        f"{relative} at {offset}"
                    )
                self._write_remote_chunk(
                    pod,
                    root,
                    relative,
                    offset=offset,
                    total_size=size,
                    data=data,
                )
                offset += len(data)
        self._commit_remote_file(
            pod,
            root,
            relative,
            size=size,
            sha256=expected_sha256,
        )

    def _upload_bytes(
        self,
        pod: str,
        root: Path,
        relative: str,
        content: bytes,
    ) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix="brunner-archive-",
            suffix=".json",
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_bytes(content)
            self._upload_file(
                pod,
                root,
                relative,
                temporary,
                size=len(content),
                expected_sha256=hashlib.sha256(content).hexdigest(),
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_archive(self, archive: dict[str, Any]) -> None:
        pod_resource = self._transfer_pod(write=True)
        pod = str(pod_resource["metadata"]["name"])
        role = "archive-writer"
        self.client.delete("pod", pod, wait=True)
        self.client.delete("networkpolicy", pod, wait=True)
        self.client.apply(
            self._transfer_network_policy(pod, role=role)
        )
        self.client.apply(pod_resource)
        try:
            self._wait_transfer_pod(pod)
            expected_manifest = archive["manifest_sha256"]
            existing_manifest = self._remote_file_info(
                pod,
                RESULTS_ROOT,
                RESULT_MANIFEST,
            )
            if (
                existing_manifest.get("exists") is True
                and existing_manifest.get("sha256") != expected_manifest
            ):
                raise IntegrityError(
                    "results PVC already contains a different campaign "
                    "archive"
                )
            campaign_record = archive["files"]["campaign.json"]
            existing_state = self._remote_file_info(
                pod,
                CONTROL_ROOT,
                "campaign.json",
            )
            if existing_state.get("exists") is True and (
                existing_state.get("size") != campaign_record["size"]
                or existing_state.get("sha256")
                != campaign_record["sha256"]
            ):
                raise IntegrityError(
                    "control PVC already contains different campaign state"
                )

            for relative, record in sorted(archive["files"].items()):
                self._upload_file(
                    pod,
                    RESULTS_ROOT,
                    relative,
                    archive["root"] / relative,
                    size=int(record["size"]),
                    expected_sha256=str(record["sha256"]),
                )
            manifest_path = archive["manifest_path"]
            self._upload_file(
                pod,
                RESULTS_ROOT,
                RESULT_MANIFEST,
                manifest_path,
                size=manifest_path.stat().st_size,
                expected_sha256=expected_manifest,
            )
            for trial in archive["state"]["trials"]:
                test_id = str(trial["test_id"])
                relative = f"trials/{test_id}/metadata/manifest.json"
                record = archive["files"][relative]
                self._upload_file(
                    pod,
                    CONTROL_ROOT,
                    relative,
                    archive["root"] / relative,
                    size=int(record["size"]),
                    expected_sha256=str(record["sha256"]),
                )
            state_path = archive["root"] / "campaign.json"
            self._upload_file(
                pod,
                CONTROL_ROOT,
                "campaign.json",
                state_path,
                size=int(campaign_record["size"]),
                expected_sha256=str(campaign_record["sha256"]),
            )
            self._upload_file(
                pod,
                CONTROL_ROOT,
                "campaign.json.bak",
                state_path,
                size=int(campaign_record["size"]),
                expected_sha256=str(campaign_record["sha256"]),
            )
            marker = json.dumps(
                {
                    "schema_version": "1.0",
                    "campaign_id": self.campaign.plan.campaign_id,
                    "archive_manifest_sha256": expected_manifest,
                },
                indent=2,
                sort_keys=True,
            ).encode() + b"\n"
            self._upload_bytes(
                pod,
                CONTROL_ROOT,
                RESUME_ARCHIVE_MARKER,
                marker,
            )
        finally:
            self.client.delete("pod", pod, wait=True)
            self.client.delete("networkpolicy", pod, wait=True)

    def _download_file(
        self,
        pod: str,
        destination: Path,
        record: dict[str, Any],
    ) -> None:
        relative = str(record["path"])
        size = int(record["size"])
        expected_sha256 = str(record["sha256"])
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        if target.is_symlink() or partial.is_symlink():
            raise IntegrityError(
                f"campaign archive destination contains a symlink: {target}"
            )
        if target.exists() and not target.is_file():
            raise IntegrityError(
                "campaign archive destination contains a non-file at "
                f"{target}"
            )
        if target.is_file():
            if (
                target.stat().st_size == size
                and sha256_file(target) == expected_sha256
            ):
                return
            target.unlink()
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > size:
            partial.unlink()
            offset = 0
        with partial.open("ab") as stream:
            while offset < size:
                count = min(
                    self.campaign.controller.retrieval_chunk_bytes,
                    size - offset,
                )
                data = self._read(pod, relative, offset, count)
                if len(data) != count:
                    raise IntegrityError(
                        f"result file ended early: {relative} at {offset}"
                    )
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                offset += len(data)
        if sha256_file(partial) != expected_sha256:
            raise IntegrityError(
                f"result checksum mismatch after retrieval: {relative}"
            )
        partial.replace(target)

    def _assert_local_archive_tree(self, root: Path) -> None:
        if not root.exists():
            return
        if not root.is_dir() or root.is_symlink():
            raise IntegrityError(
                f"campaign archive path is unsafe: {root}"
            )
        for path in root.rglob("*"):
            if path.is_symlink():
                raise IntegrityError(
                    f"campaign archive path contains a symlink: {path}"
                )

    def _check_existing_archive_identity(
        self,
        root: Path,
        *,
        require_manifest: bool,
    ) -> None:
        if not root.exists() or not any(root.iterdir()):
            return
        manifest_path = root / RESULT_MANIFEST
        if not manifest_path.is_file() or manifest_path.is_symlink():
            if not require_manifest:
                return
            raise IntegrityError(
                "campaign archive destination is nonempty but has no "
                f"valid {RESULT_MANIFEST}"
            )
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError) as error:
            raise IntegrityError(
                "existing campaign archive manifest is unreadable"
            ) from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("campaign_id")
            != self.campaign.plan.campaign_id
        ):
            raise IntegrityError(
                "campaign archive destination belongs to another campaign"
            )

    def _prepare_local_sync(
        self,
        destination: Path,
    ) -> tuple[Path, Path]:
        if not destination.name:
            raise IntegrityError(
                "campaign archive destination must not be a filesystem root"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(
            f".{destination.name}.brunner-sync"
        )
        backup = destination.with_name(
            f".{destination.name}.brunner-backup"
        )
        for path in (destination, staging, backup):
            self._assert_local_archive_tree(path)

        if backup.exists():
            load_campaign_archive(
                backup,
                expected_campaign_id=self.campaign.plan.campaign_id,
            )
            try:
                load_campaign_archive(
                    destination,
                    expected_campaign_id=self.campaign.plan.campaign_id,
                )
            except IntegrityError:
                if destination.exists():
                    shutil.rmtree(destination)
                backup.replace(destination)
            else:
                shutil.rmtree(backup)

        self._check_existing_archive_identity(
            destination,
            require_manifest=True,
        )
        self._check_existing_archive_identity(
            staging,
            require_manifest=False,
        )
        staging.mkdir(parents=True, exist_ok=True)
        return staging, backup

    def _seed_staging_file(
        self,
        source_root: Path,
        staging_root: Path,
        record: dict[str, Any],
    ) -> None:
        relative = str(record["path"])
        source = source_root / relative
        target = staging_root / relative
        partial = target.with_name(target.name + ".part")
        if target.exists() or partial.exists() or not source.is_file():
            return
        if source.is_symlink():
            raise IntegrityError(
                f"campaign archive contains a symlink: {source}"
            )
        if (
            source.stat().st_size != int(record["size"])
            or sha256_file(source) != str(record["sha256"])
        ):
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)

    def _prune_staging_archive(
        self,
        staging: Path,
        records: list[dict[str, Any]],
    ) -> None:
        expected = {
            str(record["path"])
            for record in records
        }
        expected.add(RESULT_MANIFEST)
        for path in sorted(staging.rglob("*"), reverse=True):
            if path.is_symlink():
                raise IntegrityError(
                    f"campaign archive staging contains a symlink: {path}"
                )
            relative = path.relative_to(staging).as_posix()
            if path.is_file() and relative not in expected:
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    def _publish_local_archive(
        self,
        destination: Path,
        staging: Path,
        backup: Path,
    ) -> None:
        if backup.exists():
            raise IntegrityError(
                f"campaign archive backup was not recovered: {backup}"
            )
        if destination.exists():
            destination.replace(backup)
        try:
            staging.replace(destination)
        except Exception:
            if not destination.exists() and backup.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    def _remote_manifest_identity(
        self,
    ) -> tuple[str, int, dict[str, Any] | None]:
        status_config_map = self.client.get(
            "configmap",
            self.resources.status_config_map,
        )
        status = None
        if status_config_map is not None:
            raw = status_config_map.get("data", {}).get("status.json")
            if isinstance(raw, str):
                status = json.loads(raw)
        claim = self.client.get("pvc", self.resources.results_claim)
        if claim is None:
            raise BackendRequestError(
                f"results PVC does not exist: {self.resources.results_claim}"
            )
        annotations = claim.get("metadata", {}).get("annotations", {})
        status_sha256 = (
            status.get("result_manifest_sha256")
            if isinstance(status, dict)
            else None
        )
        status_size = (
            status.get("result_manifest_size")
            if isinstance(status, dict)
            else None
        )
        annotation_sha256 = annotations.get(
            RESULT_MANIFEST_SHA256_ANNOTATION
        )
        annotation_size = annotations.get(
            RESULT_MANIFEST_SIZE_ANNOTATION
        )
        expected_manifest_sha256 = (
            annotation_sha256
            if isinstance(annotation_sha256, str)
            else status_sha256
        )
        raw_manifest_size = (
            annotation_size
            if annotation_size is not None
            else status_size
        )
        try:
            manifest_size = int(raw_manifest_size)
        except (TypeError, ValueError):
            manifest_size = None
        if (
            not isinstance(expected_manifest_sha256, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                expected_manifest_sha256,
            )
            is None
            or not isinstance(manifest_size, int)
            or manifest_size <= 0
        ):
            raise IntegrityError(
                "campaign status has no valid result manifest identity"
            )
        return expected_manifest_sha256, manifest_size, status

    def _parse_remote_manifest(
        self,
        manifest_bytes: bytes,
    ) -> dict[str, Any]:
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as error:
            raise IntegrityError(
                "campaign result manifest is not valid JSON"
            ) from error
        if not isinstance(manifest, dict):
            raise IntegrityError(
                "campaign result manifest must be an object"
            )
        if manifest.get("schema_version") not in {"1.0", "2.0"}:
            raise IntegrityError(
                "campaign result manifest has an unsupported schema version"
            )
        if manifest.get("campaign_id") != self.campaign.plan.campaign_id:
            raise IntegrityError(
                "result manifest belongs to a different campaign"
            )
        campaign_sha256 = manifest.get("campaign_sha256")
        if (
            not isinstance(campaign_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", campaign_sha256) is None
        ):
            raise IntegrityError(
                "result manifest has no valid campaign definition digest"
            )
        records = manifest.get("files")
        if not isinstance(records, list):
            raise IntegrityError(
                "campaign result manifest files must be an array"
            )
        paths: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise IntegrityError(
                    "result manifest contains a malformed file record"
                )
            relative = Path(str(record.get("path") or ""))
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() == RESULT_MANIFEST
            ):
                raise IntegrityError(
                    f"result manifest has unsafe path: {relative}"
                )
            relative_name = relative.as_posix()
            if relative_name in paths:
                raise IntegrityError(
                    f"result manifest repeats a path: {relative_name}"
                )
            paths.add(relative_name)
            try:
                size = int(record["size"])
                digest = str(record["sha256"])
            except (KeyError, TypeError, ValueError) as error:
                raise IntegrityError(
                    "result manifest contains invalid file metadata: "
                    f"{relative_name}"
                ) from error
            if size < 0 or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise IntegrityError(
                    "result manifest contains invalid file metadata: "
                    f"{relative_name}"
                )
        return manifest

    def sync(self, destination: Path) -> dict[str, Any]:
        requested_destination = destination.expanduser()
        if requested_destination.is_symlink():
            raise IntegrityError(
                "campaign archive destination is unsafe: "
                f"{requested_destination}"
            )
        destination = requested_destination.resolve()
        staging, backup = self._prepare_local_sync(destination)

        pod_resource = self._transfer_pod(write=False)
        pod = str(pod_resource["metadata"]["name"])
        role = "archive-reader"
        self.client.delete("pod", pod, wait=True)
        self.client.delete("networkpolicy", pod, wait=True)
        self.client.apply(
            self._transfer_network_policy(pod, role=role)
        )
        self.client.apply(pod_resource)
        try:
            self._wait_transfer_pod(pod)
            last_error: Exception | None = None
            for attempt in range(1, 4):
                expected_sha256, manifest_size, status = (
                    self._remote_manifest_identity()
                )
                manifest_bytes = self._read(
                    pod,
                    RESULT_MANIFEST,
                    0,
                    manifest_size,
                )
                observed = hashlib.sha256(manifest_bytes).hexdigest()
                if observed != expected_sha256:
                    last_error = IntegrityError(
                        "result manifest changed during synchronization: "
                        f"{observed} != {expected_sha256}"
                    )
                    continue
                manifest = self._parse_remote_manifest(manifest_bytes)
                try:
                    for record in manifest["files"]:
                        self._seed_staging_file(
                            destination,
                            staging,
                            record,
                        )
                        self._download_file(pod, staging, record)
                except IntegrityError as error:
                    last_error = error
                    if attempt < 3:
                        continue
                    raise
                self._prune_staging_archive(
                    staging,
                    manifest["files"],
                )
                manifest_path = staging / RESULT_MANIFEST
                partial_manifest = manifest_path.with_name(
                    manifest_path.name + ".part"
                )
                with partial_manifest.open("wb") as stream:
                    stream.write(manifest_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                partial_manifest.replace(manifest_path)
                archive = load_campaign_archive(
                    staging,
                    expected_campaign_id=self.campaign.plan.campaign_id,
                )
                self._validate_archive_compatibility(archive)
                self._publish_local_archive(
                    destination,
                    staging,
                    backup,
                )
                return {
                    "campaign_id": self.campaign.plan.campaign_id,
                    "destination": str(destination),
                    "files": len(manifest["files"]),
                    "manifest_sha256": observed,
                    "terminal": archive["terminal"],
                    "remote_status": (
                        status.get("status")
                        if isinstance(status, dict)
                        else None
                    ),
                }
            if last_error is not None:
                raise last_error
            raise IntegrityError(
                "campaign archive synchronization did not complete"
            )
        finally:
            self.client.delete("pod", pod, wait=True)
            self.client.delete("networkpolicy", pod, wait=True)

    def retire(self, archive_root: Path) -> dict[str, Any]:
        result_claim = self.client.get(
            "pvc",
            self.resources.results_claim,
        )
        control_claim = self.client.get(
            "pvc",
            self.resources.control_claim,
        )
        if result_claim is None and control_claim is None:
            archive = load_campaign_archive(
                archive_root,
                expected_campaign_id=self.campaign.plan.campaign_id,
                require_terminal=True,
                require_resumable=True,
            )
            return {
                "campaign_id": self.campaign.plan.campaign_id,
                "archive": str(archive["root"]),
                "manifest_sha256": archive["manifest_sha256"],
                "already_retired": True,
                "deleted_claims": [],
            }
        if result_claim is None:
            raise IntegrityError(
                "cannot verify retirement because the results PVC is missing"
            )
        self.sync(archive_root)
        archive = load_campaign_archive(
            archive_root,
            expected_campaign_id=self.campaign.plan.campaign_id,
            require_terminal=True,
            require_resumable=True,
        )
        result_claim = self.client.get(
            "pvc",
            self.resources.results_claim,
        )
        if result_claim is None:
            raise IntegrityError(
                "results PVC disappeared during verified retirement"
            )
        annotations = result_claim.get("metadata", {}).get(
            "annotations",
            {},
        )
        remote_sha256 = annotations.get(
            RESULT_MANIFEST_SHA256_ANNOTATION
        )
        remote_size = annotations.get(RESULT_MANIFEST_SIZE_ANNOTATION)
        if (
            remote_sha256 != archive["manifest_sha256"]
            or str(remote_size)
            != str(archive["manifest_path"].stat().st_size)
        ):
            raise IntegrityError(
                "local archive does not match the finalized results PVC; "
                "run campaign-sync again before retirement"
            )
        remote_campaign_sha256 = annotations.get(
            CAMPAIGN_SHA256_ANNOTATION
        )
        if remote_campaign_sha256 != archive["manifest"].get(
            "campaign_sha256"
        ):
            raise IntegrityError(
                "local archive campaign revision does not match the "
                "results PVC"
            )
        status_config_map = self.client.get(
            "configmap",
            self.resources.status_config_map,
        )
        deployment = self.client.get(
            "deployment",
            self.resources.deployment,
        )
        if deployment is not None:
            raw_status = (
                status_config_map.get("data", {}).get("status.json")
                if status_config_map is not None
                else None
            )
            status = (
                json.loads(raw_status)
                if isinstance(raw_status, str)
                else None
            )
            if (
                not isinstance(status, dict)
                or status.get("result_ready") is not True
                or status.get("result_manifest_sha256")
                != archive["manifest_sha256"]
            ):
                raise BackendRequestError(
                    "campaign controller has not confirmed the verified "
                    "terminal archive; synchronization and retirement must "
                    "wait"
                )

        campaign_labels = (
            f"dev.brunner/campaign={self.resources.base}"
        )
        claims = self.client.get(
            "pvc",
            labels=campaign_labels,
        ) or {"items": []}
        claim_names = sorted(
            str(claim.get("metadata", {}).get("name"))
            for claim in claims.get("items", ())
            if claim.get("metadata", {}).get("name")
        )
        self.client.delete(
            "deployment",
            self.resources.deployment,
            wait=True,
        )
        self.client.delete(
            "pod",
            labels=campaign_labels,
            wait=True,
        )
        self.client.delete(
            "job",
            labels=campaign_labels,
            wait=True,
        )
        self.client.delete(
            "pod",
            labels=campaign_labels,
            wait=True,
        )
        self.client.delete(
            "networkpolicy",
            labels=campaign_labels,
            wait=True,
        )
        self.client.delete(
            "service",
            labels=campaign_labels,
            wait=True,
        )
        for name in claim_names:
            self.client.delete("pvc", name, wait=True)
        for kind, name in (
            ("deployment", self.resources.proxy),
            ("configmap", self.resources.proxy),
            ("configmap", self.resources.status_config_map),
            ("rolebinding", self.resources.role),
            ("role", self.resources.role),
            ("serviceaccount", self.resources.service_account),
            ("configmap", self.resources.lock_config_map),
        ):
            self.client.delete(kind, name, wait=True)
        return {
            "campaign_id": self.campaign.plan.campaign_id,
            "archive": str(archive["root"]),
            "manifest_sha256": archive["manifest_sha256"],
            "already_retired": False,
            "deleted_claims": claim_names,
        }
