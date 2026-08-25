from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from brunner.archive import ARCHIVE_MANIFEST, load_campaign_archive
from brunner.errors import IntegrityError


def _write_archive(
    root: Path,
    *,
    status: str = "complete",
    phase: str = "complete",
) -> None:
    state = {
        "campaign_id": "portable",
        "status": status,
        "trials": [
            {
                "test_id": "run-a",
                "phase": phase,
            }
        ],
    }
    files = {
        "campaign.json": (
            json.dumps(state, sort_keys=True) + "\n"
        ).encode(),
        "trials/run-a/metadata/manifest.json": b"{}\n",
    }
    records = []
    for relative, content in sorted(files.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        records.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "2.0",
        "campaign_id": "portable",
        "campaign_sha256": "a" * 64,
        "terminal": status in {"complete", "attention_required"},
        "files": records,
    }
    (root / ARCHIVE_MANIFEST).write_text(
        json.dumps(manifest, sort_keys=True)
    )


def test_load_campaign_archive_validates_resumable_terminal_snapshot(
    tmp_path: Path,
) -> None:
    _write_archive(tmp_path)

    archive = load_campaign_archive(
        tmp_path,
        expected_campaign_id="portable",
        require_terminal=True,
        require_resumable=True,
    )

    assert archive["terminal"] is True
    assert set(archive["files"]) == {
        "campaign.json",
        "trials/run-a/metadata/manifest.json",
    }


def test_load_campaign_archive_rejects_corrupted_content(
    tmp_path: Path,
) -> None:
    _write_archive(tmp_path)
    (tmp_path / "campaign.json").write_text("changed")

    with pytest.raises(IntegrityError, match="size mismatch"):
        load_campaign_archive(tmp_path)


def test_load_campaign_archive_rejects_false_terminal_claim(
    tmp_path: Path,
) -> None:
    _write_archive(tmp_path, status="running", phase="running")
    manifest_path = tmp_path / ARCHIVE_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["terminal"] = True
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(IntegrityError, match="disagree on terminal"):
        load_campaign_archive(tmp_path)


def test_load_campaign_archive_rejects_symlinks(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _write_archive(archive)
    (archive / "unsafe").symlink_to(archive / "campaign.json")

    with pytest.raises(IntegrityError, match="symlink"):
        load_campaign_archive(archive)


def test_remote_upload_protocol_resumes_and_commits_real_subprocess(
    tmp_path: Path,
) -> None:
    root = tmp_path / "remote"
    root.mkdir()
    content = b"portable campaign archive"
    command = [
        sys.executable,
        "-m",
        "brunner.backends.remote",
        "write-chunk",
        str(root),
        "nested/archive.bin",
    ]
    subprocess.run(
        [*command, "0", str(len(content))],
        input=content[:9],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [*command, "9", str(len(content))],
        input=content[9:],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "brunner.backends.remote",
            "commit-file",
            str(root),
            "nested/archive.bin",
            str(len(content)),
            hashlib.sha256(content).hexdigest(),
        ],
        check=True,
        capture_output=True,
    )

    assert (root / "nested/archive.bin").read_bytes() == content
    assert not (root / "nested/archive.bin.brunner-part").exists()
