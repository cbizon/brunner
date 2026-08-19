from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brunner.backends import KubernetesBackend, KubernetesProfile
from brunner.artifacts import artifact_metadata
from brunner.backends.squid import MANAGED_PROXY_LABELS, MANAGED_PROXY_PORT
from brunner.campaign import (
    CampaignEngine,
    CampaignPlan,
    campaign_resource_name,
)
from brunner.contract import OutputContract
from brunner.definition import BenchmarkDefinition
from brunner.errors import (
    BackendConnectivityError,
    BackendRequestError,
    IntegrityError,
)
from brunner.hashing import sha256_file
from brunner.io import write_json_atomic


CONTROL_ROOT = Path("/brunner/control")
RESULTS_ROOT = Path("/brunner/results")
RESULT_MANIFEST = "result-manifest.json"
RESULT_MANIFEST_SHA256_ANNOTATION = (
    "dev.brunner/result-manifest-sha256"
)
RESULT_MANIFEST_SIZE_ANNOTATION = "dev.brunner/result-manifest-size"
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
    dashboard_port: int = 8765
    command_timeout_seconds: float = 120
    retrieval_chunk_bytes: int = 4 * 1024 * 1024
    max_published_trial_bytes: int | None = 10 * 1024 * 1024 * 1024
    controller_cpu_request: str = "250m"
    controller_cpu_limit: str = "2"
    controller_memory_request: str = "512Mi"
    controller_memory_limit: str = "4Gi"
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
        if not 1 <= self.dashboard_port <= 65535:
            raise ValueError("controller dashboard_port is invalid")
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
    def service(self) -> str:
        return f"{self.base}-monitor"

    @property
    def assessment_policy(self) -> str:
        return f"{self.base}-assessment"


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
        "ports": [
            {
                "name": "http",
                "containerPort": profile.dashboard_port,
                "protocol": "TCP",
            }
        ],
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
        "readinessProbe": {
            "httpGet": {"path": "/", "port": "http"},
            "initialDelaySeconds": 2,
            "periodSeconds": 10,
        },
    }
    controller_pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": True,
        "enableServiceLinks": False,
        "serviceAccountName": resources.service_account,
        "securityContext": _pod_security_context(),
        "terminationGracePeriodSeconds": 30,
        "initContainers": [
            {
                "name": "wait-for-preparation",
                "image": profile.image,
                "command": [
                    "sh",
                    "-c",
                    (
                        "deadline=$(("
                        f"$(date +%s)+{math.ceil(profile.preparation_timeout_seconds)}"
                        ")); "
                        "until test -f "
                        f"{CONTROL_ROOT}/prepared-{campaign.sha256}.json; "
                        "do test $(date +%s) -lt $deadline || exit 1; "
                        "sleep 2; done"
                    ),
                ],
                "workingDir": "/tmp",
                "securityContext": _security_context(),
                "volumeMounts": mounts,
            }
        ],
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
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": resources.service,
                "namespace": resources.namespace,
                "labels": labels,
            },
            "spec": {
                "selector": pod_labels,
                "ports": [
                    {
                        "name": "http",
                        "port": profile.dashboard_port,
                        "targetPort": "http",
                    }
                ],
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
            raise RuntimeError(
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


def _result_inventory(results_root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(results_root.rglob("*")):
        if path.is_symlink():
            raise IntegrityError(
                f"result bundle contains a symlink: {path}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(results_root).as_posix()
        if relative == RESULT_MANIFEST:
            continue
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return files


def publish_trial_results(
    source: Path,
    destination: Path,
    *,
    max_bytes: int | None,
) -> Path:
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
            baseline = value["file_inventory"]

    selected: dict[str, dict[str, Any]] = {}
    for path in sorted(source.rglob("*")):
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
    partial.mkdir(parents=True, exist_ok=True)
    for relative, expected in selected.items():
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
        if not path.is_file():
            continue
        relative = path.relative_to(partial).as_posix()
        if relative == "publication.json":
            continue
        if relative not in selected:
            path.unlink()
    write_json_atomic(
        partial / "publication.json",
        {
            "schema_version": "1.0",
            "files": selected,
            "total_bytes": total,
            "published_at": _now(),
        },
    )
    if destination.exists():
        shutil.rmtree(destination)
    partial.replace(destination)
    return destination


def finalize_result_bundle(
    results_root: Path,
    state: dict[str, Any],
    campaign: ClusterCampaign,
) -> dict[str, Any]:
    results_root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(results_root / "campaign.json", state)
    manifest = {
        "schema_version": "1.0",
        "campaign_id": campaign.plan.campaign_id,
        "campaign_sha256": campaign.sha256,
        "created_at": _now(),
        "files": _result_inventory(results_root),
    }
    manifest_path = results_root / RESULT_MANIFEST
    write_json_atomic(manifest_path, manifest)
    return {
        "manifest": manifest,
        "sha256": sha256_file(manifest_path),
        "size": manifest_path.stat().st_size,
    }


def _assessment_providers(
    definition: BenchmarkDefinition,
) -> frozenset[str]:
    return frozenset(
        assessment.reviewer.provider
        for assessment in definition.resolved_assessments()
        if assessment.reviewer is not None
    )


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
    ) -> None:
        self.definition = definition
        self.campaign = campaign
        self.resources = resources
        self.client = client
        self.benchmark_ref = benchmark_ref
        self.campaign_ref = campaign_ref
        self.proxy_url = proxy_url

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
                                "matchLabels": dict(MANAGED_PROXY_LABELS)
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

    def _wait(self, job_name: str) -> None:
        deadline = time.monotonic() + sum(
            assessment.timeout_seconds
            for assessment in self.definition.resolved_assessments()
        ) + 600
        while time.monotonic() < deadline:
            job = self.client.get("job", job_name)
            if job is None:
                raise BackendRequestError(
                    f"assessment Job disappeared: {job_name}"
                )
            conditions = {
                item.get("type"): item
                for item in job.get("status", {}).get("conditions", ())
                if isinstance(item, dict)
            }
            if conditions.get("Complete", {}).get("status") == "True":
                return
            if conditions.get("Failed", {}).get("status") == "True":
                raise BackendRequestError(
                    "assessment Job failed: "
                    + str(conditions["Failed"].get("message") or job_name)
                )
            pods = self.client.get(
                "pods",
                labels=f"job-name={job_name}",
            ) or {"items": []}
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
            time.sleep(self.campaign.controller.poll_seconds)
        raise TimeoutError(f"assessment Job timed out: {job_name}")

    def __call__(self, trial: Path) -> dict[str, Any]:
        from brunner.evaluation import _validate_evaluation_result

        job_name = self._job_name(trial)
        job = self._job(trial, job_name)
        labels = dict(job["metadata"]["labels"])
        self.client.apply(self._network_policy(job_name, labels))
        existing = self.client.get("job", job_name)
        if existing is None:
            self.client.apply(job)
        self._wait(job_name)
        results_path = trial / self.definition.evaluation.results_path
        if not results_path.is_file():
            raise IntegrityError(
                "assessment Job completed without evaluation results: "
                f"{results_path}"
            )
        result = _validate_evaluation_result(
            json.loads(results_path.read_text())
        )
        self.client.delete("job", job_name)
        self.client.delete("networkpolicy", job_name)
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
    verify_campaign_sha256(campaign, expected_sha256)
    result_manifest = RESULTS_ROOT / RESULT_MANIFEST
    if result_manifest.is_file():
        existing = json.loads(result_manifest.read_text())
        if existing.get("campaign_sha256") != campaign.sha256:
            result_manifest.unlink()
            (RESULTS_ROOT / "campaign.json").unlink(missing_ok=True)
    backend = KubernetesBackend(campaign.backend)
    engine = CampaignEngine(
        definition,
        contract,
        campaign.plan,
        backend,
        control_root=CONTROL_ROOT,
        results_root=RESULTS_ROOT,
    )
    state = engine.initialize()
    marker = CONTROL_ROOT / f"prepared-{campaign.sha256}.json"
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


def finalize_cluster_trial(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    campaign: ClusterCampaign,
    *,
    expected_sha256: str,
    trial_relative: str,
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
    return finalize_evaluation(definition, contract, trial)


def run_cluster_controller(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    campaign: ClusterCampaign,
    *,
    benchmark_ref: str,
    campaign_ref: str,
    expected_sha256: str,
) -> None:
    from brunner.dashboard import start_campaign_server

    verify_campaign_sha256(campaign, expected_sha256)
    resources = campaign_resources(definition, campaign)
    client = Kubectl(
        resources.namespace,
        timeout_seconds=campaign.controller.command_timeout_seconds,
    )
    pod_name = os.environ.get("BRUNNER_CONTROLLER_POD_NAME", "unknown")
    pod_uid = os.environ.get("BRUNNER_CONTROLLER_POD_UID", "unknown")
    lock = ConfigMapLock(
        client,
        resources,
        holder=f"{pod_name}/{pod_uid}",
        duration_seconds=campaign.controller.lock_duration_seconds,
    )
    lock.acquire()
    backend = KubernetesBackend(campaign.backend)
    backend._ensure_managed_proxy()
    finalizer = KubernetesEvaluationFinalizer(
        definition=definition,
        campaign=campaign,
        resources=resources,
        client=client,
        benchmark_ref=benchmark_ref,
        campaign_ref=campaign_ref,
        proxy_url=backend._proxy_url,
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
        ),
    )
    server, _ = start_campaign_server(
        RESULTS_ROOT,
        host="0.0.0.0",
        port=campaign.controller.dashboard_port,
    )
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="brunner-cluster-monitor",
        daemon=True,
    )
    server_thread.start()
    try:
        while True:
            lock.assert_held()
            marker = RESULTS_ROOT / RESULT_MANIFEST
            if marker.is_file():
                state = json.loads(
                    (RESULTS_ROOT / "campaign.json").read_text()
                )
                manifest_sha256 = sha256_file(marker)
                manifest_size = marker.stat().st_size
                status = _campaign_status(
                    campaign,
                    state,
                    result_ready=True,
                    manifest_sha256=manifest_sha256,
                    manifest_size=manifest_size,
                )
                client.apply(_status_resource(resources, status))
                time.sleep(campaign.controller.poll_seconds)
                continue
            state = engine.advance()
            status = _campaign_status(
                campaign,
                state,
                result_ready=False,
            )
            client.apply(_status_resource(resources, status))
            if state.get("status") in TERMINAL_CAMPAIGN_STATES:
                bundle = finalize_result_bundle(
                    RESULTS_ROOT,
                    state,
                    campaign,
                )
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
                client.apply(
                    _status_resource(
                        resources,
                        _campaign_status(
                            campaign,
                            state,
                            result_ready=True,
                            manifest_sha256=str(bundle["sha256"]),
                            manifest_size=int(bundle["size"]),
                        ),
                    )
                )
            time.sleep(campaign.controller.poll_seconds)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
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

    def submit(self) -> dict[str, Any]:
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
        self.client.apply(preparation)
        self.client.apply(deployment)
        return {
            "campaign_id": self.campaign.plan.campaign_id,
            "campaign_sha256": self.campaign.sha256,
            "namespace": self.resources.namespace,
            "controller": self.resources.deployment,
            "monitor_service": self.resources.service,
            "control_claim": self.resources.control_claim,
            "results_claim": self.resources.results_claim,
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
        status["monitor_service"] = self.resources.service
        return status

    def monitor(self, *, local_port: int = 8765) -> int:
        if not 1 <= local_port <= 65535:
            raise ValueError("local monitor port is invalid")
        command = [
            self.client.executable,
            "port-forward",
            "-n",
            self.resources.namespace,
            f"service/{self.resources.service}",
            (
                f"{local_port}:"
                f"{self.campaign.controller.dashboard_port}"
            ),
        ]
        process = subprocess.Popen(command)
        try:
            return process.wait()
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            return process.wait()

    def _retrieval_pod(self) -> dict[str, Any]:
        labels = {
            **_labels(self.resources),
            "dev.brunner/role": "result-reader",
        }
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
                    "volumeMounts": [
                        {
                            "name": "results",
                            "mountPath": str(RESULTS_ROOT),
                            "readOnly": True,
                        },
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "results",
                    "persistentVolumeClaim": {
                        "claimName": self.resources.results_claim,
                        "readOnly": True,
                    },
                },
                {"name": "tmp", "emptyDir": {}},
            ],
        }
        _pod_placement(self.campaign.controller, pod_spec)
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{self.resources.base}-result-reader",
                "namespace": self.resources.namespace,
                "labels": labels,
            },
            "spec": pod_spec,
        }

    def _retrieval_network_policy(self, pod_name: str) -> dict[str, Any]:
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
                        "dev.brunner/role": "result-reader",
                    }
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }

    def _wait_reader(self, name: str) -> None:
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
                f"result reader did not become ready: "
                f"{result.stderr or result.stdout}"
            )

    def _read(
        self,
        pod: str,
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
            str(RESULTS_ROOT),
            path,
            str(offset),
            str(count),
        )
        return result.stdout

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

    def retrieve(self, destination: Path) -> dict[str, Any]:
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
        if status is not None and status.get("result_ready") is not True:
            raise BackendRequestError("campaign result bundle is not ready")
        expected_manifest_sha256 = (
            status.get("result_manifest_sha256")
            if status is not None
            else annotations.get(RESULT_MANIFEST_SHA256_ANNOTATION)
        )
        raw_manifest_size = (
            status.get("result_manifest_size")
            if status is not None
            else annotations.get(RESULT_MANIFEST_SIZE_ANNOTATION)
        )
        try:
            manifest_size = int(raw_manifest_size)
        except (TypeError, ValueError):
            manifest_size = None
        if (
            not isinstance(expected_manifest_sha256, str)
            or not isinstance(manifest_size, int)
        ):
            raise IntegrityError(
                "campaign status has no valid result manifest identity"
            )
        pod_resource = self._retrieval_pod()
        pod = str(pod_resource["metadata"]["name"])
        self.client.delete("pod", pod, wait=True)
        self.client.delete("networkpolicy", pod, wait=True)
        self.client.apply(self._retrieval_network_policy(pod))
        self.client.apply(pod_resource)
        try:
            self._wait_reader(pod)
            manifest_bytes = self._read(
                pod,
                RESULT_MANIFEST,
                0,
                manifest_size,
            )
            observed = hashlib.sha256(manifest_bytes).hexdigest()
            if observed != expected_manifest_sha256:
                raise IntegrityError(
                    "result manifest checksum mismatch: "
                    f"{observed} != {expected_manifest_sha256}"
                )
            manifest = json.loads(manifest_bytes)
            if manifest.get("campaign_sha256") != self.campaign.sha256:
                raise IntegrityError(
                    "result manifest belongs to a different campaign "
                    "definition"
                )
            destination = destination.expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            for record in manifest.get("files", ()):
                if not isinstance(record, dict):
                    raise IntegrityError(
                        "result manifest contains a malformed file record"
                    )
                relative = Path(str(record.get("path") or ""))
                if (
                    not relative.parts
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    raise IntegrityError(
                        f"result manifest has unsafe path: {relative}"
                    )
                self._download_file(pod, destination, record)
            manifest_path = destination / RESULT_MANIFEST
            partial_manifest = manifest_path.with_name(
                manifest_path.name + ".part"
            )
            partial_manifest.write_bytes(manifest_bytes)
            partial_manifest.replace(manifest_path)
            return {
                "campaign_id": self.campaign.plan.campaign_id,
                "destination": str(destination),
                "files": len(manifest.get("files", ())),
                "manifest_sha256": observed,
            }
        finally:
            self.client.delete("pod", pod, wait=True)
            self.client.delete("networkpolicy", pod, wait=True)

    def delete(self, *, delete_results: bool) -> dict[str, Any]:
        self.client.delete(
            "deployment",
            self.resources.deployment,
            wait=True,
        )
        self.client.delete(
            "pod",
            labels=f"dev.brunner/campaign={self.resources.base}",
            wait=True,
        )
        self.client.delete(
            "job",
            labels=f"dev.brunner/campaign={self.resources.base}",
            wait=True,
        )
        self.client.delete(
            "pod",
            labels=f"dev.brunner/campaign={self.resources.base}",
            wait=True,
        )
        self.client.delete(
            "networkpolicy",
            labels=f"dev.brunner/campaign={self.resources.base}",
            wait=True,
        )
        claims = self.client.get(
            "pvc",
            labels=f"dev.brunner/campaign={self.resources.base}",
        ) or {"items": []}
        preserved = {
            self.resources.control_claim,
            self.resources.results_claim,
        }
        for claim in claims.get("items", ()):
            name = claim.get("metadata", {}).get("name")
            if isinstance(name, str) and name not in preserved:
                self.client.delete("pvc", name, wait=True)
        for kind, name in (
            ("service", self.resources.service),
            ("configmap", self.resources.status_config_map),
            ("rolebinding", self.resources.role),
            ("role", self.resources.role),
            ("serviceaccount", self.resources.service_account),
            ("configmap", self.resources.lock_config_map),
            ("pvc", self.resources.control_claim),
        ):
            self.client.delete(kind, name, wait=True)
        if delete_results:
            self.client.delete(
                "pvc",
                self.resources.results_claim,
                wait=True,
            )
        return {
            "campaign_id": self.campaign.plan.campaign_id,
            "deleted_results": delete_results,
            "results_claim": (
                None
                if delete_results
                else self.resources.results_claim
            ),
        }
