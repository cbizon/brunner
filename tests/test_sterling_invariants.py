from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from brunner import BRUNNER_RUNTIME_PROTOCOL
from brunner.artifacts import artifact_metadata, file_inventory
from brunner.backends import BackendCapacity, WorkloadSpec
from brunner.backends.base import (
    BackendHandle,
    TrustedEvaluationSpec,
    native_resource_name,
)
from brunner.backends.kubernetes import (
    REFERENCE_MANIFEST_SHA256_ANNOTATION,
    KubernetesBackend,
    KubernetesProfile,
    render_job,
    render_network_policies,
)
from brunner.campaign import CampaignPlan, CampaignRunner, CampaignTrial
from brunner.contract import load_output_contract
from brunner.dashboard import start_campaign_server
from brunner.definition import ArtifactPolicy
from brunner.errors import BackendRequestError, IntegrityError
from brunner.runtime_protocol import validate_trial_runtime
from brunner.trial import TrialIdentity, create_trial
from examples.text_benchmark.definition import build_definition


ROOT = Path(__file__).parents[1]
DIGEST = "sha256:" + "1" * 64
IMAGE = f"registry.example/brunner@{DIGEST}"


def _trial(tmp_path: Path, test_id: str = "trial") -> Path:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    return create_trial(
        definition,
        contract,
        tmp_path / "trials",
        TrialIdentity(test_id, "codex", "model", None),
    )


def _workload(trial: Path, **kwargs: object) -> WorkloadSpec:
    values = {
        "workload_id": trial.name,
        "trial": trial,
        "command": ("python", "-m", "brunner.agent_cli", "/brunner/trial"),
        "timeout_seconds": 60,
        "image": IMAGE,
    }
    values.update(kwargs)
    return WorkloadSpec(**values)


def test_network_policy_only_allows_dns_and_configured_proxy(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path)
    workload = _workload(trial)
    profile = KubernetesProfile(
        namespace="benchmarks",
        agent_image=IMAGE,
        artifact_reader_image=IMAGE,
        proxy_url="http://egress-proxy.proxy.svc:3128",
        proxy_namespace="proxy",
        proxy_pod_selector={"app": "egress-proxy"},
        proxy_port=3128,
    )
    labels = {
        "app.kubernetes.io/name": "brunner",
        "dev.brunner/workload": native_resource_name(
            workload.workload_id,
            workload.resource_id,
        ),
    }

    pipeline, helpers = render_network_policies(
        workload,
        profile,
        labels,
    )
    job = render_job(
        "trial-job",
        "trial-data",
        workload,
        profile,
        labels,
    )

    assert pipeline["spec"]["podSelector"]["matchLabels"][
        "dev.brunner/role"
    ] == "pipeline"
    assert len(pipeline["spec"]["egress"]) == 2
    assert pipeline["spec"]["egress"][0]["ports"] == [
        {"protocol": "UDP", "port": 53},
        {"protocol": "TCP", "port": 53},
    ]
    assert pipeline["spec"]["egress"][1]["to"][0]["podSelector"][
        "matchLabels"
    ] == {"app": "egress-proxy"}
    assert helpers["spec"]["egress"] == []
    assert job["spec"]["template"]["metadata"]["labels"][
        "dev.brunner/role"
    ] == "pipeline"
    environment = {
        item["name"]: item.get("value")
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["HTTPS_PROXY"] == profile.proxy_url
    assert job["spec"]["backoffLimit"] == 6


def test_network_policy_is_applied_before_staging_or_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial(tmp_path)
    workload = _workload(trial, image="agent:test")
    backend = KubernetesBackend(
        KubernetesProfile(
            agent_image="agent:test",
            artifact_reader_image="reader:test",
            preflight_enabled=False,
            require_image_digests=False,
        )
    )
    lifecycle: list[str] = []
    monkeypatch.setattr(
        backend,
        "_get",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        backend,
        "_apply",
        lambda resource: lifecycle.append(str(resource["kind"])),
    )
    monkeypatch.setattr(
        backend,
        "_stage_trial",
        lambda *args, **kwargs: lifecycle.append("stage"),
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

    backend.submit(workload)

    assert lifecycle == [
        "NetworkPolicy",
        "NetworkPolicy",
        "PersistentVolumeClaim",
        "stage",
        "Job",
    ]


def test_mutable_images_are_rejected_by_default(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    backend = KubernetesBackend(
        KubernetesProfile(
            agent_image="agent:latest",
            artifact_reader_image=IMAGE,
        )
    )

    with pytest.raises(BackendRequestError, match="immutable image"):
        backend._ensure_preflight(
            _workload(trial, image="agent:latest")
        )


def test_profile_agent_image_is_part_of_workload_identity(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path)
    workload = _workload(trial, image=None)
    first = KubernetesBackend(
        KubernetesProfile(
            agent_image=IMAGE,
            artifact_reader_image=IMAGE,
        )
    ).prepare_workload(workload)
    second = KubernetesBackend(
        KubernetesProfile(
            agent_image=(
                "registry.example/brunner@sha256:" + "2" * 64
            ),
            artifact_reader_image=IMAGE,
        )
    ).prepare_workload(workload)

    assert first.image == IMAGE
    assert first.sha256 != second.sha256


def test_workload_rejects_reserved_ownership_labels(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path)

    with pytest.raises(ValueError, match="Brunner-reserved"):
        _workload(
            trial,
            labels={"dev.brunner/workload": "not-brunner-owned"},
        ).validate()


def test_active_job_ignores_failed_old_pod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial(tmp_path)
    backend = KubernetesBackend(
        KubernetesProfile(
            preflight_enabled=False,
            require_image_digests=False,
            unsafe_disable_network_policy_for_tests=True,
        )
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id=trial.name,
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    old = {
        "metadata": {
            "name": "old",
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "status": {
            "phase": "Failed",
            "containerStatuses": [
                {
                    "name": "agent",
                    "state": {
                        "terminated": {
                            "exitCode": 137,
                            "reason": "OOMKilled",
                        }
                    },
                }
            ],
        },
    }
    current = {
        "metadata": {
            "name": "current",
            "creationTimestamp": "2026-01-01T00:01:00Z",
        },
        "status": {"phase": "Running"},
    }

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        if kind == "pvc":
            return {"status": {"phase": "Bound"}}
        if kind == "job":
            return {"status": {"active": 1}}
        if kind == "pods":
            return {"items": [current, old]}
        raise AssertionError((kind, name, kwargs))

    monkeypatch.setattr(backend, "_get", get_resource)

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "running"
    assert snapshot.details["pod_phase"] == "Running"
    assert [pod["name"] for pod in snapshot.details["pods"]] == [
        "old",
        "current",
    ]


def test_reference_claim_requires_rwx_and_exact_manifest_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial(tmp_path)
    expected = "a" * 64
    evaluation = TrustedEvaluationSpec(
        benchmark_id="benchmark",
        benchmark_version="1.0",
        contract_sha256="contract",
        image=IMAGE,
        command=("evaluate",),
        results_path="evaluation/results.json",
        timeout_seconds=60,
        reference_manifest_path="manifest.json",
        reference_manifest_sha256=expected,
    )
    workload = _workload(trial, evaluation=evaluation)
    backend = KubernetesBackend(
        KubernetesProfile(
            reference_claim_name="reference",
            artifact_reader_image=IMAGE,
            preflight_enabled=False,
        )
    )
    claim = {
        "metadata": {
            "annotations": {
                REFERENCE_MANIFEST_SHA256_ANNOTATION: expected,
            }
        },
        "spec": {"accessModes": ["ReadWriteMany"]},
    }
    monkeypatch.setattr(backend, "_get", lambda *args, **kwargs: claim)

    backend._validate_reference_claim(workload)
    claim["metadata"]["annotations"][
        REFERENCE_MANIFEST_SHA256_ANNOTATION
    ] = "b" * 64
    with pytest.raises(BackendRequestError, match="digest mismatch"):
        backend._validate_reference_claim(workload)


def test_remote_stage_verification_rejects_mutation_and_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "trial"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    resource = workspace / "resource.txt"
    resource.write_text("candidate-visible")
    metadata = artifact_metadata(resource)
    assert metadata is not None
    expected = {
        "benchmark_id": "benchmark",
        "benchmark_version": "1.0",
        "contract_sha256": "c" * 64,
        "challenge_sha256": "d" * 64,
        "file_inventory": {"resource.txt": metadata.to_dict()},
    }
    (workspace / ".brunner-challenge.json").write_text(
        json.dumps({"schema_version": "1.0", **expected})
    )
    encoded = base64.urlsafe_b64encode(
        json.dumps(expected).encode()
    ).decode()
    command = (
        sys.executable,
        "-m",
        "brunner.backends.remote",
        "verify-stage",
        str(root),
        encoded,
    )

    verified = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr

    resource.write_text("corrupt")
    corrupt = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert corrupt.returncode != 0
    assert "inventory mismatch" in corrupt.stderr

    resource.write_text("candidate-visible")
    (workspace / "escape").symlink_to(resource)
    symlink = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert symlink.returncode != 0
    assert "symlink" in symlink.stderr


def test_trial_resource_and_workload_identity_survive_move(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path)
    first = _workload(trial)
    first_name = native_resource_name(
        first.workload_id,
        first.resource_id,
    )
    first_digest = first.sha256
    moved_parent = tmp_path / "moved"
    moved_parent.mkdir()
    moved = moved_parent / trial.name
    trial.rename(moved)
    second = _workload(moved)

    assert native_resource_name(
        second.workload_id,
        second.resource_id,
    ) == first_name
    assert second.sha256 == first_digest
    assert replace(second, memory_limit="4Gi").sha256 != first_digest


def test_quota_capacity_uses_effective_pod_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial(tmp_path)
    workload = _workload(
        trial,
        cpu_request="2",
        cpu_limit="4",
        memory_request="1Gi",
        memory_limit="2Gi",
    )
    backend = KubernetesBackend(
        KubernetesProfile(
            max_parallel=10,
            preflight_enabled=False,
            require_image_digests=False,
            unsafe_disable_network_policy_for_tests=True,
        )
    )

    def get_resource(
        kind: str,
        name: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        if kind == "jobs":
            return {"items": [{"status": {"active": 1}}]}
        if kind == "resourcequota":
            return {
                "items": [
                    {
                        "metadata": {"name": "benchmark-quota"},
                        "status": {
                            "hard": {
                                "requests.cpu": "5",
                                "limits.cpu": "12",
                                "count/jobs.batch": "5",
                            },
                            "used": {
                                "requests.cpu": "1",
                                "limits.cpu": "4",
                                "count/jobs.batch": "1",
                            },
                        },
                    }
                ]
            }
        raise AssertionError((kind, name, kwargs))

    monkeypatch.setattr(backend, "_get", get_resource)

    capacity = backend.capacity(workload)

    assert capacity.available == 2
    assert capacity.details["requirements"]["requests.cpu"] == "2"
    assert capacity.details["requirements"]["limits.cpu"] == "4"
    assert capacity.details["quota_limits"][0]["quota"] == (
        "benchmark-quota"
    )


def test_preflight_checks_remote_operations_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial(tmp_path)
    workload = _workload(trial)
    backend = KubernetesBackend(
        KubernetesProfile(
            agent_image=IMAGE,
            artifact_reader_image=IMAGE,
        )
    )
    calls: list[tuple[str, ...]] = []

    def run(
        *arguments: str,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        stdout = "yes\n" if arguments[:2] == ("auth", "can-i") else "{}"
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    monkeypatch.setattr(backend, "_run", run)

    backend._ensure_preflight(workload)

    assert ("auth", "can-i", "create", "pods/exec", "-n", "default") in calls
    assert ("auth", "can-i", "list", "jobs.batch", "-n", "default") in calls
    assert (
        "auth",
        "can-i",
        "create",
        "networkpolicies.networking.k8s.io",
        "-n",
        "default",
    ) in calls
    assert backend._preflight_complete is True
    with pytest.raises(BackendRequestError, match="immutable image"):
        backend._ensure_preflight(
            replace(workload, image="registry.example/brunner:latest")
        )


def test_terminal_event_rbac_failure_is_diagnostic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial(tmp_path)
    backend = KubernetesBackend(
        KubernetesProfile(
            preflight_enabled=False,
            require_image_digests=False,
            unsafe_disable_network_policy_for_tests=True,
        )
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id=trial.name,
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    pod = {
        "metadata": {"name": "trial-pod", "uid": "pod-uid"},
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

    monkeypatch.setattr(backend, "_get", get_resource)
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="events is forbidden",
        ),
    )

    snapshot = backend.inspect(handle)

    assert snapshot.phase == "succeeded"
    assert any(
        "events unavailable" in warning
        for warning in snapshot.warnings
    )


def test_missing_job_and_pvc_is_terminal_storage_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial(tmp_path)
    backend = KubernetesBackend(
        KubernetesProfile(
            preflight_enabled=False,
            require_image_digests=False,
            unsafe_disable_network_policy_for_tests=True,
        )
    )
    handle = BackendHandle(
        backend="kubernetes",
        workload_id=trial.name,
        native_id="trial-job",
        trial=trial,
        metadata={"claim_name": "trial-data"},
    )
    monkeypatch.setattr(
        backend,
        "_get",
        lambda *args, **kwargs: None,
    )

    snapshot = backend.inspect(handle)

    assert snapshot.reason == "TrialStorageMissing"
    assert snapshot.details["retryable_infrastructure"] is False


def test_runtime_protocol_is_checked_in_trial_and_helper(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path)
    validate_trial_runtime(trial)
    manifest = json.loads(
        (trial / "metadata/manifest.json").read_text()
    )
    manifest["brunner_runtime_protocol"] = "obsolete"
    (trial / "metadata/manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(IntegrityError, match="incompatible"):
        validate_trial_runtime(trial)

    protocol = subprocess.run(
        (
            sys.executable,
            "-m",
            "brunner.backends.remote",
            "protocol",
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(protocol.stdout)["protocol"] == (
        BRUNNER_RUNTIME_PROTOCOL
    )


def test_manifest_declared_artifact_is_not_failure_fallback_payload(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path)
    submission = trial / "workspace/submission"
    submission.mkdir()
    (submission / "huge.bin").write_bytes(b"x" * 4096)
    (submission / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "output": "huge.bin",
            }
        )
    )

    inventory = file_inventory(
        trial,
        ArtifactPolicy(max_collection_bytes=None),
    )

    assert "workspace/submission/manifest.json" in inventory
    assert "workspace/submission/huge.bin" not in inventory


def test_campaign_monitor_serves_generated_dashboard(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text("campaign monitor")
    server, url = start_campaign_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.read().decode() == "campaign monitor"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _InitializationBackend:
    name = "kubernetes"
    agent_isolation = "container"
    trusted_evaluation = "kubernetes"

    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity:
        return BackendCapacity(1, 0, 0, 1)


def test_campaign_resume_rejects_changed_workload_contract(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    root = tmp_path / "campaign"
    first = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="identity",
            root=root,
            trials=(CampaignTrial("trial-a", "codex", "model"),),
            backend_image="agent:first",
        ),
        _InitializationBackend(),
    )
    first.initialize()
    second = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="identity",
            root=root,
            trials=(CampaignTrial("trial-a", "codex", "model"),),
            backend_image="agent:changed",
        ),
        _InitializationBackend(),
    )

    state = second.initialize()

    entry = state["trials"][0]
    assert entry["phase"] == "attention_required"
    assert "workload changed" in entry["error"]


def test_campaign_rejects_nondeterministic_materialization(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    script = tmp_path / "materialize.py"
    script.write_text(
        """
import os
import uuid
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "random.txt").write_text(uuid.uuid4().hex)
"""
    )
    definition = replace(
        definition,
        challenge=replace(
            definition.challenge,
            materialize_command=(sys.executable, str(script)),
        ),
    )
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="nondeterministic",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial("trial-a", "codex", "model"),
                CampaignTrial("trial-b", "codex", "model"),
            ),
            backend_image="agent:test",
        ),
        _InitializationBackend(),
    )

    with pytest.raises(RuntimeError, match="not deterministic"):
        runner.initialize()
