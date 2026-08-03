from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from brunner import (
    AssessmentDefinition,
    AssessmentReport,
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
)
from brunner.assessment import (
    _preflight_provider_schema,
    _resolved_provider_schema,
    run_assessments,
)
from brunner.contract import load_output_contract
from brunner.errors import ConfigurationError, ProviderSchemaError
from brunner.evaluation import evaluate_trial
from brunner.providers import ProviderSettings
from brunner.trial import TrialIdentity, create_trial


ROOT = Path(__file__).parents[1]
EXAMPLE_ROOT = ROOT / "examples/text_benchmark"
COMMON_SCHEMA = (
    "https://brunner.dev/schemas/assessment-common.schema.json"
)


def _write_assessment_materials(
    root: Path,
    *,
    verdict_type: str = "string",
) -> None:
    root.mkdir()
    (root / "prompt.md").write_text(
        "Review the supplied benchmark evidence.\n"
    )
    (root / "rubric.md").write_text(
        "Every applicable criterion requires evidence.\n"
    )
    (root / "review.schema.json").write_text(
        json.dumps(
            {
                "$schema": (
                    "https://json-schema.org/draft/2020-12/schema"
                ),
                "type": "object",
                "additionalProperties": False,
                "required": ["verdict", "criterion"],
                "properties": {
                    "verdict": {"type": verdict_type},
                    "criterion": {
                        "$ref": f"{COMMON_SCHEMA}#/$defs/criterion"
                    },
                },
            }
        )
    )


def _copy_benchmark(tmp_path: Path) -> Path:
    root = tmp_path / "benchmark"
    shutil.copytree(EXAMPLE_ROOT, root)
    return root


def _definition(
    root: Path,
    assessment: AssessmentDefinition,
) -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="text-uppercase",
        version="1.0.0",
        root=root,
        contract_path=root / "output-contract.json",
        challenge=ChallengeDefinition(root=root / "challenge"),
        evaluation=EvaluationDefinition(
            command=(sys.executable, str(root / "evaluator.py")),
        ),
        assessments=(assessment,),
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
) -> tuple[Path, object]:
    contract = load_output_contract(definition.contract_path)
    trial = create_trial(
        definition,
        contract,
        tmp_path / "tests",
        TrialIdentity(
            test_id="assessment-test",
            provider="codex",
            model="candidate-model",
            effort="high",
        ),
    )
    _write_valid_submission(trial)
    return trial, contract


def _pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )


def test_command_assessment_builds_dossier_validates_and_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    prepare = assessment_root / "prepare.py"
    prepare.write_text(
        """
import json
import os
from pathlib import Path

evaluation = json.loads(Path(os.environ["BRUNNER_EVALUATION_RESULTS"]).read_text())
Path(os.environ["BRUNNER_ASSESSMENT_BENCHMARK_INPUT"]).write_text(
    json.dumps({"exact_match": evaluation["metrics"]["exact_match"]})
)
"""
    )
    assess = assessment_root / "assess.py"
    assess.write_text(
        """
from brunner.assessment import (
    load_assessment_input,
    write_assessment_output,
)

assessment_input = load_assessment_input()
dossier = assessment_input.dossier
result = {
    "verdict": "pass",
    "criterion": {
        "applicability": "applicable",
        "rating": "correct",
        "confidence": "high",
        "summary": "The deterministic evaluator passed.",
        "evidence": [{
            "source": "deterministic_metric",
            "path": "evaluation/results.json",
            "finding": f"exact_match={dossier['benchmark_input']['exact_match']}",
        }],
    },
}
write_assessment_output(assessment_input, result)
"""
    )
    render = assessment_root / "render.py"
    render.write_text(
        """
import os
from pathlib import Path

trial = Path(os.environ["BRUNNER_TRIAL_ROOT"])
(trial / "evaluation/qualitative-review.html").write_text(
    "<html><body>qualitative review</body></html>"
)
"""
    )
    assessment = AssessmentDefinition(
        assessment_id="qualitative",
        root=assessment_root,
        prompt_path="prompt.md",
        rubric_paths=("rubric.md",),
        output_schema_path="review.schema.json",
        input_path="evaluation/review-input.json",
        output_path="evaluation/qualitative-review.json",
        prepare_command=(sys.executable, str(prepare)),
        command=(sys.executable, str(assess)),
        render_command=(sys.executable, str(render)),
        reports=(
            AssessmentReport(
                path="evaluation/qualitative-review.html",
                media_type="text/html",
                title="Qualitative review",
                primary=True,
            ),
        ),
        required=True,
    )
    definition = _definition(root, assessment)
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    review = json.loads(
        (trial / "evaluation/qualitative-review.json").read_text()
    )
    dossier_text = (trial / "evaluation/review-input.json").read_text()
    assessment_result = result["assessments"][0]
    assert result["status"] == "complete"
    assert result["assessment_status"] == "complete"
    assert result["required_assessments_complete"] is True
    assert review["verdict"] == "pass"
    assert assessment_result["status"] == "complete"
    assert assessment_result["input"]["sha256"]
    assert assessment_result["output"]["sha256"]
    assert assessment_result["identity_blinding"]["identity_blinded"] is True
    assert assessment_result["contract"]["contract_sha256"]
    assert assessment_result["reports"] == [
        {
            "path": "evaluation/qualitative-review.html",
            "media_type": "text/html",
            "title": "Qualitative review",
            "primary": True,
        }
    ]
    assert "candidate-model" not in dossier_text
    assert "benchmark_input" in dossier_text
    assert (trial / "evaluation/qualitative-review.html").is_file()
    report = (trial / "evaluation/run-report.html").read_text()
    assert "qualitative" in report
    assert "Qualitative review" in report
    assert 'href="qualitative-review.json"' not in report

    rerun = run_assessments(definition, contract, trial, result)
    rerun_dossier = json.loads(
        (trial / "evaluation/review-input.json").read_text()
    )
    copied_evaluation = json.loads(
        (
            trial
            / "assessments/qualitative/workspace/evidence/trial/"
            "evaluation/results.json"
        ).read_text()
    )
    assert rerun["status"] == "complete"
    assert "assessments" not in rerun_dossier["deterministic_evaluation"]
    assert "assessments" not in copied_evaluation


def test_optional_assessment_failure_does_not_replace_evaluation_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    assess = assessment_root / "assess.py"
    assess.write_text(
        """
import json
import os
from pathlib import Path

Path(os.environ["BRUNNER_ASSESSMENT_OUTPUT"]).write_text(
    json.dumps({"verdict": 3, "criterion": {}})
)
"""
    )
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="optional-review",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/optional-review.json",
            command=(sys.executable, str(assess)),
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "complete"
    assert result["assessment_status"] == "partial"
    assert result["required_assessments_complete"] is True
    assert result["assessments"][0]["status"] == "failed"
    assert "invalid assessment output" in (
        result["assessments"][0]["error"]["message"]
    )


def test_required_assessment_failure_is_reported_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    assess = assessment_root / "assess.py"
    assess.write_text(
        """
import os
from pathlib import Path

Path(os.environ["BRUNNER_ASSESSMENT_OUTPUT"]).write_text("{}")
"""
    )
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="required-review",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/required-review.json",
            command=(sys.executable, str(assess)),
            required=True,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assert result["status"] == "complete"
    assert result["assessment_status"] == "failed"
    assert result["required_assessments_complete"] is False
    assert result["assessments"][0]["status"] == "failed"


def test_provider_reviewer_records_identity_usage_and_structured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    reviewer = tmp_path / "codex-reviewer"
    reviewer.write_text(
        """#!/bin/sh
set -eu
if [ "${BRUNNER_TRIAL_ROOT+x}" = x ]; then exit 9; fi
case "$PWD" in
  *assessment-test*) exit 10 ;;
esac
final=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--output-last-message" ]; then final="$argument"; fi
  previous="$argument"
done
result='{"verdict":"pass","criterion":{"applicability":"applicable","rating":"correct","confidence":"high","summary":"Evidence supports the result.","evidence":[{"source":"metric","path":"evaluation/results.json","finding":"exact_match=1"}]}}'
printf '%s\n' "$result" > "$final"
printf '%s\n' '{"type":"turn.completed","structured_output":{"verdict":"pass","criterion":{"applicability":"applicable","rating":"correct","confidence":"high","summary":"Evidence supports the result.","evidence":[{"source":"metric","path":"evaluation/results.json","finding":"exact_match=1"}]}},"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}'
"""
    )
    reviewer.chmod(0o755)
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="model-review",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/model-review.json",
            reviewer=ProviderSettings(
                provider="codex",
                model="judge-model",
                effort="high",
            ),
            reviewer_executable=str(reviewer),
            required=True,
            max_attempts=1,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment_result = result["assessments"][0]
    assert assessment_result["status"] == "complete"
    assert assessment_result["method"]["kind"] == "reviewer"
    assert assessment_result["method"]["provider"] == "codex"
    assert assessment_result["method"]["model"] == "judge-model"
    assert assessment_result["method"]["effort"] == "high"
    assert assessment_result["method"]["executable"] == str(reviewer)
    assert assessment_result["usage"]["total_tokens"] == 5
    assert assessment_result["attempts"][0]["status"] == "complete"
    assert assessment_result["reports"] == [
        {
            "path": "evaluation/model-review.json",
            "media_type": "application/json",
            "title": "model-review assessment",
        }
    ]
    assert (
        json.loads((trial / "evaluation/model-review.json").read_text())[
            "verdict"
        ]
        == "pass"
    )


def test_provider_schema_inlines_only_referenced_common_definitions() -> None:
    resolved = _resolved_provider_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["criterion"],
            "properties": {
                "criterion": {
                    "$ref": f"{COMMON_SCHEMA}#/$defs/criterion"
                }
            },
        }
    )

    definitions = resolved["$defs"]
    assert "brunnerAssessmentCommon" not in definitions
    assert definitions["brunnerAssessmentCommon__criterion"]["type"] == (
        "object"
    )
    assert definitions["brunnerAssessmentCommon__evidence"]["type"] == "object"
    assert resolved["properties"]["criterion"]["$ref"] == (
        "#/$defs/brunnerAssessmentCommon__criterion"
    )


def test_codex_schema_preflight_rejects_typeless_definition_container() -> None:
    with pytest.raises(ProviderSchemaError, match="typeless schema container"):
        _preflight_provider_schema(
            "codex",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["result"],
                "properties": {
                    "result": {"$ref": "#/$defs/container"}
                },
                "$defs": {
                    "container": {
                        "$schema": (
                            "https://json-schema.org/draft/2020-12/schema"
                        ),
                        "$defs": {
                            "value": {"type": "string"},
                        },
                    }
                },
            },
        )


def test_codex_reviewer_preserves_local_defs_without_common_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    assessment_root.mkdir()
    (assessment_root / "prompt.md").write_text("Review the evidence.\n")
    (assessment_root / "review.schema.json").write_text(
        json.dumps(
            {
                "$schema": (
                    "https://json-schema.org/draft/2020-12/schema"
                ),
                "type": "object",
                "additionalProperties": False,
                "required": ["verdict"],
                "properties": {
                    "verdict": {"$ref": "#/$defs/localVerdict"}
                },
                "$defs": {
                    "localVerdict": {
                        "type": "string",
                        "enum": ["pass"],
                    }
                },
            }
        )
    )
    reviewer = tmp_path / "codex-reviewer"
    reviewer.write_text(
        f"""#!{sys.executable}
import json
import pathlib
import sys

arguments = sys.argv[1:]
schema_path = pathlib.Path(
    arguments[arguments.index("--output-schema") + 1]
)
schema = json.loads(schema_path.read_text())
assert set(schema["$defs"]) == {{"localVerdict"}}
assert "brunnerAssessmentCommon" not in json.dumps(schema)
final = pathlib.Path(
    arguments[arguments.index("--output-last-message") + 1]
)
result = {{"verdict": "pass"}}
final.write_text(json.dumps(result))
print(json.dumps({{
    "type": "turn.completed",
    "structured_output": result,
}}), flush=True)
"""
    )
    reviewer.chmod(0o755)
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="local-defs-review",
            root=assessment_root,
            prompt_path="prompt.md",
            output_schema_path="review.schema.json",
            output_path="evaluation/local-defs-review.json",
            reviewer=ProviderSettings(
                provider="codex",
                model="judge-model",
            ),
            reviewer_executable=str(reviewer),
            required=True,
            max_attempts=3,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment = result["assessments"][0]
    assert assessment["status"] == "complete"
    assert len(assessment["attempts"]) == 1


def test_codex_schema_preflight_fails_before_reviewer_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    assessment_root.mkdir()
    (assessment_root / "prompt.md").write_text("Review the evidence.\n")
    (assessment_root / "review.schema.json").write_text(
        json.dumps(
            {
                "$schema": (
                    "https://json-schema.org/draft/2020-12/schema"
                ),
                "$defs": {
                    "verdict": {
                        "type": "string",
                        "enum": ["pass"],
                    }
                },
            }
        )
    )
    marker = tmp_path / "reviewer-launched"
    reviewer = tmp_path / "codex-reviewer"
    reviewer.write_text(
        "#!/bin/sh\n"
        f"touch {marker}\n"
        "exit 1\n"
    )
    reviewer.chmod(0o755)
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="invalid-schema-review",
            root=assessment_root,
            prompt_path="prompt.md",
            output_schema_path="review.schema.json",
            output_path="evaluation/invalid-schema-review.json",
            reviewer=ProviderSettings(
                provider="codex",
                model="judge-model",
            ),
            reviewer_executable=str(reviewer),
            required=True,
            max_attempts=3,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment = result["assessments"][0]
    assert assessment["status"] == "failed"
    assert assessment["attempts"] == []
    assert assessment["error"]["type"] == "ProviderSchemaError"
    assert "root must have type 'object'" in assessment["error"]["message"]
    assert not marker.exists()


def test_provider_reviewer_retries_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    reviewer = tmp_path / "codex-reviewer"
    reviewer.write_text(
        """#!/bin/sh
set -eu
count_file="$HOME/attempt-count"
count=0
if [ -f "$count_file" ]; then count="$(cat "$count_file")"; fi
count=$((count + 1))
printf '%s' "$count" > "$count_file"
if [ "$count" -eq 1 ]; then
  printf '%s\n' '{"type":"turn.failed","error":"temporary failure"}'
  exit 1
fi
final=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--output-last-message" ]; then final="$argument"; fi
  previous="$argument"
done
result='{"verdict":"pass","criterion":{"applicability":"not_applicable","rating":"not_applicable","confidence":"high","summary":"","evidence":[]}}'
printf '%s\n' "$result" > "$final"
printf '%s\n' '{"type":"turn.completed","structured_output":{"verdict":"pass","criterion":{"applicability":"not_applicable","rating":"not_applicable","confidence":"high","summary":"","evidence":[]}}}'
"""
    )
    reviewer.chmod(0o755)
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="retry-review",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/retry-review.json",
            reviewer=ProviderSettings(
                provider="codex",
                model="judge-model",
            ),
            reviewer_executable=str(reviewer),
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    attempts = result["assessments"][0]["attempts"]
    assert [attempt["status"] for attempt in attempts] == [
        "failed",
        "complete",
    ]


def test_provider_reviewer_model_substitution_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    reviewer = tmp_path / "claude-reviewer"
    reviewer.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' '{"type":"assistant","message":{"model":"claude-opus-5","content":[]}}'
printf '%s\n' '{"type":"result","is_error":false,"result":{"verdict":"pass","criterion":{"applicability":"not_applicable","rating":"not_applicable","confidence":"high","summary":"","evidence":[]}}}'
"""
    )
    reviewer.chmod(0o755)
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="substituted-reviewer",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/substituted-reviewer.json",
            reviewer=ProviderSettings(
                provider="claude",
                model="claude-fable-5",
            ),
            reviewer_executable=str(reviewer),
            max_attempts=3,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment = result["assessments"][0]
    assert assessment["status"] == "failed"
    assert len(assessment["attempts"]) == 1
    assert assessment["attempts"][0]["model_mismatch"][
        "observed_model"
    ] == "claude-opus-5"
    assert "substituted model 'claude-opus-5'" in (
        assessment["error"]["message"]
    )
    assert not (trial / "evaluation/substituted-reviewer.json").exists()


def test_provider_reviewer_does_not_reuse_prior_attempt_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    reviewer = tmp_path / "codex-reviewer"
    reviewer.write_text(
        """#!/bin/sh
set -eu
count_file="$HOME/attempt-count"
count=0
if [ -f "$count_file" ]; then count="$(cat "$count_file")"; fi
count=$((count + 1))
printf '%s' "$count" > "$count_file"
final=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--output-last-message" ]; then final="$argument"; fi
  previous="$argument"
done
if [ "$count" -eq 1 ]; then
  result='{"verdict":"pass","criterion":{"applicability":"not_applicable","rating":"not_applicable","confidence":"high","summary":"","evidence":[]}}'
  printf '%s\n' "$result" > "$final"
  printf '%s\n' '{"type":"turn.failed","error":"temporary failure"}'
  exit 1
fi
printf '%s\n' '{"type":"turn.completed"}'
"""
    )
    reviewer.chmod(0o755)
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="stale-review",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/stale-review.json",
            reviewer=ProviderSettings(
                provider="codex",
                model="judge-model",
            ),
            reviewer_executable=str(reviewer),
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment = result["assessments"][0]
    assert assessment["status"] == "failed"
    assert len(assessment["attempts"]) == 2
    assert assessment["attempts"][1]["failure"] == (
        "reviewer returned no valid current structured output"
    )
    assert not (trial / "evaluation/stale-review.json").exists()


def test_provider_reviewer_launch_failure_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    missing_reviewer = tmp_path / "missing-reviewer"
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="missing-reviewer",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/missing-reviewer.json",
            reviewer=ProviderSettings(
                provider="codex",
                model="judge-model",
            ),
            reviewer_executable=str(missing_reviewer),
            max_attempts=3,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment = result["assessments"][0]
    assert assessment["status"] == "failed"
    assert len(assessment["attempts"]) == 1
    assert str(missing_reviewer) in assessment["attempts"][0]["launch_error"]
    assert "reviewer launch failed" in assessment["error"]["message"]


def test_assessment_contract_drift_fails_before_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    assess = assessment_root / "assess.py"
    assess.write_text(
        """
import json
import os
from pathlib import Path

Path(os.environ["BRUNNER_ASSESSMENT_OUTPUT"]).write_text(
    json.dumps({
        "verdict": "pass",
        "criterion": {
            "applicability": "not_applicable",
            "rating": "not_applicable",
            "confidence": "high",
            "summary": "",
            "evidence": [],
        },
    })
)
"""
    )
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="drift-review",
            root=assessment_root,
            prompt_path="prompt.md",
            rubric_paths=("rubric.md",),
            output_schema_path="review.schema.json",
            output_path="evaluation/drift-review.json",
            command=(sys.executable, str(assess)),
            required=True,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    assess.write_text(
        'raise RuntimeError("must not run after contract drift")\n'
    )
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment_result = result["assessments"][0]
    assert result["status"] == "complete"
    assert assessment_result["status"] == "failed"
    assert assessment_result["error"]["type"] == "IntegrityError"
    assert "contract changed" in assessment_result["error"]["message"]


def test_assessment_contract_normalizes_runtime_install_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first/assessment"
    second_root = tmp_path / "second/assessment"
    for assessment_root in (first_root, second_root):
        assessment_root.parent.mkdir()
        _write_assessment_materials(assessment_root)
        (assessment_root / "assess.py").write_text("print('assess')\n")
        (assessment_root / "render.py").write_text("print('render')\n")

    def assessment(
        root: Path,
        python: Path,
    ) -> AssessmentDefinition:
        return AssessmentDefinition(
            assessment_id="portable-review",
            root=root,
            prompt_path="prompt.md",
            output_schema_path="review.schema.json",
            output_path="evaluation/portable-review.json",
            command=(str(python), str(root / "assess.py")),
            render_command=(str(python), str(root / "render.py")),
            portable_command_paths=True,
        )

    first_python = tmp_path / "first/venv/bin/python"
    second_python = tmp_path / "second/venv/bin/python"
    monkeypatch.setattr(
        "brunner.definition.sys.executable",
        str(first_python),
    )
    first = assessment(first_root, first_python).contract_manifest()
    monkeypatch.setattr(
        "brunner.definition.sys.executable",
        str(second_python),
    )
    second_definition = assessment(second_root, second_python)
    second = second_definition.contract_manifest()

    assert first["contract_sha256"] == second["contract_sha256"]
    assert first["method"]["command"] == [
        "{python}",
        "{assessment_root}/assess.py",
    ]
    assert first["render_command"] == [
        "{python}",
        "{assessment_root}/render.py",
    ]
    assert first["portable_command_paths"] is True

    default_contract = AssessmentDefinition(
        assessment_id="author-controlled-review",
        root=second_root,
        prompt_path="prompt.md",
        output_schema_path="review.schema.json",
        output_path="evaluation/author-controlled-review.json",
        command=(str(second_python), str(second_root / "assess.py")),
    ).contract_manifest()
    assert default_contract["method"]["command"] == [
        str(second_python),
        str(second_root / "assess.py"),
    ]
    assert "portable_command_paths" not in default_contract

    (second_root / "render.py").write_text("print('changed')\n")
    changed = second_definition.contract_manifest()
    assert changed["contract_sha256"] != first["contract_sha256"]


def test_assessment_requires_exactly_one_execution_method(
    tmp_path: Path,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    assessment = AssessmentDefinition(
        assessment_id="invalid",
        root=assessment_root,
        prompt_path="prompt.md",
        output_schema_path="review.schema.json",
        output_path="evaluation/review.json",
    )

    with pytest.raises(ConfigurationError, match="exactly one"):
        assessment.validate()


def test_assessment_outputs_cannot_modify_candidate_workspace(
    tmp_path: Path,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    assessment = AssessmentDefinition(
        assessment_id="invalid-output",
        root=assessment_root,
        prompt_path="prompt.md",
        output_schema_path="review.schema.json",
        output_path="workspace/submission/review.json",
        command=(sys.executable, "-c", "pass"),
    )

    with pytest.raises(ConfigurationError, match="must be under"):
        assessment.validate()


def test_assessment_report_cannot_overwrite_evaluation_results(
    tmp_path: Path,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="invalid-report",
            root=assessment_root,
            prompt_path="prompt.md",
            output_schema_path="review.schema.json",
            output_path="evaluation/review.json",
            command=(sys.executable, "-c", "pass"),
            reports=(
                AssessmentReport(
                    path="evaluation/results.json",
                    media_type="application/json",
                ),
            ),
        ),
    )

    with pytest.raises(
        ConfigurationError,
        match="artifact paths cannot overwrite evaluation results",
    ):
        definition.validate()


def test_assessment_rejects_output_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _copy_benchmark(tmp_path)
    assessment_root = root / "assessment"
    _write_assessment_materials(assessment_root)
    assess = assessment_root / "assess.py"
    assess.write_text(
        """
raise RuntimeError("must not follow output symlink")
"""
    )
    definition = _definition(
        root,
        AssessmentDefinition(
            assessment_id="symlink-review",
            root=assessment_root,
            prompt_path="prompt.md",
            output_schema_path="review.schema.json",
            output_path="evaluation/symlink-review.json",
            command=(sys.executable, str(assess)),
            required=True,
        ),
    )
    trial, contract = _create_trial(tmp_path, definition)
    output = trial / "evaluation/symlink-review.json"
    output.symlink_to(trial / "evaluation/results.json")
    _pythonpath(monkeypatch)

    result = evaluate_trial(definition, contract, trial)

    assessment_result = result["assessments"][0]
    assert assessment_result["status"] == "failed"
    assert "contains a symlink" in assessment_result["error"]["message"]
