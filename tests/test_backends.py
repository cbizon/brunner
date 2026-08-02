from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brunner.backends import BackendHandle, LocalBackend, WorkloadSpec
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
from brunner.errors import BackendConnectivityError, BackendRequestError


ROOT = Path(__file__).parents[1]


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_local_backend_runs_detached_workload_and_collects(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    workspace = trial / "workspace"
    workspace.mkdir(parents=True)
    backend = LocalBackend(max_parallel=1)
    workload = WorkloadSpec(
        workload_id="local-example",
        trial=trial,
        command=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path;"
                "Path('result.txt').write_text('complete\\n');"
                "print('finished')"
            ),
        ),
        timeout_seconds=10,
        environment={
            "PYTHONPATH": str(ROOT / "src")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        },
    )

    handle = backend.submit(workload)
    deadline = time.monotonic() + 10
    snapshot = backend.inspect(handle)
    while not snapshot.terminal and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = backend.inspect(handle)

    assert snapshot.phase == "succeeded"
    assert "finished" in backend.logs(handle)
    destination = tmp_path / "collected"
    report = backend.collect(
        handle,
        destination,
        ArtifactPolicy(excluded_globs=("backend/**",)),
    )
    assert report["files"] >= 1
    assert (
        destination / "workspace/result.txt"
    ).read_text() == "complete\n"
    backend.cleanup(handle)
    assert backend.inspect(handle).phase == "cleaned"


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
    backend = LocalBackend(max_parallel=2)
    handles = []
    for campaign in ("campaign-a", "campaign-b"):
        trial = tmp_path / campaign / "same-id"
        (trial / "workspace").mkdir(parents=True)
        handles.append(
            backend.submit(
                WorkloadSpec(
                    workload_id="same-id",
                    trial=trial,
                    command=(
                        sys.executable,
                        "-c",
                        "import time; time.sleep(0.3)",
                    ),
                    timeout_seconds=2,
                )
            )
        )

    capacity = backend.capacity()

    assert capacity.running + capacity.pending == 2
    assert capacity.available == 0
    for handle in handles:
        deadline = time.monotonic() + 2
        while (
            not backend.inspect(handle).terminal
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        backend.cleanup(handle)


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


def test_local_backend_detects_worker_that_never_started(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    backend = LocalBackend()
    # A worker command that dies before it can record worker_pid: this used to
    # leave the trial reporting "pending" forever.
    handle = backend.submit(
        WorkloadSpec(
            workload_id="broken",
            trial=trial,
            command=(sys.executable, "-c", "raise SystemExit(9)"),
            timeout_seconds=30,
        )
    )
    state_path = trial / "backend/local/state.json"
    state = json.loads(state_path.read_text())
    state.pop("worker_pid", None)
    state["phase"] = "pending"
    state_path.write_text(json.dumps(state))
    launcher_path = trial / "backend/local/launcher.json"
    launcher = json.loads(launcher_path.read_text())
    launcher["exit_code"] = 9
    launcher_path.write_text(json.dumps(launcher))

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "failed"
    assert snapshot.reason == "WorkerStartFailed"


def test_local_backend_records_launcher_pid_and_worker_log(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    backend = LocalBackend()
    backend.submit(
        WorkloadSpec(
            workload_id="logged",
            trial=trial,
            command=(sys.executable, "-c", "pass"),
            timeout_seconds=30,
        )
    )

    launcher = json.loads(
        (trial / "backend/local/launcher.json").read_text()
    )

    assert isinstance(launcher["launcher_pid"], int)
    assert (trial / "backend/local/worker.log").is_file()


def test_local_backend_tolerates_worker_still_starting(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    root = trial / "backend/local"
    root.mkdir(parents=True)
    # State written by submit() but neither the worker nor the launcher record
    # has landed yet: that window must not be reported as a failure.
    (root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "backend": "local",
                "workload_id": "starting",
                "phase": "pending",
                "submitted_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    backend = LocalBackend()
    handle = BackendHandle(
        backend="local",
        workload_id="starting",
        native_id="starting",
        trial=trial,
    )

    assert backend.inspect(handle).phase == "pending"


def test_local_backend_reports_worker_that_never_launched(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    root = trial / "backend/local"
    root.mkdir(parents=True)
    stale = datetime.now(UTC) - timedelta(seconds=600)
    (root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "backend": "local",
                "workload_id": "orphan",
                "phase": "pending",
                "submitted_at": stale.isoformat(),
            }
        )
    )
    backend = LocalBackend()
    handle = BackendHandle(
        backend="local",
        workload_id="orphan",
        native_id="orphan",
        trial=trial,
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "failed"
    assert snapshot.reason == "WorkerMissing"
