from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha256_tree(root: Path) -> str:
    digest, _ = _sha256_tree(root, collect_inventory=False)
    return digest


def sha256_tree_inventory(
    root: Path,
) -> tuple[str, dict[str, dict[str, Any]]]:
    digest, inventory = _sha256_tree(root, collect_inventory=True)
    assert inventory is not None
    return digest, inventory


def _sha256_tree(
    root: Path,
    *,
    collect_inventory: bool,
) -> tuple[str, dict[str, dict[str, Any]] | None]:
    digest = hashlib.sha256()
    inventory: dict[str, dict[str, Any]] | None = (
        {} if collect_inventory else None
    )
    for path in sorted(root.rglob("*")):
        relative_name = path.relative_to(root).as_posix()
        relative = relative_name.encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if path.is_symlink():
            target = os.readlink(os.fsencode(path))
            digest.update(b"L")
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
            if inventory is not None:
                inventory[relative_name] = {
                    "type": "symlink",
                    "size": len(target),
                    "sha256": hashlib.sha256(target).hexdigest(),
                }
        elif path.is_file():
            digest.update(b"F")
            file_digest = hashlib.sha256() if inventory is not None else None
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    if file_digest is not None:
                        file_digest.update(chunk)
            if inventory is not None:
                assert file_digest is not None
                inventory[relative_name] = {
                    "type": "file",
                    "size": path.stat().st_size,
                    "sha256": file_digest.hexdigest(),
                }
        elif path.is_dir():
            digest.update(b"D")
    return digest.hexdigest(), inventory
