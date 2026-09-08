from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from brunner.archive import load_campaign_archive
from brunner.archive_transfer import (
    RESTORE_JOURNAL,
    RESTORE_MARKER,
    RESTORE_PROTOCOL,
    RestoreStream,
    restore_archive_stream,
)
from brunner.errors import BackendConnectivityError, BackendRequestError, IntegrityError


def _archive(tmp_path: Path, *, count: int = 4) -> dict[str, Any]:
    root = tmp_path / "source"
    root.mkdir()
    state = {
        "campaign_id": "restore",
        "status": "complete",
        "trials": [
            {"test_id": f"run-{index:02d}", "phase": "complete"} for index in range(16)
        ],
    }
    contents = {
        "campaign.json": json.dumps(state).encode(),
        "empty": b"",
        "bulk": b"data" * 4096,
        **{f"files/{index:04d}": bytes([index % 256]) * 100 for index in range(count)},
        **{
            f"trials/{trial['test_id']}/metadata/manifest.json": b"{}"
            for trial in state["trials"]
        },
    }
    records = []
    for name, content in contents.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append(
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    (root / "result-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "campaign_id": "restore",
                "campaign_sha256": "a" * 64,
                "terminal": True,
                "files": records,
            }
        )
    )
    return load_campaign_archive(root, require_terminal=True, require_resumable=True)


def _command(tmp_path: Path) -> tuple[str, ...]:
    for name in ("results", "control"):
        (tmp_path / name).mkdir(exist_ok=True)
    return (
        sys.executable,
        "-m",
        "brunner.backends.remote",
        "restore-stream",
        str(tmp_path / "results"),
        str(tmp_path / "control"),
        "--lock-path",
        str(tmp_path / "receiver.lock"),
    )


def _verify(tmp_path: Path, archive: dict[str, Any]) -> None:
    restored = load_campaign_archive(
        tmp_path / "results", require_terminal=True, require_resumable=True
    )
    assert restored["manifest_sha256"] == archive["manifest_sha256"]
    control = tmp_path / "control"
    assert (control / "campaign.json").read_bytes() == (
        archive["root"] / "campaign.json"
    ).read_bytes()
    assert (control / "campaign.json.bak").read_bytes() == (
        control / "campaign.json"
    ).read_bytes()
    assert json.loads((control / RESTORE_JOURNAL).read_text())["status"] == "complete"
    assert (
        json.loads((control / RESTORE_MARKER).read_text())["archive_manifest_sha256"]
        == archive["manifest_sha256"]
    )
    assert not list((tmp_path / "results").rglob("*.brunner-part"))
    assert not (control / "bulk").exists()


def test_stream_restores_753_files_in_one_real_subprocess(tmp_path: Path) -> None:
    archive = _archive(tmp_path, count=734)
    commands = []
    command = _command(tmp_path)

    def launch() -> tuple[str, ...]:
        commands.append(command)
        return command

    events = []
    result = restore_archive_stream(
        launch, archive, chunk_bytes=1024, retry_seconds=0, progress=events.append
    )
    assert len(archive["files"]) == 753
    assert len(commands) == 1
    assert result["reconnects"] == 0
    assert result["files_verified"] == 753
    assert sum(event.get("bytes", 0) for event in events) == sum(
        record["size"] for record in archive["files"].values()
    )
    _verify(tmp_path, archive)


@pytest.mark.parametrize("partial", [b"data" * 512, b"invalid-prefix", b"x" * 20000])
def test_stream_adopts_legacy_partial_files(tmp_path: Path, partial: bytes) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    (tmp_path / "results/bulk.brunner-part").write_bytes(partial)
    restore_archive_stream(command, archive, retry_seconds=0)
    _verify(tmp_path, archive)


FAULT_RECEIVER = r"""
import os
from pathlib import Path
import sys
import brunner.archive_transfer as transfer

root = Path(sys.argv[1])
fault = sys.argv[2]
marker = root / "fault-used"
original_send = transfer.RestoreStream.send
original_publish = transfer._publish
original_read = transfer.RestoreStream.read_bytes

def fail():
    marker.write_text("1")
    os._exit(75)

def send(self, message):
    if fault == "three-outages" and message.get("type") == "ack":
        count = int(marker.read_text()) if marker.exists() else 0
        if count < 3:
            marker.write_text(str(count + 1))
            os._exit(75)
    if fault == "slow-progress" and message.get("type") == "ack":
        import time
        time.sleep(0.03)
    if not marker.exists():
        if fault == "before-ack" and message.get("type") == "ack":
            fail()
        if fault == "before-complete" and message.get("type") == "complete":
            fail()
    original_send(self, message)
    if not marker.exists() and fault == "after-ack" and message.get("type") == "ack":
        fail()

def publish(partial, target):
    if not marker.exists() and fault == "after-link" and target.name == "bulk":
        os.link(partial, target)
        fail()
    if fault == "disk-full":
        import errno
        raise OSError(errno.ENOSPC, "No space left on device")
    original_publish(partial, target)
    if not marker.exists() and fault == "after-publish" and target.name == "bulk":
        fail()

def read(self, size):
    if not marker.exists() and fault == "mid-chunk" and size == 1024:
        original_read(self, size // 2)
        fail()
    return original_read(self, size)

transfer.RestoreStream.send = send
transfer.RestoreStream.read_bytes = read
transfer._publish = publish
if not marker.exists() and fault == "address-exhausted":
    marker.write_text("1")
    print("read tcp: can't assign requested address", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(transfer.serve_restore(
    root / "results", root / "control", lock_path=root / "receiver.lock"
))
"""


@pytest.mark.parametrize(
    "fault",
    [
        "mid-chunk",
        "before-ack",
        "after-ack",
        "after-link",
        "after-publish",
        "before-complete",
        "address-exhausted",
    ],
)
def test_stream_reconnects_after_ambiguous_write_and_commit(
    tmp_path: Path, fault: str
) -> None:
    archive = _archive(tmp_path)
    _command(tmp_path)
    command = (sys.executable, "-c", FAULT_RECEIVER, str(tmp_path), fault)
    result = restore_archive_stream(
        command, archive, retry_seconds=0.1, chunk_bytes=1024
    )
    assert result["reconnects"] == 1
    assert (tmp_path / "fault-used").exists()
    _verify(tmp_path, archive)


def test_repeated_transport_failure_exhausts_budget_without_completing(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    calls = []

    def unavailable() -> tuple[str, ...]:
        calls.append(1)
        return (
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('no such host'); sys.exit(1)",
        )

    with pytest.raises(BackendConnectivityError, match="retry budget exhausted"):
        restore_archive_stream(unavailable, archive, retry_seconds=0.1)
    assert len(calls) == 2
    assert not (tmp_path / "control" / RESTORE_MARKER).exists()


@pytest.mark.parametrize("error", ["Forbidden", "invalid choice: 'restore-stream'"])
def test_permanent_exec_error_is_not_retried(tmp_path: Path, error: str) -> None:
    archive = _archive(tmp_path)
    calls = []

    def rejected() -> tuple[str, ...]:
        calls.append(1)
        return (
            sys.executable,
            "-c",
            f"import sys; sys.stderr.write({error!r}); sys.exit(1)",
        )

    with pytest.raises(BackendRequestError, match="exec exited 1"):
        restore_archive_stream(rejected, archive)
    assert len(calls) == 1


def test_stream_inactivity_timeout_is_bounded(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    command = (sys.executable, "-c", "import time; time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(BackendConnectivityError, match="inactivity timeout"):
        restore_archive_stream(
            command, archive, inactivity_seconds=0.1, retry_seconds=0
        )
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("name", ["bulk", "campaign.json", "result-manifest.json"])
def test_changed_remote_content_is_never_overwritten(tmp_path: Path, name: str) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    target = tmp_path / "results" / name
    target.write_bytes(b"do not replace")
    with pytest.raises(IntegrityError, match="overwrite changed"):
        restore_archive_stream(command, archive, retry_seconds=0)
    assert target.read_bytes() == b"do not replace"
    assert not (tmp_path / "control" / RESTORE_MARKER).exists()


@pytest.mark.parametrize("name", ["bulk", "bulk.brunner-part", "files"])
def test_receiver_rejects_symlinks(tmp_path: Path, name: str) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (tmp_path / "results" / name).symlink_to(outside)
    with pytest.raises(IntegrityError, match="symlink"):
        restore_archive_stream(command, archive, retry_seconds=0)
    assert outside.read_text() == "untouched"


def test_completed_checkpoint_does_not_hide_changed_files(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    restore_archive_stream(command, archive)
    (tmp_path / "results/bulk").write_bytes(b"bad!" * 4096)
    with pytest.raises(IntegrityError, match="overwrite changed"):
        restore_archive_stream(command, archive, retry_seconds=0)


@pytest.mark.parametrize(
    "checkpoint", ["invalid JSON", '{"archive_manifest_sha256":"different"}']
)
def test_receiver_rejects_corrupted_or_foreign_checkpoint(
    tmp_path: Path, checkpoint: str
) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    (tmp_path / "control" / RESTORE_JOURNAL).write_text(checkpoint)
    with pytest.raises(IntegrityError, match="checkpoint"):
        restore_archive_stream(command, archive, retry_seconds=0)


def test_source_changed_after_validation_fails_checksum(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    (archive["root"] / "bulk").write_bytes(b"bad!" * 4096)
    with pytest.raises(IntegrityError, match="checksum mismatch"):
        restore_archive_stream(command, archive, retry_seconds=0)
    assert not (tmp_path / "results/bulk").exists()
    assert not (tmp_path / "control" / RESTORE_MARKER).exists()


def test_receiver_serializes_concurrent_sessions(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as first:
        assert first.stdout is not None and first.stdin is not None
        stream = RestoreStream(first.stdout.fileno(), first.stdin.fileno(), 3)
        assert stream.receive()["protocol"] == RESTORE_PROTOCOL
        stream.send(
            {
                "type": "begin",
                "manifest": archive["manifest_path"].read_text(),
                "manifest_sha256": archive["manifest_sha256"],
            }
        )
        while stream.receive()["type"] != "need":
            pass
        try:
            with pytest.raises(
                BackendConnectivityError, match="another archive receiver"
            ):
                restore_archive_stream(command, archive, retry_seconds=0)
        finally:
            first.kill()
            first.wait()
    restore_archive_stream(command, archive, retry_seconds=0)
    _verify(tmp_path, archive)


def test_completed_restore_replay_is_read_only(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    restore_archive_stream(command, archive, retry_seconds=0)
    paths = [
        path
        for folder in ("results", "control")
        for path in (tmp_path / folder).rglob("*")
        if path.is_file()
    ]
    before = {path: path.stat() for path in paths}
    events = []
    restore_archive_stream(command, archive, retry_seconds=0, progress=events.append)
    assert not any(event["type"] == "uploaded" for event in events)
    for path in paths:
        assert path.stat().st_mtime_ns == before[path].st_mtime_ns
        assert path.stat().st_ino == before[path].st_ino
    _verify(tmp_path, archive)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a/./alias",
        "bulk/nested",
        "bulk.brunner-part",
        "result-manifest.json",
    ],
)
def test_receiver_rejects_manifest_path_and_partial_collisions(
    tmp_path: Path,
    path: str,
) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    manifest = archive["manifest"]
    manifest["files"].append(
        {
            "path": path,
            "size": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    )
    content = json.dumps(manifest).encode()
    archive["manifest_path"].write_bytes(content)
    archive["manifest_sha256"] = hashlib.sha256(content).hexdigest()
    with pytest.raises(IntegrityError):
        restore_archive_stream(command, archive, retry_seconds=0)
    assert list((tmp_path / "results").iterdir()) == []


def test_disk_exhaustion_is_terminal_and_preserves_diagnostics(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    _command(tmp_path)
    events = []
    with pytest.raises(IntegrityError, match="No space left on device"):
        restore_archive_stream(
            (sys.executable, "-c", FAULT_RECEIVER, str(tmp_path), "disk-full"),
            archive,
            progress=events.append,
        )
    assert not any(event["type"] == "reconnecting" for event in events)
    assert not (tmp_path / "control" / RESTORE_MARKER).exists()
    assert (tmp_path / "results/bulk.brunner-part").exists()


def test_multiple_reconnections_advance_partial_frontier(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    _command(tmp_path)
    result = restore_archive_stream(
        (sys.executable, "-c", FAULT_RECEIVER, str(tmp_path), "three-outages"),
        archive,
        retry_seconds=0.1,
        chunk_bytes=1024,
    )
    assert result["reconnects"] == 3
    _verify(tmp_path, archive)


def test_new_invocation_resumes_durable_progress(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    command = _command(tmp_path)
    with pytest.raises(BackendConnectivityError):
        restore_archive_stream(
            (sys.executable, "-c", FAULT_RECEIVER, str(tmp_path), "before-ack"),
            archive,
            retry_seconds=0,
            chunk_bytes=1024,
        )
    assert (tmp_path / "results/bulk.brunner-part").stat().st_size == 1024
    checkpoint = json.loads((tmp_path / "control" / RESTORE_JOURNAL).read_text())
    assert checkpoint["offset"] == 1024
    events = []
    restore_archive_stream(command, archive, retry_seconds=0, progress=events.append)
    assert sum(event.get("bytes", 0) for event in events) == (
        sum(record["size"] for record in archive["files"].values()) - 1024
    )
    _verify(tmp_path, archive)


def test_progressing_restore_can_exceed_inactivity_duration(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    _command(tmp_path)
    started = time.monotonic()
    result = restore_archive_stream(
        (sys.executable, "-c", FAULT_RECEIVER, str(tmp_path), "slow-progress"),
        archive,
        inactivity_seconds=1,
        retry_seconds=0,
        chunk_bytes=256,
    )
    assert time.monotonic() - started > 1
    assert result["reconnects"] == 0
    _verify(tmp_path, archive)


@pytest.mark.skipif(
    os.environ.get("BRUNNER_LARGE_ARCHIVE_TEST") != "1",
    reason="opt-in 810 MiB archive transfer and reconnect regression",
)
def test_large_archive_restore_advances_after_interruption(tmp_path: Path) -> None:
    archive = _archive(tmp_path, count=734)
    _command(tmp_path)
    total = 810 * 1024 * 1024
    remaining = total - sum(
        record["size"] for name, record in archive["files"].items() if name != "bulk"
    )
    size = remaining
    digest = hashlib.sha256()
    with (archive["root"] / "bulk").open("wb") as output:
        while remaining:
            block = b"resource" * min(131072, remaining // 8) or b"x" * remaining
            output.write(block)
            digest.update(block)
            remaining -= len(block)
    manifest = archive["manifest"]
    for record in manifest["files"]:
        if record["path"] == "bulk":
            record.update(size=size, sha256=digest.hexdigest())
    archive["manifest_path"].write_text(json.dumps(manifest))
    archive = load_campaign_archive(archive["root"], require_resumable=True)
    result = restore_archive_stream(
        (sys.executable, "-c", FAULT_RECEIVER, str(tmp_path), "before-ack"),
        archive,
        retry_seconds=0.1,
    )
    assert result["reconnects"] == 1
    assert result["bytes_verified"] == total
    assert result["files_verified"] == 753
    _verify(tmp_path, archive)
