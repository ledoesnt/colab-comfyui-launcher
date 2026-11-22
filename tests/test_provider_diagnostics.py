"""Classified provider failures: classifier, consumers and the CLI shim.

Fixtures only: no runtime allocation, network or real credentials. The shim
tests run inside the official CLI interpreter against the pinned 0.7.4
modules, with fake HTTP sessions and a fake kernel; they are skipped when
that interpreter is not installed.
"""

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import budget_guard as budget
import colab_provider as provider
import colabctl as ctl
import provider_errors as pe
from client_requests import ClientRequests
from test_budget_guard import Clock, Provider


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def shim_line(**observed):
    return pe.encode(pe.classify(observed))


class ClassifierTests(unittest.TestCase):
    def assign(self, **observed):
        return pe.classify(
            dict(
                command="new",
                hardware="G4",
                endpoint="assign",
                method="POST",
                assign_posted=True,
                **observed,
            )
        )

    def test_allocation_http_refusals_are_definitive(self):
        for status, code, retryable in (
            (503, "capacity_unavailable", True),
            (412, "allocation_refused", True),
            (400, "accelerator_not_entitled", False),
            (429, "rate_limited", True),
            (403, "permission_denied", False),
            (404, "provider_http_error", False),
        ):
            with self.subTest(status=status):
                diagnostic = self.assign(http_status=status)
                self.assertEqual(diagnostic["code"], code)
                self.assertEqual(diagnostic["outcome"], "not_created")
                self.assertEqual(diagnostic["retryable"], retryable)
        message = pe.describe(self.assign(http_status=503))
        self.assertIn("no G4 capacity", message)
        self.assertIn("No runtime was created", message)

    def test_allocation_outcome_unknown_without_proof(self):
        self.assertEqual(self.assign(http_status=500)["outcome"], "unknown")
        self.assertEqual(self.assign(network="read_timeout")["outcome"], "unknown")
        self.assertEqual(self.assign(network="connection")["outcome"], "unknown")
        self.assertEqual(
            self.assign(network="connect_timeout")["outcome"], "not_created"
        )
        # Absent evidence (no shim) is unknown; explicit False proves no POST.
        self.assertEqual(pe.classify({"command": "new"})["outcome"], "unknown")
        self.assertEqual(
            pe.classify(
                {"command": "new", "assign_posted": False, "exception_type": "X"}
            )["outcome"],
            "not_created",
        )
        self.assertEqual(
            pe.classify(
                {
                    "command": "new",
                    "endpoint": "assign",
                    "method": "GET",
                    "http_status": 500,
                }
            )["outcome"],
            "not_created",
        )

    def test_exec_dispatch_boundary(self):
        before = pe.classify(
            {"command": "exec", "failure": "kernel_busy", "dispatched": False}
        )
        self.assertEqual(
            (before["code"], before["outcome"]), ("kernel_busy", "not_dispatched")
        )
        after = pe.classify(
            {"command": "exec", "failure": "execution_timeout", "dispatched": True}
        )
        self.assertEqual(after["outcome"], "unknown")
        raised = pe.classify(
            {
                "command": "exec",
                "failure": "remote_exception",
                "dispatched": True,
                "remote_exception": "ValueError",
            }
        )
        self.assertEqual(
            (raised["outcome"], raised["remote_exception"]), ("failed", "ValueError")
        )

    def test_auth_and_session_causes(self):
        self.assertEqual(
            pe.classify({"command": "sessions", "refresh_error": "transport"})["code"],
            "auth_refresh_failed",
        )
        expired = pe.classify({"command": "usage", "refresh_error": "rejected"})
        self.assertEqual(
            (expired["code"], expired["outcome"]), ("auth_required", "none")
        )
        gone = pe.classify(
            {
                "command": "exec",
                "session_missing": True,
                "pruned": True,
                "session": "s1",
                "dispatched": False,
            }
        )
        self.assertEqual(
            (gone["code"], gone["outcome"]), ("runtime_gone", "not_dispatched")
        )
        # Without the vendored exec's explicit record nothing proves "not sent".
        unproven = pe.classify(
            {"command": "exec", "session_missing": True, "pruned": True}
        )
        self.assertEqual(unproven["outcome"], "unknown")
        self.assertIn("'s1' no longer exists", pe.describe(gone))
        missing = pe.classify({"command": "exec", "session_missing": True})
        self.assertEqual(missing["code"], "session_not_found")

    def test_parse_validates_and_never_trusts_provider_text(self):
        injected = json.loads(
            shim_line(command="exec", failure="kernel_busy")[len(pe.PREFIX) :]
        )
        line = pe.PREFIX + json.dumps(
            dict(
                injected,
                phase="see https://private.invalid/x?token=abc",
                exception_type="token=abc def",
                session="ok-session",
                message="refresh_token=PRIVATE",
                extra="PRIVATE",
            )
        )
        diagnostic, rest = pe.parse("before\n" + line + "\nafter")
        self.assertEqual(rest, "before\nafter")
        self.assertEqual(diagnostic["code"], "kernel_busy")
        self.assertIsNone(diagnostic["phase"])
        self.assertIsNone(diagnostic["exception_type"])
        self.assertEqual(diagnostic["session"], "ok-session")
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))
        self.assertNotIn("PRIVATE", json.dumps(pe.public(diagnostic)))
        for bad in (
            "{",
            json.dumps({"v": 2, "code": "kernel_busy"}),
            json.dumps({"v": 1, "code": "made_up"}),
            json.dumps([1]),
        ):
            self.assertIsNone(pe.parse(pe.PREFIX + bad)[0])

    def test_fixed_official_messages_fallback(self):
        for text, code in (
            ("[colab] Session 'x' not found.", "session_not_found"),
            (
                "[colab] Allocation refused (precondition failed). This can",
                "allocation_refused",
            ),
            ("[colab] Backend rejected accelerator 'G4'.", "accelerator_not_entitled"),
            ("Enter the authorization code: ", "auth_required"),
            ("unrecognized", "command_failed"),
        ):
            with self.subTest(code=code):
                diagnostic = pe.fallback("new", text, "", 1, hardware="G4")
                self.assertEqual(diagnostic["code"], code)
        self.assertEqual(
            pe.fallback(
                "new", "[colab] Allocation refused (precondition failed)", "", 1
            )["outcome"],
            "not_created",
        )


class ProviderConsumerTests(unittest.TestCase):
    def setUp(self):
        self.run = mock.Mock()
        self.api = provider.ColabProvider(self.run, oauth_cache=Path("/nonexistent"))

    def test_structured_capacity_refusal_is_a_definitive_failure(self):
        self.run.return_value = completed(
            "[colab] Creating session 'selected'...\n",
            "[colab-launcher] text\n"
            + shim_line(
                command="new",
                hardware="G4",
                endpoint="assign",
                method="POST",
                assign_posted=True,
                http_status=503,
            ),
            1,
        )
        with self.assertRaises(provider.ProviderError) as caught:
            self.api.create("selected", "G4")
        error = caught.exception
        self.assertEqual(error.code, "capacity_unavailable")
        self.assertFalse(error.outcome_unknown)
        self.assertIn("no G4 capacity", str(error))
        self.assertEqual(error.diagnostic["http_status"], 503)

    def test_structured_auth_failure_on_read(self):
        self.run.return_value = completed(
            "", shim_line(command="sessions", auth_required=True), 1
        )
        with self.assertRaises(provider.ProviderError) as caught:
            self.api.sessions()
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertIn("Sign in", str(caught.exception))

    def test_unclassified_creation_failure_stays_unknown(self):
        self.run.return_value = completed("", "Traceback: boom", 1)
        with self.assertRaises(provider.ProviderError) as caught:
            self.api.create("selected", "T4")
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(caught.exception.code, "command_failed")


class BudgetAllocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.clock = Clock()
        self.provider = Provider()
        self.manager = budget.BudgetManager(
            Path(temporary.name) / "budgets",
            self.provider,
            clock=lambda: self.clock.wall,
            uptime=lambda: self.clock.up,
            boot_identity=lambda: self.clock.boot,
        )
        self.manager.ensure_guard = mock.Mock()
        self.manager._guard_alive = mock.Mock(return_value=True)

    def refusal(self, outcome_unknown=False):
        diagnostic = pe.classify(
            {
                "command": "new",
                "hardware": "T4",
                "endpoint": "assign",
                "method": "POST",
                "assign_posted": True,
                "http_status": 503,
            }
        )
        return provider.ProviderError(
            "unsafe provider text token=PRIVATE",
            code="capacity_unavailable",
            outcome_unknown=outcome_unknown,
            diagnostic=diagnostic,
        )

    def create(self, error, **kwargs):
        self.provider.create_visible = False
        with mock.patch.object(self.provider, "create", side_effect=error):
            return self.manager.create(
                "session-test",
                "T4",
                limit_cu=10,
                rate_ceiling=4,
                reserve_cu=2,
                **kwargs,
            )

    def test_definitive_refusal_with_clean_listing_is_terminal_and_explained(self):
        result = self.create(self.refusal(), request_id="first")
        self.assertEqual(result["status"], "allocation_failed")
        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["allocation_error_code"], "capacity_unavailable")
        self.assertIn("no T4 capacity", result["error"])
        self.assertIn("Retry later", result["next"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertIn("allocation_failed", budget.TERMINAL)
        # The same request ID only replays; a new request may allocate again.
        replay = self.manager.create(
            "session-test",
            "T4",
            limit_cu=10,
            rate_ceiling=4,
            reserve_cu=2,
            request_id="first",
        )
        self.assertEqual(replay["job_id"], result["job_id"])
        self.provider.create_visible = True
        again = self.manager.create(
            "session-test",
            "T4",
            limit_cu=10,
            rate_ceiling=4,
            reserve_cu=2,
            request_id="second",
        )
        self.assertEqual(again["status"], "active")

    def test_new_orphan_assignment_keeps_outcome_unknown(self):
        self.provider.rows = [
            {"name": "?", "assignment_id": "unexplained", "hardware": "T4"}
        ]
        with mock.patch.object(
            self.provider, "sessions", side_effect=[[], list(self.provider.rows)]
        ):
            result = self.create(self.refusal())
        self.assertEqual(result["status"], "allocation_unknown")
        self.assertIn("unnamed new assignment", result["reason"])
        self.assertIn("do not repeat 'new'", result["error"])

    def test_unknown_outcome_never_becomes_terminal(self):
        result = self.create(self.refusal(outcome_unknown=True))
        self.assertEqual(result["status"], "allocation_unknown")
        self.assertFalse(result["terminal"])

    def test_create_waits_for_guard_reconcile_instead_of_reporting_pending(self):
        release = threading.Event()
        held = threading.Event()

        def guard():
            with self.manager._lock("session-test", ".reconcile.lock"):
                held.set()
                release.wait(5)

        original = self.provider.create

        def create(session, gpu=None):
            original(session, gpu)
            worker = threading.Thread(target=guard)
            worker.start()
            held.wait(5)
            threading.Timer(0.6, release.set).start()

        with mock.patch.object(self.provider, "create", side_effect=create):
            started = time.monotonic()
            result = self.manager.create(
                "session-test", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2
            )
        self.assertEqual(result["status"], "active")
        self.assertGreaterEqual(time.monotonic() - started, 0.5)


class BridgeTests(unittest.TestCase):
    def exec_failure(self, **observed):
        return completed(
            "",
            "[colab-launcher] x\n"
            + shim_line(command="exec", session="s1", **observed),
            1,
        )

    def test_predispatch_failure_is_retried_then_succeeds(self):
        success = completed('LAUNCHER_RESULT={"ok": true, "status": "ready"}\n')
        with (
            mock.patch.object(
                ctl,
                "_run_colab",
                side_effect=[
                    self.exec_failure(
                        failure="kernel_connect_failed", dispatched=False
                    ),
                    success,
                ],
            ) as run,
            mock.patch.object(ctl.time, "sleep"),
            redirect_stderr(io.StringIO()),
        ):
            result = ctl.execute_remote("s1", {"action": "status"}, 120)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["attempts"], 2)

    def test_exhausted_retries_report_the_busy_kernel_cause(self):
        with (
            mock.patch.object(
                ctl,
                "_run_colab",
                side_effect=[
                    self.exec_failure(failure="kernel_busy", dispatched=False),
                    self.exec_failure(failure="kernel_busy", dispatched=False),
                    self.exec_failure(
                        failure="kernel_connect_failed", dispatched=False
                    ),
                ],
            ),
            mock.patch.object(ctl.time, "sleep"),
            redirect_stderr(io.StringIO()),
            self.assertRaises(ctl.BridgeError) as caught,
        ):
            ctl.execute_remote("s1", {"action": "status"}, 120)
        result = caught.exception.result
        self.assertEqual(result["provider_error"]["code"], "kernel_busy")
        self.assertEqual(
            result["attempt_codes"],
            ["kernel_busy", "kernel_busy", "kernel_connect_failed"],
        )
        self.assertEqual(result["status"], "not_submitted")

    def test_dispatched_or_permanent_failures_are_not_retried(self):
        for observed, status in (
            (
                {"failure": "execution_timeout", "dispatched": True},
                "submission_unknown",
            ),
            ({"failure": "connection_lost", "dispatched": True}, "submission_unknown"),
            ({"session_missing": True, "dispatched": False}, "not_submitted"),
            (
                {
                    "failure": "remote_exception",
                    "dispatched": True,
                    "remote_exception": "KeyError",
                },
                "failed",
            ),
        ):
            with self.subTest(observed=observed):
                with (
                    mock.patch.object(
                        ctl, "_run_colab", return_value=self.exec_failure(**observed)
                    ) as run,
                    self.assertRaises(ctl.BridgeError) as caught,
                ):
                    ctl.execute_remote("s1", {"action": "status"}, 120)
                self.assertEqual(run.call_count, 1)
                result = caught.exception.result
                self.assertEqual(result["status"], status)
                self.assertEqual(
                    result["provider_error"]["outcome"],
                    pe.classify({"command": "exec", **observed})["outcome"],
                )
                self.assertIn(
                    result["provider_error"]["message"], str(caught.exception)
                )

    def test_retry_stops_when_deadline_is_short(self):
        with (
            mock.patch.object(
                ctl,
                "_run_colab",
                return_value=self.exec_failure(failure="kernel_busy", dispatched=False),
            ) as run,
            self.assertRaises(ctl.BridgeError) as caught,
        ):
            ctl.execute_remote("s1", {"action": "status"}, 10)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(caught.exception.result["resubmit_allowed"])

    def test_local_timeout_is_explained(self):
        with (
            mock.patch.object(ctl.shutil, "which", return_value="/fixture/colab"),
            mock.patch.object(
                ctl, "colab_command", side_effect=lambda argv, **kw: argv
            ),
            mock.patch.object(
                ctl.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["colab"], 30),
            ),
            self.assertRaises(ctl.BridgeError) as caught,
        ):
            ctl._run_colab(["exec", "-s", "s1"], 30)
        self.assertIn("exceeded its 30s limit", str(caught.exception))
        self.assertEqual(caught.exception.result["status"], "submission_unknown")

    def test_not_dispatched_submission_receipt_is_terminal(self):
        from test_atomic_admission import initial, manager
        from test_submit_receipt_deadline import call_context

        with tempfile.TemporaryDirectory() as folder:
            m, clock, api = manager(Path(folder) / "budgets")
            initial(m)
            with (
                mock.patch.object(budget.time, "monotonic", clock),
                call_context(folder, m, api),
                mock.patch.object(
                    ctl,
                    "_run_colab",
                    return_value=completed(
                        "",
                        shim_line(
                            command="exec", failure="kernel_busy", dispatched=False
                        ),
                        1,
                    ),
                ),
                mock.patch.object(ctl.time, "sleep"),
                self.assertRaises(ctl.BridgeError) as caught,
            ):
                ctl.submit_command(
                    types.SimpleNamespace(
                        session="episode", request_id="busy-1", timeout=20
                    ),
                    {"action": "install", "install_timeout": 60},
                )
            result = caught.exception.result
            self.assertEqual(result["status"], "not_submitted")
            self.assertTrue(result["terminal"])
            self.assertTrue(result["resubmit_allowed"])
            self.assertFalse(result["job_may_still_be_running"])
            stored = ClientRequests("episode", Path(folder) / "receipts").read("busy-1")
            self.assertEqual(stored["result"]["status"], "not_submitted")
            self.assertEqual(stored["result"]["provider_error"]["code"], "kernel_busy")

    def test_status_summary_names_the_failed_phase(self):
        summary = ctl.status_summary(
            {
                "deployed": True,
                "installation": {
                    "status": "failed",
                    "phase": "packages",
                    "exit_code": 1,
                    "log_tail": ["ERROR: resolver"],
                },
            }
        )
        self.assertEqual(summary["state"], "install_failed")
        self.assertEqual(summary["problems"][0]["phase"], "packages")
        self.assertEqual(summary["problems"][0]["log_tail"], ["ERROR: resolver"])
        blocked = ctl.status_summary(
            {"deployed": True, "budget": {"managed": True, "status": "release_pending"}}
        )
        self.assertEqual(blocked["state"], "budget_blocked")
        ready = ctl.status_summary(
            {
                "deployed": True,
                "installation": {"status": "ready"},
                "comfyui_alive": True,
                "http_ready": True,
                "access_mode": "local",
            }
        )
        self.assertEqual(ready["state"], "ready")
        self.assertIn("ssh_forward.py", ready["next"])


def cli_interpreter():
    binary = shutil.which("colab")
    if not binary:
        return None
    try:
        with open(binary, "rb") as stream:
            header = stream.readline(4096).decode("utf-8", "replace").strip()
    except OSError:
        return None
    parts = shlex.split(header[2:]) if header.startswith("#!") else []
    interpreter = parts[0] if len(parts) == 1 else None
    if not interpreter or not os.access(interpreter, os.X_OK):
        return None
    probe = subprocess.run(
        [
            interpreter,
            "-c",
            "import importlib.metadata as m; print(m.version('google-colab-cli'))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return interpreter if probe.stdout.strip() == "0.7.4" else None


HARNESS = r"""
import contextlib, io, json, queue, sys, tempfile, threading, time, types, uuid
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import account_cli, provider_errors as pe
import requests
from colab_cli import client, common, contents
results = {}

class Response:
    def __init__(self, status, body=""):
        self.status_code, self.text, self.reason = status, body, "fixture"
        self.ok = status < 400
        self.request = types.SimpleNamespace(headers={})
        self.headers = {}

class Session:
    def __init__(self, *responses):
        self.responses, self.timeouts = list(responses), []
    def request(self, method, url, headers=None, params=None, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

grant = Response(200, json.dumps({"acc": "g4", "nbh": "x", "token": "xsrf", "variant": "GPU"}))
for name, post in (
    ("capacity", Response(503)),
    ("refused", Response(412)),
    ("connect_timeout", requests.exceptions.ConnectTimeout("fixture")),
    ("read_timeout", requests.exceptions.ReadTimeout("fixture")),
):
    trace = account_cli.Trace(account_cli.command_details(
        ["--auth=oauth2", "new", "-s", "s1", "--gpu", "G4"]))
    session = Session(grant, post)
    with account_cli.hardened(trace, time.monotonic() + 60) as active:
        assert active
        try:
            client.Client(client.Prod(), session).assign(
                uuid.uuid4(), variant=client.Variant.GPU, accelerator=client.Accelerator.G4)
        except Exception as error:
            trace["exception_type"] = type(error).__name__
    results[name] = dict(pe.classify(dict(trace, returncode=1)),
                         timeouts=[list(t) for t in session.timeouts])

trace = account_cli.Trace(account_cli.command_details(["--auth=oauth2", "sessions"]))
with account_cli.hardened(trace, None), mock.patch.object(sys, "stdin", io.StringIO("")):
    from colab_cli import auth
    try:
        auth._run_remote_flow({})
        results["auth"] = "no error"
    except account_cli.AuthRequired:
        results["auth"] = pe.classify(dict(trace, returncode=1))

trace = account_cli.Trace(account_cli.command_details(["--auth=oauth2", "upload", "-s", "s1"]))
with account_cli.hardened(trace, None), mock.patch.object(
        requests, "request", return_value=Response(500)) as sent:
    holder = types.SimpleNamespace(url="https://runtime.invalid", token="PRIVATE-TOKEN")
    source = Path(tempfile.mkdtemp()) / "fixture.bin"
    source.write_bytes(b"fixture")
    try:
        contents.ContentsClient(holder).upload(str(source), "content/x.py")
    except Exception as error:
        results["contents_message"] = str(error)
    results["contents"] = pe.classify(dict(trace, returncode=1))
    results["contents_timeout"] = list(sent.call_args.kwargs["timeout"])

class Channel:
    def __init__(self):
        self.items = queue.Queue()
    def get_msg(self, timeout=None):
        return self.items.get(timeout=timeout)

def message(kind, content, parent="m1"):
    return {"header": {"msg_type": kind}, "parent_header": {"msg_id": parent}, "content": content}

class Socket:
    def __init__(self, behaviour, ready=True):
        self.connection_ready = threading.Event()
        if ready:
            self.connection_ready.set()
        self.iopub_channel, self.shell_channel = Channel(), Channel()
        self.behaviour, self.sent = behaviour, []
    def execute(self, code, **kwargs):
        self.sent.append(code)
        self.behaviour(self)
        return "m1"

class Manager:
    def __init__(self, socket, states):
        self.client, self.states = socket, list(states)
    def refresh_model(self, timeout=10):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if state == "fails":
            raise requests.exceptions.ReadTimeout("fixture")
        return {"execution_state": state}

def run_exec(behaviour, states=("idle",), ready=True, session_found=True, seconds=20):
    socket = Socket(behaviour, ready)
    kernel = types.SimpleNamespace(_manager=Manager(socket, states))
    class Runtime:
        def __init__(self, *args, **kwargs):
            pass
        kernel_client = property(lambda self: kernel)
        def stop(self):
            pass
    fake = types.SimpleNamespace(name="s1", url="u", token="t", kernel_id="k", session_id="x", running=None)
    store = types.SimpleNamespace(add=lambda value: None)
    trace = account_cli.Trace(account_cli.command_details(["--auth=oauth2", "exec", "-s", "s1"]))
    code = Path(tempfile.mkdtemp()) / "program.py"
    code.write_text("print('fixture')\n")
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            common.State, "get_session", lambda self, name, ignore_missing_session=False: fake if session_found else None))
        stack.enter_context(account_cli.hardened(trace, None))
        stack.enter_context(mock.patch("colab_cli.runtime.ColabRuntime", Runtime))
        stack.enter_context(mock.patch("colab_cli.common.setup_logging", lambda flag: None))
        stack.enter_context(mock.patch.object(common.state, "_store", store))
        stack.enter_context(mock.patch.object(common.state, "_history", types.SimpleNamespace(log_event=lambda *a: None)))
        stack.enter_context(mock.patch.object(account_cli, "BUSY_WAIT_SECONDS", 0.5))
        stack.enter_context(contextlib.redirect_stdout(output))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        try:
            returncode = account_cli.launcher_exec(trace, time.monotonic() + seconds, "s1", str(code), 60)
        except account_cli.Failed:
            returncode = 1
    return dict(pe.classify(dict(trace, returncode=returncode)), rc=returncode,
                stdout=output.getvalue(), sent=len(socket.sent),
                prelude=bool(socket.sent and socket.sent[0].startswith(account_cli.PRELUDE)))

def ok(socket):
    socket.iopub_channel.items.put(message("stream", {"name": "stdout", "text": "LAUNCHER_RESULT={}\n"}))
    socket.iopub_channel.items.put(message("stream", {"name": "stdout", "text": "ignored"}, parent="other"))
    socket.iopub_channel.items.put(message("status", {"execution_state": "idle"}))
    socket.shell_channel.items.put(message("execute_reply", {"status": "ok"}))

def raises(socket):
    socket.iopub_channel.items.put(message("error", {"ename": "ValueError", "evalue": "x", "traceback": ["tb"]}))
    socket.iopub_channel.items.put(message("status", {"execution_state": "idle"}))

results["exec_ok"] = run_exec(ok, states=("busy", "idle"))
results["exec_busy"] = run_exec(ok, states=("busy",))
# Observed busy, then the state query fails: still busy, never dispatched.
results["exec_busy_then_unknown"] = run_exec(ok, states=("busy", "fails"))
# Never observed (query fails): the connected socket may still dispatch.
results["exec_unknown"] = run_exec(ok, states=("fails",))
results["exec_timeout"] = run_exec(lambda socket: None, seconds=3.0)
results["exec_error"] = run_exec(raises)
results["exec_not_ready"] = run_exec(ok, ready=False, seconds=1.5)
# Too little time left after the state check: provably not sent.
results["exec_no_time"] = run_exec(ok, seconds=1.0)
results["exec_missing"] = run_exec(ok, session_found=False)
print(json.dumps(results))
"""


@unittest.skipUnless(
    cli_interpreter(), "official google-colab-cli==0.7.4 not installed"
)
class ShimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        interpreter = cli_interpreter()
        with tempfile.TemporaryDirectory() as home:
            environment = dict(os.environ, HOME=home)
            environment.pop("PYTHONPATH", None)
            completed_run = subprocess.run(
                [interpreter, "-I", "-c", HARNESS, str(SCRIPTS)],
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
                cwd=home,
                check=False,
            )
        if completed_run.returncode != 0:
            raise AssertionError(completed_run.stderr[-3000:])
        cls.results = json.loads(completed_run.stdout.strip().splitlines()[-1])

    def test_real_client_allocation_failures(self):
        capacity = self.results["capacity"]
        self.assertEqual(
            (capacity["code"], capacity["outcome"]),
            ("capacity_unavailable", "not_created"),
        )
        self.assertEqual(capacity["http_status"], 503)
        self.assertTrue(all(timeout for timeout in capacity["timeouts"]))
        self.assertEqual(self.results["refused"]["code"], "allocation_refused")
        connect = self.results["connect_timeout"]
        self.assertEqual(
            (connect["code"], connect["outcome"]), ("network_error", "not_created")
        )
        read = self.results["read_timeout"]
        self.assertEqual((read["code"], read["outcome"]), ("http_timeout", "unknown"))

    def test_noninteractive_login_never_prompts(self):
        self.assertIsInstance(self.results["auth"], dict)
        self.assertEqual(self.results["auth"]["code"], "auth_required")

    def test_contents_errors_are_bounded_and_token_free(self):
        self.assertEqual(self.results["contents"]["code"], "provider_server_error")
        self.assertNotIn("PRIVATE-TOKEN", self.results["contents_message"])
        self.assertNotIn("runtime.invalid", self.results["contents_message"])
        self.assertEqual(len(self.results["contents_timeout"]), 2)

    def test_vendored_exec_success_waits_for_idle_and_streams_own_output(self):
        result = self.results["exec_ok"]
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["stdout"], "LAUNCHER_RESULT={}\n")
        self.assertTrue(result["prelude"])
        self.assertEqual(result["sent"], 1)

    def test_vendored_exec_failure_boundaries(self):
        for name in ("exec_busy", "exec_busy_then_unknown"):
            busy = self.results[name]
            self.assertEqual(
                (busy["code"], busy["outcome"], busy["sent"], busy["kernel_state"]),
                ("kernel_busy", "not_dispatched", 0, "busy"),
            )
        self.assertEqual(self.results["exec_unknown"]["rc"], 0)
        timeout = self.results["exec_timeout"]
        self.assertEqual(
            (timeout["code"], timeout["outcome"], timeout["sent"]),
            ("execution_timeout", "unknown", 1),
        )
        error = self.results["exec_error"]
        self.assertEqual(
            (error["code"], error["remote_exception"]),
            ("remote_exception", "ValueError"),
        )
        ready = self.results["exec_not_ready"]
        self.assertEqual(
            (ready["code"], ready["outcome"], ready["sent"]),
            ("kernel_connect_failed", "not_dispatched", 0),
        )
        self.assertEqual(self.results["exec_missing"]["code"], "session_not_found")
        late = self.results["exec_no_time"]
        self.assertEqual(
            (late["code"], late["outcome"], late["sent"]),
            ("predispatch_timeout", "not_dispatched", 0),
        )


class ShimArgumentTests(unittest.TestCase):
    def test_argument_shapes(self):
        import account_cli

        self.assertEqual(
            account_cli.launcher_exec_arguments(
                [
                    "--auth=oauth2",
                    "exec",
                    "-s",
                    "s1",
                    "-f",
                    "/tmp/p.py",
                    "--timeout",
                    "60",
                ]
            ),
            ("s1", "/tmp/p.py", 60.0),
        )
        self.assertIsNone(
            account_cli.launcher_exec_arguments(["--auth=oauth2", "exec", "-s", "s1"])
        )
        details = account_cli.command_details(
            ["--auth=oauth2", "new", "-s", "a", "--gpu", "g4"]
        )
        self.assertEqual(
            details,
            {
                "command": "new",
                "session": "a",
                "hardware": "G4",
                "assign_posted": False,
            },
        )
        self.assertEqual(
            account_cli.endpoint_kind("https://x/tun/m/assign?nbh=1"), "assign"
        )
        self.assertEqual(
            account_cli.endpoint_kind("https://x/tun/m/unassign/abc"), "unassign"
        )
        self.assertIsNone(account_cli.endpoint_kind("https://x/other"))

    def test_named_account_command_carries_deadline(self):
        import accounts

        with tempfile.TemporaryDirectory() as root:
            store = accounts.AccountStore(Path(root))
            store.update(add="account-a")
            binary = Path(root) / "colab"
            binary.write_text("#!" + sys.executable + "\n")
            binary.chmod(0o700)
            with mock.patch.object(accounts, "AccountStore", return_value=store):
                argv = accounts.colab_command(
                    ["sessions"],
                    account="account-a",
                    executable=str(binary),
                    deadline=42,
                )
                plain = accounts.colab_command(
                    ["sessions"], account="default", deadline=42
                )
        self.assertEqual(
            argv[3:], ["--launcher-deadline=42.000", "--auth=oauth2", "sessions"]
        )
        self.assertEqual(plain, ["colab", "--auth=oauth2", "sessions"])


if __name__ == "__main__":
    unittest.main()
