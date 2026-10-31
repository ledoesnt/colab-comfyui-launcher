import json
import multiprocessing as mp
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import budget_guard as bg


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class API:
    def __init__(self):
        self.scope = "fixed"
        self.results = []
        self.calls = 0
        self.stops = []

    def account_scope(self):
        return self.scope

    def sessions(self):
        self.calls += 1
        x = (
            self.results.pop(0)
            if self.results
            else [{"name": "episode", "assignment_id": "owned", "hardware": "G4"}]
        )
        if isinstance(x, Exception):
            raise x
        return x

    def usage(self):
        return {
            "balance": 100.0,
            "rate_hourly": 8.9,
            "assignments_count": 1,
            "scope": "account",
        }

    def verified_release(self, session, assignment_id, expected_scope):
        if expected_scope != self.scope:
            return {"ok": False, "status": "release_unknown", "error": "scope mismatch"}
        self.stops.append((session, assignment_id))
        return {
            "ok": True,
            "verified": True,
            "status": "released",
            "assignment_id": assignment_id,
        }


def manager(folder, clock=None, api=None):
    c = clock or Clock()
    a = api or API()
    m = bg.BudgetManager(
        folder,
        provider=a,
        clock=c,
        uptime=lambda: c.t - 1000,
        boot_identity=lambda: "testboot",
    )
    m.ensure_guard = lambda _: None
    m._guard_alive = lambda _: True
    return m, c, a


def initial(m):
    r = {
        "schema": 1,
        "account": "default",
        "session": "episode",
        "job_id": "job",
        "request_id": "request",
        "assignment_id": "owned",
        "account_scope": "fixed",
        "hardware": "G4",
        "status": "active",
        "submitted_at": 1000.0,
        "allocated_at": 1000.0,
        "accounted_seconds": 0.0,
        "clock_wall": 1000.0,
        "clock_uptime": 0.0,
        "clock_boot_id": "testboot",
        "effective_rate_ceiling": 9.0,
        "rate_ceiling": 9.0,
        "limit_cu": 10.0,
        "reserve_cu": 0.3,
        "release_reserve_cu": 0.15,
        "work_deadline": 4880.0,
        "release_deadline": 4940.0,
        "estimated_exhaustion": 5000.0,
        "release_requested": False,
        "query_error": None,
        "query_generation": 1,
        "last_query_at": 900.0,
        "release_attempts": 0,
        "guard": {},
        "ownership": "created",
    }
    m._directory()
    m._write(m._path("episode"), r)
    return r


def lock_update_worker(folder, ready):
    m, _, _ = manager(folder)
    with m._lock("episode", ".reconcile.lock"):
        ready.set()
        time.sleep(0.15)
        m._mutate("episode", "job", lambda r: r.update(query_generation=2))


def dispatch_worker(folder, entered, proceed, output):
    m, _, _ = manager(folder)
    m.authorize = lambda *a: 30
    try:
        with m.dispatch_lease("episode", "start", 30):
            entered.set()
            assert proceed.wait(3)
            r = m._read(m._path("episode"))
            output.put(("sent", time.monotonic(), r["release_requested"]))
    except (bg.BudgetError, TimeoutError) as e:
        output.put(("blocked", time.monotonic(), type(e).__name__))


def release_worker(folder, entered, committed, output):
    m, _, _ = manager(folder)
    assert entered.wait(3)
    m._mutate(
        "episode",
        "job",
        lambda r: r.update(release_requested=True, status="release_pending"),
    )
    output.put(("release", time.monotonic(), True))
    committed.set()


class Regression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="launcher-admission-")
        self.m, self.c, self.api = manager(self.tmp.name)
        initial(self.m)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        return self.m._read(self.m._path("episode"))

    def test_query_failure_is_not_instance_absence(self):
        self.api.results = [bg.ProviderError("unlogged secret", code="timeout")]
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "query_failed")
        self.assertFalse(d["release_requested"])
        self.assertEqual(d["assignment_id"], "owned")
        self.assertFalse(self.api.stops)

    def test_authoritative_absence_is_distinct(self):
        self.api.results = [[]]
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "released")
        self.assertTrue(d["verified"])
        self.assertFalse(self.api.stops)

    def test_single_temporary_query_failure_recovers_same_instance(self):
        old = self.read()
        self.api.results = [bg.ProviderError("timeout", code="timeout")]
        self.m.tick("episode", force=True)
        first = self.read()
        self.c.t = 1002
        with self.assertRaises(bg.BudgetError):
            bg.new_work_state_barrier(self.m.status("episode"), "start")
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "active")
        self.assertFalse(d["release_requested"])
        self.assertEqual(d["assignment_id"], "owned")
        self.assertIsNone(self.read()["query_retry_until"])
        self.assertLessEqual(d["release_deadline"], old["release_deadline"])
        self.assertEqual(len(self.read()["query_failure_history"]), 1)
        self.assertFalse(self.api.stops)
        self.assertEqual(first["query_retry_until"], 1010)

    def test_continuous_failure_never_extends_retry_or_absolute_deadline(self):
        self.api.results = [
            bg.ProviderError("timeout", code="timeout") for _ in range(3)
        ]
        old = self.read()
        self.m.tick("episode", force=True)
        until = self.read()["query_retry_until"]
        self.c.t = 1005
        self.m.tick("episode", force=True)
        self.assertEqual(self.read()["query_retry_until"], until)
        self.c.t = 1010
        d = self.m.tick("episode", force=True)
        self.assertTrue(d["release_requested"])
        self.assertNotEqual(d["status"], "released")
        self.assertLessEqual(d["release_deadline"], old["release_deadline"])
        with self.assertRaises(bg.BudgetError):
            bg.new_work_state_barrier(d, "render")
        self.c.t = 1011
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "released")
        self.assertEqual(self.api.stops, [("episode", "owned")])

    def test_changed_scope_without_original_allocation_is_not_rebound(self):
        self.api.scope = "different"
        self.api.results = [[]]
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "release_unknown")
        self.assertEqual(self.read()["account_scope"], "fixed")
        self.assertFalse(self.api.stops)
        with self.assertRaises(bg.BudgetError):
            bg.new_work_state_barrier(d, "start")

    def test_wrong_profile_rejected_before_provider(self):
        r = self.read()
        r["account"] = "other_profile"
        self.m._write(self.m._path("episode"), r)
        with self.assertRaises(bg.BudgetError):
            self.m.status("episode")
        self.assertEqual(self.api.calls, 0)

    def test_released_state_never_resurrected(self):
        self.m._mutate(
            "episode",
            "job",
            lambda r: r.update(
                status="released", release_requested=True, release_verified_at=1000
            ),
        )
        self.m._observe(
            "episode",
            "job",
            [{"name": "episode", "assignment_id": "owned", "hardware": "G4"}],
            "fixed",
        )
        self.assertEqual(self.read()["status"], "released")
        with self.assertRaises(bg.BudgetError):
            bg.new_work_state_barrier(self.m.status("episode"), "start")

    def test_diagnostics_never_store_exception_secret_text(self):
        self.api.results = [
            bg.ProviderError("Bearer DO_NOT_STORE", code="command_failed", returncode=7)
        ]
        self.m.tick("episode", force=True)
        raw = json.dumps(self.read())
        self.assertNotIn("DO_NOT_STORE", raw)
        self.assertNotIn("Bearer", raw)
        self.assertEqual(
            self.read()["last_query_failure"]["provider_code"], "command_failed"
        )

    def test_real_cross_process_reconcile_lock_reuses_one_result(self):
        ctx = mp.get_context("fork")
        ready = ctx.Event()
        proc = ctx.Process(target=lock_update_worker, args=(self.tmp.name, ready))
        proc.start()
        self.assertTrue(ready.wait(2))
        allowed = self.m.authorize("episode", "render", 30)
        proc.join(3)
        self.assertEqual(proc.exitcode, 0)
        self.assertEqual(allowed, 30)
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.read()["query_generation"], 2)

    def test_real_cross_process_start_then_release_is_atomic(self):
        ctx = mp.get_context("fork")
        entered = ctx.Event()
        proceed = ctx.Event()
        committed = ctx.Event()
        out = ctx.Queue()
        start = ctx.Process(
            target=dispatch_worker, args=(self.tmp.name, entered, proceed, out)
        )
        release = ctx.Process(
            target=release_worker, args=(self.tmp.name, entered, committed, out)
        )
        start.start()
        self.assertTrue(entered.wait(2))
        release.start()
        time.sleep(0.1)
        self.assertFalse(committed.is_set())
        proceed.set()
        start.join(3)
        release.join(3)
        self.assertEqual(start.exitcode, 0)
        self.assertEqual(release.exitcode, 0)
        events = [out.get(timeout=1), out.get(timeout=1)]
        events.sort(key=lambda x: x[1])
        self.assertEqual(events[0][0], "sent")
        self.assertFalse(events[0][2])
        self.assertEqual(events[1][0], "release")
        self.assertTrue(self.read()["release_requested"])

    def test_release_first_prevents_any_dispatch(self):
        self.m._mutate(
            "episode",
            "job",
            lambda r: r.update(release_requested=True, status="release_pending"),
        )
        self.m.authorize = lambda *a: 30
        sent = []
        with (
            self.assertRaises(bg.BudgetError),
            self.m.dispatch_lease("episode", "start", 30),
        ):
            sent.append(True)
        self.assertFalse(sent)

    def test_dispatch_lease_reuses_one_fresh_verified_tuple(self):
        with self.m.dispatch_lease("episode", "start", 30) as lease:
            self.assertEqual(lease["allocation"]["name"], "episode")
            self.assertEqual(lease["allocation"]["assignment_id"], "owned")
            self.assertEqual(lease["allocation"]["account_scope"], "fixed")
            self.assertEqual(self.api.calls, 1)
            self.assertGreater(lease["allocation"]["query_generation"], 1)
        self.assertEqual(self.api.calls, 1)

    def test_deadline_still_prevents_dispatch(self):
        self.c.t = 4941
        self.m.authorize = lambda *a: 30
        with (
            self.assertRaises(bg.BudgetError),
            self.m.dispatch_lease("episode", "render", 30),
        ):
            self.fail("late dispatch")

    def test_identity_changed_after_preauthorization_rejected(self):
        def changed(*args):
            self.m._mutate(
                "episode", "job", lambda r: r.update(assignment_id="different")
            )
            return 30

        self.m.authorize = changed
        with (
            self.assertRaises(bg.BudgetError),
            self.m.dispatch_lease("episode", "start", 30),
        ):
            self.fail("rebound dispatch")

    def test_authorize_waits_for_one_shared_transient_recovery(self):
        self.api.results = [bg.ProviderError("temporary", code="timeout")]

        def recover():
            time.sleep(0.1)
            self.m.tick("episode", force=True)

        t = threading.Thread(target=recover)
        t.start()
        allowed = self.m.authorize("episode", "render", 30)
        t.join(2)
        self.assertEqual(allowed, 30)
        self.assertEqual(self.api.calls, 2)
        self.assertEqual(self.read()["status"], "active")
        self.assertFalse(self.read()["release_requested"])

    def test_query_retry_never_extends_original_budget_deadlines(self):
        old = self.read()
        self.api.results = [bg.ProviderError("temporary", code="timeout")]
        self.m.tick("episode", force=True)
        state = self.read()
        self.assertEqual(state["work_deadline"], old["work_deadline"])
        self.assertEqual(state["release_deadline"], old["release_deadline"])
        self.assertLessEqual(state["query_retry_until"], old["work_deadline"])

    def test_refresh_same_platform_allocation_authorizes_and_dispatches(self):
        self.api.scope = "refreshed-file-stat"
        original = self.read()
        with self.m.dispatch_lease("episode", "start", 30) as lease:
            self.assertEqual(lease["allocation"]["assignment_id"], "owned")
            self.assertEqual(lease["account_scope"], "refreshed-file-stat")
        self.assertEqual(self.read()["account"], "default")
        self.assertEqual(self.read()["work_deadline"], original["work_deadline"])
        self.assertEqual(self.read()["release_deadline"], original["release_deadline"])

    def test_changed_account_listing_cannot_rebind_to_same_named_replacement(self):
        self.api.scope = "another-login-file-stat"
        self.api.results = [
            [{"name": "episode", "assignment_id": "replacement", "hardware": "G4"}]
        ]
        with (
            self.assertRaises(bg.BudgetError),
            self.m.dispatch_lease("episode", "start", 30),
        ):
            self.fail("replacement dispatched")
        self.assertEqual(self.read()["assignment_id"], "owned")
        self.assertEqual(self.read()["account_scope"], "fixed")
        self.assertFalse(self.api.stops)

    def test_original_assignment_renamed_is_not_a_refresh_recovery(self):
        self.api.scope = "refreshed-file-stat"
        self.api.results = [
            [{"name": "other-session", "assignment_id": "owned", "hardware": "G4"}]
        ]
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "identity_mismatch")
        self.assertEqual(self.read()["account_scope"], "fixed")
        self.assertFalse(self.api.stops)

    def test_file_refresh_during_query_requires_stable_reverification(self):
        scopes = iter(["fixed", "refreshed-file-stat"])
        self.api.account_scope = lambda: next(scopes)
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "query_failed")
        self.assertFalse(d["release_requested"])
        self.assertEqual(self.read()["account_scope"], "fixed")
        self.api.account_scope = lambda: "refreshed-file-stat"
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "active")
        self.assertEqual(self.read()["account_scope"], "refreshed-file-stat")
        self.assertEqual(self.read()["assignment_id"], "owned")

    def test_stable_refresh_cannot_claim_absence_as_verified_release(self):
        self.api.scope = "another-login-file-stat"
        self.api.results = [[]]
        d = self.m.tick("episode", force=True)
        self.assertEqual(d["status"], "release_unknown")
        self.assertFalse(d["verified"])
        self.assertFalse(self.api.stops)

    def test_guard_recovers_query_failure_without_waiting_normal_poll_interval(self):
        self.m._mutate("episode", "job", lambda r: r.update(last_query_at=1000.0))
        self.api.results = [bg.ProviderError("temporary", code="timeout")]
        self.m.tick("episode", force=True)
        self.c.t = 1002
        d = self.m.tick("episode")
        self.assertEqual(d["status"], "active")
        self.assertEqual(self.api.calls, 2)
        self.assertFalse(d["release_requested"])

    def test_real_file_metadata_refresh_requires_same_official_assignment(self):
        credential_fixture = Path(self.tmp.name) / "synthetic-cache.json"
        credential_fixture.write_text("synthetic fixture v1")
        probe = bg.ColabProvider(oauth_cache=credential_fixture)
        self.api.account_scope = probe.account_scope
        first = probe.account_scope()
        self.m._mutate("episode", "job", lambda r: r.update(account_scope=first))
        credential_fixture.write_text("synthetic fixture v2 changed size")
        self.assertNotEqual(probe.account_scope(), first)
        with self.m.dispatch_lease("episode", "start", 30) as lease:
            self.assertEqual(lease["allocation"]["assignment_id"], "owned")
            self.assertEqual(lease["account_scope"], probe.account_scope())


if __name__ == "__main__":
    unittest.main()
