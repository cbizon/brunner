from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from brunner.campaign import default_workload_factory
from brunner.cluster import render_cluster_resources
from brunner.contract import load_output_contract
from brunner.evaluation import evaluation_spec, execute_evaluation
from brunner.submission import validate_submission
from brunner.trial import TrialIdentity, create_trial
from examples.diffusion_benchmark.campaign import build_campaign
from examples.diffusion_benchmark.cases import CASE_SPECS, build_request
from examples.diffusion_benchmark.definition import build_definition
from examples.diffusion_benchmark.reference_solver import solve


ROOT = Path(__file__).parents[1]
EXAMPLE_ROOT = ROOT / "examples/diffusion_benchmark"
COMPLETED_UNITS = [
    "implement-solver",
    "run-dirichlet-cases",
    "run-neumann-case",
]


def _gold_trial(tmp_path: Path) -> tuple[object, object, Path]:
    definition = build_definition()
    contract = load_output_contract(
        definition.contract_path,
        expected_benchmark_id=definition.benchmark_id,
    )
    trial = create_trial(
        definition,
        contract,
        tmp_path / "trials",
        TrialIdentity(
            "reference",
            "codex",
            "reference-solver",
            "low",
        ),
    )
    submission = trial / "workspace/submission"
    submission.mkdir()
    shutil.copy2(
        EXAMPLE_ROOT / "reference_solver.py",
        submission / "solver.py",
    )
    outputs = {}
    for spec in CASE_SPECS:
        output_path = submission / f"{spec['case_id']}.json"
        output_path.write_text(
            json.dumps(solve(build_request(spec)), indent=2) + "\n"
        )
        outputs[str(spec["case_id"])] = output_path.name
    (submission / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "solver": "solver.py",
                "case_outputs": outputs,
            },
            indent=2,
        )
        + "\n"
    )
    (submission / "run-status.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "submission_manifest": "submission/manifest.json",
                "completed_units": COMPLETED_UNITS,
                "limitations": [],
            },
            indent=2,
        )
        + "\n"
    )
    (trial / "status.json").write_text(
        json.dumps({"status": "complete"}) + "\n"
    )
    return definition, contract, trial


def _evaluation_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join((str(ROOT), str(ROOT / "src"))),
    )


def test_diffusion_challenge_materializes_candidate_visible_cases(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    assert not (definition.challenge.root / "cases").exists()

    trial = create_trial(
        definition,
        contract,
        tmp_path / "trials",
        TrialIdentity("materialized", "codex", "model-a", "low"),
    )
    workspace = trial / "workspace"

    assert not (definition.challenge.root / "cases").exists()
    assert {
        path.stem for path in (workspace / "cases").glob("*.json")
    } == {
        "index",
        "zero-dirichlet-sine",
        "offset-dirichlet-sine",
        "insulated-neumann-cosine",
    }
    assert (
        workspace / "schema/artifacts/case-results.schema.json"
    ).is_file()
    assert (workspace / "schema/output-contract.json").is_file()
    prompt = (workspace / "PROMPT.md").read_text()
    assert "case_outputs" in prompt
    assert "advance exactly to `t_final`" in prompt
    assert "use a shorter final timestep" in prompt
    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    marker = json.loads(
        (workspace / ".brunner-challenge.json").read_text()
    )
    assert metadata["challenge_sha256"] == marker["challenge_sha256"]


def test_diffusion_reference_solver_passes_trusted_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evaluation_environment(monkeypatch)
    definition, contract, trial = _gold_trial(tmp_path)

    validated = validate_submission(trial / "workspace", contract)
    result = execute_evaluation(
        evaluation_spec(definition, contract),
        trial,
    )

    assert len(validated.artifacts) == 4
    assert result["status"] == "complete"
    assert result["summary"]["passed"] is True
    assert result["summary"]["cases_passed"] == 3
    assert result["summary"]["minimum_observed_order"] > 1.9
    assert result["summary"]["max_held_out_rms_error"] < 0.015
    assert result["summary"]["max_held_out_abs_error"] < 0.04
    assert result["summary"]["solver_wall_seconds"] < 10
    report = trial / "evaluation/diffusion-report.html"
    assert report.is_file()
    report_text = report.read_text()
    assert "Diffusion solver evaluation" in report_text
    assert "Convergence step" in report_text


def test_diffusion_semantic_output_error_is_a_structured_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evaluation_environment(monkeypatch)
    definition, contract, trial = _gold_trial(tmp_path)
    output_path = (
        trial
        / "workspace/submission/zero-dirichlet-sine.json"
    )
    output = json.loads(output_path.read_text())
    output["steps_completed"] += 1
    output_path.write_text(json.dumps(output) + "\n")

    result = execute_evaluation(
        evaluation_spec(definition, contract),
        trial,
    )

    assert result["status"] == "failed"
    assert result["summary"]["execution_failures"] == 1
    assert result["error"]["type"] == "CandidateSolverError"
    assert "final profile step differs" in result["error"]["message"]
    assert (trial / "evaluation/diffusion-report.html").is_file()


def test_diffusion_rejects_final_time_overshoot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _evaluation_environment(monkeypatch)
    definition, contract, trial = _gold_trial(tmp_path)
    output_path = (
        trial
        / "workspace/submission/zero-dirichlet-sine.json"
    )
    output = json.loads(output_path.read_text())
    output["simulated_time"] += output["dt_requested"]
    output_path.write_text(json.dumps(output) + "\n")

    result = execute_evaluation(
        evaluation_spec(definition, contract),
        trial,
    )

    assert result["status"] == "failed"
    assert result["error"]["type"] == "CandidateSolverError"
    assert "simulated_time must equal t_final" in result["error"]["message"]


def test_diffusion_campaign_runs_two_models_and_exposes_dashboard(
    tmp_path: Path,
) -> None:
    definition = build_definition()
    contract = load_output_contract(definition.contract_path)
    campaign = build_campaign(definition, contract)

    campaign.validate()
    assert [
        (trial.provider, trial.model, trial.effort)
        for trial in campaign.plan.trials
    ] == [
        ("codex", "gpt-5.6-luna", "low"),
        ("claude", "claude-sonnet-5", "low"),
    ]
    assert campaign.plan.max_parallel == 2
    assert campaign.backend.image_pull_secrets == (
        "registry-credentials",
    )
    assert campaign.controller.image_pull_secrets == (
        "registry-credentials",
    )
    assert definition.qualitative_review is not None
    assert "workspace/submission" in (
        definition.qualitative_review.trial_evidence_paths
    )

    luna = default_workload_factory(
        tmp_path,
        campaign.plan.trials[0],
        campaign.plan,
        definition,
        "kubernetes",
    )
    assert "--provider-id" in luna.command
    assert "AZURE_OPENAI_API_KEY" in luna.command

    rendered = render_cluster_resources(
        definition,
        campaign,
        benchmark_ref="examples.diffusion_benchmark.definition",
        campaign_ref="examples.diffusion_benchmark.campaign",
    )
    service = next(
        resource
        for resource in rendered
        if resource["kind"] == "Service"
    )
    assert service["spec"]["ports"][0]["port"] == 8765
    assert campaign.controller.reviewer_secret_environment["codex"][
        "AZURE_OPENAI_API_KEY"
    ] == (
        "codex-provider-credentials",
        "AZURE_OPENAI_API_KEY",
    )


def test_diffusion_agent_image_excludes_trusted_benchmark_code() -> None:
    agent_recipe = (
        EXAMPLE_ROOT / "images/agent.Dockerfile"
    ).read_text()
    controller_recipe = (
        EXAMPLE_ROOT / "images/controller.Dockerfile"
    ).read_text()

    assert "COPY examples" not in agent_recipe
    assert "COPY examples" in controller_recipe
