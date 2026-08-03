from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brunner.errors import ConfigurationError
from brunner.hashing import sha256_file, sha256_tree
from brunner.providers.base import ProviderSettings
from brunner.timing import DEFAULT_MAX_ACTIVITY_INTERVAL_SECONDS


def _relative_path(value: str, *, field_name: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(
            f"{field_name} must be a safe relative path: {value!r}"
        )
    return path.as_posix()


def _contract_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _assessment_artifact_path(value: str, *, field_name: str) -> str:
    normalized = _relative_path(value, field_name=field_name)
    if Path(normalized).parts[0] not in {"assessments", "evaluation"}:
        raise ConfigurationError(
            f"{field_name} must be under evaluation/ or assessments/: "
            f"{value!r}"
        )
    return normalized


def _contract_command(
    command: tuple[str, ...],
    *,
    assessment_root: Path,
) -> list[str]:
    root = assessment_root.resolve()
    python = Path(sys.executable).resolve()
    normalized = []
    for argument in command:
        candidate = Path(argument)
        if not candidate.is_absolute():
            normalized.append(argument)
            continue
        resolved = candidate.resolve()
        if resolved == python:
            normalized.append("{python}")
        elif resolved.is_relative_to(root):
            relative = resolved.relative_to(root).as_posix()
            normalized.append(f"{{assessment_root}}/{relative}")
        else:
            normalized.append(argument)
    return normalized


@dataclass(frozen=True)
class ChallengeDefinition:
    root: Path
    prompt_template: str = "prompt.md"
    rendered_prompt: str = "PROMPT.md"
    output_marker: str = "{{BRUNNER_OUTPUT_CONTRACT}}"
    forbidden_names: tuple[str, ...] = ()
    materialize_command: tuple[str, ...] = ()
    materialize_timeout_seconds: float = 60 * 60

    def validate(self) -> None:
        if not self.root.is_dir():
            raise ConfigurationError(
                f"challenge root does not exist: {self.root}"
            )
        _relative_path(self.prompt_template, field_name="prompt_template")
        _relative_path(self.rendered_prompt, field_name="rendered_prompt")
        template = self.root / self.prompt_template
        if not template.is_file():
            raise ConfigurationError(f"prompt template does not exist: {template}")
        marker_count = template.read_text().count(self.output_marker)
        if marker_count != 1:
            raise ConfigurationError(
                "prompt template must contain the output marker exactly once"
            )
        if any(
            not isinstance(argument, str) or not argument.strip()
            for argument in self.materialize_command
        ):
            raise ConfigurationError(
                "challenge materialize_command arguments must be "
                "non-empty strings"
            )
        if self.materialize_timeout_seconds <= 0:
            raise ConfigurationError(
                "challenge materialize_timeout_seconds must be positive"
            )


@dataclass(frozen=True)
class EvaluationDefinition:
    command: tuple[str, ...]
    results_path: str = "evaluation/results.json"
    primary_report: str | None = None
    timeout_seconds: float = 12 * 60 * 60
    image: str | None = None
    container_runtime: str = "docker"

    def validate(self) -> None:
        if not self.command:
            raise ConfigurationError("evaluation command cannot be empty")
        _relative_path(self.results_path, field_name="evaluation results_path")
        if self.primary_report is not None:
            _relative_path(
                self.primary_report,
                field_name="evaluation primary_report",
            )
        if self.timeout_seconds <= 0:
            raise ConfigurationError("evaluation timeout must be positive")
        if self.image is not None and not self.image.strip():
            raise ConfigurationError("evaluation image cannot be empty")
        if not self.container_runtime.strip():
            raise ConfigurationError(
                "evaluation container runtime cannot be empty"
            )


@dataclass(frozen=True)
class AssessmentReport:
    path: str
    media_type: str
    title: str | None = None
    primary: bool = False

    def validate(self) -> None:
        _relative_path(self.path, field_name="assessment report path")
        if not self.media_type.strip():
            raise ConfigurationError(
                "assessment report media_type cannot be empty"
            )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "path": self.path,
            "media_type": self.media_type,
        }
        if self.title is not None:
            value["title"] = self.title
        if self.primary:
            value["primary"] = True
        return value


@dataclass(frozen=True)
class AssessmentDefinition:
    assessment_id: str
    root: Path
    prompt_path: str
    output_schema_path: str
    output_path: str
    rubric_paths: tuple[str, ...] = ()
    input_path: str | None = None
    command: tuple[str, ...] = ()
    reviewer: ProviderSettings | None = None
    reviewer_executable: str | None = None
    prepare_command: tuple[str, ...] = ()
    render_command: tuple[str, ...] = ()
    trial_evidence_paths: tuple[str, ...] = (
        "workspace",
        "transcript",
        "timing",
        "usage",
    )
    trusted_evidence_paths: tuple[str, ...] = ()
    reports: tuple[AssessmentReport, ...] = ()
    required: bool = False
    run_if_evaluation_failed: bool = False
    redact_candidate_identity: bool = True
    timeout_seconds: float = 60 * 60
    max_attempts: int = 3
    retry_initial_seconds: float = 10
    retry_max_seconds: float = 60
    portable_command_paths: bool = False

    @property
    def resolved_input_path(self) -> str:
        if self.input_path is not None:
            return self.input_path
        return f"assessments/{self.assessment_id}/review-input.json"

    def material_path(self, relative: str, *, field_name: str) -> Path:
        normalized = _relative_path(relative, field_name=field_name)
        path = (self.root / normalized).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ConfigurationError(
                f"{field_name} escapes assessment root: {relative!r}"
            )
        return path

    def validate(self) -> None:
        if not self.assessment_id.strip():
            raise ConfigurationError("assessment_id cannot be empty")
        if "/" in self.assessment_id or "\\" in self.assessment_id:
            raise ConfigurationError(
                "assessment_id cannot contain path separators"
            )
        if not self.root.is_dir():
            raise ConfigurationError(
                f"assessment root does not exist: {self.root}"
            )
        for relative, field_name in (
            (self.prompt_path, "assessment prompt_path"),
            (self.output_schema_path, "assessment output_schema_path"),
        ):
            path = self.material_path(relative, field_name=field_name)
            if not path.is_file():
                raise ConfigurationError(
                    f"{field_name} does not exist: {path}"
                )
        for relative in self.rubric_paths:
            path = self.material_path(
                relative,
                field_name="assessment rubric_path",
            )
            if not path.is_file():
                raise ConfigurationError(
                    f"assessment rubric does not exist: {path}"
                )
        for relative in self.trusted_evidence_paths:
            path = self.material_path(
                relative,
                field_name="assessment trusted_evidence_path",
            )
            if not path.exists():
                raise ConfigurationError(
                    f"trusted assessment evidence does not exist: {path}"
                )
        _assessment_artifact_path(
            self.resolved_input_path,
            field_name="assessment input_path",
        )
        _assessment_artifact_path(
            self.output_path,
            field_name="assessment output_path",
        )
        if self.resolved_input_path == self.output_path:
            raise ConfigurationError(
                "assessment input_path and output_path must differ"
            )
        for relative in self.trial_evidence_paths:
            _relative_path(
                relative,
                field_name="assessment trial_evidence_path",
            )
        if bool(self.command) == bool(self.reviewer):
            raise ConfigurationError(
                "assessment must define exactly one of command or reviewer"
            )
        if self.reviewer is not None:
            from brunner.providers import get_provider

            get_provider(self.reviewer.provider).validate_settings(
                self.reviewer
            )
        elif self.reviewer_executable is not None:
            raise ConfigurationError(
                "reviewer_executable requires reviewer settings"
            )
        if self.timeout_seconds <= 0:
            raise ConfigurationError(
                "assessment timeout must be positive"
            )
        if self.max_attempts < 1:
            raise ConfigurationError(
                "assessment max_attempts must be positive"
            )
        if self.retry_initial_seconds <= 0:
            raise ConfigurationError(
                "assessment retry_initial_seconds must be positive"
            )
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ConfigurationError(
                "assessment retry_max_seconds must not be shorter than "
                "retry_initial_seconds"
            )
        for report in self.reports:
            report.validate()
            _assessment_artifact_path(
                report.path,
                field_name="assessment report path",
            )

    def contract_manifest(self) -> dict[str, Any]:
        self.validate()

        def contract_command(command: tuple[str, ...]) -> list[str]:
            if not self.portable_command_paths:
                return list(command)
            return _contract_command(
                command,
                assessment_root=self.root,
            )

        materials = []
        recorded_paths = set()
        for role, relative in (
            ("prompt", self.prompt_path),
            ("output_schema", self.output_schema_path),
            *[("rubric", path) for path in self.rubric_paths],
        ):
            path = self.material_path(
                relative,
                field_name=f"assessment {role}",
            )
            materials.append(
                {
                    "role": role,
                    "path": relative,
                    "sha256": sha256_file(path),
                }
            )
            recorded_paths.add(path)
        for relative in self.trusted_evidence_paths:
            path = self.material_path(
                relative,
                field_name="assessment trusted_evidence_path",
            )
            materials.append(
                {
                    "role": "trusted_evidence",
                    "path": relative,
                    "sha256": (
                        sha256_file(path)
                        if path.is_file()
                        else sha256_tree(path)
                    ),
                }
            )
            recorded_paths.add(path)
        for role, command in (
            ("prepare_command", self.prepare_command),
            ("assessment_command", self.command),
            ("render_command", self.render_command),
        ):
            for argument in command:
                candidate = Path(argument)
                path = (
                    candidate
                    if candidate.is_absolute()
                    else self.root / candidate
                ).resolve()
                if (
                    path in recorded_paths
                    or not path.is_file()
                    or not path.is_relative_to(self.root.resolve())
                ):
                    continue
                materials.append(
                    {
                        "role": role,
                        "path": path.relative_to(
                            self.root.resolve()
                        ).as_posix(),
                        "sha256": sha256_file(path),
                    }
                )
                recorded_paths.add(path)
        method: dict[str, Any]
        if self.reviewer is not None:
            method = {
                "kind": "reviewer",
                "provider": self.reviewer.provider,
                "model": self.reviewer.model,
                "effort": self.reviewer.effort,
                "allowed_efforts": self.reviewer.allowed_efforts,
                "provider_id": self.reviewer.provider_id,
                "provider_name": self.reviewer.provider_name,
                "base_url": self.reviewer.base_url,
                "environment_key": self.reviewer.environment_key,
                "extra_environment_keys": sorted(
                    self.reviewer.extra_environment
                ),
                "executable": self.reviewer_executable,
            }
        else:
            method = {
                "kind": "command",
                "command": contract_command(self.command),
            }
        value = {
            "assessment_id": self.assessment_id,
            "materials": materials,
            "method": method,
            "output_path": self.output_path,
            "input_path": self.resolved_input_path,
            "prepare_command": contract_command(self.prepare_command),
            "render_command": contract_command(self.render_command),
            "trial_evidence_paths": list(self.trial_evidence_paths),
            "trusted_evidence_paths": list(
                self.trusted_evidence_paths
            ),
            "reports": [report.to_dict() for report in self.reports],
            "required": self.required,
            "run_if_evaluation_failed": self.run_if_evaluation_failed,
            "redact_candidate_identity": self.redact_candidate_identity,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "retry_initial_seconds": self.retry_initial_seconds,
            "retry_max_seconds": self.retry_max_seconds,
        }
        if self.portable_command_paths:
            value["portable_command_paths"] = True
        return {
            **value,
            "contract_sha256": _contract_digest(value),
        }


@dataclass(frozen=True)
class QualitativeReviewDefinition:
    reviewer: ProviderSettings
    reviewer_executable: str | None = None
    required: bool = False
    run_if_evaluation_failed: bool = True
    trial_evidence_paths: tuple[str, ...] = (
        "workspace",
        "transcript",
        "timing",
        "usage",
        "status.json",
    )
    timeout_seconds: float = 60 * 60
    max_attempts: int = 3
    retry_initial_seconds: float = 10
    retry_max_seconds: float = 60

    def to_assessment(self) -> AssessmentDefinition:
        root = Path(__file__).resolve().parent / "qualitative"
        return AssessmentDefinition(
            assessment_id="qualitative-review",
            root=root,
            prompt_path="reviewer-prompt.md",
            rubric_paths=("RUBRIC.md",),
            output_schema_path="qualitative-review.schema.json",
            input_path="evaluation/qualitative-review-input.json",
            output_path="evaluation/qualitative-review.json",
            reviewer=self.reviewer,
            reviewer_executable=self.reviewer_executable,
            render_command=(sys.executable, str(root / "render.py")),
            portable_command_paths=True,
            trial_evidence_paths=self.trial_evidence_paths,
            reports=(
                AssessmentReport(
                    path="evaluation/qualitative-review.html",
                    media_type="text/html",
                    title="Qualitative review",
                    primary=True,
                ),
            ),
            required=self.required,
            run_if_evaluation_failed=self.run_if_evaluation_failed,
            timeout_seconds=self.timeout_seconds,
            max_attempts=self.max_attempts,
            retry_initial_seconds=self.retry_initial_seconds,
            retry_max_seconds=self.retry_max_seconds,
        )


@dataclass(frozen=True)
class ReferenceDefinition:
    root: Path
    manifest_path: str = "manifest.json"
    validate_command: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.root.is_dir():
            raise ConfigurationError(
                f"reference root does not exist: {self.root}"
            )
        _relative_path(
            self.manifest_path,
            field_name="reference manifest_path",
        )


@dataclass(frozen=True)
class ArtifactPolicy:
    excluded_globs: tuple[str, ...] = (
        "**/.venv/**",
        "**/__pycache__/**",
        "**/.pytest_cache/**",
        "**/.mypy_cache/**",
        "**/*.pyc",
        "**/*.pyo",
        "provider-home/**",
    )
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    allow_symlinks: bool = False

    def validate(self) -> None:
        for name, globs in self.groups.items():
            if not name.strip():
                raise ConfigurationError("artifact group names cannot be empty")
            if not globs:
                raise ConfigurationError(
                    f"artifact group {name!r} must contain at least one glob"
                )


@dataclass(frozen=True)
class RuntimeDefaults:
    timeout_seconds: float = 48 * 60 * 60
    finalization_seconds: float = 30 * 60
    retry_initial_seconds: float = 30
    retry_max_seconds: float = 15 * 60
    provider_exit_grace_seconds: float = 60
    backend_shutdown_grace_seconds: float = 2 * 60
    max_attempts: int = 50
    max_activity_interval_seconds: float | None = (
        DEFAULT_MAX_ACTIVITY_INTERVAL_SECONDS
    )
    submission_poll_seconds: float = 2.0

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ConfigurationError("timeout must be positive")
        if not 0 < self.finalization_seconds < self.timeout_seconds:
            raise ConfigurationError(
                "finalization window must be positive and shorter than timeout"
            )
        if self.retry_initial_seconds <= 0:
            raise ConfigurationError("initial retry delay must be positive")
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ConfigurationError(
                "maximum retry delay must not be shorter than initial delay"
            )
        if self.provider_exit_grace_seconds < 0:
            raise ConfigurationError(
                "provider exit grace period cannot be negative"
            )
        if self.backend_shutdown_grace_seconds <= 0:
            raise ConfigurationError(
                "backend shutdown grace period must be positive"
            )
        if self.max_attempts < 1:
            raise ConfigurationError(
                "runtime max_attempts must be positive"
            )
        if (
            self.max_activity_interval_seconds is not None
            and self.max_activity_interval_seconds <= 0
        ):
            raise ConfigurationError(
                "max_activity_interval_seconds must be positive or None"
            )
        if self.submission_poll_seconds < 0:
            raise ConfigurationError(
                "submission_poll_seconds cannot be negative"
            )


@dataclass(frozen=True)
class BenchmarkDefinition:
    benchmark_id: str
    version: str
    root: Path
    contract_path: Path
    challenge: ChallengeDefinition
    evaluation: EvaluationDefinition
    assessments: tuple[AssessmentDefinition, ...] = ()
    reference: ReferenceDefinition | None = None
    artifacts: ArtifactPolicy = field(default_factory=ArtifactPolicy)
    runtime: RuntimeDefaults = field(default_factory=RuntimeDefaults)
    qualitative_review: QualitativeReviewDefinition | None = None

    def resolved_assessments(self) -> tuple[AssessmentDefinition, ...]:
        standard = (
            (self.qualitative_review.to_assessment(),)
            if self.qualitative_review is not None
            else ()
        )
        return (*standard, *self.assessments)

    def validate(self) -> None:
        if not self.benchmark_id.strip():
            raise ConfigurationError("benchmark_id cannot be empty")
        if not self.version.strip():
            raise ConfigurationError("benchmark version cannot be empty")
        if not self.root.is_dir():
            raise ConfigurationError(
                f"benchmark root does not exist: {self.root}"
            )
        if not self.contract_path.is_file():
            raise ConfigurationError(
                f"output contract does not exist: {self.contract_path}"
            )
        self.challenge.validate()
        self.evaluation.validate()
        resolved_assessments = self.resolved_assessments()
        assessment_ids = [
            assessment.assessment_id for assessment in resolved_assessments
        ]
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ConfigurationError(
                "assessment IDs must be unique within a benchmark"
            )
        artifact_owners: dict[str, str] = {}
        for assessment in resolved_assessments:
            assessment.validate()
            artifact_paths = {
                assessment.resolved_input_path,
                assessment.output_path,
                *(report.path for report in assessment.reports),
            }
            if self.evaluation.results_path in artifact_paths:
                raise ConfigurationError(
                    "assessment artifact paths cannot overwrite "
                    "evaluation results"
                )
            for path in artifact_paths:
                owner = artifact_owners.get(path)
                if owner is not None and owner != assessment.assessment_id:
                    raise ConfigurationError(
                        f"assessment artifact path {path!r} is used by "
                        f"both {owner!r} and {assessment.assessment_id!r}"
                    )
                artifact_owners[path] = assessment.assessment_id
        # Benchmark-owned assessments stay in the benchmark checkout. The
        # standard qualitative review is packaged with Brunner instead.
        for assessment in self.assessments:
            if not assessment.root.resolve().is_relative_to(
                self.root.resolve()
            ):
                raise ConfigurationError(
                    "assessment root must be within benchmark root: "
                    f"{assessment.root}"
                )
        self.artifacts.validate()
        self.runtime.validate()
        if self.reference is not None:
            self.reference.validate()
