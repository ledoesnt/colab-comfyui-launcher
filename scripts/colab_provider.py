"""Small, fail-closed adapter for the official Colab CLI 0.7.4 text protocol.

Only the CLI reads its own credentials. This module never opens its credential
or session stores, and never forwards untrusted provider output. Assignment
endpoints are opaque allocation identifiers, not runtime proxy URLs or tokens.
"""

from __future__ import annotations

import hashlib
import math
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROVIDER_ERROR_CODES = frozenset(
    {
        "provider_error",
        "invalid_session",
        "invalid_assignment",
        "invalid_response",
        "invalid_gpu",
        "timeout",
        "unavailable",
        "command_failed",
        "provider_diagnostics",
        "scope_changed",
        "assignment_absent",
        "identity_mismatch",
    }
)


class ProviderError(RuntimeError):
    """A safe error whose message contains no provider response material."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        outcome_unknown: bool = False,
        returncode: int | None = None,
    ):
        super().__init__(message)
        self.code = (
            code
            if type(code) is str and code in PROVIDER_ERROR_CODES
            else "provider_error"
        )
        self.outcome_unknown = outcome_unknown
        # Preserve only process status, never captured provider response bytes.
        self.returncode = returncode if type(returncode) is int else None


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ASSIGNMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}\Z")
SESSION_LINE = re.compile(
    r"\[(?P<name>[^\]\s]+)\] (?P<assignment_id>[A-Za-z0-9][A-Za-z0-9._~-]{0,255})"
    r" \| Hardware: (?P<hardware>[A-Za-z0-9._-]+)"
    r" \| Shape: (?P<shape>Standard|High-RAM)"
    r" \| Variant: (?P<variant>DEFAULT|GPU|TPU)\Z"
)
EMPTY_SESSIONS = "[colab] No active sessions found on server."
PRUNED = re.compile(r"\[colab\] Pruned [0-9]+ stale local session\(s\)\.\Z")
UPGRADE_LINES = (
    re.compile(
        r"\[colab\] A new version of Colab CLI is available: [A-Za-z0-9.+_-]+"
        r" \(current: [A-Za-z0-9.+_-]+\)\Z"
    ),
    re.compile(
        r"\[colab\] You can run 'colab update --install' to upgrade in place\.\Z"
    ),
    re.compile(
        r"\[colab\] Run '(?:colab update|uv tool install -U google-colab-cli|"
        r"pip install --upgrade google-colab-cli)' to update\.\Z"
    ),
    re.compile(
        r'\[colab\] To silence this check, set "enable_update_check": false in '
        r"~/\.config/colab-cli/settings\.json\Z"
    ),
)


def validate_session(session: str) -> None:
    if not isinstance(session, str) or SESSION.fullmatch(session) is None:
        raise ProviderError(
            "Session names must use 1–128 letters, digits, dots, underscores or hyphens, "
            "starting with a letter or digit.",
            code="invalid_session",
        )


def validate_assignment(assignment_id: str) -> None:
    if (
        not isinstance(assignment_id, str)
        or ASSIGNMENT.fullmatch(assignment_id) is None
    ):
        raise ProviderError("Invalid allocation identifier.", code="invalid_assignment")


def _lines(value: str, *, mutation: bool = False) -> tuple[list[str], bool]:
    if not isinstance(value, str) or len(value) > 1024 * 1024:
        raise ProviderError(
            "Colab returned an unsupported response.",
            code="invalid_response",
            outcome_unknown=mutation,
        )
    result = []
    notice = False
    for raw in ANSI.sub("", value).splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(pattern.fullmatch(line) for pattern in UPGRADE_LINES):
            notice = True
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in line):
            raise ProviderError(
                "Colab returned an unsupported response.",
                code="invalid_response",
                outcome_unknown=mutation,
            )
        result.append(line)
    return result, notice


def parse_sessions(output: str) -> list[dict[str, str]]:
    """Validate the whole listing, including nameless server assignments.

    A partially recognized listing must never become proof of absence. Ordinary
    ``status`` output is intentionally not accepted: its local-only empty state
    is not a server-side assignment listing.
    """
    lines, _ = _lines(output)
    rows: list[dict[str, str]] = []
    empty = False
    ids: set[str] = set()
    names: set[str] = set()
    for line in lines:
        if PRUNED.fullmatch(line):
            continue
        if line == EMPTY_SESSIONS:
            if empty:
                raise ProviderError(
                    "Colab returned a conflicting session listing.",
                    code="invalid_response",
                )
            empty = True
            continue
        matched = SESSION_LINE.fullmatch(line)
        if matched is None:
            raise ProviderError(
                "Colab session listing could not be fully verified.",
                code="invalid_response",
            )
        row = matched.groupdict()
        name, assignment_id = row["name"], row["assignment_id"]
        if name != "?":
            validate_session(name)
        if assignment_id in ids or (name != "?" and name in names):
            raise ProviderError(
                "Colab returned duplicate session identities.", code="invalid_response"
            )
        ids.add(assignment_id)
        names.add(name)
        rows.append(row)
    if (empty and rows) or (not empty and not rows):
        raise ProviderError(
            "Colab session listing was empty or conflicting.", code="invalid_response"
        )
    return rows


def parse_usage(output: str) -> dict[str, Any]:
    lines, _ = _lines(output)
    patterns = {
        "balance": r"Current balance: ([0-9]+(?:\.[0-9]+)?) compute units",
        "rate_hourly": r"Usage rate: ([0-9]+(?:\.[0-9]+)?)/hr",
        "assignments_count": r"Active assignments: ([0-9]+)",
    }
    result: dict[str, Any] = {"scope": "account"}
    for line in lines:
        for field, pattern in patterns.items():
            match = re.fullmatch(pattern, line)
            if match:
                if field in result:
                    raise ProviderError(
                        "Colab returned duplicate usage fields.",
                        code="invalid_response",
                    )
                try:
                    number = (
                        int(match[1])
                        if field == "assignments_count"
                        else float(match[1])
                    )
                except (OverflowError, ValueError):
                    raise ProviderError(
                        "Colab returned invalid usage fields.", code="invalid_response"
                    ) from None
                if isinstance(number, float) and not math.isfinite(number):
                    raise ProviderError(
                        "Colab returned invalid usage fields.", code="invalid_response"
                    )
                result[field] = number
                break
        else:
            raise ProviderError(
                "Colab usage response could not be fully verified.",
                code="invalid_response",
            )
    if any(field not in result for field in patterns):
        raise ProviderError(
            "Colab usage response is incomplete.", code="invalid_response"
        )
    return result


class ColabProvider:
    """Bounded calls through the installed official CLI, with no write retries.

    The official CLI only exposes stop by local session name. The immediate
    identity recheck protects stale names; concurrent external CLI mutations of
    that name between the check and stop cannot be made atomic by this adapter.
    """

    def __init__(
        self,
        run: Callable[..., Any] = subprocess.run,
        *,
        oauth_cache: Path | None = None,
    ):
        self.run = run
        self.oauth_cache = oauth_cache or Path.home() / ".config/colab-cli/token.json"

    def account_scope(self) -> str:
        """A change detector using file metadata only, never credential bytes.

        Refreshing a token can also change this value. Such false positives are
        conservative: allocation presence can re-establish the current scope,
        but absence after a scope change cannot establish remote release.
        """
        try:
            metadata = self.oauth_cache.lstat()
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unknown"
        if not stat.S_ISREG(metadata.st_mode):
            return "unknown"
        fields = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
        )
        return "stat:" + hashlib.sha256(repr(fields).encode("ascii")).hexdigest()

    def _command(
        self, arguments: list[str], *, timeout: float, mutation: bool = False
    ) -> list[str]:
        try:
            completed = self.run(
                ["colab", "--auth=oauth2", *arguments],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ProviderError(
                "Colab command timed out; query the existing allocation before retrying.",
                code="timeout",
                outcome_unknown=mutation,
            ) from None
        except OSError:
            raise ProviderError(
                "Official Colab CLI could not be started.", code="unavailable"
            ) from None
        if completed.returncode != 0:
            raise ProviderError(
                "Colab command failed; use the official interactive login when authentication is required.",
                code="command_failed",
                outcome_unknown=mutation,
                returncode=completed.returncode,
            )
        lines, stdout_notice = _lines(completed.stdout, mutation=mutation)
        errors, stderr_notice = _lines(completed.stderr or "", mutation=mutation)
        if stdout_notice or stderr_notice:
            print("Colab CLI reported an available upgrade.", file=sys.stderr)
        # sync_sessions in CLI 0.7.4 can turn a failed credential lookup into an
        # empty assignment list. Never accept accompanying auth/error output as
        # evidence that an allocation was released.
        if errors:
            raise ProviderError(
                "Colab returned diagnostics; the provider result remains unverified.",
                code="provider_diagnostics",
                outcome_unknown=mutation,
                returncode=completed.returncode,
            )
        return lines

    def sessions(self) -> list[dict[str, str]]:
        return parse_sessions("\n".join(self._command(["sessions"], timeout=20)))

    def usage(self) -> dict[str, Any]:
        return parse_usage("\n".join(self._command(["usage"], timeout=15)))

    def create(self, session: str, gpu: str | None = None) -> None:
        validate_session(session)
        arguments = ["new", "-s", session]
        if gpu is not None:
            if gpu not in ("T4", "L4", "G4", "H100", "A100"):
                raise ProviderError("Unsupported GPU selection.", code="invalid_gpu")
            arguments.extend(["--gpu", gpu])
        lines = self._command(arguments, timeout=300, mutation=True)
        if lines != [
            f"[colab] Creating session '{session}'...",
            "[colab] Session READY.",
        ]:
            raise ProviderError(
                "Allocation outcome is unknown; inspect sessions before any further creation.",
                code="invalid_response",
                outcome_unknown=True,
            )

    def stop(self, session: str, assignment_id: str) -> None:
        validate_session(session)
        validate_assignment(assignment_id)
        scope = self.account_scope()
        rows = self.sessions()
        if scope == "unknown" or self.account_scope() != scope:
            raise ProviderError(
                "Colab account scope changed during verification; no stop was issued.",
                code="scope_changed",
            )
        owned = next(
            (row for row in rows if row["assignment_id"] == assignment_id), None
        )
        named = next((row for row in rows if row["name"] == session), None)
        if owned is None:
            raise ProviderError(
                "The selected allocation is already absent; no stop was issued.",
                code="assignment_absent",
            )
        if (
            owned["name"] != session
            or named is None
            or named["assignment_id"] != assignment_id
        ):
            raise ProviderError(
                "Session identity changed; no stop was issued.",
                code="identity_mismatch",
            )
        lines = self._command(["stop", "-s", session], timeout=30, mutation=True)
        if lines != [
            f"[colab] Stopping session '{session}'...",
            "[colab] Session terminated.",
        ]:
            raise ProviderError(
                "Stop outcome is unknown; verify the allocation on the platform.",
                code="invalid_response",
                outcome_unknown=True,
            )

    def verified_release(
        self,
        session: str,
        assignment_id: str,
        *,
        expected_scope: str | None = None,
    ) -> dict[str, Any]:
        """One recoverable release attempt; only platform absence is success.

        Persistent callers pass the scope from their last exact allocation
        observation. Otherwise an account switch immediately before this
        method could make a different account's empty list appear conclusive.
        """
        validate_session(session)
        validate_assignment(assignment_id)
        result: dict[str, Any] = {
            "ok": False,
            "session": session,
            "assignment_id": assignment_id,
            "status": "release_unknown",
            "verified": False,
            "release_requested": False,
        }
        current_scope = self.account_scope()
        scope = current_scope if expected_scope is None else expected_scope
        if scope == "unknown" or current_scope != scope:
            result.update(
                status="scope_changed",
                error="Colab account scope differs from the allocation observation; no stop was issued.",
                error_code="scope_changed",
            )
            return result

        def verified_absent() -> dict[str, Any]:
            if scope == "unknown" or self.account_scope() != scope:
                result.update(
                    status="scope_changed",
                    error="Colab account scope changed; absence does not verify release of the original allocation.",
                    error_code="scope_changed",
                )
            else:
                result.update(ok=True, status="released", verified=True)
            return result

        try:
            rows = self.sessions()
        except ProviderError as error:
            result.update(error=str(error), error_code=error.code)
            return result
        target = next(
            (row for row in rows if row["assignment_id"] == assignment_id), None
        )
        if target is None:
            result["already_absent"] = True
            return verified_absent()
        if scope == "unknown" or self.account_scope() != scope:
            result.update(
                status="scope_changed",
                error="Colab account scope changed during verification; no stop was issued.",
                error_code="scope_changed",
            )
            return result
        if target["name"] != session:
            result.update(
                status="identity_mismatch",
                error="Allocation no longer belongs to the selected session name; no stop was issued.",
                error_code="identity_mismatch",
            )
            return result
        failure: ProviderError | None = None
        try:
            self.stop(session, assignment_id)
            result["release_requested"] = True
        except ProviderError as error:
            failure = error
            result.update(error=str(error), error_code=error.code)
        # Always query after a stop attempt, including a timeout or rejected
        # response. A request may have succeeded while its response was lost.
        try:
            rows = self.sessions()
        except ProviderError as error:
            result.update(
                status="release_unknown", error=str(error), error_code=error.code
            )
            return result
        target = next(
            (row for row in rows if row["assignment_id"] == assignment_id), None
        )
        if target is None:
            return verified_absent()
        elif target["name"] != session:
            result.update(status="identity_mismatch", error_code="identity_mismatch")
        else:
            result.update(status="release_failed" if failure else "release_pending")
        return result
