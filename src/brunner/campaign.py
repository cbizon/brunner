from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from brunner.backends import (
    BackendHandle,
    ExecutionBackend,
    WorkloadSpec,
)
from brunner.contract import OutputContract
from brunner.definition import BenchmarkDefinition
from brunner.errors import (
    ArtifactTransferError,
    BackendConnectivityError,
    BackendError,
)
from brunner.evaluation import evaluate_trial
from brunner.io import write_json_atomic
from brunner.providers import ProviderSettings
from brunner.trial import TrialIdentity, create_trial


WorkloadFactory = Callable[
    [
        Path,
        "CampaignTrial",
        "CampaignPlan",
        BenchmarkDefinition,
        str,
    ],
    WorkloadSpec,
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "trial"


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CampaignTrial:
    provider: str
    model: str
    effort: str | None = None
    replicate: int = 1
    environment_keys: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.provider.strip():
            raise ValueError("campaign trial provider cannot be empty")
        if not self.model.strip():
            raise ValueError("campaign trial model cannot be empty")
        if self.replicate < 1:
            raise ValueError("campaign replicate must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "replicate": self.replicate,
            "environment_keys": list(self.environment_keys),
        }


@dataclass(frozen=True)
class CampaignPlan:
    campaign_id: str
    root: Path
    trials: tuple[CampaignTrial, ...]
    max_parallel: int = 1
    backend_image: str | None = None
    provider_executable: str | None = None
    included_artifact_groups: frozenset[str] = frozenset()

    def validate(self) -> None:
        if not self.campaign_id.strip():
            raise ValueError("campaign_id cannot be empty")
        if not self.trials:
            raise ValueError("campaign must contain at least one trial")
        if self.max_parallel < 1:
            raise ValueError("campaign max_parallel must be positive")
        for trial in self.trials:
            trial.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "root": str(self.root.resolve()),
            "trials": [trial.to_dict() for trial in self.trials],
            "max_parallel": self.max_parallel,
            "backend_image": self.backend_image,
            "provider_executable": self.provider_executable,
            "included_artifact_groups": sorted(
                self.included_artifact_groups
            ),
        }


def expand_matrix(
    *,
    providers: tuple[ProviderSettings, ...],
    replicates: int = 1,
) -> tuple[CampaignTrial, ...]:
    if replicates < 1:
        raise ValueError("replicates must be positive")
    return tuple(
        CampaignTrial(
            provider=settings.provider,
            model=settings.model,
            effort=settings.effort,
            replicate=replicate,
        )
        for settings in providers
        for replicate in range(1, replicates + 1)
    )


def trial_id(campaign_id: str, trial: CampaignTrial) -> str:
    identity = {
        "provider": trial.provider,
        "model": trial.model,
        "effort": trial.effort,
        "replicate": trial.replicate,
    }
    suffix = _canonical_digest(identity)[:8]
    effort = trial.effort or "default"
    prefix = "-".join(
        (
            _slug(campaign_id),
            _slug(trial.provider),
            _slug(trial.model),
            _slug(effort),
            f"r{trial.replicate:02d}",
        )
    )
    return f"{prefix[:80].rstrip('-')}-{suffix}"


def default_workload_factory(
    trial: Path,
    campaign_trial: CampaignTrial,
    plan: CampaignPlan,
    definition: BenchmarkDefinition,
    backend_name: str,
) -> WorkloadSpec:
    missing_environment = [
        key
        for key in campaign_trial.environment_keys
        if key not in os.environ
    ]
    if missing_environment:
        raise ValueError(
            "campaign environment variables are not set: "
            + ", ".join(missing_environment)
        )
    backend_trial = (
        trial if backend_name == "local" else Path("/brunner/trial")
    )
    python = sys.executable if backend_name == "local" else "python"
    command = [
        python,
        "-m",
        "brunner.agent_cli",
        str(backend_trial),
    ]
    if plan.provider_executable:
        command.extend(
            ("--provider-executable", plan.provider_executable)
        )
    return WorkloadSpec(
        workload_id=trial.name,
        trial=trial,
        command=tuple(command),
        timeout_seconds=definition.runtime.timeout_seconds,
        environment={
            key: os.environ[key]
            for key in campaign_trial.environment_keys
        },
        image=plan.backend_image,
        labels={"dev.brunner/campaign": _slug(plan.campaign_id)[:63]},
    )


def _handle_from_dict(value: dict[str, Any]) -> BackendHandle:
    return BackendHandle(
        backend=str(value["backend"]),
        workload_id=str(value["workload_id"]),
        native_id=str(value["native_id"]),
        trial=Path(value["trial"]),
        metadata=dict(value.get("metadata", {})),
    )


class CampaignRunner:
    def __init__(
        self,
        definition: BenchmarkDefinition,
        contract: OutputContract,
        plan: CampaignPlan,
        backend: ExecutionBackend,
        *,
        workload_factory: WorkloadFactory = default_workload_factory,
    ) -> None:
        plan.validate()
        definition.validate()
        self.definition = definition
        self.contract = contract
        self.plan = plan
        self.backend = backend
        self.workload_factory = workload_factory
        self.root = plan.root.resolve()
        self.state_path = self.root / "campaign.json"
        self.dashboard_path = self.root / "index.html"

    def initialize(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text())
            expected = {
                "benchmark_id": self.definition.benchmark_id,
                "benchmark_version": self.definition.version,
                "contract_sha256": self.contract.sha256,
                "plan_sha256": _canonical_digest(self.plan.to_dict()),
            }
            mismatches = {
                key: {"expected": value, "actual": state.get(key)}
                for key, value in expected.items()
                if state.get(key) != value
            }
            if mismatches:
                raise RuntimeError(
                    f"campaign identity changed: {mismatches}"
                )
            return state

        entries = []
        tests_root = self.root / "trials"
        tests_root.mkdir()
        for campaign_trial in self.plan.trials:
            identifier = trial_id(
                self.plan.campaign_id,
                campaign_trial,
            )
            trial = create_trial(
                self.definition,
                self.contract,
                tests_root,
                TrialIdentity(
                    test_id=identifier,
                    provider=campaign_trial.provider,
                    model=campaign_trial.model,
                    effort=campaign_trial.effort,
                ),
            )
            entries.append(
                {
                    "test_id": identifier,
                    "trial": str(trial),
                    "provider": campaign_trial.provider,
                    "model": campaign_trial.model,
                    "effort": campaign_trial.effort,
                    "replicate": campaign_trial.replicate,
                    "environment_keys": list(
                        campaign_trial.environment_keys
                    ),
                    "phase": "pending",
                    "outcome": None,
                    "attempts": {
                        "submission": 0,
                        "collection": 0,
                    },
                }
            )
        state = {
            "schema_version": "1.0",
            "campaign_id": self.plan.campaign_id,
            "benchmark_id": self.definition.benchmark_id,
            "benchmark_version": self.definition.version,
            "contract_sha256": self.contract.sha256,
            "plan_sha256": _canonical_digest(self.plan.to_dict()),
            "backend": self.backend.name,
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "trials": entries,
            "events": [],
        }
        self._save(state)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        write_json_atomic(self.state_path, state)
        from brunner.dashboard import write_campaign_dashboard

        write_campaign_dashboard(state, self.dashboard_path)

    @staticmethod
    def _event(
        state: dict[str, Any],
        event_type: str,
        message: str,
        *,
        test_id: str | None = None,
    ) -> None:
        state["events"].append(
            {
                "time": _now(),
                "type": event_type,
                "message": message,
                "test_id": test_id,
            }
        )
        state["events"] = state["events"][-200:]

    def _pause_connectivity(
        self,
        state: dict[str, Any],
        error: BackendConnectivityError,
    ) -> dict[str, Any]:
        state["status"] = "paused_backend_connectivity"
        state["pause_reason"] = str(error)
        self._event(
            state,
            "backend_connectivity",
            str(error),
        )
        self._save(state)
        return state

    def _campaign_trial(
        self,
        entry: dict[str, Any],
    ) -> CampaignTrial:
        return CampaignTrial(
            provider=str(entry["provider"]),
            model=str(entry["model"]),
            effort=entry.get("effort"),
            replicate=int(entry["replicate"]),
            environment_keys=tuple(
                entry.get("environment_keys", ())
            ),
        )

    def _collect_and_evaluate(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
        handle: BackendHandle,
        backend_phase: str,
    ) -> None:
        entry["phase"] = "collecting"
        entry["attempts"]["collection"] += 1
        self._save(state)
        destination = self.root / "collected" / entry["test_id"]
        try:
            collection = self.backend.collect(
                handle,
                destination,
                self.definition.artifacts,
                included_groups=self.plan.included_artifact_groups,
            )
        except BackendConnectivityError:
            raise
        except (ArtifactTransferError, BackendError, OSError) as error:
            entry["phase"] = "collection_failed"
            entry["collection_error"] = str(error)
            self._event(
                state,
                "collection_failed",
                str(error),
                test_id=entry["test_id"],
            )
            self._save(state)
            return
        entry["collection"] = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in collection.items()
        }
        entry["collected_trial"] = str(destination)
        entry.pop("collection_error", None)
        entry["phase"] = "evaluating"
        self._save(state)
        evaluation = evaluate_trial(
            self.definition,
            self.contract,
            destination,
        )
        entry["evaluation"] = {
            "status": evaluation["status"],
            "results": str(
                destination / self.definition.evaluation.results_path
            ),
            "report": str(
                (
                    destination
                    / self.definition.evaluation.results_path
                ).with_name("run-report.html")
            ),
        }
        entry["backend_phase"] = backend_phase
        entry["outcome"] = (
            "succeeded"
            if backend_phase == "succeeded"
            and evaluation["status"] == "complete"
            else "failed"
        )
        entry["phase"] = "cleanup_pending"
        self._save(state)
        try:
            self.backend.cleanup(handle)
        except BackendConnectivityError:
            raise
        except BackendError as error:
            entry["phase"] = "attention_required"
            entry["cleanup_error"] = str(error)
            self._event(
                state,
                "cleanup_failed",
                str(error),
                test_id=entry["test_id"],
            )
            self._save(state)
            return
        entry["phase"] = "complete"
        entry["completed_at"] = _now()
        entry.pop("cleanup_error", None)
        self._event(
            state,
            "trial_complete",
            f"trial finished with outcome {entry['outcome']}",
            test_id=entry["test_id"],
        )
        self._save(state)

    def advance(self) -> dict[str, Any]:
        state = self.initialize()
        state["status"] = "running"
        state.pop("pause_reason", None)

        for entry in state["trials"]:
            if entry["phase"] == "pending" and not entry.get("handle"):
                continue
            if entry["phase"] not in {
                "submitted",
                "pending",
                "running",
                "collection_failed",
                "cleanup_pending",
            }:
                continue
            handle_value = entry.get("handle")
            if not isinstance(handle_value, dict):
                entry["phase"] = "attention_required"
                entry["error"] = "trial has no backend handle"
                continue
            handle = _handle_from_dict(handle_value)
            if entry["phase"] == "cleanup_pending":
                try:
                    self.backend.cleanup(handle)
                except BackendConnectivityError as error:
                    return self._pause_connectivity(state, error)
                except BackendError as error:
                    entry["phase"] = "attention_required"
                    entry["cleanup_error"] = str(error)
                    continue
                entry["phase"] = "complete"
                entry["completed_at"] = _now()
                entry.pop("cleanup_error", None)
                self._event(
                    state,
                    "cleanup_complete",
                    "backend cleanup completed",
                    test_id=entry["test_id"],
                )
                continue
            if entry["phase"] == "collection_failed":
                backend_phase = str(
                    entry.get("backend_phase", "failed")
                )
                try:
                    self._collect_and_evaluate(
                        state,
                        entry,
                        handle,
                        backend_phase,
                    )
                except BackendConnectivityError as error:
                    return self._pause_connectivity(state, error)
                continue
            try:
                snapshot = self.backend.inspect(handle)
            except BackendConnectivityError as error:
                return self._pause_connectivity(state, error)
            except BackendError as error:
                entry["phase"] = "attention_required"
                entry["error"] = str(error)
                self._event(
                    state,
                    "inspection_failed",
                    str(error),
                    test_id=entry["test_id"],
                )
                continue
            entry["backend_snapshot"] = snapshot.to_dict()
            if snapshot.phase in {"pending", "running"}:
                entry["phase"] = snapshot.phase
                continue
            if snapshot.phase in {"succeeded", "failed"}:
                log_path = Path(entry["trial"]) / "backend/workload.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    log_path.write_text(self.backend.logs(handle))
                except BackendError as error:
                    entry["log_warning"] = str(error)
                entry["backend_phase"] = snapshot.phase
                try:
                    self._collect_and_evaluate(
                        state,
                        entry,
                        handle,
                        snapshot.phase,
                    )
                except BackendConnectivityError as error:
                    return self._pause_connectivity(state, error)
            else:
                entry["phase"] = "attention_required"
                entry["error"] = (
                    f"backend returned {snapshot.phase}: "
                    f"{snapshot.reason or snapshot.message or ''}"
                )

        active = sum(
            bool(entry.get("handle"))
            and entry["phase"] in {
                "submitted",
                "pending",
                "running",
                "collecting",
                "evaluating",
                "cleanup_pending",
            }
            for entry in state["trials"]
        )
        available_by_plan = max(0, self.plan.max_parallel - active)
        if available_by_plan:
            try:
                capacity = self.backend.capacity()
            except BackendConnectivityError as error:
                return self._pause_connectivity(state, error)
            except BackendError as error:
                state["status"] = "attention_required"
                self._event(
                    state,
                    "capacity_failed",
                    str(error),
                )
                self._save(state)
                return state
            available = available_by_plan
            if capacity.available is not None:
                available = min(available, capacity.available)
            for entry in state["trials"]:
                if available <= 0:
                    break
                if entry["phase"] != "pending" or entry.get("handle"):
                    continue
                trial = Path(entry["trial"])
                campaign_trial = self._campaign_trial(entry)
                workload = self.workload_factory(
                    trial,
                    campaign_trial,
                    self.plan,
                    self.definition,
                    self.backend.name,
                )
                entry["attempts"]["submission"] += 1
                try:
                    handle = self.backend.submit(workload)
                except BackendConnectivityError as error:
                    return self._pause_connectivity(state, error)
                except BackendError as error:
                    entry["phase"] = "attention_required"
                    entry["error"] = str(error)
                    self._event(
                        state,
                        "submission_failed",
                        str(error),
                        test_id=entry["test_id"],
                    )
                    continue
                entry["handle"] = handle.to_dict()
                entry["phase"] = "submitted"
                entry["submitted_at"] = _now()
                available -= 1
                self._event(
                    state,
                    "trial_submitted",
                    f"submitted to {self.backend.name}",
                    test_id=entry["test_id"],
                )

        phases = {entry["phase"] for entry in state["trials"]}
        if phases == {"complete"}:
            state["status"] = "complete"
            state["completed_at"] = _now()
        elif phases & {"attention_required", "collection_failed"}:
            state["status"] = "attention_required"
        else:
            state["status"] = "running"
        self._save(state)
        return state

    def run(
        self,
        *,
        poll_seconds: float = 5,
    ) -> dict[str, Any]:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while True:
            state = self.advance()
            if state["status"] != "running":
                return state
            time.sleep(poll_seconds)
