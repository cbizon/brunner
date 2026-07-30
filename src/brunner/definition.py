from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from brunner.errors import ConfigurationError


def _relative_path(value: str, *, field_name: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(
            f"{field_name} must be a safe relative path: {value!r}"
        )
    return path.as_posix()


@dataclass(frozen=True)
class ChallengeDefinition:
    root: Path
    prompt_template: str = "prompt.md"
    rendered_prompt: str = "PROMPT.md"
    output_marker: str = "{{BRUNNER_OUTPUT_CONTRACT}}"
    forbidden_names: tuple[str, ...] = ()

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


@dataclass(frozen=True)
class BenchmarkDefinition:
    benchmark_id: str
    version: str
    root: Path
    contract_path: Path
    challenge: ChallengeDefinition
    evaluation: EvaluationDefinition
    reference: ReferenceDefinition | None = None
    artifacts: ArtifactPolicy = field(default_factory=ArtifactPolicy)
    runtime: RuntimeDefaults = field(default_factory=RuntimeDefaults)

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
        self.artifacts.validate()
        self.runtime.validate()
        if self.reference is not None:
            self.reference.validate()
