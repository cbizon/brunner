from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import traceback
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from brunner.backends.base import TrustedEvaluationSpec
from brunner import BRUNNER_RUNTIME_PROTOCOL
from brunner.contract import OutputContract, load_output_contract
from brunner.definition import BenchmarkDefinition
from brunner.errors import ContractError, EvaluationError, IntegrityError
from brunner.failure import failure_from_exception, failure_record
from brunner.io import write_json_atomic
from brunner.hashing import sha256_file
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


def evaluation_spec(
    definition: BenchmarkDefinition,
    contract: OutputContract,
) -> TrustedEvaluationSpec:
    reference = definition.reference
    evaluation = definition.evaluation
    reference_manifest_sha256 = None
    if reference is not None:
        manifest = reference.root / reference.manifest_path
        if not manifest.is_file():
            raise IntegrityError(
                f"reference manifest does not exist: {manifest}"
            )
        reference_manifest_sha256 = sha256_file(manifest)
    return TrustedEvaluationSpec(
        benchmark_id=definition.benchmark_id,
        benchmark_version=definition.version,
        contract_sha256=contract.sha256,
        image=evaluation.image,
        command=evaluation.command,
        results_path=evaluation.results_path,
        primary_report=evaluation.primary_report,
        timeout_seconds=evaluation.timeout_seconds,
        runtime_protocol=BRUNNER_RUNTIME_PROTOCOL,
        reference_manifest_path=(
            reference.manifest_path if reference is not None else None
        ),
        reference_manifest_sha256=reference_manifest_sha256,
        reference_validate_command=(
            reference.validate_command if reference is not None else ()
        ),
        cpu_request=evaluation.cpu_request,
        cpu_limit=evaluation.cpu_limit,
        memory_request=evaluation.memory_request,
        memory_limit=evaluation.memory_limit,
        ephemeral_storage_request=evaluation.ephemeral_storage_request,
        ephemeral_storage_limit=evaluation.ephemeral_storage_limit,
    )


def evaluation_spec_from_dict(value: dict[str, Any]) -> TrustedEvaluationSpec:
    if value.get("schema_version") != "2.0":
        raise EvaluationError("unsupported trusted evaluation specification")
    return TrustedEvaluationSpec(
        benchmark_id=str(value["benchmark_id"]),
        benchmark_version=str(value["benchmark_version"]),
        contract_sha256=str(value["contract_sha256"]),
        image=str(value.get("image") or "remote-evaluator"),
        command=tuple(str(item) for item in value["command"]),
        results_path=str(value["results_path"]),
        primary_report=(
            str(value["primary_report"])
            if value.get("primary_report") is not None
            else None
        ),
        timeout_seconds=float(value["timeout_seconds"]),
        runtime_protocol=str(value["runtime_protocol"]),
        reference_manifest_path=(
            str(value["reference_manifest_path"])
            if value.get("reference_manifest_path") is not None
            else None
        ),
        reference_manifest_sha256=(
            str(value["reference_manifest_sha256"])
            if value.get("reference_manifest_sha256") is not None
            else None
        ),
        reference_validate_command=tuple(
            str(item)
            for item in value.get("reference_validate_command", ())
        ),
    )


def _failure_result(
    spec: TrustedEvaluationSpec,
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
        "benchmark_id": spec.benchmark_id,
        "benchmark_version": spec.benchmark_version,
        "contract_sha256": spec.contract_sha256,
        "provider_status": provider_status,
        "evaluator_return_code": return_code,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def execute_evaluation(
    spec: TrustedEvaluationSpec,
    trial: Path,
    *,
    reference_root: Path | None = None,
    timeout_seconds: float | None = None,
    working_directory_root: Path | None = None,
) -> dict[str, Any]:
    """Execute deterministic evaluation inside the trusted environment."""
    spec.validate()
    trial = trial.resolve()
    contract = load_output_contract(
        trial / "workspace/schema/output-contract.json",
        expected_benchmark_id=spec.benchmark_id,
    )
    if contract.sha256 != spec.contract_sha256:
        raise ContractError(
            "staged output contract differs from evaluator contract"
        )
    evaluation_timeout = (
        spec.timeout_seconds
        if timeout_seconds is None
        else min(timeout_seconds, spec.timeout_seconds)
    )
    deadline = time.monotonic() + evaluation_timeout

    def remaining_seconds() -> float:
        return max(0.0, deadline - time.monotonic())

    results_path = trial / spec.results_path
    results_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = results_path.with_name("evaluator.stdout.log")
    stderr_path = results_path.with_name("evaluator.stderr.log")
    error_path = results_path.with_name("error.txt")
    results_path.unlink(missing_ok=True)
    error_path.unlink(missing_ok=True)
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
        if metadata.get("brunner_runtime_protocol") != spec.runtime_protocol:
            raise IntegrityError(
                "trial runtime protocol differs from evaluator runtime"
            )
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
        failure_context = {
            "operation": "evaluator_workspace_setup",
            "domain": "evaluation",
            "reason": "EvaluatorWorkspaceSetupFailed",
            "disposition": "attention",
            "resource": "evaluator_tmp",
        }
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONSAFEPATH": "1",
                "PYTHONNOUSERSITE": "1",
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
        temporary_root = (
            working_directory_root.resolve()
            if working_directory_root is not None
            else None
        )
        with tempfile.TemporaryDirectory(
            prefix="brunner-evaluation-",
            dir=temporary_root,
        ) as temporary_name:
            trusted_working_directory = Path(temporary_name).resolve()
            forbidden_roots = [trial]
            if reference_root is not None:
                forbidden_roots.append(reference_root.resolve())
            if any(
                trusted_working_directory.is_relative_to(root)
                for root in forbidden_roots
            ):
                failure_context = {
                    "operation": "evaluator_isolation_validation",
                    "domain": "integrity",
                    "reason": "EvaluatorIsolationInvalid",
                    "disposition": "attention",
                    "resource": "evaluator_tmp",
                }
                raise IntegrityError(
                    "trusted evaluator working directory overlaps an "
                    "untrusted or read-only benchmark mount"
                )
            if spec.reference_manifest_path is not None:
                if reference_root is None:
                    raise IntegrityError(
                        "trusted evaluation requires a mounted reference "
                        "bundle"
                    )
                failure_context = {
                    "operation": "reference_validation",
                    "domain": "integrity",
                    "reason": "ReferenceValidationFailed",
                    "disposition": "attention",
                }
                reference_root = reference_root.resolve()
                reference_manifest_path = (
                    reference_root / spec.reference_manifest_path
                )
                if (
                    sha256_file(reference_manifest_path)
                    != spec.reference_manifest_sha256
                ):
                    raise IntegrityError(
                        "mounted reference manifest digest does not match the "
                        "orchestrator-approved manifest"
                    )
                reference_manifest = validate_reference_manifest(
                    reference_root,
                    reference_manifest_path,
                )
                reference_metadata = reference_manifest.get("metadata")
                expected_reference_metadata = {
                    "benchmark_id": spec.benchmark_id,
                    "benchmark_version": spec.benchmark_version,
                    "contract_sha256": contract.sha256,
                }
                if not isinstance(reference_metadata, dict):
                    raise IntegrityError(
                        "reference bundle metadata is missing"
                    )
                mismatches = {
                    key: {
                        "expected": expected,
                        "actual": reference_metadata.get(key),
                    }
                    for key, expected in expected_reference_metadata.items()
                    if reference_metadata.get(key) != expected
                }
                if mismatches:
                    raise IntegrityError(
                        f"reference bundle identity mismatch: {mismatches}"
                    )
                environment["BRUNNER_REFERENCE_ROOT"] = str(reference_root)
                environment["BRUNNER_REFERENCE_MANIFEST"] = str(
                    reference_manifest_path.resolve()
                )
                if spec.reference_validate_command:
                    failure_context = {
                        "operation": "reference_validation_command",
                        "domain": "evaluation",
                        "reason": "ReferenceValidatorFailed",
                        "disposition": "attention",
                    }
                    reference_return_code = _run_evaluator(
                        spec.reference_validate_command,
                        cwd=trusted_working_directory,
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
            return_code = _run_evaluator(
                spec.command,
                cwd=trusted_working_directory,
                environment=environment,
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
        if (
            spec.primary_report is not None
            and not any(
                report.get("path") == spec.primary_report
                for report in reports
            )
        ):
            reports.append(
                {
                    "path": spec.primary_report,
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
        assert validated is not None
        result.update(
            {
                "benchmark_id": spec.benchmark_id,
                "benchmark_version": spec.benchmark_version,
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
                str(failure_context["resource"])
                if failure_context.get("resource") is not None
                else (
                    "evaluation_runtime"
                    if failure_context["domain"] == "evaluation"
                    else None
                )
            ),
        )
        result = _failure_result(
            spec,
            error=error,
            provider_status=provider_status,
            return_code=return_code,
            traceback_path=str(error_path.relative_to(trial)),
            failure=failure,
        )
        write_json_atomic(results_path, result)
    return result


def finalize_evaluation(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    trial: Path,
    *,
    output_trial: Path | None = None,
) -> dict[str, Any]:
    """Validate remote evaluation and run small post-collection assessments."""
    trial = trial.resolve()
    output_trial = (
        output_trial.resolve()
        if output_trial is not None
        else trial
    )
    output_trial.mkdir(parents=True, exist_ok=True)
    source_results_path = trial / definition.evaluation.results_path
    results_path = output_trial / definition.evaluation.results_path
    results_path.parent.mkdir(parents=True, exist_ok=True)
    if not source_results_path.is_file():
        raise EvaluationError(
            "Sterling evaluator result was not collected: "
            f"{source_results_path}"
        )
    result = _validate_evaluation_result(
        json.loads(source_results_path.read_text())
    )
    expected_identity = {
        "benchmark_id": definition.benchmark_id,
        "benchmark_version": definition.version,
        "contract_sha256": contract.sha256,
    }
    mismatch = {
        key: {"expected": expected, "actual": result.get(key)}
        for key, expected in expected_identity.items()
        if result.get(key) != expected
    }
    if mismatch:
        raise IntegrityError(
            f"Sterling evaluation identity mismatch: {mismatch}"
        )
    for report in result["reports"]:
        _safe_report_path(trial, str(report["path"]))

    from brunner.assessment import run_assessments

    assessment_index = run_assessments(
        definition,
        contract,
        trial,
        result,
        output_trial=output_trial,
    )
    result["assessment_status"] = assessment_index["status"]
    result["required_assessments_complete"] = assessment_index[
        "required_assessments_complete"
    ]
    result["assessments"] = assessment_index["assessments"]
    if "failure" in assessment_index:
        result["assessment_failure"] = assessment_index["failure"]
    write_json_atomic(results_path, result)

    if output_trial == trial:
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
        pass
    return result
