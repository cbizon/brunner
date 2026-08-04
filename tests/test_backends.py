from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from brunner.backends import BackendHandle, WorkloadSpec
from brunner.backends.base import backend_registry_key, native_resource_name
from brunner.backends.container import ContainerBackend
from brunner.backends.kubernetes import (
    KubernetesBackend,
    KubernetesProfile,
    ReaderMountError,
    render_helper_pod,
    render_job,
    render_pvc,
)
from brunner.errors import BackendConnectivityError, BackendRequestError


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
        node_selector={"pool": "bench"},
    )
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=61.2,
        cpu="2",
        memory="4Gi",
        environment={"NON_SECRET": "value"},
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
    assert secret["valueFrom"]["secretKeyRef"]["name"] == (
        "provider-credentials"
    )
    encoded = json.dumps(job)
    assert "provider-credentials" in encoded
    assert "OPENAI_API_KEY" in encoded
    assert "secret-value" not in encoded
    expression = reader["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]
    assert expression["operator"] == "NotIn"
    assert expression["values"] == ["node-a"]


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


def test_backend_registry_keeps_same_id_from_different_trials(
    tmp_path: Path,
) -> None:
    keys = {
        backend_registry_key(
            "same-id",
            tmp_path / campaign / "same-id",
        )
        for campaign in ("campaign-a", "campaign-b")
    }

    assert len(keys) == 2


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

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "failed"
    assert snapshot.details["retryable_infrastructure"] is expected


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
    monkeypatch.setattr(backend, "_delete", lambda kind, name: None)

    with pytest.raises(
        ReaderMountError,
        match="FailedMount: storage aggregate is offline",
    ):
        backend._reader(handle, 1, ())
