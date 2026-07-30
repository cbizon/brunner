from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brunner.contract import OutputContract, load_output_contract
from brunner.errors import ContractError, IntegrityError
from brunner.reference import validate_reference_manifest
from brunner.submission import ValidatedArtifact, ValidatedSubmission, validate_submission


@dataclass(frozen=True)
class EvaluationInput:
    trial_root: Path
    workspace: Path
    contract: OutputContract
    submission: ValidatedSubmission
    results_path: Path
    reference_root: Path | None = None
    reference_manifest: dict[str, Any] | None = None

    def artifacts(self, artifact_id: str) -> tuple[ValidatedArtifact, ...]:
        selected = tuple(
            artifact
            for artifact in self.submission.artifacts
            if artifact.artifact_id == artifact_id
        )
        if not selected:
            raise ContractError(
                f"submission has no validated artifact {artifact_id!r}"
            )
        return selected

    def artifact(self, artifact_id: str) -> ValidatedArtifact:
        selected = self.artifacts(artifact_id)
        if len(selected) != 1:
            raise ContractError(
                f"artifact {artifact_id!r} has {len(selected)} files; "
                "use artifacts()"
            )
        return selected[0]


def _required_path(environment: dict[str, str], name: str) -> Path:
    value = environment.get(name)
    if not value:
        raise ContractError(f"missing evaluator environment variable {name}")
    return Path(value).resolve()


def load_evaluation_input(
    environment: dict[str, str] | None = None,
) -> EvaluationInput:
    environment = environment or dict(os.environ)
    trial_root = _required_path(environment, "BRUNNER_TRIAL_ROOT")
    workspace = _required_path(environment, "BRUNNER_WORKSPACE")
    contract = load_output_contract(
        _required_path(environment, "BRUNNER_OUTPUT_CONTRACT")
    )
    expected_digest = environment.get("BRUNNER_CONTRACT_SHA256")
    if expected_digest != contract.sha256:
        raise IntegrityError(
            "evaluator contract digest does not match the trial contract"
        )
    submission = validate_submission(workspace, contract)
    results_path = _required_path(
        environment,
        "BRUNNER_EVALUATION_RESULTS",
    )

    reference_root = None
    reference_manifest = None
    reference_root_value = environment.get("BRUNNER_REFERENCE_ROOT")
    reference_manifest_value = environment.get(
        "BRUNNER_REFERENCE_MANIFEST"
    )
    if bool(reference_root_value) != bool(reference_manifest_value):
        raise ContractError(
            "reference root and manifest must be provided together"
        )
    if reference_root_value and reference_manifest_value:
        reference_root = Path(reference_root_value).resolve()
        reference_manifest_path = Path(reference_manifest_value).resolve()
        reference_manifest = validate_reference_manifest(
            reference_root,
            reference_manifest_path,
        )
        metadata = reference_manifest.get("metadata", {})
        reference_digest = metadata.get("contract_sha256")
        if (
            reference_digest is not None
            and reference_digest != contract.sha256
        ):
            raise IntegrityError(
                "reference bundle contract digest does not match the trial"
            )

    return EvaluationInput(
        trial_root=trial_root,
        workspace=workspace,
        contract=contract,
        submission=submission,
        results_path=results_path,
        reference_root=reference_root,
        reference_manifest=reference_manifest,
    )


def write_evaluation_result(
    evaluation_input: EvaluationInput,
    *,
    status: str,
    summary: dict[str, Any],
    metrics: dict[str, Any],
    reports: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    value: dict[str, Any] = {
        "schema_version": "1.0",
        "status": status,
        "summary": summary,
        "metrics": metrics,
        "reports": reports or [],
    }
    if error is not None:
        value["error"] = error
    evaluation_input.results_path.parent.mkdir(parents=True, exist_ok=True)
    evaluation_input.results_path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n"
    )
