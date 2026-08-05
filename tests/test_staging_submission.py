from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys

import pytest

from brunner.cli import run_cli
from brunner.contract import load_output_contract
from brunner.definition import (
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
)
from brunner.errors import (
    ChallengeMaterializationError,
    ConfigurationError,
    ContractError,
    IntegrityError,
)
from brunner.staging import stage_challenge
from brunner.submission import validate_submission
from brunner.trial import TrialIdentity, create_trial
from examples.text_benchmark.definition import (
    build_materialized_definition,
)


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


def materialized_definition(
    tmp_path: Path,
    script_body: str,
    *,
    arguments: tuple[str, ...] = (),
    forbidden_names: tuple[str, ...] = (),
    timeout_seconds: float = 5,
) -> tuple[BenchmarkDefinition, Path]:
    challenge_root = tmp_path / "challenge"
    shutil.copytree(EXAMPLE_ROOT / "challenge", challenge_root)
    script = tmp_path / "materialize.py"
    script.write_text(script_body)
    base = definition()
    return (
        replace(
            base,
            challenge=replace(
                base.challenge,
                root=challenge_root,
                forbidden_names=forbidden_names,
                materialize_command=(
                    sys.executable,
                    str(script),
                    *arguments,
                ),
                materialize_timeout_seconds=timeout_seconds,
            ),
        ),
        challenge_root,
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


def test_example_materializer_generates_candidate_resource(
    tmp_path: Path,
) -> None:
    benchmark = build_materialized_definition()
    contract = load_output_contract(benchmark.contract_path)

    stage_challenge(benchmark, contract, tmp_path / "workspace")

    assert (
        tmp_path / "workspace/resources/materialized-note.txt"
    ).read_text().startswith("This candidate-visible resource")


def test_materializer_adds_hashed_candidate_resource_without_mutating_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark, source = materialized_definition(
        tmp_path,
        """
import os
import json
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
assert Path.cwd() == root
resources = root / "resources"
resources.mkdir()
(resources / "generated.txt").write_text("candidate-visible\\n")
(root / "environment.json").write_text(json.dumps(
    {
        "cache": os.environ.get("BRUNNER_RESOURCE_CACHE"),
        "reference_visible": (
            "BRUNNER_REFERENCE_ROOT" in os.environ
        ),
    }
))
""",
    )
    cache = tmp_path / "resource-cache"
    monkeypatch.setenv("BRUNNER_RESOURCE_CACHE", str(cache))
    monkeypatch.setenv(
        "BRUNNER_REFERENCE_ROOT",
        str(tmp_path / "trusted-reference"),
    )
    contract = load_output_contract(benchmark.contract_path)
    workspace = tmp_path / "workspace"

    report = stage_challenge(benchmark, contract, workspace)

    assert (
        workspace / "resources/generated.txt"
    ).read_text() == "candidate-visible\n"
    assert not (source / "resources").exists()
    assert not (source / "environment.json").exists()
    assert json.loads((workspace / "environment.json").read_text()) == {
        "cache": str(cache),
        "reference_visible": False,
    }
    prompt = (workspace / "PROMPT.md").read_text()
    assert "{{BRUNNER_OUTPUT_CONTRACT}}" not in prompt
    assert "Uppercase transformation" in prompt
    assert (workspace / "schema/output-contract.json").is_file()
    assert (workspace / "schema/submission.schema.json").is_file()
    assert (workspace / "schema/final-response.schema.json").is_file()
    assert len(report.challenge_sha256) == 64


def test_materialized_content_changes_challenge_digest(
    tmp_path: Path,
) -> None:
    script_body = """
import os
import sys
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "materialized.txt").write_text(sys.argv[1])
"""
    alpha, _ = materialized_definition(
        tmp_path / "alpha",
        script_body,
        arguments=("alpha",),
    )
    beta, _ = materialized_definition(
        tmp_path / "beta",
        script_body,
        arguments=("beta",),
    )
    contract = load_output_contract(alpha.contract_path)

    alpha_report = stage_challenge(
        alpha,
        contract,
        tmp_path / "alpha-workspace",
    )
    beta_report = stage_challenge(
        beta,
        contract,
        tmp_path / "beta-workspace",
    )

    assert alpha_report.challenge_sha256 != beta_report.challenge_sha256


def test_materializer_failure_aborts_with_process_diagnostics(
    tmp_path: Path,
) -> None:
    benchmark, _ = materialized_definition(
        tmp_path,
        """
import sys

print("materializer standard output")
print("materializer standard error", file=sys.stderr)
raise SystemExit(7)
""",
    )
    contract = load_output_contract(benchmark.contract_path)
    workspace = tmp_path / "workspace"

    with pytest.raises(ChallengeMaterializationError) as captured:
        stage_challenge(benchmark, contract, workspace)

    message = str(captured.value)
    assert "command:" in message
    assert "materialize.py" in message
    assert "exit code: 7" in message
    assert "materializer standard output" in message
    assert "materializer standard error" in message
    assert not workspace.exists()


def test_materializer_failure_bounds_captured_output(
    tmp_path: Path,
) -> None:
    benchmark, _ = materialized_definition(
        tmp_path,
        """
import sys

print("x" * 100_000)
print("y" * 100_000, file=sys.stderr)
raise SystemExit(9)
""",
    )
    contract = load_output_contract(benchmark.contract_path)

    with pytest.raises(ChallengeMaterializationError) as captured:
        stage_challenge(benchmark, contract, tmp_path / "workspace")

    message = str(captured.value)
    assert "truncated" in message
    assert len(message) < 45_000


def test_materializer_timeout_aborts_staging(tmp_path: Path) -> None:
    benchmark, _ = materialized_definition(
        tmp_path,
        """
import time

print("materializer started", flush=True)
time.sleep(60)
""",
        timeout_seconds=0.1,
    )
    contract = load_output_contract(benchmark.contract_path)
    workspace = tmp_path / "workspace"

    with pytest.raises(
        ChallengeMaterializationError,
        match="timed out",
    ) as captured:
        stage_challenge(benchmark, contract, workspace)

    assert "materializer started" in str(captured.value)
    assert "exit code: unavailable" in str(captured.value)
    assert not workspace.exists()


def test_materializer_introduced_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    benchmark, _ = materialized_definition(
        tmp_path,
        """
import os
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "target.txt").write_text("target")
(root / "link.txt").symlink_to("target.txt")
""",
    )
    contract = load_output_contract(benchmark.contract_path)

    with pytest.raises(IntegrityError, match="symlink"):
        stage_challenge(benchmark, contract, tmp_path / "workspace")


def test_materializer_introduced_forbidden_name_is_rejected(
    tmp_path: Path,
) -> None:
    benchmark, _ = materialized_definition(
        tmp_path,
        """
import os
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "trusted-answer.txt").write_text("not candidate visible")
""",
        forbidden_names=("trusted-answer.txt",),
    )
    contract = load_output_contract(benchmark.contract_path)

    with pytest.raises(IntegrityError, match="forbidden name"):
        stage_challenge(benchmark, contract, tmp_path / "workspace")


def test_source_isolation_is_checked_before_materializer_runs(
    tmp_path: Path,
) -> None:
    benchmark, source = materialized_definition(
        tmp_path,
        """
import os
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "command-ran.txt").write_text("ran")
""",
        forbidden_names=("trusted-answer.txt",),
    )
    (source / "trusted-answer.txt").write_text("not materializer visible")
    contract = load_output_contract(benchmark.contract_path)

    with pytest.raises(IntegrityError, match="forbidden name"):
        stage_challenge(benchmark, contract, tmp_path / "workspace")

    assert not (source / "command-ran.txt").exists()


def test_no_materializer_preserves_staging_output(tmp_path: Path) -> None:
    contract = load_output_contract(EXAMPLE_ROOT / "output-contract.json")

    first = stage_challenge(
        definition(),
        contract,
        tmp_path / "first-workspace",
    )
    second = stage_challenge(
        definition(),
        contract,
        tmp_path / "second-workspace",
    )

    assert first.challenge_sha256 == second.challenge_sha256
    assert (
        tmp_path / "first-workspace/PROMPT.md"
    ).read_text() == (
        tmp_path / "second-workspace/PROMPT.md"
    ).read_text()
    assert (
        tmp_path / "first-workspace/input.txt"
    ).read_bytes() == (
        tmp_path / "second-workspace/input.txt"
    ).read_bytes()


def test_no_materializer_rejects_source_symlink(tmp_path: Path) -> None:
    challenge_root = tmp_path / "challenge"
    shutil.copytree(EXAMPLE_ROOT / "challenge", challenge_root)
    outside = tmp_path / "outside.txt"
    outside.write_text("must not become candidate-visible")
    (challenge_root / "outside-link.txt").symlink_to(outside)
    benchmark = replace(
        definition(),
        challenge=replace(definition().challenge, root=challenge_root),
    )
    contract = load_output_contract(benchmark.contract_path)

    with pytest.raises(IntegrityError, match="symlink"):
        stage_challenge(benchmark, contract, tmp_path / "workspace")

    assert not (tmp_path / "workspace").exists()


def test_interrupted_trial_creation_never_publishes_partial_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = definition()
    contract = load_output_contract(benchmark.contract_path)
    tests_root = tmp_path / "trials"
    monkeypatch.setattr(
        "brunner.trial.stage_challenge",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt()
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        create_trial(
            benchmark,
            contract,
            tests_root,
            TrialIdentity("interrupted", "codex", "test-model", None),
        )

    assert not (tests_root / "interrupted").exists()
    assert list(tests_root.glob(".interrupted.creating-*")) == []


def test_trial_creation_uses_materialized_challenge(tmp_path: Path) -> None:
    benchmark, _ = materialized_definition(
        tmp_path / "benchmark",
        """
import os
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "trial-resource.txt").write_text("available before submission")
""",
    )
    contract = load_output_contract(benchmark.contract_path)

    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "trials",
        TrialIdentity("materialized", "codex", "test-model", None),
    )

    assert (
        trial / "workspace/trial-resource.txt"
    ).read_text() == "available before submission"
    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    staged = json.loads(
        (trial / "workspace/.brunner-challenge.json").read_text()
    )
    assert metadata["challenge_sha256"] == staged["challenge_sha256"]


def test_trial_metadata_records_optional_display_title(tmp_path: Path) -> None:
    benchmark = replace(definition(), display_title="Text transformation")
    contract = load_output_contract(benchmark.contract_path)

    trial = create_trial(
        benchmark,
        contract,
        tmp_path / "trials",
        TrialIdentity("display-title", "codex", "test-model", "high"),
    )

    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    assert metadata["display_title"] == "Text transformation"


def test_empty_display_title_is_rejected() -> None:
    benchmark = replace(definition(), display_title=" ")

    with pytest.raises(ConfigurationError, match="display_title"):
        benchmark.validate()


def test_failed_materialization_removes_partial_trial(
    tmp_path: Path,
) -> None:
    benchmark, _ = materialized_definition(
        tmp_path / "benchmark",
        "raise SystemExit(9)\n",
    )
    contract = load_output_contract(benchmark.contract_path)
    tests_root = tmp_path / "trials"

    with pytest.raises(ChallengeMaterializationError):
        create_trial(
            benchmark,
            contract,
            tests_root,
            TrialIdentity("failed", "codex", "test-model", None),
        )

    assert not (tests_root / "failed").exists()


def test_cli_stage_and_trial_create_use_materialization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    benchmark, _ = materialized_definition(
        tmp_path / "benchmark",
        """
import os
from pathlib import Path

root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
(root / "cli-resource.txt").write_text("created by materializer")
""",
    )
    stage_workspace = tmp_path / "staged"

    run_cli(benchmark, ["stage", str(stage_workspace)])
    capsys.readouterr()
    run_cli(
        benchmark,
        [
            "trial-create",
            str(tmp_path / "trials"),
            "--provider",
            "codex",
            "--model",
            "test-model",
            "--test-id",
            "cli-materialized",
        ],
    )
    capsys.readouterr()

    assert (stage_workspace / "cli-resource.txt").is_file()
    assert (
        tmp_path
        / "trials/cli-materialized/workspace/cli-resource.txt"
    ).is_file()


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
