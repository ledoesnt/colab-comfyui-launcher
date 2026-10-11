"""Offline balance parsing, low-frequency polling, and header rendering."""

import subprocess
import threading
import unittest
from unittest import mock

from test_wizard import Frame, LocalWorker, dashboard, wizard


class AccountUsageTests(unittest.TestCase):
    def test_official_cli_balance_and_rate_are_parsed_noninteractively(self):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                "Current balance: 125.50 compute units\n"
                "Usage rate: 10.00/hr\nActive assignments: 2\n",
                "",
            )
        )
        self.assertEqual(
            dashboard.Backend(run).account_usage(),
            {"ok": True, "balance": 125.5, "rate_hourly": 10.0},
        )
        run.assert_called_once_with(
            ["colab", "--auth=oauth2", "usage"],
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )

    def test_unverified_output_and_failures_do_not_expose_provider_output(self):
        for output in (
            "login required https://fixture.invalid/private",
            "Current balance: NaN compute units\nUsage rate: 2.00/hr",
            "Current balance: 25.00 compute units",
            "Current balance: 25.00 compute units\nUsage rate: -1.00/hr",
        ):
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, output, ""))
            self.assertEqual(dashboard.Backend(run).account_usage(), {"ok": False})
        for failure in (
            OSError("private diagnostic"),
            subprocess.TimeoutExpired("colab", 15),
        ):
            self.assertEqual(
                dashboard.Backend(mock.Mock(side_effect=failure)).account_usage(),
                {"ok": False},
            )
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "private"))
        self.assertEqual(dashboard.Backend(run).account_usage(), {"ok": False})


class UsageWizardTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)
        self.ui.account = {"state": "authenticated"}

    def emit(self, kind, value):
        self.worker.events.put((kind, value))
        self.ui._events()

    def test_balance_polling_is_limited_to_five_minutes_even_after_failure(self):
        for now in (0, 1, 10, 299):
            with mock.patch.object(wizard.time, "monotonic", return_value=now):
                self.ui._refresh()
            self.emit("usage", {"ok": False})
            self.emit("done", "usage")
        self.assertEqual([action for action, _ in self.worker.submitted], ["usage"])
        self.assertEqual(self.ui.page, "home")
        self.assertEqual(self.ui.error, "")
        with mock.patch.object(wizard.time, "monotonic", return_value=300):
            self.ui._refresh()
        self.assertEqual([action for action, _ in self.worker.submitted], ["usage"] * 2)

    def test_no_requests_before_login_during_work_or_in_demo(self):
        for state in ("unknown", "checking", "not_authenticated", "unavailable"):
            self.ui.account = {"state": state}
            self.ui._refresh()
        self.ui.account = {"state": "authenticated"}
        self.worker.busy = True
        self.ui._refresh()
        self.worker.busy = False
        self.ui.demo = True
        self.ui._refresh()
        self.assertEqual(self.worker.submitted, [])

    def test_failed_refresh_retains_cached_balance_and_marks_it_stale(self):
        self.emit("usage", {"ok": True, "balance": 100.0, "rate_hourly": 8.0})
        self.assertIn("100.00 CU · ~12h 30m", self.ui._header())
        self.emit("usage", {"ok": False})
        self.assertIn("100.00 CU · ~12h 30m · stale", self.ui._header())
        self.assertEqual(self.ui.page, "home")

    def test_account_changes_clear_balance_and_ignore_late_usage(self):
        self.emit("usage", {"ok": True, "balance": 100.0, "rate_hourly": 8.0})
        self.emit("account_signed_out", {"state": "not_authenticated"})
        self.emit("usage", {"ok": True, "balance": 999.0, "rate_hourly": 1.0})
        self.assertEqual(self.ui.usage, {})
        self.assertNotIn("CU", self.ui._header())
        self.emit("account", {"state": "authenticated"})
        self.ui._refresh()
        self.assertEqual(self.worker.submitted[-1][0], "usage")

    def test_header_fits_small_terminal_and_plain_mode(self):
        self.emit("usage", {"ok": True, "balance": 100.0, "rate_hourly": 8.0})
        for height, width in ((24, 50), (24, 80), (36, 120)):
            screen = Frame(height, width)
            with mock.patch.object(wizard.curses, "doupdate"):
                self.ui._draw(screen)
            self.assertIn("100.00 CU · ~12h 30m", screen.text.splitlines()[0])
        self.assertIn("100.00 CU · ~12h 30m", self.ui._plain_snapshot().splitlines()[0])
        self.emit("usage", {"ok": True, "balance": 100.0, "rate_hourly": 0.0})
        self.assertIn("100.00 CU · idle", self.ui._header())

    def test_usage_completion_does_not_change_pipeline_or_authorization(self):
        self.ui.page = "pipeline"
        self.ui.auth = {"waiting": True}
        self.emit("done", "usage")
        self.assertEqual(self.ui.page, "pipeline")
        self.assertEqual(self.ui.auth, {"waiting": True})


class UsageWorkerTests(unittest.TestCase):
    def test_read_only_poll_allows_user_action_to_queue_and_isolates_errors(self):
        started, finish = threading.Event(), threading.Event()
        backend = mock.Mock()

        def usage():
            started.set()
            finish.wait(2)
            raise OSError("private diagnostic")

        backend.account_usage.side_effect = usage
        backend.account_status.return_value = {"state": "authenticated"}
        worker = dashboard.Worker(backend)
        try:
            self.assertTrue(worker.submit("usage", dashboard.Config()))
            self.assertTrue(started.wait(1))
            self.assertFalse(worker.submit("usage", dashboard.Config()))
            self.assertTrue(worker.submit("account_status", dashboard.Config()))
            finish.set()
            worker.tasks.join()
            events = list(worker.events.queue)
            self.assertIn(("usage", {"ok": False}), events)
            self.assertIn(("account", {"state": "authenticated"}), events)
            self.assertFalse(any(kind == "error" for kind, _ in events))
            backend.bridge.assert_not_called()
        finally:
            finish.set()
            worker.close()
            worker.thread.join(1)


if __name__ == "__main__":
    unittest.main()
