"""Login preflight through a local CLI and real PTY; no Google/VM requests."""

import importlib.util
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dashboard = sys.modules.get("dashboard") or load_module(
    "dashboard", ROOT / "scripts/dashboard.py"
)
wizard = load_module("auth_preflight_wizard", ROOT / "scripts/wizard.py")
provider = load_module("auth_preflight_provider", ROOT / "scripts/provider_terminal.py")

AUTHENTICATED = {"state": "authenticated", "message": "Fixture login verified."}
SIGNED_OUT = {"state": "not_authenticated", "message": "Fixture login required."}
UNKNOWN = {"state": "unavailable", "message": "Fixture network unavailable."}

# Reproduce the official OAuth prompt while requiring a controlling PTY for
# the interactive response. Files contain only fixture state/command metadata.
FAKE_CLI = r"""
import os
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
assert sys.argv[2:] == ["--auth=oauth2", "sessions"], "Only read-only sessions is allowed"
with (root / "commands").open("a") as stream:
    stream.write("sessions tty=" + str(sys.stdin.isatty()) + "\n")
mode = (root / "mode").read_text()
if mode == "delayed_verified":
    (root / "entered").touch()
    time.sleep(0.5)
if mode == "network":
    print("ConnectionError: fixture network unavailable", file=sys.stderr)
    raise SystemExit(2)
if (root / "verified").exists():
    print("[colab] No active sessions found on server.")
    raise SystemExit(0)
print("To authorize colab-cli, visit this URL in any browser:", file=sys.stderr, flush=True)
print("  https://accounts.google.com/o/oauth2/auth?state=fixture-only", file=sys.stderr, flush=True)
print("After approving, Google will display an authorization code.", file=sys.stderr, flush=True)
print("Enter the authorization code: ", end="", flush=True)
if sys.stdin.isatty():
    with open("/dev/tty") as terminal:
        assert os.isatty(terminal.fileno())
        value = terminal.readline().strip()
else:
    try:
        value = input()
    except EOFError:
        raise SystemExit(3)
if value != "fixture-accepted":
    raise SystemExit(4)
if mode != "zero_without_verification":
    (root / "verified").touch()
print("[colab] No active sessions found on server.")
"""


class LocalCLIBackend(dashboard.Backend):
    def __init__(self, directory, timeout=3):
        self.directory = directory
        self.timeout = timeout
        self.terminals = []
        self.create = mock.Mock(side_effect=AssertionError("Must not allocate a VM"))
        self.release = mock.Mock(side_effect=AssertionError("Must not release a VM"))
        self.bridge = mock.Mock(side_effect=AssertionError("Must not mount or deploy"))
        self.ssh = mock.Mock(side_effect=AssertionError("Must not change SSH"))
        super().__init__(self.run_fixture)

    def command(self):
        return [
            sys.executable,
            "-u",
            str(self.directory / "cli.py"),
            str(self.directory),
            "--auth=oauth2",
            "sessions",
        ]

    def run_fixture(self, argv, **kwargs):
        if argv != ["colab", "--auth=oauth2", "sessions"]:
            raise AssertionError("Only official read-only sessions is permitted")
        return subprocess.run(self.command(), check=False, **kwargs)

    def account_terminal(self, _config, on_event):
        terminal = provider.ProviderTerminal(self.command(), on_event, self.timeout)
        self.terminals.append(terminal)
        return terminal


class AccountStatusTests(unittest.TestCase):
    def check(self, stdout="", stderr="", returncode=0):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["fixture"], returncode, stdout, stderr
            )
        )
        result = dashboard.Backend(run).account_status()
        run.assert_called_once_with(
            ["colab", "--auth=oauth2", "sessions"],
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        return result

    def test_empty_and_unnamed_runtime_responses_verify_login_without_exposing_rows(
        self,
    ):
        for output in (
            "[colab] No active sessions found on server.\n",
            "[?] fixture-endpoint | Hardware: G4 | Shape: Standard\n",
            "\x1b[32m[selected] fixture-endpoint | Hardware: CPU\x1b[0m\n",
        ):
            with self.subTest(output=output):
                result = self.check(output)
                self.assertEqual(result["state"], "authenticated")
                self.assertNotIn("fixture-endpoint", str(result))

    def test_only_recognized_provider_login_prompt_means_signed_out(self):
        result = self.check(
            "Enter the authorization code: ",
            "To authorize colab-cli, visit this URL in any browser:\n"
            "https://accounts.google.com/o/oauth2/auth?state=fixture-private\n"
            "EOFError: EOF when reading a line",
            1,
        )
        self.assertEqual(result["state"], "not_authenticated")
        self.assertNotIn("fixture-private", str(result))
        self.assertNotIn("https://", str(result))

    def test_network_unknown_output_and_generic_eof_are_not_logout(self):
        for output, error, returncode in (
            ("", "DNS lookup failed", 1),
            ("", "EOFError: EOF when reading a line", 1),
            ("[colab] unexpected fixture response", "", 0),
        ):
            with self.subTest(error=error):
                result = self.check(output, error, returncode)
                self.assertEqual(result["state"], "unavailable")
                if error:
                    self.assertNotIn(error, str(result))

    def test_timeout_and_missing_executable_return_safe_unknown_state(self):
        for failure in (
            subprocess.TimeoutExpired(["fixture"], 30, output="fixture-private"),
            FileNotFoundError("fixture-private"),
        ):
            with self.subTest(failure=type(failure).__name__):
                result = dashboard.Backend(
                    mock.Mock(side_effect=failure)
                ).account_status()
                self.assertEqual(result["state"], "unavailable")
                self.assertNotIn("fixture-private", str(result))

    def test_cancel_before_login_worker_activation_never_starts_provider_request(self):
        backend = mock.Mock(spec=dashboard.Backend)
        with mock.patch.object(dashboard.threading.Thread, "start"):
            worker = dashboard.Worker(backend)
        config = dashboard.Config(session="retain-fixture-runtime")
        self.assertTrue(worker.submit("account_login", config))
        self.assertTrue(worker.cancel_account())
        worker.thread.start()
        try:
            events = []
            for _ in range(5):
                event = worker.events.get(timeout=1)
                events.append(event)
                if event == ("done", "account_login"):
                    break
            self.assertIn(("done", "account_login"), events)
            self.assertFalse(worker.account_checked)
            backend.account_status.assert_not_called()
            backend.account_terminal.assert_not_called()
            backend.create.assert_not_called()
            backend.release.assert_not_called()
        finally:
            worker.close()
            worker.thread.join(1)
            self.assertFalse(worker.thread.is_alive())


class AccountPTYTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="launcher-auth-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "cli.py").write_text(FAKE_CLI)
        (self.directory / "mode").write_text("normal")
        self.backend = LocalCLIBackend(self.directory)
        self.worker = dashboard.Worker(self.backend)
        self.config = dashboard.Config(session="retain-fixture-runtime")
        self.seen = []
        self.addCleanup(self.close_worker)

    def close_worker(self):
        self.worker.close()
        self.worker.thread.join(2)
        self.assertFalse(self.worker.thread.is_alive())
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()

    def until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.worker.events.get(timeout=0.05)
            except queue.Empty:
                continue
            self.seen.append(event)
            if predicate(*event):
                return event
        self.fail("Local authentication fixture did not reach the expected event")

    def start_login(self):
        self.assertTrue(self.worker.submit("account_login", self.config))
        self.until(lambda kind, value: kind == "auth" and value.get("needs_code"))
        self.assertFalse(self.worker.account_checked)
        self.assertTrue(self.worker.busy)

    def test_read_only_check_does_not_answer_provider_prompt_or_open_a_pty(self):
        self.assertEqual(self.backend.account_status()["state"], "not_authenticated")
        self.assertFalse((self.directory / "verified").exists())
        self.assertEqual(
            (self.directory / "commands").read_text(), "sessions tty=False\n"
        )
        self.assertEqual(self.backend.terminals, [])

    def test_real_controlling_pty_waits_for_explicit_code_and_rechecks_login(self):
        self.start_login()
        self.assertFalse((self.directory / "verified").exists())
        self.assertTrue(self.worker.send_auth("fixture-accepted"))
        self.until(lambda kind, value: kind == "done" and value == "account_login")
        self.assertTrue(self.worker.account_checked)
        self.assertEqual(
            [value["state"] for kind, value in self.seen if kind == "account"],
            ["not_authenticated", "authenticated"],
        )
        self.assertEqual(
            (self.directory / "commands").read_text(),
            "sessions tty=False\nsessions tty=True\nsessions tty=False\n",
        )
        self.assertNotIn("fixture-accepted", str(self.seen))
        self.assertIsNone(self.worker.auth_terminal)
        self.assertFalse(self.worker.send_auth("fixture-accepted"))

    def test_existing_verified_login_skips_interactive_authorization(self):
        (self.directory / "verified").touch()
        self.assertTrue(self.worker.submit("account_login", self.config))
        self.until(lambda kind, value: kind == "done")
        self.assertTrue(self.worker.account_checked)
        self.assertEqual(self.backend.terminals, [])

    def test_zero_cli_exit_without_verified_followup_is_not_logged_in(self):
        (self.directory / "mode").write_text("zero_without_verification")
        self.start_login()
        self.assertTrue(self.worker.send_auth("fixture-accepted"))
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))

    def test_timeout_clears_auth_and_cannot_mark_login_ready(self):
        self.backend.timeout = 0.3
        self.start_login()
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertIn(("auth_clear", None), self.seen)
        self.assertFalse((self.directory / "verified").exists())

    def test_cancel_preserves_runtime_and_rejects_late_auth_callbacks(self):
        self.start_login()
        self.assertTrue(self.worker.cancel_account())
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertIsNone(self.worker.auth_terminal)
        self.assertEqual(self.config.session, "retain-fixture-runtime")
        terminal = self.backend.terminals[0]
        terminal.on_event({"kind": "auth_url", "url": "https://fixture.invalid/late"})
        self.assertTrue(self.worker.events.empty())

    def test_unknown_network_state_does_not_force_new_authorization(self):
        (self.directory / "mode").write_text("network")
        self.assertTrue(self.worker.submit("account_login", self.config))
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertEqual(self.backend.terminals, [])
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))

    def test_unverified_new_action_is_blocked_without_authorizing_or_allocating(self):
        # An earlier UI verification is insufficient proof immediately before
        # allocating a new runtime: the Worker must recheck the provider.
        self.worker.account_checked = True
        self.assertTrue(self.worker.submit("wizard_create", self.config))
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertEqual(self.backend.terminals, [])
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))

    def test_real_worker_delayed_cli_recheck_back_cannot_leave_checking_when_idle(self):
        (self.directory / "verified").touch()
        ui = wizard.Wizard(dashboard.Config(), self.backend, self.worker)

        def pump_until(predicate, timeout=4):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ui._events()
                if predicate():
                    return
                time.sleep(0.005)
            self.fail("Real local Worker/CLI did not reach the expected UI state")

        pump_until(
            lambda: not self.worker.busy and ui.account.get("state") == "authenticated"
        )
        ui.config = replace(self.config)
        ui.status = {"http_ready": True}
        ui.ssh = {"running": True}
        (self.directory / "mode").write_text("delayed_verified")
        ui._activate(wizard.Choice("account", "Manage Colab account"))
        ui._activate(wizard.Choice("account_check", "Recheck login"))
        pump_until(lambda: (self.directory / "entered").exists())
        self.assertTrue(self.worker.busy)
        self.assertEqual(ui.account["state"], "checking")
        ui._key("\n")  # Waiting pages default to the enabled Back choice.
        self.assertEqual(ui.page, "home")
        self.assertEqual(ui.account["state"], "unknown")
        self.assertIn("cancelled", ui.account["message"])
        pump_until(lambda: not self.worker.busy)
        ui._events()  # Drain actual late cancellation/done events, no fake done.
        self.assertNotEqual(ui.account["state"], "checking")
        self.assertEqual(ui.page, "home")
        self.assertEqual(ui.config.session, "retain-fixture-runtime")
        self.assertEqual(ui.status, {"http_ready": True})
        self.assertEqual(ui.ssh, {"running": True})
        self.assertEqual(self.backend.terminals, [])
        self.assertEqual(
            (self.directory / "commands").read_text(),
            "sessions tty=False\nsessions tty=False\n",
        )


class LocalWorker:
    def __init__(self):
        self.events = queue.Queue()
        self.busy = False
        self.submitted = []
        self.cancelled = False
        self.responses = []

    def submit(self, action, config):
        if self.busy:
            return False
        self.busy = True
        self.submitted.append((action, replace(config)))
        return True

    def cancel_account(self):
        self.cancelled = True
        self.busy = False
        return True

    def send_auth(self, value):
        self.responses.append(value)
        return True


class AccountNavigationTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)

    def choose(self, key):
        self.ui._activate(wizard.Choice(key, "Fixture choice"))

    def checked(self, state):
        self.worker.events.put(("account", state))
        self.worker.busy = False
        self.worker.events.put(("done", self.ui.last_operation))
        self.ui._events()

    def test_initial_page_checks_account_without_creating_or_querying_a_runtime(self):
        backend = mock.Mock(spec=dashboard.Backend)
        ui = wizard.Wizard(dashboard.Config(), backend, self.worker)
        self.assertEqual(ui.account["state"], "checking")
        self.assertEqual(
            [action for action, _ in self.worker.submitted], ["account_status"]
        )
        self.assertEqual(ui.page, "home")

    def test_new_requires_verified_login_and_then_only_opens_hardware_selection(self):
        self.choose("new")
        self.assertEqual(self.ui.page, "account")
        self.assertEqual(self.ui.account_intent, "new")
        self.checked(SIGNED_OUT)
        self.assertEqual(self.ui.page, "account")
        self.choose("account_login")
        self.checked(AUTHENTICATED)
        self.assertEqual(self.ui.page, "hardware")
        self.assertEqual(
            [action for action, _ in self.worker.submitted],
            ["account_status", "account_login"],
        )
        self.assertEqual(self.ui.config.session, "")

    def test_existing_list_is_requested_only_after_account_verification(self):
        self.choose("existing")
        self.assertEqual(
            [action for action, _ in self.worker.submitted], ["account_status"]
        )
        self.checked(UNKNOWN)
        self.assertEqual(self.ui.page, "account")
        self.choose("account_check")
        self.checked(AUTHENTICATED)
        self.assertEqual(self.ui.page, "listing")
        self.assertEqual(self.worker.submitted[-1][0], "sessions")

    def test_startup_after_authorization_requires_review_and_new_confirmation(self):
        self.ui.config.cpu = True
        self.ui._page("summary")
        self.ui._begin()
        self.assertEqual(self.ui.account_intent, "begin")
        self.checked(AUTHENTICATED)
        self.assertEqual(self.ui.page, "summary")
        self.assertEqual(
            [action for action, _ in self.worker.submitted], ["account_status"]
        )
        with mock.patch.object(Path, "is_file", return_value=True):
            self.ui._begin()
        self.assertEqual(self.worker.submitted[-1][0], "wizard_create")

    def test_existing_session_constructor_authenticates_before_inspection(self):
        ui = wizard.Wizard(
            dashboard.Config(session="retain-fixture-runtime"), object(), self.worker
        )
        self.ui = ui
        self.assertEqual(ui.account_intent, "inspect")
        self.assertEqual(
            [action for action, _ in self.worker.submitted], ["account_status"]
        )
        self.checked(AUTHENTICATED)
        self.assertEqual(self.worker.submitted[-1][0], "inspect")
        self.assertEqual(ui.config.session, "retain-fixture-runtime")

    def test_cancelled_login_cannot_reopen_from_queued_auth_or_error_events(self):
        self.ui.config.session = "retain-fixture-runtime"
        self.ui.status = {"comfyui_alive": True}
        self.ui.ssh = {"running": True}
        self.ui._page("ready")
        self.choose("account_login")
        self.choose("back")
        self.assertTrue(self.worker.cancelled)
        self.worker.events.put(
            ("auth", {"url": "https://fixture.invalid/late", "waiting": True})
        )
        self.worker.events.put(("error", "Fixture authorization cancelled"))
        self.worker.events.put(("done", "account_login"))
        self.ui._events()
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual(self.ui.auth, {})
        self.assertEqual(self.ui.config.session, "retain-fixture-runtime")
        self.assertEqual(self.ui.status, {"comfyui_alive": True})
        self.assertEqual(self.ui.ssh, {"running": True})

    def test_model_management_does_not_require_account_or_runtime(self):
        with mock.patch.object(wizard, "model_catalog_entries", return_value=[]):
            self.choose("models")
        self.assertEqual(self.ui.page, "models")
        self.assertEqual(self.worker.submitted, [])

    def test_authorization_url_stays_in_details_and_code_requires_masked_enter(self):
        self.choose("account_login")
        address = "https://accounts.google.com/o/oauth2/auth?state=fixture-only"
        self.worker.events.put(
            ("auth", {"url": address, "waiting": True, "needs_code": True})
        )
        self.ui._events()
        self.assertEqual(self.ui.page, "account")
        self.assertIn(address, "\n".join(row[0] for row in self.ui._details(90)))
        self.choose("auth_code")
        for character in "fixture-accepted":
            self.ui._key(character)
        self.assertEqual(self.worker.responses, [])
        screen = mock.Mock()
        screen.getmaxyx.return_value = (36, 120)
        with (
            mock.patch.object(self.ui, "_write") as write,
            mock.patch.object(wizard.curses, "doupdate"),
        ):
            self.ui._draw(screen)
        displayed = "\n".join(call.args[3] for call in write.call_args_list)
        self.assertNotIn("fixture-accepted", displayed)
        self.assertIn("*" * len("fixture-accepted"), displayed)
        self.ui._key("\n")
        self.assertEqual(self.worker.responses, ["fixture-accepted"])
        self.assertEqual(self.ui.page, "account")
        self.assertEqual(self.ui.input_value, "")


if __name__ == "__main__":
    unittest.main()
