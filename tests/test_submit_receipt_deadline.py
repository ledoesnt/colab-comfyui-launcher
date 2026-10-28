"""Offline call-level regressions: actual colabctl, receipts and BudgetManager.

The official provider boundary is a fixture; no CLI/network/credentials/GPU.
Locks and processes are real, and all state lives in private /tmp directories.
"""

import contextlib
import io
import json
import multiprocessing as mp
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import budget_guard as bg
import colab_provider as provider_module
import colabctl as ctl
import operation_deadline as deadline
from client_requests import ClientRequests
from test_atomic_admission import API, initial

CTX = mp.get_context("fork")


class LiveClock:
    @property
    def t(self):
        return time.time()

    def __call__(self):
        return time.time()


def live_manager(folder, api=None):
    api = api or API()
    m = bg.BudgetManager(
        Path(folder) / "budgets",
        provider=api,
        clock=time.time,
        uptime=time.monotonic,
        boot_identity=lambda: "fixture",
    )
    m.ensure_guard = lambda _: None
    m._guard_alive = lambda _: True
    return m, api


def prepare(folder, work=3600):
    m, api = live_manager(folder)
    r = initial(m)
    now = time.time()
    r.update(
        allocated_at=now,
        submitted_at=now,
        clock_wall=now,
        clock_uptime=time.monotonic(),
        clock_boot_id="fixture",
        work_deadline=now + work,
        release_deadline=now + work + 60,
        usage_attempted_at=now,
        last_query_at=now - 31,
    )
    m._write(m._path("episode"), r)
    return m, api, r


@contextlib.contextmanager
def call_context(folder, m=None, api=None):
    m, api = (m, api) if m is not None else live_manager(folder)
    receipts = lambda session: ClientRequests(session, Path(folder) / "receipts")
    with (
        patch.object(ctl, "budget_manager", return_value=m),
        patch.object(ctl, "provider", return_value=api),
        patch.object(ctl, "ClientRequests", side_effect=receipts),
    ):
        yield m, api


def receipt_holder(folder, ready, unblock, intent=False):
    r = ClientRequests("episode", Path(folder) / "receipts")
    with r.lock("same", deadline=time.monotonic() + 4):
        if intent:
            r.begin(
                "same",
                {
                    "action": "start",
                    "session": "episode",
                    "request_id": "same",
                    "assignment_id": "owned",
                },
            )
        ready.set()
        if not unblock.wait(4):
            raise RuntimeError("fixture holder cleanup timed out")


def dispatch_holder(folder, ready, unblock):
    m, _ = live_manager(folder)
    with m._lock("episode", ".dispatch.lock"):
        ready.set()
        if not unblock.wait(4):
            raise RuntimeError("fixture holder cleanup timed out")


def submit_worker(folder, entered, done, output, timeout=0.8):
    with call_context(folder):
        sent = []
        with patch.object(
            ctl,
            "execute_remote",
            side_effect=lambda *a: sent.append(a) or {"ok": True, "status": "started"},
        ):
            entered.set()
            start = time.monotonic()
            try:
                r = ctl.submit_command(
                    types.SimpleNamespace(
                        session="episode", request_id="same", timeout=timeout
                    ),
                    {"action": "start"},
                )
            except ctl.BridgeError as e:
                r = e.result
            output.put(
                {"result": r, "elapsed": time.monotonic() - start, "sent": len(sent)}
            )
            done.set()


def release_worker(folder, done, output):
    m, api = live_manager(folder)
    start = time.monotonic()
    r = m.release("episode")
    output.put({"release": r, "elapsed": time.monotonic() - start, "stops": api.stops})
    done.set()


def local_transport_worker(folder, output):
    # Real OS child, deliberately hung. This is Python, NOT the Colab CLI.
    marker = Path(folder) / "transport_started"
    command = [
        sys.executable,
        "-B",
        "-c",
        "from pathlib import Path;import time;Path("
        + repr(str(marker))
        + ').write_text("fixture");time.sleep(3)',
    ]
    with (
        call_context(folder),
        patch.object(ctl.shutil, "which", return_value=sys.executable),
        patch.object(ctl, "colab_command", return_value=command),
    ):
        begin = time.monotonic()
        try:
            r = ctl.submit_command(
                types.SimpleNamespace(
                    session="episode", request_id="same", timeout=0.3
                ),
                {"action": "start"},
            )
        except ctl.BridgeError as e:
            r = e.result
        output.put({"result": r, "elapsed": time.monotonic() - begin})


class SubmissionChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="launcher-submit-chain-")
        self.folder = self.tmp.name
        self.m, self.api, self.original = prepare(self.folder)
        self.children = []

    def tearDown(self):
        for p in self.children:
            p.join(0.1)
            if p.is_alive():
                p.terminate()
                p.join(2)
        self.tmp.cleanup()

    def start_holder(self, function=receipt_holder, intent=False):
        ready = CTX.Event()
        unblock = CTX.Event()
        args = (
            (self.folder, ready, unblock, intent)
            if function is receipt_holder
            else (self.folder, ready, unblock)
        )
        p = CTX.Process(target=function, args=args)
        p.start()
        self.children.append(p)
        self.assertTrue(ready.wait(2))
        self.addCleanup(unblock.set)
        return p, unblock

    def args(self, timeout=0.8):
        return types.SimpleNamespace(
            session="episode", request_id="same", timeout=timeout
        )

    def test_actual_submit_receipt_wait_does_not_block_actual_release(self):
        holder, unlock = self.start_holder(intent=True)
        entered, done, output = CTX.Event(), CTX.Event(), CTX.Queue()
        start = CTX.Process(
            target=submit_worker, args=(self.folder, entered, done, output, 0.8)
        )
        start.start()
        self.children.append(start)
        self.assertTrue(entered.wait(2))
        time.sleep(0.08)
        released, release_output = CTX.Event(), CTX.Queue()
        rel = CTX.Process(
            target=release_worker, args=(self.folder, released, release_output)
        )
        rel.start()
        self.children.append(rel)
        self.assertTrue(released.wait(0.35), "release blocked behind receipt waiter")
        self.assertFalse(
            done.is_set(), "submission should still be waiting for receipt"
        )
        rr = release_output.get(timeout=1)
        self.assertEqual(rr["release"]["status"], "released")
        self.assertLess(rr["elapsed"], 0.3)
        self.assertEqual(rr["stops"], [("episode", "owned")])
        self.assertTrue(done.wait(2))
        result = output.get(timeout=1)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["result"]["status"], "receipt_wait_expired")
        self.assertFalse(result["result"]["terminal"])
        self.assertLess(result["elapsed"], 1.1)
        self.assertEqual(
            self.m._read(self.m._path("episode"))["release_deadline"],
            self.original["release_deadline"],
        )
        unlock.set()
        holder.join(2)
        start.join(2)
        rel.join(2)
        self.assertEqual([p.exitcode for p in (holder, start, rel)], [0, 0, 0])

    def test_busy_existing_receipt_is_not_rewritten_terminal_or_retried(self):
        holder, unlock = self.start_holder(intent=True)
        receipts = ClientRequests("episode", Path(self.folder) / "receipts")
        before = receipts.path("same").read_bytes()
        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "execute_remote") as send,
        ):
            t = time.monotonic()
            with self.assertRaises(ctl.BridgeError) as raised:
                ctl.submit_command(self.args(0.16), {"action": "start"})
            self.assertLess(time.monotonic() - t, 0.4)
            self.assertFalse(raised.exception.result["terminal"])
            self.assertFalse(raised.exception.result["resubmit_allowed"])
            send.assert_not_called()
        self.assertEqual(receipts.path("same").read_bytes(), before)
        unlock.set()
        holder.join(2)
        self.assertEqual(holder.exitcode, 0)

    def test_cli_busy_receipt_is_one_structured_nonterminal_error(self):
        holder, unlock = self.start_holder(intent=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "resolve_account", return_value="default"),
            patch.object(ctl, "execute_remote") as send,
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
        ):
            code = ctl.main(
                [
                    "-s",
                    "episode",
                    "--request-id",
                    "same",
                    "--timeout",
                    "0.12",
                    "start",
                    "--ephemeral",
                    "--local-only",
                ]
            )
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "receipt_wait_expired")
        self.assertEqual(result["request_id"], "same")
        self.assertFalse(result["terminal"])
        self.assertFalse(result["this_call_dispatched"])
        self.assertFalse(result["resubmit_allowed"])
        self.assertTrue(result["job_may_still_be_running"])
        send.assert_not_called()
        unlock.set()
        holder.join(2)
        self.assertEqual(holder.exitcode, 0)

    def test_real_local_transport_child_timeout_unblocks_release(self):
        output = CTX.Queue()
        submit = CTX.Process(target=local_transport_worker, args=(self.folder, output))
        submit.start()
        self.children.append(submit)
        marker = Path(self.folder) / "transport_started"
        until = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "local fixture subprocess never started")
        done, release_output = CTX.Event(), CTX.Queue()
        rel = CTX.Process(
            target=release_worker, args=(self.folder, done, release_output)
        )
        rel.start()
        self.children.append(rel)
        self.assertTrue(
            done.wait(0.65), "release remained blocked beyond fixed submit cutoff"
        )
        result = output.get(timeout=1)
        self.assertEqual(result["result"]["status"], "submission_unknown")
        self.assertLess(result["elapsed"], 0.6)
        rr = release_output.get(timeout=1)
        self.assertEqual(rr["release"]["status"], "released")
        self.assertLess(rr["elapsed"], 0.6)
        submit.join(2)
        rel.join(2)
        self.assertEqual([submit.exitcode, rel.exitcode], [0, 0])

    def test_receipt_delay_reduces_actual_send_timeout_and_preserves_not_after(self):
        # Actual call-layer receipt contention; no mocked dispatch lease.
        self.m, self.api, self.original = prepare(self.folder, work=4)
        holder, unlock = self.start_holder()
        import threading

        timer = threading.Timer(0.15, unlock.set)
        timer.start()
        self.addCleanup(timer.cancel)
        captured = []
        start = time.monotonic()
        wall = time.time()

        def send(session, payload, timeout):
            captured.append((payload, timeout, time.monotonic(), deadline.current()))
            return {"ok": True, "status": "started"}

        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "execute_remote", side_effect=send),
        ):
            result = ctl.submit_command(
                self.args(0.8), {"action": "prepare", "max_seconds": 3}
            )
        self.assertTrue(result["ok"])
        self.assertEqual(len(captured), 1)
        payload, timeout, sent, cutoff = captured[0]
        self.assertGreater(sent - start, 0.12)
        self.assertLess(timeout, 0.7)
        self.assertLessEqual(payload["not_after"], wall + 3.03)
        self.assertLess(payload["max_seconds"], 2.9)
        self.assertLessEqual(cutoff, start + 0.81)
        self.assertLessEqual(payload["not_after"], self.original["work_deadline"])
        holder.join(2)
        timer.join(2)
        self.assertEqual(holder.exitcode, 0)

    def test_dispatch_mutex_contention_is_bounded_at_real_colabctl_entry(self):
        holder, unlock = self.start_holder(dispatch_holder)
        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "execute_remote") as send,
        ):
            start = time.monotonic()
            with self.assertRaises(ctl.BridgeError) as raised:
                ctl.submit_command(self.args(0.17), {"action": "start"})
            self.assertLess(time.monotonic() - start, 0.4)
            self.assertEqual(raised.exception.result["status"], "not_submitted")
            send.assert_not_called()
        unlock.set()
        holder.join(2)
        self.assertEqual(holder.exitcode, 0)

    def test_final_scope_check_consumes_same_cutoff_no_late_send(self):
        # Delay only the final scope comparison after reconciliation/lease.
        count = 0

        def scope():
            nonlocal count
            count += 1
            if count == 3:
                time.sleep(0.2)
            return "fixed"

        self.api.account_scope = scope
        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "execute_remote") as send,
        ):
            with self.assertRaises(ctl.BridgeError):
                ctl.submit_command(self.args(0.12), {"action": "start"})
            send.assert_not_called()
        receipt = ClientRequests("episode", Path(self.folder) / "receipts").read("same")
        self.assertEqual(receipt["status"], "not_submitted")

    def test_existing_unknown_intent_queries_original_never_resubmits(self):
        r = ClientRequests("episode", Path(self.folder) / "receipts")
        with r.lock("same"):
            r.begin(
                "same",
                {
                    "action": "start",
                    "session": "episode",
                    "request_id": "same",
                    "assignment_id": "owned",
                },
            )
        calls = []

        def send(session, payload, timeout):
            calls.append(payload)
            return {"ok": True, "status": "running", "terminal": False}

        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "execute_remote", side_effect=send),
        ):
            result = ctl.submit_command(self.args(), {"action": "start"})
        self.assertEqual([p["action"] for p in calls], ["job"])
        self.assertEqual(calls[0]["assignment_id"], "owned")
        self.assertEqual(result["status"], "running")

    def test_existing_query_deadline_stays_unknown_and_receipt_unchanged(self):
        r = ClientRequests("episode", Path(self.folder) / "receipts")
        with r.lock("same"):
            r.begin(
                "same",
                {
                    "action": "start",
                    "session": "episode",
                    "request_id": "same",
                    "assignment_id": "owned",
                },
            )
        before = r.path("same").read_bytes()
        with (
            call_context(self.folder, self.m, self.api),
            patch.object(
                ctl,
                "execute_remote",
                side_effect=deadline.DeadlineExceeded("fixed cutoff"),
            ),
            self.assertRaises(ctl.BridgeError) as raised,
        ):
            ctl.submit_command(self.args(), {"action": "start"})
        self.assertEqual(raised.exception.result["status"], "query_unknown")
        self.assertFalse(raised.exception.result["terminal"])
        self.assertTrue(raised.exception.result["job_may_still_be_running"])
        self.assertEqual(before, r.path("same").read_bytes())

    def test_original_budget_deadline_not_shifted_by_final_scope_check(self):
        self.m, self.api, self.original = prepare(self.folder, work=2.1)
        count = 0

        def scope():
            nonlocal count
            count += 1
            if count == 3:
                time.sleep(0.1)
            return "fixed"

        self.api.account_scope = scope
        captured = []

        def send(session, payload, timeout):
            captured.append(payload)
            return {"ok": True, "status": "started"}

        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "execute_remote", side_effect=send),
        ):
            ctl.submit_command(self.args(), {"action": "start"})
        self.assertEqual(captured[0]["not_after"], self.original["work_deadline"])
        self.assertLess(captured[0]["work_seconds_remaining"], 2.05)

    def test_release_committed_while_receipt_waits_blocks_later_send(self):
        holder, unlock = self.start_holder()
        entered, done, output = CTX.Event(), CTX.Event(), CTX.Queue()
        start = CTX.Process(
            target=submit_worker, args=(self.folder, entered, done, output, 0.8)
        )
        start.start()
        self.children.append(start)
        self.assertTrue(entered.wait(2))
        time.sleep(0.06)
        self.assertEqual(self.m.release("episode")["status"], "released")
        unlock.set()
        self.assertTrue(done.wait(2))
        result = output.get(timeout=1)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["result"]["status"], "not_submitted")
        self.assertLess(result["elapsed"], 0.5)
        holder.join(2)
        start.join(2)
        self.assertEqual([holder.exitcode, start.exitcode], [0, 0])

    def test_mismatched_arguments_same_id_are_not_dispatched(self):
        r = ClientRequests("episode", Path(self.folder) / "receipts")
        with r.lock("same"):
            r.begin(
                "same",
                {
                    "action": "start",
                    "session": "episode",
                    "request_id": "same",
                    "assignment_id": "owned",
                },
            )
        with (
            call_context(self.folder, self.m, self.api),
            patch.object(ctl, "execute_remote") as send,
        ):
            with self.assertRaises(ValueError):
                ctl.submit_command(
                    self.args(), {"action": "prepare", "max_seconds": 10}
                )
            send.assert_not_called()

    def test_real_cli_transport_recomputes_remaining_after_program_preparation(self):
        recorded = []

        def run(args, **kw):
            recorded.append(kw["timeout"])
            return subprocess.CompletedProcess(
                args, 0, 'LAUNCHER_RESULT={"ok":true}\n', ""
            )

        def program(payload):
            time.sleep(0.07)
            return "# offline fixture"

        with (
            deadline.bounded(time.monotonic() + 0.25),
            patch.object(ctl.shutil, "which", return_value="/fixture/colab"),
            patch.object(ctl, "colab_command", side_effect=lambda args, **kw: args),
            patch.object(ctl, "remote_program", side_effect=program),
            patch.object(ctl.subprocess, "run", side_effect=run),
        ):
            self.assertTrue(
                ctl.execute_remote("episode", {"action": "start"}, 45)["ok"]
            )
        self.assertEqual(len(recorded), 1)
        self.assertLess(recorded[0], 0.2)

    def test_expired_transport_never_starts_cli(self):
        def program(payload):
            time.sleep(0.08)
            return "# offline fixture"

        with (
            deadline.bounded(time.monotonic() + 0.04),
            patch.object(ctl.shutil, "which", return_value="/fixture/colab"),
            patch.object(ctl, "colab_command", return_value=["fixture"]),
            patch.object(ctl, "remote_program", side_effect=program),
            patch.object(ctl.subprocess, "run") as run,
        ):
            with self.assertRaises(deadline.DeadlineExceeded):
                ctl.execute_remote("episode", {"action": "start"}, 45)
            run.assert_not_called()

    def test_provider_subprocess_gets_remaining_same_absolute_cutoff(self):
        captured = []
        api = provider_module.ColabProvider.__new__(provider_module.ColabProvider)
        api.account = "fixture"

        def run(args, **kw):
            captured.append(kw["timeout"])
            return subprocess.CompletedProcess(args, 0, "", "")

        api.run = run
        with (
            deadline.bounded(time.monotonic() + 0.2),
            patch.object(provider_module, "colab_command", return_value=["fixture"]),
        ):
            time.sleep(0.05)
            self.assertEqual(api._command(["sessions"], timeout=45), [])
        self.assertEqual(len(captured), 1)
        self.assertLess(captured[0], 0.17)

    def test_deploy_upload_and_exec_share_one_cutoff(self):
        captured = []

        def run(args, **kw):
            captured.append(kw["timeout"])
            if args[0] == "upload":
                time.sleep(0.08)
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(
                args, 0, 'LAUNCHER_RESULT={"ok":true}\n', ""
            )

        with (
            deadline.bounded(time.monotonic() + 0.3),
            patch.object(ctl, "build_archive", return_value="fixturehash"),
            patch.object(ctl.shutil, "which", return_value="/fixture/colab"),
            patch.object(ctl, "colab_command", side_effect=lambda args, **kw: args),
            patch.object(ctl.subprocess, "run", side_effect=run),
        ):
            self.assertTrue(ctl.deploy("episode", 45, {"action": "deploy"})["ok"])
        self.assertEqual(len(captured), 2)
        self.assertLess(captured[1], captured[0] - 0.05)


if __name__ == "__main__":
    unittest.main()
