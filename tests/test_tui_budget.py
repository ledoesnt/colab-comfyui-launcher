"""Offline budget forms, guard continuity and verified-release UI contracts."""

import json
import subprocess
import unittest
from dataclasses import replace
from unittest import mock

from test_wizard import Frame, LocalWorker, dashboard, wizard


def budget_fixture():
    return {
        "ok": True,
        "managed": True,
        "session": "existing-t4",
        "assignment_id": "fixture-assignment",
        "status": "active",
        "limit_cu": 10.0,
        "rate_ceiling": 4.0,
        "reserve_cu": 1.0,
        "work_deadline": 1_800_000_000,
        "release_deadline": 1_800_000_100,
        "hold_deadline": None,
        "estimate": {"consumed_cu": 2.5},
        "observed_account": {"balance": 80.0, "stale": True},
        "guard": {"running": True},
    }


class BudgetBackendTests(unittest.TestCase):
    def test_unknown_or_invalid_rate_prevents_any_allocation_command(self):
        run = mock.Mock()
        backend = dashboard.Backend(run)
        for rate in (None, 0, -1, float("inf"), float("nan")):
            with self.subTest(rate=rate), self.assertRaises(dashboard.DashboardError):
                backend.create(
                    dashboard.Config(session="fixture", cu_limit=10, rate_ceiling=rate)
                )
        run.assert_not_called()

    def test_managed_create_keeps_gpu_and_explicit_budget_in_cli(self):
        provider = {
            "name": "new-t4",
            "hardware": "T4",
            "assignment_id": "fixture-assignment",
        }
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"ok": True, "provider": provider}), ""
            )
        )
        result = dashboard.Backend(run).create(
            dashboard.Config(
                session="new-t4", gpu="T4", cu_limit=10, rate_ceiling=4, reserve_cu=1
            )
        )
        argv = run.call_args.args[0]
        self.assertEqual(result, provider)
        self.assertEqual(argv[argv.index("--gpu") + 1], "T4")
        self.assertEqual(argv[argv.index("--cu-limit") + 1], "10")
        self.assertEqual(argv[argv.index("--rate-ceiling") + 1], "4")
        self.assertEqual(argv[argv.index("--reserve-cu") + 1], "1")
        self.assertEqual(run.call_args.kwargs["timeout"], 360)

    def test_prepare_modes_and_hold_use_shared_cli_semantics(self):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', "")
        )
        backend = dashboard.Backend(run)
        config = dashboard.Config(session="existing", hold_seconds=120)
        for download in (False, True):
            backend.bridge(config, "prepare", download_missing=download)
            argv = run.call_args.args[0]
            self.assertEqual(
                argv[argv.index("--mode") + 1],
                "download_missing" if download else "verify_only",
            )
            self.assertEqual("--download-missing" in argv, download)
        backend.bridge(config, "hold")
        self.assertEqual(run.call_args.args[0][-3:], ["hold", "--seconds", "120"])

    def test_inspect_restores_budget_without_calling_create_or_reset(self):
        backend = dashboard.Backend()
        status = {"ok": True, "budget": budget_fixture(), "configuration": None}
        with (
            mock.patch.object(
                backend,
                "official",
                return_value="[existing-t4] fixture | Hardware: T4 | Shape: Standard | Variant: GPU",
            ),
            mock.patch.object(backend, "bridge", return_value=status) as bridge,
            mock.patch.object(
                backend, "ssh", return_value={"found": False, "running": False}
            ),
        ):
            inspected = backend.inspect(
                dashboard.Config(session="existing-t4", gpu="G4", cu_limit=999)
            )
        self.assertEqual(inspected["config"].gpu, "T4")
        self.assertEqual(inspected["config"].cu_limit, 10)
        self.assertEqual(inspected["status"]["budget"]["estimate"]["consumed_cu"], 2.5)
        self.assertEqual([call.args[1] for call in bridge.call_args_list], ["status"])

    def test_account_changes_are_blocked_before_credentials_or_provider_access(self):
        run = mock.Mock()
        with (
            mock.patch.object(
                dashboard, "active_managed_budgets", return_value=[budget_fixture()]
            ),
            mock.patch.object(dashboard.Path, "home") as home,
            self.assertRaisesRegex(dashboard.DashboardError, "Release and verify"),
        ):
            dashboard.Backend(run).account_logout()
        run.assert_not_called()
        home.assert_not_called()

    def test_unmanaged_snapshot_clears_only_display_plan(self):
        config = dashboard.Config(
            session="other", cu_limit=99, rate_ceiling=8, reserve_cu=2
        )
        restored = dashboard.restore_budget_config(
            config, {"budget": {"managed": False}}
        )
        self.assertIsNone(restored.cu_limit)
        self.assertIsNone(restored.rate_ceiling)
        self.assertEqual(restored.session, "other")


class BudgetWizardTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(
            dashboard.Config(cpu=True, access="public"), object(), self.worker
        )
        self.ui.account = {"state": "authenticated"}

    def choose(self, key):
        return self.ui._activate(
            next(choice for choice in self.ui._choices() if choice.key == key)
        )

    def test_summary_configures_budget_without_starting_any_command(self):
        self.ui._page("summary")
        self.choose("budget")
        for field, value in (
            ("cu_limit", "10"),
            ("rate_ceiling", "4"),
            ("reserve_cu", "1"),
        ):
            self.choose(field)
            self.ui.input_value = value
            self.ui._confirm_input()
            self.assertEqual(self.ui.page, "budget")
        self.assertEqual(
            (
                self.ui.config.cu_limit,
                self.ui.config.rate_ceiling,
                self.ui.config.reserve_cu,
            ),
            (10, 4, 1),
        )
        self.assertEqual(self.worker.submitted, [])
        self.choose("back")
        self.assertEqual(self.ui.page, "summary")

    def test_unknown_rate_blocks_begin_before_key_or_allocation(self):
        self.ui.config.cu_limit = 10
        self.ui._page("summary")
        self.ui._begin()
        self.assertIn("rate", self.ui.error)
        self.assertEqual(self.worker.submitted, [])
        self.assertEqual(self.ui.page, "summary")

    def test_new_allocation_requires_its_own_budget_selection(self):
        self.ui.config = replace(
            self.ui.config,
            session="previous",
            cu_limit=10,
            rate_ceiling=4,
            reserve_cu=1,
        )
        self.choose("new")
        self.assertIsNone(self.ui.config.cu_limit)
        self.assertIsNone(self.ui.config.rate_ceiling)
        self.assertIsNone(self.ui.config.reserve_cu)
        self.assertEqual(self.ui.config.session, "")

    def test_first_new_keeps_explicit_cli_budget_plan(self):
        self.ui.config = replace(
            self.ui.config, cu_limit=10, rate_ceiling=4, reserve_cu=1
        )
        self.choose("new")
        self.assertEqual(self.ui.config.cu_limit, 10)

    def test_reconnect_restores_display_and_does_not_reset_budget(self):
        self.ui.config.session = "existing-t4"
        self.ui.resume = True
        original = budget_fixture()
        self.worker.events.put(("status", {"budget": original}))
        self.ui._events()
        self.assertEqual(self.ui.config.cu_limit, 10)
        self.assertEqual(original["work_deadline"], 1_800_000_000)
        self.assertEqual(original["estimate"]["consumed_cu"], 2.5)
        self.ui._page("budget")
        self.assertEqual([choice.key for choice in self.ui._choices()], ["back"])
        self.assertEqual(self.worker.submitted, [])

    def test_hold_is_explicit_finite_and_only_available_for_managed_instance(self):
        self.ui._page("ready")
        self.assertNotIn("hold", [choice.key for choice in self.ui._choices()])
        self.ui.status["budget"] = budget_fixture()
        self.ui.config.session = "existing-t4"
        self.choose("hold")
        for value in ("", "nan", "inf", "-1", "0"):
            self.ui.input_value = value
            self.ui._confirm_input()
            self.assertEqual(self.worker.submitted, [])
        self.ui.input_value = "2.5"
        self.ui._confirm_input()
        self.assertEqual(self.worker.submitted[-1][0], "hold")
        self.assertEqual(self.worker.submitted[-1][1].hold_seconds, 150)
        self.assertEqual(self.ui.status["budget"]["work_deadline"], 1_800_000_000)

    def test_budget_display_distinguishes_estimate_and_observed_account(self):
        lines = dashboard.budget_lines(self.ui.config, {"budget": budget_fixture()})
        text = "\n".join(lines)
        self.assertIn("Conservative estimate: 2.500 / 10 CU", text)
        self.assertIn("Observed account balance: 80.00 CU · stale", text)
        self.assertIn("Work protection:", text)
        self.assertIn("controller offline/shutdown", text)

    def test_budget_page_renders_at_supported_terminal_sizes(self):
        self.ui._page("budget")
        self.ui.config = replace(self.ui.config, cu_limit=10, rate_ceiling=4)
        for height, width in ((24, 50), (24, 80), (36, 120)):
            with (
                self.subTest(size=(height, width)),
                mock.patch.object(wizard.curses, "doupdate"),
            ):
                screen = Frame(height, width)
                self.ui._draw(screen)
                self.assertIn("CU", screen.text)

    def test_quit_does_not_stop_guard_or_resources(self):
        self.ui._page("ready")
        self.ui.status["budget"] = budget_fixture()
        self.assertFalse(self.choose("quit"))
        self.assertTrue(self.ui.quitting)
        self.assertEqual(self.worker.submitted, [])


if __name__ == "__main__":
    unittest.main()
