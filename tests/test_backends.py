from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from brunner.backends import LocalBackend, WorkloadSpec
from brunner.backends.container import ContainerBackend
from brunner.backends.kubernetes import (
    KubernetesBackend,
    KubernetesProfile,
    render_helper_pod,
    render_job,
    render_pvc,
)
from brunner.definition import ArtifactPolicy
from brunner.errors import BackendConnectivityError


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
