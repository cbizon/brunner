from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import brunner
import brunner.cluster as cluster_module
from brunner.backends import KubernetesProfile
from brunner.campaign import CampaignPlan, CampaignTrial
from brunner.cluster import (
    CAMPAIGN_IMAGE_OVERRIDES_ENV,
    EVALUATION_IMAGE_OVERRIDE_ENV,
    RESULT_MANIFEST,
    RESULT_MANIFEST_SHA256_ANNOTATION,
    RESULT_MANIFEST_SIZE_ANNOTATION,
    CampaignResources,
    ClusterCampaign,
    ClusterCampaignClient,
    ConfigMapLock,
    ControllerProfile,
    KubernetesEvaluationFinalizer,
    Kubectl,
    apply_campaign_image_overrides,
    apply_definition_image_override,
    campaign_image_environment,
    definition_image_environment,
    campaign_resources,
    finalize_result_bundle,
    publish_trial_results,
    render_cluster_resources,
)
from brunner.errors import (
    BackendRequestError,
    EvaluationPending,
    IntegrityError,
)
from examples.text_benchmark.definition import build_definition


IMAGE = "registry.example/brunner@sha256:" + "1" * 64


def _campaign(
    *trials: CampaignTrial,
    campaign_id: str = "cluster-campaign",
) -> ClusterCampaign:
    return ClusterCampaign(
        plan=CampaignPlan(
            campaign_id=campaign_id,
            trials=trials
            or (CampaignTrial("run-a", "codex", "model-a"),),
            backend_image=IMAGE,
            provider_secret_environment={
                "codex": {
                    "OPENAI_API_KEY": (
                        "codex-provider-credentials",
                        "OPENAI_API_KEY",
                    )
                }
            },
        ),
        backend=KubernetesProfile(
            namespace="bizon",
            agent_image=IMAGE,
            artifact_reader_image=IMAGE,
            proxy_image=IMAGE,
        ),
        controller=ControllerProfile(
            namespace="bizon",
            image=IMAGE,
        ),
    )


def test_public_api_replaces_campaign_runner_and_local_root() -> None:
    assert not hasattr(brunner, "CampaignRunner")
    with pytest.raises(TypeError, match="unexpected keyword argument 'root'"):
        CampaignPlan(  # type: ignore[call-arg]
            campaign_id="old-control-plane",
            root=Path("campaign"),
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        )


def test_campaign_digest_is_independent_of_trial_order() -> None:
    first = _campaign(
        CampaignTrial("run-a", "codex", "model-a"),
        CampaignTrial("run-b", "claude", "model-b"),
    )
    second = _campaign(
        CampaignTrial("run-b", "claude", "model-b"),
        CampaignTrial("run-a", "codex", "model-a"),
    )

    assert first.sha256 == second.sha256


def test_campaign_image_overrides_resolve_embedded_image_identity() -> None:
    submitted = _campaign()
    stale_image = "registry.example/brunner@sha256:" + "0" * 64
    embedded = ClusterCampaign(
        plan=replace(
            submitted.plan,
            backend_image=stale_image,
        ),
        backend=replace(
            submitted.backend,
            agent_image=stale_image,
            artifact_reader_image=stale_image,
            proxy_image=stale_image,
        ),
        controller=replace(
            submitted.controller,
            image=stale_image,
        ),
    )

    resolved = apply_campaign_image_overrides(
        embedded,
        campaign_image_environment(submitted),
    )

    assert resolved == submitted
    assert resolved.sha256 == submitted.sha256


def test_definition_image_override_resolves_embedded_evaluator_image() -> None:
    submitted = build_definition()
    stale = replace(
        submitted,
        evaluation=replace(
            submitted.evaluation,
            image="registry.example/evaluator@sha256:" + "0" * 64,
        ),
    )

    resolved = apply_definition_image_override(
        stale,
        definition_image_environment(submitted),
    )

    assert resolved.evaluation.image == submitted.evaluation.image


def test_rendered_control_plane_enforces_cluster_ownership() -> None:
    definition = build_definition()
    campaign = _campaign()
    resources = campaign_resources(definition, campaign)
    rendered = render_cluster_resources(
        definition,
        campaign,
        benchmark_ref="examples.text_benchmark.definition",
        campaign_ref="my_benchmark.campaign",
    )
    by_kind = {}
    for resource in rendered:
        by_kind.setdefault(resource["kind"], []).append(resource)

    assert len(by_kind["PersistentVolumeClaim"]) == 2
    assert {
        claim["metadata"]["name"]
        for claim in by_kind["PersistentVolumeClaim"]
    } == {resources.control_claim, resources.results_claim}
    assert all(
        claim["spec"]["accessModes"] == ["ReadWriteMany"]
        for claim in by_kind["PersistentVolumeClaim"]
    )

    role_rules = by_kind["Role"][0]["rules"]
    role_resources = {
        name for rule in role_rules for name in rule["resources"]
    }
    quota_rule = next(
        rule for rule in role_rules if "resourcequotas" in rule["resources"]
    )
    assert quota_rule["verbs"] == ["get", "list"]
    assert all(
        rule["apiGroups"] != ["coordination.k8s.io"]
        for rule in role_rules
    )
    assert "secrets" not in role_resources

    preparation = by_kind["Job"][0]["spec"]["template"]["spec"]
    assert preparation["automountServiceAccountToken"] is False
    preparation_environment = {
        entry["name"]: entry["value"]
        for entry in preparation["containers"][0]["env"]
    }
    assert CAMPAIGN_IMAGE_OVERRIDES_ENV in preparation_environment
    assert EVALUATION_IMAGE_OVERRIDE_ENV in preparation_environment
    assert preparation["containers"][0]["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "4Gi"},
    }

    controller = by_kind["Deployment"][0]["spec"]["template"]["spec"]
    assert controller["serviceAccountName"] == resources.service_account
    assert controller["automountServiceAccountToken"] is True
    assert "initContainers" not in controller
    controller_environment = {
        entry["name"]: entry["value"]
        for entry in controller["containers"][0]["env"]
        if "value" in entry
    }
    assert (
        controller_environment[CAMPAIGN_IMAGE_OVERRIDES_ENV]
        == preparation_environment[CAMPAIGN_IMAGE_OVERRIDES_ENV]
    )
    assert (
        controller_environment[EVALUATION_IMAGE_OVERRIDE_ENV]
        == preparation_environment[EVALUATION_IMAGE_OVERRIDE_ENV]
    )
    assert {
        mount["mountPath"]
        for mount in controller["containers"][0]["volumeMounts"]
    } >= {"/brunner/control", "/brunner/results"}
    assert not any(
        "valueFrom" in value
        and "secretKeyRef" in value["valueFrom"]
        for value in controller["containers"][0]["env"]
    )

    monitor = by_kind["Service"][0]
    assert monitor["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_assessment_job_receives_submitted_image_identity() -> None:
    definition = build_definition()
    campaign = _campaign()
    resources = campaign_resources(definition, campaign)
    finalizer = KubernetesEvaluationFinalizer(
        definition=definition,
        campaign=campaign,
        resources=resources,
        client=None,  # type: ignore[arg-type]
        benchmark_ref="examples.text_benchmark.definition",
        campaign_ref="my_benchmark.campaign",
        proxy_url=None,
        proxy_labels={},
    )

    job = finalizer._job(
        Path("/brunner/control/collected/run-a"),
        "assessment-run-a",
    )
    environment = {
        entry["name"]: entry["value"]
        for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in entry
    }
    input_mount = next(
        mount
        for mount in job["spec"]["template"]["spec"]["containers"][0][
            "volumeMounts"
        ]
        if mount.get("readOnly")
    )
    output_mount = next(
        mount
        for mount in job["spec"]["template"]["spec"]["containers"][0][
            "volumeMounts"
        ]
        if mount.get("subPath")
        == "assessment-output/assessment-run-a"
    )

    assert (
        environment[CAMPAIGN_IMAGE_OVERRIDES_ENV]
        == campaign_image_environment(campaign)[
            CAMPAIGN_IMAGE_OVERRIDES_ENV
        ]
    )
    assert (
        environment[EVALUATION_IMAGE_OVERRIDE_ENV]
        == definition_image_environment(definition)[
            EVALUATION_IMAGE_OVERRIDE_ENV
        ]
    )
    assert input_mount == {
        "name": "control",
        "mountPath": "/brunner/control/collected/run-a",
        "subPath": "collected/run-a",
        "readOnly": True,
    }
    assert output_mount == {
        "name": "control",
        "mountPath": "/brunner/control/assessment-output/assessment-run-a",
        "subPath": "assessment-output/assessment-run-a",
    }
    control_volumes = [
        volume
        for volume in job["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "control"
    ]
    assert control_volumes == [
        {
            "name": "control",
            "persistentVolumeClaim": {
                "claimName": resources.control_claim,
            },
        }
    ]


class PendingAssessmentClient:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []

    def apply(self, resource: dict[str, Any]) -> None:
        self.applied.append(resource)

    def get(
        self,
        kind: str,
        name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        return None


def test_assessment_submission_is_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    trial = control / "collected/run-a"
    trial.mkdir(parents=True)
    monkeypatch.setattr(cluster_module, "CONTROL_ROOT", control)
    definition = build_definition()
    campaign = _campaign()
    resources = campaign_resources(definition, campaign)
    client = PendingAssessmentClient()
    finalizer = KubernetesEvaluationFinalizer(
        definition=definition,
        campaign=campaign,
        resources=resources,
        client=client,  # type: ignore[arg-type]
        benchmark_ref="examples.text_benchmark.definition",
        campaign_ref="my_benchmark.campaign",
        proxy_url=None,
        proxy_labels={},
    )

    with pytest.raises(EvaluationPending, match="submitted"):
        finalizer(trial)

    assert [item["kind"] for item in client.applied] == [
        "NetworkPolicy",
        "Job",
    ]
    assert (
        control
        / "assessment-output"
        / finalizer._job_name(trial)
    ).is_dir()


class FailedPreparationClient:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []

    def get(
        self,
        kind: str,
        name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        assert kind == "job"
        return {
            "status": {
                "conditions": [
                    {
                        "type": "Failed",
                        "status": "True",
                        "reason": "BackoffLimitExceeded",
                        "message": "materializer exited 17",
                    }
                ]
            }
        }

    def apply(self, resource: dict[str, Any]) -> None:
        self.applied.append(resource)


class CompletedPreparationClient(FailedPreparationClient):
    def get(
        self,
        kind: str,
        name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        assert kind == "job"
        return {
            "status": {
                "conditions": [
                    {
                        "type": "Complete",
                        "status": "True",
                    }
                ]
            }
        }


def test_controller_reports_terminal_preparation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(cluster_module, "CONTROL_ROOT", control)
    campaign = _campaign()
    resources = campaign_resources(build_definition(), campaign)
    client = FailedPreparationClient()

    with pytest.raises(
        BackendRequestError,
        match="materializer exited 17",
    ):
        cluster_module._wait_for_preparation(
            campaign,
            resources,
            client,  # type: ignore[arg-type]
        )

    status = json.loads(
        client.applied[-1]["data"]["status.json"]
    )
    assert status["status"] == "attention_required"
    assert status["preparation_status"] == "failed"
    assert status["preparation_error"] == "materializer exited 17"


def test_controller_rejects_completed_preparation_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(cluster_module, "CONTROL_ROOT", control)
    campaign = _campaign()
    resources = campaign_resources(build_definition(), campaign)
    client = CompletedPreparationClient()
    monotonic_values = iter((0.0, 0.0, 0.0, 0.0, 11.0, 11.0))
    monkeypatch.setattr(
        cluster_module.time,
        "monotonic",
        lambda: next(monotonic_values, 11.0),
    )
    monkeypatch.setattr(
        cluster_module.time,
        "sleep",
        lambda seconds: None,
    )

    with pytest.raises(
        BackendRequestError,
        match="completed without publishing",
    ):
        cluster_module._wait_for_preparation(
            campaign,
            resources,
            client,  # type: ignore[arg-type]
        )

    status = json.loads(
        client.applied[-1]["data"]["status.json"]
    )
    assert status["preparation_status"] == "failed"
    assert "prepared-" in status["preparation_error"]


class FakeConfigMapLockClient:
    def __init__(self) -> None:
        self.lock: dict[str, Any] | None = None

    def get(self, kind: str, name: str) -> dict[str, Any] | None:
        assert kind == "configmap"
        assert name.endswith("-lock")
        return self.lock

    def create(self, resource: dict[str, Any]) -> bool:
        if self.lock is not None:
            return False
        self.lock = resource
        self.lock["metadata"]["resourceVersion"] = "1"
        return True

    def replace(self, resource: dict[str, Any]) -> bool:
        if self.lock is None:
            return False
        current = self.lock["metadata"].get("resourceVersion")
        if resource["metadata"].get("resourceVersion") != current:
            return False
        resource["metadata"]["resourceVersion"] = str(int(current) + 1)
        self.lock = resource
        return True


def test_config_map_lock_excludes_a_second_live_controller() -> None:
    client = FakeConfigMapLockClient()
    resources = CampaignResources("brunner-test-123", "bizon")
    first = ConfigMapLock(
        client,  # type: ignore[arg-type]
        resources,
        holder="pod-a/uid-a",
        duration_seconds=60,
    )
    first.acquire()
    try:
        second = ConfigMapLock(
            client,  # type: ignore[arg-type]
            resources,
            holder="pod-b/uid-b",
            duration_seconds=60,
        )
        assert second._try_acquire_or_renew() is False
        assert client.lock is not None
        state = json.loads(client.lock["data"]["lock.json"])
        assert state["holder"] == "pod-a/uid-a"
    finally:
        first.close()


def test_config_map_lock_takes_over_an_expired_holder() -> None:
    client = FakeConfigMapLockClient()
    resources = CampaignResources("brunner-test-123", "bizon")
    first = ConfigMapLock(
        client,  # type: ignore[arg-type]
        resources,
        holder="pod-a/uid-a",
        duration_seconds=60,
    )
    assert first._try_acquire_or_renew() is True
    assert client.lock is not None
    state = json.loads(client.lock["data"]["lock.json"])
    state["renewed_at"] = "2000-01-01T00:00:00+00:00"
    client.lock["data"]["lock.json"] = json.dumps(state)

    second = ConfigMapLock(
        client,  # type: ignore[arg-type]
        resources,
        holder="pod-b/uid-b",
        duration_seconds=60,
    )

    assert second._try_acquire_or_renew() is True
    assert client.lock is not None
    replaced = json.loads(client.lock["data"]["lock.json"])
    assert replaced["holder"] == "pod-b/uid-b"
    assert replaced["transitions"] == 1


def test_config_map_lock_rejects_corrupt_state() -> None:
    client = FakeConfigMapLockClient()
    resources = CampaignResources("brunner-test-123", "bizon")
    client.lock = {
        "metadata": {"resourceVersion": "1"},
        "data": {"lock.json": "not-json"},
    }
    lock = ConfigMapLock(
        client,  # type: ignore[arg-type]
        resources,
        holder="pod-a/uid-a",
        duration_seconds=60,
    )

    with pytest.raises(IntegrityError, match="invalid lock.json"):
        lock._try_acquire_or_renew()


def test_result_bundle_manifest_covers_all_result_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "trials/run-a").mkdir(parents=True)
    (tmp_path / "trials/run-a/result.txt").write_text("result")
    (tmp_path / "index.html").write_text("<html></html>")
    state = {"status": "complete", "trials": []}

    bundle = finalize_result_bundle(tmp_path, state, _campaign())

    manifest = bundle["manifest"]
    paths = {record["path"] for record in manifest["files"]}
    assert paths == {
        "campaign.json",
        "index.html",
        "trials/run-a/result.txt",
    }
    assert bundle["sha256"] == hashlib.sha256(
        (tmp_path / RESULT_MANIFEST).read_bytes()
    ).hexdigest()


def test_result_bundle_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("data")
    (tmp_path / "unsafe").symlink_to(source)

    with pytest.raises(IntegrityError, match="symlink"):
        finalize_result_bundle(
            tmp_path,
            {"status": "complete", "trials": []},
            _campaign(),
        )


def test_publication_omits_unchanged_challenge_and_assessment_workspace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "control/collected/run-a"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    large_input = workspace / "large-input.bin"
    large_input.write_bytes(b"candidate-visible input")
    input_metadata = {
        "type": "file",
        "size": large_input.stat().st_size,
        "sha256": hashlib.sha256(large_input.read_bytes()).hexdigest(),
    }
    marker = workspace / ".brunner-challenge.json"
    marker.write_text(
        json.dumps(
            {"file_inventory": {"large-input.bin": input_metadata}}
        )
    )
    (source / "evaluation").mkdir()
    (source / "evaluation/results.json").write_text('{"status":"complete"}')
    copied_evidence = (
        source
        / "assessments/review/workspace/evidence/trial/workspace"
    )
    copied_evidence.mkdir(parents=True)
    (copied_evidence / "large-input.bin").write_bytes(
        large_input.read_bytes()
    )
    stale_provider_home = (
        source / "assessments/review/.reviewer-provider-home"
    )
    stale_provider_home.mkdir()
    (stale_provider_home / "auth.json").write_text("must not publish")
    source.with_name("run-a-inventory.json").write_text(
        json.dumps(
            {
                "workspace/large-input.bin": input_metadata,
                "workspace/.brunner-challenge.json": {
                    "type": "file",
                    "size": marker.stat().st_size,
                    "sha256": hashlib.sha256(
                        marker.read_bytes()
                    ).hexdigest(),
                },
            }
        )
    )
    destination = tmp_path / "results/trials/run-a"

    publish_trial_results(source, destination, max_bytes=1024)

    assert not (destination / "workspace/large-input.bin").exists()
    assert not (
        destination
        / "assessments/review/workspace/evidence/trial/workspace/"
        "large-input.bin"
    ).exists()
    assert not (
        destination
        / "assessments/review/.reviewer-provider-home/auth.json"
    ).exists()
    assert (destination / "evaluation/results.json").is_file()
    assert (destination / "publication.json").is_file()


class FakeRetrievalKubectl:
    def __init__(
        self,
        *,
        status: dict[str, Any],
        manifest_sha256: str,
        manifest_size: int,
    ) -> None:
        self.status = status
        self.manifest_sha256 = manifest_sha256
        self.manifest_size = manifest_size
        self.applied: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str | None]] = []

    def get(
        self,
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
    ) -> dict[str, Any] | None:
        if kind == "configmap":
            return {"data": {"status.json": json.dumps(self.status)}}
        if kind == "pvc":
            return {
                "metadata": {
                    "annotations": {
                        RESULT_MANIFEST_SHA256_ANNOTATION: (
                            self.manifest_sha256
                        ),
                        RESULT_MANIFEST_SIZE_ANNOTATION: str(
                            self.manifest_size
                        ),
                    }
                }
            }
        raise AssertionError((kind, name, labels))

    def apply(self, resource: dict[str, Any]) -> None:
        self.applied.append(resource)

    def delete(
        self,
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
        wait: bool = False,
    ) -> None:
        self.deleted.append((kind, name))


def test_retrieval_resumes_partial_files_and_verifies_checksums(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _campaign()
    definition = build_definition()
    content = b"complete result content"
    file_record = {
        "path": "trials/run-a/result.txt",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    manifest = {
        "schema_version": "1.0",
        "campaign_id": campaign.plan.campaign_id,
        "campaign_sha256": campaign.sha256,
        "files": [file_record],
    }
    manifest_bytes = json.dumps(manifest).encode()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    fake = FakeRetrievalKubectl(
        status={
            "result_ready": True,
            "result_manifest_sha256": manifest_sha256,
            "result_manifest_size": len(manifest_bytes),
        },
        manifest_sha256=manifest_sha256,
        manifest_size=len(manifest_bytes),
    )
    client = ClusterCampaignClient(
        definition,
        campaign,
        benchmark_ref="examples.text_benchmark.definition",
        campaign_ref="my_benchmark.campaign",
    )
    client.client = fake  # type: ignore[assignment]
    monkeypatch.setattr(client, "_wait_reader", lambda name: None)

    def read(
        pod: str,
        path: str,
        offset: int,
        count: int,
    ) -> bytes:
        source = (
            manifest_bytes
            if path == RESULT_MANIFEST
            else content
        )
        return source[offset : offset + count]

    monkeypatch.setattr(client, "_read", read)
    partial = tmp_path / "trials/run-a/result.txt.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:7])

    result = client.retrieve(tmp_path)

    assert result["files"] == 1
    assert (tmp_path / "trials/run-a/result.txt").read_bytes() == content
    assert not partial.exists()
    assert (tmp_path / RESULT_MANIFEST).read_bytes() == manifest_bytes
    assert {resource["kind"] for resource in fake.applied} == {
        "NetworkPolicy",
        "Pod",
    }


class FakeCleanupKubectl:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str | None]] = []
        self.deleted: list[tuple[str, str | None]] = []
        self.delete_calls: list[
            tuple[str, str | None, str | None, bool]
        ] = []

    def get(
        self,
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
    ) -> dict[str, Any] | None:
        self.queries.append((kind, labels))
        assert kind == "pvc"
        return {
            "items": [
                {
                    "metadata": {
                        "name": "brunner-run-a-data",
                    }
                }
            ]
        }

    def delete(
        self,
        kind: str,
        name: str | None = None,
        *,
        labels: str | None = None,
        wait: bool = False,
    ) -> None:
        self.deleted.append((kind, name))
        self.delete_calls.append((kind, name, labels, wait))


def test_cluster_cleanup_queries_pvc_resource() -> None:
    campaign = _campaign()
    definition = build_definition()
    fake = FakeCleanupKubectl()
    client = ClusterCampaignClient(
        definition,
        campaign,
        benchmark_ref="examples.text_benchmark.definition",
        campaign_ref="my_benchmark.campaign",
    )
    client.client = fake  # type: ignore[assignment]

    result = client.delete(delete_results=True)

    assert fake.queries == [
        (
            "pvc",
            (
                "dev.brunner/campaign="
                f"{client.resources.base}"
            ),
        )
    ]
    assert ("pvc", "brunner-run-a-data") in fake.deleted
    assert ("pvc", client.resources.control_claim) in fake.deleted
    assert ("pvc", client.resources.results_claim) in fake.deleted
    campaign_labels = (
        f"dev.brunner/campaign={client.resources.base}"
    )
    assert fake.delete_calls[:4] == [
        ("deployment", client.resources.deployment, None, True),
        ("pod", None, campaign_labels, True),
        ("job", None, campaign_labels, True),
        ("pod", None, campaign_labels, True),
    ]
    assert result["deleted_results"] is True


def test_cluster_client_uses_configured_kubectl_binary() -> None:
    definition = build_definition()
    campaign = _campaign()
    client = ClusterCampaignClient(
        definition,
        campaign,
        benchmark_ref="examples.text_benchmark.definition",
        campaign_ref="my_benchmark.campaign",
        kubectl="custom-kubectl",
    )

    assert isinstance(client.client, Kubectl)
    assert client.client.executable == "custom-kubectl"
