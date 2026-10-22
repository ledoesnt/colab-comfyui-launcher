"""Offline fault injection for allocation budgets; no real CLI or credentials."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import budget_guard as budget
from colab_provider import ProviderError


class Clock:
    def __init__(self):
        self.wall, self.up, self.boot = 1_000_000.0, 10.0, "test-boot"

    def advance(self, seconds):
        self.wall += seconds
        self.up += seconds


class Provider:
    def __init__(self):
        self.rows = []
        self.created = []
        self.released = []
        self.scope = "test-account-stat"
        self.query_failure = False
        self.usage_failure = False
        self.create_failure = False
        self.create_visible = True
        self.release_result = {"ok": True, "status": "released"}
        self.rate = 2.0

    def sessions(self):
        if self.query_failure:
            raise ProviderError("fixture offline")
        return list(self.rows)

    def account_scope(self):
        return self.scope

    def create(self, session, gpu=None):
        self.created.append((session, gpu))
        if self.create_visible:
            self.rows.append(
                {
                    "name": session,
                    "assignment_id": "allocation-" + str(len(self.created)),
                    "hardware": gpu or "CPU",
                    "shape": "Standard",
                    "variant": "GPU" if gpu else "DEFAULT",
                }
            )
        if self.create_failure:
            raise ProviderError("fixture lost response", outcome_unknown=True)

    def verified_release(self, session, assignment_id, *, expected_scope=None):
        if expected_scope is not None and expected_scope != self.scope:
            return {"ok": False, "status": "release_unknown"}
        self.released.append((session, assignment_id))
        if self.release_result["ok"]:
            self.rows = [
                row for row in self.rows if row["assignment_id"] != assignment_id
            ]
        return dict(self.release_result)

    def usage(self):
        if self.usage_failure:
            raise ProviderError("fixture usage unavailable")
        return {
            "balance": 100.0,
            "rate_hourly": self.rate,
            "assignments_count": len(self.rows),
            "scope": "account",
        }


class BudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "budgets"
        self.clock = Clock()
        self.provider = Provider()
        self.manager = self.make_manager()

    def make_manager(self):
        manager = budget.BudgetManager(
            self.root,
            self.provider,
            clock=lambda: self.clock.wall,
            uptime=lambda: self.clock.up,
            boot_identity=lambda: self.clock.boot,
        )
        manager.ensure_guard = mock.Mock()
        manager._guard_alive = mock.Mock(return_value=True)
        return manager

    def create(self, **kwargs):
        return self.manager.create(
            "session-test", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2, **kwargs
        )

    def tick(self):
        return self.manager.tick("session-test", force=True)

    def test_creation_binds_allocation_and_counts_every_phase(self):
        result = self.create(request_id="request-test")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["assignment_id"], "allocation-1")
        self.assertEqual(result["hardware"], "T4")
        self.assertEqual(result["actual_hardware"], "T4")
        self.assertEqual(result["request_id"], "request-test")
        self.assertEqual(result["work_deadline"], self.clock.wall + 7200)
        self.assertEqual(result["release_deadline"], self.clock.wall + 8100)
        self.clock.advance(900)
        result = self.tick()
        self.assertEqual(result["estimate"]["consumed_cu"], 1)
        self.assertIn("idle", result["estimate"]["includes"])
        self.assertIsNone(result["estimate"]["actual_instance_cu"])
        self.assertEqual(result["observed_account"]["scope"], "account")

    def test_guard_must_be_ready_before_new(self):
        self.manager.ensure_guard.side_effect = budget.BudgetError(
            "fixture startup failure"
        )
        with self.assertRaises(budget.BudgetError):
            self.create()
        self.assertEqual(self.provider.created, [])
        self.assertEqual(
            self.manager.status("session-test")["status"], "allocation_cancelled"
        )

    def test_guard_dying_between_handshake_and_submit_blocks_new(self):
        self.manager._guard_alive.return_value = False
        with self.assertRaises(budget.BudgetError):
            self.create()
        self.assertEqual(self.provider.created, [])

    def test_release_during_guard_handshake_cancels_before_submission(self):
        self.manager.ensure_guard.side_effect = lambda session: self.manager.release(
            session
        )
        with self.assertRaises(budget.BudgetError):
            self.create()
        self.assertEqual(self.provider.created, [])
        self.assertEqual(
            self.manager.status("session-test")["status"], "allocation_cancelled"
        )

    def test_unknown_rate_and_invalid_reserves_block_before_provider(self):
        for rate in (None, 0, -1, float("nan"), float("inf"), True):
            with self.subTest(rate=rate), self.assertRaises(budget.BudgetError):
                self.manager.create("test", "T4", limit_cu=10, rate_ceiling=rate)
        for reserve in (0, -1, 10, 11):
            with self.subTest(reserve=reserve), self.assertRaises(budget.BudgetError):
                self.manager.create(
                    "test", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=reserve
                )
        self.assertEqual(self.provider.created, [])

    def test_reconnecting_and_repeated_create_do_not_reset_budget(self):
        initial = self.create()
        self.clock.advance(1800)
        replacement = self.make_manager()
        result = replacement.create(
            "session-test", "G4", limit_cu=500, rate_ceiling=1, reserve_cu=1
        )
        self.assertEqual(len(self.provider.created), 1)
        self.assertEqual(result["job_id"], initial["job_id"])
        self.assertEqual(result["request_id"], initial["request_id"])
        self.assertEqual(result["allocated_at"], initial["allocated_at"])
        self.assertEqual(result["limit_cu"], 10)
        self.assertEqual(result["estimate"]["consumed_cu"], 2)
        self.assertTrue(result["already_managed"])

    def test_same_request_id_cannot_claim_different_budget_parameters(self):
        self.create(request_id="same-request")
        for changes in (
            {"gpu": "L4"},
            {"limit_cu": 20},
            {"rate_ceiling": 8},
            {"reserve_cu": 3},
        ):
            values = {
                "gpu": "T4",
                "limit_cu": 10,
                "rate_ceiling": 4,
                "reserve_cu": 2,
                "request_id": "same-request",
                **changes,
            }
            with self.subTest(changes=changes), self.assertRaises(budget.BudgetError):
                self.manager.create("session-test", **values)
        self.assertEqual(len(self.provider.created), 1)

    def test_archived_request_id_replays_original_allocation(self):
        first = self.create(request_id="original-request")
        self.manager.release("session-test")
        second = self.create(request_id="new-request")
        again = self.create(request_id="original-request")
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertNotEqual(again["job_id"], second["job_id"])
        self.assertEqual(again["status"], "released")
        self.assertEqual(len(self.provider.created), 2)

    def test_history_rotation_after_preflight_cannot_duplicate_old_request(self):
        first = self.create(request_id="original-request")
        self.manager.release("session-test")
        original = self.manager._existing_create_record

        def rotate(session, request_id):
            self.manager._existing_create_record = original
            self.create(request_id="other-request")
            self.manager.release("session-test")
            # Inject the stale preflight miss from before the history rotation.

        self.manager._existing_create_record = rotate
        result = self.create(request_id="original-request")
        self.assertEqual(result["job_id"], first["job_id"])
        self.assertEqual(result["status"], "released")
        self.assertEqual(len(self.provider.created), 2)

    def test_unknown_create_never_reposts_even_after_client_restart(self):
        self.provider.create_failure = True
        self.provider.create_visible = False
        first = self.create()
        self.assertEqual(first["status"], "allocation_unknown")
        replacement = self.make_manager()
        replacement.create(
            "session-test", "T4", limit_cu=10, rate_ceiling=4, reserve_cu=2
        )
        self.assertEqual(len(self.provider.created), 1)
        self.assertEqual(replacement.status("session-test")["job_id"], first["job_id"])
        self.provider.rows.append(
            {
                "name": "session-test",
                "assignment_id": "later-allocation",
                "hardware": "T4",
            }
        )
        recovered = replacement.tick("session-test", force=True)
        self.assertEqual(recovered["assignment_id"], "later-allocation")
        self.assertEqual(recovered["status"], "active")

    def test_lost_create_response_with_visible_assignment_is_reconciled(self):
        self.provider.create_failure = True
        result = self.create()
        self.assertEqual(result["status"], "active")
        self.assertEqual(len(self.provider.created), 1)

    def test_allocation_failure_persists_only_safe_classification_after_restart(self):
        diagnostic = ProviderError(
            "provider stderr: Bearer PRIVATE-FIXTURE token=PRIVATE-FIXTURE",
            code="command_failed",
            returncode=17,
            outcome_unknown=True,
        )
        with mock.patch.object(
            self.provider, "create", side_effect=diagnostic
        ) as create:
            result = self.create(request_id="failed-allocation")
            self.assertEqual(result["status"], "allocation_unknown")
            recovered = self.make_manager().create(
                "session-test",
                "T4",
                limit_cu=10,
                rate_ceiling=4,
                reserve_cu=2,
                request_id="failed-allocation",
            )
        create.assert_called_once()
        stored = json.loads(self.manager._path("session-test").read_text())
        for record in (result, recovered, stored):
            self.assertEqual(record["allocation_error_code"], "command_failed")
            self.assertEqual(record["allocation_error_returncode"], 17)
            self.assertNotIn("PRIVATE-FIXTURE", json.dumps(record))
            self.assertNotIn("provider stderr", json.dumps(record))
        self.assertEqual(recovered["job_id"], result["job_id"])

    def test_explicit_adopt_recovers_unbound_request_without_resetting_budget(self):
        self.provider.create_visible = False
        first = self.create()
        self.clock.advance(900)
        self.provider.scope = "refreshed-after-unknown"
        self.provider.rows = [
            {"name": "session-test", "assignment_id": "recovered-id", "hardware": "T4"}
        ]
        self.assertEqual(self.tick()["status"], "allocation_unknown")
        recovered = self.manager.adopt(
            "session-test",
            "recovered-id",
            limit_cu=500,
            rate_ceiling=1,
            reserve_cu=1,
            allocated_at=self.clock.wall,
            confirm=True,
        )
        self.assertEqual(recovered["job_id"], first["job_id"])
        self.assertEqual(recovered["request_id"], first["request_id"])
        self.assertEqual(recovered["allocated_at"], first["allocated_at"])
        self.assertEqual(recovered["limit_cu"], 10)
        self.assertEqual(recovered["estimate"]["consumed_cu"], 1)
        self.assertEqual(recovered["assignment_id"], "recovered-id")

    def test_existing_session_requires_explicit_adoption(self):
        self.provider.rows = [
            {"name": "session-test", "assignment_id": "existing", "hardware": "T4"}
        ]
        with self.assertRaises(budget.BudgetError):
            self.create()
        self.assertEqual(self.provider.created, [])
        self.assertFalse(self.root.exists())

    def test_adoption_requires_exact_identity_and_original_time(self):
        self.provider.rows = [
            {"name": "session-test", "assignment_id": "existing", "hardware": "T4"}
        ]
        common = {
            "limit_cu": 10,
            "rate_ceiling": 4,
            "reserve_cu": 2,
            "allocated_at": self.clock.wall - 900,
        }
        with self.assertRaises(budget.BudgetError):
            self.manager.adopt("session-test", "existing", **common)
        with self.assertRaises(budget.BudgetError):
            self.manager.adopt("session-test", "different", confirm=True, **common)
        result = self.manager.adopt("session-test", "existing", confirm=True, **common)
        self.assertEqual(result["ownership"], "adopted")
        self.assertEqual(result["estimate"]["consumed_cu"], 1)
        again = self.manager.adopt(
            "session-test",
            "existing",
            confirm=True,
            **dict(common, limit_cu=20, allocated_at=self.clock.wall),
        )
        self.assertEqual(again["job_id"], result["job_id"])
        self.assertEqual(again["limit_cu"], 10)

    def test_release_requires_platform_confirmation_and_retries(self):
        self.create()
        self.provider.release_result = {"ok": False, "status": "release_pending"}
        first = self.manager.release("session-test")
        self.assertFalse(first["ok"])
        self.assertEqual(first["status"], "release_pending")
        self.assertNotIn("release_verified_at", first)
        self.provider.release_result = {"ok": True, "status": "released"}
        second = self.tick()
        self.assertEqual(second["status"], "released")
        self.assertIn("release_verified_at", second)
        self.assertEqual(len(self.provider.released), 2)

    def test_release_uses_locked_submission_state_not_stale_preflight(self):
        active = self.create()
        original_read = self.manager._read
        stale = dict(
            original_read(self.manager._path("session-test")),
            status="planned",
            submitted_at=None,
            assignment_id=None,
        )
        reads = 0

        def read(path):
            nonlocal reads
            reads += 1
            return stale if reads == 1 else original_read(path)

        self.manager._read = read
        result = self.manager.release("session-test")
        self.assertEqual(result["status"], "released")
        self.assertEqual(
            self.provider.released, [("session-test", active["assignment_id"])]
        )

    def test_release_response_lost_then_absent_is_verified_without_resending(self):
        self.create()
        self.provider.release_result = {"ok": False, "status": "release_unknown"}
        self.manager.release("session-test")
        self.provider.rows = []
        result = self.tick()
        self.assertEqual(result["status"], "released")
        self.assertEqual(len(self.provider.released), 1)

    def test_same_name_replacement_never_gets_stopped(self):
        self.create()
        self.provider.rows = [
            {"name": "session-test", "assignment_id": "replacement", "hardware": "G4"}
        ]
        result = self.manager.release("session-test")
        self.assertEqual(result["assignment_id"], "allocation-1")
        self.assertEqual(result["status"], "released")
        self.assertEqual(self.provider.released, [])

    def test_orphan_owned_assignment_is_not_absence_or_permission_to_stop(self):
        self.create()
        self.provider.rows[0]["name"] = "?"
        result = self.manager.release("session-test")
        self.assertEqual(result["status"], "identity_mismatch")
        self.assertEqual(self.provider.released, [])

    def test_outage_blocks_new_work_but_estimate_keeps_accruing(self):
        self.create()
        self.provider.query_failure = True
        self.clock.advance(1800)
        result = self.tick()
        self.assertEqual(result["status"], "query_failed")
        self.assertEqual(result["estimate"]["consumed_cu"], 2)
        with self.assertRaises(budget.BudgetError):
            self.manager.authorize("session-test", "render", 900)
        self.clock.advance(6400)
        self.assertEqual(self.tick()["status"], "release_unknown")
        self.assertEqual(self.provider.released, [])
        self.provider.query_failure = False
        self.assertEqual(self.tick()["status"], "released")

    def test_usage_failure_uses_known_explicit_ceiling_and_marks_stale(self):
        self.create()
        self.provider.usage_failure = True
        self.clock.advance(301)
        result = self.tick()
        self.assertTrue(result["observed_account"]["stale"])
        self.assertIsNotNone(result["usage_error"])
        self.assertEqual(self.manager.authorize("session-test", "render", 900), 900)

    def test_account_observation_can_only_shorten_protection_points(self):
        original = self.create()
        self.provider.rate = 8
        self.clock.advance(301)
        updated = self.tick()
        self.assertLess(updated["work_deadline"], original["work_deadline"])
        self.assertEqual(updated["observed_account"]["rate_hourly"], 8)
        self.assertEqual(updated["rate_ceiling"], 4)
        self.assertEqual(updated["estimate"]["rate_hourly"], 8)
        self.provider.rate = 1
        self.clock.advance(301)
        later = self.tick()
        self.assertEqual(later["work_deadline"], updated["work_deadline"])
        self.assertEqual(later["effective_rate_ceiling"], 8)

    def test_work_deadline_preserves_collection_window_then_releases(self):
        self.create()
        self.clock.advance(7100)
        self.assertEqual(self.manager.authorize("session-test", "render", 900), 100)
        self.clock.advance(101)
        self.assertEqual(self.tick()["status"], "collect_only")
        with self.assertRaises(budget.BudgetError):
            self.manager.authorize("session-test", "render", 900)
        self.assertEqual(self.manager.authorize("session-test", "collect", 900), 899)
        self.assertIsNone(self.manager.authorize("session-test", "stop"))
        self.clock.advance(899)
        self.assertEqual(self.tick()["status"], "released")

    def test_idle_hold_is_capped_at_work_deadline_and_survives_restart(self):
        self.create()
        result = self.manager.hold("session-test", 10000)
        self.assertEqual(result["hold_deadline"], result["work_deadline"])
        replacement = self.make_manager()
        self.assertEqual(
            replacement.status("session-test")["hold_deadline"], result["hold_deadline"]
        )
        self.clock.advance(7200)
        self.assertEqual(
            replacement.tick("session-test", force=True)["status"], "released"
        )

    def test_rate_increase_shortens_existing_idle_hold(self):
        self.create()
        original = self.manager.hold("session-test", 7000)
        self.provider.rate = 8
        self.clock.advance(301)
        result = self.tick()
        self.assertLess(result["hold_deadline"], original["hold_deadline"])
        self.assertEqual(result["hold_deadline"], result["work_deadline"])

    def test_hold_cannot_revive_released_record(self):
        self.create()
        original_authorize = self.manager.authorize

        def authorize(*args):
            result = original_authorize(*args)
            self.manager.release("session-test")
            return result

        self.manager.authorize = authorize
        with self.assertRaises(budget.BudgetError):
            self.manager.hold("session-test", 60)
        self.assertEqual(self.manager.status("session-test")["status"], "released")

    def test_hold_is_scoped_to_original_job_if_session_is_reused(self):
        first = self.create()
        original_authorize = self.manager.authorize

        def authorize(*args):
            result = original_authorize(*args)
            self.manager.release("session-test")
            self.create()
            return result

        self.manager.authorize = authorize
        with self.assertRaises(budget.BudgetError):
            self.manager.hold("session-test", 60)
        current = self.manager.status("session-test")
        self.assertNotEqual(current["job_id"], first["job_id"])
        self.assertIsNone(current["hold_deadline"])

    def test_expired_release_retries_keep_regular_poll_interval(self):
        first = self.create()
        self.provider.release_result = {"ok": False, "status": "release_pending"}
        self.clock.advance(8200)

        class EndLoop(Exception):
            pass

        with (
            mock.patch.object(budget.time, "sleep", side_effect=EndLoop) as sleep,
            self.assertRaises(EndLoop),
        ):
            self.manager.watch("session-test", first["job_id"])
        sleep.assert_called_once_with(5)

    def test_cleanup_is_not_time_limited_but_requires_original_identity(self):
        self.create()
        self.clock.advance(7201)
        self.assertEqual(self.manager.authorize("session-test", "stop", 120), 120)
        self.provider.rows = [
            {"name": "session-test", "assignment_id": "replacement", "hardware": "G4"}
        ]
        with self.assertRaises(budget.BudgetError):
            self.manager.authorize("session-test", "stop", 120)

    def test_wall_clock_rollback_does_not_reset_or_extend_budget(self):
        original = self.create()
        self.clock.advance(100)
        self.tick()
        self.clock.wall -= 200
        self.clock.up += 50
        result = self.tick()
        self.assertEqual(result["estimate"]["elapsed_seconds"], 150)
        self.assertLess(result["work_deadline"], original["work_deadline"])
        self.clock.up += 8100
        self.assertEqual(self.tick()["status"], "released")

    def test_reboot_with_backward_clock_fails_closed_and_releases(self):
        self.create()
        self.clock.advance(100)
        self.tick()
        self.clock.boot, self.clock.up = "replacement-boot", 2
        self.clock.wall -= 200
        result = self.tick()
        self.assertEqual(result["status"], "released")
        self.assertEqual(len(self.provider.released), 1)

    def test_account_switch_cannot_turn_absence_into_release(self):
        self.create()
        self.provider.scope = "different-account-stat"
        self.provider.rows = []
        result = self.manager.release("session-test")
        self.assertEqual(result["status"], "release_unknown")
        self.assertFalse(result["ok"])
        self.assertEqual(self.provider.released, [])

    def test_scope_switch_between_observation_and_release_cannot_claim_success(self):
        self.create()
        original = self.provider.verified_release

        def switched(*args, **kwargs):
            self.provider.scope = "different-account-stat"
            self.provider.rows = []
            return original(*args, **kwargs)

        self.provider.verified_release = switched
        result = self.manager.release("session-test")
        self.assertEqual(result["status"], "release_unknown")
        self.assertFalse(result["verified"])
        self.assertEqual(self.provider.released, [])

    def test_same_allocation_visible_after_token_refresh_reestablishes_scope(self):
        self.create()
        self.provider.scope = "refreshed-token-stat"
        self.assertEqual(self.tick()["status"], "active")
        self.assertEqual(self.manager.release("session-test")["status"], "released")

    def test_new_allocation_gets_separate_budget_and_archives_old_record(self):
        first = self.create()
        self.manager.release("session-test")
        self.clock.advance(100)
        second = self.manager.create(
            "session-test", "L4", limit_cu=20, rate_ceiling=8, reserve_cu=2
        )
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["assignment_id"], second["assignment_id"])
        self.assertEqual(second["limit_cu"], 20)
        self.assertEqual(second["estimate"]["consumed_cu"], 0)
        self.assertTrue((self.root / "history" / (first["job_id"] + ".json")).is_file())

    def test_repeated_explicit_request_after_release_does_not_allocate_again(self):
        self.create(request_id="stable-request")
        self.manager.release("session-test")
        result = self.create(request_id="stable-request")
        self.assertEqual(result["status"], "released")
        self.assertEqual(len(self.provider.created), 1)

    def test_readonly_absent_state_does_not_create_directories(self):
        self.assertEqual(self.manager.list_active(), [])
        self.assertFalse(self.manager.status("test")["managed"])
        self.assertIsNone(self.manager.authorize("test", "render", 30))
        self.assertFalse(self.root.exists())

    def test_active_listing_keeps_unknown_and_excludes_verified_release(self):
        self.create()
        self.assertEqual(len(self.manager.list_active()), 1)
        self.provider.query_failure = True
        self.manager.release("session-test")
        self.assertEqual(len(self.manager.list_active()), 1)
        self.provider.query_failure = False
        self.tick()
        self.assertEqual(self.manager.list_active(), [])

    def test_corrupt_or_symlink_state_is_not_reset(self):
        self.create()
        path = self.manager._path("session-test")
        path.write_text("{broken")
        with self.assertRaises(budget.BudgetError):
            self.create()
        self.assertEqual(path.read_text(), "{broken")
        path.unlink()
        target = self.root / "target"
        target.write_text("do not touch")
        path.symlink_to(target)
        with self.assertRaises(budget.BudgetError):
            self.manager.status("session-test")
        self.assertEqual(target.read_text(), "do not touch")

    def test_reconcile_in_progress_blocks_new_submission(self):
        self.create()
        with (
            self.manager._lock("session-test", ".reconcile.lock"),
            self.assertRaises(budget.BudgetError),
        ):
            self.manager.authorize("session-test", "render", 30)

    def test_concurrent_creates_submit_only_once(self):
        barrier = threading.Barrier(2)
        original = self.provider.sessions
        count_lock = threading.Lock()
        calls = 0

        def sessions():
            nonlocal calls
            with count_lock:
                calls += 1
                current = calls
            if current <= 2:
                barrier.wait(timeout=3)
            return original()

        self.provider.sessions = sessions
        errors = []

        def run():
            try:
                self.create()
            except budget.BudgetError as error:
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertLessEqual(len(errors), 1)
        self.assertEqual(len(self.provider.created), 1)


class GuardLifecycleTests(unittest.TestCase):
    """Real local child processes; all provider operations are file fixtures."""

    def test_detached_guard_survives_creator_exit_and_restarts_without_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "provider.json"
            fixture.write_text("[]")
            helper = root / "fixture_guard.py"
            helper.write_text(
                "import json,sys,subprocess\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0,{str(SCRIPTS)!r})\n"
                "from budget_guard import BudgetManager\n"
                f"FIXTURE=Path({str(fixture)!r})\n"
                "class Provider:\n"
                " def sessions(self): return json.loads(FIXTURE.read_text())\n"
                " def account_scope(self): return 'fixture-no-credentials'\n"
                " def usage(self): return {'balance':100,'rate_hourly':1,'assignments_count':1}\n"
                " def create(self,session,gpu=None): FIXTURE.write_text(json.dumps([{'name':session,'assignment_id':'fixture-allocation','hardware':gpu or 'CPU'}]))\n"
                " def verified_release(self,session,assignment_id,**kwargs):\n"
                "  FIXTURE.write_text('[]')\n"
                "  return {'ok':True,'status':'released'}\n"
                "def spawn(args,**kw):\n"
                " args[1]=__file__\n"
                " return subprocess.Popen(args,**kw)\n"
                f"manager=BudgetManager(Path({str(root / 'state')!r}),Provider(),popen=spawn,poll_seconds=0.1)\n"
                "if sys.argv[1]=='create':\n"
                " print(json.dumps(manager.create('fixture',limit_cu=1,rate_ceiling=1,reserve_cu=0.1)))\n"
                "elif sys.argv[1]=='restart':\n"
                " print(json.dumps(manager.ensure_guard('fixture')))\n"
                "elif sys.argv[1]=='release':\n"
                " print(json.dumps(manager.release('fixture')))\n"
                "else:\n"
                " job=sys.argv[sys.argv.index('--job-id')+1]\n"
                " raise SystemExit(manager.watch('fixture',job))\n"
            )

            def command(action):
                completed = subprocess.run(
                    [sys.executable, str(helper), action],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                return json.loads(completed.stdout)

            first = command("create")
            first_pid = first["guard"]["pid"]
            first_stamp = first["guard"]["stamp"]
            second_pid = None
            owned_stamps = {first_pid: first_stamp}
            try:
                self.assertNotEqual(first_pid, os.getpid())
                self.assertEqual(budget.process_stamp(first_pid), first_stamp)
                self.assertEqual(os.getsid(first_pid), first_pid)
                # Parent command has exited; the independent process continues.
                time.sleep(0.2)
                self.assertEqual(budget.process_stamp(first_pid), first_stamp)
                os.kill(first_pid, signal.SIGKILL)
                deadline = time.monotonic() + 3
                while (
                    budget.process_stamp(first_pid) is not None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                restarted = command("restart")
                second_pid = restarted["guard"]["pid"]
                owned_stamps[second_pid] = restarted["guard"]["stamp"]
                self.assertNotEqual(second_pid, first_pid)
                self.assertEqual(restarted["job_id"], first["job_id"])
                self.assertEqual(restarted["allocated_at"], first["allocated_at"])
                self.assertLessEqual(restarted["work_deadline"], first["work_deadline"])
                self.assertAlmostEqual(
                    restarted["work_deadline"], first["work_deadline"], places=4
                )
                self.assertGreaterEqual(
                    restarted["estimate"]["consumed_cu"],
                    first["estimate"]["consumed_cu"],
                )
                result = command("release")
                self.assertEqual(result["status"], "released")
                deadline = time.monotonic() + 3
                while (
                    budget.process_stamp(second_pid) is not None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertIsNone(budget.process_stamp(second_pid))
            finally:
                for pid in (first_pid, second_pid):
                    if pid and budget.process_stamp(pid) == owned_stamps.get(pid):
                        os.kill(pid, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
