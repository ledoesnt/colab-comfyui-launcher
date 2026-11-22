"""Fault injection: the remote side accepted the request, then the reply was lost.

1. Allocation: Colab created the runtime but the ``new`` response never
   arrived. The same request is found again through the budget job's
   allocation key; no second creation request is sent, and a lookup that
   cannot confirm anything keeps the record pending instead of failing it.
2. Render: the runtime accepted a render, then the local connection dropped.
   The same request ID only queries; the original output comes back and the
   server ran the request once. A retry is allowed only when "not sent" is
   proven.

Fixtures only: no allocation, network or real credentials.
"""

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import accounts
import budget_guard as budget
import colab_provider as provider
import colabctl as bridge
import provider_errors as pe
import test_render_contract as render_contract
import test_render_workflow as render_fixtures
import test_request_protocol as protocol
from client_requests import ClientRequests
from test_budget_guard import Clock, Provider
from test_provider_diagnostics import cli_interpreter
from test_render_workflow import DATA, renderer


def lost_reply():
    """What the shim reports when POST /assign was sent and no reply arrived."""
    diagnostic = pe.classify(
        {
            "command": "new",
            "session": "session-test",
            "hardware": "T4",
            "phase": "assign_post",
            "endpoint": "assign",
            "method": "POST",
            "assign_posted": True,
            "network": "read_timeout",
            "returncode": 1,
        }
    )
    return provider.ProviderError(
        pe.describe(diagnostic),
        code=diagnostic["code"],
        outcome_unknown=True,
        returncode=1,
        diagnostic=diagnostic,
    )


class KeyedServer(Provider):
    """Colab assignments keyed by notebook hash; every POST reply is lost."""

    supports_allocation_lookup = True

    def __init__(self):
        super().__init__()
        self.by_key = {}
        self.lookups = []
        self.accepts = True

    def create(self, session, gpu=None, *, allocation_key=None):
        self.created.append((session, gpu, allocation_key))
        if self.accepts:
            self.accept_late(allocation_key, gpu)
        raise lost_reply()

    def accept_late(self, key, gpu="T4"):
        assignment = f"allocation-{len(self.by_key) + 1}"
        self.by_key[key] = assignment
        # The CLI never saw the reply, so the listing cannot name it yet.
        self.rows.append(
            {
                "name": "?",
                "assignment_id": assignment,
                "hardware": gpu or "CPU",
                "shape": "Standard",
                "variant": "GPU",
            }
        )

    def lookup_allocation(self, session, gpu, allocation_key):
        self.lookups.append((session, gpu, allocation_key))
        assignment = self.by_key.get(allocation_key)
        if assignment is None:
            return False
        for row in self.rows:
            if row["assignment_id"] == assignment:
                row["name"] = session
        return True


class AllocationReplyLostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.clock = Clock()
        self.server = KeyedServer()
        self.manager = budget.BudgetManager(
            Path(temporary.name) / "budgets",
            self.server,
            clock=lambda: self.clock.wall,
            uptime=lambda: self.clock.up,
            boot_identity=lambda: self.clock.boot,
        )
        self.manager.ensure_guard = mock.Mock()
        self.manager._guard_alive = mock.Mock(return_value=True)

    def new(self, request_id="allocation-request"):
        return self.manager.create(
            "session-test",
            "T4",
            limit_cu=10,
            rate_ceiling=4,
            reserve_cu=2,
            request_id=request_id,
        )

    def test_accepted_allocation_is_found_by_its_key_without_second_create(self):
        first = self.new()
        self.assertEqual(first["status"], "active")
        self.assertEqual(first["assignment_id"], "allocation-1")
        self.assertEqual(first["request_id"], "allocation-request")
        self.assertEqual(first["allocation_recovered_by"], "allocation_key_lookup")
        self.assertIn("No second creation was sent", first["reason"])
        self.assertEqual(first["create_requests"], 1)
        self.assertEqual(first["allocation_lookup"]["result"], "found")
        self.assertEqual(first["allocation_lookup"]["assignment_id"], "allocation-1")
        self.assertNotIn("allocation_key", first)
        # The creation request carried the key derived from this budget job.
        self.assertEqual(len(self.server.created), 1)
        key = self.server.created[0][2]
        self.assertEqual(key, budget.allocation_key(first["job_id"]))
        self.assertEqual(self.server.lookups, [("session-test", "T4", key)])
        # Same request_id: replay of the same job, never another creation.
        replay = self.new()
        self.assertEqual(
            (replay["job_id"], replay["assignment_id"], replay["already_managed"]),
            (first["job_id"], "allocation-1", True),
        )
        self.assertEqual(len(self.server.created), 1)
        polled = self.manager.status("session-test")
        self.assertEqual(
            (polled["request_id"], polled["job_id"]),
            ("allocation-request", first["job_id"]),
        )

    def test_unconfirmed_allocation_stays_pending_and_is_never_recreated(self):
        self.server.accepts = False
        first = self.new()
        self.assertEqual(first["status"], "allocation_unknown")
        self.assertFalse(first["ok"])
        self.assertFalse(first["terminal"])
        self.assertIn("found no runtime yet", first["reason"])
        self.assertIn("Pending confirmation", first["next"])
        self.assertIn("never repeat 'new'", first["next"])
        self.assertEqual(first["allocation_lookup"]["result"], "absent")
        # Neither replays nor an hour of guard ticks turn absence into failure.
        for _ in range(4):
            self.clock.advance(900)
            self.manager.tick("session-test", first["job_id"])
            replay = self.new()
            self.assertEqual(replay["status"], "allocation_unknown")
            self.assertEqual(replay["job_id"], first["job_id"])
        self.assertEqual(len(self.server.created), 1)
        self.assertGreaterEqual(len(self.server.lookups), 2)
        # A late provider-side acceptance is still bound to the same job.
        self.server.accept_late(self.server.created[0][2])
        self.clock.advance(30)
        late = self.manager.tick("session-test", first["job_id"], force=True)
        self.assertEqual(
            (late["status"], late["assignment_id"], late["job_id"]),
            ("active", "allocation-1", first["job_id"]),
        )
        self.assertEqual(len(self.server.created), 1)

    def test_unbound_request_lapses_only_after_any_runtime_must_be_gone(self):
        self.server.accepts = False
        first = self.new()
        self.clock.advance(budget.RUNTIME_LIFETIME_BOUND - 60)
        pending = self.manager.tick("session-test", force=True)
        self.assertNotIn(pending["status"], budget.TERMINAL)
        self.assertFalse(pending["terminal"])
        self.clock.advance(120)
        lapsed = self.manager.tick("session-test", force=True)
        self.assertEqual(
            (lapsed["status"], lapsed["terminal"], lapsed["job_id"]),
            ("allocation_lapsed", True, first["job_id"]),
        )
        self.assertIn("allocation_lapsed", budget.TERMINAL)
        self.assertEqual(self.manager.list_active(), [])
        self.assertEqual(len(self.server.created), 1)

    def test_old_allocation_absent_after_scope_change_is_released_by_lifetime(self):
        first = self.new()
        self.assertEqual(first["status"], "active")
        self.server.rows.clear()
        self.server.scope = "re-signed-in"
        recent = self.manager.tick("session-test", force=True)
        self.assertEqual(recent["status"], "release_unknown")
        self.clock.advance(budget.RUNTIME_LIFETIME_BOUND + 60)
        old = self.manager.tick("session-test", force=True)
        self.assertEqual(
            (old["status"], old["release_verified_by"], old["terminal"]),
            ("released", "runtime_lifetime", True),
        )

    def test_unexplained_orphan_is_not_bound_without_a_key_match(self):
        self.server.accepts = False
        self.server.rows.append(
            {"name": "?", "assignment_id": "foreign", "hardware": "T4"}
        )
        with mock.patch.object(
            self.server,
            "sessions",
            side_effect=lambda: [r for r in self.server.rows if self.server.created],
        ):
            result = self.new()
        self.assertEqual(result["status"], "allocation_unknown")
        self.assertIsNone(result["assignment_id"])
        self.assertIn("unnamed new assignment", result["reason"])

    def test_preflight_failures_say_nothing_was_submitted(self):
        diagnostic = pe.classify(
            {"command": "sessions", "endpoint": "assignments", "network": "connection"}
        )
        offline = provider.ProviderError(
            "offline", code="network_error", diagnostic=diagnostic
        )
        with (
            mock.patch.object(self.server, "sessions", side_effect=offline),
            self.assertRaises(budget.AllocationNotSubmitted) as raised,
        ):
            self.new()
        self.assertEqual(
            (
                raised.exception.result["status"],
                raised.exception.result["resubmit_allowed"],
            ),
            ("not_submitted", True),
        )
        self.assertEqual(raised.exception.diagnostic["code"], "network_error")
        self.assertEqual(self.server.created, [])
        # Nothing was recorded, so the same request ID allocates normally.
        scopes = iter(["before", "refreshed", "refreshed", "refreshed"])
        with mock.patch.object(
            self.server, "account_scope", side_effect=lambda: next(scopes, "refreshed")
        ):
            result = self.new()
        self.assertEqual(result["status"], "active")
        self.assertEqual(len(self.server.created), 1)
        # An existing session name is not retryable as-is.
        self.server.rows.append(
            {"name": "taken", "assignment_id": "x", "hardware": "T4"}
        )
        with self.assertRaises(budget.AllocationNotSubmitted) as raised:
            self.manager.create(
                "taken", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2
            )
        self.assertFalse(raised.exception.result["resubmit_allowed"])

    def test_output_keeps_validated_diagnostic_fields(self):
        diagnostic = lost_reply().diagnostic
        diagnostic["exception_type"] = "ReadTimeout"
        shown = bridge.safe_structure(
            {
                "allocation_diagnostic": dict(diagnostic),
                "provider_error": pe.public(dict(diagnostic)),
                "error": "requests ReadTimeout at https://example.invalid/x",
            }
        )
        self.assertEqual(
            shown["allocation_diagnostic"]["exception_type"], "ReadTimeout"
        )
        self.assertEqual(shown["provider_error"]["exception_type"], "ReadTimeout")
        self.assertNotIn("example.invalid", shown["error"])
        forged = bridge.safe_structure(
            dict(diagnostic, exception_type="token=PRIVATE https://x.invalid")
        )
        self.assertIsNone(forged["exception_type"])

    def test_guard_tick_during_successful_create_needs_no_lookup(self):
        # Live 2026-11-19: the guard listed while POST /assign was in flight,
        # marked the record unknown, and a needless lookup then claimed the
        # reply had been lost although the creation had succeeded.
        server = Provider()
        server.supports_allocation_lookup = True
        server.lookup_allocation = mock.Mock(side_effect=AssertionError("no lookup"))
        manager = self.manager
        manager.provider = server
        original = server.create

        def create(session, gpu=None, *, allocation_key=None):
            record = manager._read(manager._path(session))
            observed = manager._observe(session, record["job_id"], [], server.scope)
            self.assertEqual(observed["status"], "allocation_pending")
            original(session, gpu)

        server.create = create
        result = self.new()
        self.assertEqual(result["status"], "active")
        self.assertNotIn("allocation_recovered_by", result)
        server.lookup_allocation.assert_not_called()

    def test_lookup_never_overlaps_a_creation_that_may_be_in_flight(self):
        record = {
            "status": "allocation_unknown",
            "assignment_id": None,
            "allocation_rejected": False,
            "allocation_lookup": {"supported": True},
            "allocation_key": "0" * 32,
            "submitted_at": self.clock.wall,
        }
        self.assertFalse(self.manager._lookup_due(record, True))
        self.clock.advance(budget.CREATE_LOOKUP_AFTER)
        self.assertTrue(self.manager._lookup_due(record, True))
        refused = dict(record, allocation_rejected=True)
        self.assertFalse(self.manager._lookup_due(refused, True))

    def test_provider_lookup_is_query_only_and_named_accounts_only(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            observed = {
                "command": "new",
                "session": "session-test",
                "lookup_only": True,
                "failure": "allocation_lookup_empty",
                "assign_posted": False,
            }
            return subprocess.CompletedProcess(
                argv, 1, "", pe.encode(pe.classify(observed))
            )

        with mock.patch.object(provider, "current_account", return_value="account-a"):
            api = provider.ColabProvider(run, oauth_cache=Path("/nonexistent"))
        with mock.patch.object(
            provider, "colab_command", side_effect=lambda a, **k: [a, k]
        ):
            found = api.lookup_allocation("session-test", "G4", "a" * 32)
        self.assertFalse(found)
        arguments, options = calls[0]
        self.assertEqual(arguments, ["new", "-s", "session-test", "--gpu", "G4"])
        self.assertEqual(options["allocation_key"], "a" * 32)
        self.assertTrue(options["lookup_only"])
        diagnostic = pe.classify(
            {
                "command": "new",
                "lookup_only": True,
                "failure": "allocation_lookup_empty",
            }
        )
        self.assertEqual(diagnostic["outcome"], "none")
        with self.assertRaises(accounts.AccountError):
            accounts.colab_command(["new"], account="default", allocation_key="a" * 32)


SHIM_HARNESS = r"""
import contextlib, io, json, os, sys, tempfile, types, uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs
sys.path.insert(0, sys.argv[1])
import account_cli, provider_errors as pe
import requests
from colab_cli import auto_update, client, common

auto_update.run_background_check = lambda *args, **kwargs: None
PROXY = {"token": "proxy-fixture", "tokenExpiresInSeconds": 3600, "url": "https://runtime.invalid"}

class Response:
    def __init__(self, body):
        self.status_code, self.text, self.reason, self.ok = 200, json.dumps(body), "OK", True
        self.request = types.SimpleNamespace(headers={})
        self.headers = {}

class Server:
    # /tun/m/assign keyed by nbh. The POST is accepted, then its reply is lost.
    def __init__(self):
        self.assignments, self.calls = {}, []
    def request(self, method, url, headers=None, params=None, **kwargs):
        nbh = parse_qs(urlparse(url).query)["nbh"][0]
        self.calls.append([method, nbh])
        if method == "GET":
            if nbh in self.assignments:
                return Response({"endpoint": self.assignments[nbh], "runtimeProxyInfo": PROXY})
            return Response({"acc": "G4", "nbh": nbh, "token": "xsrf", "variant": "GPU"})
        self.assignments[nbh] = "gpu-g4-s-fixture%d" % (len(self.assignments) + 1)
        raise requests.exceptions.ReadTimeout("fixture: reply lost after acceptance")

server = Server()
common.state._client = client.Client(client.Prod(), server)
directory = Path(tempfile.mkdtemp())

def invoke(*arguments):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = account_cli.main(["account_cli.py", str(directory), *arguments])
    diagnostic, _ = pe.parse(err.getvalue())
    return {"code": code, "stdout": out.getvalue().splitlines(), "diagnostic": diagnostic}

key, other = uuid.uuid4().hex, uuid.uuid4().hex
new = ["--auth=oauth2", "new", "-s", "s1", "--gpu", "G4"]
results = {
    "nbh": client.uuid_to_web_safe_base64(uuid.UUID(hex=key)),
    "create": invoke("--launcher-allocation=" + key, *new),
    "lookup": invoke("--launcher-allocation=" + key, "--launcher-lookup-only", *new),
    "absent": invoke("--launcher-allocation=" + other, "--launcher-lookup-only", *new),
    "calls": server.calls,
    "endpoint": server.assignments.get(client.uuid_to_web_safe_base64(uuid.UUID(hex=key))),
}
saved = directory / "sessions.json"
results["saved"] = saved.read_text() if saved.exists() else ""
for name, arguments in {
    "bad_key": ["--launcher-allocation=NOT-A-KEY", *new],
    "lookup_without_key": ["--launcher-lookup-only", *new],
    "key_on_exec": ["--launcher-allocation=" + key, "--auth=oauth2", "exec", "-s", "s1"],
}.items():
    results[name] = invoke(*arguments)
results["calls_after_rejections"] = len(server.calls)
print(json.dumps(results))
"""


@unittest.skipUnless(
    cli_interpreter(), "official google-colab-cli==0.7.4 not installed"
)
class AllocationKeyShimTests(unittest.TestCase):
    """The real official ``new`` command (0.7.4) under the shim's key hooks."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as home:
            environment = dict(os.environ, HOME=home)
            environment.pop("PYTHONPATH", None)
            run = subprocess.run(
                [cli_interpreter(), "-I", "-c", SHIM_HARNESS, str(SCRIPTS)],
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
                cwd=home,
                check=False,
            )
        if run.returncode != 0:
            raise AssertionError(run.stderr[-3000:])
        cls.results = json.loads(run.stdout.strip().splitlines()[-1])

    def test_create_sends_the_key_and_a_lost_reply_is_unknown(self):
        create = self.results["create"]
        self.assertEqual(create["code"], 1)
        self.assertEqual(
            (create["diagnostic"]["code"], create["diagnostic"]["outcome"]),
            ("http_timeout", "unknown"),
        )
        self.assertEqual(
            self.results["calls"][:2],
            [["GET", self.results["nbh"]], ["POST", self.results["nbh"]]],
        )

    def test_lookup_returns_the_same_assignment_and_never_posts(self):
        lookup = self.results["lookup"]
        self.assertEqual(lookup["code"], 0)
        self.assertEqual(lookup["stdout"][-1], "[colab] Session READY.")
        self.assertEqual(self.results["calls"][2], ["GET", self.results["nbh"]])
        self.assertIn(self.results["endpoint"], self.results["saved"])
        self.assertIn('"s1"', self.results["saved"])
        posts = [call for call in self.results["calls"] if call[0] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_absent_key_reports_pending_lookup_without_creating(self):
        absent = self.results["absent"]
        self.assertEqual(absent["code"], 1)
        self.assertEqual(
            (absent["diagnostic"]["code"], absent["diagnostic"]["outcome"]),
            ("allocation_lookup_empty", "none"),
        )
        self.assertEqual(self.results["calls"][3][0], "GET")
        self.assertEqual(len(self.results["calls"]), 4)

    def test_malformed_launcher_options_fail_before_any_request(self):
        for name in ("bad_key", "lookup_without_key", "key_on_exec"):
            self.assertEqual(self.results[name]["code"], 2, name)
            self.assertEqual(
                self.results[name]["diagnostic"]["code"], "cli_unavailable", name
            )
        self.assertEqual(self.results["calls_after_rejections"], 4)


def exec_failure(**observed):
    return subprocess.CompletedProcess(
        [], 1, "", pe.encode(pe.classify(dict(command="exec", **observed)))
    )


class RenderReplyLostTests(unittest.TestCase):
    """Bridge + remote request journal; the provider "kernel" runs the program."""

    setUp_protocol = protocol.RequestProtocolTests.setUp
    cli = protocol.RequestProtocolTests.cli

    def setUp(self):
        self.setUp_protocol()
        remote = tempfile.TemporaryDirectory(prefix="launcher-remote-")
        self.addCleanup(remote.cleanup)
        self.remote = Path(remote.name)
        self.namespace = {}
        exec(  # noqa: S102 - trusted local remote-program fixture.
            bridge.REMOTE_HELPERS.replace(
                "/content/colab-comfyui-runtime", remote.name
            ),
            self.namespace,
        )
        self.renders = []
        self.namespace["run_background"] = self.start_render
        self.programs = []
        self.replies = []

    def start_render(self, payload):
        # The accepted render finishes in the background (ComfyUI accepted it).
        self.renders.append(payload["request_id"])
        jobs = self.remote / "render-jobs"
        jobs.mkdir(exist_ok=True)
        (jobs / (payload["request_id"] + ".json")).write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "succeeded",
                    "terminal": True,
                    "request_id": payload["request_id"],
                    "assignment_id": payload["assignment_id"],
                    "prompt_id": "prompt-original",
                    "assets": [{"filename": "original.mp4", "sha256": "f" * 64}],
                }
            )
        )
        return {"ok": True, "status": "started", "task": "render"}

    def kernel(self, arguments, timeout):
        program = Path(arguments[4]).read_text()
        literal = re.search(r"^payload = json\.loads\((.*)\)$", program, re.MULTILINE)
        payload = json.loads(ast.literal_eval(literal.group(1)))
        self.programs.append(payload["action"])
        reply = self.replies.pop(0)
        if reply == "not_sent":
            return exec_failure(failure="kernel_busy", dispatched=False)
        result = self.namespace["dispatch_request"](payload)
        if reply == "connection_lost":
            return exec_failure(failure="connection_lost", dispatched=True)
        if reply == "killed":
            # The CLI died after sending; it never wrote a diagnostic.
            return subprocess.CompletedProcess([], -9, "", "")
        return subprocess.CompletedProcess(
            [], 0, bridge.RESULT_PREFIX + json.dumps(result) + "\n", ""
        )

    def run_cli(self, *arguments):
        with mock.patch.object(bridge, "_run_colab", side_effect=self.kernel):
            return self.cli(*arguments)

    def recover(self, first_reply):
        self.replies = [first_reply, "ok"]
        code, first, _ = self.run_cli("--request-id", "render-1", "render")
        self.assertEqual(code, 1)
        self.assertEqual(first["status"], "submission_unknown")
        self.assertFalse(first["terminal"])
        self.assertFalse(first["resubmit_allowed"])
        self.assertTrue(first["job_may_still_be_running"])
        # The runtime accepted the render exactly once despite the lost reply.
        self.assertEqual(self.renders, ["render-1"])
        self.assertEqual(self.programs, ["render"])
        # A restarted client re-issues the same command: query only.
        code, recovered, _ = self.run_cli("--request-id", "render-1", "render")
        self.assertEqual(code, 0)
        self.assertEqual(self.programs, ["render", "job"])
        self.assertEqual(self.renders, ["render-1"])
        self.assertEqual(
            (recovered["status"], recovered["prompt_id"], recovered["request_id"]),
            ("succeeded", "prompt-original", "render-1"),
        )
        self.assertEqual(recovered["assets"][0]["filename"], "original.mp4")
        # Settled: later queries are served from the receipt, sending nothing.
        code, cached, _ = self.run_cli("job", "render-1")
        self.assertEqual((code, cached["source"]), (0, "local_receipt"))
        self.assertEqual(len(self.programs), 2)
        return first

    def test_connection_lost_after_dispatch_only_queries_the_original(self):
        first = self.recover("connection_lost")
        self.assertTrue(first["this_call_dispatched"])
        self.assertEqual(first["provider_error"]["code"], "connection_lost")

    def test_refused_collect_admission_is_not_sent_and_retryable(self):
        self.recover("connection_lost")
        # Live: collect hit the guard's in-flight reconcile and said "failed".
        self.manager.authorize.side_effect = budget.BudgetError(
            "Allocation reconciliation is still in progress."
        )
        with tempfile.TemporaryDirectory() as destination:
            code, refused, _ = self.run_cli(
                "collect", "--job", "render-1", "--destination", destination
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            (refused["status"], refused["resubmit_allowed"], refused["terminal"]),
            ("not_submitted", True, True),
        )
        self.assertFalse(refused["this_call_dispatched"])
        self.assertEqual(self.programs, ["render", "job"])

    def test_killed_cli_without_diagnostic_is_never_called_not_sent(self):
        first = self.recover("killed")
        self.assertIsNone(first["this_call_dispatched"])
        self.assertEqual(first["provider_error"]["outcome"], "unknown")

    def test_retry_is_offered_only_when_not_sent_is_proven(self):
        self.replies = ["not_sent"]
        with mock.patch.object(bridge, "EXEC_ATTEMPTS", 1):
            code, first, _ = self.run_cli("--request-id", "render-2", "render")
        self.assertEqual(code, 1)
        self.assertEqual(
            (first["status"], first["resubmit_allowed"], first["this_call_dispatched"]),
            ("not_submitted", True, False),
        )
        self.assertEqual(self.renders, [])
        # The same ID stays a record of that attempt; a new ID may submit.
        code, again, _ = self.run_cli("--request-id", "render-2", "render")
        self.assertEqual((code, again["source"]), (1, "local_receipt"))
        self.assertEqual(self.programs, ["render"])
        self.replies = ["ok"]
        code, _, _ = self.run_cli("--request-id", "render-3", "render")
        self.assertEqual(code, 0)
        self.assertEqual(self.renders, ["render-3"])
        self.assertEqual(
            ClientRequests("fixture").read("render-2")["status"], "not_submitted"
        )


class ServerExecutionEvidenceTests(unittest.TestCase):
    """The render query counts ComfyUI jobs carrying the request ID."""

    setUp = render_fixtures.RenderTests.setUp
    http = render_fixtures.RenderTests.http
    execute = render_contract.RequestRecoveryTests.execute

    def query(self):
        with (
            mock.patch.object(
                renderer,
                "verify_api_bytes",
                return_value=(hashlib.sha256(DATA).hexdigest(), len(DATA)),
            ),
            mock.patch.object(
                renderer, "media_metadata", return_value={"video": [], "audio": []}
            ),
        ):
            return renderer.query_job("durable-request", self.content / "jobs", 3)

    def test_reply_lost_after_acceptance_shows_one_server_execution(self):
        posted = {}
        retained = {}
        available = [False]

        def http(path, deadline, payload=None):
            if path == "/prompt":
                posted.update(payload)
                retained[payload["prompt_id"]] = copy.deepcopy(self.history)
                retained[payload["prompt_id"]]["prompt"] = [
                    0,
                    payload["prompt_id"],
                    payload["prompt"],
                    payload["extra_data"],
                    ["1"],
                ]
                raise renderer.RenderError("Local connection to ComfyUI dropped")
            if path.startswith("/history") and not available[0]:
                return {}  # Not yet observable: the runner must not resubmit.
            if path == "/history":
                return copy.deepcopy(retained)
            if path.startswith("/history/"):
                prompt_id = path.rsplit("/", 1)[1]
                return {prompt_id: retained[prompt_id]} if prompt_id in retained else {}
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http) as calls:
            with self.assertRaises(renderer.RenderError):
                self.execute()
            available[0] = True
            recovered = self.query()
            settled = self.query()
            evidence = recovered["execution_evidence"]
            self.assertEqual(recovered["status"], "succeeded")
            self.assertEqual(recovered["result"]["prompt_id"], posted["prompt_id"])
            self.assertEqual(
                (evidence["comfyui_jobs"], evidence["single_execution"]), (1, True)
            )
            self.assertEqual(evidence["prompt_ids"], [posted["prompt_id"]])
            self.assertEqual(settled["execution_evidence"]["comfyui_jobs"], 1)
            # A duplicate on the server would be reported, never hidden.
            duplicate = copy.deepcopy(retained[posted["prompt_id"]])
            duplicate["prompt"][1] = "second-run"
            retained["second-run"] = duplicate
            doubled = self.query()["execution_evidence"]
            self.assertEqual(
                (doubled["comfyui_jobs"], doubled["single_execution"]), (2, False)
            )
        self.assertEqual(
            sum(call.args[0] == "/prompt" for call in calls.call_args_list), 1
        )


if __name__ == "__main__":
    unittest.main()
