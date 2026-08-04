from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import pytest

from brunner import (
    AssessmentDefinition,
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
    ProviderSettings,
    QualitativeReviewDefinition,
)
from brunner.artifacts import collect_local_artifacts
from brunner.backends import (
    BackendCapacity,
    BackendHandle,
    BackendSnapshot,
    WorkloadSpec,
)
from brunner.campaign import CampaignPlan, CampaignRunner, CampaignTrial
from brunner.contract import load_output_contract
from brunner.definition import ArtifactPolicy
from brunner.errors import ConfigurationError
from brunner.evaluation import evaluate_trial
from brunner.trial import TrialIdentity, create_trial


ROOT = Path(__file__).parents[1]
EXAMPLE_ROOT = ROOT / "examples/text_benchmark"


def _evidence(finding: str) -> dict[str, Any]:
    return {
        "source": "deterministic_evaluation",
        "path": "evidence/trial/evaluation/results.json",
        "location": "metrics.exact_match",
        "finding": finding,
    }


def _criterion(
    rating: str = "correct",
    summary: str = "The available evidence supports the result.",
) -> dict[str, Any]:
    return {
        "applicability": "applicable",
        "rating": rating,
        "confidence": "high",
        "summary": summary,
        "evidence": [_evidence("exact_match=1")],
    }


def _valid_review() -> dict[str, Any]:
    criteria = {
        name: _criterion()
        for name in (
            "task_fidelity",
            "result_quality",
            "implementation_quality",
            "testing_and_validation",
            "reproducibility",
            "efficiency_and_time_use",
            "rule_compliance",
            "claims_and_evidence",
        )
    }
    return {
        "schema_version": "1.0",
        "rubric_version": "1.0",
        "task_summary": "<script>alert('review')</script>",
        "approach": {
            "primary_classification": "direct_implementation",
            "components": ["direct_implementation"],
            "output_provenance": (
                "generated_by_submitted_implementation"
            ),
            "confidence": "high",
            "summary": "The submitted program generated the output.",
            "evidence": [_evidence("The output matches the input transform.")],
        },
        "criteria": criteria,
        "transcript_review": {
            "narrative": "The agent implemented, ran, and checked the task.",
            "milestones": [
                {
                    "sequence": 1,
                    "phase": "implementation",
                    "started_at": None,
                    "ended_at": None,
                    "summary": "Implemented the transformation.",
                    "evidence": [],
                }
            ],
            "time_accounting": {
                "source": "insufficient",
                "wall_seconds": None,
                "agent_active_seconds": None,
                "foreground_tool_seconds": None,
                "external_wait_seconds": None,
                "subscription_wait_seconds": None,
                "runner_retry_wait_seconds": None,
                "runner_overhead_seconds": None,
                "unclassified_seconds": None,
                "background_job_seconds": None,
                "exclusive_partition": None,
                "background_job_may_overlap": None,
                "summary": "This fixture has no runner timing records.",
                "limitations": ["Timing is unavailable in this fixture."],
            },
        },
        "overall": {
            "rating": "strong",
            "confidence": "high",
            "bottom_line": "The submission satisfies the benchmark.",
            "strengths": ["Correct generated output"],
            "major_failures": [],
            "evidence": [_evidence("The deterministic evaluator completed.")],
        },
        "review_limitations": ["The fixture contains limited transcript data."],
    }


def _write_reviewer(path: Path, review: dict[str, Any]) -> None:
    encoded = json.dumps(review)
    path.write_text(
        f"""#!{sys.executable}
import json
import sys
from pathlib import Path

assert Path("contract/RUBRIC.md").is_file()
assert Path("contract/reviewer-prompt.md").is_file()
assert Path("contract/qualitative-review.schema.json").is_file()
assert Path("review-input.json").is_file()
assert Path("resolved-output.schema.json").is_file()
resolved_schema = json.loads(
    Path("resolved-output.schema.json").read_text()
)
assert '"allOf"' not in json.dumps(resolved_schema)
arguments = sys.argv[1:]
final = Path(arguments[arguments.index("--output-last-message") + 1])
result = json.loads({encoded!r})
final.write_text(json.dumps(result))
print(json.dumps({{
    "type": "turn.completed",
    "structured_output": result,
    "usage": {{"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}},
}}))
"""
    )
    path.chmod(0o755)


def _copy_benchmark(tmp_path: Path) -> Path:
    root = tmp_path / "benchmark"
    shutil.copytree(EXAMPLE_ROOT, root)
    return root


def _definition(
    root: Path,
    reviewer: Path | None = None,
) -> BenchmarkDefinition:
    qualitative_review = None
    if reviewer is not None:
        qualitative_review = QualitativeReviewDefinition(
            reviewer=ProviderSettings(
                provider="codex",
                model="review-model",
                effort="high",
            ),
            reviewer_executable=str(reviewer),
            max_attempts=1,
        )
    return BenchmarkDefinition(
        benchmark_id="text-uppercase",
        version="1.0.0",
        root=root,
        contract_path=root / "output-contract.json",
        challenge=ChallengeDefinition(root=root / "challenge"),
        evaluation=EvaluationDefinition(
            command=(sys.executable, str(root / "evaluator.py")),
        ),
        qualitative_review=qualitative_review,
    )


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


def _create_trial(
    tmp_path: Path,
    definition: BenchmarkDefinition,
    *,
    test_id: str = "qualitative-review-test",
) -> tuple[Path, Any]:
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            test_id=test_id,
            provider="codex",
            model="candidate-model",
            effort="high",
        ),
    )
    _write_valid_submission(trial)
    return trial, contract


def _set_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )


def test_standard_qualitative_review_runs_and_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    reviewer = tmp_path / "reviewer"
    _write_reviewer(reviewer, _valid_review())
    definition = _definition(root, reviewer)
    trial, contract = _create_trial(tmp_path, definition)
    (trial / "backend/workload.log").write_text(
        json.dumps(
            {
                "provider": "codex",
                "model": "candidate-model",
                "effort": "high",
            }
        )
    )
    _set_pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment = result["assessments"][0]
    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    dossier = json.loads(
        (trial / "evaluation/qualitative-review-input.json").read_text()
    )
    rendered = (trial / "evaluation/qualitative-review.html").read_text()
    assert result["assessment_status"] == "complete"
    assert result["required_assessments_complete"] is True
    assert assessment["assessment_id"] == "qualitative-review"
    assert assessment["status"] == "complete"
    assert assessment["required"] is False
    assert assessment["usage"]["total_tokens"] == 12
    assert metadata["assessment_contracts"][0]["assessment_id"] == (
        "qualitative-review"
    )
    roles = {
        material["role"]
        for material in metadata["assessment_contracts"][0]["materials"]
    }
    assert roles == {"prompt", "rubric", "output_schema", "render_command"}
    assert dossier["identity_blinding"]["identity_blinded"] is True
    assert "candidate-model" not in json.dumps(dossier)
    evidence_root = (
        trial
        / "assessments/qualitative-review/workspace/evidence"
    )
    copied_evidence = "\n".join(
        path.read_text(errors="replace")
        for path in evidence_root.rglob("*")
        if path.is_file()
    )
    assert "candidate-model" not in copied_evidence
    assert not (
        evidence_root / "trial/backend/workload.log"
    ).exists()
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "Task Fidelity" in rendered


def test_benchmark_without_qualitative_review_is_unchanged(
    tmp_path: Path,
) -> None:
    definition = _definition(_copy_benchmark(tmp_path))
    trial, contract = _create_trial(tmp_path, definition)

    result = evaluate_trial(definition, contract, trial)

    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    assert result["assessment_status"] == "not_configured"
    assert result["assessments"] == []
    assert metadata["assessment_contracts"] == []
    assert not (trial / "evaluation/qualitative-review.json").exists()


def test_qualitative_renderer_derives_report_from_output_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brunner.qualitative.render import main

    trial = tmp_path / "trial"
    output = trial / "evaluation/custom-review.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(_valid_review()))
    monkeypatch.setenv("BRUNNER_TRIAL_ROOT", str(trial))
    monkeypatch.setenv("BRUNNER_ASSESSMENT_OUTPUT", str(output))

    main()

    assert (trial / "evaluation/custom-review.html").is_file()
    assert not (trial / "evaluation/qualitative-review.html").exists()


def test_standard_review_runs_after_failed_deterministic_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    reviewer = tmp_path / "reviewer"
    _write_reviewer(reviewer, _valid_review())
    definition = _definition(root, reviewer)
    trial, contract = _create_trial(tmp_path, definition)
    (trial / "workspace/submission/result.txt").unlink()
    _set_pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "failed"
    assert result["assessment_status"] == "complete"
    assert result["assessments"][0]["status"] == "complete"
    assert (trial / "evaluation/qualitative-review.json").is_file()


def test_standard_review_id_cannot_collide_with_custom_assessment(
    tmp_path: Path,
) -> None:
    root = _copy_benchmark(tmp_path)
    reviewer = tmp_path / "reviewer"
    _write_reviewer(reviewer, _valid_review())
    assessment_root = root / "assessment"
    assessment_root.mkdir()
    (assessment_root / "prompt.md").write_text("Review.\n")
    (assessment_root / "schema.json").write_text(
        json.dumps({"type": "object"})
    )
    definition = replace(
        _definition(root, reviewer),
        assessments=(
            AssessmentDefinition(
                assessment_id="qualitative-review",
                root=assessment_root,
                prompt_path="prompt.md",
                output_schema_path="schema.json",
                output_path="evaluation/custom-review.json",
                command=(sys.executable, "-c", "pass"),
            ),
        ),
    )

    with pytest.raises(
        ConfigurationError,
        match="assessment IDs must be unique",
    ):
        definition.validate()


def test_standard_review_artifacts_cannot_be_overwritten(
    tmp_path: Path,
) -> None:
    root = _copy_benchmark(tmp_path)
    reviewer = tmp_path / "reviewer"
    _write_reviewer(reviewer, _valid_review())
    assessment_root = root / "assessment"
    assessment_root.mkdir()
    (assessment_root / "prompt.md").write_text("Review.\n")
    (assessment_root / "schema.json").write_text(
        json.dumps({"type": "object"})
    )
    definition = replace(
        _definition(root, reviewer),
        assessments=(
            AssessmentDefinition(
                assessment_id="custom-review",
                root=assessment_root,
                prompt_path="prompt.md",
                output_schema_path="schema.json",
                output_path="evaluation/qualitative-review.json",
                command=(sys.executable, "-c", "pass"),
            ),
        ),
    )

    with pytest.raises(
        ConfigurationError,
        match="assessment artifact path",
    ):
        definition.validate()


class ReviewBackend:
    name = "review"

    def __init__(self) -> None:
        self.cleaned: set[str] = set()

    def submit(self, workload: WorkloadSpec) -> BackendHandle:
        _write_valid_submission(workload.trial)
        return BackendHandle(
            backend=self.name,
            workload_id=workload.workload_id,
            native_id=workload.workload_id,
            trial=workload.trial,
        )

    def inspect(self, handle: BackendHandle) -> BackendSnapshot:
        return BackendSnapshot(phase="succeeded", exit_code=0)

    def logs(self, handle: BackendHandle) -> str:
        return "candidate complete\n"

    def collect(
        self,
        handle: BackendHandle,
        destination: Path,
        policy: ArtifactPolicy,
        *,
        included_groups: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        return collect_local_artifacts(
            handle.trial,
            destination,
            policy,
            included_groups=included_groups,
        )

    def cleanup(self, handle: BackendHandle) -> None:
        review = (
            Path(handle.trial).parents[1]
            / "collected"
            / handle.workload_id
            / "evaluation/qualitative-review.json"
        )
        assert review.is_file()
        self.cleaned.add(handle.workload_id)

    def capacity(self) -> BackendCapacity:
        return BackendCapacity(
            limit=1,
            running=0,
            pending=0,
            available=1,
        )


def _workload(
    trial: Path,
    campaign_trial: CampaignTrial,
    plan: CampaignPlan,
    definition: BenchmarkDefinition,
    backend_name: str,
) -> WorkloadSpec:
    return WorkloadSpec(
        workload_id=trial.name,
        trial=trial,
        command=("unused",),
        timeout_seconds=10,
    )


def test_campaign_runs_standard_review_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    reviewer = tmp_path / "reviewer"
    _write_reviewer(reviewer, _valid_review())
    definition = _definition(root, reviewer)
    contract = load_output_contract(definition.contract_path)
    backend = ReviewBackend()
    plan = CampaignPlan(
        campaign_id="qualitative-campaign",
        root=tmp_path / "campaign",
        trials=(
            CampaignTrial(
                "reviewed-run",
                "codex",
                "candidate-model",
                effort="high",
            ),
        ),
    )
    runner = CampaignRunner(
        definition,
        contract,
        plan,
        backend,
        workload_factory=_workload,
    )
    _set_pythonpath(monkeypatch)

    runner.advance()
    state = runner.advance()

    trial = state["trials"][0]
    assert trial["phase"] == "complete"
    assert trial["outcome"] == "succeeded"
    assert trial["evaluation"]["assessment_status"] == "complete"
    assert backend.cleaned == {"reviewed-run"}
    dashboard = (plan.root / "index.html").read_text()
    assert "strong" in dashboard
    assert "direct_implementation" in dashboard
