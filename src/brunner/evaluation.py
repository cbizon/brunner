from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import traceback
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from brunner.contract import OutputContract
from brunner.definition import BenchmarkDefinition
from brunner.errors import ContractError, EvaluationError, IntegrityError
from brunner.failure import failure_from_exception, failure_record
from brunner.io import write_json_atomic
from brunner.reference import validate_reference_manifest
from brunner.submission import ValidatedSubmission, validate_submission


def _evaluation_schema() -> dict[str, Any]:
    path = files("brunner.schemas").joinpath(
        "evaluation-result.schema.json"
    )
    return json.loads(path.read_text())


def _validate_evaluation_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationError("evaluation result must be a JSON object")
    errors = sorted(
        Draft202012Validator(_evaluation_schema()).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: "
            f"{error.message}"
            for error in errors
        )
        raise EvaluationError(f"invalid evaluation result: {details}")
    return value


def _safe_report_path(trial: Path, relative: str) -> Path:
    path_value = Path(relative)
    if not relative or path_value.is_absolute() or ".." in path_value.parts:
        raise EvaluationError(
            f"evaluation report path must be relative: {relative!r}"
        )
    path = (trial / path_value).resolve()
    if not path.is_relative_to(trial.resolve()):
        raise EvaluationError(f"evaluation report escapes trial: {relative}")
    if not path.is_file():
        raise EvaluationError(f"evaluation report does not exist: {path}")
    return path


def _run_evaluator(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    stdout_path: Path,
    stderr_path: Path,
) -> int:
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise TimeoutError(
                f"evaluator exceeded {timeout_seconds} seconds"
            ) from error


def evaluator_invocation(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    trial: Path,
    environment: dict[str, str],
) -> tuple[tuple[str, ...], Path, dict[str, str]]:
    evaluation = definition.evaluation
    if evaluation.image is None:
        return (
            evaluation.command,
            trial / "workspace",
            environment,
        )

    container_trial = Path("/brunner/trial")
    container_environment = {
        "BRUNNER_TRIAL_ROOT": str(container_trial),
        "BRUNNER_WORKSPACE": str(container_trial / "workspace"),
        "BRUNNER_SUBMISSION_MANIFEST": str(
            container_trial
            / Path(environment["BRUNNER_SUBMISSION_MANIFEST"]).relative_to(
                trial
            )
        ),
        "BRUNNER_RUN_STATUS": str(
            container_trial
            / Path(environment["BRUNNER_RUN_STATUS"]).relative_to(trial)
        ),
        "BRUNNER_OUTPUT_CONTRACT": str(
            container_trial / "workspace/schema/output-contract.json"
        ),
        "BRUNNER_CONTRACT_SHA256": contract.sha256,
        "BRUNNER_EVALUATION_RESULTS": str(
            container_trial
            / Path(environment["BRUNNER_EVALUATION_RESULTS"]).relative_to(
                trial
            )
        ),
    }
    arguments = [
        evaluation.container_runtime,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid",
        "--mount",
        f"type=bind,src={trial},dst={container_trial}",
        "--workdir",
        str(container_trial / "workspace"),
    ]
    if definition.reference is not None:
        container_reference = Path("/brunner/reference")
        container_environment["BRUNNER_REFERENCE_ROOT"] = str(
            container_reference
        )
        container_environment["BRUNNER_REFERENCE_MANIFEST"] = str(
            container_reference / definition.reference.manifest_path
        )
        arguments.extend(
            (
                "--mount",
                "type=bind,src="
                f"{definition.reference.root.resolve()},"
                f"dst={container_reference},readonly",
            )
        )
    for key, value in sorted(container_environment.items()):
        arguments.extend(("--env", f"{key}={value}"))
    arguments.extend((evaluation.image, *evaluation.command))
    return tuple(arguments), trial, os.environ.copy()


def _failure_result(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    *,
    error: BaseException,
    provider_status: str | None,
    return_code: int | None,
    traceback_path: str,
    failure: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "failed",
        "summary": {},
        "metrics": {},
        "reports": [],
        "error": {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback_path,
        },
        "failure": failure,
        "benchmark_id": definition.benchmark_id,
        "benchmark_version": definition.version,
        "contract_sha256": contract.sha256,
        "provider_status": provider_status,
        "evaluator_return_code": return_code,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def evaluate_trial(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    trial: Path,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run trusted evaluation.

    ``timeout_seconds`` overrides the benchmark's own evaluation timeout. A
    campaign uses it to bound a wedged evaluator, which would otherwise block
    every other trial for the benchmark's full timeout.
    """
    trial = trial.resolve()
    evaluation_timeout = (
        definition.evaluation.timeout_seconds
        if timeout_seconds is None
        else min(timeout_seconds, definition.evaluation.timeout_seconds)
    )
    # One shared budget across reference validation, the evaluator, and every
    # assessment. Handing each step the full timeout would let a bounded
    # evaluation still run for a multiple of it.
    evaluation_deadline = time.monotonic() + evaluation_timeout

    def remaining_seconds() -> float:
        return max(0.0, evaluation_deadline - time.monotonic())
    results_path = trial / definition.evaluation.results_path
    results_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = results_path.with_name("evaluator.stdout.log")
    stderr_path = results_path.with_name("evaluator.stderr.log")
    error_path = results_path.with_name("error.txt")
    status_path = trial / "status.json"
    provider_status = None
    if status_path.is_file():
        provider_status = json.loads(status_path.read_text()).get("status")

    validated: ValidatedSubmission | None = None
    return_code = None
    failure_context = {
        "operation": "trial_metadata_validation",
        "domain": "integrity",
        "reason": "TrialMetadataInvalid",
        "disposition": "attention",
    }
    try:
        metadata = json.loads((trial / "metadata/manifest.json").read_text())
        if metadata.get("contract_sha256") != contract.sha256:
            raise ContractError(
                "trial contract digest differs from evaluator contract"
            )
        failure_context = {
            "operation": "submission_validation",
            "domain": "candidate",
            "reason": "CandidateSubmissionInvalid",
            "disposition": "candidate_failed",
        }
        validated = validate_submission(trial / "workspace", contract)
        environment = os.environ.copy()
        environment.update(
            {
                "BRUNNER_TRIAL_ROOT": str(trial),
                "BRUNNER_WORKSPACE": str(trial / "workspace"),
                "BRUNNER_SUBMISSION_MANIFEST": str(
                    validated.manifest_path
                ),
                "BRUNNER_RUN_STATUS": str(validated.run_status_path),
                "BRUNNER_OUTPUT_CONTRACT": str(contract.path),
                "BRUNNER_CONTRACT_SHA256": contract.sha256,
                "BRUNNER_EVALUATION_RESULTS": str(results_path),
            }
        )
        if definition.reference is not None:
            failure_context = {
                "operation": "reference_validation",
                "domain": "integrity",
                "reason": "ReferenceValidationFailed",
                "disposition": "attention",
            }
            reference_manifest_path = (
                definition.reference.root
                / definition.reference.manifest_path
            )
            reference_manifest = validate_reference_manifest(
                definition.reference.root,
                reference_manifest_path,
            )
            reference_metadata = reference_manifest.get("metadata", {})
            reference_contract = reference_metadata.get(
                "contract_sha256"
            )
            if (
                reference_contract is not None
                and reference_contract != contract.sha256
            ):
                raise IntegrityError(
                    "reference bundle contract digest does not match trial"
                )
            environment["BRUNNER_REFERENCE_ROOT"] = str(
                definition.reference.root.resolve()
            )
            environment["BRUNNER_REFERENCE_MANIFEST"] = str(
                reference_manifest_path.resolve()
            )
            if definition.reference.validate_command:
                failure_context = {
                    "operation": "reference_validation_command",
                    "domain": "evaluation",
                    "reason": "ReferenceValidatorFailed",
                    "disposition": "attention",
                }
                reference_return_code = _run_evaluator(
                    definition.reference.validate_command,
                    cwd=definition.reference.root,
                    environment=environment,
                    timeout_seconds=remaining_seconds(),
                    stdout_path=results_path.with_name(
                        "reference-validator.stdout.log"
                    ),
                    stderr_path=results_path.with_name(
                        "reference-validator.stderr.log"
                    ),
                )
                if reference_return_code != 0:
                    raise EvaluationError(
                        "reference validation command exited "
                        f"{reference_return_code}"
                    )
        failure_context = {
            "operation": "evaluator_execution",
            "domain": "evaluation",
            "reason": "EvaluatorFailed",
            "disposition": "attention",
        }
        command, cwd, process_environment = evaluator_invocation(
            definition,
            contract,
            trial,
            environment,
        )
        return_code = _run_evaluator(
            command,
            cwd=cwd,
            environment=process_environment,
            timeout_seconds=remaining_seconds(),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        if not results_path.is_file():
            raise EvaluationError(
                f"evaluator did not write required result: {results_path}"
            )
        result = _validate_evaluation_result(
            json.loads(results_path.read_text())
        )
        if return_code != 0 and result["status"] == "complete":
            raise EvaluationError(
                "evaluator exited nonzero but reported complete"
            )
        reports = list(result["reports"])
        if definition.evaluation.primary_report is not None:
            if not any(
                report.get("path") == definition.evaluation.primary_report
                for report in reports
            ):
                reports.append(
                    {
                        "path": definition.evaluation.primary_report,
                        "media_type": "text/html",
                        "title": "Primary benchmark report",
                        "primary": True,
                    }
                )
        for report in reports:
            _safe_report_path(trial, str(report["path"]))
        result["reports"] = reports
        if result["status"] == "failed" and "failure" not in result:
            result["failure"] = failure_record(
                operation="benchmark_evaluation",
                domain="candidate",
                reason="BenchmarkEvaluationFailed",
                message=str(
                    result.get("error")
                    or result.get("summary")
                    or "trusted evaluation reported failure"
                ),
                disposition="candidate_failed",
                retryable=False,
            )
        result.update(
            {
                "benchmark_id": definition.benchmark_id,
                "benchmark_version": definition.version,
                "contract_sha256": contract.sha256,
                "provider_status": provider_status,
                "evaluator_return_code": return_code,
                "evaluated_at": datetime.now(UTC).isoformat(),
                "submission": {
                    "manifest": str(
                        validated.manifest_path.relative_to(trial)
                    ),
                    "artifacts": [
                        {
                            **artifact.to_dict(),
                            "path": str(artifact.path.relative_to(trial)),
                        }
                        for artifact in validated.artifacts
                    ],
                },
            }
        )
        error_path.unlink(missing_ok=True)
        write_json_atomic(results_path, result)
    except Exception as error:
        error_path.write_text(traceback.format_exc())
        failure = failure_from_exception(
            error,
            operation=str(failure_context["operation"]),
            domain=str(failure_context["domain"]),
            reason=str(failure_context["reason"]),
            disposition=str(failure_context["disposition"]),
            retryable=False,
            resource=(
                "evaluation_runtime"
                if failure_context["domain"] == "evaluation"
                else None
            ),
        )
        result = _failure_result(
            definition,
            contract,
            error=error,
            provider_status=provider_status,
            return_code=return_code,
            traceback_path=str(error_path.relative_to(trial)),
            failure=failure,
        )
        write_json_atomic(results_path, result)
    from brunner.assessment import run_assessments

    assessment_index = run_assessments(
        definition,
        contract,
        trial,
        result,
        deadline_epoch=time.time() + remaining_seconds(),
    )
    result["assessment_status"] = assessment_index["status"]
    result["required_assessments_complete"] = assessment_index[
        "required_assessments_complete"
    ]
    result["assessments"] = assessment_index["assessments"]
    if "failure" in assessment_index:
        result["assessment_failure"] = assessment_index["failure"]
    write_json_atomic(results_path, result)
    from brunner.report import write_run_report

    try:
        report_path = write_run_report(
            trial,
            results_path.with_name("run-report.html"),
        )
    except Exception as error:
        result["report"] = {
            "status": "failed",
            "failure": failure_from_exception(
                error,
                operation="run_report",
                domain="reporting",
                reason="RunReportFailed",
                disposition="attention",
                retryable=False,
                resource="orchestrator_filesystem",
            ),
        }
    else:
        result["report"] = {
            "status": "complete",
            "path": str(report_path.relative_to(trial)),
        }
    try:
        write_json_atomic(results_path, result)
    except OSError:
        # The authoritative evaluation and assessment result was persisted
        # before presentation. Reporting metadata must not block cleanup.
        pass
    return result
