#!/usr/bin/env python3
"""Prepare pinned local models by copying Drive or explicitly downloading to VM."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

try:
    import download_models as cache
except ModuleNotFoundError as error:
    if error.name != "download_models":
        raise
    # runtime.py also loads this module by absolute path, without scripts on sys.path.
    specification = importlib.util.spec_from_file_location(
        "launcher_model_cache", Path(__file__).with_name("download_models.py")
    )
    cache = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(cache)


def boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not value or len(value) > 128:
        raise cache.DownloadError("Cannot identify this runtime boot")
    return value


def local_models_root(path=None):
    expected = cache.CONTENT_ROOT / "colab-comfyui-runtime" / "models"
    selected = Path(path) if path else expected
    if selected != expected:
        raise cache.DownloadError(
            "Local models-root must be the dedicated runtime/models directory"
        )
    return cache.models_root(selected, True)


def local_cache_ready(root, manifest_path):
    """Small metadata checks only; intended for the service's pre-start guard."""
    try:
        expected = cache.CONTENT_ROOT / "colab-comfyui-runtime" / "models"
        if Path(root) != expected or not expected.is_dir():
            return False
        cache.check_path(expected, expected.parent)
        manifest = cache.load_manifest(manifest_path)
        receipts = cache.Receipts(
            expected, manifest["repo_id"], manifest["revision"], boot_id()
        )
        return all(
            receipts.matches(expected / item["path"], item)
            for item in manifest["files"]
        )
    except (cache.DownloadError, OSError, ValueError):
        return False


def copy_file(
    source_root,
    local_root,
    item,
    deadline,
    source_receipts,
    local_receipts,
    progress,
    verify_cache=False,
):
    source = source_root / item["path"]
    final = local_root / item["path"]
    partial = final.with_name(final.name + ".partial")
    for path, root in (
        (source, source_root),
        (final, local_root),
        (partial, local_root),
    ):
        cache.check_path(path, root)
    before = cache.file_identity(source)
    if not before or before["size"] != item["size_bytes"]:
        raise cache.DownloadError(
            "Drive cache is missing a pinned model or has the wrong size"
        )
    source_verified = source_receipts.matches(source, item)
    if verify_cache:
        if not cache.verify_file(source, item, deadline, progress):
            raise cache.DownloadError("Drive cache failed explicit SHA256 verification")
        source_receipts.record(source, item, expected=before)
        source_verified = True
    if cache.file_size(final) is not None:
        if local_receipts.matches(final, item) and source_verified:
            progress.update(
                item,
                "complete",
                item["size_bytes"],
                "skipped",
                force=True,
                verification="verified_receipt_metadata",
            )
            return {
                "path": item["path"],
                "status": "skipped",
                "bytes": item["size_bytes"],
                "verification": "verified_receipt_metadata",
                "source_verification": "full_sha256"
                if verify_cache
                else "verified_receipt_metadata",
            }
        if cache.file_size(final) != item["size_bytes"]:
            raise cache.DownloadError(
                "Existing local model has the wrong size; it was not overwritten"
            )
        local_before = cache.file_identity(final)
        if not cache.verify_file(final, item, deadline, progress):
            raise cache.DownloadError(
                "Existing local model failed SHA256 verification; it was not overwritten"
            )
        # A missing/changed Drive receipt cannot be manufactured from local bytes.
        # Validate the source once if it is needed to refresh provenance.
        if not source_verified:
            if not cache.verify_file(source, item, deadline, progress):
                raise cache.DownloadError("Drive cache failed SHA256 verification")
            source_receipts.record(source, item, expected=before)
        local_receipts.record(
            final,
            item,
            expected=local_before,
            source={"stat": {name: before[name] for name in ("size", "mtime_ns")}},
        )
        progress.update(
            item,
            "complete",
            item["size_bytes"],
            "skipped",
            force=True,
            verification="full_sha256",
        )
        return {
            "path": item["path"],
            "status": "skipped",
            "bytes": item["size_bytes"],
            "verification": "full_sha256",
            "source_verification": "verified_receipt_metadata"
            if source_verified and not verify_cache
            else "full_sha256",
        }
    final.parent.mkdir(parents=True, exist_ok=True)
    cache.check_path(partial, local_root)
    if (
        cache.file_size(partial) is not None
        and cache.file_size(partial) > item["size_bytes"]
    ):
        raise cache.DownloadError("Local partial is longer than the pinned model")
    digest, done = hashlib.sha256(), 0
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise cache.DownloadError("Drive model is not a regular file")
        if before != cache.stat_identity(os.fstat(source_fd)):
            raise cache.DownloadError("Drive model changed before copy")
        output_fd = os.open(
            partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with (
            os.fdopen(source_fd, "rb") as incoming,
            os.fdopen(output_fd, "wb") as outgoing,
        ):
            source_fd = None
            progress.update(item, "copy", 0, force=True)
            while True:
                deadline.remaining()
                chunk = incoming.read(cache.CHUNK_BYTES)
                if not chunk:
                    break
                done += len(chunk)
                if done > item["size_bytes"]:
                    raise cache.DownloadError(
                        "Drive model exceeded its pinned size during copy"
                    )
                outgoing.write(chunk)
                digest.update(chunk)
                progress.update(item, "copy", done)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            written_identity = cache.stat_identity(os.fstat(outgoing.fileno()))
    finally:
        if source_fd is not None:
            os.close(source_fd)
    if before != cache.file_identity(source):
        raise cache.DownloadError("Drive model changed during copy")
    if written_identity != cache.file_identity(partial):
        raise cache.DownloadError("Local partial changed during copy")
    if done != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
        raise cache.DownloadError(
            "Copied model failed size/SHA256 verification; no local model was published"
        )
    deadline.remaining()
    cache.check_path(final, local_root)
    cache.check_path(partial, local_root)
    if final.exists():
        raise cache.DownloadError(
            "Local model appeared during copy; it was not overwritten"
        )
    os.replace(partial, final)
    source_receipts.record(source, item, expected=before)
    local_receipts.record(
        final,
        item,
        source={"stat": {name: before[name] for name in ("size", "mtime_ns")}},
    )
    progress.update(
        item,
        "complete",
        done,
        "succeeded",
        force=True,
        verification="stream_sha256",
    )
    return {
        "path": item["path"],
        "status": "copied",
        "bytes": done,
        "verification": "stream_sha256",
        "source_verification": "stream_sha256",
    }


def prepare_ephemeral(args, manifest, local, deadline, progress):
    """Download into the one local model directory, with boot-bound receipts."""
    with cache.root_lock(local):
        receipts = cache.Receipts(
            local, manifest["repo_id"], manifest["revision"], boot_id()
        )
        results = []
        for item in manifest["files"]:
            final = local / item["path"]
            cache.check_path(final, local)
            if cache.file_size(final) is None and not getattr(
                args, "download_missing", False
            ):
                raise cache.DownloadError(
                    "Local model is missing; use --download-missing to download to VM"
                )
            results.append(
                cache.download_file(
                    local,
                    manifest["repo_id"],
                    manifest["revision"],
                    item,
                    deadline,
                    receipts,
                    getattr(args, "verify_cache", False),
                    progress,
                )
            )
    return results


def run(args):
    with cache.time_budget(args.max_seconds) as deadline:
        manifest = cache.load_manifest(args.manifest)
        ephemeral = getattr(args, "ephemeral", False)
        if ephemeral and args.cache_root is not None:
            raise cache.DownloadError(
                "--ephemeral cannot be combined with --cache-root"
            )
        source = None if ephemeral else cache.models_root(args.cache_root, False)
        local = local_models_root(getattr(args, "local_models_root", None))
        progress = cache.Progress(
            getattr(args, "progress_file", None), "prepare", manifest["files"]
        )
        try:
            if ephemeral:
                results = prepare_ephemeral(args, manifest, local, deadline, progress)
            else:
                results = prepare_drive(
                    args, manifest, source, local, deadline, progress
                )
            if not local_cache_ready(local, args.manifest):
                raise cache.DownloadError("Prepared local model receipts are not ready")
            progress.finish(True)
        except BaseException:
            progress.finish(False)
            raise
        return {
            "ok": True,
            "repo_id": manifest["repo_id"],
            "revision": manifest["revision"],
            "cache_root": None if ephemeral else str(source),
            "models_root": str(local),
            "ephemeral": ephemeral,
            "models_ready": True,
            "files": results,
            "total_size_bytes": sum(item["size_bytes"] for item in manifest["files"]),
        }


def prepare_drive(args, manifest, source, local, deadline, progress):
    with cache.root_lock(source), cache.root_lock(local):
        source_receipts = cache.Receipts(
            source, manifest["repo_id"], manifest["revision"]
        )
        local_receipts = cache.Receipts(
            local, manifest["repo_id"], manifest["revision"], boot_id()
        )
        results = []
        for item in manifest["files"]:
            source_file = source / item["path"]
            cache.check_path(source_file, source)
            cache_status = "existing"
            if cache.file_size(source_file) is None and getattr(
                args, "download_missing", False
            ):
                downloaded = cache.download_file(
                    source,
                    manifest["repo_id"],
                    manifest["revision"],
                    item,
                    deadline,
                    source_receipts,
                    False,
                    progress,
                )
                cache_status = downloaded["status"]
            prepared = copy_file(
                source,
                local,
                item,
                deadline,
                source_receipts,
                local_receipts,
                progress,
                getattr(args, "verify_cache", False),
            )
            prepared["cache_status"] = cache_status
            results.append(prepared)
    return results


def main(argv=None):
    parser = cache.JsonArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=cache.DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-root", type=Path, help="Pinned model cache under mounted Drive/MyDrive"
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="Prepare only VM models, without Drive or a second cache copy",
    )
    parser.add_argument(
        "--local-models-root",
        type=Path,
        help="Must be /content/colab-comfyui-runtime/models",
    )
    parser.add_argument("--max-seconds", type=float, default=1800)
    parser.add_argument(
        "--verify-cache",
        action="store_true",
        help="Force full SHA on Drive cache, or on VM models with --ephemeral",
    )
    parser.add_argument(
        "--download-missing",
        action="store_true",
        help="Explicitly download missing pinned files into Drive cache, or directly to VM with --ephemeral",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        help="Local runtime progress JSON (default: prepare-progress.json)",
    )
    try:
        result = run(parser.parse_args(argv))
    except cache.DownloadError as error:
        result = {"ok": False, "error": str(error)}
    except KeyboardInterrupt:
        result = {
            "ok": False,
            "error": "Model preparation interrupted; unfinished local partials retained",
        }
    except Exception as error:  # noqa: BLE001 - redact provider/filesystem details at CLI boundary.
        result = {
            "ok": False,
            "error": "Model preparation failed (" + type(error).__name__ + ")",
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
