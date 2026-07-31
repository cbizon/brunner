from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from brunner.artifacts import collect_local_artifacts
from brunner.backends import (
    BackendCapacity,
    BackendHandle,
    BackendSnapshot,
    WorkloadSpec,
)
from brunner.campaign import (
    CampaignPlan,
    CampaignRunner,
    CampaignTrial,
    default_workload_factory,
)
from brunner.contract import load_output_contract
from brunner.definition import ArtifactPolicy
from brunner.errors import BackendConnectivityError
from examples.text_benchmark.definition import build_definition


ROOT = Path(__file__).parents[1]


class ImmediateBackend:
    name = "fake"

    def __init__(self) -> None:
        self.handles: dict[str, BackendHandle] = {}
        self.cleaned: set[str] = set()
        self.collection_calls = 0

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        submission = workload.trial / "workspace/submission"
        submission.mkdir()
        source = (workload.trial / "workspace/input.txt").read_text()
        (submission / "result.txt").write_text(source.upper())
        (submission / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "output": "result.txt",
                }
            )
        )
        (submission / "run-status.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "submission_manifest": "submission/manifest.json",
                    "completed_units": ["uppercase"],
                    "limitations": [],
                }
            )
        )
        handle = BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=workload.workload_id,
            trial=workload.trial,
        )
        self.handles[workload.workload_id] = handle
        return handle

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        return BackendSnapshot(phase="succeeded", exit_code=0)

    def logs(self, handle: BackendHandle) -> str:
        return "fake workload complete\n"

    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        self.collection_calls += 1
        return collect_local_artifacts(
            handle.trial,
            destination,
            policy,
            included_groups=included_groups,
        )

    def cleanup(self, handle: BackendHandle) -> None:
        self.cleaned.add(handle.workload_id)

    def capacity(self) -> BackendCapacity:
        return BackendCapacity(
            limit=10,
            running=0,
            pending=0,
            available=10,
        )


class OfflineBackend(ImmediateBackend):
    def capacity(self) -> BackendCapacity:
        raise BackendConnectivityError("cluster API is unavailable")


class CleanupReconnectBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_attempts = 0

    def cleanup(self, handle: BackendHandle) -> None:
        self.cleanup_attempts += 1
        if self.cleanup_attempts == 1:
            raise BackendConnectivityError("cleanup connection dropped")
        super().cleanup(handle)


class MixedStateBackend(ImmediateBackend):
    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        if handle.workload_id == "needs-attention":
            return BackendSnapshot(
                phase="unknown",
                reason="UnexpectedState",
            )
        return BackendSnapshot(phase="running")


def _workload(
    trial: Path,
    campaign_trial: CampaignTrial,
    plan: CampaignPlan,
    definition: Any,
    backend_name: str,
) -> WorkloadSpec:
    return WorkloadSpec(
        workload_id=trial.name,
        trial=trial,
        command=("unused",),
        timeout_seconds=10,
    )


def test_campaign_runs_explicit_list_collects_and_renders_dashboard(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    plan = CampaignPlan(
        campaign_id="smoke",
        root=tmp_path / "campaign",
        trials=(
            CampaignTrial("run-a", "codex", "model-a", effort="high"),
            CampaignTrial("run-b", "codex", "model-a", effort="high"),
        ),
        max_parallel=2,
    )
    runner = CampaignRunner(
        definition,
        contract,
        plan,
        backend,
        workload_factory=_workload,
    )

    submitted = runner.advance()
    completed = runner.advance()

    assert submitted["status"] == "running"
    assert completed["status"] == "complete"
    assert {
        trial["outcome"] for trial in completed["trials"]
    } == {"succeeded"}
    assert {
        trial["test_id"] for trial in completed["trials"]
    } == {"run-a", "run-b"}
    assert len(backend.cleaned) == 2
    dashboard = plan.root / "index.html"
    assert dashboard.is_file()
    assert "model-a" in dashboard.read_text()


def test_campaign_pauses_when_backend_is_unreachable(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="offline",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("offline-a", "codex", "model-a"),),
        ),
        OfflineBackend(),
        workload_factory=_workload,
    )

    state = runner.advance()

    assert state["status"] == "paused_backend_connectivity"
    assert state["trials"][0]["phase"] == "pending"


def test_campaign_resumes_cleanup_after_connectivity_loss(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = CleanupReconnectBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="cleanup",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("cleanup-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    paused = runner.advance()
    resumed = runner.advance()

    assert paused["status"] == "paused_backend_connectivity"
    assert paused["trials"][0]["phase"] == "cleanup_pending"
    assert resumed["status"] == "complete"
    assert backend.cleanup_attempts == 2


def test_required_assessment_failure_marks_campaign_trial_failed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="assessment-failure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("assessment-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )

    monkeypatch.setattr(
        "brunner.campaign.evaluate_trial",
        lambda *args: {
            "status": "complete",
            "assessment_status": "failed",
            "required_assessments_complete": False,
            "assessments": [
                {
                    "assessment_id": "required-review",
                    "status": "failed",
                    "reports": [],
                }
            ],
        },
    )

    runner.advance()
    completed = runner.advance()

    entry = completed["trials"][0]
    assert entry["phase"] == "complete"
    assert entry["outcome"] == "failed"
    assert entry["evaluation"]["assessment_status"] == "failed"


def test_campaign_appends_new_ids_without_invalidating_completed_work(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    root = tmp_path / "campaign"
    first = CampaignTrial("chosen-id", "codex", "same-model", effort="high")
    first_runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="flexible",
            root=root,
            trials=(first,),
        ),
        backend,
        workload_factory=_workload,
    )
    first_runner.advance()
    completed = first_runner.advance()
    first_completed_at = completed["trials"][0]["completed_at"]

    second_runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="flexible",
            root=root,
            trials=(
                CampaignTrial(
                    "whatever-id-i-want",
                    "codex",
                    "same-model",
                    effort="high",
                ),
                first,
            ),
            max_parallel=2,
        ),
        backend,
        workload_factory=_workload,
    )
    reconciled = second_runner.initialize()

    by_id = {
        trial["test_id"]: trial for trial in reconciled["trials"]
    }
    assert reconciled["status"] == "running"
    assert "plan_sha256" not in reconciled
    assert by_id["chosen-id"]["phase"] == "complete"
    assert by_id["chosen-id"]["completed_at"] == first_completed_at
    assert by_id["whatever-id-i-want"]["phase"] == "pending"

    second_runner.advance()
    final = second_runner.advance()
    assert final["status"] == "complete"
    assert {
        trial["test_id"] for trial in final["trials"]
    } == {"chosen-id", "whatever-id-i-want"}


def test_campaign_rejects_only_conflicting_reuse_of_an_id(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    root = tmp_path / "campaign"
    CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="identity",
            root=root,
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    ).initialize()
    conflicting = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="identity",
            root=root,
            trials=(CampaignTrial("run-a", "codex", "model-b"),),
        ),
        backend,
        workload_factory=_workload,
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        conflicting.initialize()


def test_duplicate_matching_ids_in_one_list_are_idempotent(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = CampaignTrial("same-id", "codex", "model-a")
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="duplicate",
            root=tmp_path / "campaign",
            trials=(trial, trial),
        ),
        ImmediateBackend(),
        workload_factory=_workload,
    )

    state = runner.initialize()

    assert [entry["test_id"] for entry in state["trials"]] == ["same-id"]


def test_campaign_trial_id_must_be_a_safe_path_segment() -> None:
    with pytest.raises(ValueError, match="safe path segment"):
        CampaignTrial("../escape", "codex", "model-a").validate()


def test_campaign_backend_deadline_includes_shutdown_grace(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    trial = CampaignTrial("deadline", "codex", "model-a")
    workload = default_workload_factory(
        tmp_path,
        trial,
        CampaignPlan(
            campaign_id="deadline",
            root=tmp_path / "campaign",
            trials=(trial,),
        ),
        definition,
        "local",
    )

    assert workload.timeout_seconds == (
        definition.runtime.timeout_seconds
        + definition.runtime.backend_shutdown_grace_seconds
    )


def test_campaign_recovers_interrupted_collection(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="recover-collection",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )
    state = runner.advance()
    state["trials"][0]["phase"] = "collecting"
    runner.state_path.write_text(json.dumps(state))

    completed = runner.advance()

    assert completed["status"] == "complete"
    assert completed["trials"][0]["phase"] == "complete"
    assert backend.collection_calls == 1
    assert any(
        event["type"] == "phase_recovered"
        for event in completed["events"]
    )


def test_campaign_recovers_interrupted_evaluation(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="recover-evaluation",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )
    state = runner.advance()
    entry = state["trials"][0]
    destination = runner.root / "collected" / entry["test_id"]
    handle = entry["handle"]
    backend.collect(
        BackendHandle(
            backend=handle["backend"],
            workload_id=handle["workload_id"],
            native_id=handle["native_id"],
            trial=Path(handle["trial"]),
            metadata=handle["metadata"],
        ),
        destination,
        definition.artifacts,
    )
    entry["collected_trial"] = str(destination)
    entry["backend_phase"] = "succeeded"
    entry["phase"] = "evaluating"
    runner.state_path.write_text(json.dumps(state))

    completed = runner.advance()

    assert completed["status"] == "complete"
    assert completed["trials"][0]["phase"] == "complete"
    assert backend.collection_calls == 1


def test_collection_integrity_failure_is_durable_not_stuck(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="collection-integrity",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        ImmediateBackend(),
        workload_factory=_workload,
    )
    submitted = runner.advance()
    trial = Path(submitted["trials"][0]["trial"])
    (trial / "workspace/escape").symlink_to(
        trial / "workspace/input.txt"
    )

    failed = runner.advance()

    assert failed["status"] == "attention_required"
    assert failed["trials"][0]["phase"] == "collection_failed"
    assert failed["trials"][0]["attempts"]["collection"] == 1

    unchanged = runner.advance()

    assert unchanged["status"] == "attention_required"
    assert unchanged["trials"][0]["phase"] == "collection_failed"
    assert unchanged["trials"][0]["attempts"]["collection"] == 1


def test_attention_on_one_trial_does_not_stop_healthy_work(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="mixed",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial(
                    "needs-attention",
                    "codex",
                    "model-a",
                ),
                CampaignTrial("still-running", "codex", "model-a"),
            ),
            max_parallel=2,
        ),
        MixedStateBackend(),
        workload_factory=_workload,
    )
    runner.advance()

    state = runner.advance()
    by_id = {
        entry["test_id"]: entry for entry in state["trials"]
    }

    assert state["status"] == "running"
    assert state["has_attention"] is True
    assert by_id["needs-attention"]["phase"] == "attention_required"
    assert by_id["still-running"]["phase"] == "running"


def test_attention_on_one_trial_does_not_block_pending_submission(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="mixed-pending",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial(
                    "needs-attention",
                    "codex",
                    "model-a",
                ),
                CampaignTrial("next-run", "codex", "model-a"),
            ),
            max_parallel=1,
        ),
        MixedStateBackend(),
        workload_factory=_workload,
    )
    runner.advance()

    state = runner.advance()
    by_id = {
        entry["test_id"]: entry for entry in state["trials"]
    }

    assert state["status"] == "running"
    assert state["has_attention"] is True
    assert by_id["needs-attention"]["phase"] == "attention_required"
    assert by_id["next-run"]["phase"] == "submitted"
