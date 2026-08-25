from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from brunner.errors import IntegrityError
from brunner.hashing import sha256_file


ARCHIVE_MANIFEST = "result-manifest.json"
ARCHIVE_SCHEMA_VERSIONS = frozenset({"1.0", "2.0"})
TERMINAL_CAMPAIGN_STATES = frozenset({"complete", "attention_required"})
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _safe_archive_file(root: Path, relative_value: object) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise IntegrityError("campaign archive contains an invalid file path")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise IntegrityError(
            f"campaign archive contains an unsafe file path: {relative}"
        )
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise IntegrityError(
                f"campaign archive contains a symlink: {current}"
            )
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise IntegrityError(
            f"campaign archive file escapes its root: {relative}"
        )
    return path


def load_campaign_archive(
    root: Path,
    *,
    expected_campaign_id: str | None = None,
    require_terminal: bool = False,
    require_resumable: bool = False,
) -> dict[str, Any]:
    requested_root = root.expanduser()
    if requested_root.is_symlink():
        raise IntegrityError(
            f"campaign archive directory does not exist or is unsafe: "
            f"{requested_root}"
        )
    root = requested_root.resolve()
    if not root.is_dir():
        raise IntegrityError(
            f"campaign archive directory does not exist or is unsafe: {root}"
        )
    for path in root.rglob("*"):
        if path.is_symlink():
            raise IntegrityError(
                f"campaign archive contains a symlink: {path}"
            )

    manifest_path = root / ARCHIVE_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise IntegrityError(
            f"campaign archive manifest is missing or unsafe: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as error:
        raise IntegrityError(
            f"campaign archive manifest is unreadable: {manifest_path}"
        ) from error
    if not isinstance(manifest, dict):
        raise IntegrityError("campaign archive manifest must be an object")
    if manifest.get("schema_version") not in ARCHIVE_SCHEMA_VERSIONS:
        raise IntegrityError(
            "campaign archive manifest has an unsupported schema version: "
            f"{manifest.get('schema_version')!r}"
        )
    campaign_id = manifest.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise IntegrityError(
            "campaign archive manifest has no valid campaign_id"
        )
    if (
        expected_campaign_id is not None
        and campaign_id != expected_campaign_id
    ):
        raise IntegrityError(
            "campaign archive belongs to a different campaign: "
            f"{campaign_id!r} != {expected_campaign_id!r}"
        )
    campaign_sha256 = manifest.get("campaign_sha256")
    if (
        not isinstance(campaign_sha256, str)
        or SHA256_PATTERN.fullmatch(campaign_sha256) is None
    ):
        raise IntegrityError(
            "campaign archive manifest has no valid campaign_sha256"
        )

    records = manifest.get("files")
    if not isinstance(records, list):
        raise IntegrityError(
            "campaign archive manifest files must be an array"
        )
    files: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise IntegrityError(
                "campaign archive contains a malformed file record"
            )
        relative = record.get("path")
        path = _safe_archive_file(root, relative)
        relative_name = str(relative)
        if relative_name == ARCHIVE_MANIFEST:
            raise IntegrityError(
                "campaign archive manifest cannot inventory itself"
            )
        if relative_name in files:
            raise IntegrityError(
                f"campaign archive repeats a file path: {relative_name}"
            )
        try:
            expected_size = int(record["size"])
            expected_sha256 = str(record["sha256"])
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(
                f"campaign archive has invalid metadata for {relative_name}"
            ) from error
        if (
            expected_size < 0
            or SHA256_PATTERN.fullmatch(expected_sha256) is None
        ):
            raise IntegrityError(
                f"campaign archive has invalid metadata for {relative_name}"
            )
        if not path.is_file():
            raise IntegrityError(
                f"campaign archive file is missing: {relative_name}"
            )
        if path.stat().st_size != expected_size:
            raise IntegrityError(
                f"campaign archive file size mismatch: {relative_name}"
            )
        if sha256_file(path) != expected_sha256:
            raise IntegrityError(
                f"campaign archive checksum mismatch: {relative_name}"
            )
        files[relative_name] = {
            "path": relative_name,
            "size": expected_size,
            "sha256": expected_sha256,
        }

    campaign_record = files.get("campaign.json")
    if campaign_record is None:
        raise IntegrityError(
            "campaign archive does not contain campaign.json"
        )
    try:
        state = json.loads((root / "campaign.json").read_text())
    except (json.JSONDecodeError, OSError) as error:
        raise IntegrityError(
            "campaign archive contains an unreadable campaign.json"
        ) from error
    if not isinstance(state, dict):
        raise IntegrityError("archived campaign state must be an object")
    if state.get("campaign_id") != campaign_id:
        raise IntegrityError(
            "archived campaign state and manifest disagree on campaign_id"
        )

    state_terminal = state.get("status") in TERMINAL_CAMPAIGN_STATES
    manifest_terminal = manifest.get("terminal")
    if manifest_terminal is not None and not isinstance(
        manifest_terminal,
        bool,
    ):
        raise IntegrityError(
            "campaign archive manifest terminal must be a boolean"
        )
    if (
        isinstance(manifest_terminal, bool)
        and manifest_terminal != state_terminal
    ):
        raise IntegrityError(
            "campaign archive manifest and state disagree on terminal status"
        )
    terminal = state_terminal
    if require_terminal and not terminal:
        raise IntegrityError("campaign archive is not terminal")
    if require_resumable:
        if not terminal:
            raise IntegrityError(
                "only a terminal campaign archive can be resumed"
            )
        trials = state.get("trials")
        if not isinstance(trials, list):
            raise IntegrityError(
                "archived campaign state has no valid trial list"
            )
        incomplete: list[str] = []
        for trial in trials:
            if not isinstance(trial, dict):
                incomplete.append("<unknown>")
            elif trial.get("phase") != "complete":
                incomplete.append(
                    str(trial.get("test_id") or "<unknown>")
                )
        incomplete.sort()
        if incomplete:
            raise IntegrityError(
                "campaign archive cannot be resumed because these trials "
                f"are not complete: {incomplete}"
            )
        for trial in trials:
            test_id_value = trial.get("test_id")
            if not isinstance(test_id_value, str) or not test_id_value:
                raise IntegrityError(
                    "archived campaign state has a trial without a valid "
                    "test_id"
                )
            test_id = test_id_value
            test_path = Path(test_id)
            if (
                test_path.is_absolute()
                or test_path.name != test_id
                or test_id in {".", ".."}
            ):
                raise IntegrityError(
                    "archived campaign state has an unsafe test_id: "
                    f"{test_id!r}"
                )
            metadata_relative = (
                f"trials/{test_id}/metadata/manifest.json"
            )
            if metadata_relative not in files:
                raise IntegrityError(
                    "campaign archive cannot resume trial without metadata: "
                    f"{test_id}"
                )

    return {
        "root": root,
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_file(manifest_path),
        "manifest": manifest,
        "files": files,
        "state": state,
        "terminal": terminal,
    }
