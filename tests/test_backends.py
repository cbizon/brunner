from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from brunner.artifacts import artifact_metadata
from brunner.backends import (
    BackendHandle,
    BackendSnapshot,
    TrustedEvaluationSpec,
    WorkloadSpec,
)
from brunner.backends.base import native_resource_name
from brunner.backends.container import ContainerBackend
from brunner.backends.kubernetes import (
    KubernetesBackend,
    KubernetesProfile as ProductionKubernetesProfile,
    ReaderMountError,
    render_collection_job,
    render_helper_pod,
    render_job,
    render_pvc,
)
from brunner.definition import ArtifactPolicy
from brunner.errors import (
    ArtifactTransferPending,
    BackendConfigurationError,
    BackendConnectivityError,
    BackendRequestError,
    IntegrityError,
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def KubernetesProfile(**kwargs: object) -> ProductionKubernetesProfile:
    kwargs.setdefault("require_image_digests", False)
    kwargs.setdefault("preflight_enabled", False)
    kwargs.setdefault("unsafe_disable_network_policy_for_tests", True)
    return ProductionKubernetesProfile(**kwargs)


def _write_stage_marker(trial: Path) -> None:
    workspace = trial / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".brunner-challenge.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "workspace": str(workspace),
                "challenge_sha256": "a" * 64,
                "contract_sha256": "b" * 64,
                "benchmark_id": "test",
                "benchmark_version": "1.0",
                "file_inventory": {},
            }
        )
    )


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
        cpu_request="2",
        cpu_limit="8",
        memory_request="16Gi",
        memory_limit="64Gi",
        ephemeral_storage_request="1Gi",
        ephemeral_storage_limit="3Gi",
    )
    labels = {"app.kubernetes.io/name": "brunner"}

    pvc = render_pvc("case-1-data", profile, labels)
    job = render_job(
        "case-1",
        "case-1-data",
        workload,
        profile,
        labels,
        proxy_url="http://10.96.4.12:3128",
    )
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
    assert job["spec"]["backoffLimit"] == 0
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
    assert proxy["value"] == "http://10.96.4.12:3128"
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


def test_kubernetes_job_uses_only_workload_provider_secret(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    profile = KubernetesProfile(
        agent_image="agent:latest",
        secret_environment={
            "SHARED_CERTIFICATE": ("shared-settings", "certificate")
        },
    )
    workload = WorkloadSpec(
        workload_id="codex",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=60,
        secret_environment={
            "OPENAI_API_KEY": ("codex-credentials", "OPENAI_API_KEY")
        },
    )

    job = render_job(
        "codex",
        "codex-data",
        workload,
        profile,
        {"app.kubernetes.io/name": "brunner"},
    )

    encoded = json.dumps(job)
    assert "codex-credentials" in encoded
    assert "OPENAI_API_KEY" in encoded
    assert "shared-settings" in encoded
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in encoded
    assert "claude-credentials" not in encoded


def test_workload_secret_references_affect_identity_without_values(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    baseline = WorkloadSpec(
        workload_id="codex",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=60,
    )
    credentialed = WorkloadSpec(
        workload_id="codex",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=60,
        secret_environment={
            "OPENAI_API_KEY": ("codex-credentials", "api-key")
        },
    )

    assert baseline.sha256 != credentialed.sha256


def test_kubernetes_secret_references_do_not_read_laptop_or_cluster_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
    )
    workload = WorkloadSpec(
        workload_id="codex",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=60,
        secret_environment={
            "OPENAI_API_KEY": ("codex-credentials", "api-key")
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "local-secret-value")
    monkeypatch.setattr(
        backend,
        "_get",
        lambda *args, **kwargs: pytest.fail(
            "controller must not read Kubernetes Secrets"
        ),
    )
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *args, **kwargs: pytest.fail(
            "controller must not provision Kubernetes Secrets"
        ),
    )

    backend._ensure_workload_secrets(workload)


def test_kubernetes_secret_reference_validation_needs_no_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(KubernetesProfile())
    workload = WorkloadSpec(
        workload_id="claude",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=60,
        secret_environment={
            "CLAUDE_CODE_OAUTH_TOKEN": (
                "claude-credentials",
                "oauth-token",
            )
        },
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    backend._ensure_workload_secrets(workload)


def test_kubernetes_rejects_ambiguous_secret_key_reuse(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
    )
    workload = WorkloadSpec(
        workload_id="codex",
        trial=trial,
        command=("brunner-worker",),
        timeout_seconds=60,
        secret_environment={
            "OPENAI_API_KEY": ("provider-credentials", "token"),
            "AZURE_OPENAI_API_KEY": ("provider-credentials", "token"),
        },
    )

    with pytest.raises(
        BackendConfigurationError,
        match="cannot be provisioned unambiguously",
    ):
        backend._ensure_workload_secrets(workload)


def test_kubernetes_pipeline_runs_evaluator_after_agent_without_secrets(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    _write_stage_marker(trial)
    profile = KubernetesProfile(
        namespace="benchmarks",
        agent_image="agent:latest",
        reference_claim_name="benchmark-reference",
        secret_environment={
            "OPENAI_API_KEY": ("provider-credentials", "openai")
        },
    )
    workload = WorkloadSpec(
        workload_id="pipeline",
        trial=trial,
        command=("python", "-m", "brunner.agent_cli", "/brunner/trial"),
        timeout_seconds=61.2,
        evaluation=TrustedEvaluationSpec(
            benchmark_id="benchmark",
            benchmark_version="1.0",
            contract_sha256="abc123",
            image="evaluator:latest",
            command=("python", "-m", "benchmark.evaluator"),
            results_path="evaluation/custom-results.json",
            timeout_seconds=120,
            reference_manifest_path="manifest.json",
            reference_manifest_sha256="c" * 64,
            cpu_request="3",
            cpu_limit="8",
            memory_request="16Gi",
            memory_limit="64Gi",
        ),
    )

    job = render_job(
        "pipeline",
        "pipeline-data",
        workload,
        profile,
        {"app.kubernetes.io/name": "brunner"},
        proxy_url="http://10.96.4.12:3128",
    )

    pod = job["spec"]["template"]["spec"]
    agent = pod["initContainers"][0]
    evaluator = pod["containers"][0]
    assert pod["activeDeadlineSeconds"] == 182
    assert agent["name"] == "agent"
    assert evaluator["name"] == "evaluator"
    assert evaluator["image"] == "evaluator:latest"
    assert evaluator["command"] == [
        "python",
        "-m",
        "brunner.evaluation_cli",
        "/brunner/trial",
    ]
    assert evaluator["workingDir"] == "/tmp"
    assert pod["enableServiceLinks"] is False
    agent_environment = {item["name"] for item in agent["env"]}
    evaluator_environment = {
        item["name"] for item in evaluator["env"]
    }
    assert "OPENAI_API_KEY" in agent_environment
    assert "HTTPS_PROXY" in agent_environment
    assert "OPENAI_API_KEY" not in evaluator_environment
    assert "HTTPS_PROXY" not in evaluator_environment
    assert "PYTHONSAFEPATH" in evaluator_environment
    assert "PYTHONNOUSERSITE" in evaluator_environment
    encoded_spec = next(
        item["value"]
        for item in evaluator["env"]
        if item["name"] == "BRUNNER_EVALUATION_SPEC"
    )
    assert json.loads(encoded_spec)["command"] == [
        "python",
        "-m",
        "benchmark.evaluator",
    ]
    assert json.loads(encoded_spec)["results_path"] == (
        "evaluation/custom-results.json"
    )
    handle = KubernetesBackend(profile)._submission_handle(
        workload,
        job_name="pipeline",
        claim_name="pipeline-data",
    )
    assert handle.metadata["evaluation_results_path"] == (
        "evaluation/custom-results.json"
    )
    assert handle.metadata["network_isolation_mode"] == "strict"
    assert {
        mount["name"]: mount
        for mount in evaluator["volumeMounts"]
    }["reference"]["readOnly"] is True
    assert all(
        mount["name"] != "reference"
        for mount in agent["volumeMounts"]
    )
    reference = next(
        volume
        for volume in pod["volumes"]
        if volume["name"] == "reference"
    )
    assert reference["persistentVolumeClaim"] == {
        "claimName": "benchmark-reference",
        "readOnly": True,
    }
    assert evaluator["resources"] == {
        "requests": {"cpu": "3", "memory": "16Gi"},
        "limits": {"cpu": "8", "memory": "64Gi"},
    }


def test_kubernetes_pipeline_requires_configured_reference_claim(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    workload = WorkloadSpec(
        workload_id="pipeline",
        trial=trial,
        command=("agent",),
        timeout_seconds=60,
        image="agent:latest",
        evaluation=TrustedEvaluationSpec(
            benchmark_id="benchmark",
            benchmark_version="1.0",
            contract_sha256="abc123",
            image="evaluator:latest",
            command=("evaluate",),
            results_path="evaluation/results.json",
            timeout_seconds=60,
            reference_manifest_path="manifest.json",
        ),
    )

    with pytest.raises(
        BackendRequestError,
        match="reference_claim_name",
    ):
        render_job(
            "pipeline",
            "pipeline-data",
            workload,
            KubernetesProfile(namespace="benchmarks"),
            {"app.kubernetes.io/name": "brunner"},
        )


@pytest.mark.parametrize("chunk_bytes", (0, -1))
def test_kubernetes_profile_rejects_invalid_artifact_chunk_size(
    chunk_bytes: int,
) -> None:
    with pytest.raises(ValueError, match="artifact_chunk_bytes"):
        KubernetesProfile(artifact_chunk_bytes=chunk_bytes)


@pytest.mark.parametrize("chunk_attempts", (0, -1))
def test_kubernetes_profile_rejects_invalid_artifact_chunk_attempts(
    chunk_attempts: int,
) -> None:
    with pytest.raises(ValueError, match="artifact_chunk_attempts"):
        KubernetesProfile(artifact_chunk_attempts=chunk_attempts)


def test_kubernetes_profile_rejects_negative_chunk_retry_delay() -> None:
    with pytest.raises(ValueError, match="artifact_chunk_retry_seconds"):
        KubernetesProfile(artifact_chunk_retry_seconds=-1)


def test_kubernetes_collection_uses_configured_artifact_chunk_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"abcdefghij"
    inventory = {
        "result.bin": {
            "type": "file",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    }
    reads: list[tuple[int, int]] = []
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_chunk_bytes=4,
        )
    )
    monkeypatch.setattr(
        backend,
        "_remote_inventory",
        lambda *args, **kwargs: inventory,
    )
    monkeypatch.setattr(
        backend,
        "_probe_backend_reachable",
        lambda: True,
    )

    def read_remote(
        pod: str,
        relative_path: str,
        offset: int,
        count: int,
    ) -> bytes:
        reads.append((offset, count))
        return payload[offset : offset + count]

    monkeypatch.setattr(backend, "_read_remote", read_remote)
    destination = tmp_path / "collected"

    result = backend._collect_from_reader(
        "reader",
        destination,
        ArtifactPolicy(),
        frozenset(),
    )

    assert reads == [(0, 4), (4, 4), (8, 2)]
    assert (destination / "result.bin").read_bytes() == payload
    assert result["files"] == 1


def test_kubernetes_collection_reuses_unchanged_staged_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    staged = trial / "workspace/large-input.bin"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"x" * 4096)
    staged_metadata = artifact_metadata(staged)
    assert staged_metadata is not None
    marker = trial / "workspace/.brunner-challenge.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "file_inventory": {
                    "large-input.bin": staged_metadata.to_dict(),
                },
            }
        )
    )
    output = b"complete\n"
    inventory = {
        "workspace/large-input.bin": staged_metadata.to_dict(),
        "status.json": {
            "type": "file",
            "size": len(output),
            "sha256": hashlib.sha256(output).hexdigest(),
        },
    }
    reads = []
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_chunk_bytes=4,
        )
    )
    monkeypatch.setattr(
        backend,
        "_remote_inventory",
        lambda *args, **kwargs: inventory,
    )

    def read_remote(
        pod: str,
        relative_path: str,
        offset: int,
        count: int,
    ) -> bytes:
        reads.append((relative_path, offset, count))
        assert relative_path == "status.json"
        return output[offset : offset + count]

    monkeypatch.setattr(backend, "_read_remote", read_remote)
    destination = tmp_path / "collected"

    result = backend._collect_from_reader(
        "reader",
        destination,
        ArtifactPolicy(max_collection_bytes=32),
        frozenset(),
        trial,
    )

    assert reads == [
        ("status.json", 0, 4),
        ("status.json", 4, 4),
        ("status.json", 8, 1),
    ]
    assert (destination / "workspace/large-input.bin").samefile(staged)
    assert result["reused_staged_files"] == 1
    assert result["transferred_bytes"] == len(output)


def test_kubernetes_collection_caps_changed_staged_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    staged = trial / "workspace/large-input.bin"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"x" * 4096)
    staged_metadata = artifact_metadata(staged)
    assert staged_metadata is not None
    (trial / "workspace/.brunner-challenge.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "file_inventory": {
                    "large-input.bin": staged_metadata.to_dict(),
                },
            }
        )
    )
    changed = b"y" * 4096
    inventory = {
        "workspace/large-input.bin": {
            "type": "file",
            "size": len(changed),
            "sha256": hashlib.sha256(changed).hexdigest(),
        }
    }
    backend = KubernetesBackend(
        KubernetesProfile(namespace="benchmarks")
    )
    monkeypatch.setattr(
        backend,
        "_remote_inventory",
        lambda *args, **kwargs: inventory,
    )
    monkeypatch.setattr(
        backend,
        "_read_remote",
        lambda *args, **kwargs: pytest.fail(
            "oversized changed input must be rejected before transfer"
        ),
    )

    with pytest.raises(IntegrityError, match="exceeding the configured"):
        backend._collect_from_reader(
            "reader",
            tmp_path / "collected",
            ArtifactPolicy(
                max_collection_bytes=32,
                max_diagnostic_collection_bytes=32,
            ),
            frozenset(),
            trial,
        )


def test_kubernetes_collection_retries_chunk_on_same_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"abcdefghij"
    inventory = {
        "result.bin": {
            "type": "file",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    }
    reads: list[tuple[str, int, int]] = []
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_chunk_bytes=4,
            artifact_chunk_attempts=3,
            artifact_chunk_retry_seconds=0,
        )
    )
    monkeypatch.setattr(
        backend,
        "_remote_inventory",
        lambda *args, **kwargs: inventory,
    )
    failed_once = False

    def read_remote(
        pod: str,
        relative_path: str,
        offset: int,
        count: int,
    ) -> bytes:
        nonlocal failed_once
        reads.append((pod, offset, count))
        if offset == 4 and not failed_once:
            failed_once = True
            raise BackendConnectivityError(
                "transient kubectl stream reset"
            )
        return payload[offset : offset + count]

    monkeypatch.setattr(backend, "_read_remote", read_remote)
    monkeypatch.setattr(
        backend,
        "_probe_backend_reachable",
        lambda: True,
    )
    destination = tmp_path / "collected"

    backend._collect_from_reader(
        "reader",
        destination,
        ArtifactPolicy(),
        frozenset(),
    )

    assert reads == [
        ("reader", 0, 4),
        ("reader", 4, 4),
        ("reader", 4, 4),
        ("reader", 8, 2),
    ]
    assert (destination / "result.bin").read_bytes() == payload


def test_kubernetes_chunk_retry_preserves_backend_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = KubernetesBackend(
        KubernetesProfile(
            artifact_chunk_attempts=3,
            artifact_chunk_retry_seconds=0,
        )
    )
    reads = 0

    def read_remote(*args, **kwargs) -> bytes:
        nonlocal reads
        reads += 1
        raise BackendConnectivityError("cluster unavailable")

    monkeypatch.setattr(backend, "_read_remote", read_remote)
    monkeypatch.setattr(
        backend,
        "_probe_backend_reachable",
        lambda: False,
    )

    with pytest.raises(BackendConnectivityError, match="unavailable"):
        backend._read_remote_with_retries(
            "reader",
            "result.bin",
            0,
            4,
        )

    assert reads == 1


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


def test_kubernetes_artifact_reader_mounts_trial_read_only() -> None:
    helper = render_helper_pod(
        "case-1-reader",
        "case-1-data",
        "reader:latest",
        KubernetesProfile(namespace="benchmarks"),
        {"dev.brunner/role": "artifact-reader"},
        trial_read_only=True,
    )

    assert helper["spec"]["containers"][0]["volumeMounts"][0] == {
        "name": "trial",
        "mountPath": "/brunner/trial",
        "readOnly": True,
    }
    assert helper["spec"]["volumes"][0]["persistentVolumeClaim"] == {
        "claimName": "case-1-data",
        "readOnly": True,
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
    _write_stage_marker(trial)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
    )
    job_name = native_resource_name("case-1", trial)
    claim_name = native_resource_name("case-1", trial, suffix="-data")
    remote: dict[str, dict[str, object]] = {}
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

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(backend, "_apply", apply_resource)

    with pytest.raises(BackendConnectivityError):
        backend.submit(workload)
    handle = backend.submit(workload)

    assert handle.native_id == job_name
    assert handle.metadata["claim_name"] == claim_name
    assert job_apply_calls == 1
    stager = remote[f"job/{job_name}"]["spec"]["template"]["spec"][
        "initContainers"
    ][0]
    assert stager["name"] == "stager"
    assert stager["volumeMounts"][0]["readOnly"] is True
    assert (trial / "backend/kubernetes.json").is_file()


@pytest.mark.parametrize(
    "fault_boundary",
    [
        "pvc_created",
        "job_created",
    ],
)
def test_kubernetes_submission_recovers_each_pre_job_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_boundary: str,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    _write_stage_marker(trial)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
    )
    job_name = native_resource_name("case-1", trial)
    claim_name = native_resource_name("case-1", trial, suffix="-data")
    remote: dict[str, dict[str, object]] = {}
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
        if kind == "job":
            inject_once("job_created")

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(backend, "_apply", apply_resource)

    with pytest.raises(BackendConnectivityError):
        backend.submit(workload)
    handle = backend.submit(workload)

    assert handle.native_id == job_name
    assert remote[f"job/{job_name}"]["kind"] == "Job"
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
        lambda name, uid, **kwargs: (),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "failed"
    assert snapshot.details["retryable_infrastructure"] is expected


def test_kubernetes_termination_preserves_events_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "backend").mkdir(parents=True)
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
    observed = BackendSnapshot(
        phase="running",
        details={"retryable_infrastructure": False},
    )
    job = {"metadata": {"name": "trial-job", "uid": "job-uid"}}
    pod = {"metadata": {"name": "trial-pod", "uid": "pod-uid"}}
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(backend, "inspect", lambda unused: observed)
    monkeypatch.setattr(
        backend,
        "_get",
        lambda kind, name=None, **kwargs: job,
    )
    monkeypatch.setattr(
        backend,
        "_pods_for_handle",
        lambda unused: (pod,),
    )

    def events(
        name: str,
        uid: str | None,
        **kwargs: object,
    ) -> tuple[dict[str, object], ...]:
        return (
            {
                "involved_name": name,
                "message": f"event for {uid}",
            },
        )

    monkeypatch.setattr(backend, "_events", events)
    monkeypatch.setattr(
        backend,
        "_delete_and_wait",
        lambda kind, name: calls.append((kind, name)),
    )

    snapshot = backend.terminate(
        handle,
        reason="TrialDeadlineExceeded",
    )

    assert calls == [("job", "trial-job")]
    assert snapshot.details["termination_events"] == [
        {
            "involved_name": "trial-job",
            "message": "event for job-uid",
        },
        {
            "involved_name": "trial-pod",
            "message": "event for pod-uid",
        },
    ]
    persisted = json.loads(
        (trial / "backend/kubernetes.json").read_text()
    )
    assert (
        persisted["termination_snapshot"]["details"][
            "termination_events"
        ]
        == snapshot.details["termination_events"]
    )


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
        lambda name, uid, **kwargs: events.get(name, ()),
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


def test_kubernetes_candidate_evaluation_failure_is_not_restarted(
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
    pipeline = {
        "status": "complete",
        "provider_result_present": True,
        "infrastructure_failure": False,
    }
    evaluation = {
        "status": "failed",
        "candidate_failure": True,
        "retryable_infrastructure": False,
        "failure": {
            "domain": "candidate",
            "reason": "BenchmarkEvaluationFailed",
            "message": "candidate output was incorrect",
        },
    }
    pod = {
        "metadata": {"name": "trial-pod", "uid": "pod-uid"},
        "spec": {"nodeName": "node-a"},
        "status": {
            "phase": "Succeeded",
            "initContainerStatuses": [
                {
                    "name": "agent",
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "reason": "Completed",
                            "message": json.dumps(
                                {"brunner_pipeline": pipeline}
                            ),
                        }
                    },
                }
            ],
            "containerStatuses": [
                {
                    "name": "evaluator",
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "reason": "Completed",
                            "message": json.dumps(
                                {"brunner_evaluation": evaluation}
                            ),
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
                "status": {
                    "conditions": [{"type": "Complete"}]
                },
            }
        if kind == "pods":
            return {"items": [pod]}
        raise AssertionError((kind, name, kwargs))

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_events",
        lambda name, uid, **kwargs: (),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "succeeded"
    assert snapshot.reason is None
    assert snapshot.details["retryable_infrastructure"] is False
    assert snapshot.details["brunner_pipeline"] == pipeline
    assert snapshot.details["brunner_evaluation"] == evaluation
    assert snapshot.details["terminated_container"]["container"] == (
        "evaluator"
    )


def test_kubernetes_old_warning_event_does_not_override_completed_job(
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
        lambda name, uid, **kwargs: events.get(name, ()),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "succeeded"
    assert snapshot.reason is None
    assert snapshot.message is None
    assert snapshot.exit_code == 0
    assert snapshot.details["retryable_infrastructure"] is False
    assert snapshot.warnings == (
        "Evicted: Pod ephemeral local storage usage exceeds "
        "the total limit of containers 256Mi.",
    )


def test_kubernetes_missing_job_with_pvc_is_retryable_failure(
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

    assert snapshot.phase == "failed"
    assert snapshot.reason == "JobMissing"
    assert snapshot.details["claim_phase"] == "Bound"
    assert snapshot.details["retryable_infrastructure"] is True


def test_kubernetes_restart_reuses_staged_pvc_without_overwriting_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    _write_stage_marker(trial)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
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
                    "labels": {"dev.brunner/workload": workload_name},
                    "annotations": {
                        "dev.brunner/staged": "true",
                        "dev.brunner/challenge-sha256": "a" * 64,
                        "dev.brunner/workload-sha256": workload.sha256,
                        "dev.brunner/runtime-protocol": "1.0",
                    },
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
    restarted = backend.restart(previous, workload, 1)

    assert deleted == [("job", workload_name)]
    assert len(applied) == 1
    assert applied[0]["kind"] == "Job"
    assert "initContainers" not in applied[0]["spec"]["template"]["spec"]
    assert restarted.native_id.endswith("-r1")
    assert restarted.metadata["claim_name"] == claim_name
    assert restarted.metadata["restart_generation"] == 1


def test_kubernetes_restart_rejects_network_isolation_mode_change(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    (trial / "workspace").mkdir(parents=True)
    _write_stage_marker(trial)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    previous = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id="case-1",
        trial=trial,
        metadata={
            "claim_name": "case-1-data",
            "egress_proxy_sha256": None,
            "network_isolation_mode": "strict",
        },
    )
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="bizon",
            network_isolation_mode="controlled-egress",
        )
    )

    with pytest.raises(
        BackendRequestError,
        match="different network isolation mode",
    ):
        backend.restart(previous, workload, 1)


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


def test_kubernetes_stage_runs_as_job_init_container_without_api_copy(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    _write_stage_marker(trial)
    workload = WorkloadSpec(
        workload_id="case-1",
        trial=trial,
        command=("brunner-agent",),
        timeout_seconds=60,
        image="agent:latest",
    )
    claim_name = native_resource_name("case-1", trial, suffix="-data")
    job = render_job(
        "case-1-job",
        claim_name,
        workload,
        KubernetesProfile(namespace="benchmarks"),
        {
            "app.kubernetes.io/name": "brunner",
            "dev.brunner/workload": "case-1-job",
        },
        stage_source_claim="campaign-control",
        stage_source_sub_path="trial",
        stage_report={
            "challenge_sha256": "a" * 64,
            "file_inventory": {},
            "benchmark_id": "test",
            "benchmark_version": "1.0",
            "contract_sha256": "b" * 64,
        },
        stager_image="reader:latest",
    )

    stager = job["spec"]["template"]["spec"]["initContainers"][0]
    assert stager["name"] == "stager"
    assert stager["command"][2] == "brunner.backends.remote"
    assert stager["command"][3] == "stage-copy"
    assert stager["volumeMounts"][0] == {
        "name": "stage-source",
        "mountPath": "/brunner/source",
        "readOnly": True,
        "subPath": "trial",
    }
    agent = job["spec"]["template"]["spec"]["containers"][0]
    assert all(
        mount["name"] != "stage-source"
        for mount in agent["volumeMounts"]
    )


def test_collection_job_mounts_control_claim_once() -> None:
    job = render_collection_job(
        "case-1-collect",
        trial_claim_name="case-1-data",
        control_claim_name="campaign-control",
        baseline_sub_path="trials/case-1",
        destination_relative="collected/case-1",
        image="reader:latest",
        profile=KubernetesProfile(namespace="benchmarks"),
        labels={"dev.brunner/role": "artifact-reader"},
        encoded_policy="policy",
        evaluation_results_path="evaluation/results.json",
    )

    pod_spec = job["spec"]["template"]["spec"]
    collector = pod_spec["containers"][0]
    assert collector["command"][5] == (
        "/brunner/control/trials/case-1"
    )
    assert [
        volume["name"] for volume in pod_spec["volumes"]
    ].count("control") == 1
    assert all(
        volume["name"] != "baseline"
        for volume in pod_spec["volumes"]
    )
    assert all(
        mount["name"] != "baseline"
        for mount in collector["volumeMounts"]
    )


def test_kubernetes_collect_surfaces_collection_job_cleanup_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id="case-1-job",
        trial=trial,
        metadata={"claim_name": "case-1-data"},
    )
    destination = tmp_path / "collected"

    def completed_job(
        kind: str,
        name: str,
        **kwargs: object,
    ) -> dict[str, object]:
        destination.mkdir(exist_ok=True)
        destination.with_name("collected-inventory.json").write_text("{}")
        destination.with_name("collected-collection.json").write_text(
            json.dumps({"files": 0})
        )
        return {
            "status": {
                "conditions": [{"type": "Complete", "status": "True"}]
            }
        }

    monkeypatch.setattr(
        backend,
        "_get",
        completed_job,
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
            destination,
            policy=ArtifactPolicy(),
        )


def test_kubernetes_collection_rejects_completed_job_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id="case-1",
        native_id="case-1-job",
        trial=trial,
        metadata={"claim_name": "case-1-data"},
    )
    get_calls = 0

    def get_job(
        kind: str,
        name: str,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal get_calls
        get_calls += 1
        return {
            "status": {
                "conditions": [
                    {
                        "type": "Complete",
                        "status": "True",
                        "lastTransitionTime": "2000-01-01T00:00:00Z",
                    }
                ]
            }
        }

    monkeypatch.setattr(
        backend,
        "_get",
        get_job,
    )

    with pytest.raises(
        IntegrityError,
        match="completed without a verified collection",
    ):
        backend.collect(
            handle,
            tmp_path / "collected",
            policy=ArtifactPolicy(),
        )

    assert get_calls == 1


def test_kubernetes_collection_waits_for_completed_job_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    backend = KubernetesBackend(
        KubernetesProfile(
            namespace="benchmarks",
            artifact_reader_image="reader:latest",
        ),
        source_claim_name="campaign-control",
        source_root=tmp_path,
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
        "_get",
        lambda kind, name, **kwargs: {
            "status": {
                "conditions": [
                    {
                        "type": "Complete",
                        "status": "True",
                        "lastTransitionTime": "2999-01-01T00:00:00Z",
                    }
                ]
            }
        },
    )

    with pytest.raises(
        ArtifactTransferPending,
        match="waiting for the collection",
    ):
        backend.collect(
            handle,
            tmp_path / "collected",
            policy=ArtifactPolicy(),
        )


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
        lambda names, role: None,
    )
    monkeypatch.setattr(
        backend,
        "_delete_helper_pods",
        lambda names, role: None,
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
        ((stable_name, "case-1-r1"), "artifact-reader"),
    ]
    assert deleted == [
        (
            "job",
            native_resource_name(
                "case-1",
                trial,
                suffix="-collect",
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
        "_delete_helper_pods",
        lambda names, role: None,
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
