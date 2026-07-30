from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from brunner.definition import ArtifactPolicy
from brunner.errors import IntegrityError
from brunner.artifacts import file_inventory, inventory_difference
from brunner.hashing import sha256_bytes
from brunner.io import write_json_atomic


REFERENCE_POLICY = ArtifactPolicy(
    excluded_globs=(
        "**/__pycache__/**",
        "**/*.pyc",
        "**/.brunner-reference.json",
    )
)


def _reference_inventory(
    reference_root: Path,
    manifest_path: Path,
) -> dict[str, dict[str, Any]]:
    inventory = file_inventory(reference_root, REFERENCE_POLICY)
    try:
        manifest_name = manifest_path.resolve().relative_to(
            reference_root.resolve()
        ).as_posix()
    except ValueError as error:
        raise IntegrityError(
            "reference manifest must be inside the reference root"
        ) from error
    inventory.pop(manifest_name, None)
    return inventory


def build_reference_manifest(
    reference_root: Path,
    output: Path,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inventory = _reference_inventory(reference_root, output)
    value = {
        "schema_version": "1.0",
        "files": inventory,
        "metadata": metadata or {},
    }
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    value["sha256"] = sha256_bytes(encoded)
    write_json_atomic(output, value)
    return value


def validate_reference_manifest(
    reference_root: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    value = json.loads(manifest_path.read_text())
    expected = value.get("files")
    if not isinstance(expected, dict):
        raise IntegrityError("reference manifest files must be an object")
    observed = _reference_inventory(reference_root, manifest_path)
    if observed != expected:
        raise IntegrityError(
            "reference inventory mismatch: "
            + inventory_difference(observed, expected)
        )
    digest_value = {
        key: item for key, item in value.items() if key != "sha256"
    }
    encoded = json.dumps(
        digest_value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = sha256_bytes(encoded)
    if value.get("sha256") != digest:
        raise IntegrityError("reference manifest digest mismatch")
    return value
