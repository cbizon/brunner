from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from brunner.backends import BackendHandle, BackendSnapshot, WorkloadSpec
from brunner.backends.base import native_resource_name
from brunner.backends.container import ContainerBackend
from brunner.backends.kubernetes import (
    KubernetesBackend,
    KubernetesProfile,
    ReaderMountError,
    render_helper_pod,
    render_job,
    render_pvc,
)
from brunner.definition import ArtifactPolicy
from brunner.errors import (
    BackendConnectivityError,
    BackendRequestError,
    IntegrityError,
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_kubernetes_resources_preserve_secret_boundary(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    profile = KubernetesProfile(
        namespace="benchmarks",
        agent_image="agent:latest",
        artifact_reader_image="reader:latest",
        storage_class_name="fast",
        secret_environment={
            "OPENAI_API_KEY": ("provider-credentials", "openai")
        },
        nonsecret_environment={
            "HTTPS_PROXY": "http://proxy.internal:3128",
        },
        node_selector={"pool": "bench"},
    )
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=61.2,
        cpu_request="2",
        cpu_limit="8",
        memory_request="16Gi",
        memory_limit="64Gi",
        ephemeral_storage_request="1Gi",
        ephemeral_storage_limit="3Gi",
    )
    labels = {"app.kubernetes.io/name": "brunner"}

    pvc = render_pvc("case-1-data", profile, labels)
    job = render_job("case-1", "case-1-data", workload, profile, labels)
    reader = render_helper_pod(
        "case-1-reader",
        "case-1-data",
        "reader:latest",
        profile,
        labels,
        excluded_nodes=("node-a",),
    )

    assert pvc["spec"]["storageClassName"] == "fast"
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["activeDeadlineSeconds"] == 62
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["terminationGracePeriodSeconds"] == 30
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod_spec["volumes"][-1] == {"name": "tmp", "emptyDir": {}}
    assert pod_spec["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert {"name": "tmp", "mountPath": "/tmp"} in (
        pod_spec["containers"][0]["volumeMounts"]
    )
    environment = pod_spec["containers"][0]["env"]
    secret = next(
        item for item in environment if item["name"] == "OPENAI_API_KEY"
    )
    proxy = next(
        item for item in environment if item["name"] == "HTTPS_PROXY"
    )
    termination_log = next(
        item
        for item in environment
        if item["name"] == "BRUNNER_TERMINATION_LOG"
    )
    assert secret["valueFrom"]["secretKeyRef"]["name"] == (
        "provider-credentials"
    )
    assert proxy["value"] == "http://proxy.internal:3128"
    assert termination_log["value"] == "/dev/termination-log"
    assert pod_spec["containers"][0]["resources"] == {
        "requests": {
            "cpu": "2",
            "memory": "16Gi",
            "ephemeral-storage": "1Gi",
        },
        "limits": {
            "cpu": "8",
            "memory": "64Gi",
            "ephemeral-storage": "3Gi",
        },
    }
    encoded = json.dumps(job)
    assert "provider-credentials" in encoded
    assert "OPENAI_API_KEY" in encoded
    assert "secret-value" not in encoded
    expression = reader["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]
    assert expression["operator"] == "NotIn"
    assert expression["values"] == ["node-a"]


def test_kubernetes_legacy_resources_remain_compatible(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="legacy",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
        cpu="500m",
        memory="4Gi",
    )

    job = render_job(
        "legacy",
        "legacy-data",
        workload,
        KubernetesProfile(namespace="benchmarks"),
        {"app.kubernetes.io/name": "brunner"},
    )

    resources = job["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    assert resources == {
        "requests": {"cpu": "500m", "memory": "4Gi"},
        "limits": {"cpu": "500m", "memory": "4Gi"},
    }


def test_kubernetes_legacy_requests_allow_explicit_burst_limits(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="burstable",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
        cpu="2",
        cpu_limit="8",
        memory="8Gi",
        memory_limit="32Gi",
    )

    job = render_job(
        "burstable",
        "burstable-data",
        workload,
        KubernetesProfile(namespace="benchmarks"),
        {"app.kubernetes.io/name": "brunner"},
    )

    resources = job["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    assert resources == {
        "requests": {"cpu": "2", "memory": "8Gi"},
        "limits": {"cpu": "8", "memory": "32Gi"},
    }


def test_kubernetes_legacy_storage_sets_request_and_limit(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="legacy-storage",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
        storage="2Gi",
    )

    job = render_job(
        "legacy-storage",
        "legacy-storage-data",
        workload,
        KubernetesProfile(namespace="benchmarks"),
        {"app.kubernetes.io/name": "brunner"},
    )

    resources = job["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    assert resources == {
        "requests": {"ephemeral-storage": "2Gi"},
        "limits": {"ephemeral-storage": "2Gi"},
    }


def test_kubernetes_helper_pod_uses_neutral_working_directory() -> None:
    profile = KubernetesProfile(
        namespace="benchmarks",
        agent_image="agent:latest",
    )

    helper = render_helper_pod(
        "case-1-stage",
        "case-1-data",
        "agent:latest",
        profile,
        {"dev.brunner/role": "trial-stager"},
    )

    container = helper["spec"]["containers"][0]
    assert container["workingDir"] == "/tmp"
    assert container["volumeMounts"] == [
        {"name": "trial", "mountPath": "/brunner/trial"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    assert helper["spec"]["automountServiceAccountToken"] is False
    assert helper["spec"]["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }


def test_native_resource_names_do_not_collapse_caller_ids(
    tmp_path: Path,
) -> None:
    trial_a = tmp_path / "campaign-a" / "trial"
    trial_b = tmp_path / "campaign-b" / "trial"

    names = {
        native_resource_name("A B", trial_a),
        native_resource_name("a-b", trial_a),
        native_resource_name("foo_bar", trial_a),
        native_resource_name("foo-bar", trial_a),
        native_resource_name("x" * 80 + "a", trial_a),
        native_resource_name("x" * 80 + "b", trial_a),
        native_resource_name("same-id", trial_a),
        native_resource_name("same-id", trial_b),
    }

    assert len(names) == 8
    assert all(len(name) <= 63 for name in names)


def test_container_recovers_handle_from_durable_trial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    state_path = trial / "backend/container.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "native_id": "persisted-container-id",
                "name": "persisted-container",
            }
        )
    )
    workload = WorkloadSpec(
        workload_id="same-id",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = ContainerBackend()
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *args, **kwargs: pytest.fail(
            "durable handle recovery must not query the runtime"
        ),
    )

    handle = backend.submit(workload)

    assert handle.native_id == "persisted-container-id"
    assert handle.metadata == {"name": "persisted-container"}
    assert not hasattr(backend, "_handles")


def test_container_submission_adopts_existing_named_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = ContainerBackend()
    calls = []

    def run(*arguments: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[0] != "inspect":
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(
                {
                    "Id": "existing-container-id",
                    "Config": {
                        "Labels": {"dev.brunner.workload": "case-1"}
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(backend, "_run", run)

    handle = backend.submit(workload)

    assert handle.native_id == "existing-container-id"
    assert calls == [
        (
            "inspect",
            "--format",
            "{{json .}}",
            native_resource_name("case-1", trial),
        )
    ]
    state = json.loads((trial / "backend/container.json").read_text())
    assert state["native_id"] == "existing-container-id"


def test_container_inherits_credentials_without_argv_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    backend = ContainerBackend(
        inherited_environment=("OPENAI_API_KEY",),
        nonsecret_environment={
            "HTTPS_PROXY": "http://proxy.internal:3128",
        },
    )
    commands = []

    def run(*arguments: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[0] == "inspect":
            return subprocess.CompletedProcess(
                arguments,
                1,
                stdout="",
                stderr="Error: No such object",
            )
        if arguments[0] == "run":
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout="new-container-id\n",
                stderr="",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(backend, "_run", run)

    handle = backend.submit(workload)

    assert handle.native_id == "new-container-id"
    run_arguments = commands[-1]
    assert "OPENAI_API_KEY" in run_arguments
    assert "OPENAI_API_KEY=super-secret-value" not in run_arguments
    assert not any("super-secret-value" in value for value in run_arguments)
    assert "HTTPS_PROXY=http://proxy.internal:3128" in run_arguments


def test_container_enforces_limits_and_ignores_scheduler_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
        cpu_request="500m",
        cpu_limit="2",
        memory_request="1Gi",
        memory_limit="4Gi",
    )
    backend = ContainerBackend()
    commands = []

    def run(
        *arguments: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[0] == "inspect":
            return subprocess.CompletedProcess(
                arguments,
                1,
                stdout="",
                stderr="Error: No such object",
            )
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout="container-id\n",
            stderr="",
        )

    monkeypatch.setattr(backend, "_run", run)

    backend.submit(workload)

    run_arguments = commands[-1]
    assert run_arguments[run_arguments.index("--cpus") + 1] == "2"
    assert run_arguments[run_arguments.index("--memory") + 1] == "4Gi"
    assert "500m" not in run_arguments
    assert "1Gi" not in run_arguments


def test_container_fails_when_inherited_credential_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = ContainerBackend(
        inherited_environment=("OPENAI_API_KEY",),
    )

    with pytest.raises(
        BackendRequestError,
        match="OPENAI_API_KEY",
    ):
        backend.submit(workload)


def test_kubernetes_submission_adopts_job_after_ambiguous_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    job_name = native_resource_name("case-1", trial)
    claim_name = native_resource_name("case-1", trial, suffix="-data")
    remote: dict[str, dict[str, object]] = {}
    staging_calls = 0
    job_apply_calls = 0

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object] | None:
        assert name is not None
        return remote.get(f"{kind}/{name}")

    def apply_resource(resource: dict[str, object]) -> None:
        nonlocal job_apply_calls
        kind = str(resource["kind"]).lower()
        if kind == "persistentvolumeclaim":
            kind = "pvc"
        metadata = resource["metadata"]
        assert isinstance(metadata, dict)
        name = str(metadata["name"])
        remote[f"{kind}/{name}"] = resource
        if kind == "job":
            job_apply_calls += 1
            if job_apply_calls == 1:
                raise BackendConnectivityError(
                    "connection dropped after Job creation"
                )

    def stage_trial(*args: object, **kwargs: object) -> None:
        nonlocal staging_calls
        staging_calls += 1

    def run_command(
        *arguments: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[:3] == ("annotate", "pvc", claim_name)
        pvc = remote[f"pvc/{claim_name}"]
        metadata = pvc["metadata"]
        assert isinstance(metadata, dict)
        metadata.setdefault("annotations", {})["dev.brunner/staged"] = "true"
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(backend, "_apply", apply_resource)
    monkeypatch.setattr(backend, "_stage_trial", stage_trial)
    monkeypatch.setattr(backend, "_run", run_command)

    with pytest.raises(BackendConnectivityError):
        backend.submit(workload)
    handle = backend.submit(workload)

    assert handle.native_id == job_name
    assert handle.metadata["claim_name"] == claim_name
    assert staging_calls == 1
    assert job_apply_calls == 1
    assert (trial / "backend/kubernetes.json").is_file()


@pytest.mark.parametrize(
    ("fault_boundary", "expected_staging_calls"),
    [
        ("pvc_created", 1),
        ("trial_staged", 2),
        ("pvc_annotated", 1),
    ],
)
def test_kubernetes_submission_recovers_each_pre_job_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_boundary: str,
    expected_staging_calls: int,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    job_name = native_resource_name("case-1", trial)
    claim_name = native_resource_name("case-1", trial, suffix="-data")
    remote: dict[str, dict[str, object]] = {}
    staging_calls = 0
    fault_injected = False

    def inject_once(boundary: str) -> None:
        nonlocal fault_injected
        if fault_boundary == boundary and not fault_injected:
            fault_injected = True
            raise BackendConnectivityError(
                f"orchestrator lost contact after {boundary}"
            )

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object] | None:
        assert name is not None
        return remote.get(f"{kind}/{name}")

    def apply_resource(resource: dict[str, object]) -> None:
        kind = str(resource["kind"]).lower()
        if kind == "persistentvolumeclaim":
            kind = "pvc"
        metadata = resource["metadata"]
        assert isinstance(metadata, dict)
        name = str(metadata["name"])
        remote[f"{kind}/{name}"] = resource
        if kind == "pvc":
            inject_once("pvc_created")

    def stage_trial(*args: object, **kwargs: object) -> None:
        nonlocal staging_calls
        staging_calls += 1
        inject_once("trial_staged")

    def run_command(
        *arguments: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[:3] == ("annotate", "pvc", claim_name)
        pvc = remote[f"pvc/{claim_name}"]
        metadata = pvc["metadata"]
        assert isinstance(metadata, dict)
        metadata.setdefault("annotations", {})["dev.brunner/staged"] = "true"
        inject_once("pvc_annotated")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(backend, "_apply", apply_resource)
    monkeypatch.setattr(backend, "_stage_trial", stage_trial)
    monkeypatch.setattr(backend, "_run", run_command)

    with pytest.raises(BackendConnectivityError):
        backend.submit(workload)
    handle = backend.submit(workload)

    assert handle.native_id == job_name
    assert remote[f"job/{job_name}"]["kind"] == "Job"
    assert staging_calls == expected_staging_calls
    assert (trial / "backend/kubernetes.json").is_file()


@pytest.mark.parametrize(
    ("job_reason", "container_reason", "exit_code", "expected"),
    [
        ("BackoffLimitExceeded", "Error", 137, True),
        ("DeadlineExceeded", "Error", 143, False),
        ("BackoffLimitExceeded", "StartError", 1, False),
    ],
)
def test_kubernetes_snapshot_classifies_infrastructure_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_reason: str,
    container_reason: str,
    exit_code: int,
    expected: bool,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="trial",
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    pod = {
        "spec": {"nodeName": "node-a"},
        "status": {
            "phase": "Failed",
            "containerStatuses": [
                {
                    "name": "agent",
                    "state": {
                        "terminated": {
                            "exitCode": exit_code,
                            "reason": container_reason,
                        }
                    },
                }
            ],
        },
    }

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        if kind == "pvc":
            return {"status": {"phase": "Bound"}}
        if kind == "job":
            return {
                "status": {
                    "conditions": [
                        {
                            "type": "Failed",
                            "reason": job_reason,
                            "message": "job failed",
                        }
                    ]
                }
            }
        if kind == "pods":
            return {"items": [pod]}
        raise AssertionError((kind, name, kwargs))

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_events",
        lambda name, uid, required=False: (),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "failed"
    assert snapshot.details["retryable_infrastructure"] is expected


@pytest.mark.parametrize(
    ("reason", "message", "expected_reason", "expected_retryable"),
    [
        ("OOMKilled", None, "OOMKilled", True),
        (
            "Completed",
            json.dumps(
                {
                    "brunner_pipeline": {
                        "status": "interrupted",
                        "provider_result_present": False,
                        "infrastructure_failure": True,
                        "infrastructure_reason": "AgentInterrupted",
                        "retryable_infrastructure": True,
                        "signal": 15,
                        "signal_name": "SIGTERM",
                    }
                }
            ),
            "AgentInterrupted",
            True,
        ),
        (
            "Completed",
            json.dumps(
                {
                    "brunner_pipeline": {
                        "status": "provider_error",
                        "provider_result_present": False,
                        "infrastructure_failure": True,
                        "infrastructure_reason": "AgentProviderError",
                        "retryable_infrastructure": False,
                    }
                }
            ),
            "AgentProviderError",
            False,
        ),
    ],
)
def test_kubernetes_complete_job_preserves_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    message: str | None,
    expected_reason: str,
    expected_retryable: bool,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="trial",
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    terminated = {"exitCode": 0, "reason": reason}
    if message is not None:
        terminated["message"] = message
    pod = {
        "metadata": {"name": "trial-pod", "uid": "pod-uid"},
        "spec": {"nodeName": "node-a"},
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "agent",
                    "state": {"terminated": terminated},
                }
            ],
        },
    }

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        if kind == "pvc":
            return {"status": {"phase": "Bound"}}
        if kind == "job":
            return {
                "metadata": {"name": "trial-job", "uid": "job-uid"},
                "status": {"conditions": [{"type": "Complete"}]},
            }
        if kind == "pods":
            return {"items": [pod]}
        raise AssertionError((kind, name, kwargs))

    events = {
        "trial-job": (
            {
                "type": "Normal",
                "reason": "Completed",
                "message": "Job completed",
            },
        ),
        "trial-pod": (
            {
                "type": "Warning",
                "reason": expected_reason,
                "message": "agent terminated",
            },
        ),
    }
    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_events",
        lambda name, uid, required=False: events.get(name, ()),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "failed"
    assert snapshot.reason == expected_reason
    assert snapshot.exit_code == 0
    assert (
        snapshot.details["retryable_infrastructure"]
        is expected_retryable
    )
    assert snapshot.details["kubernetes_events"] == {
        "job": list(events["trial-job"]),
        "pod": list(events["trial-pod"]),
    }
    assert f"{expected_reason}: agent terminated" in snapshot.warnings


def test_kubernetes_eviction_event_overrides_successful_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="trial",
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    pod = {
        "metadata": {"name": "trial-pod", "uid": "pod-uid"},
        "spec": {"nodeName": "node-a"},
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": "agent",
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "reason": "Completed",
                        }
                    },
                }
            ],
        },
    }
    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        if kind == "pvc":
            return {"status": {"phase": "Bound"}}
        if kind == "job":
            return {
                "metadata": {"name": "trial-job", "uid": "job-uid"},
                "status": {"conditions": [{"type": "Complete"}]},
            }
        if kind == "pods":
            return {"items": [pod]}
        raise AssertionError((kind, name, kwargs))

    events = {
        "trial-job": (
            {
                "type": "Normal",
                "reason": "Completed",
                "message": "Job completed",
            },
        ),
        "trial-pod": (
            {
                "type": "Warning",
                "reason": "Evicted",
                "message": (
                    "Pod ephemeral local storage usage exceeds "
                    "the total limit of containers 256Mi."
                ),
            },
        ),
    }
    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_events",
        lambda name, uid, required=False: events.get(name, ()),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "failed"
    assert snapshot.reason == "Evicted"
    assert snapshot.message == (
        "Pod ephemeral local storage usage exceeds "
        "the total limit of containers 256Mi."
    )
    assert snapshot.exit_code == 0
    assert snapshot.details["retryable_infrastructure"] is True
    assert snapshot.warnings == (
        "Evicted: Pod ephemeral local storage usage exceeds "
        "the total limit of containers 256Mi.",
    )


def test_kubernetes_missing_job_is_unknown_even_when_pvc_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="trial",
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    monkeypatch.setattr(
        backend,
        "_get",
        lambda kind, name=None, **kwargs: (
            {"status": {"phase": "Bound"}} if kind == "pvc" else None
        ),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "unknown"
    assert snapshot.reason == "JobMissing"
    assert snapshot.details["claim_phase"] == "Bound"


def test_kubernetes_restart_reuses_pvc_without_restaging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    workload_name = native_resource_name("case-1", trial)
    claim_name = native_resource_name("case-1", trial, suffix="-data")
    previous = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id=workload_name,
        trial=trial,
        metadata={"claim_name": claim_name},
    )
    deleted = []
    applied = []

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object] | None:
        if kind == "pvc":
            return {
                "metadata": {
                    "labels": {"dev.brunner/workload": workload_name}
                }
            }
        if kind == "job":
            return None
        raise AssertionError((kind, name, kwargs))

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: deleted.append((kind, name)),
    )
    monkeypatch.setattr(backend, "_apply", applied.append)
    monkeypatch.setattr(
        backend,
        "_stage_trial",
        lambda *args, **kwargs: pytest.fail("restart must not restage trial"),
    )

    restarted = backend.restart(previous, workload, 1)

    assert deleted == [("job", workload_name)]
    assert len(applied) == 1
    assert applied[0]["kind"] == "Job"
    assert restarted.native_id.endswith("-r1")
    assert restarted.metadata["claim_name"] == claim_name
    assert restarted.metadata["restart_generation"] == 1


@pytest.mark.parametrize("backend_type", ["container", "kubernetes"])
def test_runtime_connectivity_failures_are_distinct(
    tmp_path: Path,
    backend_type: str,
) -> None:
    binary = tmp_path / backend_type
    _write_executable(
        binary,
        "echo 'Unable to connect to the server: connection refused' >&2\n"
        "exit 1\n",
    )
    if backend_type == "container":
        backend = ContainerBackend(runtime=str(binary))
    else:
        backend = KubernetesBackend(
            KubernetesProfile(),
            kubectl=str(binary),
        )

    with pytest.raises(BackendConnectivityError):
        backend.capacity()


def test_kubernetes_dns_failure_is_connectivity_error() -> None:
    backend = KubernetesBackend(KubernetesProfile())

    error = backend._error(
        ("get", "jobs"),
        1,
        b"",
        b"Unable to connect to the server: dial tcp: no such host",
    )

    assert isinstance(error, BackendConnectivityError)


@pytest.mark.parametrize(
    "message",
    [
        "unexpected EOF",
        "HTTP/2: client connection lost",
        "Error from server (InternalError): internal server error",
        "502 Bad Gateway",
        "429 Too Many Requests",
    ],
)
def test_kubernetes_transient_api_failures_are_connectivity_errors(
    message: str,
) -> None:
    backend = KubernetesBackend(KubernetesProfile())

    error = backend._error(
        ("get", "jobs"),
        1,
        b"",
        message.encode(),
    )

    assert isinstance(error, BackendConnectivityError)


def test_kubernetes_ambiguous_failure_probes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = KubernetesBackend(KubernetesProfile())
    monkeypatch.setattr(
        backend,
        "_probe_backend_reachable",
        lambda: False,
    )

    disconnected = backend._error(
        ("get", "jobs"),
        1,
        b"",
        b"transport closed without a status",
    )

    monkeypatch.setattr(
        backend,
        "_probe_backend_reachable",
        lambda: True,
    )
    rejected = backend._error(
        ("apply", "-f", "-"),
        1,
        b"",
        b"admission webhook rejected this object",
    )

    assert isinstance(disconnected, BackendConnectivityError)
    assert isinstance(rejected, BackendRequestError)


def test_kubernetes_warning_events_tolerate_null_optional_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    payload = {
        "items": [
            {
                "type": "Warning",
                "reason": "FailedMount",
                "message": "storage aggregate is offline",
                "series": None,
                "metadata": None,
            }
        ]
    }
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    warnings = backend._warning_events("reader", "reader-uid")

    assert warnings == (
        "FailedMount: storage aggregate is offline",
    )


def test_kubernetes_terminal_event_failure_is_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="Error from server (Forbidden): events is forbidden",
        ),
    )

    with pytest.raises(BackendRequestError, match="events is forbidden"):
        backend._events("trial-job", "job-uid", required=True)


def test_kubernetes_snapshot_includes_pending_pvc_warning_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="trial",
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        if kind == "pvc":
            return {
                "metadata": {"uid": "claim-uid"},
                "status": {"phase": "Pending"},
            }
        if kind == "job":
            return {"status": {}}
        if kind == "pods":
            return {"items": []}
        raise AssertionError((kind, name, kwargs))

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_warning_events",
        lambda name, uid: (
            "ProvisioningFailed: containing aggregate is not online",
        ),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "pending"
    assert any(
        "ProvisioningFailed: containing aggregate is not online" in warning
        for warning in snapshot.warnings
    )


def test_artifact_reader_failure_includes_kubernetes_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        )
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="trial",
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    monkeypatch.setattr(backend, "_apply", lambda resource: None)
    monkeypatch.setattr(
        backend,
        "_wait_for_pod",
        lambda name, timeout: (_ for _ in ()).throw(
            BackendRequestError("reader did not become ready")
        ),
    )
    monkeypatch.setattr(
        backend,
        "_get",
        lambda *args, **kwargs: {
            "metadata": {"uid": "reader-uid"},
            "spec": {"nodeName": "node-a"},
        },
    )
    monkeypatch.setattr(
        backend,
        "_warning_events",
        lambda name, uid: (
            "FailedMount: storage aggregate is offline",
        ),
    )
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: None,
    )

    with pytest.raises(
        ReaderMountError,
        match="FailedMount: storage aggregate is offline",
    ):
        backend._reader(handle, 1, ())


def test_kubernetes_deletes_stale_helpers_by_labels_and_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    selectors = []
    deleted = []

    def get_resource(
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        assert kind == "pods"
        assert name is None
        selectors.append(labels)
        return {
            "items": [
                {"metadata": {"name": "stale-reader"}},
                {"metadata": {"name": "stale-reader"}},
            ]
        }

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: deleted.append((kind, name)),
    )

    backend._delete_helper_pods(
        ("workload", "workload-r1"),
        "artifact-reader",
    )

    assert selectors == [
        (
            "dev.brunner/workload=workload,"
            "dev.brunner/role=artifact-reader"
        ),
        (
            "dev.brunner/workload=workload-r1,"
            "dev.brunner/role=artifact-reader"
        ),
    ]
    assert deleted == [("pod", "stale-reader")]


def test_kubernetes_stage_labels_and_waits_for_helper_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    workload_name = native_resource_name("case-1", trial)
    claim_name = native_resource_name("case-1", trial, suffix="-data")
    cleaned = []
    resources = []
    deleted = []
    monkeypatch.setattr(
        backend,
        "_delete_helper_pods",
        lambda names, role: cleaned.append((names, role)),
    )
    monkeypatch.setattr(backend, "_apply", resources.append)
    monkeypatch.setattr(
        backend,
        "_wait_for_pod",
        lambda name, timeout: None,
    )
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            "",
            "",
        ),
    )
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: deleted.append((kind, name)),
    )

    backend._stage_trial(
        workload,
        claim_name,
        "agent:latest",
        {
            "app.kubernetes.io/name": "brunner",
            "dev.brunner/workload": workload_name,
        },
    )

    assert cleaned == [((workload_name,), "trial-stager")]
    assert resources[0]["metadata"]["labels"]["dev.brunner/role"] == (
        "trial-stager"
    )
    assert deleted == [
        (
            "pod",
            native_resource_name(
                "case-1",
                trial,
                suffix="-stage",
            ),
        ),
        (
            "pod",
            native_resource_name(
                "case-1",
                trial,
                suffix="-stage",
            ),
        ),
    ]


def test_kubernetes_collect_surfaces_reader_cleanup_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
            reader_attempts=1,
        )
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id="case-1-job",
        trial=trial,
        metadata={"claim_name": "case-1-data"},
    )
    monkeypatch.setattr(
        backend,
        "_delete_helper_pods",
        lambda names, role: None,
    )
    monkeypatch.setattr(
        backend,
        "_reader",
        lambda *args, **kwargs: ("reader-pod", "node-a"),
    )
    monkeypatch.setattr(
        backend,
        "_collect_from_reader",
        lambda *args, **kwargs: {"files": []},
    )
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: (_ for _ in ()).throw(
            BackendConnectivityError("cleanup connection dropped")
        ),
    )

    with pytest.raises(
        BackendConnectivityError,
        match="cleanup connection dropped",
    ):
        backend.collect(
            handle,
            tmp_path / "collected",
            policy=ArtifactPolicy(),
        )


def test_kubernetes_collection_does_not_retry_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
            reader_attempts=3,
        )
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id="case-1-job",
        trial=trial,
        metadata={"claim_name": "case-1-data"},
    )
    reader_calls = 0

    def reader(*args: object, **kwargs: object) -> tuple[str, str]:
        nonlocal reader_calls
        reader_calls += 1
        return f"reader-{reader_calls}", "node-a"

    monkeypatch.setattr(
        backend,
        "_delete_helper_pods",
        lambda names, role: None,
    )
    monkeypatch.setattr(backend, "_reader", reader)
    monkeypatch.setattr(
        backend,
        "_collect_from_reader",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            IntegrityError("artifact checksum mismatch")
        ),
    )
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: None,
    )

    with pytest.raises(IntegrityError, match="checksum mismatch"):
        backend.collect(
            handle,
            tmp_path / "collected",
            policy=ArtifactPolicy(),
        )

    assert reader_calls == 1


def test_kubernetes_remote_inventory_rejects_malformed_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    monkeypatch.setattr(
        backend,
        "_run_bytes",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            b"not-json",
            b"",
        ),
    )

    with pytest.raises(IntegrityError, match="not valid JSON"):
        backend._remote_inventory(
            "reader",
            ArtifactPolicy(),
            frozenset(),
        )


def test_kubernetes_cleanup_waits_for_helpers_job_and_pvc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id="case-1-r1",
        trial=trial,
        metadata={"claim_name": "case-1-data"},
    )
    helper_cleanup = []
    deleted = []
    monkeypatch.setattr(
        backend,
        "inspect",
        lambda value: BackendSnapshot(phase="succeeded"),
    )
    monkeypatch.setattr(
        backend,
        "_delete_helper_pods",
        lambda names, role: helper_cleanup.append((names, role)),
    )
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: deleted.append((kind, name)),
    )

    backend.cleanup(handle)

    stable_name = native_resource_name("case-1", trial)
    assert helper_cleanup == [
        ((stable_name, "case-1-r1"), "trial-stager"),
        ((stable_name, "case-1-r1"), "artifact-reader"),
    ]
    assert deleted == [
        (
            "pod",
            native_resource_name(
                "case-1",
                trial,
                suffix="-stage",
            ),
        ),
        ("job", "case-1-r1"),
        ("pvc", "case-1-data"),
    ]


def test_kubernetes_cleanup_surfaces_helper_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id="case-1-job",
        trial=trial,
        metadata={"claim_name": "case-1-data"},
    )
    monkeypatch.setattr(
        backend,
        "inspect",
        lambda value: BackendSnapshot(phase="succeeded"),
    )
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: (_ for _ in ()).throw(
            BackendConnectivityError("cleanup connection dropped")
        ),
    )

    with pytest.raises(
        BackendConnectivityError,
        match="cleanup connection dropped",
    ):
        backend.cleanup(handle)
