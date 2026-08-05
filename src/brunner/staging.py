from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from brunner.contract import OutputContract, render_output_requirements
from brunner.definition import BenchmarkDefinition, ChallengeDefinition
from brunner.errors import (
    ChallengeMaterializationError,
    ConfigurationError,
    IntegrityError,
)
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
    if workspace.is_symlink():
        raise IntegrityError(
            f"isolated workspace is a symlink: {workspace}"
        )
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


def _diagnostic_text(value: str | bytes | None) -> str:
    if value is None:
        return "<empty>"
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = value
    text = text.rstrip()
    if not text:
        return "<empty>"
    limit = 20_000
    if len(text) > limit:
        return f"... <truncated {len(text) - limit} characters>\n{text[-limit:]}"
    return text


def _diagnostic_tail(stream: BinaryIO, *, limit: int = 20_000) -> bytes:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - limit))
    value = stream.read(limit)
    if size <= limit:
        return value
    prefix = f"... <truncated {size - limit} bytes>\n".encode()
    return prefix + value[-max(0, limit - len(prefix)) :]


def _materialization_failure(
    challenge: ChallengeDefinition,
    *,
    exit_code: int | None,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    timed_out: bool = False,
) -> ChallengeMaterializationError:
    command = shlex.join(challenge.materialize_command)
    status = (
        f"timed out after {challenge.materialize_timeout_seconds} seconds"
        if timed_out
        else "failed"
    )
    exit_detail = "unavailable" if exit_code is None else str(exit_code)
    return ChallengeMaterializationError(
        f"challenge materialization {status}\n"
        f"command: {command}\n"
        f"exit code: {exit_detail}\n"
        f"stdout:\n{_diagnostic_text(stdout)}\n"
        f"stderr:\n{_diagnostic_text(stderr)}"
    )


def _run_materializer(
    challenge: ChallengeDefinition,
    challenge_root: Path,
) -> None:
    challenge_root = challenge_root.resolve()
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("BRUNNER_")
    }
    resource_cache = os.environ.get("BRUNNER_RESOURCE_CACHE")
    if resource_cache is not None:
        environment["BRUNNER_RESOURCE_CACHE"] = resource_cache
    environment["BRUNNER_CHALLENGE_ROOT"] = str(challenge_root)
    with (
        tempfile.TemporaryFile() as stdout_stream,
        tempfile.TemporaryFile() as stderr_stream,
    ):
        try:
            process = subprocess.Popen(
                challenge.materialize_command,
                cwd=challenge_root,
                env=environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            raise ChallengeMaterializationError(
                "challenge materialization could not start\n"
                f"command: {shlex.join(challenge.materialize_command)}\n"
                "exit code: unavailable\n"
                "stdout:\n<empty>\n"
                f"stderr:\n{type(error).__name__}: {error}"
            ) from error
        try:
            process.wait(
                timeout=challenge.materialize_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise _materialization_failure(
                challenge,
                exit_code=None,
                stdout=_diagnostic_tail(stdout_stream),
                stderr=_diagnostic_tail(stderr_stream),
                timed_out=True,
            ) from error
        if process.returncode != 0:
            raise _materialization_failure(
                challenge,
                exit_code=process.returncode,
                stdout=_diagnostic_tail(stdout_stream),
                stderr=_diagnostic_tail(stderr_stream),
            )


@contextmanager
def _challenge_source(
    challenge: ChallengeDefinition,
) -> Iterator[Path]:
    assert_isolated_workspace(
        challenge.root,
        forbidden_names=challenge.forbidden_names,
    )
    if not challenge.materialize_command:
        yield challenge.root
        return
    with tempfile.TemporaryDirectory(
        prefix="brunner-challenge-"
    ) as temporary:
        challenge_root = Path(temporary) / "challenge"
        _copy_challenge(challenge.root, challenge_root)
        _run_materializer(challenge, challenge_root)
        assert_isolated_workspace(
            challenge_root,
            forbidden_names=challenge.forbidden_names,
        )
        yield challenge_root


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

    with _challenge_source(definition.challenge) as challenge_source:
        _copy_challenge(challenge_source, destination)
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
