"""Instance-scoped input upload and verified output collection.

The local client uses injected official-CLI transports. The remote handler reads
the launcher instance marker written after a provider identity check; it never
creates or changes that binding. No credentials or private provider state enter
asset manifests.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

BASE = Path("/content/colab-comfyui-runtime")
CONTENT_ROOT = Path("/content")
DRIVE_MOUNT = CONTENT_ROOT / "drive"
CHUNK_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_ASSETS = 1000


class AssetsError(RuntimeError):
    """A transfer was not verified; no unverified file is reported as complete."""


class Deadline:
    def __init__(self, seconds: float):
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise AssetsError("Asset timeout must be finite and positive.")
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise AssetsError(
                "Asset transfer deadline exceeded; completion is unverified."
            )
        return remaining


def _identity(session: Any, assignment_id: Any) -> tuple[str, str]:
    for value in (session, assignment_id):
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise AssetsError("A verified session and assignment ID are required.")
    if session.startswith("-"):
        raise AssetsError("Invalid session name.")
    return session, assignment_id


def instance_namespace(assignment_id: str) -> str:
    return hashlib.sha256(assignment_id.encode("utf-8")).hexdigest()


def _relative(value: Any) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise AssetsError("Asset path is invalid.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in (".", "..") for part in path.parts)
        or value == "."
    ):
        raise AssetsError("Asset path must stay within its owned directory.")
    return path


def _plain_path(path: Path) -> Path:
    """Reject every existing symlink component, including a dangling final link."""
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        try:
            if stat.S_ISLNK(item.lstat().st_mode):
                raise AssetsError("Asset paths must not contain symlinks.")
        except FileNotFoundError:
            pass
    return path


def _directory(path: Path) -> Path:
    path = _plain_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise AssetsError("Asset directory is not a directory.")
    return path


def _open_regular(path: Path):
    path = _plain_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AssetsError("Asset source must be a regular file.")
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    return stream


def _digest(path: Path, deadline: Deadline, destination=None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path) as source:
        before = os.fstat(source.fileno())
        while True:
            deadline.remaining()
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if destination is not None:
                destination.write(chunk)
        after = os.fstat(source.fileno())

    def signature(value):
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if signature(before) != signature(after) or size != before.st_size:
        raise AssetsError("Asset changed while being read; transfer is unverified.")
    return digest.hexdigest(), size


def _checksum(record: dict[str, Any]) -> tuple[str, int]:
    digest, size = record.get("sha256"), record.get("bytes")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AssetsError("Asset manifest requires a SHA256 checksum.")
    if type(size) is not int or size < 0:
        raise AssetsError("Asset manifest requires a nonnegative byte count.")
    return digest, size


def _checked(path: Path, record: dict[str, Any], deadline: Deadline) -> None:
    if _digest(path, deadline) != _checksum(record):
        raise AssetsError("Asset size or SHA256 did not match; file was not published.")


def _json(path: Path) -> dict[str, Any]:
    with _open_regular(path) as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise AssetsError("Asset state exceeds its size limit.")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise AssetsError("Asset state must be an object.")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path = _plain_path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _lock(path: Path, deadline: Deadline):
    path = _plain_path(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.1, deadline.remaining()))
        yield
    finally:
        os.close(descriptor)


def _manifest(manifest: Any, session: str, assignment_id: str) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict) or (
        manifest.get("session") != session
        or manifest.get("assignment_id") != assignment_id
    ):
        raise AssetsError("Asset manifest belongs to a different or unknown instance.")
    entries = manifest.get("assets")
    if not isinstance(entries, list) or not 0 < len(entries) <= MAX_MANIFEST_ASSETS:
        raise AssetsError("Asset manifest needs a bounded, nonempty asset list.")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AssetsError("Asset manifest entry must be an object.")
        if (
            entry.get("session", session) != session
            or entry.get("assignment_id", assignment_id) != assignment_id
        ):
            raise AssetsError("Asset entry belongs to a different instance.")
        relative = str(_relative(entry.get("relative_path")))
        if relative in seen:
            raise AssetsError("Asset manifest has duplicate paths.")
        seen.add(relative)
        _checksum(entry)
    return entries


def _storage(base: Path, content_root: Path, drive_mount: Path) -> Path:
    state = _json(base / "services.json")
    value = state.get("storage_root")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise AssetsError("Owned launcher storage has not been configured.")
    root = _plain_path(Path(value))
    if state.get("ephemeral") is True:
        if not root.is_relative_to(content_root) or root.is_relative_to(drive_mount):
            raise AssetsError("Ephemeral assets are outside runtime storage.")
    elif state.get("ephemeral") is False:
        mydrive = _plain_path(drive_mount / "MyDrive")
        if (
            not os.path.ismount(drive_mount)
            or root == mydrive
            or not root.is_relative_to(mydrive)
        ):
            raise AssetsError(
                "Persistent assets require mounted, dedicated Drive storage."
            )
    else:
        raise AssetsError("Owned storage has no explicit persistence mode.")
    if not root.is_dir():
        raise AssetsError("Owned asset storage is missing.")
    return root


def remote_action(
    payload: dict[str, Any],
    *,
    base: Path | None = None,
    content_root: Path | None = None,
    drive_mount: Path | None = None,
) -> dict[str, Any]:
    """Dispatch asset-stage / asset-commit / asset-describe in the owned VM."""
    base = _plain_path(base or BASE)
    content_root = _plain_path(content_root or CONTENT_ROOT)
    drive_mount = _plain_path(drive_mount or DRIVE_MOUNT)
    session, assignment_id = _identity(
        payload.get("session"), payload.get("assignment_id")
    )
    marker = _json(base / "instance.json")
    if marker.get("session") != session or marker.get("assignment_id") != assignment_id:
        raise AssetsError("Runtime instance identity does not match the asset request.")
    seconds = payload.get("operation_seconds", 45.0)
    deadline = (
        Deadline(min(seconds, 45.0))
        if type(seconds) in (int, float)
        else Deadline(seconds)
    )
    root = _storage(base, content_root, drive_mount)
    action = payload.get("action")
    common = {"ok": True, "session": session, "assignment_id": assignment_id}
    if action == "asset-stage":
        checksum, size = _checksum(payload)
        original = payload.get("original_filename")
        if (
            not isinstance(original, str)
            or len(original) > 255
            or len(_relative(original).parts) != 1
        ):
            raise AssetsError("Input filename must be one plain filename.")
        # The readable suffix keeps the source recognisable; the complete SHA
        # prevents different files with the same original name from colliding.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", original)[-80:]
        # Keep inputs at the input-directory root: older native LoadImage menus
        # enumerate only that level. Both full hashes still scope every name.
        relative = f"launcher-{instance_namespace(assignment_id)}-{checksum}-{safe}"
        staging = _directory(base / "asset-staging")
        stage_id = uuid.uuid4().hex
        stage = staging / stage_id
        stage.mkdir(mode=0o700)
        record = {
            **common,
            "stage_id": stage_id,
            "original_filename": original,
            "relative_path": relative,
            "sha256": checksum,
            "bytes": size,
        }
        _write_json(stage / "record.json", record)
        return {**record, "status": "staged", "upload_path": str(stage / "payload")}
    if action == "asset-commit":
        stage_id = payload.get("stage_id")
        if not isinstance(stage_id, str) or not re.fullmatch(r"[0-9a-f]{32}", stage_id):
            raise AssetsError("Invalid upload staging ID.")
        stage = _plain_path(base / "asset-staging" / stage_id)
        record = _json(stage / "record.json")
        if (
            record.get("session") != session
            or record.get("assignment_id") != assignment_id
        ):
            raise AssetsError("Upload staging belongs to a different instance.")
        relative = _relative(record.get("relative_path"))
        if len(relative.parts) != 1 or not str(relative).startswith(
            f"launcher-{instance_namespace(assignment_id)}-"
        ):
            raise AssetsError("Upload staging has an invalid destination.")
        target = _plain_path(root / "input" / str(relative))
        _directory(target.parent)
        with _lock(base / "asset-commit.lock", deadline):
            if target.exists():
                _checked(target, record, deadline)
                reused = True
            else:
                temporary = target.with_name(".asset-" + uuid.uuid4().hex + ".partial")
                try:
                    with temporary.open("xb") as output:
                        os.chmod(temporary, 0o600)
                        result = _digest(stage / "payload", deadline, output)
                        output.flush()
                        os.fsync(output.fileno())
                    if result != _checksum(record):
                        raise AssetsError(
                            "Uploaded size or SHA256 did not match; input was not published."
                        )
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                reused = False
        # Keep the small receipt so an interrupted client can repeat commit.
        (stage / "payload").unlink(missing_ok=True)
        return {
            **record,
            "status": "verified",
            "filename": target.name,
            "comfyui_name": str(relative),
            "path": str(target),
            "type": "input",
            "verification": "sha256",
            "reused": reused,
        }
    if action == "asset-describe":
        entries = _manifest(payload.get("manifest"), session, assignment_id)
        records = []
        for entry in entries:
            path = _plain_path(root / "output" / str(_relative(entry["relative_path"])))
            _checked(path, entry, deadline)
            records.append(
                {
                    **entry,
                    "filename": path.name,
                    "remote_path": str(path),
                    "session": session,
                    "assignment_id": assignment_id,
                    "type": "output",
                }
            )
        return {**common, "status": "verified", "assets": records}
    raise AssetsError("Unsupported asset operation.")


class AssetsClient:
    """Bounded transfers through existing colabctl transports, with no retries.

    ``identity_check(session)`` must query the provider and return its current
    assignment ID. ``run_colab(arguments, timeout)`` and
    ``execute_remote(session, payload, timeout)`` use the existing bridge forms.
    """

    def __init__(
        self,
        session: str,
        assignment_id: str,
        run_colab: Callable,
        execute_remote: Callable,
        identity_check: Callable,
        timeout: float = 120.0,
    ):
        self.session, self.assignment_id = _identity(session, assignment_id)
        self.run_colab = run_colab
        self.execute_remote = execute_remote
        self.identity_check = identity_check
        self.timeout = timeout

    def _check_identity(self) -> None:
        if self.identity_check(self.session) != self.assignment_id:
            raise AssetsError(
                "Session now identifies a different instance; transfer stopped."
            )

    def _remote(self, action: str, deadline: Deadline, **payload) -> dict[str, Any]:
        self._check_identity()
        result = self.execute_remote(
            self.session,
            {
                **payload,
                "action": action,
                "session": self.session,
                "assignment_id": self.assignment_id,
                "operation_seconds": min(45.0, deadline.remaining()),
            },
            deadline.remaining(),
        )
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("session") != self.session
            or result.get("assignment_id") != self.assignment_id
        ):
            raise AssetsError(
                "Remote asset result is unverified or belongs to another instance."
            )
        self._check_identity()
        return result

    def _transfer(
        self, action: str, source: str, destination: str, deadline: Deadline
    ) -> None:
        self._check_identity()
        result = self.run_colab(
            [action, "-s", self.session, source, destination], deadline.remaining()
        )
        if result.returncode != 0:
            raise AssetsError(
                "Official CLI asset transfer failed; completion is unverified."
            )
        self._check_identity()

    def upload(self, source: Path) -> dict[str, Any]:
        deadline = Deadline(self.timeout)
        source = Path(source)
        with tempfile.TemporaryDirectory(prefix="launcher-asset-upload-") as temporary:
            # Upload an immutable snapshot; never hash one version and transfer
            # another if the caller edits the original during a slow upload.
            snapshot = Path(temporary) / "payload"
            with snapshot.open("xb") as stream:
                os.chmod(snapshot, 0o600)
                sha256, size = _digest(source, deadline, stream)
            stage = self._remote(
                "asset-stage",
                deadline,
                sha256=sha256,
                bytes=size,
                original_filename=source.name,
            )
            remote_path = stage.get("upload_path")
            if not isinstance(remote_path, str) or not remote_path.startswith("/"):
                raise AssetsError("Remote upload staging path is invalid.")
            _relative(remote_path[1:])
            self._transfer("upload", str(snapshot), remote_path[1:], deadline)
            result = self._remote(
                "asset-commit", deadline, stage_id=stage.get("stage_id")
            )
            if _checksum(result) != (sha256, size):
                raise AssetsError(
                    "Uploaded result checksum differs from the local snapshot."
                )
            return result

    def collect(
        self,
        manifest: dict[str, Any],
        destination: Path,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        deadline = Deadline(self.timeout)
        entries = _manifest(manifest, self.session, self.assignment_id)
        job_id = (
            job_id
            or manifest.get("job_id")
            or manifest.get("request_id")
            or manifest.get("prompt_id")
        )
        if not isinstance(job_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", job_id
        ):
            raise AssetsError("Output collection requires a stable job ID.")
        described = self._remote("asset-describe", deadline, manifest=manifest)
        remote_entries = _manifest(described, self.session, self.assignment_id)
        by_path = {entry["relative_path"]: entry for entry in remote_entries}
        if set(by_path) != {entry["relative_path"] for entry in entries}:
            raise AssetsError("Remote assets differ from the requested manifest.")
        root = _directory(
            Path(destination) / instance_namespace(self.assignment_id) / job_id
        )
        records = []
        with _lock(root / ".collect.lock", deadline):
            for entry in entries:
                remote = by_path[entry["relative_path"]]
                if _checksum(remote) != _checksum(entry):
                    raise AssetsError(
                        "Remote asset checksum differs from its manifest."
                    )
                path = _relative(entry["relative_path"])
                # Preserve subfolders; include SHA in the basename so even a
                # revised output under the same job cannot replace older bytes.
                safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[-120:]
                target = _plain_path(
                    root / str(path.parent) / (entry["sha256"] + "_" + safe_name)
                )
                _directory(target.parent)
                if target.exists():
                    _checked(target, entry, deadline)
                    reused = True
                else:
                    remote_path = remote.get("remote_path")
                    if not isinstance(remote_path, str) or not remote_path.startswith(
                        "/"
                    ):
                        raise AssetsError("Remote output path is invalid.")
                    _relative(remote_path[1:])
                    partial = target.with_name(
                        "." + target.name + ".partial-" + uuid.uuid4().hex
                    )
                    try:
                        # Precreate privately; official download opens this path
                        # for writing. Each attempt owns a unique partial file.
                        with partial.open("xb"):
                            os.chmod(partial, 0o600)
                        self._transfer(
                            "download", remote_path[1:], str(partial), deadline
                        )
                        _checked(partial, entry, deadline)
                        os.replace(partial, target)
                    finally:
                        partial.unlink(missing_ok=True)
                    reused = False
                records.append(
                    {
                        **entry,
                        "filename": path.name,
                        "local_path": str(target),
                        "session": self.session,
                        "assignment_id": self.assignment_id,
                        "verification": "sha256",
                        "reused": reused,
                    }
                )
            self._check_identity()
            deadline.remaining()
            result = {
                "ok": True,
                "status": "verified",
                "session": self.session,
                "assignment_id": self.assignment_id,
                "job_id": job_id,
                "assets": records,
                "manifest_path": str(root / "manifest.json"),
            }
            _write_json(root / "manifest.json", result)
        return result
