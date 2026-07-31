from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from brunner import __version__
from brunner.contract import OutputContract
from brunner.definition import BenchmarkDefinition
from brunner.io import write_json_atomic
from brunner.staging import StageReport, stage_challenge


TRIAL_DIRECTORIES = (
    "metadata",
    "workspace",
    "transcript",
    "usage",
    "timing",
    "evaluation",
    "assessments",
    "backend",
)


@dataclass(frozen=True)
class TrialIdentity:
    test_id: str
    provider: str
    model: str
    effort: str | None


def new_test_id(provider: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{provider}-{uuid.uuid4().hex[:8]}"


def create_trial(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    tests_root: Path,
    identity: TrialIdentity,
) -> Path:
    definition.validate()
    trial = tests_root.resolve() / identity.test_id
    trial.mkdir(parents=True, exist_ok=False)
    for name in TRIAL_DIRECTORIES:
        (trial / name).mkdir()
    staged = stage_challenge(
        definition,
        contract,
        trial / "workspace",
    )
    write_trial_metadata(
        definition,
        contract,
        identity,
        trial,
        staged,
    )
    return trial


def write_trial_metadata(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    identity: TrialIdentity,
    trial: Path,
    staged: StageReport,
) -> None:
    write_json_atomic(
        trial / "metadata/manifest.json",
        {
            "schema_version": "1.0",
            "test_id": identity.test_id,
            "provider": identity.provider,
            "model": identity.model,
            "effort": identity.effort,
            "benchmark_id": definition.benchmark_id,
            "benchmark_version": definition.version,
            "brunner_version": __version__,
            "contract_sha256": contract.sha256,
            "challenge_sha256": staged.challenge_sha256,
            "assessment_contracts": [
                assessment.contract_manifest()
                for assessment in definition.assessments
            ],
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    write_json_atomic(
        trial / "metadata/agent-run.json",
        {
            "schema_version": "1.0",
            "benchmark_id": definition.benchmark_id,
            "benchmark_version": definition.version,
            "contract_sha256": contract.sha256,
            "rendered_prompt": definition.challenge.rendered_prompt,
            "runtime": {
                "timeout_seconds": definition.runtime.timeout_seconds,
                "finalization_seconds": (
                    definition.runtime.finalization_seconds
                ),
                "retry_initial_seconds": (
                    definition.runtime.retry_initial_seconds
                ),
                "retry_max_seconds": (
                    definition.runtime.retry_max_seconds
                ),
                "provider_exit_grace_seconds": (
                    definition.runtime.provider_exit_grace_seconds
                ),
                "backend_shutdown_grace_seconds": (
                    definition.runtime.backend_shutdown_grace_seconds
                ),
            },
        },
    )


def load_trial_identity(trial: Path) -> TrialIdentity:
    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    return TrialIdentity(
        test_id=str(metadata["test_id"]),
        provider=str(metadata["provider"]),
        model=str(metadata["model"]),
        effort=(
            str(metadata["effort"])
            if metadata.get("effort") is not None
            else None
        ),
    )
