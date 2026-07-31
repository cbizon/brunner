from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from brunner.contract import OutputContract
from brunner.definition import AssessmentDefinition, BenchmarkDefinition
from brunner.errors import AssessmentError, IntegrityError
from brunner.hashing import sha256_file, sha256_tree
from brunner.io import load_json_object, write_json_atomic
from brunner.providers import ProviderRunContext, get_provider
from brunner.providers.base import response_from_record
from brunner.runner import run_attempt
from brunner.usage import read_json_records


IDENTITY_KEYS = frozenset(
    {
        "effort",
        "model",
        "provider",
        "provider_id",
        "provider_name",
    }
)
ASSESSMENT_RESULT_KEYS = frozenset(
    {
        "assessment_status",
        "assessments",
        "required_assessments_complete",
    }
)


class ReviewerAssessmentError(AssessmentError):
    def __init__(
        self,
        message: str,
        attempts: list[dict[str, Any]],
        usage: dict[str, Any] | None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.usage = usage


@dataclass(frozen=True)
class AssessmentInput:
    assessment_id: str
    trial_root: Path
    workspace: Path
    dossier_path: Path
    dossier: dict[str, Any]
    output_path: Path
    output_schema_path: Path
    output_schema: dict[str, Any]
    evaluation_results_path: Path
    benchmark_input_path: Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _packaged_schema(name: str) -> dict[str, Any]:
    path = files("brunner.schemas").joinpath(name)
    return json.loads(path.read_text())


def _required_environment_path(
    environment: dict[str, str],
    name: str,
) -> Path:
    value = environment.get(name)
    if not value:
        raise AssessmentError(
            f"missing assessment environment variable {name}"
        )
    return Path(value).resolve()


def load_assessment_input(
    environment: dict[str, str] | None = None,
) -> AssessmentInput:
    environment = environment or dict(os.environ)
    assessment_id = environment.get("BRUNNER_ASSESSMENT_ID")
    if not assessment_id:
        raise AssessmentError(
            "missing assessment environment variable "
            "BRUNNER_ASSESSMENT_ID"
        )
    dossier_path = _required_environment_path(
        environment,
        "BRUNNER_ASSESSMENT_INPUT",
    )
    dossier = load_json_object(dossier_path)
    if dossier.get("assessment_id") != assessment_id:
        raise IntegrityError(
            "assessment dossier identity does not match the environment"
        )
    output_schema_path = _required_environment_path(
        environment,
        "BRUNNER_ASSESSMENT_SCHEMA",
    )
    output_schema = load_json_object(output_schema_path)
    Draft202012Validator.check_schema(output_schema)
    return AssessmentInput(
        assessment_id=assessment_id,
        trial_root=_required_environment_path(
            environment,
            "BRUNNER_TRIAL_ROOT",
        ),
        workspace=_required_environment_path(
            environment,
            "BRUNNER_ASSESSMENT_WORKSPACE",
        ),
        dossier_path=dossier_path,
        dossier=dossier,
        output_path=_required_environment_path(
            environment,
            "BRUNNER_ASSESSMENT_OUTPUT",
        ),
        output_schema_path=output_schema_path,
        output_schema=output_schema,
        evaluation_results_path=_required_environment_path(
            environment,
            "BRUNNER_EVALUATION_RESULTS",
        ),
        benchmark_input_path=_required_environment_path(
            environment,
            "BRUNNER_ASSESSMENT_BENCHMARK_INPUT",
        ),
    )


def write_assessment_output(
    assessment_input: AssessmentInput,
    value: Any,
) -> None:
    _validate_output(assessment_input.output_schema, value)
    write_json_atomic(assessment_input.output_path, value)


def _schema_registry() -> Registry:
    common = _packaged_schema("assessment-common.schema.json")
    return Registry().with_resource(
        str(common["$id"]),
        Resource.from_contents(common),
    )


def _validation_errors(
    schema: dict[str, Any],
    value: Any,
) -> list[str]:
    validator = Draft202012Validator(
        schema,
        registry=_schema_registry(),
    )
    return [
        f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: "
        f"{error.message}"
        for error in sorted(
            validator.iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
    ]


def _validate_output(schema: dict[str, Any], value: Any) -> None:
    errors = _validation_errors(schema, value)
    if errors:
        raise AssessmentError(
            "invalid assessment output: " + "; ".join(errors)
        )


def _validate_result(value: dict[str, Any]) -> None:
    errors = _validation_errors(
        _packaged_schema("assessment-result.schema.json"),
        value,
    )
    if errors:
        raise AssessmentError(
            "invalid assessment result envelope: " + "; ".join(errors)
        )


def _safe_trial_path(
    trial: Path,
    relative: str,
    *,
    require_file: bool = False,
) -> Path:
    path_value = Path(relative)
    if not relative or path_value.is_absolute() or ".." in path_value.parts:
        raise AssessmentError(
            f"assessment path must be relative: {relative!r}"
        )
    unresolved = trial / path_value
    current = trial
    for part in path_value.parts:
        current = current / part
        if current.is_symlink():
            raise AssessmentError(
                f"assessment path contains a symlink: {relative}"
            )
    path = unresolved.resolve()
    if not path.is_relative_to(trial.resolve()):
        raise AssessmentError(f"assessment path escapes trial: {relative}")
    if require_file and not path.is_file():
        raise AssessmentError(f"assessment file does not exist: {path}")
    return path


def _timing_facts(trial: Path) -> dict[str, Any]:
    accounting_path = trial / "timing/accounting.json"
    if accounting_path.is_file():
        return load_json_object(accounting_path)
    goal_path = trial / "timing/goal.json"
    return {
        "schema_version": "1.0",
        "summary": (
            load_json_object(goal_path)
            if goal_path.is_file()
            else {}
        ),
        "limitations": [
            "Canonical timing accounting was not produced for this trial."
        ],
    }


def _deterministic_evaluation(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in ASSESSMENT_RESULT_KEYS
    }


def _redact_json(
    value: Any,
    *,
    candidate_identity: dict[str, Any],
) -> tuple[Any, int]:
    if isinstance(value, list):
        selected = []
        count = 0
        for item in value:
            redacted, item_count = _redact_json(
                item,
                candidate_identity=candidate_identity,
            )
            selected.append(redacted)
            count += item_count
        return selected, count
    if not isinstance(value, dict):
        return value, 0
    selected = {}
    count = 0
    for key, item in value.items():
        if (
            key.lower() in IDENTITY_KEYS
            and key.lower() in candidate_identity
            and item == candidate_identity[key.lower()]
        ):
            selected[key] = "<redacted>"
            count += 1
            continue
        redacted, item_count = _redact_json(
            item,
            candidate_identity=candidate_identity,
        )
        selected[key] = redacted
        count += item_count
    return selected, count


def _copy_file(
    source: Path,
    destination: Path,
    *,
    candidate_identity: dict[str, Any],
    redact_identity: bool,
) -> int:
    if source.is_symlink():
        raise AssessmentError(
            f"assessment evidence cannot contain symlinks: {source}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not redact_identity or source.suffix not in {".json", ".jsonl"}:
        shutil.copy2(source, destination)
        return 0
    if source.suffix == ".json":
        try:
            value = json.loads(source.read_text())
        except (UnicodeDecodeError, json.JSONDecodeError):
            shutil.copy2(source, destination)
            return 0
        redacted, count = _redact_json(
            value,
            candidate_identity=candidate_identity,
        )
        destination.write_text(
            json.dumps(redacted, indent=2, sort_keys=True) + "\n"
        )
        return count
    lines = []
    count = 0
    for line in source.read_text(errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        redacted, item_count = _redact_json(
            value,
            candidate_identity=candidate_identity,
        )
        lines.append(json.dumps(redacted, sort_keys=True))
        count += item_count
    destination.write_text("\n".join(lines) + ("\n" if lines else ""))
    return count


def _copy_path(
    source: Path,
    destination: Path,
    *,
    candidate_identity: dict[str, Any],
    redact_identity: bool,
) -> int:
    if source.is_symlink():
        raise AssessmentError(
            f"assessment evidence cannot contain symlinks: {source}"
        )
    if source.is_file():
        return _copy_file(
            source,
            destination,
            candidate_identity=candidate_identity,
            redact_identity=redact_identity,
        )
    destination.mkdir(parents=True, exist_ok=True)
    redacted = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise AssessmentError(
                f"assessment evidence cannot contain symlinks: {path}"
            )
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            redacted += _copy_file(
                path,
                target,
                candidate_identity=candidate_identity,
                redact_identity=redact_identity,
            )
    return redacted


def _path_record(path: Path, relative: Path) -> dict[str, Any]:
    if path.is_file():
        return {
            "path": relative.as_posix(),
            "type": "file",
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return {
        "path": relative.as_posix(),
        "type": "directory",
        "sha256": sha256_tree(path),
    }


def _run_process(
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
                f"assessment command exceeded {timeout_seconds} seconds"
            ) from error


def _run_prepare_command(
    assessment: AssessmentDefinition,
    *,
    trial: Path,
    work_root: Path,
    input_path: Path,
    benchmark_input_path: Path,
    environment: dict[str, str],
) -> dict[str, Any] | None:
    if not assessment.prepare_command:
        return None
    return_code = _run_process(
        assessment.prepare_command,
        cwd=assessment.root,
        environment=environment,
        timeout_seconds=assessment.timeout_seconds,
        stdout_path=work_root / "prepare.stdout.log",
        stderr_path=work_root / "prepare.stderr.log",
    )
    if return_code != 0:
        raise AssessmentError(
            f"assessment input builder exited {return_code}"
        )
    if not benchmark_input_path.is_file():
        raise AssessmentError(
            "assessment input builder did not write "
            f"{benchmark_input_path.relative_to(trial)}"
        )
    return load_json_object(benchmark_input_path)


def _resolved_provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    common = _packaged_schema("assessment-common.schema.json")
    common_id = str(common["$id"])

    def rewrite(value: Any) -> Any:
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        selected = {key: rewrite(item) for key, item in value.items()}
        reference = selected.get("$ref")
        if isinstance(reference, str) and reference.startswith(
            common_id + "#"
        ):
            suffix = reference.removeprefix(common_id + "#")
            selected["$ref"] = (
                "#/$defs/brunnerAssessmentCommon" + suffix
            )
        return selected

    selected = rewrite(schema)
    embedded_common = rewrite(common)

    def rewrite_embedded(value: Any) -> Any:
        if isinstance(value, list):
            return [rewrite_embedded(item) for item in value]
        if not isinstance(value, dict):
            return value
        rewritten = {
            key: rewrite_embedded(item) for key, item in value.items()
        }
        reference = rewritten.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            rewritten["$ref"] = (
                "#/$defs/brunnerAssessmentCommon"
                + reference.removeprefix("#")
            )
        return rewritten

    embedded_common = rewrite_embedded(embedded_common)
    embedded_common.pop("$id", None)
    definitions = dict(selected.get("$defs", {}))
    definitions["brunnerAssessmentCommon"] = embedded_common
    selected["$defs"] = definitions
    return selected


def _reviewer_prompt(
    assessment: AssessmentDefinition,
    workspace: Path,
) -> str:
    prompt = (workspace / "contract" / assessment.prompt_path).read_text()
    rubrics = "\n".join(
        f"- contract/{path}" for path in assessment.rubric_paths
    )
    return (
        f"{prompt.rstrip()}\n\n"
        "Brunner assessment inputs:\n"
        "- review-input.json\n"
        "- evidence/trial/ for candidate artifacts\n"
        "- evidence/trusted/ for benchmark-provided trusted evidence\n"
        f"{rubrics + chr(10) if rubrics else ''}"
        "- resolved-output.schema.json\n\n"
        "Inspect the supplied copies only. Return only JSON conforming to "
        "resolved-output.schema.json."
    )


def _response_candidates(
    observed: object,
    events_path: Path,
    final_output_path: Path,
) -> list[Any]:
    candidates = []
    if observed is not None:
        candidates.append(observed)
    for record in reversed(read_json_records(events_path)):
        response = response_from_record(record)
        if response is not None:
            candidates.append(response)
    if final_output_path.is_file():
        try:
            candidates.append(json.loads(final_output_path.read_text()))
        except json.JSONDecodeError:
            pass
    return candidates


def _run_reviewer(
    assessment: AssessmentDefinition,
    *,
    workspace: Path,
    work_root: Path,
    output_path: Path,
    schema: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    assert assessment.reviewer is not None
    adapter = get_provider(assessment.reviewer.provider)
    settings = adapter.validate_settings(assessment.reviewer)
    with tempfile.TemporaryDirectory(
        prefix=f"brunner-{assessment.assessment_id}-"
    ) as temporary:
        isolated_root = Path(temporary)
        isolated_workspace = isolated_root / "workspace"
        shutil.copytree(workspace, isolated_workspace)
        transcript = isolated_root / "reviewer"
        attempts_root = transcript / "attempts"
        provider_home = isolated_root / "provider-home"
        attempts_root.mkdir(parents=True)
        provider_home.mkdir()
        combined_events = transcript / "events.jsonl"
        combined_stderr = transcript / "stderr.log"
        resolved_schema_path = (
            isolated_workspace / "resolved-output.schema.json"
        )
        write_json_atomic(
            resolved_schema_path,
            _resolved_provider_schema(schema),
        )
        run_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("BRUNNER_")
        }
        run_environment.update(settings.extra_environment)
        run_environment.update(
            {
                "HOME": str(provider_home),
                "CODEX_HOME": str(provider_home / "codex"),
            }
        )
        Path(run_environment["CODEX_HOME"]).mkdir(parents=True)

        def persist_transcript() -> None:
            destination = work_root / "reviewer"
            if destination.exists():
                shutil.rmtree(destination)
            if transcript.exists():
                shutil.copytree(transcript, destination)

        deadline = time.time() + assessment.timeout_seconds
        retry_seconds = assessment.retry_initial_seconds
        attempts = []
        prompt = _reviewer_prompt(assessment, isolated_workspace)
        last_error = "reviewer did not return valid structured output"
        for number in range(1, assessment.max_attempts + 1):
            if time.time() >= deadline:
                last_error = (
                    "assessment reviewer exceeded "
                    f"{assessment.timeout_seconds} seconds"
                )
                break
            events = attempts_root / f"{number:04d}.events.jsonl"
            stderr = attempts_root / f"{number:04d}.stderr.log"
            final_output = attempts_root / f"{number:04d}.final.json"
            context = ProviderRunContext(
                workspace=isolated_workspace,
                transcript_dir=transcript,
                final_schema_path=resolved_schema_path,
                final_output_path=final_output,
                persist_session=False,
                resume_session=False,
                session_id=None,
                executable=assessment.reviewer_executable,
                read_only=True,
            )
            provider_command = adapter.build_command(settings, context)
            started_at = _now()

            def terminal_success_ready() -> bool:
                try:
                    candidates = _response_candidates(
                        None,
                        events,
                        final_output,
                    )
                except OSError:
                    return False
                return any(
                    not _validation_errors(schema, candidate)
                    for candidate in candidates
                )

            outcome = run_attempt(
                adapter=adapter,
                command=provider_command.command,
                workspace=isolated_workspace,
                environment={
                    **run_environment,
                    **provider_command.environment,
                },
                prompt=prompt,
                attempt_events=events,
                attempt_stderr=stderr,
                combined_events=combined_events,
                combined_stderr=combined_stderr,
                deadline_epoch=deadline,
                stop_requested=threading.Event(),
                terminal_exit_grace_seconds=5,
                terminal_success_ready=terminal_success_ready,
            )
            attempt: dict[str, Any] = {
                "number": number,
                "started_at": started_at,
                "ended_at": _now(),
                "events": (
                    f"reviewer/attempts/{events.name}"
                ),
                "stderr": (
                    f"reviewer/attempts/{stderr.name}"
                ),
                "final_output": (
                    f"reviewer/attempts/{final_output.name}"
                ),
            }
            attempt.update(
                {
                    key: value
                    for key, value in outcome.items()
                    if key != "observed_response"
                }
            )
            records = read_json_records(events)
            stderr_text = (
                stderr.read_text(errors="replace")
                if stderr.is_file()
                else ""
            )
            failure = adapter.classify_failure(records, stderr_text)
            if failure is not None:
                attempt["failure"] = failure.summary
                attempt["terminal"] = failure.terminal
                last_error = failure.summary
            launch_error = outcome.get("launch_error")
            if launch_error is not None:
                last_error = f"reviewer launch failed: {launch_error}"
                attempt["status"] = "failed"
                attempt["failure"] = last_error
                attempts.append(attempt)
                break
            if (
                int(outcome["return_code"]) == 0
                and outcome["terminal_result_succeeded"]
            ):
                validation_messages = []
                candidates = _response_candidates(
                    outcome.get("observed_response"),
                    events,
                    final_output,
                )
                for candidate in candidates:
                    errors = _validation_errors(schema, candidate)
                    if not errors:
                        write_json_atomic(output_path, candidate)
                        attempt["status"] = "complete"
                        attempts.append(attempt)
                        usage = None
                        try:
                            usage = adapter.parse_usage(
                                read_json_records(combined_events)
                            )
                        except ValueError:
                            pass
                        persist_transcript()
                        return attempts, usage
                    validation_messages.extend(errors)
                if not candidates:
                    last_error = (
                        "reviewer returned no valid current structured output"
                    )
                elif validation_messages:
                    last_error = (
                        "reviewer output failed schema validation: "
                        + "; ".join(validation_messages)
                    )
            elif int(outcome["return_code"]) == 0:
                last_error = (
                    "reviewer exited without a successful terminal event"
                )
            attempt["status"] = "failed"
            attempt["failure"] = last_error
            attempts.append(attempt)
            if failure is not None and failure.terminal:
                break
            if number < assessment.max_attempts:
                wait_seconds = min(
                    retry_seconds,
                    max(0.0, deadline - time.time()),
                )
                if wait_seconds:
                    time.sleep(wait_seconds)
                retry_seconds = min(
                    retry_seconds * 2,
                    assessment.retry_max_seconds,
                )
        usage = None
        try:
            usage = adapter.parse_usage(read_json_records(combined_events))
        except ValueError:
            pass
        persist_transcript()
        raise ReviewerAssessmentError(last_error, attempts, usage)


def _method_metadata(
    assessment: AssessmentDefinition,
) -> dict[str, Any]:
    if assessment.reviewer is not None:
        return {
            "kind": "reviewer",
            "provider": assessment.reviewer.provider,
            "model": assessment.reviewer.model,
            "effort": assessment.reviewer.effort,
            "provider_id": assessment.reviewer.provider_id,
            "provider_name": assessment.reviewer.provider_name,
            "base_url": assessment.reviewer.base_url,
            "environment_key": assessment.reviewer.environment_key,
            "extra_environment_keys": sorted(
                assessment.reviewer.extra_environment
            ),
            "executable": assessment.reviewer_executable,
        }
    return {
        "kind": "command",
        "command": list(assessment.command),
    }


def _write_result(
    path: Path,
    value: dict[str, Any],
) -> dict[str, Any]:
    _validate_result(value)
    write_json_atomic(path, value)
    return value


def _contract_from_trial(
    trial: Path,
    assessment: AssessmentDefinition,
) -> dict[str, Any]:
    metadata = load_json_object(trial / "metadata/manifest.json")
    expected = {
        item["assessment_id"]: item
        for item in metadata.get("assessment_contracts", [])
        if isinstance(item, dict) and "assessment_id" in item
    }.get(assessment.assessment_id)
    observed = assessment.contract_manifest()
    if expected is None:
        raise IntegrityError(
            f"trial has no recorded contract for assessment "
            f"{assessment.assessment_id!r}"
        )
    if expected.get("contract_sha256") != observed["contract_sha256"]:
        raise IntegrityError(
            f"assessment contract changed for "
            f"{assessment.assessment_id!r}"
        )
    return observed


def _prepare_workspace(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    assessment: AssessmentDefinition,
    trial: Path,
    evaluation: dict[str, Any],
) -> tuple[
    Path,
    Path,
    dict[str, Any],
    dict[str, Any],
]:
    work_root = trial / "assessments" / assessment.assessment_id
    workspace = work_root / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    contract_root = workspace / "contract"
    evidence_root = workspace / "evidence"
    candidate_metadata = load_json_object(
        trial / "metadata/manifest.json"
    )
    candidate_identity = {
        key: candidate_metadata.get(key) for key in IDENTITY_KEYS
    }
    material_records = []
    for role, relative in (
        ("prompt", assessment.prompt_path),
        ("output_schema", assessment.output_schema_path),
        *[("rubric", path) for path in assessment.rubric_paths],
    ):
        source = assessment.material_path(
            relative,
            field_name=f"assessment {role}",
        )
        destination = contract_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        material_records.append(
            {
                "role": role,
                "path": f"contract/{relative}",
                "sha256": sha256_file(destination),
            }
        )
    evidence = []
    redacted_fields = 0
    trial_evidence_paths = list(assessment.trial_evidence_paths)
    if definition.evaluation.results_path not in trial_evidence_paths:
        trial_evidence_paths.append(definition.evaluation.results_path)
    deterministic_evaluation = _deterministic_evaluation(evaluation)
    for relative in trial_evidence_paths:
        source = _safe_trial_path(trial, relative)
        record: dict[str, Any] = {
            "source": "trial",
            "original_path": relative,
        }
        if not source.exists():
            record["available"] = False
            evidence.append(record)
            continue
        destination = evidence_root / "trial" / relative
        if relative == definition.evaluation.results_path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            selected_evaluation = deterministic_evaluation
            if assessment.redact_candidate_identity:
                selected_evaluation, item_count = _redact_json(
                    deterministic_evaluation,
                    candidate_identity=candidate_identity,
                )
                redacted_fields += item_count
            write_json_atomic(destination, selected_evaluation)
        else:
            redacted_fields += _copy_path(
                source,
                destination,
                candidate_identity=candidate_identity,
                redact_identity=assessment.redact_candidate_identity,
            )
        record.update(
            {
                "available": True,
                **_path_record(
                    destination,
                    destination.relative_to(workspace),
                ),
            }
        )
        evidence.append(record)
    for relative in assessment.trusted_evidence_paths:
        source = assessment.material_path(
            relative,
            field_name="assessment trusted_evidence_path",
        )
        destination = evidence_root / "trusted" / relative
        _copy_path(
            source,
            destination,
            candidate_identity={},
            redact_identity=False,
        )
        evidence.append(
            {
                "source": "trusted",
                "original_path": relative,
                "available": True,
                **_path_record(
                    destination,
                    destination.relative_to(workspace),
                ),
            }
        )
    contract_manifest = _contract_from_trial(trial, assessment)
    identity_blinding = {
        "requested": assessment.redact_candidate_identity,
        "direct_identity_fields_redacted": redacted_fields,
        "candidate_identity_omitted_from_dossier": True,
        "identity_blinded": assessment.redact_candidate_identity,
        "limitations": [
            "Provider-specific transcript structure may still permit "
            "inference of the provider family.",
            "Free-text and binary evidence are not rewritten.",
        ],
    }
    dossier = {
        "schema_version": "1.0",
        "assessment_id": assessment.assessment_id,
        "benchmark": {
            "benchmark_id": definition.benchmark_id,
            "benchmark_version": definition.version,
            "contract_sha256": contract.sha256,
        },
        "trial": {
            "test_id": candidate_metadata["test_id"],
        },
        "assessment_contract": contract_manifest,
        "materials": material_records,
        "deterministic_evaluation": deterministic_evaluation,
        "timing": _timing_facts(trial),
        "evidence": evidence,
        "identity_blinding": identity_blinding,
    }
    return workspace, work_root, dossier, identity_blinding


def _assessment_environment(
    *,
    trial: Path,
    assessment: AssessmentDefinition,
    workspace: Path,
    input_path: Path,
    benchmark_input_path: Path,
    output_path: Path,
    result_path: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "BRUNNER_TRIAL_ROOT": str(trial),
            "BRUNNER_ASSESSMENT_ID": assessment.assessment_id,
            "BRUNNER_ASSESSMENT_WORKSPACE": str(workspace),
            "BRUNNER_ASSESSMENT_INPUT": str(input_path),
            "BRUNNER_ASSESSMENT_BENCHMARK_INPUT": str(
                benchmark_input_path
            ),
            "BRUNNER_ASSESSMENT_OUTPUT": str(output_path),
            "BRUNNER_ASSESSMENT_SCHEMA": str(
                assessment.material_path(
                    assessment.output_schema_path,
                    field_name="assessment output_schema_path",
                )
            ),
            "BRUNNER_ASSESSMENT_RESULT": str(result_path),
            "BRUNNER_EVALUATION_RESULTS": str(
                trial / "evaluation/results.json"
            ),
        }
    )
    return environment


def run_assessment(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    assessment: AssessmentDefinition,
    trial: Path,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    trial = trial.resolve()
    work_root = trial / "assessments" / assessment.assessment_id
    work_root.mkdir(parents=True, exist_ok=True)
    result_path = work_root / "result.json"
    started_at = _now()
    started = time.monotonic()
    method = _method_metadata(assessment)
    contract_manifest: dict[str, Any] = assessment.contract_manifest()
    input_record = None
    attempts: list[dict[str, Any]] = []
    usage = None
    identity_blinding = None
    if (
        evaluation.get("status") != "complete"
        and not assessment.run_if_evaluation_failed
    ):
        return _write_result(
            result_path,
            {
                "schema_version": "1.0",
                "assessment_id": assessment.assessment_id,
                "status": "skipped",
                "required": assessment.required,
                "method": method,
                "contract": contract_manifest,
                "input": None,
                "reports": [],
                "skip_reason": (
                    "deterministic evaluation did not complete"
                ),
                "started_at": started_at,
                "ended_at": _now(),
                "elapsed_seconds": time.monotonic() - started,
            },
        )
    try:
        workspace, work_root, dossier, identity_blinding = (
            _prepare_workspace(
                definition,
                contract,
                assessment,
                trial,
                evaluation,
            )
        )
        input_path = _safe_trial_path(
            trial,
            assessment.resolved_input_path,
        )
        output_path = _safe_trial_path(trial, assessment.output_path)
        benchmark_input_path = work_root / "benchmark-input.json"
        environment = _assessment_environment(
            trial=trial,
            assessment=assessment,
            workspace=workspace,
            input_path=input_path,
            benchmark_input_path=benchmark_input_path,
            output_path=output_path,
            result_path=result_path,
        )
        write_json_atomic(input_path, dossier)
        benchmark_input = _run_prepare_command(
            assessment,
            trial=trial,
            work_root=work_root,
            input_path=input_path,
            benchmark_input_path=benchmark_input_path,
            environment=environment,
        )
        if benchmark_input is not None:
            dossier["benchmark_input"] = benchmark_input
            write_json_atomic(input_path, dossier)
        shutil.copy2(input_path, workspace / "review-input.json")
        input_record = {
            "path": str(input_path.relative_to(trial)),
            "sha256": sha256_file(input_path),
        }
        schema = json.loads(
            assessment.material_path(
                assessment.output_schema_path,
                field_name="assessment output_schema_path",
            ).read_text()
        )
        Draft202012Validator.check_schema(schema)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if assessment.reviewer is not None:
            try:
                attempts, usage = _run_reviewer(
                    assessment,
                    workspace=workspace,
                    work_root=work_root,
                    output_path=output_path,
                    schema=schema,
                )
            except ReviewerAssessmentError as error:
                attempts = error.attempts
                usage = error.usage
                raise AssessmentError(str(error)) from error
        else:
            return_code = _run_process(
                assessment.command,
                cwd=assessment.root,
                environment=environment,
                timeout_seconds=assessment.timeout_seconds,
                stdout_path=work_root / "assessment.stdout.log",
                stderr_path=work_root / "assessment.stderr.log",
            )
            attempts = [
                {
                    "number": 1,
                    "return_code": return_code,
                    "status": (
                        "complete" if return_code == 0 else "failed"
                    ),
                }
            ]
            if return_code != 0:
                raise AssessmentError(
                    f"assessment command exited {return_code}"
                )
        if not output_path.is_file():
            raise AssessmentError(
                f"assessment did not write required output: {output_path}"
            )
        value = json.loads(output_path.read_text())
        _validate_output(schema, value)
        write_json_atomic(output_path, value)
        if assessment.render_command:
            render_return_code = _run_process(
                assessment.render_command,
                cwd=assessment.root,
                environment=environment,
                timeout_seconds=assessment.timeout_seconds,
                stdout_path=work_root / "render.stdout.log",
                stderr_path=work_root / "render.stderr.log",
            )
            if render_return_code != 0:
                raise AssessmentError(
                    f"assessment renderer exited {render_return_code}"
                )
        reports = [
            {
                "path": str(output_path.relative_to(trial)),
                "media_type": "application/json",
                "title": f"{assessment.assessment_id} assessment",
            }
        ]
        for report in assessment.reports:
            _safe_trial_path(
                trial,
                report.path,
                require_file=True,
            )
            if report.path not in {
                item["path"] for item in reports
            }:
                reports.append(report.to_dict())
        result = {
            "schema_version": "1.0",
            "assessment_id": assessment.assessment_id,
            "status": "complete",
            "required": assessment.required,
            "method": method,
            "contract": contract_manifest,
            "input": input_record,
            "output": {
                "path": str(output_path.relative_to(trial)),
                "sha256": sha256_file(output_path),
            },
            "reports": reports,
            "attempts": attempts,
            "identity_blinding": identity_blinding,
            "started_at": started_at,
            "ended_at": _now(),
            "elapsed_seconds": time.monotonic() - started,
        }
        if usage is not None:
            result["usage"] = usage
        return _write_result(result_path, result)
    except (
        AssessmentError,
        IntegrityError,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        subprocess.SubprocessError,
        TimeoutError,
        ValueError,
    ) as error:
        traceback_path = work_root / "error.txt"
        traceback_path.write_text(traceback.format_exc())
        result = {
            "schema_version": "1.0",
            "assessment_id": assessment.assessment_id,
            "status": "failed",
            "required": assessment.required,
            "method": method,
            "contract": contract_manifest,
            "input": input_record,
            "reports": [],
            "attempts": attempts,
            "started_at": started_at,
            "ended_at": _now(),
            "elapsed_seconds": time.monotonic() - started,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": str(traceback_path.relative_to(trial)),
            },
        }
        if usage is not None:
            result["usage"] = usage
        if identity_blinding is not None:
            result["identity_blinding"] = identity_blinding
        return _write_result(result_path, result)


def run_assessments(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    trial: Path,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    results = [
        run_assessment(
            definition,
            contract,
            assessment,
            trial,
            evaluation,
        )
        for assessment in definition.assessments
    ]
    required_complete = all(
        result["status"] == "complete"
        for result in results
        if result["required"]
    )
    if not results:
        status = "not_configured"
    elif not required_complete:
        status = "failed"
    elif all(result["status"] == "complete" for result in results):
        status = "complete"
    elif all(result["status"] == "skipped" for result in results):
        status = "skipped"
    else:
        status = "partial"
    index = {
        "schema_version": "1.0",
        "status": status,
        "required_assessments_complete": required_complete,
        "assessments": results,
    }
    if results:
        write_json_atomic(trial / "assessments/index.json", index)
    return index
