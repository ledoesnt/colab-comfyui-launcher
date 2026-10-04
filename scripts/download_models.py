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
from pathlib import Path, PurePosixPath

CONTENT_ROOT = Path("/content")
DRIVE_MOUNT = CONTENT_ROOT / "drive"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "models" / "h3.json"
CHUNK_BYTES = 4 * 1024 * 1024


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


def load_manifest(path):
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
    if not isinstance(files, list) or not files:
        raise DownloadError("Manifest must contain a nonempty files list")
    paths = set()
    for item in files:
        if not isinstance(item, dict):
            raise DownloadError("Each model file must be an object")
        path = item.get("path")
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
            raise DownloadError("Model paths must be safe relative filenames")
        if path in paths:
            raise DownloadError("Duplicate model path in manifest")
        paths.add(path)
        if type(item.get("size_bytes")) is not int or item["size_bytes"] <= 0:
            raise DownloadError("Model size_bytes must be a positive integer")
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", item["sha256"]
        ):
            raise DownloadError("Model sha256 must be a lowercase SHA256 digest")
    for path in paths:
        if any(str(parent) in paths for parent in PurePosixPath(path).parents):
            raise DownloadError("Model file paths conflict with a parent directory")
    total = sum(item["size_bytes"] for item in files)
    if "total_size_bytes" in manifest and (
        type(manifest["total_size_bytes"]) is not int
        or manifest["total_size_bytes"] != total
    ):
        raise DownloadError("Manifest total_size_bytes does not match its files")
    return manifest


def models_root(path, ephemeral):
    drive = DRIVE_MOUNT.resolve()
    if ephemeral:
        root = (
            Path(path).expanduser().resolve()
            if path
            else (
                CONTENT_ROOT / "colab-comfyui-runtime" / "ephemeral-assets" / "models"
            ).resolve()
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
            Path(path).expanduser().resolve()
            if path
            else (mydrive / "colab-comfyui" / "models").resolve()
        )
        if root == mydrive.resolve() or not root.is_relative_to(mydrive.resolve()):
            raise DownloadError(
                "models-root must be a dedicated directory inside /content/drive/MyDrive"
            )
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


def verify_file(path, item, deadline):
    if file_size(path) != item["size_bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            deadline.remaining()
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
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


def download_file(root, repo, revision, item, deadline):
    final = root / item["path"]
    partial = final.with_name(final.name + ".partial")
    check_path(final, root)
    check_path(partial, root)
    if file_size(final) is not None:
        if not verify_file(final, item, deadline):
            raise DownloadError(
                "Existing model failed size/SHA256 verification; it was not overwritten"
            )
        return {"path": item["path"], "status": "skipped", "bytes": item["size_bytes"]}
    offset = file_size(partial) or 0
    if offset > item["size_bytes"]:
        raise DownloadError("Partial file is longer than the pinned model size")
    final.parent.mkdir(parents=True, exist_ok=True)
    check_path(final, root)
    check_path(partial, root)
    result_status = "resumed" if offset else "downloaded"
    if offset < item["size_bytes"]:
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{item['path']}"
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
            check_path(partial, root)
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
            flags |= os.O_APPEND if offset else os.O_TRUNC
            fd = os.open(partial, flags, 0o600)
            received = 0
            with os.fdopen(fd, "ab" if offset else "wb") as stream:
                while True:
                    deadline.remaining()
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    if received + len(chunk) > body_size:
                        raise DownloadError("Response body exceeds its pinned range")
                    stream.write(chunk)
                    received += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if received != body_size or offset + received != item["size_bytes"]:
                raise DownloadError(
                    "Download was incomplete; partial file retained for resume"
                )
    if not verify_file(partial, item, deadline):
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
    return {"path": item["path"], "status": result_status, "bytes": item["size_bytes"]}


def run(args):
    with time_budget(args.max_seconds) as deadline:
        manifest = load_manifest(args.manifest)
        root = models_root(args.models_root, args.ephemeral)
        with root_lock(root):
            results = [
                download_file(
                    root, manifest["repo_id"], manifest["revision"], item, deadline
                )
                for item in manifest["files"]
            ]
        return {
            "ok": True,
            "repo_id": manifest["repo_id"],
            "revision": manifest["revision"],
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
