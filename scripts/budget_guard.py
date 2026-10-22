#!/usr/bin/env python3
"""Persistent, per-allocation CU protection using the official Colab CLI.

The provider exposes account usage, not an allocation bill or a hard quota.
This controller therefore keeps observations separate from an explicit rate
ceiling estimate. It must remain powered, connected, and authenticated to
verify release. State contains identifiers and accounting only, never tokens.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from colab_provider import ColabProvider, ProviderError, validate_session


class BudgetError(RuntimeError):
    """The requested action cannot be safely covered by the stored budget."""


TERMINAL = frozenset({"released", "allocation_cancelled"})
READ_ACTIONS = frozenset({"status", "job", "jobs", "inspect", "release"})
COLLECT_ACTIONS = frozenset({"collect", "download-output", "asset-download"})
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise BudgetError(f"{name} must be a positive finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BudgetError(f"{name} must be a positive finite number.") from exc
    if not math.isfinite(number) or number <= 0:
        raise BudgetError(f"{name} must be a positive finite number.")
    return number


def _session(value: str) -> str:
    try:
        validate_session(value)
    except ProviderError as exc:
        raise BudgetError(str(exc)) from exc
    return value


def process_stamp(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, ValueError, IndexError):
        return None


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _uptime() -> float:
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))


def _default_root() -> Path:
    override = os.environ.get("COLAB_LAUNCHER_STATE_DIR")
    if override:
        return Path(override).expanduser() / "budgets"
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "colab-comfyui-launcher/budgets"
    )


class BudgetManager:
    def __init__(
        self,
        state_root: Path | str | None = None,
        provider: Any = None,
        *,
        clock=time.time,
        uptime=_uptime,
        boot_identity=_boot_id,
        popen=subprocess.Popen,
        poll_seconds: float = 5.0,
    ):
        self.root = Path(state_root) if state_root is not None else _default_root()
        self.provider = provider if provider is not None else ColabProvider()
        self.clock, self.uptime, self.boot_identity = clock, uptime, boot_identity
        self.popen = popen
        self.poll_seconds = _positive(poll_seconds, "Guard poll interval")

    def _path(self, session: str, suffix: str = ".json") -> Path:
        key = hashlib.sha256(_session(session).encode()).hexdigest()
        return self.root / (key + suffix)

    def _directory(self) -> None:
        if self.root.is_symlink():
            raise BudgetError("Budget state directory must not be a symlink.")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise BudgetError("Budget state directory must be owned by this user.")
        self.root.chmod(0o700)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BudgetError("Budget state cannot be read safely.") from exc
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_size > 262144
            ):
                raise BudgetError("Budget state has unsafe ownership, type or size.")
            try:
                value = json.load(handle)
            except (ValueError, UnicodeError) as exc:
                raise BudgetError(
                    "Budget state is corrupt; refuse to reset accounting."
                ) from exc
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise BudgetError("Unsupported budget state; refuse to reset accounting.")
        return value

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _lock(self, session: str, suffix: str = ".state.lock", *, nonblocking=False):
        self._directory()
        fd = os.open(
            self._path(session, suffix), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise BudgetError("Budget lock must be owned by this user.")
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            yield
        finally:
            os.close(fd)

    def _mutate(self, session: str, job_id: str | None, callback) -> dict[str, Any]:
        with self._lock(session):
            record = self._read(self._path(session))
            if record is None or (job_id is not None and record["job_id"] != job_id):
                raise BudgetError(
                    "Budget allocation changed; inspect the current session."
                )
            callback(record)
            record["updated_at"] = self.clock()
            self._write(self._path(session), record)
            return record

    def _guard_alive(self, record: dict[str, Any]) -> bool:
        guard = record.get("guard") or {}
        pid, stamp = guard.get("pid"), guard.get("stamp")
        heartbeat = guard.get("heartbeat_at", 0)
        return (
            type(pid) is int
            and pid > 1
            and isinstance(stamp, str)
            and process_stamp(pid) == stamp
            and guard.get("boot_id") == self.boot_identity()
            and 0 <= self.uptime() - guard.get("heartbeat_uptime", -1e9) <= 120
            and isinstance(heartbeat, (float, int))
        )

    def _advance(self, record: dict[str, Any]) -> None:
        now, uptime, boot = self.clock(), self.uptime(), self.boot_identity()
        if record["status"] in TERMINAL or record.get("submitted_at") is None:
            return
        elapsed = max(
            record.get("accounted_seconds", 0), now - record["allocated_at"], 0
        )
        if boot == record.get("clock_boot_id"):
            elapsed = max(
                elapsed,
                record.get("accounted_seconds", 0)
                + max(0, uptime - record.get("clock_uptime", uptime)),
            )
        elif now < record.get("clock_wall", now):
            record["release_requested"] = True
            record["reason"] = "clock_uncertain_after_reboot"
        record.update(
            accounted_seconds=elapsed,
            clock_wall=now,
            clock_uptime=uptime,
            clock_boot_id=boot,
        )
        rate = record["effective_rate_ceiling"]
        cost = elapsed * rate / 3600
        record["estimate"] = {
            "kind": "conservative_upper_bound",
            "consumed_cu": cost,
            "remaining_cu": max(0.0, record["limit_cu"] - cost),
            "rate_hourly": rate,
            "elapsed_seconds": elapsed,
            "includes": [
                "allocation",
                "installation",
                "download",
                "generation",
                "idle",
            ],
            "actual_instance_cu": None,
        }
        for field, reserve in (
            ("work_deadline", record["reserve_cu"]),
            ("release_deadline", record["release_reserve_cu"]),
            ("estimated_exhaustion", 0),
        ):
            remaining = (record["limit_cu"] - reserve) * 3600 / rate - elapsed
            record[field] = min(record[field], now + max(0, remaining))
        if record.get("hold_deadline") is not None:
            record["hold_deadline"] = min(
                record["hold_deadline"], record["work_deadline"]
            )
            record["hold_until_elapsed"] = min(
                record["hold_until_elapsed"],
                (record["limit_cu"] - record["reserve_cu"]) * 3600 / rate,
            )
        if cost >= record["limit_cu"] - record["release_reserve_cu"]:
            record["release_requested"] = True
            record["reason"] = "budget_release_point"
        hold = record.get("hold_deadline")
        if hold is not None and now >= hold:
            record["release_requested"] = True
            record["reason"] = "idle_hold_expired"
        if (
            record.get("hold_until_elapsed") is not None
            and elapsed >= record["hold_until_elapsed"]
        ):
            record["release_requested"] = True
            record["reason"] = "idle_hold_expired"
        if record.get("release_requested") and record["status"] not in (
            "release_unknown",
            "release_failed",
            "identity_mismatch",
        ):
            record["status"] = "release_pending"
        elif (
            cost >= record["limit_cu"] - record["reserve_cu"]
            and record["status"] == "active"
        ):
            record["status"] = "collect_only"

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(record))
        self._advance(result)
        result["managed"] = True
        result["ok"] = result["status"] in {
            "active",
            "holding",
            "collect_only",
            "released",
        }
        result["verified"] = result["status"] == "released" and bool(
            result.get("release_verified_at")
        )
        result["guard"]["running"] = self._guard_alive(record)
        observed = result.get("observed_account")
        if observed:
            observed["stale"] = (
                bool(result.get("usage_error"))
                or self.clock() - observed["observed_at"] > 360
            )
        for key in (
            "baseline_assignments",
            "account_scope",
            "clock_boot_id",
            "clock_uptime",
            "clock_wall",
        ):
            result.pop(key, None)
        return result

    def status(self, session: str, refresh: bool = False) -> dict[str, Any]:
        record = self._read(self._path(session))
        if record is None:
            return {"ok": True, "managed": False, "session": session}
        if refresh and record["status"] not in TERMINAL:
            self.ensure_guard(session)
            self.tick(session, record["job_id"], force=True)
            record = self._read(self._path(session))
        return self._public(record)

    def list_active(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        if self.root.is_symlink():
            raise BudgetError("Budget state directory must not be a symlink.")
        records = []
        for path in sorted(self.root.glob("*.json")):
            record = self._read(path)
            if record is not None and record["status"] not in TERMINAL:
                records.append(self._public(record))
        return records

    def _new_record(
        self,
        session,
        gpu,
        limit_cu,
        rate_ceiling,
        reserve_cu,
        request_id,
        allocated_at,
        ownership,
    ):
        if gpu is not None and gpu not in {"T4", "L4", "G4", "H100", "A100"}:
            raise BudgetError("Choose a GPU variant supported by the official CLI.")
        limit = _positive(limit_cu, "CU limit")
        rate = _positive(
            rate_ceiling,
            "Conservative CU/hour rate ceiling (unknown rate blocks allocation)",
        )
        reserve = _positive(
            reserve_cu
            if reserve_cu is not None
            else max(limit * 0.1, rate * 240 / 3600),
            "Reserved CU",
        )
        if reserve >= limit:
            raise BudgetError("CU limit must exceed the download/release reserve.")
        request_id = request_id or uuid.uuid4().hex
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise BudgetError("request_id must use 1–128 letters, digits, '-' or '_'.")
        now = self.clock()
        return {
            "schema": 1,
            "session": _session(session),
            "job_id": uuid.uuid4().hex,
            "request_id": request_id,
            "assignment_id": None,
            "hardware": gpu or "CPU",
            "ownership": ownership,
            "status": "planned",
            "created_at": now,
            "updated_at": now,
            "allocated_at": allocated_at,
            "allocation_time_source": "pre_request_conservative"
            if ownership == "created"
            else "user_supplied_conservative",
            "submitted_at": None,
            "accounted_seconds": 0,
            "clock_wall": now,
            "clock_uptime": self.uptime(),
            "clock_boot_id": self.boot_identity(),
            "limit_cu": limit,
            "rate_ceiling": rate,
            "effective_rate_ceiling": rate,
            "reserve_cu": reserve,
            "release_reserve_cu": reserve / 2,
            "work_deadline": allocated_at + (limit - reserve) * 3600 / rate,
            "release_deadline": allocated_at + (limit - reserve / 2) * 3600 / rate,
            "estimated_exhaustion": allocated_at + limit * 3600 / rate,
            "hold_deadline": None,
            "hold_until_elapsed": None,
            "release_requested": False,
            "estimate": {
                "kind": "conservative_upper_bound",
                "consumed_cu": 0,
                "remaining_cu": limit,
                "rate_hourly": rate,
                "actual_instance_cu": None,
            },
            "observed_account": None,
            "guard": {},
            "release_attempts": 0,
            "limitation": "Best-effort launcher protection; controller outage, lost authentication or network failure can prevent remote release. Not a Colab hard quota.",
        }

    def _install_record(self, record: dict[str, Any]) -> dict[str, Any]:
        session = record["session"]
        with self._lock(session):
            # Recheck history under the same lock as rotation/current writes.
            # A delayed replay must not miss a record archived after preflight.
            historical = self._historical_request(session, record["request_id"])
            if historical is not None:
                return historical
            previous = self._read(self._path(session))
            if previous:
                if (
                    previous["status"] not in TERMINAL
                    or previous["request_id"] == record["request_id"]
                ):
                    return previous
                archive = self.root / "history"
                archive.mkdir(mode=0o700, exist_ok=True)
                if archive.is_symlink():
                    raise BudgetError("Budget history must not be a symlink.")
                self._write(archive / (previous["job_id"] + ".json"), previous)
            self._write(self._path(session), record)
        return record

    def _historical_request(self, session: str, request_id: str | None):
        if request_id:
            for path in (self.root / "history").glob("*.json"):
                historical = self._read(path)
                if (
                    historical
                    and historical["session"] == session
                    and historical["request_id"] == request_id
                ):
                    return historical
        return None

    def _existing_create_record(self, session: str, request_id: str | None):
        if not self.root.exists():
            return None
        with self._lock(session):
            historical = self._historical_request(session, request_id)
            if historical is not None:
                return historical
            previous = self._read(self._path(session))
            if previous and (
                previous["status"] not in TERMINAL
                or previous["request_id"] == request_id
            ):
                return previous
        return None

    def create(
        self,
        session: str,
        gpu: str | None = None,
        *,
        limit_cu,
        rate_ceiling,
        reserve_cu=None,
        request_id=None,
    ) -> dict[str, Any]:
        record = self._new_record(
            session,
            gpu,
            limit_cu,
            rate_ceiling,
            reserve_cu,
            request_id,
            self.clock(),
            "created",
        )

        def replay(previous):
            if previous["request_id"] == record["request_id"] and any(
                previous[field] != record[field]
                for field in ("hardware", "limit_cu", "rate_ceiling", "reserve_cu")
            ):
                raise BudgetError(
                    "request_id already belongs to different allocation parameters; its stored budget was not changed."
                )
            if previous["status"] not in TERMINAL:
                self.ensure_guard(session)
                self.tick(session, previous["job_id"], force=True)
                current = self._read(self._path(session))
                if current["job_id"] != previous["job_id"]:
                    previous = (
                        self._read(
                            self.root / "history" / (previous["job_id"] + ".json")
                        )
                        or previous
                    )
                else:
                    previous = current
            result = self._public(previous)
            result.update(already_managed=True, budget_configuration_unchanged=True)
            return result

        previous = self._existing_create_record(session, request_id)
        if previous:
            return replay(previous)
        scope = self.provider.account_scope()
        rows = self.provider.sessions()
        if scope == "unknown" or scope != self.provider.account_scope():
            raise BudgetError(
                "Authentication scope changed during preflight; allocation was not submitted."
            )
        if any(row["name"] == session for row in rows):
            raise BudgetError(
                "Session already exists. Explicitly adopt its allocation; creation was not submitted."
            )
        record["baseline_assignments"] = [row["assignment_id"] for row in rows]
        record["account_scope"] = scope
        if record["account_scope"] == "unknown":
            raise BudgetError(
                "Authentication scope cannot be tracked; allocation was not submitted."
            )
        installed = self._install_record(record)
        if installed["job_id"] != record["job_id"]:
            return replay(installed)
        # The independent guard must have acquired its lifetime lock and written
        # a heartbeat before the allocation POST is even eligible to run.
        try:
            self.ensure_guard(session)
        except BudgetError:

            def guard_failed(value):
                if (
                    value["status"] == "planned"
                    and value.get("submitted_at") is None
                    and not value.get("assignment_id")
                ):
                    value.update(
                        status="allocation_cancelled",
                        reason="guard_start_failed_before_allocation",
                    )
                else:
                    value["guard_error"] = (
                        "Guard startup failed; inspect the existing allocation."
                    )

            self._mutate(session, record["job_id"], guard_failed)
            raise

        def submitting(value):
            if (
                value["submitted_at"] is not None
                or value["status"] != "planned"
                or value.get("release_requested")
            ):
                raise BudgetError(
                    "Allocation was already submitted; inspect its recorded outcome."
                )
            if not self._guard_alive(value):
                raise BudgetError(
                    "Budget guard is not alive; allocation was not submitted."
                )
            now = self.clock()
            shift = now - value["allocated_at"]
            value["allocated_at"] = now
            for field in ("work_deadline", "release_deadline", "estimated_exhaustion"):
                value[field] += shift
            value.update(
                submitted_at=now,
                status="allocation_pending",
                clock_wall=now,
                clock_uptime=self.uptime(),
            )

        self._mutate(session, record["job_id"], submitting)
        try:
            self.provider.create(session, gpu)
        except ProviderError:

            def unknown(value):
                value["allocation_error"] = (
                    "Allocation request outcome is unknown; query this request, do not repeat new."
                )
                if not value.get("assignment_id") and value["status"] not in TERMINAL:
                    value["status"] = "allocation_unknown"

            self._mutate(session, record["job_id"], unknown)
        self.tick(session, record["job_id"], force=True)
        return self.status(session)

    def adopt(
        self,
        session: str,
        assignment_id: str,
        *,
        limit_cu,
        rate_ceiling,
        allocated_at,
        reserve_cu=None,
        confirm=False,
        request_id=None,
    ) -> dict[str, Any]:
        if confirm is not True:
            raise BudgetError(
                "Explicit confirmation is required to take over an existing allocation."
            )
        allocated_at = _positive(allocated_at, "Original allocation timestamp")
        if allocated_at > self.clock():
            raise BudgetError("Original allocation timestamp cannot be in the future.")
        record = self._new_record(
            session,
            None,
            limit_cu,
            rate_ceiling,
            reserve_cu,
            request_id,
            allocated_at,
            "adopted",
        )
        previous = self._read(self._path(session))
        if previous and previous["status"] not in TERMINAL:
            if (
                previous.get("assignment_id")
                and previous["assignment_id"] != assignment_id
            ):
                raise BudgetError(
                    "Another allocation is still recorded; verify its release first."
                )
            if previous.get("assignment_id"):
                self.ensure_guard(session)
                return self.status(session, refresh=True)
        scope = self.provider.account_scope()
        rows = self.provider.sessions()
        if scope == "unknown" or scope != self.provider.account_scope():
            raise BudgetError(
                "Authentication scope changed during adoption; no ownership was recorded."
            )
        selected = next(
            (
                row
                for row in rows
                if row["name"] == session and row["assignment_id"] == assignment_id
            ),
            None,
        )
        if selected is None:
            raise BudgetError("The exact allocation was not verified for adoption.")
        if previous and previous["status"] not in TERMINAL:

            def recover(value):
                if value["status"] in TERMINAL:
                    raise BudgetError(
                        "The pending request was cancelled or released before adoption; inspect it again."
                    )
                if value.get("assignment_id") not in (None, assignment_id):
                    raise BudgetError(
                        "Allocation identity changed during explicit recovery."
                    )
                value.update(
                    assignment_id=assignment_id,
                    actual_hardware=selected["hardware"],
                    ownership="adopted",
                    account_scope=scope,
                    allocated_at=min(value["allocated_at"], allocated_at),
                    submitted_at=value.get("submitted_at") or allocated_at,
                    explicit_recovery_at=self.clock(),
                    status="active",
                    query_error=None,
                )
                self._advance(value)

            self._mutate(session, previous["job_id"], recover)
            self.ensure_guard(session)
            result = self.status(session, refresh=True)
            result["budget_configuration_unchanged"] = True
            return result
        record.update(
            assignment_id=assignment_id,
            hardware=selected["hardware"],
            status="active",
            submitted_at=allocated_at,
            account_scope=scope,
            last_query_at=self.clock(),
        )
        if record["account_scope"] == "unknown":
            raise BudgetError(
                "Authentication scope cannot be tracked; adoption was not recorded."
            )
        self._advance(record)
        installed = self._install_record(record)
        if installed["job_id"] != record["job_id"]:
            raise BudgetError(
                "Another budget already owns this session; inspect it before proceeding."
            )
        self.ensure_guard(session)
        return self.status(session, refresh=True)

    def ensure_guard(self, session: str) -> dict[str, Any]:
        record = self._read(self._path(session))
        if not record:
            raise BudgetError("No budget is recorded for this session.")
        if record["status"] in TERMINAL or self._guard_alive(record):
            return self._public(record)
        with self._lock(session, ".launch.lock"):
            record = self._read(self._path(session))
            if record["status"] in TERMINAL or self._guard_alive(record):
                return self._public(record)
            arguments = [
                sys.executable,
                str(Path(__file__).resolve()),
                "_watch",
                "--state-root",
                str(self.root.absolute()),
                "--session",
                session,
                "--job-id",
                record["job_id"],
                "--poll-seconds",
                str(self.poll_seconds),
            ]
            try:
                process = self.popen(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                raise BudgetError(
                    "Independent budget guard could not start; no new work is allowed."
                ) from exc
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = self._read(self._path(session))
                if (
                    current
                    and current["job_id"] == record["job_id"]
                    and self._guard_alive(current)
                ):
                    return self._public(current)
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            raise BudgetError(
                "Independent budget guard did not confirm readiness; no new work is allowed."
            )

    def authorize(
        self, session: str, action: str, seconds: float | None = None
    ) -> float | None:
        record = self._read(self._path(session))
        if record is None:
            return None
        if action in READ_ACTIONS:
            return seconds
        if action == "stop":
            rows = self.provider.sessions()
            if not any(
                row["assignment_id"] == record.get("assignment_id")
                and row["name"] == session
                for row in rows
            ):
                raise BudgetError(
                    "Allocation identity is not verified for service cleanup; no stop was authorized."
                )
            return seconds
        if seconds is not None:
            seconds = _positive(seconds, "Task duration")
        self.ensure_guard(session)
        query_generation = record.get("query_generation", 0)
        self.tick(session, record["job_id"], force=True)
        record = self._read(self._path(session))
        if not self._guard_alive(record):
            raise BudgetError("Budget guard is unavailable; new work is blocked.")
        allowed = (
            {"active", "holding", "collect_only"}
            if action in COLLECT_ACTIONS
            else {"active", "holding"}
        )
        if (
            record["status"] not in allowed
            or record.get("query_error")
            or not record.get("assignment_id")
            or record.get("release_requested")
        ):
            raise BudgetError(
                "Budget or allocation state is not verified for new work; inspect budget status."
            )
        if record.get("query_generation", 0) <= query_generation:
            raise BudgetError(
                "Allocation reconciliation is still in progress; wait for its result before new work."
            )
        deadline = (
            record["release_deadline"]
            if action in COLLECT_ACTIONS
            else record["work_deadline"]
        )
        if record.get("hold_deadline") is not None:
            deadline = min(deadline, record["hold_deadline"])
        remaining = deadline - self.clock()
        if remaining < 1:
            raise BudgetError(
                "Budget protection point reached; only release/status remain available."
            )
        return min(seconds, remaining) if seconds is not None else remaining

    def hold(self, session: str, seconds: float) -> dict[str, Any]:
        seconds = _positive(seconds, "Idle hold duration")
        original = self._read(self._path(session))
        if original is None:
            raise BudgetError("Idle holds require a managed allocation.")
        self.authorize(session, "hold", seconds)

        def update(record):
            self._advance(record)
            if record["status"] not in {"active", "holding"} or record.get(
                "release_requested"
            ):
                raise BudgetError(
                    "Allocation stopped accepting idle holds before this update."
                )
            remaining = max(0, record["work_deadline"] - self.clock())
            duration = min(seconds, remaining)
            record.update(
                hold_deadline=self.clock() + duration,
                hold_until_elapsed=record["accounted_seconds"] + duration,
                status="holding",
            )

        self._mutate(session, original["job_id"], update)
        return self.status(session)

    def release(self, session: str) -> dict[str, Any]:
        record = self._read(self._path(session))
        if not record:
            raise BudgetError(
                "This allocation is not managed; explicitly adopt it or release its exact provider identity."
            )
        if record["status"] in TERMINAL:
            return self._public(record)

        def request_release(value):
            if value["status"] in TERMINAL:
                return
            if value.get("submitted_at") is None:
                value.update(
                    status="allocation_cancelled",
                    reason="Cancelled before allocation submission.",
                )
            else:
                value.update(
                    release_requested=True,
                    status="release_pending",
                    reason="explicit_release",
                )

        record = self._mutate(session, record["job_id"], request_release)
        if record["status"] in TERMINAL:
            return self._public(record)
        # A failed daemon start must not prevent a user's immediate verified
        # release attempt. The persisted request can be retried after recovery.
        try:
            self.ensure_guard(session)
        except BudgetError:
            pass
        self.tick(session, record["job_id"], force=True)
        return self.status(session)

    def _observe(self, session, job_id, rows, scope):
        def update(record):
            self._advance(record)
            if record["status"] in TERMINAL:
                return
            now = self.clock()
            record.update(
                last_query_at=now,
                query_error=None,
                query_generation=record.get("query_generation", 0) + 1,
            )
            if not record.get("assignment_id"):
                if record.get("submitted_at") is None:
                    return
                matches = [
                    row
                    for row in rows
                    if row["name"] == session
                    and row["assignment_id"]
                    not in record.get("baseline_assignments", [])
                ]
                if len(matches) != 1:
                    record["status"] = "allocation_unknown"
                    record["reason"] = (
                        "Allocation not yet bound; creation is never automatically repeated."
                    )
                    return
                selected = matches[0]
                if scope == "unknown" or scope != record.get("account_scope"):
                    record.update(
                        status="allocation_unknown",
                        reason="Authentication scope changed before allocation identity was bound.",
                    )
                    return
                record.update(
                    assignment_id=selected["assignment_id"],
                    allocation_verified_at=now,
                    actual_hardware=selected["hardware"],
                )
                if selected["hardware"] != record["hardware"]:
                    record.update(
                        release_requested=True,
                        reason="Allocated hardware differs from request.",
                    )
            matching = next(
                (
                    row
                    for row in rows
                    if row["assignment_id"] == record["assignment_id"]
                ),
                None,
            )
            if matching is None:
                if scope == "unknown" or scope != record.get("account_scope"):
                    record.update(
                        status="release_unknown",
                        query_error="Authentication scope changed; disappearance cannot verify release.",
                    )
                    return
                record.update(
                    status="released",
                    released_at=now,
                    release_verified_at=now,
                    reason="Provider allocation list confirms absence.",
                )
                return
            if matching["name"] != session:
                record.update(
                    status="identity_mismatch",
                    query_error="Allocation is no longer mapped to this session; no stop was sent.",
                )
                return
            record["account_scope"] = scope
            record["actual_hardware"] = matching["hardware"]
            if record.get("release_requested"):
                record["status"] = "release_pending"
            elif (
                record["estimate"]["consumed_cu"]
                >= record["limit_cu"] - record["reserve_cu"]
            ):
                record["status"] = "collect_only"
            else:
                record["status"] = (
                    "holding" if record.get("hold_deadline") else "active"
                )

        return self._mutate(session, job_id, update)

    def tick(
        self, session: str, job_id: str | None = None, *, force: bool = False
    ) -> dict[str, Any]:
        """One bounded reconciliation. A separate I/O lock prevents double stop.

        The accounting lock is never held across provider calls, so readers and
        task admission can still observe the elapsed budget during an outage.
        """
        try:
            with self._lock(session, ".reconcile.lock", nonblocking=True):
                return self._tick_locked(session, job_id, force=force)
        except BlockingIOError:
            return self.status(session)

    def _tick_locked(self, session, job_id, *, force):
        record = self._mutate(session, job_id, self._advance)
        job_id = record["job_id"]
        if record["status"] in TERMINAL or record.get("submitted_at") is None:
            return self._public(record)
        if (
            not force
            and not record.get("release_requested")
            and self.clock() - record.get("last_query_at", 0) < 30
        ):
            return self._public(record)
        try:
            scope_before = self.provider.account_scope()
            rows = self.provider.sessions()
            scope_after = self.provider.account_scope()
            if scope_before == "unknown" or scope_before != scope_after:
                raise ProviderError("Authentication state changed during query.")
            record = self._observe(session, job_id, rows, scope_after)
        except ProviderError:

            def failure(value):
                self._advance(value)
                value.update(
                    query_error="Provider allocation query failed; accounting continues conservatively.",
                    last_query_attempt=self.clock(),
                )
                if value["status"] not in TERMINAL:
                    value["status"] = (
                        "release_unknown"
                        if value.get("release_requested")
                        else "query_failed"
                    )

            record = self._mutate(session, job_id, failure)
            return self._public(record)
        if (
            record["status"] in TERMINAL
            or record["status"] == "identity_mismatch"
            or record.get("query_error")
            or not record.get("assignment_id")
        ):
            return self._public(record)
        if record.get("release_requested"):
            self._mutate(
                session,
                job_id,
                lambda value: value.update(
                    release_attempts=value["release_attempts"] + 1,
                    release_attempted_at=self.clock(),
                ),
            )
            result = self.provider.verified_release(
                session, record["assignment_id"], expected_scope=record["account_scope"]
            )
            if self.provider.account_scope() != record["account_scope"]:
                result = {
                    "ok": False,
                    "status": "release_unknown",
                    "error": "Authentication scope changed; release of the original allocation remains unverified.",
                }

            def released(value):
                self._advance(value)
                if result.get("ok") is True and result.get("status") == "released":
                    value.update(
                        status="released",
                        released_at=self.clock(),
                        release_verified_at=self.clock(),
                        release_error=None,
                    )
                else:
                    value.update(
                        status=result.get("status", "release_unknown"),
                        release_error=result.get(
                            "error",
                            "Release has not been verified; the guard will check before retrying.",
                        ),
                    )

            return self._public(self._mutate(session, job_id, released))
        if self.clock() - record.get("usage_attempted_at", 0) >= 300:
            self._mutate(
                session,
                job_id,
                lambda value: value.update(usage_attempted_at=self.clock()),
            )
            try:
                usage = self.provider.usage()

                def observed(value):
                    value["observed_account"] = dict(
                        usage, scope="account", observed_at=self.clock()
                    )
                    value["usage_error"] = None
                    # The whole-account rate is only an upper estimate. Never
                    # attribute its balance drop to this instance or extend time.
                    value["effective_rate_ceiling"] = max(
                        value["effective_rate_ceiling"], usage["rate_hourly"]
                    )
                    self._advance(value)

                record = self._mutate(session, job_id, observed)
            except ProviderError:
                record = self._mutate(
                    session,
                    job_id,
                    lambda value: value.update(
                        usage_error="Account usage unavailable; explicit conservative rate remains in force."
                    ),
                )
        return self._public(record)

    def watch(self, session: str, job_id: str) -> int:
        try:
            with self._lock(session, ".guard.lock", nonblocking=True):
                pid, stamp = os.getpid(), process_stamp(os.getpid())
                if stamp is None:
                    return 2
                while True:
                    record = self._read(self._path(session))
                    if (
                        not record
                        or record["job_id"] != job_id
                        or record["status"] in TERMINAL
                    ):
                        return 0

                    def heartbeat(value):
                        value["guard"] = {
                            "pid": pid,
                            "stamp": stamp,
                            "boot_id": self.boot_identity(),
                            "heartbeat_at": self.clock(),
                            "heartbeat_uptime": self.uptime(),
                        }

                    self._mutate(session, job_id, heartbeat)
                    try:
                        self.tick(session, job_id)
                    except (BudgetError, OSError, ValueError):
                        # No raw provider output or credential material is logged.
                        self._mutate(
                            session,
                            job_id,
                            lambda value: value.update(
                                guard_error="Guard iteration failed; pending release remains durable."
                            ),
                        )
                    current = self._read(self._path(session))
                    if current["status"] in TERMINAL:
                        return 0
                    wait = self.poll_seconds
                    if current.get("submitted_at") is not None:
                        deadlines = [current["release_deadline"]]
                        if current.get("hold_deadline"):
                            deadlines.append(current["hold_deadline"])
                        until_deadline = min(deadlines) - self.clock()
                        if until_deadline > 0:
                            wait = min(wait, max(0.1, until_deadline))
                    time.sleep(wait)
        except BlockingIOError:
            return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("_watch",))
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        return BudgetManager(args.state_root, poll_seconds=args.poll_seconds).watch(
            args.session, args.job_id
        )
    except (BudgetError, ProviderError, OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
