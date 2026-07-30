from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brunner.definition import ArtifactPolicy
from brunner.errors import IntegrityError
from brunner.io import write_json_atomic


CHUNK_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactMetadata:
    type: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "size": self.size,
            "sha256": self.sha256,
        }


def artifact_metadata(path: Path) -> ArtifactMetadata | None:
    if path.is_symlink():
        target = os.readlink(os.fsencode(path))
        return ArtifactMetadata(
            type="symlink",
            size=len(target),
            sha256=hashlib.sha256(target).hexdigest(),
        )
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return ArtifactMetadata(
        type="file",
        size=path.stat().st_size,
        sha256=digest,
    )


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatch(name, pattern)
        or (
            pattern.startswith("**/")
            and fnmatch.fnmatch(name, pattern.removeprefix("**/"))
        )
        for pattern in patterns
    )


def collectable_artifact(
    name: str,
    policy: ArtifactPolicy,
    *,
    included_groups: frozenset[str] = frozenset(),
) -> bool:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    if _matches(name, policy.excluded_globs):
        return False
    for group, patterns in policy.groups.items():
        if _matches(name, patterns) and group not in included_groups:
            return False
    return True


def file_inventory(
    root: Path,
    policy: ArtifactPolicy,
    *,
    included_groups: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    inventory = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if not collectable_artifact(
            name,
            policy,
            included_groups=included_groups,
        ):
            continue
        if path.is_symlink() and not policy.allow_symlinks:
            raise IntegrityError(
                f"artifact policy rejects symlink: {path}"
            )
        metadata = artifact_metadata(path)
        if metadata is not None:
            inventory[name] = metadata.to_dict()
    return inventory


def inventory_difference(
    local: dict[str, dict[str, Any]],
    expected: dict[str, dict[str, Any]],
    *,
    limit: int = 5,
) -> str:
    local_names = set(local)
    expected_names = set(expected)
    categories = (
        ("missing", sorted(expected_names - local_names)),
        ("unexpected", sorted(local_names - expected_names)),
        (
            "mismatched",
            sorted(
                name
                for name in local_names & expected_names
                if local[name] != expected[name]
            ),
        ),
    )
    parts = []
    for label, names in categories:
        if not names:
            continue
        displayed = ", ".join(names[:limit])
        if len(names) > limit:
            displayed += f", and {len(names) - limit} more"
        parts.append(f"{label}: {displayed}")
    return "; ".join(parts) or "inventories differ"


def _copy_file_resumable(
    source: Path,
    destination: Path,
    expected: dict[str, Any],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected_size = int(expected["size"])
    if destination.exists() and destination.stat().st_size > expected_size:
        destination.unlink()
    offset = destination.stat().st_size if destination.exists() else 0
    mode = "ab" if offset else "wb"
    with source.open("rb") as input_stream, destination.open(mode) as output:
        input_stream.seek(offset)
        while offset < expected_size:
            data = input_stream.read(min(CHUNK_BYTES, expected_size - offset))
            if not data:
                raise IntegrityError(
                    f"artifact ended before expected size: {source}"
                )
            output.write(data)
            output.flush()
            offset += len(data)
    actual = artifact_metadata(destination)
    if actual is None or actual.to_dict() != expected:
        destination.unlink(missing_ok=True)
        raise IntegrityError(f"artifact checksum mismatch: {source}")


def prepare_partial_artifacts(
    destination: Path,
    inventory: dict[str, dict[str, Any]],
    policy: ArtifactPolicy,
    included_groups: frozenset[str],
) -> tuple[Path, set[str]]:
    partial = destination.with_name(destination.name + ".partial")
    partial.mkdir(parents=True, exist_ok=True)
    complete = set()
    for path in sorted(partial.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        name = path.relative_to(partial).as_posix()
        expected = inventory.get(name)
        if expected is None or not collectable_artifact(
            name,
            policy,
            included_groups=included_groups,
        ):
            path.unlink()
            continue
        actual = artifact_metadata(path)
        if actual is not None and actual.to_dict() == expected:
            complete.add(name)
            continue
        if (
            expected.get("type") == "file"
            and path.is_file()
            and 0 < path.stat().st_size < int(expected["size"])
        ):
            continue
        path.unlink()
    return partial, complete


def finalize_artifact_collection(
    partial: Path,
    destination: Path,
    inventory: dict[str, dict[str, Any]],
    policy: ArtifactPolicy,
    *,
    included_groups: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    observed = file_inventory(
        partial,
        policy,
        included_groups=included_groups,
    )
    if observed != inventory:
        raise IntegrityError(
            "artifact checksum verification failed: "
            + inventory_difference(observed, inventory)
        )
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    partial.rename(destination)
    inventory_path = destination.with_name(
        destination.name + "-inventory.json"
    )
    write_json_atomic(inventory_path, inventory)
    return {
        "result": destination,
        "inventory": inventory_path,
        "files": len(inventory),
    }


def collect_local_artifacts(
    source: Path,
    destination: Path,
    policy: ArtifactPolicy,
    *,
    included_groups: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    source = source.resolve()
    destination = destination.resolve()
    inventory = file_inventory(
        source,
        policy,
        included_groups=included_groups,
    )
    partial, complete = prepare_partial_artifacts(
        destination,
        inventory,
        policy,
        included_groups,
    )
    for name, expected in inventory.items():
        if name in complete:
            continue
        source_path = source / name
        target = partial / name
        if expected["type"] == "symlink":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            target.symlink_to(os.readlink(source_path))
        else:
            _copy_file_resumable(source_path, target, expected)
    return finalize_artifact_collection(
        partial,
        destination,
        inventory,
        policy,
        included_groups=included_groups,
    )
