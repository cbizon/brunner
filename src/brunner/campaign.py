from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from brunner.backends import (
    BackendHandle,
    CONTAINER_ISOLATION,
    ExecutionBackend,
    WorkloadSpec,
)
from brunner.contract import OutputContract
from brunner.definition import BenchmarkDefinition
from brunner.errors import (
    ArtifactTransferError,
    BackendConnectivityError,
    BackendError,
    BrunnerError,
    IntegrityError,
)
from brunner.evaluation import evaluate_trial
from brunner.io import write_json_atomic
from brunner.pipeline import summarize_pipeline_state
from brunner.trial import TrialIdentity, create_trial, load_trial_identity


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


def _load_optional_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class CampaignTrial:
    test_id: str
    provider: str
    model: str
    effort: str | None = None

    def validate(self) -> None:
        if not self.test_id.strip():
            raise ValueError("campaign trial test_id cannot be empty")
        test_path = Path(self.test_id)
        if (
            test_path.is_absolute()
            or test_path.name != self.test_id
            or self.test_id in {".", ".."}
        ):
            raise ValueError(
                "campaign trial test_id must be one safe path segment"
            )
        if not self.provider.strip():
            raise ValueError("campaign trial provider cannot be empty")
        if not self.model.strip():
            raise ValueError("campaign trial model cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
        }


@dataclass(frozen=True)
class CampaignPlan:
    campaign_id: str
    root: Path
    trials: tuple[CampaignTrial, ...]
    max_parallel: int = 1
    backend_image: str | None = None
    provider_executable: str | None = None
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    included_artifact_groups: frozenset[str] = frozenset()
    collection_retry_seconds: float = 60.0
    collection_max_attempts: int = 3
    infrastructure_max_restarts: int = 2
    trial_timeout_seconds: float | None = None
    trial_timeout_margin_seconds: float = 5 * 60
    max_pause_seconds: float | None = None
    evaluation_timeout_seconds: float | None = None

    def validate(self) -> None:
        if not self.campaign_id.strip():
            raise ValueError("campaign_id cannot be empty")
        if not self.trials:
            raise ValueError("campaign must contain at least one trial")
        if self.max_parallel < 1:
            raise ValueError("campaign max_parallel must be positive")
        for name, value in (
            ("cpu_request", self.cpu_request),
            ("cpu_limit", self.cpu_limit),
            ("memory_request", self.memory_request),
            ("memory_limit", self.memory_limit),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"campaign {name} cannot be empty")
        if self.collection_retry_seconds < 0:
            raise ValueError(
                "campaign collection_retry_seconds must not be negative"
            )
        if self.collection_max_attempts < 1:
            raise ValueError(
                "campaign collection_max_attempts must be positive"
            )
        if self.infrastructure_max_restarts < 0:
            raise ValueError(
                "campaign infrastructure_max_restarts cannot be negative"
            )
        if (
            self.trial_timeout_seconds is not None
            and self.trial_timeout_seconds <= 0
        ):
            raise ValueError(
                "campaign trial_timeout_seconds must be positive or None"
            )
        if self.trial_timeout_margin_seconds < 0:
            raise ValueError(
                "campaign trial_timeout_margin_seconds cannot be negative"
            )
        if self.max_pause_seconds is not None and self.max_pause_seconds <= 0:
            raise ValueError(
                "campaign max_pause_seconds must be positive or None"
            )
        if (
            self.evaluation_timeout_seconds is not None
            and self.evaluation_timeout_seconds <= 0
        ):
            raise ValueError(
                "campaign evaluation_timeout_seconds must be positive or None"
            )
        identities: dict[str, dict[str, Any]] = {}
        for trial in self.trials:
            trial.validate()
            current = trial.to_dict()
            previous = identities.setdefault(trial.test_id, current)
            if previous != current:
                raise ValueError(
                    f"campaign trial test_id is reused with conflicting "
                    f"configuration: {trial.test_id}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "root": str(self.root.resolve()),
            "trials": [trial.to_dict() for trial in self.trials],
            "max_parallel": self.max_parallel,
            "backend_image": self.backend_image,
            "provider_executable": self.provider_executable,
            "cpu_request": self.cpu_request,
            "cpu_limit": self.cpu_limit,
            "memory_request": self.memory_request,
            "memory_limit": self.memory_limit,
            "included_artifact_groups": sorted(
                self.included_artifact_groups
            ),
            "collection_retry_seconds": self.collection_retry_seconds,
            "collection_max_attempts": self.collection_max_attempts,
            "infrastructure_max_restarts": (
                self.infrastructure_max_restarts
            ),
            "trial_timeout_seconds": self.trial_timeout_seconds,
            "trial_timeout_margin_seconds": (
                self.trial_timeout_margin_seconds
            ),
            "max_pause_seconds": self.max_pause_seconds,
            "evaluation_timeout_seconds": self.evaluation_timeout_seconds,
        }


def default_workload_factory(
    trial: Path,
    campaign_trial: CampaignTrial,
    plan: CampaignPlan,
    definition: BenchmarkDefinition,
    backend_name: str,
) -> WorkloadSpec:
    command = [
        "python",
        "-m",
        "brunner.agent_cli",
        "/brunner/trial",
    ]
    if plan.provider_executable:
        command.extend(
            ("--provider-executable", plan.provider_executable)
        )
    return WorkloadSpec(
        workload_id=trial.name,
        trial=trial,
        command=tuple(command),
        timeout_seconds=(
            definition.runtime.timeout_seconds
            + definition.runtime.backend_shutdown_grace_seconds
        ),
        image=plan.backend_image,
        cpu_request=plan.cpu_request,
        cpu_limit=plan.cpu_limit,
        memory_request=plan.memory_request,
        memory_limit=plan.memory_limit,
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
        if getattr(backend, "agent_isolation", None) != CONTAINER_ISOLATION:
            raise ValueError(
                "campaign backends must run agents in a container isolation "
                "boundary; host-process execution is not supported"
            )
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
        self.lock_path = self.root / "campaign.lock"
        self._lock_depth = 0
        self._lock_stream: TextIO | None = None

    @contextmanager
    def _campaign_lock(self) -> Iterator[None]:
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return

        self.root.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            stream.seek(0)
            owner = stream.read().strip() or "owner information unavailable"
            stream.close()
            raise RuntimeError(
                f"another orchestrator is using campaign "
                f"{self.plan.campaign_id}: {owner}"
            ) from error

        owner = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "campaign_id": self.plan.campaign_id,
            "state_path": str(self.state_path),
            "acquired_at": _now(),
        }
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps(owner, sort_keys=True) + "\n")
        stream.flush()
        self._lock_stream = stream
        self._lock_depth = 1
        try:
            yield
        finally:
            self._lock_depth = 0
            self._lock_stream = None
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def initialize(self) -> dict[str, Any]:
        with self._campaign_lock():
            return self._initialize()

    def _initialize(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text())
            expected = {
                "campaign_id": self.plan.campaign_id,
                "benchmark_id": self.definition.benchmark_id,
                "benchmark_version": self.definition.version,
                "contract_sha256": self.contract.sha256,
                "backend": self.backend.name,
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
            state["schema_version"] = "2.0"
            state.pop("plan_sha256", None)
            state.setdefault("trials", [])
            state.setdefault("events", [])
            recovered = self._recover_in_progress_phases(state)
            added = self._sync_plan_trials(state)
            if added or recovered:
                state["status"] = "running"
            self._save(state)
            return state

        state = {
            "schema_version": "2.0",
            "campaign_id": self.plan.campaign_id,
            "benchmark_id": self.definition.benchmark_id,
            "benchmark_version": self.definition.version,
            "contract_sha256": self.contract.sha256,
            "backend": self.backend.name,
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "trials": [],
            "events": [],
        }
        self._sync_plan_trials(state)
        self._save(state)
        return state

    def _recover_in_progress_phases(
        self,
        state: dict[str, Any],
    ) -> int:
        recovered = 0
        for entry in state.get("trials", []):
            phase = entry.get("phase")
            if phase == "collecting":
                entry["phase"] = "collection_pending"
                entry["collection_warning"] = (
                    "collection was interrupted before completion"
                )
                in_progress = entry.pop("collection_attempt", None)
                if in_progress is None:
                    # Before collection_attempt existed, entering collecting
                    # charged the attempt before collect() was called. Undo
                    # that legacy in-progress charge during recovery.
                    attempts = entry.setdefault("attempts", {})
                    attempts["collection"] = max(
                        0,
                        int(attempts.get("collection", 0)) - 1,
                    )
            elif phase == "evaluating":
                entry["phase"] = "evaluation_pending"
                entry["evaluation_error"] = (
                    "evaluation was interrupted before completion"
                )
            else:
                continue
            self._event(
                state,
                "phase_recovered",
                f"recovered interrupted {phase} phase",
                test_id=entry.get("test_id"),
            )
            recovered += 1
        return recovered

    def _sync_plan_trials(self, state: dict[str, Any]) -> int:
        entries = {
            str(entry["test_id"]): entry
            for entry in state.get("trials", [])
            if isinstance(entry, dict) and "test_id" in entry
        }
        tests_root = self.root / "trials"
        tests_root.mkdir(parents=True, exist_ok=True)
        added = 0
        seen = set()
        for campaign_trial in self.plan.trials:
            if campaign_trial.test_id in seen:
                continue
            seen.add(campaign_trial.test_id)
            existing = entries.get(campaign_trial.test_id)
            expected = {
                "provider": campaign_trial.provider,
                "model": campaign_trial.model,
                "effort": campaign_trial.effort,
            }
            if existing is not None:
                attempts = existing.setdefault("attempts", {})
                attempts.setdefault("submission", 0)
                attempts.setdefault("collection", 0)
                attempts.setdefault("infrastructure", 0)
                actual = {
                    "provider": existing.get("provider"),
                    "model": existing.get("model"),
                    "effort": existing.get("effort"),
                }
                mismatches = {
                    key: {
                        "expected": value,
                        "actual": actual[key],
                    }
                    for key, value in expected.items()
                    if actual[key] != value
                }
                if mismatches:
                    raise RuntimeError(
                        "campaign trial identity changed for "
                        f"{campaign_trial.test_id}: {mismatches}"
                    )
                continue
            trial_path = tests_root / campaign_trial.test_id
            if trial_path.exists():
                identity = load_trial_identity(trial_path)
                metadata = _load_optional_object(
                    trial_path / "metadata/manifest.json"
                )
                if metadata is None:
                    raise RuntimeError(
                        "existing trial directory has no valid metadata: "
                        f"{trial_path}"
                    )
                recovered = {
                    "test_id": identity.test_id,
                    "provider": identity.provider,
                    "model": identity.model,
                    "effort": identity.effort,
                    "benchmark_id": metadata.get("benchmark_id"),
                    "benchmark_version": metadata.get(
                        "benchmark_version"
                    ),
                    "contract_sha256": metadata.get("contract_sha256"),
                }
                recovery_expected = {
                    "test_id": campaign_trial.test_id,
                    "provider": campaign_trial.provider,
                    "model": campaign_trial.model,
                    "effort": campaign_trial.effort,
                    "benchmark_id": self.definition.benchmark_id,
                    "benchmark_version": self.definition.version,
                    "contract_sha256": self.contract.sha256,
                }
                recovery_mismatches = {
                    key: {
                        "expected": value,
                        "actual": recovered.get(key),
                    }
                    for key, value in recovery_expected.items()
                    if recovered.get(key) != value
                }
                if recovery_mismatches:
                    raise RuntimeError(
                        "existing trial directory conflicts with campaign "
                        f"trial {campaign_trial.test_id}: "
                        f"{recovery_mismatches}"
                    )
                trial = trial_path
            else:
                trial = create_trial(
                    self.definition,
                    self.contract,
                    tests_root,
                    TrialIdentity(
                        test_id=campaign_trial.test_id,
                        provider=campaign_trial.provider,
                        model=campaign_trial.model,
                        effort=campaign_trial.effort,
                    ),
                )
            entry = {
                "test_id": campaign_trial.test_id,
                "trial": str(trial),
                **expected,
                "phase": "pending",
                "outcome": None,
                "attempts": {
                    "submission": 0,
                    "collection": 0,
                    "infrastructure": 0,
                },
            }
            state["trials"].append(entry)
            entries[campaign_trial.test_id] = entry
            self._event(
                state,
                "trial_added",
                "campaign trial added",
                test_id=campaign_trial.test_id,
            )
            added += 1
        return added

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
        state["has_attention"] = True
        state["pause_reason"] = str(error)
        paused_since = state.get("paused_since")
        if not paused_since:
            paused_since = _now()
            state["paused_since"] = paused_since
            self._event(
                state,
                "backend_connectivity",
                str(error),
            )
        if self.plan.max_pause_seconds is not None:
            try:
                since = datetime.fromisoformat(str(paused_since))
            except ValueError:
                since = datetime.now(UTC)
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            paused_seconds = (datetime.now(UTC) - since).total_seconds()
            if paused_seconds > self.plan.max_pause_seconds:
                # A campaign that waits forever looks identical to one that is
                # working; surface it instead.
                state["status"] = "attention_required"
                state["has_attention"] = True
                state["pause_reason"] = (
                    f"backend unreachable for {paused_seconds:.0f}s: {error}"
                )
                self._event(
                    state,
                    "backend_connectivity_timeout",
                    state["pause_reason"],
                )
        self._save(state)
        return state

    def _trial_timeout_seconds(self) -> float:
        if self.plan.trial_timeout_seconds is not None:
            return self.plan.trial_timeout_seconds
        # Default to the backend's own workload deadline plus a margin: past
        # that point the backend itself is stuck, so nothing else will stop it.
        runtime = self.definition.runtime
        return (
            runtime.timeout_seconds
            + runtime.backend_shutdown_grace_seconds
            + self.plan.trial_timeout_margin_seconds
        )

    def _overdue_seconds(self, entry: dict[str, Any]) -> float | None:
        submitted_at = entry.get("submitted_at")
        if not submitted_at:
            return None
        try:
            submitted = datetime.fromisoformat(str(submitted_at))
        except ValueError:
            return None
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - submitted).total_seconds()
        limit = self._trial_timeout_seconds()
        return elapsed if elapsed > limit else None

    def _campaign_trial(
        self,
        entry: dict[str, Any],
    ) -> CampaignTrial:
        return CampaignTrial(
            test_id=str(entry["test_id"]),
            provider=str(entry["provider"]),
            model=str(entry["model"]),
            effort=entry.get("effort"),
        )

    def _submit_entry(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
    ) -> BackendHandle | None:
        trial = Path(entry["trial"])
        workload = self.workload_factory(
            trial,
            self._campaign_trial(entry),
            self.plan,
            self.definition,
            self.backend.name,
        )
        entry["phase"] = "submitting"
        entry["attempts"]["submission"] += 1
        entry.pop("error", None)
        self._save(state)
        try:
            handle = self.backend.submit(workload)
        except BackendConnectivityError:
            raise
        except BackendError as error:
            entry["phase"] = "attention_required"
            entry["error"] = str(error)
            self._event(
                state,
                "submission_failed",
                str(error),
                test_id=entry["test_id"],
            )
            self._save(state)
            return None
        entry["handle"] = handle.to_dict()
        entry["phase"] = "submitted"
        submitted_at = handle.metadata.get("submitted_at")
        entry["submitted_at"] = (
            submitted_at if isinstance(submitted_at, str) else _now()
        )
        self._event(
            state,
            "trial_submitted",
            f"submitted to or adopted from {self.backend.name}",
            test_id=entry["test_id"],
        )
        # Persist the handle immediately. A later crash must not lose the
        # identity of a workload that may already be running remotely.
        self._save(state)
        return handle

    def _restart_infrastructure_entry(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
        handle: BackendHandle,
    ) -> BackendHandle | None:
        restart = getattr(self.backend, "restart", None)
        if not callable(restart):
            return None
        workload = self.workload_factory(
            Path(entry["trial"]),
            self._campaign_trial(entry),
            self.plan,
            self.definition,
            self.backend.name,
        )
        resuming = entry["phase"] == "infrastructure_retrying"
        retry_history = entry.setdefault("infrastructure_retries", [])
        if resuming:
            generation = int(entry["attempts"]["infrastructure"])
            retry_record = next(
                (
                    item
                    for item in retry_history
                    if isinstance(item, dict)
                    and item.get("generation") == generation
                ),
                None,
            )
            if retry_record is None:
                current = entry.get("infrastructure_retry")
                retry_record = (
                    current
                    if isinstance(current, dict)
                    else {"generation": generation}
                )
                retry_history.append(retry_record)
            entry["infrastructure_retry"] = retry_record
        else:
            entry["attempts"]["infrastructure"] += 1
            generation = int(entry["attempts"]["infrastructure"])
            entry["phase"] = "infrastructure_retrying"
            retry_record = {
                "generation": generation,
                "started_at": _now(),
                "previous_handle": handle.to_dict(),
                "previous_snapshot": entry.get("backend_snapshot"),
            }
            retry_history.append(retry_record)
            entry["infrastructure_retry"] = retry_record
            self._event(
                state,
                "infrastructure_retry_started",
                f"restarting failed backend workload as generation {generation}",
                test_id=entry["test_id"],
            )
            self._save(state)
        try:
            restarted = restart(handle, workload, generation)
        except BackendConnectivityError:
            raise
        except BackendError as error:
            entry["phase"] = "attention_required"
            entry["error"] = str(error)
            retry_record["error"] = str(error)
            self._event(
                state,
                "infrastructure_retry_failed",
                str(error),
                test_id=entry["test_id"],
            )
            self._save(state)
            return None
        entry.setdefault("first_submitted_at", entry.get("submitted_at"))
        entry["handle"] = restarted.to_dict()
        entry["phase"] = "submitted"
        entry["submitted_at"] = (
            restarted.metadata.get("submitted_at")
            if isinstance(restarted.metadata.get("submitted_at"), str)
            else _now()
        )
        entry["backend_workload_live"] = True
        retry_record["completed_at"] = _now()
        retry_record["handle"] = restarted.to_dict()
        entry.pop("pipeline", None)
        entry.pop("benchmark", None)
        entry.pop("evaluation", None)
        entry["outcome"] = None
        entry.pop("failure_class", None)
        entry.pop("error", None)
        self._event(
            state,
            "infrastructure_retry_submitted",
            f"submitted backend generation {generation}",
            test_id=entry["test_id"],
        )
        self._save(state)
        return restarted

    def _begin_collection_attempt(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
    ) -> int:
        attempt_number = int(entry["attempts"]["collection"]) + 1
        entry["phase"] = "collecting"
        entry["collection_attempt"] = {
            "number": attempt_number,
            "started_at": _now(),
        }
        entry.pop("next_collection_attempt_at", None)
        self._save(state)
        return attempt_number

    @staticmethod
    def _finish_collection_attempt(
        entry: dict[str, Any],
        attempt_number: int,
    ) -> None:
        entry["attempts"]["collection"] = attempt_number
        entry.pop("collection_attempt", None)

    def _cleanup_entry(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
        handle: BackendHandle,
    ) -> None:
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
        entry["backend_workload_live"] = False
        entry.pop("cleanup_error", None)
        self._event(
            state,
            "trial_complete",
            f"trial finished with outcome {entry['outcome']}",
            test_id=entry["test_id"],
        )
        self._save(state)

    def _collect_and_evaluate(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
        handle: BackendHandle,
        backend_phase: str,
    ) -> None:
        destination = self.root / "collected" / entry["test_id"]
        entry["backend_phase"] = backend_phase
        if entry["phase"] != "evaluation_pending":
            attempt_number = self._begin_collection_attempt(state, entry)
            try:
                collection = self.backend.collect(
                    handle,
                    destination,
                    self.definition.artifacts,
                    included_groups=self.plan.included_artifact_groups,
                )
            except BackendConnectivityError:
                raise
            except ArtifactTransferError as error:
                self._finish_collection_attempt(entry, attempt_number)
                attempts = int(entry["attempts"]["collection"])
                if attempts < self.plan.collection_max_attempts:
                    next_attempt = datetime.now(UTC) + timedelta(
                        seconds=self.plan.collection_retry_seconds
                    )
                    entry["phase"] = "collection_retry_wait"
                    entry["collection_error"] = str(error)
                    entry["next_collection_attempt_at"] = (
                        next_attempt.isoformat()
                    )
                    self._event(
                        state,
                        "collection_retry_scheduled",
                        (
                            f"artifact transfer attempt {attempts} failed; "
                            f"retrying at {next_attempt.isoformat()}"
                        ),
                        test_id=entry["test_id"],
                    )
                    self._save(state)
                    return
                entry["phase"] = "collection_failed"
                entry["collection_error"] = str(error)
                self._event(
                    state,
                    "collection_failed",
                    (
                        f"artifact transfer failed after {attempts} "
                        f"attempts: {error}"
                    ),
                    test_id=entry["test_id"],
                )
                self._save(state)
                return
            except (
                BackendError,
                IntegrityError,
                OSError,
            ) as error:
                self._finish_collection_attempt(entry, attempt_number)
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
            self._finish_collection_attempt(entry, attempt_number)
            entry["collection"] = {
                key: str(value) if isinstance(value, Path) else value
                for key, value in collection.items()
            }
            entry["collected_trial"] = str(destination)
            entry.pop("collection_error", None)
            entry.pop("collection_warning", None)
        elif not destination.is_dir():
            entry["phase"] = "collection_failed"
            entry["collection_error"] = (
                "collected trial is missing after interrupted evaluation"
            )
            self._save(state)
            return
        runner_state = _load_optional_object(destination / "status.json")
        pipeline = summarize_pipeline_state(runner_state)
        entry["pipeline"] = pipeline
        usage = _load_optional_object(destination / "usage/usage.json")
        timing = _load_optional_object(
            destination / "timing/accounting.json"
        )
        if usage is not None:
            entry["usage"] = {
                key: usage.get(key)
                for key in (
                    "logical_input_tokens",
                    "uncached_input_tokens",
                    "cache_read_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                )
            }
        if timing is not None and isinstance(timing.get("summary"), dict):
            entry["timing"] = dict(timing["summary"])
        if pipeline["provider_result_present"] is not True:
            reason = str(
                pipeline.get("infrastructure_reason")
                or "AgentPipelineIncomplete"
            )
            entry["evaluation"] = {
                "status": "not_run",
                "assessment_status": "not_run",
                "required_assessments_complete": False,
                "assessments": [],
                "reason": reason,
            }
            entry["benchmark"] = {
                "status": "not_run",
                "succeeded": None,
                "reason": reason,
            }
            entry["outcome"] = "failed"
            entry["failure_class"] = "infrastructure"
            self._event(
                state,
                "evaluation_skipped",
                (
                    "trusted evaluation skipped because the agent pipeline "
                    f"did not produce a terminal provider result: {reason}"
                ),
                test_id=entry["test_id"],
            )
            self._cleanup_entry(state, entry, handle)
            return
        entry["phase"] = "evaluating"
        self._save(state)
        try:
            evaluation = evaluate_trial(
                self.definition,
                self.contract,
                destination,
                timeout_seconds=self.plan.evaluation_timeout_seconds,
            )
        except (
            BrunnerError,
            json.JSONDecodeError,
            OSError,
            ValueError,
        ) as error:
            entry["phase"] = "attention_required"
            entry["evaluation_error"] = str(error)
            self._event(
                state,
                "evaluation_failed",
                str(error),
                test_id=entry["test_id"],
            )
            self._save(state)
            return
        entry["evaluation"] = {
            "status": evaluation["status"],
            "assessment_status": evaluation["assessment_status"],
            "required_assessments_complete": evaluation[
                "required_assessments_complete"
            ],
            "assessments": evaluation["assessments"],
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
        entry.pop("evaluation_error", None)
        benchmark_succeeded = (
            backend_phase == "succeeded"
            and evaluation["status"] == "complete"
            and evaluation["required_assessments_complete"]
        )
        entry["benchmark"] = {
            "status": evaluation["status"],
            "succeeded": benchmark_succeeded,
            "assessment_status": evaluation["assessment_status"],
        }
        entry["outcome"] = (
            "succeeded"
            if benchmark_succeeded
            else "failed"
        )
        if benchmark_succeeded:
            entry.pop("failure_class", None)
        else:
            entry["failure_class"] = "benchmark"
        self._cleanup_entry(state, entry, handle)

    def advance(self) -> dict[str, Any]:
        with self._campaign_lock():
            return self._advance()

    def _advance(self) -> dict[str, Any]:
        state = self._initialize()
        state["status"] = "running"
        state.pop("pause_reason", None)

        for entry in state["trials"]:
            if entry["phase"] != "submitting" or entry.get("handle"):
                continue
            try:
                self._submit_entry(state, entry)
            except BackendConnectivityError as error:
                return self._pause_connectivity(state, error)

        for entry in state["trials"]:
            if entry["phase"] != "infrastructure_retrying":
                continue
            handle_value = entry.get("handle")
            if not isinstance(handle_value, dict):
                entry["phase"] = "attention_required"
                entry["error"] = (
                    "infrastructure retry has no previous backend handle"
                )
                continue
            try:
                self._restart_infrastructure_entry(
                    state,
                    entry,
                    _handle_from_dict(handle_value),
                )
            except BackendConnectivityError as error:
                return self._pause_connectivity(state, error)

        for entry in state["trials"]:
            if (
                entry["phase"] in {"pending", "submitting"}
                and not entry.get("handle")
            ):
                continue
            if entry["phase"] not in {
                "submitted",
                "pending",
                "running",
                "collection_pending",
                "collection_retry_wait",
                "evaluation_pending",
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
                    self._cleanup_entry(state, entry, handle)
                except BackendConnectivityError as error:
                    return self._pause_connectivity(state, error)
                continue
            if entry["phase"] == "collection_pending":
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
            if entry["phase"] == "collection_retry_wait":
                retry_at = entry.get("next_collection_attempt_at")
                if retry_at:
                    try:
                        retry_time = datetime.fromisoformat(str(retry_at))
                    except ValueError:
                        retry_time = datetime.now(UTC)
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=UTC)
                    if retry_time > datetime.now(UTC):
                        continue
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
            if entry["phase"] == "evaluation_pending":
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
            snapshot_pipeline = snapshot.details.get("brunner_pipeline")
            if isinstance(snapshot_pipeline, dict):
                entry["pipeline"] = snapshot_pipeline
            entry["backend_workload_live"] = snapshot.phase in {
                "pending",
                "running",
            }
            if snapshot.phase in {"pending", "running"}:
                entry["phase"] = snapshot.phase
                overdue = self._overdue_seconds(entry)
                if overdue is not None:
                    message = (
                        f"backend still reports {snapshot.phase} "
                        f"{overdue:.0f}s after submission, past the "
                        f"{self._trial_timeout_seconds():.0f}s limit"
                    )
                    attention = entry.get("attention")
                    first_report = not (
                        isinstance(attention, dict)
                        and attention.get("kind") == "trial_overdue"
                        and attention.get("active") is True
                    )
                    since = (
                        attention.get("since")
                        if isinstance(attention, dict)
                        else None
                    )
                    entry["attention"] = {
                        "kind": "trial_overdue",
                        "active": True,
                        "since": since or _now(),
                        "message": message,
                        "backend_phase": snapshot.phase,
                        "overdue_seconds": overdue,
                    }
                    entry["error"] = message
                    if first_report:
                        self._event(
                            state,
                            "trial_overdue",
                            message,
                            test_id=entry["test_id"],
                        )
                continue
            if snapshot.phase in {"succeeded", "failed"}:
                if (
                    snapshot.phase == "failed"
                    and snapshot.details.get("retryable_infrastructure") is True
                    and int(entry["attempts"].get("infrastructure", 0))
                    < self.plan.infrastructure_max_restarts
                ):
                    try:
                        restarted = self._restart_infrastructure_entry(
                            state,
                            entry,
                            handle,
                        )
                    except BackendConnectivityError as error:
                        return self._pause_connectivity(state, error)
                    if restarted is not None:
                        continue
                attention = entry.get("attention")
                if (
                    isinstance(attention, dict)
                    and attention.get("kind") == "trial_overdue"
                    and attention.get("active") is True
                ):
                    attention["active"] = False
                    attention["resolved_at"] = _now()
                    entry.pop("error", None)
                    self._event(
                        state,
                        "trial_overdue_resolved",
                        f"backend now reports {snapshot.phase}",
                        test_id=entry["test_id"],
                    )
                log_path = Path(entry["trial"]) / "backend/workload.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    workload_logs = self.backend.logs(handle)
                except BackendConnectivityError as error:
                    return self._pause_connectivity(state, error)
                except BackendError as error:
                    entry["log_warning"] = str(error)
                else:
                    if workload_logs or not log_path.exists():
                        log_path.write_text(workload_logs)
                    entry.pop("log_warning", None)
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

        # A trial needing attention normally frees its slot, but not while the
        # backend still reports its workload as pending or running: an overdue
        # trial is still consuming a real slot, and releasing it would let the
        # campaign exceed max_parallel.
        active = sum(
            bool(entry.get("handle"))
            and (
                entry["phase"] in {
                    "submitted",
                    "infrastructure_retrying",
                    "pending",
                    "running",
                    "collection_pending",
                    "collection_retry_wait",
                    "collecting",
                    "evaluating",
                    "cleanup_pending",
                }
                or bool(entry.get("backend_workload_live"))
            )
            for entry in state["trials"]
        )
        available_by_plan = max(0, self.plan.max_parallel - active)
        if available_by_plan:
            try:
                capacity = self.backend.capacity()
            except BackendConnectivityError as error:
                return self._pause_connectivity(state, error)
            except BackendError as error:
                state["scheduler_error"] = str(error)
                self._event(
                    state,
                    "capacity_failed",
                    str(error),
                )
                available = 0
            else:
                state.pop("scheduler_error", None)
                available = available_by_plan
                if capacity.available is not None:
                    available = min(available, capacity.available)
            for entry in state["trials"]:
                if available <= 0:
                    break
                if entry["phase"] != "pending" or entry.get("handle"):
                    continue
                try:
                    handle = self._submit_entry(state, entry)
                except BackendConnectivityError as error:
                    return self._pause_connectivity(state, error)
                if handle is None:
                    continue
                available -= 1

        phases = {entry["phase"] for entry in state["trials"]}
        active_attention = any(
            isinstance(entry.get("attention"), dict)
            and entry["attention"].get("active") is True
            for entry in state["trials"]
        )
        if phases == {"complete"}:
            state["status"] = "complete"
            state["completed_at"] = _now()
            state["has_attention"] = False
        elif phases & {
            "pending",
            "submitting",
            "submitted",
            "infrastructure_retrying",
            "running",
            "collection_pending",
            "collection_retry_wait",
            "collecting",
            "evaluation_pending",
            "evaluating",
            "cleanup_pending",
        }:
            state["status"] = "running"
            state["has_attention"] = bool(
                phases & {"attention_required", "collection_failed"}
                or state.get("scheduler_error")
                or active_attention
            )
        elif phases & {"attention_required", "collection_failed"}:
            state["status"] = "attention_required"
            state["has_attention"] = True
        else:
            state["status"] = "running"
            state["has_attention"] = False
        # Reaching here means no step paused, so the pause clock resets. It
        # must not reset on every attempt, or the pause timeout never fires.
        state.pop("paused_since", None)
        self._save(state)
        return state

    def run(
        self,
        *,
        poll_seconds: float = 5,
    ) -> dict[str, Any]:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        with self._campaign_lock():
            while True:
                state = self._advance()
                if state["status"] not in {
                    "running",
                    "paused_backend_connectivity",
                }:
                    return state
                time.sleep(poll_seconds)
