"""Run pinned official Colab CLI with only its dedicated cache redirected.

This adapter runs in the CLI's interpreter. HOME, SSH paths and proxy variables
are preserved. No credential file is read, copied, swapped or parsed here.

With the verified 0.7.4 package it also hardens the provider boundary, because
the official commands report most failures as raw tracebacks or not at all:

- every Colab REST and runtime-contents request gets a bounded timeout;
- without a terminal, a missing login fails as ``auth_required`` instead of
  printing an OAuth URL and waiting for a code that cannot arrive;
- a failure ends with one classified ``LAUNCHER_PROVIDER_ERROR=`` stderr line
  (see ``provider_errors.py``) instead of a traceback that may show locals;
- ``exec`` in the launcher's exact argument shape uses a vendored copy of the
  official execution path with a real deadline, a kernel readiness/busy gate
  and an exact record of whether launcher code reached the kernel;
- ``new`` can carry the budget record's allocation key as the notebook hash,
  so a lost allocation response can later be looked up by that key with a
  lookup-only ``new`` that never sends ``POST /tun/m/assign``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import logging
import os
import queue
import runpy
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import provider_errors
from accounts import AccountError, check_cli_directory

PINNED = "0.7.4"
DEADLINE_OPTION = "--launcher-deadline="
ALLOCATION_OPTION = "--launcher-allocation="
LOOKUP_OPTION = "--launcher-lookup-only"
# Leave time to print the diagnostic before the caller's own kill timeout.
REPORT_RESERVE = 3.0
HTTP_TIMEOUT = (15.0, 120.0)
CONTENTS_TIMEOUT = (15.0, 600.0)
# A freshly allocated runtime proxy can take ~40 s to answer its first
# request; the caller's deadline still bounds every phase.
CONNECT_SECONDS = 90.0
BUSY_WAIT_SECONDS = 30.0
RECONNECT_GRACE = 10.0
# Never send launcher code that could not even start before the deadline;
# "not sent" is provable, "sent then timed out" is not.
MIN_DISPATCH_SECONDS = 2.0
PRELUDE = "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')\n"


class Failed(Exception):
    """Raised after the trace already records the classified cause."""


class AuthRequired(Exception):
    """Interactive login is needed but no terminal is attached."""


class Trace(dict):
    """Observations only; ``provider_errors.classify`` decides the code."""

    def enter(self, phase: str, **fields) -> None:
        self["phase"] = phase
        self.update(fields)


def command_details(arguments: list[str]) -> dict:
    words = [argument for argument in arguments if not argument.startswith("-")]
    command = words[0] if words else None
    details: dict = {"command": command}
    for index, argument in enumerate(arguments[:-1]):
        if argument in ("-s", "--session"):
            details["session"] = arguments[index + 1]
        elif argument == "--gpu":
            details["hardware"] = arguments[index + 1].upper()
    if command == "new":
        # Positive evidence for "not created" until POST /assign is attempted.
        details["assign_posted"] = False
        if "hardware" not in details and "--tpu" not in arguments:
            details["hardware"] = "CPU"
    return details


def launcher_exec_arguments(arguments: list[str]) -> tuple[str, str, float] | None:
    """Only the launcher's own fixed shape uses the vendored exec path."""
    if (
        len(arguments) == 8
        and arguments[:3] == ["--auth=oauth2", "exec", "-s"]
        and arguments[4] == "-f"
        and arguments[6] == "--timeout"
    ):
        try:
            seconds = float(arguments[7])
        except ValueError:
            return None
        if 0 < seconds < float("inf"):
            return arguments[3], arguments[5], seconds
    return None


def endpoint_kind(endpoint: str) -> str | None:
    path = urlparse(str(endpoint)).path.rstrip("/")
    if path.endswith("/tun/m/assignments"):
        return "assignments"
    if path.endswith("/tun/m/assign"):
        return "assign"
    if "/tun/m/unassign/" in path:
        return "unassign"
    if path.endswith("/tun/m/ccu-info"):
        return "ccu_info"
    return None


def remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def bounded_timeout(limits: tuple[float, float], deadline: float | None):
    left = remaining(deadline)
    if left is None:
        return limits
    left = max(1.0, left)
    return (min(limits[0], left), min(limits[1], left))


def bounded_call(function, seconds: float):
    """Run a blocking library call; return (finished, value, error)."""
    outcome: dict = {}

    def target():
        try:
            outcome["value"] = function()
        except BaseException as error:  # noqa: BLE001 - re-raised by caller.
            outcome["error"] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(max(0.0, seconds))
    if worker.is_alive():
        return False, None, None
    return True, outcome.get("value"), outcome.get("error")


@contextlib.contextmanager
def hardened(trace: Trace, deadline: float | None):
    """Install launcher hooks on the pinned official modules; restore after."""
    try:
        import click
        import requests
        from colab_cli import auth, client, common, contents
        from google.auth import exceptions as auth_errors
        from google.oauth2 import credentials as oauth_credentials
    except ImportError:
        yield False
        return
    restore = []

    def patch(owner, name, value):
        restore.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def network(error) -> str:
        if isinstance(error, requests.exceptions.ConnectTimeout):
            return "connect_timeout"
        if isinstance(error, requests.exceptions.Timeout):
            return "read_timeout"
        return "connection"

    original_issue = client.Client._issue_request

    def issue_request(
        self, endpoint, method="GET", headers=None, params=None, schema=None, **kwargs
    ):
        kind = endpoint_kind(endpoint)
        if kind == "assign" and method == "POST" and trace.get("lookup_only"):
            # Defense in depth under the _post_assignment refusal below.
            trace.enter("assign_lookup", endpoint="assign")
            trace["failure"] = "allocation_lookup_empty"
            raise Failed()
        trace.enter(
            f"{kind or 'colab_api'}_{str(method).lower()}",
            endpoint=kind,
            method=method,
        )
        trace.pop("http_status", None)
        trace.pop("network", None)
        if kind == "assign" and method == "POST":
            trace["assign_posted"] = True
        kwargs.setdefault("timeout", bounded_timeout(HTTP_TIMEOUT, deadline))
        try:
            return original_issue(
                self, endpoint, method, headers, params, schema, **kwargs
            )
        except client.ColabRequestError as error:
            trace["http_status"] = getattr(error.response, "status_code", None)
            raise
        except requests.exceptions.RequestException as error:
            trace["network"] = network(error)
            raise

    patch(client.Client, "_issue_request", issue_request)

    original_refresh = oauth_credentials.Credentials.refresh

    def refresh(self, request):
        previous = trace.get("phase")
        trace["phase"] = "auth_refresh"
        try:
            value = original_refresh(self, request)
        except auth_errors.RefreshError:
            trace["refresh_error"] = "rejected"
            raise
        except auth_errors.TransportError:
            trace["refresh_error"] = "transport"
            raise
        trace.pop("refresh_error", None)
        trace["phase"] = previous
        return value

    patch(oauth_credentials.Credentials, "refresh", refresh)

    original_flow = auth._run_remote_flow

    def remote_flow(client_config):
        if sys.stdin is not None and sys.stdin.isatty():
            trace.enter("oauth_interactive", endpoint="oauth")
            return original_flow(client_config)
        trace.enter("auth", endpoint="oauth", auth_required=True)
        raise AuthRequired()

    patch(auth, "_run_remote_flow", remote_flow)

    def contents_request(self, method, path, params=None, json_data=None):
        # Vendored ContentsClient._request (0.7.4) with timeouts and errors
        # that never repeat the token-bearing request URL.
        trace.enter(
            "contents_" + str(method).lower(), endpoint="contents", method=method
        )
        trace.pop("http_status", None)
        trace.pop("network", None)
        url = f"{self.base_url}/api/contents/{quote(path.strip('/'), safe='/')}"
        request_params = {"authuser": "0", "colab-runtime-proxy-token": self.token}
        if params:
            request_params.update(params)
        try:
            response = requests.request(
                method,
                url,
                params=request_params,
                json=json_data,
                timeout=bounded_timeout(CONTENTS_TIMEOUT, deadline),
            )
        except requests.exceptions.RequestException as error:
            trace["network"] = network(error)
            raise ConnectionError(
                f"runtime contents request failed ({type(error).__name__})"
            ) from None
        trace["http_status"] = response.status_code
        if response.status_code == 404:
            raise FileNotFoundError(f"File or directory not found: {path}")
        if not response.ok:
            raise ConnectionError(
                f"runtime contents API returned HTTP {response.status_code}"
            )
        trace.pop("http_status", None)
        if method == "DELETE":
            return None
        return response.json()

    patch(contents.ContentsClient, "_request", contents_request)

    original_get = common.State.get_session

    def get_session(self, name, ignore_missing_session=False):
        trace.enter("session_lookup")
        try:
            found = original_get(self, name, ignore_missing_session)
        except click.exceptions.Exit as exit:
            if exit.exit_code:
                trace["session_missing"] = True
            raise
        if found is None:
            trace["session_missing"] = True
        return found

    patch(common.State, "get_session", get_session)

    original_prune = common.State.prune_session

    def prune_session(self, name):
        trace["pruned"] = True
        return original_prune(self, name)

    patch(common.State, "prune_session", prune_session)
    try:
        yield True
    finally:
        for owner, name, value in reversed(restore):
            setattr(owner, name, value)


def allocation_key(value: str) -> uuid.UUID:
    if len(value) != 32 or value != value.lower():
        raise AccountError("Invalid launcher allocation key.")
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        raise AccountError("Invalid launcher allocation key.") from None


@contextlib.contextmanager
def allocation_identity(trace: Trace, key: uuid.UUID | None, active: bool):
    """Bind ``new`` to the caller's durable key; lookup mode never POSTs.

    The official ``new`` sends a fresh random notebook hash, so after a lost
    response nothing can find that allocation again. With the budget job's key
    as the hash, ``GET /tun/m/assign`` for the same key returns the existing
    assignment (and ``new`` saves the session name for it) instead of a token
    for a second creation, which lookup mode refuses to send.
    """
    if key is None:
        yield
        return
    if not active:
        raise AccountError("Allocation identity requires verified CLI hooks.")
    from colab_cli import client

    if not all(
        callable(getattr(client.Client, name, None))
        for name in ("assign", "_post_assignment")
    ):
        raise AccountError("Allocation identity requires google-colab-cli==0.7.4.")
    restore = []

    def patch(name, value):
        restore.append((name, getattr(client.Client, name)))
        setattr(client.Client, name, value)

    original_assign = client.Client.assign

    def assign(self, _notebook_hash, *args, **kwargs):
        return original_assign(self, key, *args, **kwargs)

    patch("assign", assign)
    if trace.get("lookup_only"):

        def refuse_creation(self, *_args, **_kwargs):
            # GET found no assignment for this key and returned a creation
            # token. Report "absent"; never turn a lookup into a creation.
            trace.enter("assign_lookup", endpoint="assign")
            trace["failure"] = "allocation_lookup_empty"
            raise Failed()

        patch("_post_assignment", refuse_creation)
    try:
        yield
    finally:
        for name, value in reversed(restore):
            setattr(client.Client, name, value)


def report(trace: Trace, returncode: int) -> dict:
    diagnostic = provider_errors.classify(dict(trace, returncode=returncode))
    print(
        "[colab-launcher] " + provider_errors.describe(diagnostic),
        file=sys.stderr,
    )
    print(provider_errors.encode(diagnostic), file=sys.stderr, flush=True)
    return diagnostic


def launcher_exec(
    trace: Trace, deadline: float | None, session: str, path: str, timeout: float
) -> int:
    """Vendored ``colab exec`` (0.7.4 commands/execution.py) for launcher code.

    Differences from the official command, all at the provider boundary:
    the prelude runs in the same execution, the caller's deadline bounds every
    phase (the official ``--timeout`` never bounds a silent execution), the
    kernel must be connected and not busy before dispatch, and only an actual
    send marks ``dispatched``. History records a digest, not the program.
    """
    import requests
    import websocket
    from colab_cli.auth import AuthProvider
    from colab_cli.common import setup_logging, state
    from colab_cli.runtime import ColabRuntime

    state.auth_provider = AuthProvider.OAUTH2
    setup_logging(False)
    started = time.monotonic()
    exec_deadline = deadline if deadline is not None else started + timeout
    trace.update(command="exec", session=session, dispatched=False)
    code = Path(path).read_text(encoding="utf-8")
    if not code.strip():
        return 0

    def left(cap: float | None = None) -> float:
        value = exec_deadline - time.monotonic()
        return max(0.0, value if cap is None else min(cap, value))

    s = state.get_session(session, ignore_missing_session=True)
    if s is None:
        raise Failed()

    def on_kernel(identifier):
        s.kernel_id = identifier
        state.store.add(s)

    def on_session(identifier):
        s.session_id = identifier
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        session_name=session,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_kernel,
        on_session_started=on_session,
    )
    trace.enter("kernel_connect", endpoint="kernel")
    digest = hashlib.sha256(code.encode()).hexdigest()
    try:
        finished, kernel, error = bounded_call(
            lambda: runtime.kernel_client, left(CONNECT_SECONDS)
        )
        if not finished:
            trace["failure"] = "kernel_connect_failed"
            raise Failed()
        if error is not None:
            status = getattr(getattr(error, "response", None), "status_code", None)
            trace.update(http_status=status, exception_type=type(error).__name__)
            # Do not prune like the official command: an orphaned assignment
            # would lose its session name. A later listing decides absence.
            trace["failure"] = (
                "runtime_lost"
                if isinstance(error, requests.HTTPError) and status in (401, 403, 404)
                else "kernel_connect_failed"
            )
            raise Failed()
        client = kernel._manager.client
        if not client.connection_ready.wait(timeout=left(60)):
            trace["failure"] = "kernel_connect_failed"
            raise Failed()

        # Best-effort gate: never queue launcher code behind a busy kernel
        # (Drive consent, an earlier timed-out call). A slow state query is
        # not a failure; the connected websocket can still dispatch.
        trace.enter("kernel_state")
        busy_until = time.monotonic() + min(BUSY_WAIT_SECONDS, left())
        kernel_state = None
        while True:
            finished, model, error = bounded_call(
                lambda: kernel._manager.refresh_model(timeout=max(1.0, left(10))),
                left(12),
            )
            status = getattr(getattr(error, "response", None), "status_code", None)
            if finished and error is None and model is None:
                trace.update(failure="runtime_lost", http_status=404)
                raise Failed()
            if isinstance(error, requests.HTTPError) and status in (401, 403):
                trace.update(failure="runtime_lost", http_status=status)
                raise Failed()
            # A failed or slow query never turns an observed "busy" into idle.
            if finished and error is None:
                kernel_state = model.get("execution_state") or kernel_state
            trace["kernel_state"] = kernel_state or "unknown"
            if kernel_state not in ("busy", "starting"):
                break
            if time.monotonic() >= busy_until:
                trace["failure"] = "kernel_busy"
                raise Failed()
            time.sleep(1.0)

        if left() < MIN_DISPATCH_SECONDS:
            trace["failure"] = "predispatch_timeout"
            raise Failed()
        s.running = "exec(colab-comfyui-launcher)"
        state.store.add(s)
        trace.enter("dispatch")
        try:
            message_id = client.execute(
                PRELUDE + code,
                silent=False,
                store_history=False,
                allow_stdin=False,
                stop_on_error=True,
            )
        except websocket.WebSocketConnectionClosedException:
            trace["failure"] = "kernel_connect_failed"
            raise Failed() from None
        except Exception as error:  # noqa: BLE001 - any send failure is classified.
            # A partial send cannot be ruled out.
            trace.update(
                dispatched=True,
                failure="connection_lost",
                exception_type=type(error).__name__,
            )
            raise Failed() from None
        trace["dispatched"] = True

        trace.enter("execute")
        failure = None
        lost_since = None
        while True:
            if left() <= 0:
                trace["failure"] = "execution_timeout"
                finished, model, _ = bounded_call(
                    lambda: kernel._manager.refresh_model(timeout=1), 1
                )
                if finished and isinstance(model, dict):
                    trace["kernel_state"] = model.get("execution_state")
                raise Failed()
            if client.connection_ready.is_set():
                lost_since = None
            else:
                lost_since = lost_since or time.monotonic()
                if time.monotonic() - lost_since > RECONNECT_GRACE:
                    trace["failure"] = "connection_lost"
                    raise Failed()
            try:
                message = client.iopub_channel.get_msg(timeout=left(0.5))
            except queue.Empty:
                continue
            if message.get("parent_header", {}).get("msg_id") != message_id:
                continue
            kind = message.get("header", {}).get("msg_type")
            content = message.get("content") or {}
            if kind == "stream":
                stream = sys.stderr if content.get("name") == "stderr" else sys.stdout
                stream.write(content.get("text", ""))
                stream.flush()
            elif kind in ("execute_result", "display_data"):
                text = (content.get("data") or {}).get("text/plain")
                if text:
                    print(text, flush=True)
            elif kind == "error":
                failure = content
                print("\n".join(content.get("traceback") or []), file=sys.stderr)
            elif kind == "status" and content.get("execution_state") == "idle":
                break
        trace["kernel_state"] = "idle"
        reply_until = time.monotonic() + left(5)
        while time.monotonic() < reply_until:
            try:
                reply = client.shell_channel.get_msg(
                    timeout=max(0.0, reply_until - time.monotonic())
                )
            except queue.Empty:
                break
            if reply.get("parent_header", {}).get("msg_id") == message_id:
                content = reply.get("content") or {}
                if content.get("status") == "error" and failure is None:
                    failure = content
                break
        if failure is not None:
            trace.update(
                failure="remote_exception", remote_exception=failure.get("ename")
            )
            raise Failed()
        return 0
    finally:
        trace_phase = trace.get("phase")
        with contextlib.suppress(Exception):
            if s.running:
                s.running = None
                state.store.add(s)
        bounded_call(runtime.stop, 5)
        with contextlib.suppress(Exception):
            state.history.log_event(
                session,
                "execution",
                {
                    "source": "colab-comfyui-launcher",
                    "code_sha256": digest,
                    "dispatched": trace.get("dispatched"),
                    "phase": trace_phase,
                },
            )


def run(directory: Path, arguments: list[str]) -> None:
    if importlib.metadata.version("google-colab-cli") != PINNED:
        raise AccountError("Named accounts require verified google-colab-cli==0.7.4.")
    check_cli_directory(directory)
    os.umask(0o077)
    deadline = key = None
    lookup_only = False
    while arguments and arguments[0].startswith("--launcher-"):
        option, arguments = arguments[0], arguments[1:]
        if option.startswith(DEADLINE_OPTION):
            try:
                seconds = float(option[len(DEADLINE_OPTION) :])
            except ValueError:
                raise AccountError("Invalid launcher deadline.") from None
            if not 0 < seconds < float("inf"):
                raise AccountError("Invalid launcher deadline.")
            deadline = time.monotonic() + max(1.0, seconds - REPORT_RESERVE)
        elif option.startswith(ALLOCATION_OPTION):
            key = allocation_key(option[len(ALLOCATION_OPTION) :])
        elif option == LOOKUP_OPTION:
            lookup_only = True
        else:
            raise AccountError("Unknown launcher option.")
    if (key is not None or lookup_only) and command_details(arguments)[
        "command"
    ] != "new":
        raise AccountError("Allocation options apply only to 'new'.")
    if lookup_only and key is None:
        raise AccountError("Allocation lookup requires the original key.")
    expanduser = os.path.expanduser

    def profile_path(path):
        # Exact CLI-owned namespace only; client OAuth config and SSH retain
        # their normal paths. All CLI token/session/settings/history/log paths
        # in 0.7.4 use this function before accessing their caches.
        prefix = "~/.config/colab-cli"
        if isinstance(path, str) and (path == prefix or path.startswith(prefix + "/")):
            return str(directory) + path[len(prefix) :]
        return expanduser(path)

    os.path.expanduser = profile_path
    sys.argv = ["colab", *arguments]
    trace = Trace(command_details(arguments))
    if lookup_only:
        trace["lookup_only"] = True
    try:
        # Imports happen after the path redirect: auth.py resolves its token
        # path at import time.
        with (
            hardened(trace, deadline) as active,
            allocation_identity(trace, key, active),
        ):
            exec_request = launcher_exec_arguments(arguments) if active else None
            try:
                if exec_request:
                    returncode = launcher_exec(trace, deadline, *exec_request)
                    sys.stdout.flush()
                    raise SystemExit(returncode)
                runpy.run_module("colab_cli.cli", run_name="__main__")
            except SystemExit as exit:
                code = exit.code
                if active and code not in (0, None):
                    report(trace, code if type(code) is int else 1)
                raise
            except Exception as error:
                if not active:
                    raise
                if not isinstance(error, (Failed, AuthRequired)):
                    trace.setdefault("exception_type", type(error).__name__)
                report(trace, 1)
                raise SystemExit(1) from None
    finally:
        os.path.expanduser = expanduser


def main(argv: list[str]) -> int:
    try:
        if len(argv) < 3:
            raise AccountError("Missing account context or command.")
        run(Path(argv[1]), argv[2:])
    except (AccountError, importlib.metadata.PackageNotFoundError):
        print(
            "Colab account context could not start; check private cache permissions and google-colab-cli==0.7.4.",
            file=sys.stderr,
        )
        print(
            provider_errors.encode(
                dict(
                    provider_errors.classify({"returncode": 2}),
                    code="cli_unavailable",
                    retryable=False,
                )
            ),
            file=sys.stderr,
        )
        return 2
    except SystemExit as exit:
        code = exit.code
        return code if type(code) is int else (0 if code is None else 1)
    return 0


if __name__ == "__main__":
    status = main(sys.argv)
    # Library threads (websocket, kernel client) may be non-daemon and would
    # otherwise keep a finished or failed command alive past its report.
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
        logging.shutdown()
    os._exit(status)
