"""Local, persistent model choices and bounded public Hugging Face metadata lookup.

This module fetches metadata only. Preparing weights remains a separate action.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class CatalogError(ValueError):
    """A model configuration could not be safely changed."""


def parse_url(value: str) -> tuple[str, str, str]:
    url = urllib.parse.urlsplit(value)
    if (
        url.scheme != "https"
        or url.netloc != "huggingface.co"
        or url.query
        or url.fragment
    ):
        raise CatalogError(
            "Use a public https://huggingface.co file URL without query parameters."
        )
    parts = urllib.parse.unquote(url.path).strip("/").split("/")
    if len(parts) < 5 or parts[2] not in ("resolve", "blob"):
        raise CatalogError(
            "Use a Hugging Face file URL containing /blob/REVISION/ or /resolve/REVISION/."
        )
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in parts):
        raise CatalogError(
            "The repository, revision and file must use safe relative names."
        )
    source = "/".join(parts[4:])
    if source.endswith(".partial"):
        raise CatalogError("A partial download cannot be added as a model.")
    return "/".join(parts[:2]), parts[3], source


def lookup(value: str) -> dict[str, Any]:
    repo, reference, source = parse_url(value)
    request = urllib.request.Request(
        f"https://huggingface.co/api/models/{repo}/revision/{reference}?blobs=true",
        headers={
            "Accept": "application/json",
            "User-Agent": "colab-comfyui-launcher/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if (
                urllib.parse.urlsplit(response.geturl()).netloc != "huggingface.co"
                or urllib.parse.urlsplit(response.geturl()).scheme != "https"
            ):
                raise CatalogError("Metadata redirected outside the public model host.")
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise CatalogError(
                "Repository metadata is too large; use a manually pinned manifest."
            )
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise CatalogError(
            f"Public model metadata returned HTTP {status}; private or gated files may require manual preparation."
        ) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise CatalogError(
            "Could not read public model metadata; the model list was not changed."
        ) from None
    revision = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise CatalogError("Model metadata did not provide a pinned commit.")
    if re.fullmatch(r"[0-9a-f]{40}", reference) and reference != revision:
        raise CatalogError("Model metadata does not match the requested pinned commit.")
    siblings = data.get("siblings", [])
    entry = (
        next(
            (
                item
                for item in siblings
                if isinstance(item, dict) and item.get("rfilename") == source
            ),
            None,
        )
        if isinstance(siblings, list)
        else None
    )
    lfs = entry.get("lfs") if entry else None
    if not isinstance(lfs, dict) or not re.fullmatch(
        r"[0-9a-f]{64}", str(lfs.get("sha256", ""))
    ):
        raise CatalogError(
            "This file has no published LFS SHA256; add independently verified metadata in a manifest."
        )
    size = lfs.get("size")
    if type(size) is not int or size <= 0 or entry.get("size", size) != size:
        raise CatalogError("Model metadata has no consistent positive file size.")
    return {
        "repo_id": repo,
        "revision": revision,
        "source_path": source,
        "size_bytes": size,
        "sha256": lfs["sha256"],
    }


def entries(module: Any) -> list[dict[str, Any]]:
    # Validate the complete catalog, including disabled entries, before editing.
    module.load_manifests(module.DEFAULT_MANIFEST, include_disabled=True)
    values = []
    for manifest in [module.DEFAULT_MANIFEST, *module.default_extra_manifests()]:
        data = module.load_manifest(
            manifest, allow_empty=manifest != module.DEFAULT_MANIFEST
        )
        values.extend(
            dict(
                item,
                manifest=str(manifest),
                repo_id=data["repo_id"],
                revision=data["revision"],
            )
            for item in data["files"]
        )
    return values


@contextlib.contextmanager
def lock(directory: Path):
    if directory.is_symlink():
        raise CatalogError("Model configuration directory must not be a symlink.")
    fd = os.open(
        directory / ".catalog.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def write(path: Path, data: dict[str, Any], validator: Any) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CatalogError("Model configuration must be a regular file.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".catalog-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        validator(temporary, allow_empty=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def toggle(module: Any, destination: str) -> bool:
    with lock(module.DEFAULT_MANIFEST.parent):
        row = next(
            (item for item in entries(module) if item["path"] == destination), None
        )
        if row is None:
            raise CatalogError("The selected model is no longer in the catalog.")
        path = Path(row["manifest"])
        data = module.load_manifest(path, allow_empty=path != module.DEFAULT_MANIFEST)
        item = next(item for item in data["files"] if item["path"] == destination)
        enabled = not item.get("auto_download", True)
        item["auto_download"] = enabled
        write(path, data, module.load_manifest)
        return enabled


def add(
    module: Any, metadata: dict[str, Any], destination: str, categories: frozenset[str]
) -> str:
    module.validate_model_path(destination)
    if "/" not in destination or destination.split("/", 1)[0] not in categories:
        raise CatalogError("Choose a registered ComfyUI model category.")
    with lock(module.DEFAULT_MANIFEST.parent):
        rows = entries(module)
        module.validate_path_collisions([*rows, {"path": destination}])
        identifier = hashlib.sha256(
            (metadata["repo_id"] + "@" + metadata["revision"]).encode()
        ).hexdigest()[:16]
        path = module.DEFAULT_MANIFEST.with_name(f"extra-added-{identifier}.json")
        data = (
            module.load_manifest(path, allow_empty=True)
            if path.exists()
            else {
                "name": "Models added in the terminal interface",
                "repo_id": metadata["repo_id"],
                "revision": metadata["revision"],
                "files": [],
            }
        )
        if (data["repo_id"], data["revision"]) != (
            metadata["repo_id"],
            metadata["revision"],
        ):
            raise CatalogError(
                "The saved additional repository does not match this model."
            )
        data["files"].append(
            {
                "path": destination,
                "source_path": metadata["source_path"],
                "size_bytes": metadata["size_bytes"],
                "sha256": metadata["sha256"],
                "auto_download": True,
            }
        )
        if "total_size_bytes" in data:
            data["total_size_bytes"] = sum(item["size_bytes"] for item in data["files"])
        # Validate proposed data using the same reader before replacing the catalog.
        module.validate_model_path(metadata["source_path"], "Source paths")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*",
            metadata["repo_id"],
        ):
            raise CatalogError("Model repository must be an owner/repository name.")
        if (
            not re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
            or type(metadata["size_bytes"]) is not int
            or metadata["size_bytes"] <= 0
        ):
            raise CatalogError("Model metadata is not pinned and complete.")
        write(path, data, module.load_manifest)
        return path.name
