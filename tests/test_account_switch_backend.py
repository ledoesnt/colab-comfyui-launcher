"""Local Colab sign-out/switch through isolated CLI processes and real PTYs."""

import importlib.util
import queue
import subprocess
import sys
import tempfile
import time
import unittest
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
provider = load_module("switch_account_provider", ROOT / "scripts/provider_terminal.py")

# These files contain fixture markers, never actual OAuth credential contents.
FAKE_CLI = r"""
import os
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
arguments = sys.argv[2:]
assert arguments in (["version"], ["--auth=oauth2", "sessions"])
with (root / "commands").open("a") as stream:
    stream.write(" ".join(arguments) + " tty=" + str(sys.stdin.isatty()) + "\n")
mode = (root / "mode").read_text()
if arguments == ["version"]:
    if mode == "version_delayed":
        (root / "entered_version").touch()
        time.sleep(0.4)
    print("Version: 0.8.0" if mode == "unsupported" else "Version: 0.7.4")
    raise SystemExit(0)
cache = root / ".config/colab-cli/token.json"
if mode == "network":
    print("fixture inaccessible", file=sys.stderr)
    raise SystemExit(2)
if cache.exists():
    print("[colab] No active sessions found on server.")
    raise SystemExit(0)
print("To authorize colab-cli, visit this URL in any browser:", file=sys.stderr, flush=True)
print("https://accounts.google.com/o/oauth2/auth?state=fixture-private", file=sys.stderr, flush=True)
print("Enter the authorization code: ", end="", flush=True)
if sys.stdin.isatty():
    with open("/dev/tty") as terminal:
        assert os.isatty(terminal.fileno())
        code = terminal.readline().strip()
else:
    try:
        code = input()
    except EOFError:
        raise SystemExit(3)
if code != "fixture-other-account":
    print("code=fixture-sensitive-error", file=sys.stderr)
    raise SystemExit(4)
if mode != "zero_unverified":
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("fixture-account-B")
    (root / "replacement_authorized").touch()
print("[colab] No active sessions found on server.")
"""


class LocalAccountBackend(dashboard.Backend):
    def __init__(self, directory):
        self.directory = directory
        self.timeout = 3
        self.terminals = []
        self.create = mock.Mock(side_effect=AssertionError("Must not allocate"))
        self.release = mock.Mock(side_effect=AssertionError("Must not stop VM"))
        self.bridge = mock.Mock(side_effect=AssertionError("Must not touch Drive"))
        self.ssh = mock.Mock(side_effect=AssertionError("Must not touch SSH"))
        super().__init__(self.run_fixture)

    def command(self, arguments):
        return [
            sys.executable,
            "-u",
            str(self.directory / "cli.py"),
            str(self.directory),
            *arguments,
        ]

    def run_fixture(self, argv, **kwargs):
        if argv not in (
            ["colab", "version"],
            ["colab", "--auth=oauth2", "sessions"],
        ):
            raise AssertionError("Unexpected or mutating provider command")
        return subprocess.run(self.command(argv[1:]), check=False, **kwargs)

    def account_logout(self, **kwargs):
        with mock.patch.object(dashboard.Path, "home", return_value=self.directory):
            return super().account_logout(**kwargs)

    def account_terminal(self, _config, on_event):
        terminal = provider.ProviderTerminal(
            self.command(["--auth=oauth2", "sessions"]), on_event, self.timeout
        )
        self.terminals.append(terminal)
        return terminal


class AccountFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-account-switch-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "cli.py").write_text(FAKE_CLI)
        (self.directory / "mode").write_text("normal")
        self.cache = self.directory / ".config/colab-cli/token.json"
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text("fixture-account-A")
        self.keep = [
            self.cache.parent / "sessions.json",
            self.directory / ".config/gcloud/application_default_credentials.json",
            self.directory / ".ssh/dedicated_key",
            self.directory / ".colab-cli-oauth-config.json",
            self.directory / "drive_data",
        ]
        for path in self.keep:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture-retained")
        self.backend = LocalAccountBackend(self.directory)

    def assert_preserved(self):
        for path in self.keep:
            self.assertEqual(path.read_text(), "fixture-retained")
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()

    def command_lines(self):
        commands = self.directory / "commands"
        return commands.read_text().splitlines() if commands.exists() else []


class AccountLogoutBackendTests(AccountFixture):
    def test_logout_unlinks_only_standard_cache_without_reading_or_login_request(self):
        with mock.patch.object(
            Path, "open", side_effect=AssertionError("No cache read")
        ):
            result = self.backend.account_logout()
        self.assertEqual(result["state"], "not_authenticated")
        self.assertEqual(result["reason"], "local_oauth_cache_removed")
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.command_lines(), ["version tty=False"])
        self.assert_preserved()

    def test_already_absent_cache_is_verified_without_sessions(self):
        self.cache.unlink()
        result = self.backend.account_logout()
        self.assertEqual(result["reason"], "local_oauth_cache_absent")
        self.assertEqual(self.command_lines(), ["version tty=False"])
        self.assert_preserved()

    def test_missing_cache_parent_is_not_created(self):
        self.cache.unlink()
        retained = self.keep.pop(0)
        retained.unlink()
        self.cache.parent.rmdir()
        self.assertEqual(
            self.backend.account_logout()["reason"], "local_oauth_cache_absent"
        )
        self.assertFalse(self.cache.parent.exists())
        self.assert_preserved()

    def test_cache_symlink_is_rejected_and_target_preserved(self):
        self.cache.unlink()
        self.cache.symlink_to(self.keep[-1])
        with self.assertRaisesRegex(dashboard.DashboardError, "nonregular"):
            self.backend.account_logout()
        self.assertTrue(self.cache.is_symlink())
        self.assert_preserved()

    def test_cache_directory_is_rejected(self):
        self.cache.unlink()
        self.cache.mkdir()
        with self.assertRaisesRegex(dashboard.DashboardError, "nonregular"):
            self.backend.account_logout()
        self.assertTrue(self.cache.is_dir())
        self.assert_preserved()

    def test_cache_parent_symlink_is_rejected(self):
        old_parent = self.cache.parent
        moved = self.directory / "separate-cache"
        old_parent.rename(moved)
        old_parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(dashboard.DashboardError, "nonstandard"):
            self.backend.account_logout()
        self.assertEqual((moved / "token.json").read_text(), "fixture-account-A")
        self.assert_preserved()

    def test_config_parent_symlink_is_rejected(self):
        old_config = self.directory / ".config"
        moved = self.directory / "separate-config"
        old_config.rename(moved)
        old_config.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(dashboard.DashboardError, "nonstandard"):
            self.backend.account_logout()
        self.assertEqual(self.cache.read_text(), "fixture-account-A")
        self.assert_preserved()

    def test_unsupported_cli_does_not_modify_cache(self):
        (self.directory / "mode").write_text("unsupported")
        with self.assertRaisesRegex(dashboard.DashboardError, "0.7.4 only"):
            self.backend.account_logout()
        self.assertEqual(self.cache.read_text(), "fixture-account-A")
        self.assert_preserved()

    def test_failed_cli_version_is_unknown_without_exposing_output(self):
        self.backend.run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 2, "code=fixture-secret", "token=fixture-secret"
            )
        )
        with self.assertRaises(dashboard.DashboardError) as error:
            self.backend.account_logout()
        self.assertNotIn("fixture-secret", str(error.exception))
        self.assertTrue(self.cache.exists())
        self.assert_preserved()

    def test_missing_or_timed_out_cli_preserves_cache_and_hides_output(self):
        for failure in (
            FileNotFoundError("fixture-secret"),
            subprocess.TimeoutExpired([], 15, output="fixture-secret"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.backend.run = mock.Mock(side_effect=failure)
                with self.assertRaises(dashboard.DashboardError) as error:
                    self.backend.account_logout()
                self.assertNotIn("fixture-secret", str(error.exception))
                self.assertTrue(self.cache.exists())
        self.assert_preserved()

    def test_unlink_failure_is_not_signed_out(self):
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("private")),
            self.assertRaisesRegex(dashboard.DashboardError, "not verified"),
        ):
            self.backend.account_logout()
        self.assertTrue(self.cache.exists())
        self.assert_preserved()

    def test_cache_recreated_during_logout_is_not_signed_out(self):
        original_unlink = Path.unlink

        def recreate(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            path.write_text("fixture-concurrent-account")

        with (
            mock.patch.object(Path, "unlink", recreate),
            self.assertRaisesRegex(dashboard.DashboardError, "recreated"),
        ):
            self.backend.account_logout()
        self.assertTrue(self.cache.exists())
        self.assert_preserved()

    def test_cancellation_checkpoint_prevents_removal(self):
        checkpoint = mock.Mock(side_effect=dashboard.OperationCancelled("Cancelled"))
        with self.assertRaises(dashboard.OperationCancelled):
            self.backend.account_logout(check_cancelled=checkpoint)
        self.assertTrue(self.cache.exists())
        self.assert_preserved()


class AccountSwitchWorkerTests(AccountFixture):
    def setUp(self):
        super().setUp()
        self.worker = dashboard.Worker(self.backend)
        self.worker.account_checked = True
        self.config = dashboard.Config(session="retain-original-runtime")
        self.seen = []
        self.addCleanup(self.close_worker)

    def close_worker(self):
        self.worker.close()
        self.worker.thread.join(2)
        self.assertFalse(self.worker.thread.is_alive())
        self.assert_preserved()

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
        self.fail("Local account fixture did not reach expected event")

    def switch(self):
        self.assertTrue(self.worker.submit("account_switch", self.config))
        self.until(lambda kind, value: kind == "auth" and value.get("needs_code"))
        self.assertFalse(self.worker.account_checked)
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.config.session, "retain-original-runtime")

    def test_logout_never_starts_login_or_rechecks_sessions(self):
        self.assertTrue(self.worker.submit("account_logout", self.config))
        self.until(lambda kind, value: kind == "done" and value == "account_logout")
        self.assertFalse(self.worker.account_checked)
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.command_lines(), ["version tty=False"])
        self.assertEqual(self.backend.terminals, [])
        signed_out = [
            value for kind, value in self.seen if kind == "account_signed_out"
        ]
        self.assertEqual(len(signed_out), 1)
        self.assertEqual(signed_out[0]["action"], "account_logout")

    def test_switch_reauthorizes_real_pty_then_verifies_read_only(self):
        self.switch()
        self.assertTrue(self.worker.send_auth("fixture-other-account"))
        self.until(lambda kind, value: kind == "done" and value == "account_switch")
        self.assertTrue(self.worker.account_checked)
        self.assertTrue((self.directory / "replacement_authorized").exists())
        self.assertEqual(
            self.command_lines(),
            [
                "version tty=False",
                "--auth=oauth2 sessions tty=True",
                "--auth=oauth2 sessions tty=False",
            ],
        )
        self.assertEqual(
            [value["state"] for kind, value in self.seen if kind == "account"],
            ["not_authenticated", "authenticated"],
        )
        signed_out_index = next(
            index
            for index, (kind, _) in enumerate(self.seen)
            if kind == "account_signed_out"
        )
        authorization_index = next(
            index for index, (kind, _) in enumerate(self.seen) if kind == "auth"
        )
        self.assertLess(signed_out_index, authorization_index)
        self.assertNotIn("fixture-other-account", str(self.seen))
        self.assertIsNone(self.worker.auth_terminal)

    def test_switch_refuses_authorization_if_signout_not_verified(self):
        (self.directory / "mode").write_text("unsupported")
        self.assertTrue(self.worker.submit("account_switch", self.config))
        self.until(lambda kind, value: kind == "done")
        self.assertTrue(self.cache.exists())
        self.assertEqual(self.backend.terminals, [])
        self.assertFalse(self.worker.account_checked)
        self.assertFalse(any(kind == "account_signed_out" for kind, _ in self.seen))
        self.assertFalse(any(kind == "account_invalidated" for kind, _ in self.seen))
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))
        states = [value["state"] for kind, value in self.seen if kind == "account"]
        self.assertEqual(states, ["unavailable"])

    def assert_unknown_after_cache_changed(self):
        self.assertFalse(self.worker.account_checked)
        self.assertEqual(self.backend.terminals, [])
        self.assertEqual(self.command_lines(), ["version tty=False"])
        self.assertFalse(any(kind == "account_signed_out" for kind, _ in self.seen))
        invalidated = [
            value for kind, value in self.seen if kind == "account_invalidated"
        ]
        self.assertEqual(len(invalidated), 1)
        self.assertEqual(invalidated[0]["action"], "account_switch")
        self.assertEqual(invalidated[0]["state"], "unavailable")
        self.assertEqual(invalidated[0]["reason"], "logout_unverified")
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))
        states = [value["state"] for kind, value in self.seen if kind == "account"]
        self.assertEqual(states, ["unavailable"])

    def test_cache_recreated_after_actual_unlink_invalidates_old_proof(self):
        original_unlink = Path.unlink

        def recreate(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            path.write_text("fixture-concurrent-account")

        with mock.patch.object(Path, "unlink", recreate):
            self.assertTrue(self.worker.submit("account_switch", self.config))
            self.until(lambda kind, value: kind == "done")
        self.assertEqual(self.cache.read_text(), "fixture-concurrent-account")
        self.assert_unknown_after_cache_changed()

    def test_stat_failure_after_actual_unlink_invalidates_old_proof(self):
        original_lstat = Path.lstat

        def fail_after_removal(path, *args, **kwargs):
            if path == self.cache and not path.exists():
                raise PermissionError("fixture-private-stat-error")
            return original_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", fail_after_removal):
            self.assertTrue(self.worker.submit("account_switch", self.config))
            self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.cache.exists())
        self.assertNotIn("fixture-private-stat-error", str(self.seen))
        self.assert_unknown_after_cache_changed()

    def test_cancel_during_version_check_keeps_original_cache(self):
        (self.directory / "mode").write_text("version_delayed")
        self.assertTrue(self.worker.submit("account_switch", self.config))
        deadline = time.monotonic() + 3
        while not (self.directory / "entered_version").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)
        self.assertTrue(self.worker.cancel_account())
        self.until(lambda kind, value: kind == "done")
        self.assertEqual(self.cache.read_text(), "fixture-account-A")
        self.assertEqual(self.backend.terminals, [])
        self.assertFalse(any(kind == "account_signed_out" for kind, _ in self.seen))
        self.assertFalse(self.worker.account_checked)

    def test_cancel_after_signout_preserves_truth_and_rejects_late_auth(self):
        self.switch()
        self.assertTrue(self.worker.cancel_account())
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.worker.account_checked)
        self.assertTrue(any(kind == "account_signed_out" for kind, _ in self.seen))
        self.assertIsNone(self.worker.auth_terminal)
        self.assertIn(("auth_clear", None), self.seen)
        terminal = self.backend.terminals[0]
        terminal.on_event({"kind": "auth_url", "url": "https://fixture.invalid/late"})
        self.assertTrue(self.worker.events.empty())

    def test_switch_timeout_never_claims_authenticated(self):
        self.backend.timeout = 0.3
        self.switch()
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertFalse(self.cache.exists())
        self.assertIsNone(self.worker.auth_terminal)
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))
        self.assertNotIn(
            "authenticated",
            [value["state"] for kind, value in self.seen if kind == "account"],
        )

    def test_replacement_cli_failure_never_reuses_old_login(self):
        self.switch()
        self.assertTrue(self.worker.send_auth("fixture-rejected"))
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertFalse(self.cache.exists())
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))
        self.assertNotIn("fixture-rejected", str(self.seen))
        self.assertNotIn("fixture-sensitive-error", str(self.seen))

    def test_zero_cli_exit_without_postlogin_proof_is_not_authenticated(self):
        (self.directory / "mode").write_text("zero_unverified")
        self.switch()
        self.assertTrue(self.worker.send_auth("fixture-other-account"))
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertFalse(self.cache.exists())
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))
        self.assertNotIn(
            "authenticated",
            [value["state"] for kind, value in self.seen if kind == "account"],
        )

    def test_network_postlogin_failure_is_unavailable_not_new_proof(self):
        self.switch()
        (self.directory / "mode").write_text("network")
        self.assertTrue(self.worker.send_auth("fixture-other-account"))
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.worker.account_checked)
        self.assertTrue(self.cache.exists())
        states = [value["state"] for kind, value in self.seen if kind == "account"]
        self.assertEqual(states, ["not_authenticated", "unavailable"])
        self.assertTrue(any(kind == "error" for kind, _ in self.seen))

    def test_malformed_logout_result_blocks_replacement_login(self):
        self.backend.account_logout = mock.Mock(
            return_value={"state": "authenticated", "reason": "unexpected"}
        )
        self.assertTrue(self.worker.submit("account_switch", self.config))
        self.until(lambda kind, value: kind == "done")
        self.assertEqual(self.backend.terminals, [])
        self.assertFalse(self.worker.account_checked)
        self.assertFalse(any(kind == "account_signed_out" for kind, _ in self.seen))
        self.assertTrue(self.cache.exists())

    def test_signedout_event_not_hidden_by_cancellation_after_cache_change(self):
        original_logout = self.backend.account_logout

        def logout_then_cancel(**kwargs):
            result = original_logout(**kwargs)
            self.worker.account_cancelled.set()
            self.worker.operation_cancelled.set()
            return result

        self.backend.account_logout = logout_then_cancel
        self.assertTrue(self.worker.submit("account_switch", self.config))
        self.until(lambda kind, value: kind == "done")
        self.assertFalse(self.cache.exists())
        self.assertTrue(any(kind == "account_signed_out" for kind, _ in self.seen))
        self.assertEqual(self.backend.terminals, [])
        self.assertFalse(self.worker.account_checked)

    def test_pending_switch_cancelled_before_activation_does_not_touch_cache(self):
        self.worker.close()
        self.worker.thread.join(2)
        with mock.patch.object(dashboard.threading.Thread, "start"):
            self.worker = dashboard.Worker(self.backend)
        self.assertTrue(self.worker.submit("account_switch", self.config))
        self.assertTrue(self.worker.cancel_account())
        self.worker.thread.start()
        self.until(lambda kind, value: kind == "done")
        self.assertTrue(self.cache.exists())
        self.assertEqual(self.command_lines(), [])
        self.assertEqual(self.backend.terminals, [])


if __name__ == "__main__":
    unittest.main()
