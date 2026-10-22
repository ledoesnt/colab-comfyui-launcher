"""Private durable command receipts; never store credentials or full payloads."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def request_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("request_id must contain 1–128 letters, digits, _ or -")
    return value


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ClientRequests:
    def __init__(self, session: str, root: Path | None = None):
        root = root or Path(
            os.environ.get(
                "COLAB_LAUNCHER_STATE_DIR",
                str(Path.home() / ".local/state/colab-comfyui-launcher"),
            )
        )
        self.session = session
        self.directory = (
            root / "requests" / hashlib.sha256(session.encode()).hexdigest()
        )

    def path(self, identifier: str) -> Path:
        return self.directory / (request_id(identifier) + ".json")

    @contextmanager
    def lock(self, identifier: str):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.path(identifier).with_suffix(".lock").open("a") as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def read(self, identifier: str) -> dict:
        path = self.path(identifier)
        return json.loads(path.read_text()) if path.exists() else {}

    def begin(self, identifier: str, payload: dict) -> tuple[dict, bool]:
        """Caller holds lock through dispatch. Existing intents are query-only."""
        digest = fingerprint(payload)
        old = self.read(identifier)
        if old:
            if old["payload_sha256"] != digest:
                raise ValueError("request_id already belongs to different arguments")
            return old, False
        record = {
            "version": 1,
            "request_id": identifier,
            "job_id": identifier,
            "session": self.session,
            "assignment_id": payload.get("assignment_id"),
            "account_scope": payload.get("account_scope"),
            "action": payload["action"],
            "payload_sha256": digest,
            "status": "submission_unknown",
            "created_at": time.time(),
        }
        atomic_json(self.path(identifier), record)
        return record, True

    def finish(self, identifier: str, result: dict) -> None:
        record = self.read(identifier)
        record.update(
            result=result,
            status=result.get("status", "completed"),
            updated_at=time.time(),
        )
        atomic_json(self.path(identifier), record)

    def list(self) -> list[dict]:
        if not self.directory.exists():
            return []
        records = [
            json.loads(path.read_text()) for path in self.directory.glob("*.json")
        ]
        return sorted(records, key=lambda row: row["created_at"], reverse=True)
