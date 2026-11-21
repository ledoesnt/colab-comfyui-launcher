"""Classified Colab provider failures shared by the shim and its consumers.

The shim (``account_cli.py``) runs inside the official CLI interpreter and
records *observations* only: which endpoint/phase failed, an HTTP status, a
network failure kind, whether launcher code reached the kernel. ``classify``
turns them into one stable code. Messages are always written here, never
copied from provider text, so they cannot carry URLs, tokens or bodies.
Standard library only: consumers run under the launcher's own Python.
"""

from __future__ import annotations

import json
import re
from typing import Any

PREFIX = "LAUNCHER_PROVIDER_ERROR="
VERSION = 1

# code: (summary, retryable, next step)
CODES: dict[str, tuple[str, bool, str]] = {
    "auth_required": (
        "Colab login for this account is missing, expired or revoked.",
        False,
        "Sign in interactively (dashboard: Manage Colab account -> Sign in), then retry.",
    ),
    "auth_refresh_failed": (
        (
            "The saved Colab login could not be refreshed because Google's token "
            "service was unreachable; the login itself is unverified, not proven expired."
        ),
        True,
        "Check network connectivity and retry; sign in again only if the retry reports auth_required.",
    ),
    "permission_denied": (
        "Colab rejected the request as unauthorized (HTTP {http_status}).",
        False,
        "Recheck the login of this account; sign in again if needed.",
    ),
    "capacity_unavailable": (
        "Colab has no {hardware} capacity available right now (HTTP 503 on allocation).",
        True,
        "Retry later or choose different hardware.",
    ),
    "allocation_refused": (
        (
            "Colab refused the allocation (HTTP 412): too many active sessions, or a "
            "temporary usage/capacity limit for {hardware}."
        ),
        True,
        "Release unused runtimes of this account, wait, or choose different hardware.",
    ),
    "accelerator_not_entitled": (
        (
            "Colab rejected {hardware} for this account (HTTP 400): no quota or "
            "entitlement for that accelerator."
        ),
        False,
        "Choose another accelerator or account; retrying the same request will fail again.",
    ),
    "rate_limited": (
        "Colab rate-limited the request (HTTP 429).",
        True,
        "Wait a minute before retrying.",
    ),
    "provider_server_error": (
        "Colab returned a server error (HTTP {http_status}) during {phase}.",
        True,
        "Query the current state before retrying; the provider may recover shortly.",
    ),
    "provider_http_error": (
        "Colab rejected the request (HTTP {http_status}) during {phase}.",
        False,
        "Inspect the request parameters and account state before retrying.",
    ),
    "network_error": (
        "Could not reach Colab during {phase} (connection failed).",
        True,
        "Check network connectivity, then retry.",
    ),
    "http_timeout": (
        "Colab did not answer in time during {phase}.",
        True,
        "Query the current state before retrying.",
    ),
    "allocation_lookup_empty": (
        (
            "No runtime exists yet for the original allocation key of session "
            "'{session}'; the original request may still be in progress. This "
            "lookup sent no creation request."
        ),
        False,
        "Keep polling 'budget' with the same request_id; never repeat 'new'.",
    ),
    "session_not_found": (
        "Session '{session}' is not known to this account's Colab CLI.",
        False,
        "Check the session name and --account; list sessions before creating a new runtime.",
    ),
    "runtime_gone": (
        (
            "The Colab runtime for session '{session}' no longer exists on the server "
            "(stopped, recycled or idle-timed-out)."
        ),
        False,
        "Treat the VM and its local files as lost; create a new runtime to continue.",
    ),
    "runtime_lost": (
        (
            "The runtime for session '{session}' rejected the connection "
            "(HTTP {http_status}); it was probably recycled."
        ),
        False,
        "Inspect sessions; create a new runtime if it is gone.",
    ),
    "kernel_connect_failed": (
        "Could not open the runtime kernel connection for session '{session}'.",
        True,
        "Retry; a retry cannot duplicate work.",
    ),
    "predispatch_timeout": (
        (
            "The deadline ran out before launcher code could be sent to session "
            "'{session}' (slow runtime connection or a busy kernel)."
        ),
        True,
        "Retry with a longer --timeout; a retry cannot duplicate work.",
    ),
    "kernel_busy": (
        (
            "The runtime kernel for session '{session}' stayed busy with another "
            "execution (for example Drive authorization or an earlier timed-out call)."
        ),
        True,
        "Wait for the other execution to finish, then retry.",
    ),
    "execution_timeout": (
        (
            "Launcher code was sent to session '{session}' but did not finish within "
            "the deadline (kernel state: {kernel_state})."
        ),
        False,
        "Do not resend: query the request/job state first; the kernel may still be running it.",
    ),
    "connection_lost": (
        (
            "The runtime connection for session '{session}' closed after launcher "
            "code was sent."
        ),
        False,
        "Do not resend: query the request/job state first.",
    ),
    "remote_exception": (
        "Launcher code raised {remote_exception} on the runtime of session '{session}'.",
        False,
        "Inspect status and the sanitized stderr; fix the cause before retrying.",
    ),
    "file_not_found": (
        "The runtime file was not found during {phase}.",
        False,
        "Check the remote path; deploy or produce the file first.",
    ),
    "transfer_failed": (
        "A runtime file transfer failed during {phase}.",
        True,
        "Retry the transfer; verify checksums afterwards.",
    ),
    "local_timeout": (
        "The local Colab CLI process exceeded its {seconds}s limit before reporting a result.",
        False,
        "Query the current state before retrying; the outcome is unknown.",
    ),
    "cli_unavailable": (
        "The official Colab CLI could not start (missing, or not google-colab-cli==0.7.4).",
        False,
        "Install google-colab-cli==0.7.4 as described in README.md.",
    ),
    "unexpected_error": (
        "The Colab CLI failed with {exception_type} during {phase}.",
        False,
        "Inspect the current state before retrying.",
    ),
    "command_failed": (
        "The Colab CLI exited with status {returncode} without a classified cause.",
        False,
        "Inspect the current state before retrying.",
    ),
}

OUTCOMES = frozenset({"not_created", "not_dispatched", "unknown", "failed", "none"})
OUTCOME_TEXT = {
    "not_created": "No runtime was created.",
    "not_dispatched": "Nothing was sent to the runtime.",
    "unknown": "The outcome is unknown.",
    "failed": "The operation ran and failed.",
    "none": "",
}
MUTATIONS = frozenset({"new", "stop", "exec", "upload"})
# Failures before launcher code reached the kernel; a retry cannot duplicate work.
PREDISPATCH_RETRY = frozenset(
    {
        "kernel_connect_failed",
        "predispatch_timeout",
        "kernel_busy",
        "network_error",
        "http_timeout",
        "provider_server_error",
        "auth_refresh_failed",
    }
)

_WORD = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,79}\Z")
_HARDWARE = re.compile(r"[A-Z0-9]{1,8}\Z")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ENDPOINTS = frozenset(
    {"assign", "assignments", "unassign", "ccu_info", "kernel", "contents", "oauth"}
)
_COMMANDS = frozenset(
    {"new", "stop", "exec", "upload", "download", "sessions", "usage", "status"}
)


def _status_code(observed: dict[str, Any]) -> int | None:
    value = observed.get("http_status")
    return value if type(value) is int and 100 <= value <= 599 else None


def _shape(observed: dict[str, Any]) -> dict[str, Any]:
    """Whitelisted, type-checked descriptive fields; never free text."""
    command = observed.get("command")
    command = command if command in _COMMANDS else "other"
    endpoint = observed.get("endpoint")
    dispatched = observed.get("dispatched")
    phase = observed.get("phase")
    hardware = observed.get("hardware")
    session = observed.get("session")
    state = observed.get("kernel_state")
    returncode = observed.get("returncode")
    shaped: dict[str, Any] = {
        "v": VERSION,
        "command": command,
        "phase": phase if isinstance(phase, str) and _WORD.fullmatch(phase) else None,
        "http_status": _status_code(observed),
        "endpoint": endpoint if endpoint in _ENDPOINTS else None,
        "dispatched": dispatched
        if command == "exec" and dispatched in (True, False)
        else None,
        "hardware": hardware
        if isinstance(hardware, str) and _HARDWARE.fullmatch(hardware)
        else None,
        "session": session
        if isinstance(session, str) and _SESSION.fullmatch(session)
        else None,
        "kernel_state": state
        if isinstance(state, str) and _WORD.fullmatch(state)
        else None,
        "returncode": returncode if type(returncode) is int else None,
    }
    for key in ("exception_type", "remote_exception"):
        value = observed.get(key)
        shaped[key] = (
            value if isinstance(value, str) and _NAME.fullmatch(value) else None
        )
    return shaped


def classify(observed: dict[str, Any]) -> dict[str, Any]:
    """Map raw shim observations to one diagnostic; unknown keys are ignored."""
    shaped = _shape(observed)
    command, endpoint, status = (
        shaped["command"],
        shaped["endpoint"],
        shaped["http_status"],
    )
    method = observed.get("method")
    network = observed.get("network")
    failure = observed.get("failure")
    mutation = command in MUTATIONS

    def nothing_sent() -> str:
        if command == "new":
            return "not_created"
        if command == "exec":
            return outcome
        return "not_dispatched" if mutation else "none"

    if command == "exec":
        # Only the vendored exec's explicit record of a never-sent program
        # (False, not merely absent evidence: a killed or unhardened CLI
        # reports nothing) proves a retry cannot duplicate work.
        dispatched = observed.get("dispatched")
        outcome = "not_dispatched" if dispatched is False else "unknown"
    elif command == "new":
        # Only the shim's explicit record of a never-sent POST /assign (False,
        # not merely absent evidence) or a refusal proves nothing was created.
        outcome = "not_created" if observed.get("assign_posted") is False else "unknown"
    else:
        outcome = "unknown" if mutation else "none"

    if failure in (
        "kernel_connect_failed",
        "predispatch_timeout",
        "kernel_busy",
        "execution_timeout",
        "connection_lost",
        "remote_exception",
        "runtime_lost",
        "file_not_found",
        "transfer_failed",
        "allocation_lookup_empty",
    ):
        code = failure
        if failure == "remote_exception":
            outcome = "failed"
    elif observed.get("refresh_error") == "transport":
        code, outcome = "auth_refresh_failed", nothing_sent()
    elif observed.get("auth_required") or observed.get("refresh_error") == "rejected":
        code, outcome = "auth_required", nothing_sent()
    elif observed.get("session_missing"):
        code = "runtime_gone" if observed.get("pruned") else "session_not_found"
        outcome = nothing_sent()
    elif status is not None:
        if endpoint == "assign":
            if method != "POST":
                code = (
                    "provider_server_error" if status >= 500 else "provider_http_error"
                )
                outcome = "not_created"
            elif status == 412:
                code, outcome = "allocation_refused", "not_created"
            elif status == 400 and shaped["hardware"] not in (None, "CPU"):
                code, outcome = "accelerator_not_entitled", "not_created"
            elif status == 503:
                code, outcome = "capacity_unavailable", "not_created"
            elif status == 429:
                code, outcome = "rate_limited", "not_created"
            elif status in (401, 403):
                code, outcome = "permission_denied", "not_created"
            elif status >= 500:
                code, outcome = "provider_server_error", "unknown"
            else:
                code, outcome = "provider_http_error", "not_created"
        elif endpoint in ("kernel", "contents") and status in (401, 403, 404):
            code = (
                "file_not_found"
                if endpoint == "contents" and status == 404
                else "runtime_lost"
            )
        elif status in (401, 403):
            code = "permission_denied"
        elif status == 429:
            code = "rate_limited"
        elif status >= 500:
            code = "provider_server_error"
        else:
            code = "provider_http_error"
    elif network in ("connect_timeout", "connection", "read_timeout"):
        code = "http_timeout" if network == "read_timeout" else "network_error"
        if endpoint == "assign" and method == "POST":
            # A connect timeout never sent the POST; any later failure may have.
            outcome = "not_created" if network == "connect_timeout" else "unknown"
    elif observed.get("exception_type"):
        code = "unexpected_error"
    else:
        code = "command_failed"
    if observed.get("lookup_only") is True:
        # An allocation lookup can never POST; it changes nothing remotely.
        outcome = "none"
    return dict(shaped, code=code, outcome=outcome, retryable=CODES[code][1])


def validate(value: Any) -> dict[str, Any] | None:
    """Accept only a well-formed diagnostic; rebuild it from whitelisted fields."""
    if not isinstance(value, dict) or value.get("v") != VERSION:
        return None
    code = value.get("code")
    if code not in CODES:
        return None
    outcome = value.get("outcome")
    return dict(
        _shape(value),
        code=code,
        outcome=outcome if outcome in OUTCOMES else "unknown",
        retryable=CODES[code][1],
    )


def encode(diagnostic: dict[str, Any]) -> str:
    return PREFIX + json.dumps(diagnostic, separators=(",", ":"), sort_keys=True)


def parse(stderr: str | None) -> tuple[dict[str, Any] | None, str]:
    """Return the last valid diagnostic line and stderr without such lines."""
    if not isinstance(stderr, str):
        return None, ""
    found = None
    kept = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith(PREFIX):
            try:
                candidate = validate(json.loads(stripped[len(PREFIX) :]))
            except (ValueError, RecursionError):
                candidate = None
            found = candidate or found
            continue
        kept.append(line)
    return found, "\n".join(kept)


_FALLBACKS = (
    (re.compile(r"\[colab\] Session '[^']*' not found\."), "session_not_found"),
    (re.compile(r"Allocation refused \(precondition failed\)"), "allocation_refused"),
    (re.compile(r"Backend rejected accelerator"), "accelerator_not_entitled"),
    (re.compile(r"appears to be lost \(404/401\)"), "runtime_lost"),
    (
        re.compile(
            r"To authorize colab-cli, visit this URL|Enter the authorization code"
        ),
        "auth_required",
    ),
)


def fallback(
    command: str | None,
    stdout: str,
    stderr: str,
    returncode: int | None,
    *,
    session: str | None = None,
    hardware: str | None = None,
) -> dict[str, Any]:
    """Classify an unhardened CLI exit from its known fixed official messages."""
    text = f"{stdout}\n{stderr}"
    code = next((code for pattern, code in _FALLBACKS if pattern.search(text)), None)
    observed: dict[str, Any] = {
        "command": command,
        "session": session,
        "hardware": hardware,
        "returncode": returncode,
    }
    if code in ("session_not_found", "auth_required"):
        observed["session_missing" if code == "session_not_found" else code] = True
        # The official CLI prints these before it opens any kernel connection.
        observed["dispatched"] = False
    elif code in ("allocation_refused", "accelerator_not_entitled"):
        observed.update(
            endpoint="assign",
            method="POST",
            assign_posted=True,
            http_status=412 if code == "allocation_refused" else 400,
        )
    elif code == "runtime_lost":
        observed.update(failure="runtime_lost", endpoint="kernel")
    diagnostic = classify(observed)
    if code in ("allocation_refused", "accelerator_not_entitled"):
        diagnostic["outcome"] = "not_created"
    return diagnostic


class _Defaults(dict):
    def __missing__(self, key):
        return {
            "hardware": "the requested hardware",
            "session": "the selected session",
            "phase": "the provider request",
            "kernel_state": "unknown",
            "remote_exception": "an exception",
            "exception_type": "an unexpected error",
            "http_status": "?",
            "returncode": "?",
            "seconds": "?",
        }.get(key, "?")


def describe(diagnostic: dict[str, Any], **context: Any) -> str:
    """One self-contained sentence set: cause, certainty and the next step."""
    code = diagnostic.get("code")
    summary, _, step = CODES.get(code, CODES["command_failed"])
    values = _Defaults(
        {
            key: value
            for key, value in {**diagnostic, **context}.items()
            if value is not None
        }
    )
    if values.get("phase"):
        values["phase"] = str(values["phase"]).replace("_", " ")
    parts = [summary.format_map(values)]
    outcome = OUTCOME_TEXT.get(diagnostic.get("outcome"), "")
    if outcome:
        parts.append(outcome)
    parts.append(step)
    return " ".join(parts)


def next_step(diagnostic: dict[str, Any]) -> str:
    return CODES.get(diagnostic.get("code"), CODES["command_failed"])[2]


def public(diagnostic: dict[str, Any], **context: Any) -> dict[str, Any]:
    """The JSON shape exposed to agents: classified fields plus derived text."""
    return {
        **{key: value for key, value in diagnostic.items() if key != "v"},
        "message": describe(diagnostic, **context),
        "next": next_step(diagnostic),
    }
