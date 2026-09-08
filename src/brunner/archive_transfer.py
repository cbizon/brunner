"""Checksum-verified campaign restoration over a reconnectable exec stream."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import selectors
import signal
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from brunner.archive import (
    ARCHIVE_MANIFEST,
    ARCHIVE_SCHEMA_VERSIONS,
    SHA256_PATTERN,
)
from brunner.archive import (
    safe_archive_destination as _safe_output,
)
from brunner.errors import (
    BackendConnectivityError,
    BackendRequestError,
    IntegrityError,
)
from brunner.io import write_json_atomic

RESTORE_PROTOCOL = "brunner-archive-stream-v1"
RESTORE_JOURNAL = ".brunner-restore.json"
RESTORE_MARKER = "resume-archive.json"
MAX_HEADER_BYTES = 32 * 1024 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
CONNECTIVITY_FRAGMENTS = (
    "connection refused",
    "connection reset",
    "context deadline exceeded",
    "i/o timeout",
    "no route to host",
    "no such host",
    "service unavailable",
    "tls handshake timeout",
    "unable to connect",
    "unexpected eof",
    "can't assign requested address",
    "cannot assign requested address",
    "network is unreachable",
    "broken pipe",
)


def transport_error_type(
    message: str,
) -> type[BackendRequestError | BackendConnectivityError]:
    return (
        BackendConnectivityError
        if any(fragment in message.lower() for fragment in CONNECTIVITY_FRAGMENTS)
        else BackendRequestError
    )


class RestoreStream:
    """Bounded framing with an inactivity timeout, not a whole-transfer timeout."""

    def __init__(self, reader: int, writer: int, timeout: float) -> None:
        self.reader = reader
        self.writer = writer
        self.timeout = timeout
        os.set_blocking(reader, False)
        os.set_blocking(writer, False)

    def _io(self, fd: int, event: int, data: bytes | int) -> bytes:
        remaining = data if isinstance(data, int) else memoryview(data)
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, event)
            while remaining:
                if not selector.select(self.timeout):
                    raise BackendConnectivityError("archive stream inactivity timeout")
                try:
                    if isinstance(remaining, int):
                        block = os.read(fd, min(remaining, MAX_CHUNK_BYTES))
                        if not block:
                            raise BackendConnectivityError("archive stream ended early")
                        output.extend(block)
                        remaining -= len(block)
                    else:
                        count = os.write(fd, remaining)
                        remaining = remaining[count:]
                except BlockingIOError:
                    continue
                except (BrokenPipeError, ConnectionResetError) as error:
                    raise BackendConnectivityError(str(error)) from error
        return bytes(output)

    def read_bytes(self, size: int) -> bytes:
        return self._io(self.reader, selectors.EVENT_READ, size)

    def write_bytes(self, data: bytes) -> None:
        self._io(self.writer, selectors.EVENT_WRITE, data)

    def send(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        if len(encoded) > MAX_HEADER_BYTES:
            raise IntegrityError("archive stream header exceeds the size limit")
        self.write_bytes(struct.pack("!I", len(encoded)) + encoded)

    def receive(self) -> dict[str, Any]:
        size = struct.unpack("!I", self.read_bytes(4))[0]
        if not 0 < size <= MAX_HEADER_BYTES:
            raise IntegrityError("invalid archive stream header size")
        try:
            value = json.loads(self.read_bytes(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrityError("invalid archive stream JSON") from error
        if not isinstance(value, dict):
            raise IntegrityError("archive stream frame must be an object")
        return value


def _digest(path: Path, progress: Callable[[], None]) -> str:
    digest = hashlib.sha256()
    expected_size = path.stat().st_size
    size = 0
    with path.open("rb") as source:
        while block := source.read(MAX_CHUNK_BYTES):
            size += len(block)
            if size > expected_size:
                raise IntegrityError(f"archive file grew during verification: {path}")
            digest.update(block)
            progress()
    if size != expected_size:
        raise IntegrityError(f"archive file changed during verification: {path}")
    return digest.hexdigest()


def _same_file(
    root: Path,
    record: dict[str, Any],
    progress: Callable[[], None],
) -> bool:
    path = _safe_output(root, record["path"])
    partial = _safe_output(root, record["path"] + ".brunner-part")
    if partial.exists() and not partial.is_file():
        raise IntegrityError(f"archive partial is not a regular file: {partial}")
    if not path.exists():
        return False
    if not path.is_file():
        raise IntegrityError(f"archive destination is not a regular file: {path}")
    if (
        path.stat().st_size != record["size"]
        or _digest(path, progress) != record["sha256"]
    ):
        raise IntegrityError(
            f"archive restore refuses to overwrite changed remote content: {path}"
        )
    return True


def _publish(partial: Path, target: Path) -> None:
    # Link, rather than replace, so even an unexpected writer cannot be overwritten.
    os.link(partial, target)
    partial.unlink()
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_records(
    manifest: dict[str, Any], results: Path
) -> dict[str, dict[str, Any]]:
    if manifest.get("schema_version") not in ARCHIVE_SCHEMA_VERSIONS:
        raise IntegrityError("unsupported archive manifest schema")
    if not isinstance(manifest.get("campaign_id"), str) or not manifest["campaign_id"]:
        raise IntegrityError("archive manifest has no campaign ID")
    campaign_sha = manifest.get("campaign_sha256")
    if (
        not isinstance(campaign_sha, str)
        or SHA256_PATTERN.fullmatch(campaign_sha) is None
        or (
            manifest.get("terminal") is not None
            and manifest.get("terminal") is not True
        )
    ):
        raise IntegrityError("archive manifest has invalid identity or terminal status")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise IntegrityError("archive manifest files must be an array")
    files: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise IntegrityError("invalid archive file record")
        name = record.get("path")
        if (
            not isinstance(name, str)
            or name in {"", ".", ARCHIVE_MANIFEST}
            or Path(name).as_posix() != name
        ):
            raise IntegrityError(f"invalid archive path: {name!r}")
        _safe_output(results, name)
        size = record.get("size")
        digest = record.get("sha256")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
            or name in files
        ):
            raise IntegrityError(f"invalid or duplicate archive record: {name}")
        files[name] = {"path": name, "size": size, "sha256": digest}
    paths = set(files) | {ARCHIVE_MANIFEST}
    for name in paths:
        if name + ".brunner-part" in paths or any(
            parent.as_posix() in paths
            or parent.as_posix().removesuffix(".brunner-part") in paths
            for parent in Path(name).parents
            if parent != Path(".")
        ):
            raise IntegrityError(f"archive file/partial path collision: {name}")
    if "campaign.json" not in files:
        raise IntegrityError("archive does not contain campaign.json")
    return files


def _write_bytes_verified(
    root: Path,
    relative: str,
    content: bytes,
    progress: Callable[[], None],
    *,
    read_only: bool = False,
) -> None:
    record = {
        "path": relative,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if _same_file(root, record, progress):
        return
    if read_only:
        raise IntegrityError(f"completed archive restore is missing {relative}")
    target = _safe_output(root, relative)
    partial = _safe_output(root, relative + ".brunner-part")
    if partial.exists() and not partial.is_file():
        raise IntegrityError(f"archive partial is not a regular file: {partial}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    _publish(partial, target)


def _copy_verified(
    source: Path,
    control: Path,
    record: dict[str, Any],
    progress: Callable[[], None],
    *,
    read_only: bool,
) -> None:
    if _same_file(control, record, progress):
        return
    if read_only:
        raise IntegrityError(f"completed archive restore is missing {record['path']}")
    target = _safe_output(control, record["path"])
    partial = _safe_output(control, record["path"] + ".brunner-part")
    if partial.exists() and not partial.is_file():
        raise IntegrityError(f"archive partial is not a regular file: {partial}")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as input_file, partial.open("wb") as output:
        while block := input_file.read(MAX_CHUNK_BYTES):
            size += len(block)
            if size > record["size"]:
                raise IntegrityError(
                    f"restored source grew during control copy: {source}"
                )
            output.write(block)
            digest.update(block)
            progress()
        output.flush()
        os.fsync(output.fileno())
    if size != record["size"] or digest.hexdigest() != record["sha256"]:
        raise IntegrityError(f"restored source changed during control copy: {source}")
    _publish(partial, target)


def _receive_file(
    stream: RestoreStream,
    results: Path,
    record: dict[str, Any],
    checkpoint: Callable[[str, int], None],
    progress: Callable[[], None],
) -> None:
    name = record["path"]
    target = _safe_output(results, name)
    partial = _safe_output(results, name + ".brunner-part")
    target.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists() and not partial.is_file():
        raise IntegrityError(f"archive partial is not a regular file: {partial}")
    offset = partial.stat().st_size if partial.exists() else 0
    prefix = _digest(partial, progress) if offset else hashlib.sha256().hexdigest()
    stream.send({"type": "need", "path": name, "offset": offset, "sha256": prefix})
    response = stream.receive()
    while response.get("type") == "checking_prefix":
        response = stream.receive()
    if response.get("type") == "restart":
        offset = 0
    elif response.get("type") != "append" or offset > record["size"]:
        raise IntegrityError("invalid archive file resume response")
    with partial.open("ab" if offset else "wb") as output:
        while offset < record["size"]:
            header = stream.receive()
            count = header.get("size")
            if (
                header.get("type") != "chunk"
                or header.get("offset") != offset
                or type(count) is not int
                or not 0 < count <= MAX_CHUNK_BYTES
                or offset + count > record["size"]
            ):
                raise IntegrityError(f"invalid archive chunk for {name}")
            data = stream.read_bytes(count)
            if hashlib.sha256(data).hexdigest() != header.get("sha256"):
                raise IntegrityError(f"archive chunk checksum mismatch: {name}")
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            offset += count
            checkpoint(name, offset)
            stream.send({"type": "ack", "path": name, "offset": offset})
        output.flush()
        os.fsync(output.fileno())
    if _digest(partial, progress) != record["sha256"]:
        raise IntegrityError(f"archive file checksum mismatch: {name}")
    _publish(partial, target)


def _restore_receiver(stream: RestoreStream, results: Path, control: Path) -> None:
    begin = stream.receive()
    if begin.get("type") != "begin" or not isinstance(begin.get("manifest"), str):
        raise IntegrityError("archive stream requires a manifest")
    manifest_bytes = begin["manifest"].encode()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha != begin.get("manifest_sha256"):
        raise IntegrityError("archive manifest checksum mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise IntegrityError("invalid archive manifest JSON") from error
    if not isinstance(manifest, dict):
        raise IntegrityError("archive manifest must be an object")
    files = _manifest_records(manifest, results)
    journal_path = _safe_output(control, RESTORE_JOURNAL)
    identity = {
        "protocol": RESTORE_PROTOCOL,
        "campaign_id": manifest["campaign_id"],
        "archive_manifest_sha256": manifest_sha,
    }
    already_complete = False
    if journal_path.exists():
        try:
            existing = json.loads(journal_path.read_text())
        except (ValueError, OSError) as error:
            raise IntegrityError("unreadable archive restore checkpoint") from error
        if (
            not isinstance(existing, dict)
            or any(existing.get(key) != value for key, value in identity.items())
            or existing.get("status") not in {"restoring", "complete"}
        ):
            raise IntegrityError("PVC contains a different archive restore checkpoint")
        already_complete = existing.get("status") == "complete"
    journal = {
        **identity,
        "status": "restoring",
        "files_verified": 0,
        "bytes_verified": 0,
    }
    last_progress = 0.0

    def progress() -> None:
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress >= 1:
            stream.send({"type": "progress", **journal})
            last_progress = now

    def checkpoint(path: str, offset: int) -> None:
        journal.update({"path": path, "offset": offset})
        if not already_complete:
            write_json_atomic(journal_path, journal)

    manifest_record = {
        "path": ARCHIVE_MANIFEST,
        "size": len(manifest_bytes),
        "sha256": manifest_sha,
    }
    for root, record in (
        (results, manifest_record),
        (control, files["campaign.json"]),
    ):
        present = _same_file(root, record, progress)
        if already_complete and not present:
            raise IntegrityError(
                f"completed archive restore is missing {record['path']}"
            )
    checkpoint("", 0)
    # A journal is diagnostic, not a license to skip verification. Reconciliation
    # hashes existing content on the PVC, without a network round trip per file.
    for name, record in sorted(files.items()):
        reused = _same_file(results, record, progress)
        if not reused:
            if already_complete:
                raise IntegrityError(f"completed archive restore is missing {name}")
            _receive_file(stream, results, record, checkpoint, progress)
        else:
            partial = _safe_output(results, name + ".brunner-part")
            if partial.exists():
                if not partial.is_file():
                    raise IntegrityError(
                        f"archive partial is not a regular file: {partial}"
                    )
                if not already_complete:
                    partial.unlink()
        journal["files_verified"] += 1
        journal["bytes_verified"] += record["size"]
        checkpoint(name, record["size"])
        stream.send({"type": "file_done", "reused": reused, **journal})

    try:
        state = json.loads((results / "campaign.json").read_text())
    except (OSError, ValueError) as error:
        raise IntegrityError("invalid restored campaign state") from error
    if (
        not isinstance(state, dict)
        or state.get("campaign_id") != manifest["campaign_id"]
        or state.get("status") not in {"complete", "attention_required"}
        or not isinstance(state.get("trials"), list)
    ):
        raise IntegrityError("restored campaign is not a compatible terminal state")
    control_names = ["campaign.json"]
    for trial in state["trials"]:
        if not isinstance(trial, dict) or trial.get("phase") != "complete":
            raise IntegrityError("restored campaign has unfinished trials")
        test_id = trial.get("test_id")
        if (
            not isinstance(test_id, str)
            or test_id in {"", ".", ".."}
            or Path(test_id).name != test_id
        ):
            raise IntegrityError("restored campaign has an unsafe trial ID")
        name = f"trials/{test_id}/metadata/manifest.json"
        if name not in files:
            raise IntegrityError(f"restored campaign is missing metadata: {name}")
        control_names.append(name)
    for name in control_names:
        # Only compact state/metadata is copied to control, never bulk results.
        _copy_verified(
            results / name, control, files[name], progress, read_only=already_complete
        )
    _copy_verified(
        results / "campaign.json",
        control,
        {**files["campaign.json"], "path": "campaign.json.bak"},
        progress,
        read_only=already_complete,
    )
    _write_bytes_verified(
        results, ARCHIVE_MANIFEST, manifest_bytes, progress, read_only=already_complete
    )
    _write_bytes_verified(
        control,
        RESTORE_MARKER,
        (
            json.dumps(
                {
                    "schema_version": "1.0",
                    "campaign_id": manifest["campaign_id"],
                    "archive_manifest_sha256": manifest_sha,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
        progress,
        read_only=already_complete,
    )
    journal["status"] = "complete"
    if not already_complete:
        write_json_atomic(journal_path, journal)
    stream.send({"type": "complete", **journal})


def serve_restore(
    results: Path,
    control: Path,
    *,
    lock_path: Path,
    timeout: float = 120,
) -> int:
    stream = RestoreStream(0, 1, timeout)
    stream.send({"type": "hello", "protocol": RESTORE_PROTOCOL})
    try:
        # The lock lives on the helper's local emptyDir, not the network PVC.
        # A single named helper Pod owns these mounts; process death releases it.
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.send(
                    {
                        "type": "error",
                        "kind": "busy",
                        "message": "another archive receiver is active",
                    }
                )
                return 1
            _restore_receiver(stream, results, control)
    except BackendConnectivityError:
        return 1
    except (IntegrityError, OSError) as error:
        try:
            stream.send({"type": "error", "kind": "terminal", "message": str(error)})
        except BackendConnectivityError:
            pass
        return 1
    return 0


def _exchange(
    stream: RestoreStream,
    archive: dict[str, Any],
    chunk_bytes: int,
    progress: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    hello = stream.receive()
    if hello != {"type": "hello", "protocol": RESTORE_PROTOCOL}:
        raise BackendRequestError(
            "archive writer protocol is incompatible; rebuild the controller image"
        )
    manifest = archive["manifest_path"].read_bytes()
    if hashlib.sha256(manifest).hexdigest() != archive["manifest_sha256"]:
        raise IntegrityError("local archive manifest changed before restoration")
    stream.send(
        {
            "type": "begin",
            "manifest": manifest.decode(),
            "manifest_sha256": archive["manifest_sha256"],
        }
    )
    while True:
        message = stream.receive()
        kind = message.get("type")
        if kind == "error":
            error_type = (
                BackendConnectivityError
                if message.get("kind") == "busy"
                else IntegrityError
            )
            raise error_type(f"archive receiver: {message.get('message')}")
        if kind in {"progress", "file_done", "complete"}:
            progress(message)
            if kind == "complete":
                if (
                    message.get("archive_manifest_sha256") != archive["manifest_sha256"]
                    or message.get("status") != "complete"
                    or message.get("files_verified") != len(archive["files"])
                    or message.get("bytes_verified")
                    != sum(record["size"] for record in archive["files"].values())
                ):
                    raise IntegrityError("archive completion has the wrong identity")
                return message
            continue
        if kind != "need" or message.get("path") not in archive["files"]:
            raise IntegrityError("unexpected archive receiver request")
        name = message["path"]
        record = archive["files"][name]
        source = archive["root"] / name
        # Recheck path safety after the initial local archive validation.
        _safe_output(archive["root"], name)
        if not source.is_file() or source.stat().st_size != record["size"]:
            raise IntegrityError(f"local archive source changed: {name}")
        offset = message.get("offset")
        if type(offset) is not int or offset < 0:
            raise IntegrityError("invalid archive resume offset")
        digest = hashlib.sha256()
        with source.open("rb") as input_file:
            remaining = min(offset, record["size"])
            last_check = time.monotonic()
            while remaining:
                block = input_file.read(min(MAX_CHUNK_BYTES, remaining))
                if not block:
                    raise IntegrityError(f"local archive source ended early: {name}")
                digest.update(block)
                remaining -= len(block)
                if time.monotonic() - last_check >= 1:
                    stream.send({"type": "checking_prefix"})
                    last_check = time.monotonic()
            valid_prefix = offset <= record[
                "size"
            ] and digest.hexdigest() == message.get("sha256")
            if not valid_prefix:
                offset = 0
                input_file.seek(0)
            else:
                progress({"type": "resumed", "path": name, "offset": offset})
            stream.send({"type": "append" if valid_prefix else "restart"})
            while offset < record["size"]:
                data = input_file.read(min(chunk_bytes, record["size"] - offset))
                if not data:
                    raise IntegrityError(f"local archive source ended early: {name}")
                stream.send(
                    {
                        "type": "chunk",
                        "offset": offset,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                stream.write_bytes(data)
                offset += len(data)
                ack = stream.receive()
                if ack.get("type") == "error":
                    raise IntegrityError(f"archive receiver: {ack.get('message')}")
                if ack != {"type": "ack", "path": name, "offset": offset}:
                    raise IntegrityError(f"invalid archive acknowledgement: {name}")
                progress(
                    {
                        "type": "uploaded",
                        "path": name,
                        "offset": offset,
                        "bytes": len(data),
                    }
                )


def restore_archive_stream(
    command: tuple[str, ...] | Callable[[], tuple[str, ...]],
    archive: dict[str, Any],
    *,
    inactivity_seconds: float = 120,
    retry_seconds: float = 600,
    chunk_bytes: int = MAX_CHUNK_BYTES,
    progress: Callable[[dict[str, Any]], None] = lambda event: None,
) -> dict[str, Any]:
    """Reconnect to the same helper; only verified data determines resume offsets."""
    if inactivity_seconds <= 0 or retry_seconds < 0 or chunk_bytes < 1:
        raise ValueError("invalid archive stream timeout or chunk size")
    chunk_bytes = min(chunk_bytes, MAX_CHUNK_BYTES)
    outage_start: float | None = None
    reconnects = 0
    high_water: dict[str, int] = {}
    completed_files: set[str] = set()

    def report(event: dict[str, Any]) -> None:
        nonlocal outage_start
        if event.get("type") in {"uploaded", "resumed", "file_done"}:
            name, offset = event["path"], event["offset"]
            if offset > high_water.get(name, 0):
                high_water[name] = offset
                outage_start = None
            if event["type"] == "file_done" and name not in completed_files:
                completed_files.add(name)
                outage_start = None
        progress({**event, "reconnects": reconnects})

    while True:
        try:
            arguments = command() if callable(command) else command
            with tempfile.TemporaryFile() as stderr:
                try:
                    process = subprocess.Popen(
                        arguments,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=stderr,
                        start_new_session=True,
                    )
                except OSError as error:
                    raise BackendRequestError(
                        f"cannot launch archive transfer {arguments[0]}: {error}"
                    ) from error
                try:
                    assert process.stdin is not None and process.stdout is not None
                    stream = RestoreStream(
                        process.stdout.fileno(),
                        process.stdin.fileno(),
                        inactivity_seconds,
                    )
                    result = _exchange(stream, archive, chunk_bytes, report)
                    process.stdin.close()
                    try:
                        code = process.wait(timeout=inactivity_seconds)
                    except subprocess.TimeoutExpired as error:
                        raise BackendConnectivityError(
                            "archive exec did not exit after completion"
                        ) from error
                    if code:
                        raise BackendConnectivityError(
                            f"archive exec exited {code} after completion"
                        )
                except BackendConnectivityError as error:
                    # A receiver can reject a write while the sender is still
                    # filling stdin. Prefer its structured failure to EPIPE.
                    try:
                        pending = RestoreStream(
                            process.stdout.fileno(),
                            process.stdin.fileno(),
                            min(0.2, inactivity_seconds),
                        ).receive()
                    except (BackendConnectivityError, IntegrityError, ValueError):
                        pending = {}
                    if pending.get("type") == "error":
                        if pending.get("kind") != "busy":
                            raise IntegrityError(
                                f"archive receiver: {pending.get('message')}"
                            ) from error
                        raise BackendConnectivityError(
                            f"archive receiver: {pending.get('message')}"
                        ) from error
                    try:
                        code = process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        code = None
                    if code is not None and code != 0:
                        stderr.seek(0, os.SEEK_END)
                        stderr.seek(max(0, stderr.tell() - 65536))
                        diagnostic = stderr.read().decode(errors="replace").strip()
                        if diagnostic:
                            raise transport_error_type(diagnostic)(
                                f"archive exec exited {code}: {diagnostic}. "
                                "The controller image must support restore-stream."
                            ) from error
                    raise
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.wait()
                    if process.stdin is not None:
                        process.stdin.close()
                    if process.stdout is not None:
                        process.stdout.close()
            return {**result, "reconnects": reconnects}
        except BackendConnectivityError as error:
            now = time.monotonic()
            if outage_start is None:
                outage_start = now
            remaining = retry_seconds - (now - outage_start)
            if remaining <= 0 or retry_seconds == 0:
                raise BackendConnectivityError(
                    f"archive restore retry budget exhausted; PVC progress retained. "
                    f"Rerun campaign-submit with the same --resume-from archive. {error}"
                ) from error
            reconnects += 1
            delay = min(2 ** min(reconnects, 5), 30, remaining)
            report(
                {"type": "reconnecting", "error": str(error), "delay_seconds": delay}
            )
            time.sleep(delay)
