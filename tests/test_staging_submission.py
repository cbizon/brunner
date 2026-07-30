from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from brunner.contract import load_output_contract
from brunner.definition import (
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
)
from brunner.errors import ContractError
from brunner.staging import stage_challenge
from brunner.submission import validate_submission


ROOT = Path(__file__).parents[1]
EXAMPLE_ROOT = ROOT / "examples/text_benchmark"


def definition() -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="text-uppercase",
        version="1.0.0",
        root=EXAMPLE_ROOT,
        contract_path=EXAMPLE_ROOT / "output-contract.json",
        challenge=ChallengeDefinition(root=EXAMPLE_ROOT / "challenge"),
        evaluation=EvaluationDefinition(command=(sys.executable, "-c", "pass")),
    )


def test_stage_challenge_renders_contract_and_schemas(tmp_path: Path) -> None:
    contract = load_output_contract(EXAMPLE_ROOT / "output-contract.json")
    workspace = tmp_path / "workspace"

    report = stage_challenge(definition(), contract, workspace)

    prompt = (workspace / "PROMPT.md").read_text()
    assert "{{BRUNNER_OUTPUT_CONTRACT}}" not in prompt
    assert "Uppercase transformation" in prompt
    assert "PROMPT.md" in {path.name for path in workspace.iterdir()}
    assert (workspace / "schema/output-contract.json").is_file()
    assert (workspace / "schema/submission.schema.json").is_file()
    assert (workspace / "schema/final-response.schema.json").is_file()
    assert report.contract_sha256 == contract.sha256


def test_validate_submission_follows_manifest_artifact_paths(
    tmp_path: Path,
) -> None:
    contract = load_output_contract(EXAMPLE_ROOT / "output-contract.json")
    workspace = tmp_path / "workspace"
    stage_challenge(definition(), contract, workspace)
    submission = workspace / "submission"
    submission.mkdir()
    (submission / "result.txt").write_text("BRUNNER\n")
    (submission / "manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "output": "result.txt"})
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

    validated = validate_submission(workspace, contract)

    assert validated.run_status["status"] == "complete"
    assert len(validated.artifacts) == 1
    assert validated.artifacts[0].path == submission / "result.txt"


def test_validate_submission_rejects_manifest_path_escape(
    tmp_path: Path,
) -> None:
    contract = load_output_contract(EXAMPLE_ROOT / "output-contract.json")
    workspace = tmp_path / "workspace"
    stage_challenge(definition(), contract, workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    submission = workspace / "submission"
    submission.mkdir()
    (submission / "manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "output": "../../outside.txt"})
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

    with pytest.raises(ContractError, match="escapes"):
        validate_submission(workspace, contract)


def test_complete_status_requires_every_work_unit(
    tmp_path: Path,
) -> None:
    contract = load_output_contract(EXAMPLE_ROOT / "output-contract.json")
    workspace = tmp_path / "workspace"
    stage_challenge(definition(), contract, workspace)
    submission = workspace / "submission"
    submission.mkdir()
    (submission / "result.txt").write_text("BRUNNER\n")
    (submission / "manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "output": "result.txt"})
    )
    (submission / "run-status.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "submission_manifest": "submission/manifest.json",
                "completed_units": [],
                "limitations": [],
            }
        )
    )

    with pytest.raises(ContractError, match="every work unit"):
        validate_submission(workspace, contract)
