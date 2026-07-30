from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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


def test_campaign_runs_matrix_collects_evaluates_and_renders_dashboard(
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
            CampaignTrial("codex", "model-a", replicate=1),
            CampaignTrial("claude", "model-b", effort="high", replicate=1),
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
            trials=(CampaignTrial("codex", "model-a"),),
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
            trials=(CampaignTrial("codex", "model-a"),),
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
