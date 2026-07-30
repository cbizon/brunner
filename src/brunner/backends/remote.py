from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from brunner.artifacts import file_inventory
from brunner.definition import ArtifactPolicy
from brunner.submission import safe_child


def _policy(value: str) -> tuple[ArtifactPolicy, frozenset[str]]:
    decoded = json.loads(base64.urlsafe_b64decode(value).decode())
    return (
        ArtifactPolicy(
            excluded_globs=tuple(decoded.get("excluded_globs", ())),
            groups={
                str(name): tuple(patterns)
                for name, patterns in decoded.get("groups", {}).items()
            },
            allow_symlinks=bool(decoded.get("allow_symlinks", False)),
        ),
        frozenset(decoded.get("included_groups", ())),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("root", type=Path)
    inventory.add_argument("policy")
    read = subparsers.add_parser("read")
    read.add_argument("root", type=Path)
    read.add_argument("path")
    read.add_argument("offset", type=int)
    read.add_argument("count", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    if args.command == "inventory":
        policy, groups = _policy(args.policy)
        print(
            json.dumps(
                file_inventory(
                    root,
                    policy,
                    included_groups=groups,
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
