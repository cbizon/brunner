from __future__ import annotations

import json
import os
import subprocess
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
from brunner.evaluation import (
    evaluation_spec,
    execute_evaluation,
    finalize_evaluation,
)
from brunner.reference import (
    REFERENCE_POLICY,
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


def evaluate_trial(
    definition,
    contract,
    trial: Path,
    *,
    timeout_seconds: float | None = None,
):
    execute_evaluation(
        evaluation_spec(definition, contract),
        trial,
        reference_root=(
            definition.reference.root
            if definition.reference is not None
            else None
        ),
        timeout_seconds=timeout_seconds,
    )
    return finalize_evaluation(definition, contract, trial)


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


def _serialized_evaluation_spec(spec) -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "runtime_protocol": spec.runtime_protocol,
        "benchmark_id": spec.benchmark_id,
        "benchmark_version": spec.benchmark_version,
        "contract_sha256": spec.contract_sha256,
        "command": list(spec.command),
        "results_path": spec.results_path,
        "primary_report": spec.primary_report,
        "timeout_seconds": spec.timeout_seconds,
        "reference_manifest_path": spec.reference_manifest_path,
        "reference_manifest_sha256": spec.reference_manifest_sha256,
        "reference_validate_command": list(
            spec.reference_validate_command
        ),
    }


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
        str(ROOT)
        + os.pathsep
        + str(ROOT / "src")
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


def test_evaluation_cli_runs_deterministic_evaluator_in_subprocess(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("evaluation-cli", "codex", "fake", None),
    )
    _write_valid_submission(trial)
    spec = replace(
        evaluation_spec(definition, contract),
        command=(
            sys.executable,
            str(definition.root / "evaluator.py"),
        ),
    )
    termination_log = tmp_path / "termination.log"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(ROOT)
        + os.pathsep
        + str(ROOT / "src")
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    )
    environment["BRUNNER_TERMINATION_LOG"] = str(termination_log)
    environment["BRUNNER_EVALUATION_SPEC"] = json.dumps(
        _serialized_evaluation_spec(spec)
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "brunner.evaluation_cli",
            str(trial),
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads((trial / "evaluation/results.json").read_text())
    assert result["status"] == "complete"
    summary = json.loads(termination_log.read_text())
    assert summary["brunner_evaluation"]["status"] == "complete"


def test_evaluation_cli_candidate_failure_exits_successfully(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("candidate-failure", "codex", "fake", None),
    )
    spec = evaluation_spec(definition, contract)
    termination_log = tmp_path / "termination.log"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(ROOT)
        + os.pathsep
        + str(ROOT / "src")
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    )
    environment["BRUNNER_TERMINATION_LOG"] = str(termination_log)
    environment["BRUNNER_EVALUATION_SPEC"] = json.dumps(
        _serialized_evaluation_spec(spec)
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "brunner.evaluation_cli",
            str(trial),
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(termination_log.read_text())[
        "brunner_evaluation"
    ]
    assert summary["status"] == "failed"
    assert summary["candidate_failure"] is True


def test_invalid_submission_is_identified_as_candidate_failure(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("invalid-submission", "codex", "fake", None),
    )

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "failed"
    assert result["failure"]["domain"] == "candidate"
    assert result["failure"]["reason"] == "CandidateSubmissionInvalid"


def test_evaluator_workspace_setup_failure_requires_attention(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("workspace-setup-failure", "codex", "fake", None),
    )
    _write_valid_submission(trial)

    result = execute_evaluation(
        evaluation_spec(definition, contract),
        trial,
        working_directory_root=tmp_path / "missing",
    )

    assert result["status"] == "failed"
    assert result["failure"]["domain"] == "evaluation"
    assert result["failure"]["reason"] == "EvaluatorWorkspaceSetupFailed"
    assert result["failure"]["disposition"] == "attention"
    assert result["failure"]["resource"] == "evaluator_tmp"


def test_evaluator_workspace_overlap_is_integrity_failure(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("workspace-overlap", "codex", "fake", None),
    )
    _write_valid_submission(trial)

    result = execute_evaluation(
        evaluation_spec(definition, contract),
        trial,
        working_directory_root=trial / "workspace",
    )

    assert result["status"] == "failed"
    assert result["failure"]["domain"] == "integrity"
    assert result["failure"]["reason"] == "EvaluatorIsolationInvalid"
    assert result["failure"]["disposition"] == "attention"
    assert result["failure"]["resource"] == "evaluator_tmp"


def test_evaluator_failure_is_identified_as_trusted_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("evaluator-failure", "codex", "fake", None),
    )
    _write_valid_submission(trial)
    monkeypatch.setattr(
        evaluation_module,
        "_run_evaluator",
        lambda *args, **kwargs: 7,
    )

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "failed"
    assert result["failure"]["domain"] == "evaluation"
    assert result["failure"]["reason"] == "EvaluatorFailed"


def test_report_failure_does_not_replace_evaluation_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("report-failure", "codex", "fake", None),
    )
    _write_valid_submission(trial)
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT)
        + os.pathsep
        + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    monkeypatch.setattr(
        "brunner.report.write_run_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("report volume is full")
        ),
    )

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "complete"
    assert result["report"]["status"] == "failed"
    assert result["report"]["failure"]["domain"] == "reporting"


def test_report_metadata_persistence_failure_is_non_gating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity("report-write-failure", "codex", "fake", None),
    )
    _write_valid_submission(trial)
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT)
        + os.pathsep
        + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    real_write = evaluation_module.write_json_atomic
    writes = 0

    def fail_final_write(path: Path, value: object) -> None:
        nonlocal writes
        if path == trial / "evaluation/results.json":
            writes += 1
            if writes == 3:
                raise OSError("results volume became full during reporting")
        real_write(path, value)

    monkeypatch.setattr(
        evaluation_module,
        "write_json_atomic",
        fail_final_write,
    )

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "complete"
    assert result["report"]["status"] == "complete"
    assert writes == 3


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
    assert REFERENCE_POLICY.max_collection_bytes is None


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


def test_collection_omits_evaluated_artifacts_unless_explicitly_enabled(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    artifact = source / "workspace/submission/trajectory.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"x" * 1024)
    evaluation = source / "evaluation"
    evaluation.mkdir()
    (evaluation / "results.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": "complete",
                "summary": {},
                "metrics": {},
                "reports": [],
                "submission": {
                    "artifacts": [
                        {
                            "path": (
                                "workspace/submission/trajectory.bin"
                            )
                        }
                    ]
                },
            }
        )
    )

    default_destination = tmp_path / "default"
    collect_local_artifacts(
        source,
        default_destination,
        ArtifactPolicy(max_collection_bytes=512),
    )

    assert not (
        default_destination / "workspace/submission/trajectory.bin"
    ).exists()
    assert (default_destination / "evaluation/results.json").is_file()
    with pytest.raises(IntegrityError, match="exceeding the configured"):
        collect_local_artifacts(
            source,
            tmp_path / "explicit",
            ArtifactPolicy(
                collect_evaluated_artifacts=True,
                max_collection_bytes=512,
            ),
        )


def test_collection_uses_configured_evaluation_results_path(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "workspace/submission/trajectory.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"x" * 1024)
    results = tmp_path / "evaluation/custom-results.json"
    results.parent.mkdir()
    results.write_text(
        json.dumps(
            {
                "submission": {
                    "artifacts": [
                        {
                            "path": (
                                "workspace/submission/trajectory.bin"
                            )
                        }
                    ]
                }
            }
        )
    )

    inventory = file_inventory(
        tmp_path,
        ArtifactPolicy(),
        evaluation_results_path="evaluation/custom-results.json",
    )

    assert "workspace/submission/trajectory.bin" not in inventory
    assert "evaluation/custom-results.json" in inventory


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
        str(ROOT)
        + os.pathsep
        + str(ROOT / "src")
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


def test_evaluation_spec_carries_remote_reference_contract(
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
    spec = evaluation_spec(definition, contract)

    assert spec.image == "numeric-evaluator:1"
    assert spec.command == ("evaluate",)
    assert spec.reference_manifest_path == "manifest.json"
    assert spec.contract_sha256 == contract.sha256



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
        str(ROOT)
        + os.pathsep
        + str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    recorded: list[float] = []
    working_directories: list[Path] = []
    environments: list[dict[str, str]] = []
    real_run = evaluation_module._run_evaluator

    def capture(command, **kwargs):
        recorded.append(kwargs["timeout_seconds"])
        working_directories.append(kwargs["cwd"])
        environments.append(kwargs["environment"])
        return real_run(command, **kwargs)

    monkeypatch.setattr(evaluation_module, "_run_evaluator", capture)

    evaluate_trial(definition, contract, trial, timeout_seconds=30)

    assert len(recorded) == 2
    assert recorded[0] <= 30
    # The evaluator gets what the reference validator left, not a fresh 30s.
    assert recorded[1] < recorded[0] - 0.3
    assert working_directories[0] == working_directories[1]
    assert not working_directories[0].is_relative_to(trial)
    assert not working_directories[0].is_relative_to(
        definition.reference.root
    )
    assert all(
        environment["PYTHONSAFEPATH"] == "1"
        and environment["PYTHONNOUSERSITE"] == "1"
        for environment in environments
    )
