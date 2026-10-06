#!/usr/bin/env python3
"""Explicit, bounded downloads of pinned model files; importing performs no I/O."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

CONTENT_ROOT = Path("/content")
DRIVE_MOUNT = CONTENT_ROOT / "drive"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "models" / "h3.json"
DEFAULT_EXTRA_MANIFEST = DEFAULT_MANIFEST.with_name("extra.json")
CHUNK_BYTES = 4 * 1024 * 1024
RECEIPT_NAME = ".verified-models.json"


class DownloadError(Exception):
    """An error whose message contains no credentials or redirected URLs."""


class Deadline:
    def __init__(self, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            raise DownloadError("max-seconds must be finite and positive")
        self.end = time.monotonic() + seconds

    def remaining(self):
        seconds = self.end - time.monotonic()
        if seconds <= 0:
            raise DownloadError("Overall download deadline exceeded")
        return seconds


@contextlib.contextmanager
def time_budget(seconds):
    """The Colab Linux process alarm also bounds blocked reads and filesystem I/O."""
    deadline = Deadline(seconds)

    def expired(_signum, _frame):
        raise DownloadError("Overall download deadline exceeded")

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    start = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield deadline
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            remaining = max(0.000001, old_timer[0] - (time.monotonic() - start))
            signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])


def validate_model_path(path, label="Model paths"):
    if (
        not isinstance(path, str)
        or not path
        or str(PurePosixPath(path)) != path
        or not all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
            for part in path.split("/")
        )
        or path.endswith(".partial")
    ):
        raise DownloadError(label + " must be safe relative filenames")


def validate_path_collisions(files):
    paths = set()
    for item in files:
        path = item["path"]
        if path in paths:
            raise DownloadError("Duplicate model destination path in manifests")
        paths.add(path)
    for path in paths:
        if any(str(parent) in paths for parent in PurePosixPath(path).parents):
            raise DownloadError("Model file paths conflict with a parent directory")


def load_manifest(path, allow_empty=False):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DownloadError("Cannot read a valid JSON manifest") from None
    if not isinstance(manifest, dict):
        raise DownloadError("Manifest must be a JSON object")
    repo = manifest.get("repo_id")
    revision = manifest.get("revision")
    if not isinstance(repo, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", repo
    ):
        raise DownloadError("repo_id must be an owner/repository name")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DownloadError("revision must be a pinned 40-character commit hash")
    files = manifest.get("files")
    if not isinstance(files, list) or (not files and not allow_empty):
        raise DownloadError("Manifest must contain a nonempty files list")
    for item in files:
        if not isinstance(item, dict):
            raise DownloadError("Each model file must be an object")
        validate_model_path(item.get("path"))
        if "source_path" in item:
            validate_model_path(item["source_path"], "Model source paths")
        if any(key in item for key in ("url", "repo_id", "revision")):
            raise DownloadError(
                "File sources use the pinned manifest repo_id/revision; use another manifest for another repository"
            )
        if type(item.get("size_bytes")) is not int or item["size_bytes"] <= 0:
            raise DownloadError("Model size_bytes must be a positive integer")
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", item["sha256"]
        ):
            raise DownloadError("Model sha256 must be a lowercase SHA256 digest")
    validate_path_collisions(files)
    total = sum(item["size_bytes"] for item in files)
    if "total_size_bytes" in manifest and (
        type(manifest["total_size_bytes"]) is not int
        or manifest["total_size_bytes"] != total
    ):
        raise DownloadError("Manifest total_size_bytes does not match its files")
    return manifest


def default_extra_manifests():
    """Use the deployment's exact selection, or discover local checkout lists."""
    directory = DEFAULT_MANIFEST.parent
    registry = directory / "selected-extra-manifests.json"
    if registry.exists() or registry.is_symlink():
        if registry.is_symlink() or not registry.is_file():
            raise DownloadError("Additional manifest registry must be a regular file")
        if registry.stat().st_size > 1024 * 1024:
            raise DownloadError("Additional manifest registry is unexpectedly large")
        try:
            with os.fdopen(
                os.open(registry, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="utf-8"
            ) as stream:
                value = json.load(stream)
        except (OSError, ValueError):
            raise DownloadError(
                "Cannot read a valid additional manifest registry"
            ) from None
        if not (
            isinstance(value, dict)
            and type(value.get("version")) is int
            and value["version"] == 1
            and isinstance(value.get("files"), list)
        ):
            raise DownloadError(
                "Additional manifest registry needs version 1 and a files list"
            )
        names = value["files"]
        if not all(
            isinstance(name, str)
            and re.fullmatch(r"extra(?:-[A-Za-z0-9._-]+)?\.json", name)
            for name in names
        ) or len(set(names)) != len(names):
            raise DownloadError(
                "Additional manifest registry has invalid or duplicate names"
            )
        paths = [directory / name for name in sorted(names)]
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise DownloadError(
                "Selected additional manifests must be existing regular files, without symlinks"
            )
        return paths
    paths = [DEFAULT_EXTRA_MANIFEST] if DEFAULT_EXTRA_MANIFEST.exists() else []
    paths.extend(sorted(directory.glob("extra-*.json")))
    return paths


def load_manifests(path, extra_manifest_paths=()):
    """Merge default extra JSON files and explicitly selected pinned sources."""
    selected = [load_manifest(path)]
    extra_paths = []
    if Path(path).resolve() == DEFAULT_MANIFEST.resolve():
        extra_paths.extend(default_extra_manifests())
    extra_paths.extend(extra_manifest_paths)
    visited = set()
    for extra in extra_paths:
        identity = Path(extra).resolve()
        if identity in visited:
            continue
        visited.add(identity)
        value = load_manifest(extra, allow_empty=True)
        if value["files"]:
            selected.append(value)
    manifest = dict(selected[0])
    manifest["files"] = [
        dict(item, repo_id=value["repo_id"], revision=value["revision"])
        for value in selected
        for item in value["files"]
    ]
    validate_path_collisions(manifest["files"])
    manifest["total_size_bytes"] = sum(item["size_bytes"] for item in manifest["files"])
    manifest["sources"] = [
        {
            "repo_id": value["repo_id"],
            "revision": value["revision"],
            "file_count": len(value["files"]),
        }
        for value in selected
    ]
    return manifest


def models_root(path, ephemeral):
    drive = DRIVE_MOUNT.resolve()
    if ephemeral:
        root = (
            Path(path).expanduser().absolute()
            if path
            else (
                CONTENT_ROOT / "colab-comfyui-runtime" / "ephemeral-assets" / "models"
            )
        )
        content = CONTENT_ROOT.resolve()
        if (
            root == content
            or not root.is_relative_to(content)
            or root.is_relative_to(drive)
        ):
            raise DownloadError(
                "Ephemeral models-root must be a dedicated directory under /content, outside Drive"
            )
    else:
        if not os.path.ismount(DRIVE_MOUNT):
            raise DownloadError(
                "Drive is not mounted; finish colab drivemount consent first"
            )
        mydrive = DRIVE_MOUNT / "MyDrive"
        if mydrive.is_symlink() or not mydrive.is_dir():
            raise DownloadError("Mounted Drive has no real MyDrive directory")
        root = (
            Path(path).expanduser().absolute()
            if path
            else (mydrive / "colab-comfyui" / "models")
        )
        if root == mydrive.resolve() or not root.is_relative_to(mydrive.resolve()):
            raise DownloadError(
                "models-root must be a dedicated directory inside /content/drive/MyDrive"
            )
    if ".." in root.parts:
        raise DownloadError("Model root must not contain parent traversal")
    for part in (root, *root.parents):
        if part.is_symlink():
            raise DownloadError("Model roots must not contain symlinks")
        if part == CONTENT_ROOT:
            break
    root.mkdir(parents=True, exist_ok=True)
    return root


def check_path(path, root):
    if not path.is_relative_to(root):
        raise DownloadError("Model path escapes models-root")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise DownloadError("Model paths must not contain symlinks")
        if part == root:
            break
    if not path.resolve().is_relative_to(root):
        raise DownloadError("Model path resolves outside models-root")


def file_size(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise DownloadError("Existing model or partial path is not a regular file")
    return info.st_size


def file_identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise DownloadError("Model metadata is not a regular file")
    return stat_identity(info)


def stat_identity(info):
    return {
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def written_partial_matches(path, expected):
    """Drive FUSE finalizes writable timestamps on close; ownership stays fixed.

    This is only for an owned download write, never a read/hash/receipt skip.
    Ordinary VM files retain strict timestamp checks. Stream SHA and size are
    separately checked before publishing, and receipts use post-close metadata.
    """
    current = file_identity(path)
    if current == expected:
        return True
    if (
        current is not None
        and path.is_relative_to(DRIVE_MOUNT)
        and os.path.ismount(DRIVE_MOUNT)
    ):
        return all(current[key] == expected[key] for key in ("size", "device", "inode"))
    return False


def atomic_json(path, value, root, durable=True):
    check_path(path, root)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    check_path(temporary, root)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        check_path(path, root)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Progress:
    """Throttled local snapshots; no per-chunk Drive metadata writes or fsync."""

    def __init__(self, path, operation, items):
        runtime = CONTENT_ROOT / "colab-comfyui-runtime"
        self.path = (
            Path(path)
            if path
            else runtime
            / (
                "model-progress.json"
                if operation == "download"
                else "prepare-progress.json"
            )
        )
        if (
            not self.path.is_absolute()
            or self.path.parent != runtime
            or self.path.suffix != ".json"
        ):
            raise DownloadError(
                "Progress file must be a JSON file directly inside the dedicated runtime"
            )
        for part in (runtime, *runtime.parents):
            if part.is_symlink():
                raise DownloadError("Progress path must not contain symlinks")
        runtime.mkdir(parents=True, exist_ok=True)
        check_path(self.path, runtime)
        self.root = runtime
        self.started = time.monotonic()
        self.updated = -math.inf
        self.current_started = self.started
        self.current_path = None
        self.current_phase = None
        self.phase_started = self.started
        self.phase_base = 0
        self.value = {
            "version": 1,
            "operation": operation,
            "status": "running",
            "files": [
                {
                    "path": item["path"],
                    "phase": "pending",
                    "status": "pending",
                    "done_bytes": 0,
                    "total_bytes": item["size_bytes"],
                    "rate_bytes_per_second": 0,
                    "elapsed_seconds": 0,
                    "verification": None,
                }
                for item in items
            ],
        }

    def update(
        self, item, phase, done, status="running", force=False, verification=None
    ):
        now = time.monotonic()
        if self.current_path != item["path"]:
            self.current_started = now
            self.current_path = item["path"]
            self.current_phase = None
            force = True
        elapsed = now - self.current_started
        entry = next(
            value for value in self.value["files"] if value["path"] == item["path"]
        )
        if phase != self.current_phase:
            self.current_phase = phase
            self.phase_started = now
            self.phase_base = done
        phase_elapsed = now - self.phase_started
        rate = (
            entry["rate_bytes_per_second"]
            if phase == "complete"
            else (
                round((done - self.phase_base) / phase_elapsed, 3)
                if phase_elapsed > 0
                else 0
            )
        )
        entry.update(
            phase=phase,
            status=status,
            done_bytes=done,
            rate_bytes_per_second=rate,
            elapsed_seconds=round(elapsed, 3),
        )
        if verification is not None:
            entry["verification"] = verification
        elif phase != "complete":
            entry["verification"] = None
        self.value.update(
            file=item["path"],
            path=item["path"],
            phase=phase,
            done_bytes=done,
            total_bytes=item["size_bytes"],
            rate_bytes_per_second=entry["rate_bytes_per_second"],
            elapsed_seconds=round(now - self.started, 3),
        )
        if force or now - self.updated >= 1:
            atomic_json(self.path, self.value, self.root, durable=False)
            self.updated = now

    def finish(self, ok):
        self.value["status"] = "succeeded" if ok else "failed"
        if not ok:
            for entry in self.value["files"]:
                if entry["path"] == self.current_path and entry["status"] == "running":
                    entry["status"] = "failed"
        self.value["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        atomic_json(self.path, self.value, self.root, durable=False)


class Receipts:
    """Prior full SHA verification plus current metadata, never a fresh SHA claim."""

    def __init__(self, root, repo, revision, boot_id=None):
        self.root, self.repo, self.revision, self.boot_id = (
            root,
            repo,
            revision,
            boot_id,
        )
        self.path = root / RECEIPT_NAME
        check_path(self.path, root)
        self.records = {}
        if file_size(self.path) is not None:
            if file_size(self.path) > 1024 * 1024:
                raise DownloadError("Model receipt is unexpectedly large")
            try:
                with os.fdopen(
                    os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW),
                    "r",
                    encoding="utf-8",
                ) as stream:
                    saved = json.load(stream)
                if saved.get("version") == 1 and isinstance(saved.get("records"), dict):
                    self.records = saved["records"]
            except (ValueError, AttributeError):
                # Invalid metadata cannot authorise a skip; re-hash the actual model.
                pass

    def key(self, item):
        identity = [
            item.get("repo_id", self.repo),
            item.get("revision", self.revision),
            item["path"],
            item["size_bytes"],
            item["sha256"],
        ]
        if item.get("source_path", item["path"]) != item["path"]:
            identity.append(item["source_path"])
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def matches(self, path, item):
        check_path(path, self.root)
        current = file_identity(path)
        saved = self.records.get(self.key(item))
        if (
            not current
            or current["size"] != item["size_bytes"]
            or not isinstance(saved, dict)
        ):
            return False
        if (
            saved.get("sha256") != item["sha256"]
            or saved.get("boot_id") != self.boot_id
        ):
            return False
        metadata = (
            current
            if self.boot_id is not None
            else {name: current[name] for name in ("size", "mtime_ns")}
        )
        return saved.get("stat") == metadata

    def record(self, path, item, expected=None, source=None):
        check_path(path, self.root)
        current = file_identity(path)
        if not current or current["size"] != item["size_bytes"]:
            raise DownloadError("Verified model changed before receipt publication")
        if expected is not None and current != expected:
            raise DownloadError(
                "Verified model metadata changed before receipt publication"
            )
        metadata = (
            current
            if self.boot_id is not None
            else {name: current[name] for name in ("size", "mtime_ns")}
        )
        self.records[self.key(item)] = {
            "sha256": item["sha256"],
            "stat": metadata,
            "boot_id": self.boot_id,
            "verified_at": time.time(),
        }
        if source is not None:
            self.records[self.key(item)]["source"] = source
        atomic_json(self.path, {"version": 1, "records": self.records}, self.root)


def hash_file(path, deadline, item, progress=None, total=None):
    before = file_identity(path)
    digest, done = hashlib.sha256(), 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DownloadError("Model is not a regular file")
        if before != stat_identity(os.fstat(stream.fileno())):
            raise DownloadError("Model changed before hash verification")
        if progress:
            progress.update(item, "hash", 0, force=True)
        while True:
            deadline.remaining()
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress.update(item, "hash", done)
    if before != file_identity(path) or done != (before or {}).get("size"):
        raise DownloadError("Model changed during hash verification")
    if total is not None and done != total:
        raise DownloadError("Partial changed before resumed download")
    return digest


def verify_file(path, item, deadline, progress=None):
    if file_size(path) != item["size_bytes"]:
        return False
    digest = hash_file(path, deadline, item, progress)
    return digest.hexdigest() == item["sha256"]


@contextlib.contextmanager
def root_lock(root):
    path = root / ".download.lock"
    check_path(path, root)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise DownloadError("Download lock is not a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DownloadError(
                "Another model download owns this models-root"
            ) from None
        yield


def download_file(
    root,
    repo,
    revision,
    item,
    deadline,
    receipts=None,
    verify_cache=False,
    progress=None,
):
    receipts = receipts or Receipts(root, repo, revision)
    final = root / item["path"]
    partial = final.with_name(final.name + ".partial")
    check_path(final, root)
    check_path(partial, root)
    if file_size(final) is not None:
        if not verify_cache and receipts.matches(final, item):
            verification = "verified_receipt_metadata"
        else:
            before = file_identity(final)
            if not verify_file(final, item, deadline, progress):
                raise DownloadError(
                    "Existing model failed size/SHA256 verification; it was not overwritten"
                )
            receipts.record(final, item, expected=before)
            verification = "full_sha256"
        if progress:
            progress.update(
                item,
                "complete",
                item["size_bytes"],
                "skipped",
                force=True,
                verification=verification,
            )
        return {
            "path": item["path"],
            "status": "skipped",
            "bytes": item["size_bytes"],
            "verification": verification,
        }
    offset = file_size(partial) or 0
    if offset > item["size_bytes"]:
        raise DownloadError("Partial file is longer than the pinned model size")
    final.parent.mkdir(parents=True, exist_ok=True)
    check_path(final, root)
    check_path(partial, root)
    result_status = "resumed" if offset else "downloaded"
    digest = None
    if offset < item["size_bytes"]:
        source_path = item.get("source_path", item["path"])
        source_repo = item.get("repo_id", repo)
        source_revision = item.get("revision", revision)
        url = f"https://huggingface.co/{source_repo}/resolve/{source_revision}/{source_path}"
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": "colab-comfyui-launcher/1",
        }
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            response = urllib.request.urlopen(
                request, timeout=min(30, deadline.remaining())
            )
        except urllib.error.HTTPError as error:
            error.close()
            raise DownloadError(f"Model server returned HTTP {error.code}") from None
        except (urllib.error.URLError, OSError):
            raise DownloadError("Model server connection failed") from None
        with response:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise DownloadError("Model server redirected to a non-HTTPS URL")
            status = response.status
            if (
                response.headers.get("Content-Encoding", "identity").lower()
                != "identity"
            ):
                raise DownloadError("Model server returned encoded content")
            if status == 206:
                match = re.fullmatch(
                    r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                    response.headers.get("Content-Range", ""),
                )
                if not match:
                    raise DownloadError("Partial response has no valid Content-Range")
                start, end, total = map(int, match.groups())
                if (
                    start != offset
                    or total != item["size_bytes"]
                    or not start <= end < total
                ):
                    raise DownloadError(
                        "Partial response does not match the requested range"
                    )
                body_size = end - start + 1
            elif status == 200:
                body_size = item["size_bytes"]
                if offset:
                    offset = 0
                    result_status = "restarted"
            else:
                raise DownloadError("Model server returned an unsupported HTTP status")
            length = response.headers.get("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) != body_size
            ):
                raise DownloadError(
                    "Response Content-Length does not match the pinned range"
                )
            # Resume hashes the retained prefix once; new bytes are hashed while
            # downloading. A 200 restart never reads the discarded prefix.
            digest = (
                hash_file(partial, deadline, item, progress, offset)
                if offset
                else hashlib.sha256()
            )
            check_path(partial, root)
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
            flags |= os.O_APPEND if offset else os.O_TRUNC
            fd = os.open(partial, flags, 0o600)
            received = 0
            with os.fdopen(fd, "ab" if offset else "wb") as stream:
                if (
                    not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                    or os.fstat(stream.fileno()).st_size != offset
                ):
                    raise DownloadError("Partial changed before download write")
                if progress:
                    progress.update(item, "download", offset, force=True)
                while True:
                    deadline.remaining()
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    if received + len(chunk) > body_size:
                        raise DownloadError("Response body exceeds its pinned range")
                    stream.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress.update(item, "download", offset + received)
                stream.flush()
                os.fsync(stream.fileno())
                written_identity = stat_identity(os.fstat(stream.fileno()))
                if not written_partial_matches(partial, written_identity):
                    raise DownloadError("Partial changed during download")
            if not written_partial_matches(partial, written_identity):
                raise DownloadError("Partial changed during download")
            if received != body_size or offset + received != item["size_bytes"]:
                raise DownloadError(
                    "Download was incomplete; partial file retained for resume"
                )
    if digest is None:
        digest = hash_file(partial, deadline, item, progress)
    if file_size(partial) != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
        raise DownloadError(
            "Partial file failed size/SHA256 verification; no model was published"
        )
    deadline.remaining()
    check_path(final, root)
    check_path(partial, root)
    if final.exists():
        raise DownloadError(
            "Model appeared during download; existing file was not overwritten"
        )
    os.replace(partial, final)
    receipts.record(final, item)
    if progress:
        progress.update(
            item,
            "complete",
            item["size_bytes"],
            "succeeded",
            force=True,
            verification="stream_sha256"
            if offset < item["size_bytes"]
            else "full_sha256",
        )
    return {
        "path": item["path"],
        "status": result_status,
        "bytes": item["size_bytes"],
        "verification": "stream_sha256"
        if offset < item["size_bytes"]
        else "full_sha256",
    }


def run(args):
    with time_budget(args.max_seconds) as deadline:
        manifest = load_manifests(args.manifest, getattr(args, "extra_manifest", ()))
        root = models_root(args.models_root, args.ephemeral)
        progress = Progress(
            getattr(args, "progress_file", None), "download", manifest["files"]
        )
        try:
            with root_lock(root):
                receipts = Receipts(root, manifest["repo_id"], manifest["revision"])
                results = [
                    download_file(
                        root,
                        manifest["repo_id"],
                        manifest["revision"],
                        item,
                        deadline,
                        receipts,
                        getattr(args, "verify_cache", False),
                        progress,
                    )
                    for item in manifest["files"]
                ]
            progress.finish(True)
        except BaseException:
            progress.finish(False)
            raise
        return {
            "ok": True,
            "repo_id": manifest["repo_id"],
            "revision": manifest["revision"],
            "sources": manifest["sources"],
            "models_root": str(root),
            "ephemeral": args.ephemeral,
            "files": results,
            "total_size_bytes": sum(item["size_bytes"] for item in manifest["files"]),
        }


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise DownloadError("Invalid command arguments; see --help")


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--extra-manifest",
        type=Path,
        action="append",
        default=[],
        help="Append a pinned model manifest; repeat for additional repositories",
    )
    parser.add_argument(
        "--models-root",
        type=Path,
        help="Default: /content/drive/MyDrive/colab-comfyui/models; --ephemeral uses VM assets/models",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="Explicitly use a dedicated /content directory outside Drive",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=1800,
        help="Overall deadline including hash checks (default: 1800)",
    )
    parser.add_argument(
        "--verify-cache",
        action="store_true",
        help="Re-read complete cached files and verify SHA256 instead of prior verified receipts",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        help="Local runtime progress JSON (default: model-progress.json)",
    )
    try:
        result = run(parser.parse_args(argv))
    except DownloadError as error:
        result = {"ok": False, "error": str(error)}
    except KeyboardInterrupt:
        result = {
            "ok": False,
            "error": "Download interrupted; unfinished partials retained",
        }
    except Exception as error:  # noqa: BLE001 - CLI boundary redacts provider errors.
        # urllib/socket/filesystem exceptions can contain signed URLs or credentials.
        result = {"ok": False, "error": f"Download failed ({type(error).__name__})"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
