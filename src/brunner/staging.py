from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from brunner.contract import OutputContract, render_output_requirements
from brunner.definition import BenchmarkDefinition
from brunner.errors import ConfigurationError, IntegrityError
from brunner.hashing import sha256_tree
from brunner.io import write_json_atomic


DEFAULT_IGNORED_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}


@dataclass(frozen=True)
class StageReport:
    workspace: Path
    challenge_sha256: str
    contract_sha256: str
    benchmark_id: str
    benchmark_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace": str(self.workspace),
            "challenge_sha256": self.challenge_sha256,
            "contract_sha256": self.contract_sha256,
            "benchmark_id": self.benchmark_id,
            "benchmark_version": self.benchmark_version,
        }


def assert_isolated_workspace(
    workspace: Path,
    *,
    forbidden_names: tuple[str, ...] = (),
) -> None:
    forbidden = set(forbidden_names)
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise IntegrityError(
                f"isolated workspace contains a symlink: {path}"
            )
        if path.name in forbidden:
            raise IntegrityError(
                f"isolated workspace exposes forbidden name: {path}"
            )


def _copy_challenge(source: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(
        *DEFAULT_IGNORED_NAMES,
        "*.pyc",
        "*.pyo",
    )
    shutil.copytree(source, destination, ignore=ignored)


def stage_challenge(
    definition: BenchmarkDefinition,
    contract: OutputContract,
    destination: Path,
) -> StageReport:
    definition.validate()
    if contract.benchmark_id != definition.benchmark_id:
        raise ConfigurationError(
            "contract benchmark id differs from benchmark definition"
        )
    destination = destination.resolve()
    if destination.exists():
        if any(destination.iterdir()):
            raise FileExistsError(
                f"challenge destination is not empty: {destination}"
            )
        destination.rmdir()

    _copy_challenge(definition.challenge.root, destination)
    template_path = destination / definition.challenge.prompt_template
    template = template_path.read_text()
    marker = definition.challenge.output_marker
    if template.count(marker) != 1:
        raise ConfigurationError(
            "staged prompt template does not contain exactly one output marker"
        )
    rendered = template.replace(marker, render_output_requirements(contract))
    prompt_path = destination / definition.challenge.rendered_prompt
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    same_file = prompt_path.exists() and template_path.samefile(prompt_path)
    if same_file:
        temporary_prompt = destination / ".brunner-rendered-prompt.tmp"
        temporary_prompt.write_text(rendered)
        template_path.unlink()
        temporary_prompt.replace(prompt_path)
    else:
        prompt_path.write_text(rendered)
        template_path.unlink()

    schema_root = destination / "schema"
    schema_root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        schema_root / "output-contract.json",
        contract.data,
    )
    write_json_atomic(
        schema_root / "submission.schema.json",
        contract.submission_schema,
    )
    write_json_atomic(
        schema_root / "final-response.schema.json",
        contract.final_response_schema(),
    )
    artifact_schema_root = schema_root / "artifacts"
    for artifact in contract.data.get("artifacts", ()):
        if "json_schema" not in artifact:
            continue
        artifact_schema_root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            artifact_schema_root / f"{artifact['id']}.schema.json",
            artifact["json_schema"],
        )
    assert_isolated_workspace(
        destination,
        forbidden_names=definition.challenge.forbidden_names,
    )
    challenge_sha256 = sha256_tree(destination)
    report = StageReport(
        workspace=destination,
        challenge_sha256=challenge_sha256,
        contract_sha256=contract.sha256,
        benchmark_id=definition.benchmark_id,
        benchmark_version=definition.version,
    )
    write_json_atomic(
        destination / ".brunner-challenge.json",
        {
            "schema_version": "1.0",
            **report.to_dict(),
        },
    )
    return report


def load_stage_report(workspace: Path) -> StageReport:
    marker = workspace / ".brunner-challenge.json"
    value = json.loads(marker.read_text())
    return StageReport(
        workspace=workspace.resolve(),
        challenge_sha256=str(value["challenge_sha256"]),
        contract_sha256=str(value["contract_sha256"]),
        benchmark_id=str(value["benchmark_id"]),
        benchmark_version=str(value["benchmark_version"]),
    )
