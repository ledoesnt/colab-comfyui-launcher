"""Isolated account storage, child CLI processes and allocation selection.

All tokens below are fixture markers. No Google service or real cache is used.
"""

import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import account_cli
import accounts
import budget_guard
import client_requests
import colab_provider
import colabctl
import dashboard
import ssh_forward
import wizard
from provider_terminal import ProviderTerminal
from test_budget_guard import Provider
from test_wizard import Frame, LocalWorker


class AccountFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "COLAB_LAUNCHER_ACCOUNTS_DIR": str(self.root / "accounts"),
                "COLAB_LAUNCHER_STATE_DIR": str(self.root / "state"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = accounts.AccountStore()

    def add_accounts(self):
        self.store.update(add="account-a")
        self.store.update(add="account-b")


class AccountStorageTests(AccountFixture):
    def test_missing_preferences_do_not_create_or_change_legacy_login(self):
        self.assertEqual(self.store.read()["active"], "default")
        self.assertFalse(self.store.root.exists())
        self.assertEqual(
            accounts.colab_command(["sessions"]), ["colab", "--auth=oauth2", "sessions"]
        )

    def test_saved_selection_fallback_order_and_private_permissions_survive_restart(
        self,
    ):
        self.add_accounts()
        self.store.update(active="account-a", toggle="account-b", enabled=True)
        self.store.update(toggle="default")
        value = accounts.AccountStore().read()
        self.assertEqual(value["active"], "account-a")
        self.assertEqual(value["fallbacks"], ["account-b", "default"])
        self.assertTrue(value["auto_fallback"])
        self.assertEqual(
            (self.store.root / "accounts.json").stat().st_mode & 0o777, 0o600
        )
        for path in (
            self.store.root,
            self.store.directory("account-a"),
            self.store.directory("account-a") / "cli",
        ):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_switch_preferences_never_reads_or_replaces_credentials(self):
        self.add_accounts()
        paths = [
            self.store.directory(name) / "cli/token.json"
            for name in ("account-a", "account-b")
        ]
        for path in paths:
            path.write_text("fixture")
            path.chmod(0o600)
        stamps = [path.stat() for path in paths]
        with mock.patch.object(
            Path, "read_text", side_effect=AssertionError("Do not read tokens")
        ):
            self.store.update(active="account-b")
            self.store.update(active="account-a")
        self.assertEqual([path.stat() for path in paths], stamps)

    def test_invalid_names_duplicates_and_unknown_selection_are_rejected(self):
        for value in (
            "../x",
            "/tmp/x",
            "a/b",
            "a.b",
            "-x",
            "",
            "x" * 33,
            "fixture@example.invalid",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(accounts.AccountError):
                self.store.update(
                    add=value
                ) if value is not None else accounts.account_id(value)
        self.add_accounts()
        for kwargs in (
            {"add": "account-a"},
            {"active": "absent"},
            {"toggle": "absent"},
        ):
            with self.assertRaises(accounts.AccountError):
                self.store.update(**kwargs)

    def test_symlink_registry_directory_and_credential_cache_are_rejected(self):
        other = self.root / "other"
        other.mkdir(mode=0o700)
        self.store.root.symlink_to(other, target_is_directory=True)
        with self.assertRaises(accounts.AccountError):
            self.store.update(add="account-a")
        self.store.root.unlink()
        self.add_accounts()
        cache = self.store.directory("account-a") / "cli/token.json"
        cache.symlink_to(other / "fixture")
        with self.assertRaises(accounts.AccountError):
            accounts.colab_command(["sessions"], account="account-a")
        cache.unlink()
        path = self.store.root / "accounts.json"
        path.unlink()
        path.symlink_to(other / "fixture")
        with self.assertRaises(OSError):
            self.store.read()

    def test_unsafe_or_corrupt_preferences_are_not_reset(self):
        self.add_accounts()
        path = self.store.root / "accounts.json"
        path.chmod(0o644)
        with self.assertRaises(accounts.AccountError):
            self.store.read()
        path.chmod(0o600)
        for value in ({}, {"schema": 99}, {"schema": 1, "profiles": [{}]}):
            path.write_text(json.dumps(value))
            with self.assertRaises(accounts.AccountError):
                self.store.read()

    def test_context_is_restored_and_saved_active_does_not_select_provider(self):
        self.add_accounts()
        self.store.update(active="account-b")
        self.assertEqual(accounts.current_account(), "default")
        with accounts.account_context("account-a"):
            provider = colab_provider.ColabProvider()
            with accounts.account_context("account-b"):
                self.assertEqual(accounts.current_account(), "account-b")
            self.assertEqual(accounts.current_account(), "account-a")
        self.assertEqual(provider.account, "account-a")
        self.assertEqual(
            provider.oauth_cache, self.store.directory("account-a") / "cli/token.json"
        )
        self.assertEqual(accounts.current_account(), "default")

    def test_same_named_session_has_separate_receipts_and_budget_roots(self):
        self.add_accounts()
        managers, receipts = [], []
        for name in ("account-a", "account-b"):
            with accounts.account_context(name):
                managers.append(budget_guard.BudgetManager())
                receipts.append(client_requests.ClientRequests("same-name"))
        self.assertNotEqual(
            managers[0]._path("same-name"), managers[1]._path("same-name")
        )
        self.assertNotEqual(receipts[0].directory, receipts[1].directory)
        self.assertEqual(managers[0].account, "account-a")

    def test_guard_keeps_original_account_after_selected_preference_changes(self):
        self.add_accounts()
        api = Provider()
        with accounts.account_context("account-a"):
            manager = budget_guard.BudgetManager(provider=api)
        manager.ensure_guard = mock.Mock()
        manager._guard_alive = mock.Mock(return_value=True)
        manager.create("same-name", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2)
        self.store.update(active="account-b")
        with accounts.account_context("account-b"):
            result = manager.release("same-name")
        self.assertEqual(result["account"], "account-a")
        self.assertTrue(result["verified"])
        self.assertEqual(len(api.released), 1)

    def test_wrong_guard_account_cannot_read_or_release_other_accounts_record(self):
        self.add_accounts()
        with accounts.account_context("account-a"):
            owner = budget_guard.BudgetManager(provider=Provider())
        owner.ensure_guard = mock.Mock()
        owner._guard_alive = mock.Mock(return_value=True)
        owner.create("fixture", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2)
        intruder_api = Provider()
        with accounts.account_context("account-b"):
            intruder = budget_guard.BudgetManager(owner.root, provider=intruder_api)
        with self.assertRaises(budget_guard.BudgetError):
            intruder.release("fixture")
        self.assertEqual(intruder_api.released, [])

    def test_guard_child_command_explicitly_binds_account(self):
        self.add_accounts()
        with accounts.account_context("account-a"):
            manager = budget_guard.BudgetManager(provider=Provider())
        manager.ensure_guard = mock.Mock()
        manager._guard_alive = mock.Mock(return_value=True)
        manager.create("fixture", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2)
        del manager.ensure_guard
        manager._guard_alive = mock.Mock(return_value=False)
        manager.popen = mock.Mock(
            return_value=mock.Mock(poll=mock.Mock(return_value=1))
        )
        with self.assertRaises(budget_guard.BudgetError):
            manager.ensure_guard("fixture")
        argv = manager.popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--account") + 1], "account-a")


FAKE_PACKAGE = r"""
import json
import os
import sys
from pathlib import Path

root = Path(os.path.expanduser("~/.config/colab-cli"))
root.mkdir(parents=True, exist_ok=True)
assert os.path.expanduser("~/.ssh") == str(Path(os.environ["HOME"]) / ".ssh")
token = Path(os.path.expanduser("~/.config/colab-cli/token.json"))
session_path = Path(os.path.expanduser("~/.config/colab-cli/sessions.json"))
for name in ("settings.json", "colab.log"):
    Path(os.path.expanduser("~/.config/colab-cli/" + name)).touch()
Path(os.path.expanduser("~/.config/colab-cli/history")).mkdir(exist_ok=True)
arguments = sys.argv[1:]
if arguments and arguments[0] == "--auth=oauth2":
    arguments.pop(0)
config = None
if arguments and arguments[0] == "--config":
    config = Path(arguments[1])
    del arguments[:2]
action = arguments[0]
if action == "version":
    print("Version: 0.7.4")
elif action == "sessions":
    if not token.exists():
        print("To authorize colab-cli, visit this URL in any browser:")
        print("https://accounts.google.com/fixture")
        print("Enter the authorization code: ", end="", flush=True)
        try:
            code = input()
        except EOFError:
            raise SystemExit(3)
        assert code == "fixture-consent"
        token.write_text("fixture-" + root.parent.name)
        session_path.write_text(json.dumps({"same-name": {"name": "same-name", "endpoint": root.parent.name}}))
    print("[colab] No active sessions found on server.")
elif action == "usage":
    assert token.read_text() == "fixture-" + root.parent.name
    print("Current balance: 10.00 compute units")
    print("Usage rate: 0.00/hr")
    print("Active assignments: 0")
elif action == "ssh":
    assert "--proxy-mode" in arguments
    entry = json.loads(config.read_text())["same-name"]
    assert entry["endpoint"] == root.parent.name
    print("fixture proxy route verified")
else:
    raise AssertionError("No mutating cloud command is permitted")
"""


class ChildCLITests(AccountFixture):
    def setUp(self):
        super().setUp()
        self.add_accounts()
        package = self.root / "colab_cli"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "cli.py").write_text(FAKE_PACKAGE)
        metadata = self.root / "google_colab_cli-0.7.4.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: google-colab-cli\nVersion: 0.7.4\n"
        )
        self.binary = self.root / "colab"
        self.binary.write_text("#!" + sys.executable + "\n# fixture entrypoint\n")
        self.binary.chmod(0o700)
        self.child_environment = dict(os.environ, PYTHONPATH=str(self.root))

    def cli(self, name, arguments, **kwargs):
        command = accounts.colab_command(
            arguments, account=name, executable=str(self.binary)
        )
        return subprocess.run(
            command,
            check=False,
            env=self.child_environment,
            text=True,
            capture_output=True,
            timeout=5,
            **kwargs,
        )

    def test_two_real_child_logins_are_preserved_and_reused_after_switching(self):
        before = os.environ.get("HOME")
        for name in ("account-a", "account-b"):
            result = self.cli(name, ["sessions"], input="fixture-consent\n")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.store.update(active="account-b")
        for name in ("account-a", "account-b", "account-a"):
            result = self.cli(name, ["usage"], stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("10.00 compute units", result.stdout)
        self.assertEqual(os.environ.get("HOME"), before)
        for name in ("account-a", "account-b"):
            root = self.store.directory(name) / "cli"
            self.assertEqual((root / "token.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads((root / "sessions.json").read_text())["same-name"][
                    "endpoint"
                ],
                name,
            )

    def test_readonly_missing_login_does_not_authorize_or_create_token(self):
        result = self.cli("account-b", ["sessions"], stdin=subprocess.DEVNULL)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            (self.store.directory("account-b") / "cli/token.json").exists()
        )

    def test_named_login_runs_inside_real_provider_pty_and_masks_fixture_code(self):
        command = accounts.colab_command(
            ["sessions"], account="account-b", executable=str(self.binary)
        )
        events = queue.Queue()
        terminal = ProviderTerminal(command, events.put, timeout=5)
        observed = []
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.root)}):
            terminal.start()
            try:
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    event = events.get(timeout=2)
                    observed.append(event)
                    if event["kind"] == "code_required":
                        self.assertTrue(terminal.respond("fixture-consent"))
                    if event["kind"] == "finished":
                        break
            finally:
                terminal.close()
                terminal.join(2)
        self.assertTrue((self.store.directory("account-b") / "cli/token.json").exists())
        self.assertNotIn("fixture-consent", json.dumps(observed))

    def test_unsupported_cli_version_is_rejected_before_module_execution(self):
        directory = self.store.directory("account-a") / "cli"
        with (
            mock.patch.object(
                account_cli.importlib.metadata, "version", return_value="0.8.0"
            ),
            mock.patch.object(account_cli.runpy, "run_module") as run,
            self.assertRaises(accounts.AccountError),
        ):
            account_cli.run(directory, ["sessions"])
        run.assert_not_called()

    def test_proxy_command_pins_saved_account_and_existing_snapshot(self):
        args = ssh_forward.parser().parse_args(
            ["--account", "account-b", "-s", "same-name", "start"]
        )
        with mock.patch.object(accounts.shutil, "which", return_value=str(self.binary)):
            command = ssh_forward.build_command(
                args,
                self.binary,
                Path("/usr/bin/ssh"),
                Path("/fixture/snapshot"),
                Path("/fixture/hosts"),
                "fixture",
            )
        proxy = next(item for item in command if item.startswith("ProxyCommand="))
        parts = shlex.split(proxy.split("=", 1)[1])
        self.assertIn(str(self.store.directory("account-b") / "cli"), parts)
        self.assertIn("--proxy-mode", parts)
        self.assertIn("/fixture/snapshot", parts)
        self.assertNotIn("new", parts)

    def test_proxy_child_reuses_only_the_selected_accounts_snapshot(self):
        snapshot = self.root / "proxy-fixture.json"
        snapshot.write_text(
            json.dumps(
                {
                    "same-name": {
                        "name": "same-name",
                        "endpoint": "account-b",
                        "token": "fixture-proxy",
                    }
                }
            )
        )
        snapshot.chmod(0o600)
        args = ssh_forward.parser().parse_args(
            ["--account", "account-b", "-s", "same-name", "start"]
        )
        command = ssh_forward.build_command(
            args,
            self.binary,
            Path("/usr/bin/ssh"),
            snapshot,
            self.root / "hosts",
            "fixture",
        )
        proxy = next(item for item in command if item.startswith("ProxyCommand="))
        result = subprocess.run(
            shlex.split(proxy.split("=", 1)[1]),
            env=self.child_environment,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture proxy route verified", result.stdout)


class FallbackTests(unittest.TestCase):
    def test_primary_funded_stops_selection_before_fallback(self):
        seen = []

        def usage():
            seen.append(accounts.current_account())
            return {"ok": True, "balance": 1}

        self.assertEqual(accounts.funded_account(["a", "b"], usage), "a")
        self.assertEqual(seen, ["a"])

    def test_zero_balance_uses_order_and_deduplicates(self):
        seen = []

        def usage():
            account = accounts.current_account()
            seen.append(account)
            return {"ok": True, "balance": 10 if account == "c" else 0}

        self.assertEqual(accounts.funded_account(["a", "a", "b", "c"], usage), "c")
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertEqual(accounts.current_account(), "default")

    def test_unknown_invalid_or_failed_usage_never_means_exhausted(self):
        for result in (
            {"ok": False},
            {"ok": True},
            {"ok": True, "balance": True},
            {"ok": True, "balance": float("nan")},
            {"ok": True, "balance": -1},
        ):
            usage = mock.Mock(return_value=result)
            with self.subTest(result=result), self.assertRaises(accounts.AccountError):
                accounts.funded_account(["a", "b"], usage)
            self.assertEqual(usage.call_count, 1)

    def test_all_exhausted_and_cancellation_do_not_allocate(self):
        with self.assertRaises(accounts.AccountError):
            accounts.funded_account(["a", "b"], lambda: {"ok": True, "balance": 0})
        usage = mock.Mock()
        with self.assertRaises(dashboard.OperationCancelled):
            accounts.funded_account(
                ["a", "b"], usage, mock.Mock(side_effect=dashboard.OperationCancelled())
            )
        usage.assert_not_called()


class WorkerAccountTests(AccountFixture):
    def setUp(self):
        super().setUp()
        self.add_accounts()
        self.calls = []
        self.balances = {"account-a": 0, "account-b": 10}
        self.backend = dashboard.Backend()
        self.backend.account_status = lambda: {
            "state": "authenticated",
            "message": "fixture",
        }
        self.backend.account_usage = lambda: {
            "ok": True,
            "balance": self.balances[accounts.current_account()],
        }
        self.backend.create = self.create
        self.backend.bridge = lambda config, action, **kwargs: {
            "ok": True,
            "budget": {"managed": False},
        }
        self.backend.account_logout = mock.Mock(
            side_effect=AssertionError("Do not delete any login")
        )
        self.worker = dashboard.Worker(self.backend)
        self.addCleanup(self.worker.thread.join, 2)
        self.addCleanup(self.worker.close)

    def create(self, config):
        self.calls.append((config.account, accounts.current_account()))
        return {"name": config.session, "hardware": "CPU"}

    def run_action(self, action, config):
        self.assertTrue(self.worker.submit(action, config))
        events = []
        until = time.monotonic() + 5
        while time.monotonic() < until:
            try:
                event = self.worker.events.get(timeout=0.2)
            except queue.Empty:
                continue
            events.append(event)
            if event == ("done", action):
                return events
        self.fail("Worker did not finish")

    def test_fallback_allocates_once_then_pipeline_uses_selected_account(self):
        observed = []
        self.worker._smart_pipeline = lambda config: observed.append(
            (config.account, accounts.current_account())
        )
        config = dashboard.Config(
            account="account-a",
            fallback_accounts=("account-b",),
            session="fixture",
            cpu=True,
        )
        events = self.run_action("wizard_create", config)
        self.assertFalse(any(kind == "error" for kind, _ in events), events)
        self.assertEqual(self.calls, [("account-b", "account-b")])
        self.assertEqual(observed, [("account-b", "account-b")])
        self.assertEqual(self.store.read()["active"], "account-b")
        self.assertTrue(
            any(
                kind == "allocation_account" and value.account == "account-b"
                for kind, value in events
            )
        )

    def test_unknown_creation_is_never_retried_on_another_account(self):
        self.balances["account-a"] = 10

        def unknown(config):
            self.calls.append(config.account)
            raise dashboard.DashboardError("Creation outcome is unknown")

        self.backend.create = unknown
        events = self.run_action(
            "new",
            dashboard.Config(
                account="account-a",
                fallback_accounts=("account-b",),
                session="fixture",
                cpu=True,
            ),
        )
        self.assertEqual(self.calls, ["account-a"])
        self.assertTrue(any(kind == "error" for kind, _ in events))

    def test_unresolved_original_budget_prevents_cross_account_reallocation(self):
        with accounts.account_context("account-a"):
            manager = budget_guard.BudgetManager(provider=Provider())
        manager.ensure_guard = mock.Mock()
        manager._guard_alive = mock.Mock(return_value=True)
        manager.create("fixture", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2)
        self.backend.create = mock.Mock(
            side_effect=dashboard.DashboardError(
                "Original allocation must be inspected"
            )
        )
        events = self.run_action(
            "new",
            dashboard.Config(
                account="account-b",
                fallback_accounts=("account-a",),
                session="fixture",
                cpu=True,
            ),
        )
        self.assertEqual(self.backend.create.call_count, 1)
        self.assertEqual(self.backend.create.call_args.args[0].account, "account-a")
        self.assertTrue(any(kind == "error" for kind, _ in events))

    def test_usage_diagnostics_cannot_trigger_fallback_as_if_balance_were_zero(self):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                "Current balance: 0.00 compute units\nUsage rate: 0.00/hr\nActive assignments: 0\n",
                "fixture auth error",
            )
        )
        backend = dashboard.Backend(run)
        self.assertEqual(backend.account_usage(), {"ok": False})

    def test_saved_selection_retains_credentials_and_does_not_create_or_stop(self):
        with mock.patch.object(
            dashboard, "active_managed_budgets", return_value=[{"status": "active"}]
        ):
            events = self.run_action(
                "profile_select", dashboard.Config(account="account-b")
            )
        self.assertIn(("profile_selected", "account-b"), events)
        self.backend.account_logout.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_unavailable_balance_stops_before_creation_or_fallback(self):
        self.backend.account_usage = lambda: {"ok": False}
        events = self.run_action(
            "new",
            dashboard.Config(
                account="account-a",
                fallback_accounts=("account-b",),
                session="fixture",
                cpu=True,
            ),
        )
        self.assertEqual(self.calls, [])
        self.assertTrue(any(kind == "error" for kind, _ in events))

    def test_bridge_and_ssh_pass_explicit_account_to_child(self):
        backend = dashboard.Backend()
        backend.json_command = mock.Mock(return_value={"ok": True})
        config = dashboard.Config(account="account-b", session="same-name")
        backend.bridge(config, "status")
        argv = backend.json_command.call_args.args[0]
        self.assertEqual(argv[argv.index("--account") + 1], "account-b")
        backend.ssh(config, "status")
        argv = backend.json_command.call_args.args[0]
        self.assertEqual(argv[argv.index("--account") + 1], "account-b")

    def test_direct_backend_create_binds_config_account_without_worker_context(self):
        backend = dashboard.Backend()
        observed = []

        def official(arguments, **kwargs):
            observed.append(accounts.current_account())
            if arguments[0] == "new":
                return "[colab] Session READY."
            return "[fixture] assignment | Hardware: CPU | Shape: Standard"

        backend.official = official
        backend.create(
            dashboard.Config(account="account-b", session="fixture", cpu=True)
        )
        self.assertEqual(observed, ["account-b", "account-b"])
        self.assertEqual(accounts.current_account(), "default")

    def test_cli_explicit_account_scopes_all_requests(self):
        with (
            mock.patch.object(
                colabctl,
                "run_command",
                side_effect=lambda *args: {
                    "ok": True,
                    "observed_account": accounts.current_account(),
                },
            ),
            mock.patch("builtins.print"),
        ):
            code = colabctl.main(
                ["--account", "account-b", "-s", "same-name", "status"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(accounts.current_account(), "default")


class AccountWizardTests(AccountFixture):
    def setUp(self):
        super().setUp()
        self.add_accounts()
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), dashboard.Backend(), self.worker)
        self.worker.submitted.clear()
        self.ui.account = {"state": "authenticated", "message": "fixture"}

    def choose(self, key):
        self.ui.selected = [choice.key for choice in self.ui._choices()].index(key)
        self.ui._key("\n")

    def test_account_menu_exposes_saved_profiles_and_switch_does_not_logout(self):
        self.ui._page("account")
        self.choose("profiles")
        self.assertEqual(self.ui.page, "profiles")
        self.choose("profile:account-b")
        self.assertEqual(self.worker.submitted[-1][0], "profile_select")
        self.assertEqual(self.worker.submitted[-1][1].account, "account-b")
        self.assertNotIn(
            "account_switch", [action for action, _ in self.worker.submitted]
        )

    def test_new_profile_uses_alias_input_then_separate_interactive_signin(self):
        self.ui._page("profiles")
        self.choose("profile_add")
        self.ui.input_value = "account-c"
        self.ui._confirm_input()
        self.assertEqual(self.worker.submitted[-1][0], "profile_add")
        self.assertEqual(self.worker.submitted[-1][1].profile_name, "account-c")

    def test_fallback_preferences_are_saved_in_enable_order(self):
        self.ui._page("account")
        self.choose("profiles")
        self.choose("fallback:account-b")
        self.choose("fallback:account-a")
        self.assertEqual(self.ui.config.fallback_accounts, ())
        self.choose("profile_auto")
        self.assertEqual(self.ui.config.fallback_accounts, ("account-b", "account-a"))
        self.assertTrue(self.store.read()["auto_fallback"])

    def test_switch_discards_old_runtime_evidence_and_balance(self):
        self.ui.config.session = "old-runtime"
        self.ui.usage = {"ok": True, "balance": 99}
        self.ui.ssh = {"running": True}
        self.worker.events.put(("profile_selected", "account-b"))
        self.ui._events()
        self.assertEqual(self.ui.config.account, "account-b")
        self.assertEqual(self.ui.config.session, "")
        self.assertEqual(self.ui.usage, {})
        self.assertEqual(self.ui.ssh, {})

    def test_fallback_event_preserves_attempted_session_and_budget_on_error(self):
        selected = dashboard.Config(
            account="account-b",
            session="attempted",
            cpu=True,
            cu_limit=2,
            rate_ceiling=4,
        )
        self.worker.events.put(("allocation_account", selected))
        self.worker.events.put(("error", "fixture unknown allocation"))
        self.ui._events()
        self.assertEqual(self.ui.config, selected)

    def test_profile_menu_renders_at_supported_sizes_and_back_returns_to_account(self):
        self.ui._page("account")
        self.choose("profiles")
        for height, width in ((24, 50), (30, 80), (36, 120)):
            frame = Frame(height, width)
            with mock.patch.object(wizard.curses, "doupdate"):
                self.ui._draw(frame)
        self.choose("back")
        self.assertEqual(self.ui.page, "account")


if __name__ == "__main__":
    unittest.main()
