from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from brunner.artifacts import (
    collect_local_artifacts,
    file_inventory,
)
from brunner.contract import load_output_contract
from brunner.definition import ArtifactPolicy
from brunner.errors import ContractError, IntegrityError
from brunner import evaluation as evaluation_module
from brunner.evaluation import evaluate_trial, evaluator_invocation
from brunner.reference import (
    build_reference_manifest,
    validate_reference_manifest,
)
from brunner.trial import TrialIdentity, create_trial
from brunner.submission import validate_submission
from examples.text_benchmark.definition import build_definition
from examples.numeric_benchmark.definition import (
    build_definition as build_numeric_definition,
)


ROOT = Path(__file__).parents[1]


def _write_valid_submission(trial: Path) -> None:
    submission = trial / "workspace/submission"
    submission.mkdir()
    input_text = (trial / "workspace/input.txt").read_text()
    (submission / "result.txt").write_text(input_text.upper())
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


def test_evaluate_trial_uses_contract_validated_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            test_id="evaluation",
            provider="codex",
            model="fake",
            effort=None,
        ),
    )
    _write_valid_submission(trial)
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "complete"
    assert result["metrics"]["exact_match"] == 1.0
    assert result["contract_sha256"] == contract.sha256
    assert result["submission"]["artifacts"][0]["artifact_id"] == (
        "transformed-text"
    )
    assert (trial / "evaluation/run-report.html").is_file()


def test_reference_manifest_excludes_itself_and_detects_tampering(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "answer.json").write_text('{"answer": 42}\n')
    manifest_path = reference / "manifest.json"

    manifest = build_reference_manifest(
        reference,
        manifest_path,
        metadata={"benchmark_id": "example"},
    )

    assert "manifest.json" not in manifest["files"]
    assert validate_reference_manifest(reference, manifest_path) == manifest
    (reference / "answer.json").write_text('{"answer": 43}\n')
    with pytest.raises(IntegrityError, match="inventory mismatch"):
        validate_reference_manifest(reference, manifest_path)


def test_artifact_collection_resumes_and_honors_groups(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.txt").write_text("complete result\n")
    (source / "large.bin").write_bytes(b"abcdefghij")
    debug = source / "debug"
    debug.mkdir()
    (debug / "trace.log").write_text("debug details\n")
    policy = ArtifactPolicy(groups={"debug": ("debug/**",)})
    destination = tmp_path / "collected"
    partial = tmp_path / "collected.partial"
    partial.mkdir()
    (partial / "large.bin").write_bytes(b"abc")

    report = collect_local_artifacts(source, destination, policy)

    assert report["files"] == 2
    assert (destination / "large.bin").read_bytes() == b"abcdefghij"
    assert not (destination / "debug/trace.log").exists()
    with_debug = file_inventory(
        source,
        policy,
        included_groups=frozenset({"debug"}),
    )
    assert "debug/trace.log" in with_debug


def test_artifact_inventory_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside\n")
    (source / "escape").symlink_to(target)

    with pytest.raises(IntegrityError, match="rejects symlink"):
        file_inventory(source, ArtifactPolicy())


def test_default_artifact_policy_excludes_provider_home(
    tmp_path: Path,
) -> None:
    (tmp_path / "provider-home").mkdir()
    (tmp_path / "provider-home/credential.json").write_text("{}")
    (tmp_path / "result.txt").write_text("result\n")

    inventory = file_inventory(tmp_path, ArtifactPolicy())

    assert "result.txt" in inventory
    assert "provider-home/credential.json" not in inventory


def test_reference_backed_benchmark_uses_staged_artifact_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = build_numeric_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            test_id="numeric",
            provider="codex",
            model="fake",
            effort=None,
        ),
    )
    assert (
        trial
        / "workspace/schema/artifacts/squared-values.schema.json"
    ).is_file()
    submission = trial / "workspace/submission"
    submission.mkdir()
    (submission / "results.json").write_text(
        json.dumps({"results": [4, 9, 25, 49]})
    )
    (submission / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "results": "results.json",
            }
        )
    )
    (submission / "run-status.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "submission_manifest": "submission/manifest.json",
                "completed_units": ["square-values"],
                "limitations": [],
            }
        )
    )
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "complete"
    assert result["metrics"]["value_accuracy"] == 1.0


def test_artifact_json_schema_is_enforced_before_evaluation(
    tmp_path: Path,
) -> None:
    definition = build_numeric_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("invalid", "codex", "fake", None),
    )
    submission = trial / "workspace/submission"
    submission.mkdir()
    (submission / "results.json").write_text(
        json.dumps({"results": [4, 9]})
    )
    (submission / "manifest.json").write_text(
        json.dumps(
            {"schema_version": "1.0", "results": "results.json"}
        )
    )
    (submission / "run-status.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "submission_manifest": "submission/manifest.json",
                "completed_units": ["square-values"],
                "limitations": [],
            }
        )
    )

    with pytest.raises(ContractError, match="invalid artifact"):
        validate_submission(trial / "workspace", contract)


def test_evaluator_container_invocation_mounts_reference_read_only(
    tmp_path: Path,
) -> None:
    base = build_numeric_definition()
    definition = type(base)(
        **{
            **base.__dict__,
            "evaluation": type(base.evaluation)(
                command=("evaluate",),
                image="numeric-evaluator:1",
            ),
        }
    )
    contract = load_output_contract(definition.contract_path)
    trial = tmp_path / "trial"
    (trial / "workspace/submission").mkdir(parents=True)
    environment = {
        "BRUNNER_SUBMISSION_MANIFEST": str(
            trial / "workspace/submission/manifest.json"
        ),
        "BRUNNER_RUN_STATUS": str(
            trial / "workspace/submission/run-status.json"
        ),
        "BRUNNER_EVALUATION_RESULTS": str(
            trial / "evaluation/results.json"
        ),
    }

    command, cwd, process_environment = evaluator_invocation(
        definition,
        contract,
        trial.resolve(),
        environment,
    )

    encoded = " ".join(command)
    assert command[0] == "docker"
    assert "--network none" in encoded
    assert "numeric-evaluator:1 evaluate" in encoded
    assert "dst=/brunner/reference,readonly" in encoded
    assert cwd == trial.resolve()
    assert process_environment is not environment



def test_evaluation_timeout_is_one_shared_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = build_numeric_definition()
    # A reference validator that consumes part of the budget before the
    # evaluator runs.
    definition = replace(
        base,
        reference=replace(
            base.reference,
            validate_command=(
                sys.executable,
                "-c",
                "import time; time.sleep(0.4)",
            ),
        ),
    )
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            test_id="budget",
            provider="codex",
            model="fake",
            effort=None,
        ),
    )
    submission = trial / "workspace/submission"
    submission.mkdir()
    (submission / "results.json").write_text(
        json.dumps({"results": [4, 9, 25, 49]})
    )
    (submission / "manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "results": "results.json"})
    )
    (submission / "run-status.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "submission_manifest": "submission/manifest.json",
                "completed_units": ["square-values"],
                "limitations": [],
            }
        )
    )
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    recorded: list[float] = []
    real_run = evaluation_module._run_evaluator

    def capture(command, **kwargs):
        recorded.append(kwargs["timeout_seconds"])
        return real_run(command, **kwargs)

    monkeypatch.setattr(evaluation_module, "_run_evaluator", capture)

    evaluate_trial(definition, contract, trial, timeout_seconds=30)

    assert len(recorded) == 2
    assert recorded[0] <= 30
    # The evaluator gets what the reference validator left, not a fresh 30s.
    assert recorded[1] < recorded[0] - 0.3
