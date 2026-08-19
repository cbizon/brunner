from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from brunner.artifacts import (
    artifact_metadata,
    enforce_inventory_size,
    file_inventory,
    finalize_artifact_collection,
    prepare_partial_artifacts,
)
from brunner.definition import ArtifactPolicy
from brunner.errors import IntegrityError
from brunner.io import write_json_atomic
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
    stage_copy = subparsers.add_parser("stage-copy")
    stage_copy.add_argument("source", type=Path)
    stage_copy.add_argument("destination", type=Path)
    stage_copy.add_argument("expected")
    collect_copy = subparsers.add_parser("collect-copy")
    collect_copy.add_argument("source", type=Path)
    collect_copy.add_argument("baseline", type=Path)
    collect_copy.add_argument("destination", type=Path)
    collect_copy.add_argument("policy")
    collect_copy.add_argument("evaluation_results_path")
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


def _tree_paths(root: Path) -> tuple[set[str], set[str]]:
    directories: set[str] = set()
    files: set[str] = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise IntegrityError(f"staging source contains a symlink: {path}")
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            directories.add(relative)
        elif stat.S_ISREG(mode):
            files.add(relative)
        else:
            raise IntegrityError(
                f"staging source contains an unsupported entry: {path}"
            )
    return directories, files


def _copy_resumable(source: Path, destination: Path) -> None:
    expected = artifact_metadata(source)
    if expected is None or expected.type != "file":
        raise IntegrityError(f"staging source is not a file: {source}")
    partial = destination.with_name(destination.name + ".brunner-part")
    if partial.is_symlink() or destination.is_symlink():
        raise IntegrityError(
            f"staging destination contains an unsafe symlink: {destination}"
        )
    if destination.exists():
        if not destination.is_file():
            raise IntegrityError(
                "staging refuses to replace an existing non-file: "
                f"{destination}"
            )
        observed = artifact_metadata(destination)
        if observed is not None and observed.to_dict() == expected.to_dict():
            partial.unlink(missing_ok=True)
            return
        raise IntegrityError(
            "staging refuses to overwrite changed destination content: "
            f"{destination}"
        )
    if partial.exists() and not partial.is_file():
        raise IntegrityError(
            f"staging partial path is not a regular file: {partial}"
        )
    if partial.is_file() and partial.stat().st_size > expected.size:
        partial.unlink()
    offset = partial.stat().st_size if partial.is_file() else 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, partial.open(
        "ab" if offset else "wb"
    ) as output_stream:
        input_stream.seek(offset)
        while offset < expected.size:
            chunk = input_stream.read(min(8 * 1024 * 1024, expected.size - offset))
            if not chunk:
                raise IntegrityError(
                    f"staging source ended early at byte {offset}: {source}"
                )
            output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            offset += len(chunk)
    observed = artifact_metadata(partial)
    if observed is None or observed.to_dict() != expected.to_dict():
        partial.unlink(missing_ok=True)
        raise IntegrityError(
            f"staged file checksum mismatch: {source} -> {partial}"
        )
    partial.replace(destination)
    shutil.copystat(source, destination, follow_symlinks=False)


def _sync_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise IntegrityError(f"staging source root is unsafe: {source}")
    if not destination.is_dir() or destination.is_symlink():
        raise IntegrityError(
            f"staging destination root is unsafe: {destination}"
        )
    source_directories, source_files = _tree_paths(source)
    source_partials = {
        relative + ".brunner-part" for relative in source_files
    }
    for path in sorted(destination.rglob("*")):
        relative = path.relative_to(destination).as_posix()
        if path.is_symlink():
            raise IntegrityError(
                f"staging destination contains a symlink: {path}"
            )
        if path.is_dir() and relative not in source_directories:
            raise IntegrityError(
                "staging refuses to remove an unexpected destination "
                f"directory: {path}"
            )
        if (
            path.is_file()
            and relative not in source_files
            and relative not in source_partials
        ):
            raise IntegrityError(
                "staging refuses to remove unexpected destination "
                f"content: {path}"
            )
        if not path.is_dir() and not path.is_file():
            raise IntegrityError(
                f"staging destination contains an unsupported entry: {path}"
            )
    for relative in sorted(source_directories):
        target = destination / relative
        if target.exists() and not target.is_dir():
            raise IntegrityError(
                "staging refuses to replace an existing non-directory: "
                f"{target}"
            )
        target.mkdir(parents=True, exist_ok=True)
    for relative in sorted(source_files):
        _copy_resumable(source / relative, destination / relative)


def _unchanged_staged_files(
    baseline_trial: Path,
    inventory: dict[str, dict[str, object]],
) -> frozenset[str]:
    marker = baseline_trial / "workspace/.brunner-challenge.json"
    if not marker.is_file() or marker.is_symlink():
        return frozenset()
    value = json.loads(marker.read_text())
    baseline = value.get("file_inventory")
    if not isinstance(baseline, dict):
        return frozenset()
    return frozenset(
        f"workspace/{relative}"
        for relative, metadata in baseline.items()
        if isinstance(relative, str)
        and isinstance(metadata, dict)
        and inventory.get(f"workspace/{relative}") == metadata
    )


def _collect_copy(
    source: Path,
    baseline: Path,
    destination: Path,
    encoded_policy: str,
    evaluation_results_path: str,
) -> dict[str, object]:
    if not source.is_dir() or source.is_symlink():
        raise IntegrityError(f"collection source root is unsafe: {source}")
    if not baseline.is_dir() or baseline.is_symlink():
        raise IntegrityError(
            f"collection baseline root is unsafe: {baseline}"
        )
    policy, groups, _ = _policy(encoded_policy)
    inventory_policy = ArtifactPolicy(
        excluded_globs=policy.excluded_globs,
        groups=policy.groups,
        allow_symlinks=policy.allow_symlinks,
        collect_evaluated_artifacts=policy.collect_evaluated_artifacts,
        max_collection_bytes=None,
        failure_diagnostic_globs=policy.failure_diagnostic_globs,
        max_diagnostic_collection_bytes=(
            policy.max_diagnostic_collection_bytes
        ),
    )
    inventory = file_inventory(
        source,
        inventory_policy,
        included_groups=groups,
        evaluation_results_path=evaluation_results_path,
    )
    unchanged = _unchanged_staged_files(baseline, inventory)
    transfer_inventory = {
        name: metadata
        for name, metadata in inventory.items()
        if name not in unchanged
    }
    collection_mode = "complete"
    omitted_files = 0
    omitted_bytes = 0
    try:
        transferred_bytes = enforce_inventory_size(
            transfer_inventory,
            policy.max_collection_bytes,
        )
    except IntegrityError:
        if evaluation_results_path in inventory:
            raise
        full_inventory = inventory
        inventory = file_inventory(
            source,
            inventory_policy,
            included_groups=groups,
            evaluation_results_path=evaluation_results_path,
            included_globs=policy.failure_diagnostic_globs,
        )
        unchanged = _unchanged_staged_files(baseline, inventory)
        transfer_inventory = {
            name: metadata
            for name, metadata in inventory.items()
            if name not in unchanged
        }
        transferred_bytes = enforce_inventory_size(
            transfer_inventory,
            policy.max_diagnostic_collection_bytes,
        )
        collection_mode = "diagnostics"
        omitted_files = len(full_inventory) - len(inventory)
        omitted_bytes = sum(
            int(metadata.get("size", 0))
            for name, metadata in full_inventory.items()
            if name not in inventory and metadata.get("type") == "file"
        )
    partial, complete = prepare_partial_artifacts(
        destination,
        inventory,
        inventory_policy,
        groups,
    )
    for name, expected in inventory.items():
        if name in complete:
            continue
        target = partial / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name in unchanged:
            baseline_path = baseline / name
            target.unlink(missing_ok=True)
            try:
                target.hardlink_to(baseline_path)
            except OSError as error:
                raise IntegrityError(
                    "cannot reuse unchanged staged file without copying "
                    f"its bytes: {name}: {error}"
                ) from error
            continue
        if expected.get("type") == "symlink":
            if not policy.allow_symlinks:
                raise IntegrityError(
                    f"collection policy rejects symlink: {name}"
                )
            target.unlink(missing_ok=True)
            target.symlink_to(os.readlink(source / name))
            continue
        if expected.get("type") != "file":
            raise IntegrityError(
                f"collection encountered unsupported artifact: {name}"
            )
        _copy_resumable(source / name, target)
    result = finalize_artifact_collection(
        partial,
        destination,
        inventory,
        inventory_policy,
        included_groups=groups,
    )
    summary = {
        "result": str(result["result"]),
        "inventory": str(result["inventory"]),
        "files": int(result["files"]),
        "reused_staged_files": len(unchanged),
        "transferred_bytes": transferred_bytes,
        "collection_mode": collection_mode,
        "omitted_files": omitted_files,
        "omitted_bytes": omitted_bytes,
    }
    write_json_atomic(
        destination.with_name(destination.name + "-collection.json"),
        summary,
    )
    return summary


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "protocol":
        print(json.dumps(runtime_identity(), sort_keys=True))
        return 0
    if args.command == "stage-copy":
        source = args.source.resolve()
        destination = args.destination.resolve()
        _sync_tree(source, destination)
        print(
            json.dumps(
                _verify_stage(destination, args.expected),
                sort_keys=True,
            )
        )
        return 0
    if args.command == "collect-copy":
        print(
            json.dumps(
                _collect_copy(
                    args.source.resolve(),
                    args.baseline.resolve(),
                    args.destination.resolve(),
                    args.policy,
                    args.evaluation_results_path,
                ),
                sort_keys=True,
            )
        )
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
