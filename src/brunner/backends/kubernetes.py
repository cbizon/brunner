from __future__ import annotations

import base64
import json
import math
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brunner.artifacts import (
    CHUNK_BYTES,
    artifact_metadata,
    finalize_artifact_collection,
    prepare_partial_artifacts,
)
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
    ArtifactTransferError,
    BackendError,
    BackendConnectivityError,
    BackendRequestError,
    IntegrityError,
)
from brunner.io import write_json_atomic


CONNECTIVITY_FRAGMENTS = (
    "unable to connect to the server",
    "connection refused",
    "connection reset by peer",
    "context deadline exceeded",
    "dial tcp",
    "i/o timeout",
    "no route to host",
    "no such host",
    "temporary failure in name resolution",
    "tls handshake timeout",
    "service unavailable",
)


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
    storage_size: str = "20Gi"
    storage_class_name: str | None = None
    service_account_name: str | None = None
    image_pull_secrets: tuple[str, ...] = ()
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: tuple[dict[str, Any], ...] = ()
    secret_environment: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )
    max_parallel: int | None = None
    staging_timeout_seconds: float = 10 * 60
    reader_timeout_seconds: float = 10 * 60
    reader_attempts: int = 3
    retain_failed_storage: bool = True
    command_timeout_seconds: float = 120


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
    excluded_nodes: tuple[str, ...] = (),
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "containers": [container],
        "volumes": [
            {
                "name": "trial",
                "persistentVolumeClaim": {"claimName": claim_name},
            }
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
    excluded_nodes: tuple[str, ...] = (),
) -> dict[str, Any]:
    container = {
        "name": "helper",
        "image": image,
        "command": ["sh", "-c", "trap : TERM INT; sleep 86400 & wait"],
        "volumeMounts": [{"name": "trial", "mountPath": "/brunner/trial"}],
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
            excluded_nodes=excluded_nodes,
        ),
    }


def render_job(
    name: str,
    claim_name: str,
    workload: WorkloadSpec,
    profile: KubernetesProfile,
    labels: dict[str, str],
) -> dict[str, Any]:
    image = workload.image or profile.agent_image
    if not image:
        raise BackendRequestError(
            "Kubernetes workloads require an agent image"
        )
    environment = [
        {"name": key, "value": value}
        for key, value in sorted(workload.environment.items())
    ]
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
        for name, reference in sorted(profile.secret_environment.items())
    )
    resources: dict[str, dict[str, str]] = {}
    values = {}
    if workload.cpu:
        values["cpu"] = workload.cpu
    if workload.memory:
        values["memory"] = workload.memory
    if workload.gpu:
        values["nvidia.com/gpu"] = str(workload.gpu)
    if values:
        resources = {"requests": values, "limits": values}
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
    pod_spec["activeDeadlineSeconds"] = math.ceil(
        workload.timeout_seconds
    )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": profile.namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": labels},
                "spec": pod_spec,
            },
        },
    }


class KubernetesBackend:
    name = "kubernetes"

    def __init__(
        self,
        profile: KubernetesProfile,
        *,
        kubectl: str = "kubectl",
    ) -> None:
        self.profile = profile
        self.kubectl = kubectl
        self._handles: dict[tuple[str, str], BackendHandle] = {}

    def _error(
        self,
        arguments: tuple[str, ...],
        return_code: int,
        stdout: bytes,
        stderr: bytes,
    ) -> BackendError:
        message = (stderr or stdout).decode(errors="replace").strip()
        lowered = message.lower()
        error_type = (
            BackendConnectivityError
            if any(item in lowered for item in CONNECTIVITY_FRAGMENTS)
            else BackendRequestError
        )
        return error_type(
            f"{self.kubectl} {' '.join(arguments)} exited "
            f"{return_code}: {message}"
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

    def _warning_events(
        self,
        name: str,
        uid: str | None,
    ) -> tuple[str, ...]:
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
            return ()
        try:
            events = json.loads(result.stdout).get("items", ())
        except (AttributeError, json.JSONDecodeError):
            return ()
        warnings = []
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "Warning":
                continue
            series = event.get("series")
            if not isinstance(series, dict):
                series = {}
            metadata = event.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            reason = str(event.get("reason") or "").strip()
            message = str(event.get("message") or "").strip()
            detail = ": ".join(
                value for value in (reason, message) if value
            )
            if not detail:
                continue
            timestamp = str(
                event.get("eventTime")
                or series.get("lastObservedTime")
                or event.get("lastTimestamp")
                or metadata.get("creationTimestamp")
                or ""
            )
            warnings.append((timestamp, detail))
        return tuple(detail for _, detail in sorted(warnings))

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

    def _stage_trial(
        self,
        workload: WorkloadSpec,
        claim_name: str,
        image: str,
        labels: dict[str, str],
    ) -> None:
        pod_name = native_resource_name(
            workload.workload_id,
            workload.trial,
            suffix="-stage",
        )
        self._apply(
            render_helper_pod(
                pod_name,
                claim_name,
                image,
                self.profile,
                labels,
            )
        )
        try:
            self._wait_for_pod(
                pod_name,
                self.profile.staging_timeout_seconds,
            )
            self._run(
                "cp",
                str(workload.trial.resolve()) + "/.",
                (
                    f"{self.profile.namespace}/{pod_name}:"
                    "/brunner/trial"
                ),
            )
        finally:
            self._delete("pod", pod_name)

    @staticmethod
    def _state_path(trial: Path) -> Path:
        return trial / "backend/kubernetes.json"

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        workload.validate()
        image = workload.image or self.profile.agent_image
        if not image:
            raise BackendRequestError(
                "Kubernetes workloads require an agent image"
            )
        state_path = self._state_path(workload.trial)
        if state_path.is_file():
            state = json.loads(state_path.read_text())
            handle = BackendHandle(
                backend=self.name,
                workload_id=workload.workload_id,
                native_id=str(state["native_id"]),
                trial=workload.trial.resolve(),
                metadata=dict(state["metadata"]),
            )
            self._handles[
                backend_registry_key(workload.workload_id, workload.trial)
            ] = handle
            return handle

        job_name = native_resource_name(
            workload.workload_id,
            workload.trial,
        )
        claim_name = native_resource_name(
            workload.workload_id,
            workload.trial,
            suffix="-data",
        )
        labels = {
            "app.kubernetes.io/name": "brunner",
            "dev.brunner/workload": job_name,
            **workload.labels,
        }
        self._apply(render_pvc(claim_name, self.profile, labels))
        self._stage_trial(workload, claim_name, image, labels)
        self._apply(
            render_job(
                job_name,
                claim_name,
                workload,
                self.profile,
                labels,
            )
        )
        handle = BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=job_name,
            trial=workload.trial.resolve(),
            metadata={
                "claim_name": claim_name,
                "namespace": self.profile.namespace,
                "submitted_at": _now(),
            },
        )
        write_json_atomic(
            state_path,
            {
                "schema_version": "1.0",
                **handle.to_dict(),
            },
        )
        self._handles[
            backend_registry_key(workload.workload_id, workload.trial)
        ] = handle
        return handle

    def _pod_for_handle(
        self,
        handle: BackendHandle,
    ) -> dict[str, Any] | None:
        value = self._get(
            "pods",
            labels=f"job-name={handle.native_id}",
        )
        if not value:
            return None
        items = value.get("items", [])
        return items[0] if items else None

    @staticmethod
    def _terminated_container(
        pod: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if pod is None:
            return None
        status = pod.get("status", {})
        for key in ("initContainerStatuses", "containerStatuses"):
            for item in status.get(key, []):
                terminated = item.get("state", {}).get("terminated")
                if terminated:
                    return {
                        "container": item.get("name"),
                        **terminated,
                    }
        return None

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
            return BackendSnapshot(
                phase="pending" if pvc is not None else "unknown",
                reason="JobMissing",
                message="workload job has not been created or was deleted",
                warnings=tuple(warnings),
            )
        pod = self._pod_for_handle(handle)
        terminated = self._terminated_container(pod)
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
        reason = None
        message = None
        exit_code = None
        if terminated and int(terminated.get("exitCode", 0)) != 0:
            phase = "failed"
            reason = terminated.get("reason") or "ContainerFailed"
            message = terminated.get("message")
            exit_code = terminated.get("exitCode")
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
            },
        )

    def logs(self, handle: BackendHandle) -> str:
        result = self._run(
            "logs",
            f"job/{handle.native_id}",
            "-n",
            self.profile.namespace,
            "--all-containers=true",
            "--prefix=true",
            check=False,
        )
        if result.returncode and "not found" not in result.stderr.lower():
            raise self._error(
                ("logs", f"job/{handle.native_id}"),
                result.returncode,
                result.stdout.encode(),
                result.stderr.encode(),
            )
        return result.stdout + result.stderr

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
            "dev.brunner/workload": handle.native_id,
            "dev.brunner/role": "artifact-reader",
        }
        self._apply(
            render_helper_pod(
                name,
                str(handle.metadata["claim_name"]),
                image,
                self.profile,
                labels,
                excluded_nodes=excluded_nodes,
            )
        )
        try:
            self._wait_for_pod(
                name,
                self.profile.reader_timeout_seconds,
            )
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
            self._delete("pod", name)
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
                    "included_groups": sorted(included_groups),
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
        )
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise IntegrityError(
                "remote artifact inventory is not an object"
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

    def _collect_from_reader(
        self,
        pod: str,
        destination: Path,
        policy: ArtifactPolicy,
        included_groups: frozenset[str],
    ) -> dict[str, Any]:
        inventory = self._remote_inventory(
            pod,
            policy,
            included_groups,
        )
        partial, complete = prepare_partial_artifacts(
            destination,
            inventory,
            policy,
            included_groups,
        )
        for name, expected in inventory.items():
            if name in complete:
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
                    count = min(CHUNK_BYTES, expected_size - offset)
                    data = self._read_remote(
                        pod,
                        name,
                        offset,
                        count,
                    )
                    if not data:
                        raise ArtifactTransferError(
                            f"remote artifact ended early: {name}"
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
        return finalize_artifact_collection(
            partial,
            destination,
            inventory,
            policy,
            included_groups=included_groups,
        )

    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        excluded_nodes: list[str] = []
        failures = []
        attempts = max(1, self.profile.reader_attempts)
        for attempt in range(1, attempts + 1):
            pod = None
            node = None
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
                )
                state_path = self._state_path(handle.trial)
                if state_path.is_file():
                    state = json.loads(state_path.read_text())
                    state["artifacts_collected_at"] = _now()
                    write_json_atomic(state_path, state)
                return result
            except BackendConnectivityError:
                raise
            except ReaderMountError as error:
                failures.append(f"attempt {attempt}: {error}")
                if error.node and error.node not in excluded_nodes:
                    excluded_nodes.append(error.node)
            except (
                ArtifactTransferError,
                BackendRequestError,
                IntegrityError,
            ) as error:
                failures.append(f"attempt {attempt}: {error}")
                if node and node not in excluded_nodes:
                    excluded_nodes.append(node)
            finally:
                if pod is not None:
                    try:
                        self._delete("pod", pod)
                    except BackendConnectivityError:
                        pass
        raise ArtifactTransferError(
            "artifact reader failed after retries; partial files and PVC "
            "were preserved: " + "; ".join(failures)
        )

    def cleanup(self, handle: BackendHandle) -> None:
        snapshot = self.inspect(handle)
        state_path = self._state_path(handle.trial)
        state = (
            json.loads(state_path.read_text())
            if state_path.is_file()
            else {}
        )
        self._delete("job", handle.native_id)
        if (
            snapshot.phase == "failed"
            and self.profile.retain_failed_storage
            and not state.get("artifacts_collected_at")
        ):
            state["storage_retained"] = True
            state["storage_retained_at"] = _now()
            if state_path.parent.is_dir():
                write_json_atomic(state_path, state)
            return
        self._delete(
            "pvc",
            str(handle.metadata["claim_name"]),
        )

    def capacity(self) -> BackendCapacity:
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
        available = (
            None
            if self.profile.max_parallel is None
            else max(
                0,
                self.profile.max_parallel - running - pending,
            )
        )
        return BackendCapacity(
            limit=self.profile.max_parallel,
            running=running,
            pending=pending,
            available=available,
            details={
                "namespace": self.profile.namespace,
                "checked_at": _now(),
            },
        )
