from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

import pytest

from brunner.artifacts import collect_local_artifacts
from brunner.backends import (
    BackendCapacity,
    BackendHandle,
    BackendSnapshot,
    TrialContinuation,
    WorkloadSpec,
)
from brunner.campaign import (
    CampaignEngine,
    CampaignPlan as EngineCampaignPlan,
    CampaignTrial,
    default_workload_factory,
)
from brunner.contract import load_output_contract
from brunner.definition import ArtifactPolicy, QualitativeReviewDefinition
from brunner.dashboard import write_campaign_dashboard
from brunner.errors import (
    ArtifactTransferError,
    BackendConfigurationError,
    BackendConnectivityError,
    BackendRequestError,
    EvaluationPending,
)
from brunner.evaluation import (
    evaluation_spec,
    execute_evaluation,
    finalize_evaluation,
)
from brunner.failure import failure_record
from brunner.providers import ProviderSettings
from examples.text_benchmark.definition import build_definition


ROOT = Path(__file__).parents[1]


class CampaignPlan:
    """Test-only adapter for exercising the internal reconciliation engine."""

    def __init__(self, *, root: Path, **values: Any) -> None:
        self.root = root
        self.engine_plan = EngineCampaignPlan(**values)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.engine_plan, name)


def CampaignRunner(
    definition: Any,
    contract: Any,
    plan: CampaignPlan,
    backend: Any,
    **kwargs: Any,
) -> CampaignEngine:
    return CampaignEngine(
        definition,
        contract,
        plan.engine_plan,
        backend,
        control_root=plan.root / "control",
        results_root=plan.root,
        **kwargs,
    )


class ImmediateBackend:
    name = "fake"
    agent_isolation = "container"
    trusted_evaluation = "kubernetes"
    retain_failed_storage = False

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
        final_response = {
            "status": "complete",
            "submission_manifest": "submission/manifest.json",
            "completed_units": ["uppercase"],
            "limitations": [],
        }
        (submission / "run-status.json").write_text(
            json.dumps(final_response)
        )
        (workload.trial / "status.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "final_response": final_response,
                    "attempts": [
                        {
                            "status": "complete",
                            "terminal_result_seen": True,
                        }
                    ],
                }
            )
        )
        assert workload.evaluation is not None
        execute_evaluation(
            replace(
                workload.evaluation,
                command=(
                    sys.executable,
                    str(ROOT / "examples/text_benchmark/evaluator.py"),
                ),
            ),
            workload.trial,
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

    def terminate(
        self,
        handle: BackendHandle,
        *,
        reason: str,
    ) -> BackendSnapshot:
        return BackendSnapshot(
            phase="failed",
            reason=reason,
            details={"retryable_infrastructure": True},
        )

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

    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity:
        return BackendCapacity(
            limit=10,
            running=0,
            pending=0,
            available=10,
        )


class MaterializationCheckingBackend(ImmediateBackend):
    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        assert (
            workload.trial / "workspace/campaign-resource.txt"
        ).read_text() == "materialized before backend submission"
        return super().submit(workload)


class OfflineBackend(ImmediateBackend):
    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity:
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


class CollectionReconnectBackend(ImmediateBackend):
    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        self.collection_calls += 1
        if self.collection_calls == 1:
            raise BackendConnectivityError(
                "artifact reader connection dropped"
            )
        return collect_local_artifacts(
            handle.trial,
            destination,
            policy,
            included_groups=included_groups,
        )


class FlakyConnectivityBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.capacity_attempts = 0

    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity:
        self.capacity_attempts += 1
        if self.capacity_attempts == 1:
            raise BackendConnectivityError("cluster API is unavailable")
        return super().capacity(workload)


class AmbiguousSubmissionBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0
        self.remote_handle: BackendHandle | None = None

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        self.submit_calls += 1
        if self.remote_handle is None:
            self.remote_handle = BackendHandle(
                backend=self.name,
                workload_id=workload.workload_id,
                native_id=f"remote-{workload.workload_id}",
                trial=workload.trial,
                metadata={"submitted_at": datetime.now(UTC).isoformat()},
            )
            raise BackendConnectivityError(
                "connection dropped after remote submission"
            )
        return self.remote_handle

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        return BackendSnapshot(phase="running")


class PartialRequestSubmissionBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0
        self.remote_handle: BackendHandle | None = None

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        self.submit_calls += 1
        if self.remote_handle is None:
            self.remote_handle = super().submit(workload)
            raise BackendRequestError(
                "staging helper failed after PVC and Job creation"
            )
        return self.remote_handle


class RejectedSubmissionBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        self.submit_calls += 1
        raise BackendRequestError("workload violates cluster policy")


class InvalidBackendConfiguration(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        self.submit_calls += 1
        raise BackendConfigurationError(
            "required orchestrator secret is unavailable"
        )


class TransferRetryBackend(ImmediateBackend):
    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        self.collection_calls += 1
        if self.collection_calls == 1:
            raise ArtifactTransferError("artifact stream ended early")
        return collect_local_artifacts(
            handle.trial,
            destination,
            policy,
            included_groups=included_groups,
        )


class PersistentTransferFailureBackend(ImmediateBackend):
    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        self.collection_calls += 1
        raise ArtifactTransferError("artifact stream ended early")


class EmptyLogBackend(ImmediateBackend):
    def logs(self, handle: BackendHandle) -> str:
        return ""


class CleanupRequestFailureBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_attempts = 0

    def cleanup(self, handle: BackendHandle) -> None:
        self.cleanup_attempts += 1
        if self.cleanup_attempts == 1:
            raise BackendRequestError("PVC finalizer is still pending")
        super().cleanup(handle)


class ZeroCapacityBackend(ImmediateBackend):
    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity:
        return BackendCapacity(
            limit=1,
            running=0,
            pending=1,
            available=0,
            details={"reason": "quota exhausted"},
        )


class UnexpectedInspectionBackend(ImmediateBackend):
    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        raise RuntimeError("malformed backend response")


class UnexpectedCapacityBackend(ImmediateBackend):
    def capacity(
        self,
        workload: WorkloadSpec | None = None,
    ) -> BackendCapacity:
        raise RuntimeError("malformed capacity response")


class UnexpectedLogsBackend(ImmediateBackend):
    def logs(self, handle: BackendHandle) -> str:
        raise RuntimeError("malformed log response")


class MixedStateBackend(ImmediateBackend):
    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        if handle.workload_id == "needs-attention":
            return BackendSnapshot(
                phase="unknown",
                reason="UnexpectedState",
            )
        return BackendSnapshot(phase="running")


class EventuallyCompleteBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.inspections: dict[str, int] = {}

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        count = self.inspections.get(handle.workload_id, 0) + 1
        self.inspections[handle.workload_id] = count
        if handle.workload_id == "stuck-a" and count == 1:
            return BackendSnapshot(phase="running")
        return BackendSnapshot(phase="succeeded", exit_code=0)


class RetryableInfrastructureBackend(ImmediateBackend):
    def __init__(self, *, always_fail: bool = False) -> None:
        super().__init__()
        self.always_fail = always_fail
        self.restart_calls = 0

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        if not self.always_fail and handle.native_id.endswith("-r1"):
            return BackendSnapshot(phase="succeeded", exit_code=0)
        return BackendSnapshot(
            phase="failed",
            reason="Evicted",
            exit_code=137,
            details={"retryable_infrastructure": True},
        )

    def restart(
        self,
        handle: BackendHandle,
        workload: WorkloadSpec,
        generation: int,
    ) -> BackendHandle:
        self.restart_calls += 1
        return BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=f"{workload.workload_id}-r{generation}",
            trial=workload.trial,
            metadata={"submitted_at": f"2026-08-04T12:00:0{generation}+00:00"},
        )


class ContinuationBackend(ImmediateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.continuation: TrialContinuation | None = None

    def restart(
        self,
        handle: BackendHandle,
        workload: WorkloadSpec,
        generation: int,
        *,
        continuation: TrialContinuation | None = None,
    ) -> BackendHandle:
        self.continuation = continuation
        return BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=f"{workload.workload_id}-c{generation}",
            trial=workload.trial,
            metadata={"submitted_at": "2026-08-26T12:00:00+00:00"},
        )

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        return BackendSnapshot(phase="running")


class InterruptedInfrastructureBackend(RetryableInfrastructureBackend):
    def __init__(self) -> None:
        super().__init__(always_fail=True)

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        handle = super().submit(workload)
        (workload.trial / "status.json").write_text(
            json.dumps(
                {
                    "status": "interrupted",
                    "attempts": [
                        {
                            "status": "interrupted",
                            "terminal_result_seen": False,
                            "forced_termination_reason": "stop_requested",
                        }
                    ],
                    "interruption": {
                        "signal": 15,
                        "signal_name": "SIGTERM",
                    },
                }
            )
        )
        return handle

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        return BackendSnapshot(
            phase="failed",
            reason="AgentInterrupted",
            exit_code=143,
            details={
                "retryable_infrastructure": True,
                "brunner_pipeline": {
                    "status": "interrupted",
                    "complete": False,
                    "provider_result_present": False,
                    "infrastructure_failure": True,
                    "infrastructure_reason": "AgentInterrupted",
                    "retryable_infrastructure": True,
                    "signal": 15,
                    "signal_name": "SIGTERM",
                },
            },
        )


class HostProcessBackend(ImmediateBackend):
    agent_isolation = "host"


class LocalEvaluationBackend(ImmediateBackend):
    trusted_evaluation = "unsupported"


def _workload(
    trial: Path,
    campaign_trial: CampaignTrial,
    plan: CampaignPlan,
    definition: Any,
    backend_name: str,
) -> WorkloadSpec:
    contract = load_output_contract(definition.contract_path)
    trusted_evaluation = evaluation_spec(definition, contract)
    return WorkloadSpec(
        workload_id=trial.name,
        trial=trial,
        command=("unused",),
        timeout_seconds=10,
        image=campaign_trial.backend_image or plan.backend_image,
        evaluation=trusted_evaluation,
    )


def test_campaign_rejects_host_process_backend(tmp_path: Path) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    plan = CampaignPlan(
        campaign_id="unsafe",
        root=tmp_path / "campaign",
        trials=(CampaignTrial("unsafe-a", "codex", "model-a"),),
    )

    with pytest.raises(ValueError, match="container isolation boundary"):
        CampaignRunner(
            definition,
            contract,
            plan,
            HostProcessBackend(),
            workload_factory=_workload,
        )


def test_campaign_rejects_local_evaluation_backend(tmp_path: Path) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    plan = CampaignPlan(
        campaign_id="unsafe-evaluation",
        root=tmp_path / "campaign",
        trials=(CampaignTrial("unsafe-a", "codex", "model-a"),),
    )

    with pytest.raises(ValueError, match="local evaluation is not supported"):
        CampaignRunner(
            definition,
            contract,
            plan,
            LocalEvaluationBackend(),
            workload_factory=_workload,
        )


def test_campaign_rejects_workload_without_exact_evaluator(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    plan = CampaignPlan(
        campaign_id="missing-evaluation",
        root=tmp_path / "campaign",
        trials=(CampaignTrial("unsafe-a", "codex", "model-a"),),
    )

    def missing_evaluation(*args, **kwargs):
        trial = args[0]
        return WorkloadSpec(
            workload_id=trial.name,
            trial=trial,
            command=("unused",),
            timeout_seconds=10,
        )

    runner = CampaignRunner(
        definition,
        contract,
        plan,
        ImmediateBackend(),
        workload_factory=missing_evaluation,
    )

    state = runner.advance()

    entry = state["trials"][0]
    assert entry["phase"] == "attention_required"
    assert entry["failure"]["domain"] == "configuration"
    assert "exact Sterling evaluation" in entry["error"]


def test_campaign_engine_does_not_own_process_locking(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    plan = CampaignPlan(
        campaign_id="locked",
        root=tmp_path / "campaign",
        trials=(CampaignTrial("locked-a", "codex", "model-a"),),
    )
    first = CampaignRunner(
        definition,
        contract,
        plan,
        ImmediateBackend(),
        workload_factory=_workload,
    )
    second = CampaignRunner(
        definition,
        contract,
        plan,
        ImmediateBackend(),
        workload_factory=_workload,
    )

    first.initialize()
    second.initialize()

    assert not hasattr(first, "_campaign_lock")
    assert not (plan.root / "campaign.lock").exists()


def test_campaign_lock_is_released_after_runner_exits(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    plan = CampaignPlan(
        campaign_id="released",
        root=tmp_path / "campaign",
        trials=(CampaignTrial("released-a", "codex", "model-a"),),
    )
    first = CampaignRunner(
        definition,
        contract,
        plan,
        ImmediateBackend(),
        workload_factory=_workload,
    )
    second = CampaignRunner(
        definition,
        contract,
        plan,
        ImmediateBackend(),
        workload_factory=_workload,
    )

    first.initialize()
    state = second.advance()

    assert state["trials"][0]["phase"] == "submitted"


def test_campaign_runs_explicit_list_collects_and_renders_dashboard(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
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
    rendered = dashboard.read_text()
    assert "model-a" in rendered
    assert "<th>Pipeline</th><th>Benchmark</th>" in rendered
    assert completed["trials"][0]["pipeline"]["status"] == "complete"
    assert completed["trials"][0]["benchmark"]["succeeded"] is True


def test_campaign_evaluates_completed_trial_before_admitting_next(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    calls = 0

    def pending_once(trial: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EvaluationPending("assessment Job submitted")
        return finalize_evaluation(definition, contract, trial)

    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="evaluation-priority",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial("run-a", "codex", "model-a"),
                CampaignTrial("run-b", "codex", "model-a"),
            ),
            max_parallel=1,
        ),
        backend,
        workload_factory=_workload,
        evaluation_finalizer=pending_once,
    )

    runner.advance()
    waiting = runner.advance()
    by_id = {entry["test_id"]: entry for entry in waiting["trials"]}

    assert by_id["run-a"]["phase"] == "evaluation_pending"
    assert by_id["run-b"]["phase"] == "pending"
    assert waiting["scheduler_wait"]["kind"] == "evaluation_priority"
    assert waiting["scheduler_wait"]["trials"] == ["run-a"]
    assert waiting["scheduler_wait"]["since"]
    assert len(backend.handles) == 1

    resumed = runner.advance()
    by_id = {entry["test_id"]: entry for entry in resumed["trials"]}

    assert by_id["run-a"]["phase"] in {"cleanup_pending", "complete"}
    assert by_id["run-b"]["phase"] == "submitted"
    assert len(backend.handles) == 2
    assert "scheduler_wait" not in resumed
    assert any(
        event["type"] == "evaluation_priority_resumed"
        for event in resumed["events"]
    )


def test_campaign_retries_results_pvc_publication_before_cleanup(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    plan = CampaignPlan(
        campaign_id="publication-retry",
        root=tmp_path / "campaign",
        trials=(CampaignTrial("run-a", "codex", "model-a"),),
        publication_retry_seconds=0,
    )
    calls = 0

    def publish(source: Path, entry: dict[str, Any]) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("results PVC temporarily unavailable")
        return source

    runner = CampaignRunner(
        definition,
        contract,
        plan,
        backend,
        workload_factory=_workload,
        result_publisher=publish,
    )

    runner.advance()
    waiting = runner.advance()

    assert waiting["trials"][0]["phase"] == "publication_pending"
    assert waiting["trials"][0]["attempts"]["publication"] == 1
    assert backend.cleaned == set()

    completed = runner.advance()

    assert completed["trials"][0]["phase"] == "complete"
    assert backend.cleaned == {"run-a"}


def test_campaign_materializes_before_backend_submission(
    tmp_path: Path,
) -> None:
    challenge_root = tmp_path / "challenge"
    shutil.copytree(
        ROOT / "examples/text_benchmark/challenge",
        challenge_root,
    )
    script = tmp_path / "materialize.py"
    script.write_text(
        """
import os
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "campaign-resource.txt").write_text(
    "materialized before backend submission"
)
"""
    )
    base = build_definition()
    definition = replace(
        base,
        challenge=replace(
            base.challenge,
            root=challenge_root,
            materialize_command=(sys.executable, str(script)),
        ),
    )
    contract = load_output_contract(definition.contract_path)
    plan = CampaignPlan(
        campaign_id="materialized",
        root=tmp_path / "campaign",
        trials=(
            CampaignTrial(
                "materialized-a",
                "codex",
                "model-a",
            ),
        ),
    )
    runner = CampaignRunner(
        definition,
        contract,
        plan,
        MaterializationCheckingBackend(),
        workload_factory=_workload,
    )

    state = runner.advance()

    assert state["status"] == "running"
    assert state["trials"][0]["phase"] == "submitted"


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


def test_campaign_run_waits_for_connectivity_and_resumes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = FlakyConnectivityBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="connectivity-retry",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )

    state = runner.run(poll_seconds=0.001)

    assert state["status"] == "complete"
    assert backend.capacity_attempts >= 2
    assert any(
        event["type"] == "backend_connectivity"
        for event in state["events"]
    )


def test_campaign_recovers_ambiguous_submission_by_adopting_workload(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = AmbiguousSubmissionBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="ambiguous-submit",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )

    paused = runner.advance()
    resumed = runner.advance()

    assert paused["status"] == "paused_backend_connectivity"
    assert paused["trials"][0]["phase"] == "submitting"
    assert paused["trials"][0].get("handle") is None
    assert resumed["status"] == "running"
    assert resumed["trials"][0]["phase"] == "running"
    assert resumed["trials"][0]["handle"]["native_id"] == "remote-run-a"
    assert resumed["trials"][0]["submitted_at"]
    assert backend.submit_calls == 2


def test_campaign_reconciles_partial_nonconnectivity_submission(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = PartialRequestSubmissionBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="partial-request-submit",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial("run-a", "codex", "model-a"),
                CampaignTrial("run-b", "codex", "model-a"),
            ),
            max_parallel=1,
            submission_retry_seconds=0,
            submission_max_attempts=2,
        ),
        backend,
        workload_factory=_workload,
    )

    waiting = runner.advance()
    reconciled = runner.advance()

    first = waiting["trials"][0]
    assert first["phase"] == "submission_retry_wait"
    assert first["failure"]["disposition"] == "retry"
    assert waiting["trials"][1]["phase"] == "pending"
    assert reconciled["trials"][0]["phase"] == "complete"
    assert reconciled["trials"][1]["phase"] == "submitted"
    assert backend.submit_calls == 3


def test_campaign_bounds_deterministic_submission_retries(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = RejectedSubmissionBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="rejected-submit",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
            submission_retry_seconds=0,
            submission_max_attempts=2,
        ),
        backend,
        workload_factory=_workload,
    )

    waiting = runner.advance()
    failed = runner.advance()

    assert waiting["trials"][0]["phase"] == "submission_retry_wait"
    assert failed["status"] == "attention_required"
    assert failed["trials"][0]["phase"] == "attention_required"
    assert failed["trials"][0]["attempts"]["submission"] == 2
    assert failed["trials"][0]["failure"]["cleanup_required"] is True
    assert backend.submit_calls == 2


def test_campaign_does_not_retry_backend_configuration_failure(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = InvalidBackendConfiguration()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="invalid-backend-configuration",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
            submission_retry_seconds=0,
            submission_max_attempts=10,
        ),
        backend,
        workload_factory=_workload,
    )

    state = runner.advance()

    entry = state["trials"][0]
    assert state["status"] == "attention_required"
    assert entry["phase"] == "attention_required"
    assert entry["attempts"]["submission"] == 1
    assert entry["failure"]["domain"] == "configuration"
    assert entry["failure"]["reason"] == "BackendConfigurationFailed"
    assert entry["failure"]["retryable"] is False
    assert backend.submit_calls == 1


def test_campaign_resumes_cleanup_after_connectivity_loss(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
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
    assert [
        event["type"]
        for event in resumed["events"]
        if event["test_id"] == "cleanup-a"
        and event["type"] in {"trial_complete", "cleanup_complete"}
    ] == ["trial_complete"]


def test_campaign_retries_nonconnectivity_cleanup_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = CleanupRequestFailureBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="cleanup-request-failure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("cleanup-a", "codex", "model-a"),),
            cleanup_retry_seconds=0,
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    waiting = runner.advance()
    completed = runner.advance()

    entry = waiting["trials"][0]
    assert waiting["status"] == "running"
    assert waiting["has_attention"] is True
    assert entry["phase"] == "cleanup_pending"
    assert entry["failure"]["domain"] == "cleanup"
    assert entry["failure"]["disposition"] == "retry"
    assert completed["status"] == "complete"
    assert backend.cleanup_attempts == 2


def test_campaign_dashboard_failure_does_not_block_cleanup(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    monkeypatch.setattr(
        "brunner.dashboard.write_campaign_dashboard",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("dashboard filesystem unavailable")
        ),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="dashboard-failure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    completed = runner.advance()

    assert completed["status"] == "complete"
    assert completed["dashboard"]["status"] == "failed"
    assert completed["dashboard"]["failure"]["domain"] == "reporting"
    assert backend.cleaned == {"run-a"}
    persisted = json.loads(runner.state_path.read_text())
    assert persisted["status"] == "complete"


def test_collection_connectivity_pause_does_not_consume_attempt(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = CollectionReconnectBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="collection-connectivity",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
            collection_max_attempts=1,
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    paused = runner.advance()
    completed = runner.advance()

    paused_entry = paused["trials"][0]
    assert paused["status"] == "paused_backend_connectivity"
    assert paused_entry["phase"] == "collecting"
    assert paused_entry["attempts"]["collection"] == 0
    assert paused_entry["collection_attempt"]["number"] == 1
    completed_entry = completed["trials"][0]
    assert completed["status"] == "complete"
    assert completed_entry["attempts"]["collection"] == 1
    assert "collection_attempt" not in completed_entry
    assert backend.collection_calls == 2


def test_campaign_retries_transient_artifact_transfer(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = TransferRetryBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="artifact-retry",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
            collection_retry_seconds=0,
            collection_max_attempts=2,
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    waiting = runner.advance()
    completed = runner.advance()

    assert waiting["status"] == "running"
    assert waiting["trials"][0]["phase"] == "collection_retry_wait"
    assert completed["status"] == "complete"
    assert completed["trials"][0]["attempts"]["collection"] == 2
    assert backend.collection_calls == 2


def test_campaign_stops_retrying_artifact_transfer_at_limit(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = PersistentTransferFailureBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="artifact-retry-limit",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
            collection_retry_seconds=0,
            collection_max_attempts=2,
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    waiting = runner.advance()
    failed = runner.advance()
    unchanged = runner.advance()

    assert waiting["trials"][0]["phase"] == "collection_retry_wait"
    assert failed["status"] == "attention_required"
    assert failed["trials"][0]["phase"] == "collection_failed"
    assert failed["trials"][0]["attempts"]["collection"] == 2
    assert unchanged["trials"][0]["attempts"]["collection"] == 2
    assert backend.collection_calls == 2


def test_empty_backend_logs_do_not_overwrite_recovered_log(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = EmptyLogBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="preserve-log",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )

    submitted = runner.advance()
    trial = Path(submitted["trials"][0]["trial"])
    log_path = trial / "backend/workload.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("previously recovered log\n")

    completed = runner.advance()

    assert completed["status"] == "complete"
    assert log_path.read_text() == "previously recovered log\n"


def test_dashboard_shows_live_elapsed_time_and_backend_warning(
    tmp_path: Path,
) -> None:
    output = tmp_path / "index.html"

    write_campaign_dashboard(
        {
            "campaign_id": "dashboard",
            "status": "running",
            "benchmark_id": "benchmark",
            "trials": [
                {
                    "test_id": "run-a",
                    "provider": "codex",
                    "model": "model-a",
                    "effort": "high",
                    "phase": "pending",
                    "submitted_at": "2026-07-31T10:00:00+00:00",
                    "backend_snapshot": {
                        "warnings": [
                            (
                                "FailedScheduling: 0/15 nodes are available: "
                                "pod has unbound immediate "
                                "PersistentVolumeClaims. preemption: 0/15 "
                                "nodes are available: 15 Preemption is not "
                                "helpful for scheduling."
                            ),
                            "PVC data: ProvisioningFailed: storage offline"
                        ]
                    },
                }
            ],
            "events": [],
        },
        output,
        now=datetime(2026, 7, 31, 11, 2, 3, tzinfo=UTC),
    )

    rendered = output.read_text()
    assert "<th>Elapsed</th>" in rendered
    assert "1h 2m 3s" in rendered
    assert "ProvisioningFailed: storage offline" in rendered
    assert "unbound immediate PersistentVolumeClaims" not in rendered


def test_dashboard_prefers_styled_assessment_report(
    tmp_path: Path,
) -> None:
    output = tmp_path / "index.html"
    collected = tmp_path / "collected" / "run-a"

    write_campaign_dashboard(
        {
            "campaign_id": "dashboard",
            "status": "complete",
            "benchmark_id": "benchmark",
            "trials": [
                {
                    "test_id": "run-a",
                    "provider": "codex",
                    "model": "model-a",
                    "phase": "complete",
                    "collected_trial": str(collected),
                    "evaluation": {
                        "assessments": [
                            {
                                "assessment_id": "qualitative-review",
                                "output": {
                                    "path": (
                                        "evaluation/qualitative-review.json"
                                    )
                                },
                                "reports": [
                                    {
                                        "path": (
                                            "evaluation/"
                                            "qualitative-review.json"
                                        ),
                                        "media_type": "application/json",
                                    },
                                    {
                                        "path": (
                                            "evaluation/"
                                            "qualitative-review.html"
                                        ),
                                        "media_type": "text/html",
                                    },
                                ],
                            }
                        ]
                    },
                }
            ],
            "events": [],
        },
        output,
    )

    rendered = output.read_text()
    assert "qualitative-review.json" not in rendered
    assert rendered.count("qualitative-review.html") == 1


def test_dashboard_embeds_primary_benchmark_report_from_results(
    tmp_path: Path,
) -> None:
    output = tmp_path / "index.html"
    collected = tmp_path / "collected" / "run-a"
    evaluation = collected / "evaluation"
    evaluation.mkdir(parents=True)
    comparison = evaluation / "comparison.html"
    comparison.write_text("<html><body>physical diagnostics</body></html>")
    details = evaluation / "details.json"
    details.write_text("{}")
    results = evaluation / "results.json"
    results.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "path": "evaluation/comparison.html",
                        "media_type": "text/html",
                        "title": "Physical comparison",
                        "primary": True,
                    },
                    {
                        "path": "evaluation/details.json",
                        "media_type": "application/json",
                        "title": "Deterministic metrics",
                    },
                ]
            }
        )
    )

    write_campaign_dashboard(
        {
            "campaign_id": "dashboard",
            "status": "complete",
            "benchmark_id": "benchmark",
            "trials": [
                {
                    "test_id": "run-a",
                    "provider": "codex",
                    "model": "model-a",
                    "phase": "complete",
                    "collected_trial": str(collected),
                    "evaluation": {
                        "results": str(results),
                        "report": str(evaluation / "run-report.html"),
                    },
                }
            ],
            "events": [],
        },
        output,
    )

    rendered = output.read_text()
    assert "<h2>Benchmark reports</h2>" in rendered
    assert "Physical comparison" in rendered
    assert "Deterministic metrics" in rendered
    assert "run details" in rendered
    assert (
        "src='collected/run-a/evaluation/comparison.html'" in rendered
    )
    assert "sandbox" in rendered


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
        "brunner.campaign.finalize_evaluation",
        lambda *args, **kwargs: {
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
            "reports": [
                {
                    "path": "evaluation/comparison.html",
                    "media_type": "text/html",
                    "title": "Physical comparison",
                    "primary": True,
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
    assert entry["evaluation"]["reports"] == [
        {
            "path": "evaluation/comparison.html",
            "media_type": "text/html",
            "title": "Physical comparison",
            "primary": True,
        }
    ]
    assert entry["benchmark"]["succeeded"] is None
    assert entry["failure_class"] == "infrastructure"
    assert entry["failure"]["domain"] == "assessment"


def test_evaluator_infrastructure_failure_is_not_a_benchmark_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="evaluation-infrastructure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        ImmediateBackend(),
        workload_factory=_workload,
    )
    evaluator_failure = failure_record(
        operation="evaluator_execution",
        domain="evaluation",
        reason="EvaluatorFailed",
        message="evaluator image could not start",
        disposition="attention",
        retryable=False,
    )
    monkeypatch.setattr(
        "brunner.campaign.finalize_evaluation",
        lambda *args, **kwargs: {
            "status": "failed",
            "assessment_status": "not_configured",
            "required_assessments_complete": True,
            "assessments": [],
            "failure": evaluator_failure,
        },
    )

    runner.advance()
    completed = runner.advance()

    entry = completed["trials"][0]
    assert entry["phase"] == "complete"
    assert entry["outcome"] == "failed"
    assert entry["benchmark"]["succeeded"] is None
    assert entry["failure_class"] == "infrastructure"
    assert entry["failure"]["domain"] == "evaluation"


def test_candidate_submission_failure_remains_a_benchmark_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="candidate-failure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        ImmediateBackend(),
        workload_factory=_workload,
    )
    candidate_failure = failure_record(
        operation="submission_validation",
        domain="candidate",
        reason="CandidateSubmissionInvalid",
        message="submission manifest is invalid",
        disposition="candidate_failed",
        retryable=False,
    )
    monkeypatch.setattr(
        "brunner.campaign.finalize_evaluation",
        lambda *args, **kwargs: {
            "status": "failed",
            "assessment_status": "not_configured",
            "required_assessments_complete": True,
            "assessments": [],
            "failure": candidate_failure,
        },
    )

    runner.advance()
    completed = runner.advance()

    entry = completed["trials"][0]
    assert entry["benchmark"]["succeeded"] is False
    assert entry["failure_class"] == "benchmark"
    assert entry["failure"]["domain"] == "candidate"


def test_campaign_appends_new_ids_without_invalidating_completed_work(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
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
            backend_image="example.invalid/agent@sha256:old",
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
                replace(
                    first,
                    backend_image=(
                        "example.invalid/agent@sha256:old"
                    ),
                ),
            ),
            max_parallel=2,
            backend_image="example.invalid/agent@sha256:new",
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
    assert by_id["chosen-id"]["backend_image"] == (
        "example.invalid/agent@sha256:old"
    )
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


def test_unknown_persisted_phase_becomes_durable_attention(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="invalid-phase",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        ImmediateBackend(),
        workload_factory=_workload,
    )
    state = runner.initialize()
    state["trials"][0]["phase"] = "lost_between_dimensions"
    runner.state_path.write_text(json.dumps(state))

    reconciled = runner.advance()

    entry = reconciled["trials"][0]
    assert reconciled["status"] == "attention_required"
    assert entry["phase"] == "attention_required"
    assert entry["invalid_phase"] == "lost_between_dimensions"
    assert entry["failure"]["reason"] == "InvalidCampaignPhase"


def test_campaign_recovers_corrupt_primary_state_from_backup(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="state-backup",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        ImmediateBackend(),
        workload_factory=_workload,
    )
    runner.initialize()
    runner.state_path.write_text("{not-json")

    recovered = runner.advance()

    assert recovered["trials"][0]["phase"] == "submitted"
    assert recovered["state_recovery"]["source"].endswith(
        "campaign.json.bak"
    )
    assert any(
        event["type"] == "campaign_state_recovered"
        for event in recovered["events"]
    )
    assert json.loads(runner.state_path.read_text())["campaign_id"] == (
        "state-backup"
    )


def test_zero_backend_capacity_is_visible_in_campaign_state(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ZeroCapacityBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="zero-capacity",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )

    state = runner.advance()

    assert state["status"] == "running"
    assert state["has_attention"] is False
    assert state["scheduler_wait"]["kind"] == "backend_capacity"
    assert state["backend_capacity"]["available"] == 0
    assert backend.handles == {}


def test_unexpected_backend_inspection_failure_is_durable(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="inspection-failure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        UnexpectedInspectionBackend(),
        workload_factory=_workload,
    )
    runner.advance()

    failed = runner.advance()

    entry = failed["trials"][0]
    assert failed["status"] == "attention_required"
    assert entry["phase"] == "attention_required"
    assert entry["failure"]["reason"] == (
        "UnclassifiedBackendInspectionFailure"
    )


def test_unexpected_backend_capacity_failure_is_durable(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="capacity-failure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        UnexpectedCapacityBackend(),
        workload_factory=_workload,
    )

    state = runner.advance()

    assert state["status"] == "running"
    assert state["has_attention"] is True
    assert state["failure"]["reason"] == (
        "UnclassifiedBackendCapacityFailure"
    )


def test_workload_configuration_failure_is_durable(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)

    def broken_workload(*args: object) -> WorkloadSpec:
        raise ValueError("invalid campaign image selection")

    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="workload-configuration",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        ImmediateBackend(),
        workload_factory=broken_workload,
    )

    state = runner.advance()

    entry = state["trials"][0]
    assert state["status"] == "attention_required"
    assert entry["failure"]["domain"] == "configuration"
    assert entry["failure"]["reason"] == "WorkloadConfigurationFailed"


def test_unexpected_backend_log_failure_does_not_block_collection(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="log-failure",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("run-a", "codex", "model-a"),),
        ),
        UnexpectedLogsBackend(),
        workload_factory=_workload,
    )
    runner.advance()

    completed = runner.advance()

    entry = completed["trials"][0]
    assert completed["status"] == "complete"
    assert entry["outcome"] == "succeeded"
    assert entry["log_warning"] == "malformed log response"
    assert any(
        failure["reason"] == "UnclassifiedBackendLogFailure"
        for failure in entry["failures"]
    )


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


def test_campaign_workload_includes_agent_and_sterling_evaluator(
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
            evaluation_timeout_seconds=90,
        ),
        definition,
        "kubernetes",
    )

    assert workload.command[:3] == (
        "python",
        "-m",
        "brunner.agent_cli",
    )
    assert workload.command[3] == "/brunner/trial"
    assert workload.timeout_seconds == (
        definition.runtime.timeout_seconds
        + definition.runtime.backend_shutdown_grace_seconds
    )
    assert workload.evaluation is not None
    assert workload.evaluation.image == definition.evaluation.image
    assert workload.evaluation.command == definition.evaluation.command
    assert workload.evaluation.timeout_seconds == 90


def test_campaign_workload_passes_custom_provider_connection(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    trial = CampaignTrial(
        "azure",
        "codex",
        "deployment-name",
        effort="low",
        provider_id="azure",
        provider_name="Example Azure OpenAI",
        base_url="https://example.openai.azure.com/openai/v1/",
        environment_key="AZURE_OPENAI_API_KEY",
    )
    plan = CampaignPlan(
        campaign_id="custom-provider",
        root=tmp_path / "campaign",
        trials=(trial,),
    )

    workload = default_workload_factory(
        tmp_path,
        trial,
        plan,
        definition,
        "kubernetes",
    )

    assert trial.to_dict()["provider_connection"] == {
        "provider_id": "azure",
        "provider_name": "Example Azure OpenAI",
        "base_url": "https://example.openai.azure.com/openai/v1/",
        "environment_key": "AZURE_OPENAI_API_KEY",
    }
    assert workload.command[-8:] == (
        "--provider-id",
        "azure",
        "--provider-name",
        "Example Azure OpenAI",
        "--environment-key",
        "AZURE_OPENAI_API_KEY",
        "--base-url",
        "https://example.openai.azure.com/openai/v1/",
    )


def test_campaign_trial_rejects_connection_settings_without_provider_id() -> None:
    with pytest.raises(
        ValueError,
        match="custom provider connection settings require provider_id",
    ):
        CampaignTrial(
            "invalid",
            "codex",
            "model-a",
            base_url="https://example.invalid/v1/",
        ).validate()


def test_campaign_reconciliation_preserves_custom_provider_connection(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = CampaignTrial(
        "azure",
        "codex",
        "deployment-name",
        effort="low",
        provider_id="azure",
        provider_name="Example Azure OpenAI",
        base_url="https://example.openai.azure.com/openai/v1/",
        environment_key="AZURE_OPENAI_API_KEY",
    )
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="custom-provider-reconciliation",
            root=tmp_path / "campaign",
            trials=(trial,),
        ),
        ImmediateBackend(),
    )

    state = runner.initialize()
    entry = state["trials"][0]
    workload = runner._configured_workload(entry)

    assert entry["provider_connection"] == {
        "provider_id": "azure",
        "provider_name": "Example Azure OpenAI",
        "base_url": "https://example.openai.azure.com/openai/v1/",
        "environment_key": "AZURE_OPENAI_API_KEY",
    }
    assert "--provider-id" in workload.command
    assert "https://example.openai.azure.com/openai/v1/" in (
        workload.command
    )


def test_default_workload_factory_selects_provider_secret(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    codex_trial = CampaignTrial("codex", "codex", "model-a")
    claude_trial = CampaignTrial("claude", "claude", "model-b")
    plan = CampaignPlan(
        campaign_id="provider-secrets",
        root=tmp_path / "campaign",
        trials=(codex_trial, claude_trial),
        provider_secret_environment={
            "codex": {
                "OPENAI_API_KEY": (
                    "codex-credentials",
                    "OPENAI_API_KEY",
                )
            },
            "claude": {
                "CLAUDE_CODE_OAUTH_TOKEN": (
                    "claude-credentials",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                )
            },
        },
    )

    codex = default_workload_factory(
        tmp_path,
        codex_trial,
        plan,
        definition,
        "kubernetes",
    )
    claude = default_workload_factory(
        tmp_path,
        claude_trial,
        plan,
        definition,
        "kubernetes",
    )

    assert codex.secret_environment == {
        "OPENAI_API_KEY": (
            "codex-credentials",
            "OPENAI_API_KEY",
        )
    }
    assert claude.secret_environment == {
        "CLAUDE_CODE_OAUTH_TOKEN": (
            "claude-credentials",
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
    }
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in codex.secret_environment
    assert "OPENAI_API_KEY" not in claude.secret_environment


def test_default_workload_factory_preserves_burst_resources(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    trial = CampaignTrial("burstable", "codex", "model-a")
    workload = default_workload_factory(
        tmp_path,
        trial,
        CampaignPlan(
            campaign_id="burstable",
            root=tmp_path / "campaign",
            trials=(trial,),
            cpu_request="2",
            cpu_limit="8",
            memory_request="8Gi",
            memory_limit="32Gi",
            ephemeral_storage_request="1Gi",
            ephemeral_storage_limit="3Gi",
        ),
        definition,
        "kubernetes",
    )

    assert workload.cpu_request == "2"
    assert workload.cpu_limit == "8"
    assert workload.memory_request == "8Gi"
    assert workload.memory_limit == "32Gi"
    assert workload.ephemeral_storage_request == "1Gi"
    assert workload.ephemeral_storage_limit == "3Gi"


def test_default_workload_factory_prefers_trial_backend_image(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    trial = CampaignTrial(
        "image-override",
        "codex",
        "model-a",
        backend_image="example.invalid/agent@sha256:trial",
    )
    workload = default_workload_factory(
        tmp_path,
        trial,
        CampaignPlan(
            campaign_id="image-override",
            root=tmp_path / "campaign",
            trials=(trial,),
            backend_image="example.invalid/agent@sha256:campaign",
        ),
        definition,
        "kubernetes",
    )

    assert workload.image == "example.invalid/agent@sha256:trial"


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
    state["trials"][0]["attempts"]["collection"] = 1
    runner.state_path.write_text(json.dumps(state))

    completed = runner.advance()

    assert completed["status"] == "complete"
    assert completed["trials"][0]["phase"] == "complete"
    assert completed["trials"][0]["attempts"]["collection"] == 1
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
    destination = runner.control_root / "collected" / entry["test_id"]
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


class StuckBackend(ImmediateBackend):
    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        return BackendSnapshot(phase="running")


def test_campaign_terminates_trial_that_never_leaves_running(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="stuck",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("stuck-a", "codex", "model-a"),),
            trial_timeout_seconds=0.05,
            infrastructure_max_restarts=0,
        ),
        StuckBackend(),
        workload_factory=_workload,
    )

    state = runner.advance()
    assert state["trials"][0]["phase"] == "submitted"

    time.sleep(0.1)
    state = runner.advance()
    assert state["trials"][0]["phase"] == "collection_pending"
    assert "exceeded its" in state["trials"][0]["error"]
    assert state["trials"][0]["attention"]["active"] is False
    assert state["status"] == "running"
    assert state["has_attention"] is False
    assert sum(
        event["type"] == "trial_deadline_exceeded"
        for event in state["events"]
    ) == 1


def test_campaign_running_trial_within_timeout_is_not_flagged(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="patient",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("patient-a", "codex", "model-a"),),
            trial_timeout_seconds=600,
        ),
        StuckBackend(),
        workload_factory=_workload,
    )

    runner.advance()
    state = runner.advance()

    assert state["trials"][0]["phase"] == "running"
    assert state["status"] == "running"


def test_campaign_restarts_retryable_infrastructure_failure(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = RetryableInfrastructureBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="restart",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("restart-a", "codex", "model-a"),),
            infrastructure_max_restarts=2,
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    restarted = runner.advance()
    completed = runner.advance()

    assert restarted["trials"][0]["phase"] == "submitted"
    assert restarted["trials"][0]["handle"]["native_id"] == "restart-a-r1"
    assert restarted["trials"][0]["attempts"]["infrastructure"] == 1
    retry = restarted["trials"][0]["infrastructure_retries"][0]
    assert retry["generation"] == 1
    assert retry["previous_snapshot"]["reason"] == "Evicted"
    assert backend.restart_calls == 1
    assert completed["status"] == "complete"
    assert completed["trials"][0]["outcome"] == "succeeded"


def test_campaign_stops_restarting_after_infrastructure_limit(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = RetryableInfrastructureBackend(always_fail=True)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="restart-limit",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("restart-a", "codex", "model-a"),),
            infrastructure_max_restarts=1,
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    runner.advance()
    completed = runner.advance()

    assert backend.restart_calls == 1
    assert completed["status"] == "complete"
    assert completed["trials"][0]["outcome"] == "failed"
    assert completed["trials"][0]["attempts"]["infrastructure"] == 1


def test_campaign_continues_failed_provider_on_retained_backend(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ContinuationBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="continue",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("continue-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )
    state = runner.initialize()
    entry = state["trials"][0]
    handle = BackendHandle(
        backend=backend.name,
        workload_id="continue-a",
        native_id="continue-a",
        trial=Path(entry["trial"]),
    )
    entry.update(
        {
            "phase": "complete",
            "outcome": "failed",
            "handle": handle.to_dict(),
            "pipeline": {
                "status": "provider_error",
                "provider_result_present": False,
            },
            "benchmark": {"status": "not_run"},
            "evaluation": {"status": "not_run"},
            "collected_trial": str(
                runner.control_root / "collected" / "continue-a"
            ),
        }
    )
    collected = Path(entry["collected_trial"])
    collected.mkdir(parents=True)
    (collected / "old.txt").write_text("old")
    state["status"] = "complete"
    runner._save(state)

    requested = runner.request_continuation(
        request_id="request-1",
        test_id="continue-a",
    )
    advanced = runner.advance()

    assert requested["trials"][0]["phase"] == "continuation_retrying"
    assert advanced["trials"][0]["phase"] == "running"
    assert advanced["trials"][0]["handle"]["native_id"] == "continue-a-c1"
    assert advanced["trials"][0]["outcome"] is None
    assert advanced["trials"][0]["continuations"][-1]["status"] == "running"
    assert backend.continuation == TrialContinuation("request-1")
    assert not collected.exists()


def test_campaign_retains_provider_error_storage_for_continuation(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = ImmediateBackend()
    backend.retain_failed_storage = True
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="retain",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("retain-a", "codex", "model-a"),),
        ),
        backend,
        workload_factory=_workload,
    )
    state = runner.initialize()
    entry = state["trials"][0]
    entry["pipeline"] = {
        "status": "provider_error",
        "provider_result_present": False,
    }
    entry["outcome"] = "failed"
    handle = BackendHandle(
        backend=backend.name,
        workload_id="retain-a",
        native_id="retain-a",
        trial=Path(entry["trial"]),
    )

    runner._cleanup_entry(state, entry, handle)

    assert entry["phase"] == "complete"
    assert entry["backend_storage_retained"] is True
    assert entry["handle"]["metadata"]["retain_storage"] is True


def test_campaign_does_not_evaluate_interrupted_infrastructure_run(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = InterruptedInfrastructureBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="interrupted-limit",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial("interrupted-a", "codex", "model-a"),
            ),
            infrastructure_max_restarts=1,
        ),
        backend,
        workload_factory=_workload,
    )
    monkeypatch.setattr(
        "brunner.campaign.finalize_evaluation",
        lambda *args, **kwargs: pytest.fail(
            "interrupted agent output must not be evaluated"
        ),
    )

    runner.advance()
    restarted = runner.advance()
    completed = runner.advance()

    assert restarted["trials"][0]["attempts"]["infrastructure"] == 1
    entry = completed["trials"][0]
    assert entry["phase"] == "complete"
    assert entry["outcome"] == "failed"
    assert entry["failure_class"] == "infrastructure"
    assert entry["pipeline"]["status"] == "interrupted"
    assert entry["pipeline"]["signal_name"] == "SIGTERM"
    assert entry["benchmark"] == {
        "status": "not_run",
        "succeeded": None,
        "reason": "AgentInterrupted",
    }
    assert entry["evaluation"]["status"] == "not_run"
    assert any(
        event["type"] == "evaluation_skipped"
        for event in completed["events"]
    )


def test_campaign_assesses_incomplete_pipeline_when_configured(
    tmp_path: Path,
) -> None:
    definition = replace(
        build_definition(),
        qualitative_review=QualitativeReviewDefinition(
            reviewer=ProviderSettings(
                provider="codex",
                model="review-model",
            ),
            required=True,
            run_if_evaluation_failed=True,
        ),
    )
    contract = load_output_contract(definition.contract_path)
    backend = InterruptedInfrastructureBackend()
    calls: list[bool] = []

    def finalizer(
        trial: Path,
        *,
        assessment_only: bool = False,
    ) -> dict[str, Any]:
        calls.append(assessment_only)
        return {
            "status": "not_run",
            "assessment_status": "complete",
            "required_assessments_complete": True,
            "assessments": [
                {
                    "assessment_id": "qualitative-review",
                    "status": "complete",
                    "required": True,
                }
            ],
            "reports": [],
        }

    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="interrupted-assessment",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial("interrupted-a", "codex", "model-a"),
            ),
            infrastructure_max_restarts=0,
        ),
        backend,
        workload_factory=_workload,
        evaluation_finalizer=finalizer,
    )

    runner.advance()
    completed = runner.advance()

    entry = completed["trials"][0]
    assert calls == [True]
    assert entry["phase"] == "complete"
    assert entry["outcome"] == "failed"
    assert entry["failure"]["reason"] == "AgentInterrupted"
    assert entry["benchmark"] == {
        "status": "not_run",
        "succeeded": None,
        "reason": "AgentInterrupted",
    }
    assert entry["evaluation"]["status"] == "not_run"
    assert entry["evaluation"]["assessment_status"] == "complete"
    assert entry["evaluation"]["required_assessments_complete"] is True
    assert any(
        event["type"] == "incomplete_trial_assessed"
        for event in completed["events"]
    )


def test_campaign_gives_up_after_prolonged_connectivity_loss(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="offline-limit",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("offline-a", "codex", "model-a"),),
            max_pause_seconds=0.05,
        ),
        OfflineBackend(),
        workload_factory=_workload,
    )

    state = runner.advance()
    assert state["status"] == "paused_backend_connectivity"

    time.sleep(0.1)
    state = runner.advance()

    assert state["status"] == "attention_required"
    assert "unreachable" in state["pause_reason"]


def test_campaign_waits_indefinitely_for_connectivity_by_default(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="offline-default",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("offline-a", "codex", "model-a"),),
        ),
        OfflineBackend(),
        workload_factory=_workload,
    )

    first = runner.advance()
    state_path = runner.state_path
    persisted = json.loads(state_path.read_text())
    persisted["paused_since"] = "2026-08-01T00:00:00+00:00"
    state_path.write_text(json.dumps(persisted))
    later = runner.advance()

    assert first["status"] == "paused_backend_connectivity"
    assert later["status"] == "paused_backend_connectivity"
    assert later["has_attention"] is True
    assert later["paused_since"] == "2026-08-01T00:00:00+00:00"
    assert sum(
        event["type"] == "backend_connectivity"
        for event in later["events"]
    ) == 1


def test_campaign_pause_clock_resets_after_connectivity_returns(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT) + os.pathsep + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = FlakyConnectivityBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="recovered",
            root=tmp_path / "campaign",
            trials=(CampaignTrial("recovered-a", "codex", "model-a"),),
            max_pause_seconds=600,
        ),
        backend,
        workload_factory=_workload,
    )

    state = runner.advance()
    assert state["status"] == "paused_backend_connectivity"
    assert state["paused_since"]

    state = runner.run(poll_seconds=0.01)

    assert "paused_since" not in state


def test_overdue_trial_is_terminated_and_releases_backend_slot(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    backend = EventuallyCompleteBackend()
    runner = CampaignRunner(
        definition,
        contract,
        CampaignPlan(
            campaign_id="capacity",
            root=tmp_path / "campaign",
            trials=(
                CampaignTrial("stuck-a", "codex", "model-a"),
                CampaignTrial("next-run", "codex", "model-a"),
            ),
            max_parallel=1,
            trial_timeout_seconds=0.05,
            infrastructure_max_restarts=0,
        ),
        backend,
        workload_factory=_workload,
    )

    runner.advance()
    time.sleep(0.1)
    overdue = runner.advance()
    by_id = {entry["test_id"]: entry for entry in overdue["trials"]}

    assert by_id["stuck-a"]["phase"] == "collection_pending"
    assert (
        by_id["stuck-a"]["attention"]["kind"]
        == "trial_deadline_exceeded"
    )
    assert by_id["stuck-a"]["attention"]["active"] is False
    assert by_id["next-run"]["phase"] == "pending"
    assert len(backend.handles) == 1
    assert overdue["scheduler_wait"]["kind"] == "evaluation_priority"
    assert overdue["status"] == "running"
    assert overdue["has_attention"] is False

    resumed = runner.advance()
    by_id = {entry["test_id"]: entry for entry in resumed["trials"]}

    assert by_id["stuck-a"]["phase"] in {
        "cleanup_pending",
        "complete",
    }
    assert by_id["next-run"]["phase"] == "submitted"
    assert len(backend.handles) == 2
    assert sum(
        event["type"] == "trial_deadline_exceeded"
        for event in resumed["events"]
    ) == 1
