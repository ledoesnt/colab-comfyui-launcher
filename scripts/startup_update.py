"""Opt-in source updates and verified, recoverable launcher snapshots.

No account, credential or Colab API access. Unknown/dirty/live versions stop.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path


class UpdateError(RuntimeError):
    pass


class Lease:
    def __init__(self, fd=None):
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def git(root, *args, optional=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=90,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("Git update timed out; launcher not started.") from exc
    if r.returncode and not optional:
        raise UpdateError(
            f"git {args[0]} failed (exit {r.returncode}); no fallback to old code."
        )
    return r


def text(root, *args):
    return git(root, *args).stdout.decode().strip()


def lock_path(root):
    root = Path(root).resolve()
    if (root / ".git").exists():
        p = Path(text(root, "rev-parse", "--git-path", "launcher-source.lock"))
        return p if p.is_absolute() else root / p
    if (root / ".launcher-snapshot.json").exists():
        return root.parent / ("." + root.name + ".source.lock")
    return None


def acquire(path, exclusive):
    if path is None:
        return Lease()
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise UpdateError(
            "Launcher version is in use; stop the owned task before updating."
        ) from exc
    return Lease(fd)


def read_version_lease(root):
    """Held by persistent guards too, so UI quit cannot permit a hot update."""
    return acquire(lock_path(root), False)


def prepare_source(root):
    root = Path(root).resolve()
    if not (root / ".git").exists():
        raise UpdateError("Source is not the configured Git checkout.")
    lease = acquire(lock_path(root), True)
    try:
        if text(root, "rev-parse", "--show-toplevel") != str(root):
            raise UpdateError("Source checkout root changed.")
        dirty = text(root, "status", "--porcelain", "--untracked-files=normal")
        if dirty:
            raise UpdateError(
                "Dirty launcher checkout; preserve and reconcile changes before startup:\n"
                + dirty
            )
        branch = text(root, "branch", "--show-current")
        expected = (
            git(root, "config", "--get", "launcher.updateBranch", optional=True)
            .stdout.decode()
            .strip()
        )
        if not branch or (expected and branch != expected):
            raise UpdateError(
                "Unexpected/detached source branch; no update or startup."
            )
        upstream = text(
            root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
        )
        before = text(root, "rev-parse", "HEAD")
        git(root, "pull", "--ff-only")
        head = text(root, "rev-parse", "HEAD")
        if head != text(root, "rev-parse", upstream):
            raise UpdateError(
                "Local source differs from upstream after pull; no startup."
            )
        if text(root, "status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError("Source changed during update; no startup.")
        fcntl.flock(lease.fd, fcntl.LOCK_SH)
        return {
            "commit": head,
            "changed": before != head,
            "branch": branch,
            "source": str(root),
        }, lease
    except BaseException:
        lease.close()
        raise


def sha(path):
    with Path(path).open("rb") as f:
        return (
            hashlib.file_digest(f, "sha256").hexdigest()
            if hasattr(hashlib, "file_digest")
            else hashlib.sha256(f.read()).hexdigest()
        )


def snapshot_names(root):
    result = set()
    for p in Path(root).rglob("*"):
        rel = p.relative_to(root)
        if "__pycache__" in rel.parts or p.suffix in (".pyc", ".pyo"):
            continue
        if p.is_symlink():
            raise UpdateError(
                "Snapshot contains a symlink; preserve it for manual reconciliation."
            )
        if p.is_file() and rel.as_posix() != ".launcher-snapshot.json":
            result.add(rel.as_posix())
    return result


def verify_snapshot(root, manifest):
    files = manifest["files"]
    if snapshot_names(root) != set(files):
        raise UpdateError("Snapshot file set drifted; no replacement.")
    for name, expected in files.items():
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or sha(Path(root) / p) != expected:
            raise UpdateError("Snapshot hash mismatch; no replacement: " + name)


def sync_snapshot(source, snapshot, commit, *, expected_manifest=None, run_lock=None):
    """Source lease must already be held. Legacy snapshots need an explicit hash manifest."""
    source, snapshot = Path(source).resolve(), Path(snapshot).resolve()
    if source == snapshot or source in snapshot.parents or snapshot in source.parents:
        raise UpdateError("Code snapshot and source checkout must not overlap.")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    lease = acquire(snapshot.parent / ("." + snapshot.name + ".source.lock"), True)
    try:
        with acquire(Path(run_lock) if run_lock else None, True):
            current = snapshot / ".launcher-snapshot.json"
            old = (
                json.loads(current.read_text())
                if current.exists()
                else expected_manifest
            )
            if snapshot.exists():
                if old is None:
                    raise UpdateError(
                        "Legacy snapshot needs its approved file hashes before replacement."
                    )
                verify_snapshot(snapshot, old)
                if old.get("commit") == commit:
                    fcntl.flock(lease.fd, fcntl.LOCK_SH)
                    return {"commit": commit, "reused": True, "backup": None}, lease
            with tempfile.TemporaryDirectory(
                prefix=".launcher-stage-", dir=snapshot.parent
            ) as temporary:
                stage = Path(temporary) / "snapshot"
                stage.mkdir()
                archive = git(source, "archive", "--format=tar", commit).stdout
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    for m in tar.getmembers():
                        p = Path(m.name)
                        if (
                            p.is_absolute()
                            or ".." in p.parts
                            or not (m.isdir() or m.isfile())
                        ):
                            raise UpdateError("Unsafe Git archive member.")
                        dest = stage / p
                        if m.isdir():
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with tar.extractfile(m) as src, dest.open("wb") as out:
                                out.write(src.read())
                            dest.chmod(m.mode & 0o777)
                tree = git(source, "ls-tree", "-rz", "--full-tree", commit).stdout
                hashes = {}
                for entry in tree.split(b"\0"):
                    if not entry:
                        continue
                    header, name = entry.split(b"\t", 1)
                    _, kind, blob = header.split()
                    name = name.decode()
                    p = stage / name
                    if kind != b"blob" or not p.is_file():
                        raise UpdateError("Git archive/tree mismatch.")
                    data = p.read_bytes()
                    actual = hashlib.sha1(
                        b"blob " + str(len(data)).encode() + b"\0" + data
                    ).hexdigest()
                    if actual != blob.decode():
                        raise UpdateError("Git blob mismatch: " + name)
                    hashes[name] = hashlib.sha256(data).hexdigest()
                manifest = {
                    "schema": 1,
                    "source": str(source),
                    "commit": commit,
                    "files": hashes,
                    "run_lock": str(run_lock) if run_lock else None,
                }
                (stage / ".launcher-snapshot.json").write_text(
                    json.dumps(manifest, indent=2) + "\n"
                )
                verify_snapshot(stage, manifest)
                backup = snapshot.with_name(
                    snapshot.name + ".backup-" + str(time.time_ns())
                )
                moved = False
                if snapshot.exists():
                    snapshot.rename(backup)
                    moved = True
                try:
                    stage.rename(snapshot)
                except BaseException:
                    if moved and not snapshot.exists():
                        backup.rename(snapshot)
                    raise
            fcntl.flock(lease.fd, fcntl.LOCK_SH)
            return {
                "commit": commit,
                "reused": False,
                "backup": str(backup) if moved else None,
            }, lease
    except BaseException:
        lease.close()
        raise


def prepare_launch(source, *, snapshot=None, expected_manifest=None, run_lock=None):
    result, source_lease = prepare_source(source)
    try:
        leases = contextlib.ExitStack()
        leases.enter_context(source_lease)
        if snapshot:
            snap, lease = sync_snapshot(
                source,
                snapshot,
                result["commit"],
                expected_manifest=expected_manifest,
                run_lock=run_lock,
            )
            leases.enter_context(lease)
            result["snapshot"] = snap
        return result, leases
    except BaseException:
        source_lease.close()
        raise


def guard_entry(root, argv):
    """Opt-in applies only to this launcher checkout; offline help/demo never starts a runtime."""
    if any(a in argv for a in ("--demo", "--help", "-h")):
        return None
    root = Path(root).resolve()
    manifest_path = root / ".launcher-snapshot.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    source = Path(manifest["source"]) if manifest else root
    if not (source / ".git").exists() and manifest is None:
        return None
    enabled = git(
        source, "config", "--bool", "--get", "launcher.updateBeforeStart", optional=True
    )
    if enabled.returncode not in (0, 1):
        raise UpdateError("Cannot verify launcher update preference; no startup.")
    if enabled.stdout.strip() != b"true":
        return None
    result, lease = prepare_launch(
        source,
        snapshot=root if manifest else None,
        run_lock=manifest.get("run_lock") if manifest else None,
    )
    if result["changed"] or result.get("snapshot", {}).get("reused") is False:
        lease.close()
        os.execv(sys.executable, [sys.executable, sys.argv[0], *argv])
    return lease
