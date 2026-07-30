from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from brunner.contract import (
    OutputContract,
    validate_final_response,
    validate_json,
)
from brunner.errors import ContractError
from brunner.hashing import sha256_file


@dataclass(frozen=True)
class ValidatedArtifact:
    artifact_id: str
    path: Path
    size: int
    sha256: str
    media_type: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }


@dataclass(frozen=True)
class ValidatedSubmission:
    manifest_path: Path
    manifest: dict[str, Any]
    run_status_path: Path
    run_status: dict[str, Any]
    artifacts: tuple[ValidatedArtifact, ...]


def safe_child(root: Path, value: str, *, label: str) -> Path:
    if not value:
        raise ContractError(f"{label} path cannot be empty")
    root = root.resolve()
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ContractError(f"{label} path escapes its root: {value}")
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ContractError(f"{label} cannot use a symlink: {current}")
    path = candidate.resolve()
    if not path.is_relative_to(root):
        raise ContractError(f"{label} path escapes its root: {value}")
    if not path.is_file():
        raise ContractError(f"{label} file does not exist: {path}")
    return path



def _pointer_values(value: Any, pointer: str) -> Iterable[Any]:
    parts = pointer.removeprefix("/").split("/")

    def visit(current: Any, index: int) -> Iterable[Any]:
        if index == len(parts):
            yield current
            return
        part = parts[index].replace("~1", "/").replace("~0", "~")
        if part == "*":
            if isinstance(current, dict):
                children = current.values()
            elif isinstance(current, list):
                children = current
            else:
                return
            for child in children:
                yield from visit(child, index + 1)
            return
        if isinstance(current, dict) and part in current:
            yield from visit(current[part], index + 1)
        elif isinstance(current, list):
            try:
                child = current[int(part)]
            except (ValueError, IndexError):
                return
            yield from visit(child, index + 1)

    yield from visit(value, 0)


def _artifact_paths(
    artifact: dict[str, Any],
    manifest: dict[str, Any],
    workspace: Path,
    manifest_root: Path,
) -> list[Path]:
    if "path" in artifact:
        try:
            return [
                safe_child(
                    workspace,
                    artifact["path"],
                    label=f"artifact {artifact['id']!r}",
                )
            ]
        except ContractError:
            if artifact.get("required", True):
                raise
            return []

    values = list(_pointer_values(manifest, artifact["manifest_pointer"]))
    if not values:
        if artifact.get("required", True):
            raise ContractError(
                f"artifact {artifact['id']!r} manifest pointer matched no values: "
                f"{artifact['manifest_pointer']}"
            )
        return []
    paths = []
    for value in values:
        if not isinstance(value, str):
            raise ContractError(
                f"artifact {artifact['id']!r} manifest path is not a string"
            )
        paths.append(
            safe_child(
                manifest_root,
                value,
                label=f"artifact {artifact['id']!r}",
            )
        )
    return paths


def validate_submission(
    workspace: Path,
    contract: OutputContract,
) -> ValidatedSubmission:
    workspace = workspace.resolve()
    manifest_path = safe_child(
        workspace,
        contract.submission_manifest,
        label="submission manifest",
    )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as error:
        raise ContractError(
            f"submission manifest is not valid JSON: {error}"
        ) from error
    validate_json(
        manifest,
        contract.submission_schema,
        label="submission manifest",
    )

    run_status_path = safe_child(
        workspace,
        contract.run_status_path,
        label="run status",
    )
    try:
        run_status = json.loads(run_status_path.read_text())
    except json.JSONDecodeError as error:
        raise ContractError(f"run status is not valid JSON: {error}") from error
    validate_final_response(
        run_status,
        contract,
        label="run status",
    )

    artifacts = []
    for artifact in contract.data.get("artifacts", ()):
        for path in _artifact_paths(
            artifact,
            manifest,
            workspace,
            manifest_path.parent,
        ):
            size = path.stat().st_size
            minimum = artifact.get("minimum_bytes")
            maximum = artifact.get("maximum_bytes")
            if minimum is not None and size < minimum:
                raise ContractError(
                    f"artifact {artifact['id']!r} is {size} bytes; "
                    f"minimum is {minimum}"
                )
            if maximum is not None and size > maximum:
                raise ContractError(
                    f"artifact {artifact['id']!r} is {size} bytes; "
                    f"maximum is {maximum}"
                )
            if "json_schema" in artifact:
                try:
                    artifact_value = json.loads(path.read_text())
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ContractError(
                        f"artifact {artifact['id']!r} is not valid JSON: "
                        f"{error}"
                    ) from error
                validate_json(
                    artifact_value,
                    artifact["json_schema"],
                    label=f"artifact {artifact['id']!r}",
                )
            artifacts.append(
                ValidatedArtifact(
                    artifact_id=str(artifact["id"]),
                    path=path,
                    size=size,
                    sha256=sha256_file(path),
                    media_type=artifact.get("media_type"),
                )
            )
    return ValidatedSubmission(
        manifest_path=manifest_path,
        manifest=manifest,
        run_status_path=run_status_path,
        run_status=run_status,
        artifacts=tuple(artifacts),
    )
