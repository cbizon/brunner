from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
from pathlib import Path

from brunner.artifacts import artifact_metadata, file_inventory
from brunner.definition import ArtifactPolicy
from brunner.errors import IntegrityError
from brunner.runtime_protocol import runtime_identity
from brunner.submission import safe_child


def _policy(
    value: str,
) -> tuple[ArtifactPolicy, frozenset[str], tuple[str, ...] | None]:
    decoded = json.loads(base64.urlsafe_b64decode(value).decode())
    return (
        ArtifactPolicy(
            excluded_globs=tuple(decoded.get("excluded_globs", ())),
            groups={
                str(name): tuple(patterns)
                for name, patterns in decoded.get("groups", {}).items()
            },
            allow_symlinks=bool(decoded.get("allow_symlinks", False)),
            collect_evaluated_artifacts=bool(
                decoded.get("collect_evaluated_artifacts", False)
            ),
            max_collection_bytes=decoded.get(
                "max_collection_bytes",
                10 * 1024 * 1024 * 1024,
            ),
            failure_diagnostic_globs=tuple(
                decoded.get("failure_diagnostic_globs", ())
            ),
            max_diagnostic_collection_bytes=int(
                decoded.get(
                    "max_diagnostic_collection_bytes",
                    512 * 1024 * 1024,
                )
            ),
        ),
        frozenset(decoded.get("included_groups", ())),
        (
            tuple(decoded["included_globs"])
            if decoded.get("included_globs") is not None
            else None
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("root", type=Path)
    inventory.add_argument("policy")
    inventory.add_argument(
        "evaluation_results_path",
        nargs="?",
        default="evaluation/results.json",
    )
    read = subparsers.add_parser("read")
    read.add_argument("root", type=Path)
    read.add_argument("path")
    read.add_argument("offset", type=int)
    read.add_argument("count", type=int)
    subparsers.add_parser("protocol")
    clear = subparsers.add_parser("clear")
    clear.add_argument("root", type=Path)
    verify = subparsers.add_parser("verify-stage")
    verify.add_argument("root", type=Path)
    verify.add_argument("expected")
    return parser


def _clear_root(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise IntegrityError(f"remote trial root is unsafe: {root}")
    for path in root.iterdir():
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


def _workspace_inventory(workspace: Path) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    for path in sorted(workspace.rglob("*")):
        name = path.relative_to(workspace).as_posix()
        if name == ".brunner-challenge.json":
            continue
        if path.is_symlink():
            raise IntegrityError(
                f"staged workspace contains a symlink: {path}"
            )
        metadata = artifact_metadata(path)
        if metadata is not None:
            inventory[name] = metadata.to_dict()
    return inventory


def _verify_stage(root: Path, encoded: str) -> dict[str, object]:
    expected = json.loads(base64.urlsafe_b64decode(encoded).decode())
    if not isinstance(expected, dict):
        raise IntegrityError("expected stage report is not an object")
    workspace = root / "workspace"
    marker_path = workspace / ".brunner-challenge.json"
    if not marker_path.is_file() or marker_path.is_symlink():
        raise IntegrityError("staged challenge marker is missing or unsafe")
    marker = json.loads(marker_path.read_text())
    expected_inventory = expected.get("file_inventory")
    if not isinstance(expected_inventory, dict):
        raise IntegrityError("expected stage inventory is missing")
    observed_inventory = _workspace_inventory(workspace)
    if observed_inventory != expected_inventory:
        missing = sorted(set(expected_inventory) - set(observed_inventory))
        unexpected = sorted(set(observed_inventory) - set(expected_inventory))
        mismatched = sorted(
            name
            for name in set(expected_inventory) & set(observed_inventory)
            if expected_inventory[name] != observed_inventory[name]
        )
        raise IntegrityError(
            "remote staged challenge inventory mismatch: "
            f"missing={missing[:5]}, unexpected={unexpected[:5]}, "
            f"mismatched={mismatched[:5]}"
        )
    for key in (
        "benchmark_id",
        "benchmark_version",
        "contract_sha256",
        "challenge_sha256",
    ):
        if marker.get(key) != expected.get(key):
            raise IntegrityError(
                f"remote staged challenge {key} mismatch: "
                f"{marker.get(key)!r} != {expected.get(key)!r}"
            )
    return {
        "verified": True,
        "challenge_sha256": expected["challenge_sha256"],
        "files": len(observed_inventory),
    }


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "protocol":
        print(json.dumps(runtime_identity(), sort_keys=True))
        return 0
    root = args.root.resolve()
    if args.command == "clear":
        _clear_root(root)
        return 0
    if args.command == "verify-stage":
        print(json.dumps(_verify_stage(root, args.expected), sort_keys=True))
        return 0
    if args.command == "inventory":
        policy, groups, included_globs = _policy(args.policy)
        print(
            json.dumps(
                file_inventory(
                    root,
                    policy,
                    included_groups=groups,
                    evaluation_results_path=args.evaluation_results_path,
                    included_globs=included_globs,
                ),
                sort_keys=True,
            )
        )
        return 0
    path = safe_child(root, args.path, label="remote artifact")
    if args.offset < 0 or args.count <= 0:
        raise ValueError("remote read offset/count are invalid")
    with path.open("rb") as stream:
        stream.seek(args.offset)
        remaining = args.count
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            os.write(sys.stdout.fileno(), chunk)
            remaining -= len(chunk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
