"""Legacy storage migration and first-login naming, using isolated fixtures."""

import json
import os
import queue
from pathlib import Path
from unittest import mock

from test_accounts import (
    AccountFixture,
    accounts,
    colabctl,
    dashboard,
    ssh_forward,
    wizard,
)
from test_wizard import LocalWorker


class MigrationFixture(AccountFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        home = mock.patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        idle = mock.patch.object(accounts, "require_idle_accounts")
        self.idle = idle.start()
        self.addCleanup(idle.stop)
        self.legacy = self.home / ".config/colab-cli"

    def populate(self):
        self.add_accounts()
        self.store.update(active="account-b", toggle="default")
        self.legacy.mkdir(parents=True)
        for name in ("token.json", "sessions.json", "settings.json", "colab.log"):
            (self.legacy / name).write_text("fixture-only-" + name)
            (self.legacy / name).chmod(0o664)
        (self.legacy / "history").mkdir()
        (self.legacy / "history/item").write_text("fixture history")
        self.record = {
            "schema": 1,
            "session": "fixture",
            "job_id": "original-job",
            "request_id": "original-request",
            "allocated_at": 100,
            "work_deadline": 200,
            "release_deadline": 250,
            "status": "release_pending",
            "accounted_seconds": 20,
        }
        self.budget = accounts.launcher_root() / "budgets/job.json"
        self.budget.parent.mkdir(parents=True)
        self.budget.write_text(json.dumps(self.record))
        for part in ("requests", "ssh"):
            path = accounts.launcher_root() / part
            path.mkdir()
            (path / "fixture").write_text("fixture retained state")


class MigrationTests(MigrationFixture):
    def test_moves_cache_and_all_state_without_opening_any_cli_file(self):
        self.populate()
        token = self.legacy / "token.json"
        before = token.stat()
        other = self.store.directory("account-b") / "cli/token.json"
        other.write_text("fixture other account")
        other.chmod(0o600)
        other_before = other.stat()
        original_open = Path.open

        def only_metadata(path, *args, **kwargs):
            if path.name in (
                "token.json",
                "sessions.json",
                "settings.json",
                "colab.log",
            ):
                raise AssertionError("Do not open CLI cache files")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", only_metadata):
            preferences = self.store.migrate_default("primary")
        destination = self.store.directory("primary")
        after = (destination / "cli/token.json").stat()
        self.assertEqual(
            (before.st_ino, before.st_mtime_ns, before.st_size),
            (after.st_ino, after.st_mtime_ns, after.st_size),
        )
        self.assertEqual(after.st_mode & 0o777, 0o600)
        self.assertEqual(other.stat(), other_before)
        self.assertEqual(preferences["profiles"], ["primary", "account-a", "account-b"])
        self.assertEqual(preferences["active"], "account-b")
        self.assertEqual(preferences["fallbacks"], ["primary"])
        self.assertTrue(self.legacy.is_symlink())
        self.assertEqual(self.legacy.resolve(), destination / "cli")
        self.assertEqual(
            json.loads((destination / "state/budgets/job.json").read_text()),
            {**self.record, "account": "primary"},
        )
        for part in ("budgets", "requests", "ssh"):
            self.assertFalse((accounts.launcher_root() / part).exists())
        with accounts.account_context("primary"):
            self.assertEqual(accounts.state_root(), destination / "state")
            self.assertEqual(accounts.cli_directory(), destination / "cli")

    def test_migrated_primary_stays_implicit_default_when_active_account_changes(self):
        self.populate()
        self.store.update(active="default")
        self.assertEqual(self.store.migrate_default("primary")["active"], "primary")
        self.store.update(active="account-b")
        self.assertEqual(accounts.resolve_account("default"), "primary")
        self.assertEqual(accounts.resolve_account("account-b"), "account-b")

    def test_repeat_migration_is_idempotent(self):
        self.populate()
        value = self.store.migrate_default("primary")
        token = self.store.directory("primary") / "cli/token.json"
        before = token.stat()
        self.assertEqual(self.store.migrate_default("primary"), value)
        self.assertEqual(token.stat(), before)

    def test_existing_destination_is_not_overwritten(self):
        self.populate()
        self.store.update(add="primary")
        with self.assertRaises(accounts.AccountError):
            self.store.migrate_default("primary")
        self.assertFalse(self.legacy.is_symlink())
        self.assertTrue(self.budget.exists())

    def test_symlink_or_hardlinked_cache_is_rejected_before_any_move(self):
        self.populate()
        token = self.legacy / "token.json"
        token.unlink()
        token.symlink_to(self.home / "outside")
        with self.assertRaises(accounts.AccountError):
            self.store.migrate_default("primary")
        token.unlink()
        token.write_text("fixture")
        os.link(token, self.home / "linked")
        with self.assertRaises(accounts.AccountError):
            self.store.migrate_default("primary")
        self.assertTrue(self.budget.exists())
        self.assertFalse((self.store.root / "primary").exists())

    def test_other_account_or_corrupt_budget_preserves_original_storage(self):
        self.populate()
        for record in ({**self.record, "account": "account-b"}, {"schema": 99}):
            self.budget.write_text(json.dumps(record))
            with self.assertRaises(accounts.AccountError):
                self.store.migrate_default("primary")
            self.assertTrue(self.legacy.is_dir())
            self.assertFalse(self.legacy.is_symlink())
            self.assertTrue(self.budget.exists())

    def test_live_controller_blocks_migration(self):
        self.populate()
        self.idle.side_effect = accounts.AccountError("fixture controller running")
        with self.assertRaises(accounts.AccountError):
            self.store.migrate_default("primary")
        self.assertTrue(self.budget.exists())
        self.assertNotIn("primary", self.store.read()["profiles"])

    def test_preferences_write_failure_rolls_back_directory_moves_and_budget_label(
        self,
    ):
        self.populate()
        before = self.store.read()
        token_before = (self.legacy / "token.json").stat()
        with (
            mock.patch.object(self.store, "_save", side_effect=OSError("fixture")),
            self.assertRaises(OSError),
        ):
            self.store.migrate_default("primary")
        self.assertFalse(self.legacy.is_symlink())
        self.assertEqual(
            (self.legacy / "token.json").stat().st_ino, token_before.st_ino
        )
        self.assertEqual(json.loads(self.budget.read_text()), self.record)
        self.assertEqual(self.store.read(), before)
        self.assertFalse((self.store.root / "primary").exists())

    def test_default_cli_and_ssh_resolve_original_account_after_migration(self):
        self.populate()
        self.store.migrate_default("primary")
        with (
            mock.patch.object(
                colabctl, "run_command", return_value={"ok": True}
            ) as run,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(colabctl.main(["-s", "fixture", "status"]), 0)
            self.assertEqual(run.call_args.args[0].account, "primary")
        with (
            mock.patch.object(
                ssh_forward, "execute", return_value={"ok": True}
            ) as execute,
            mock.patch("builtins.print"),
        ):
            ssh_forward.main(["-s", "fixture", "status"])
            args = execute.call_args.args[0]
        self.assertEqual(args.account, "primary")
        self.assertEqual(
            args.config, self.store.directory("primary") / "cli/sessions.json"
        )
        self.assertEqual(args.state_dir, self.store.directory("primary") / "state/ssh")


class FirstLoginTests(MigrationFixture):
    def test_cli_add_initializes_and_selects_the_first_named_account(self):
        with mock.patch("builtins.print"):
            self.assertEqual(accounts.main(["add", "primary"]), 0)
        value = self.store.read()
        self.assertEqual(value["profiles"], ["primary"])
        self.assertEqual(value["active"], "primary")

    def test_first_login_worker_authorizes_inside_the_new_account_context(self):
        backend = dashboard.Backend()
        worker = dashboard.Worker(backend)
        self.addCleanup(worker.thread.join, 2)
        self.addCleanup(worker.close)
        seen = []
        worker._authorize_account = lambda config: seen.append(
            (config.account, accounts.current_account())
        )
        self.assertTrue(
            worker.submit("profile_login", dashboard.Config(profile_name="primary"))
        )
        events = []
        while True:
            try:
                event = worker.events.get(timeout=3)
            except queue.Empty:
                self.fail("First login did not finish")
            events.append(event)
            if event == ("done", "profile_login"):
                break
        self.assertFalse(any(kind == "error" for kind, _ in events), events)
        self.assertEqual(seen, [("primary", "primary")])
        self.assertEqual(self.store.read()["profiles"], ["primary"])

    def test_first_login_selection_preserves_the_requested_startup_flow(self):
        self.store.update(add="primary", active="primary", first=True)
        worker = LocalWorker()
        ui = wizard.Wizard(dashboard.Config(), dashboard.Backend(), worker)
        ui.account_intent = "new"
        ui.last_operation = "profile_login"
        worker.events.put(("profile_selected", "primary"))
        ui._events()
        self.assertEqual(ui.account_intent, "new")
        self.assertEqual(ui.config.account, "primary")

    def test_startup_status_waits_for_name_without_invoking_legacy_cli(self):
        run = mock.Mock(side_effect=AssertionError("Do not authorize before naming"))
        status = dashboard.Backend(run).account_status()
        self.assertEqual(status["reason"], "first_name_required")
        self.assertEqual(status["state"], "not_authenticated")
        run.assert_not_called()
        self.assertFalse(self.legacy.exists())

    def test_new_install_names_first_account_without_creating_default_profile(self):
        self.assertTrue(self.store.first_name_required())
        value = self.store.update(add="primary", active="primary", first=True)
        self.assertEqual(value["profiles"], ["primary"])
        self.assertFalse(self.store.first_name_required())
        self.assertFalse(self.legacy.exists())

    def test_legacy_token_or_runtime_state_prevents_initialization(self):
        self.legacy.mkdir(parents=True)
        token = self.legacy / "token.json"
        token.write_text("fixture retained login")
        self.assertFalse(self.store.first_name_required())
        with self.assertRaises(accounts.AccountError):
            self.store.update(add="primary", active="primary", first=True)
        token.unlink()
        (accounts.launcher_root() / "budgets").mkdir(parents=True)
        self.assertFalse(self.store.first_name_required())

    def test_first_signin_opens_name_input_then_submits_interactive_login(self):
        worker = LocalWorker()
        ui = wizard.Wizard(dashboard.Config(), dashboard.Backend(), worker)
        worker.submitted.clear()
        ui.account = {"state": "not_authenticated"}
        ui._page("account")
        ui.selected = [c.key for c in ui._choices()].index("account_login")
        ui._key("\n")
        self.assertEqual(ui.input_field, "first_profile_name")
        self.assertEqual(worker.submitted, [])
        ui.input_value = "primary"
        ui._confirm_input()
        self.assertEqual(worker.submitted[-1][0], "profile_login")
        self.assertEqual(worker.submitted[-1][1].profile_name, "primary")

    def test_cancel_first_name_does_not_save_or_authorize(self):
        worker = LocalWorker()
        ui = wizard.Wizard(dashboard.Config(), dashboard.Backend(), worker)
        worker.submitted.clear()
        ui._page("account")
        ui._input("first_profile_name", "account")
        ui._key("\x1b")
        self.assertEqual(ui.page, "account")
        self.assertEqual(worker.submitted, [])
        self.assertFalse((self.store.root / "accounts.json").exists())


class AutomaticUpgradeTests(MigrationFixture):
    def test_first_launch_asks_for_existing_account_name_before_provider_requests(self):
        self.populate()
        worker = LocalWorker()
        ui = wizard.Wizard(
            dashboard.Config(session="fixture-runtime"), dashboard.Backend(), worker
        )
        self.assertEqual(ui.page, "input")
        self.assertEqual(ui.input_field, "existing_account_name")
        self.assertEqual(ui.account_intent, "inspect")
        self.assertEqual(worker.submitted, [])
        ui.input_value = "primary"
        ui._confirm_input()
        action, config = worker.submitted[-1]
        self.assertEqual(action, "profile_migrate")
        self.assertEqual(config.profile_name, "primary")
        self.assertEqual(config.session, "fixture-runtime")

    def test_postponing_automatic_upgrade_does_not_change_login_or_preferences(self):
        self.populate()
        before = self.store.read()
        token_before = (self.legacy / "token.json").stat()
        worker = LocalWorker()
        ui = wizard.Wizard(dashboard.Config(), dashboard.Backend(), worker)
        ui._key("\x1b")
        self.assertEqual(ui.page, "home")
        self.assertEqual(worker.submitted, [])
        self.assertEqual(self.store.read(), before)
        self.assertEqual((self.legacy / "token.json").stat(), token_before)

    def run_upgrade(self, config, *, login_required=False):
        backend = dashboard.Backend()
        statuses = (
            ["not_authenticated", "authenticated"]
            if login_required
            else ["authenticated"]
        )
        seen = []

        def status():
            seen.append(accounts.current_account())
            return {"state": statuses.pop(0), "message": "fixture"}

        backend.account_status = status
        backend.account_terminal = mock.Mock(
            side_effect=AssertionError("No provider request")
        )
        worker = dashboard.Worker(backend)
        self.addCleanup(worker.thread.join, 2)
        self.addCleanup(worker.close)
        interactive = []
        worker._provider_embedded = lambda cfg, *_: interactive.append(
            (cfg.account, accounts.current_account())
        )
        self.assertTrue(worker.submit("profile_migrate", config))
        events = []
        while True:
            try:
                event = worker.events.get(timeout=3)
            except queue.Empty:
                self.fail("Automatic upgrade did not finish")
            events.append(event)
            if event == ("done", "profile_migrate"):
                break
        self.assertFalse(any(kind == "error" for kind, _ in events), events)
        return events, seen, interactive

    def test_upgrade_reuses_login_and_retains_requested_session(self):
        self.populate()
        self.store.update(active="default")
        events, seen, interactive = self.run_upgrade(
            dashboard.Config(profile_name="primary", session="fixture-runtime")
        )
        self.assertEqual(seen, ["primary"])
        self.assertEqual(interactive, [])
        config = next(value for kind, value in events if kind == "config")
        self.assertEqual(config.account, "primary")
        self.assertEqual(config.session, "fixture-runtime")
        worker = LocalWorker()
        ui = wizard.Wizard(config, dashboard.Backend(), worker)
        self.assertNotEqual(ui.input_field, "existing_account_name")

    def test_upgrade_requests_authorization_when_migrated_login_is_missing(self):
        self.populate()
        _, seen, interactive = self.run_upgrade(
            dashboard.Config(profile_name="primary"), login_required=True
        )
        self.assertEqual(seen, ["primary", "primary"])
        self.assertEqual(interactive, [("primary", "primary")])

    def test_upgrade_preserves_a_previously_selected_other_account(self):
        self.populate()
        _, seen, interactive = self.run_upgrade(
            dashboard.Config(account="account-b", profile_name="primary")
        )
        self.assertEqual(self.store.read()["active"], "account-b")
        self.assertEqual(seen, ["account-b"])
        self.assertEqual(interactive, [])
