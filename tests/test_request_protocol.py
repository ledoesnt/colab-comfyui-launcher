"""Offline restart/crash/JSON contract tests; no credentials or provider calls."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import colabctl as bridge
from client_requests import ClientRequests


class RequestProtocolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="launcher-protocol-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for patch in (
            mock.patch.dict(os.environ, COLAB_LAUNCHER_STATE_DIR=directory.name),
            mock.patch.object(
                bridge,
                "assignment",
                return_value={"name": "fixture", "assignment_id": "allocation-a"},
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.manager = mock.Mock()
        self.manager.authorize.return_value = None
        self.manager.status.return_value = {"ok": True, "managed": False}
        patch = mock.patch.object(bridge, "budget_manager", return_value=self.manager)
        patch.start()
        self.addCleanup(patch.stop)

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = bridge.main(["-s", "fixture", *args])
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_upgrade_banner_never_leaks_into_json_stdout(self):
        output = "[colab] A new version of Colab CLI is available\n"
        output += (
            bridge.RESULT_PREFIX + json.dumps({"ok": True, "http_ready": True}) + "\n"
        )
        with mock.patch.object(
            bridge,
            "_run_colab",
            return_value=subprocess.CompletedProcess([], 0, output, "provider log"),
        ):
            code, result, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertTrue(result["http_ready"])

    def test_invalid_arguments_are_one_json_error_and_stderr_usage(self):
        code, result, stderr = self.cli("start")
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertIn("required", stderr)

    def help_text(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = bridge.main(["-s", "fixture", *args, "--help"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.manager.mock_calls, [])
        return " ".join(stdout.getvalue().split())

    def test_help_distinguishes_asset_total_deadline_from_provider_timeouts(self):
        root = self.help_text()
        self.assertIn("upload/collect share one total asset-operation deadline", root)
        self.assertIn("CU protection", root)
        self.assertIn("default: 120s", root)
        self.assertIn("Budget allocation uses separate provider timeouts", root)
        for action, cutoff in (
            ("upload", "work_deadline"),
            ("collect", "release_deadline"),
        ):
            with self.subTest(action=action):
                text = self.help_text(action)
                self.assertIn(f"global --timeout before {action}", text)
                self.assertIn("total asset-operation deadline", text)
                self.assertIn(cutoff, text)
                self.assertIn("hold_deadline", text)

    def test_help_explains_nonterminal_allocation_polling_and_refresh(self):
        for action in ("new", "budget"):
            with self.subTest(action=action):
                text = self.help_text(action)
                for required in (
                    "allocation_pending",
                    "allocation_unknown",
                    "exit 1",
                    "nonterminal",
                    "request_id",
                    "budget job_id",
                    "do not repeat new",
                ):
                    self.assertIn(required, text)
        text = self.help_text("budget")
        self.assertIn("Read-only local status polling without --refresh", text)
        self.assertIn("an expired budget may release the instance", text)
        self.assertIn("'job ID' queries separate task records", text)

    def test_unknown_submission_restart_only_queries_original(self):
        calls = []

        def transport(session, payload, timeout):
            calls.append(payload)
            if len(calls) == 1:
                raise bridge.BridgeError("Reply was lost after remote acceptance")
            self.assertEqual(payload["action"], "job")
            return {"ok": True, "status": "succeeded", "assets": []}

        with mock.patch.object(bridge, "execute_remote", side_effect=transport):
            code, first, _ = self.cli("--request-id", "durable-1", "render")
            self.assertEqual(code, 1)
            self.assertEqual(first["status"], "submission_unknown")
            # A fresh CLI invocation reads the persisted intent, not process memory.
            code, recovered, _ = self.cli("--request-id", "durable-1", "render")
        self.assertEqual(code, 0)
        self.assertEqual([call["action"] for call in calls], ["render", "job"])
        self.assertEqual(recovered["request_id"], first["request_id"])
        self.assertEqual(
            ClientRequests("fixture").read("durable-1")["status"], "succeeded"
        )

    def test_same_id_different_payload_never_dispatches(self):
        with mock.patch.object(
            bridge, "execute_remote", return_value={"ok": True, "status": "started"}
        ) as remote:
            self.cli("--request-id", "same", "render", "--max-seconds", "20")
            code, result, _ = self.cli(
                "--request-id", "same", "render", "--max-seconds", "30"
            )
        self.assertEqual(code, 2)
        self.assertIn("different arguments", result["error"])
        self.assertEqual(remote.call_count, 1)

    def test_allocation_replacement_cannot_reuse_old_request(self):
        with mock.patch.object(
            bridge, "execute_remote", return_value={"ok": True, "status": "started"}
        ) as remote:
            self.cli("--request-id", "old", "prepare")
            with mock.patch.object(
                bridge,
                "assignment",
                return_value={"name": "fixture", "assignment_id": "allocation-b"},
            ):
                code, result, _ = self.cli("job", "old")
        self.assertEqual(code, 1)
        self.assertIn("replacement", result["error"])
        self.assertEqual(remote.call_count, 1)

    def test_terminal_result_query_survives_instance_release(self):
        with mock.patch.object(
            bridge,
            "execute_remote",
            return_value={"ok": True, "status": "succeeded", "assets": []},
        ):
            self.cli("--request-id", "finished", "render")
        with mock.patch.object(
            bridge, "assignment", side_effect=AssertionError("No provider query needed")
        ):
            code, result, _ = self.cli("job", "finished")
        self.assertEqual(code, 0)
        self.assertEqual(result["source"], "local_receipt")

    def test_failed_supervisor_receipt_does_not_hide_running_remote_job(self):
        store = ClientRequests("fixture")
        with store.lock("uncertain"):
            store.begin(
                "uncertain", {"action": "render", "assignment_id": "allocation-a"}
            )
            store.finish(
                "uncertain",
                {"ok": False, "status": "failed", "job_may_still_be_running": True},
            )
        with mock.patch.object(
            bridge, "execute_remote", return_value={"ok": True, "status": "succeeded"}
        ) as remote:
            code, result, _ = self.cli("job", "uncertain")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "succeeded")
        remote.assert_called_once()

    def test_budget_rejection_is_known_not_submitted(self):
        self.manager.authorize.side_effect = RuntimeError("Guard has no heartbeat")
        with mock.patch.object(bridge, "execute_remote") as remote:
            code, result, _ = self.cli("--request-id", "blocked", "render")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "not_submitted")
        remote.assert_not_called()

    def test_historical_allocation_request_does_not_query_current_same_name(self):
        self.manager.create.return_value = {
            "ok": True,
            "status": "released",
            "assignment_id": "old-allocation",
        }
        with mock.patch.object(
            bridge,
            "assignment",
            side_effect=AssertionError("Old result must not acquire the new instance"),
        ):
            code, result, _ = self.cli(
                "--request-id",
                "allocation-old",
                "new",
                "--gpu",
                "T4",
                "--cu-limit",
                "10",
                "--rate-ceiling",
                "20",
            )
        self.assertEqual(code, 0)
        self.assertEqual(result["assignment_id"], "old-allocation")
        self.assertNotIn("provider", result)

    def test_legacy_release_retry_retains_original_identity_and_account_scope(self):
        api = mock.Mock()
        api.verified_release.side_effect = [
            {
                "ok": False,
                "status": "release_unknown",
                "verified": False,
                "assignment_id": "allocation-a",
            },
            {
                "ok": True,
                "status": "released",
                "verified": True,
                "assignment_id": "allocation-a",
            },
        ]
        with (
            mock.patch.object(bridge, "provider", return_value=api),
            mock.patch.object(
                bridge,
                "assignment",
                return_value={
                    "assignment_id": "allocation-a",
                    "account_scope": "scope-original",
                },
            ) as identity,
        ):
            code, first, _ = self.cli("release")
            self.assertEqual(code, 1)
            identity.side_effect = AssertionError(
                "Retry must use original persisted identity"
            )
            code, second, _ = self.cli("release")
        self.assertEqual(code, 0)
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(
            api.verified_release.call_args_list,
            [
                mock.call("fixture", "allocation-a", expected_scope="scope-original"),
                mock.call("fixture", "allocation-a", expected_scope="scope-original"),
            ],
        )

    def test_budget_refresh_is_explicit(self):
        self.cli("budget", "--refresh")
        self.manager.status.assert_called_once_with("fixture", refresh=True)

    def test_budget_clips_task_but_retry_does_not_change_request_fingerprint(self):
        self.manager.authorize.return_value = 12.5
        with mock.patch.object(
            bridge, "execute_remote", return_value={"ok": True, "status": "started"}
        ) as remote:
            self.cli("--request-id", "budgeted", "prepare", "--max-seconds", "120")
            payload = remote.call_args.args[1]
            self.assertEqual(payload["max_seconds"], 12.5)
            self.assertIn("not_after", payload)
            self.manager.authorize.return_value = 5.0
            code, _, _ = self.cli(
                "--request-id", "budgeted", "prepare", "--max-seconds", "120"
            )
            self.assertEqual(remote.call_args.args[1]["action"], "job")
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.authorize.call_count, 1)

    def test_server_node_errors_are_structured_on_stdout(self):
        details = {
            "ok": False,
            "status": "failed",
            "error": "node validation failed",
            "node_errors": {
                "4": {"errors": [{"type": "bad_input", "message": "Missing model"}]}
            },
        }
        with mock.patch.object(
            bridge,
            "execute_remote",
            side_effect=bridge.BridgeError("node validation failed", details),
        ):
            code, result, _ = self.cli("--request-id", "rejected", "render")
        self.assertEqual(code, 1)
        self.assertEqual(result["node_errors"], details["node_errors"])

    def test_custom_api_and_mapping_are_loaded_without_template_restriction(self):
        workflow = self.root / "workflow.json"
        mapping = self.root / "mapping.json"
        data = {"9": {"class_type": "CustomOutput", "inputs": {}}}
        workflow.write_text(json.dumps(data))
        mapping.write_text('{"version":1,"parameters":{}}')
        with mock.patch.object(
            bridge, "execute_remote", return_value={"ok": True}
        ) as remote:
            code, _, _ = self.cli(
                "render",
                "--workflow-file",
                str(workflow),
                "--mapping",
                str(mapping),
                "--export-frames",
            )
        self.assertEqual(code, 0)
        self.assertEqual(remote.call_args.args[1]["workflow_data"], data)
        self.assertTrue(remote.call_args.args[1]["export_frames"])

    def test_verify_only_rejects_implicit_download_override(self):
        with mock.patch.object(bridge, "execute_remote") as remote:
            code, result, _ = self.cli(
                "prepare", "--mode", "verify_only", "--download-missing"
            )
        self.assertEqual(code, 2)
        self.assertIn("conflicts", result["error"])
        remote.assert_not_called()

    def test_asset_transfers_honor_budget_seconds_and_recovered_manifest(self):
        self.manager.authorize.return_value = 5.0
        recovered = {
            "ok": True,
            "status": "started",
            "session": "fixture",
            "assignment_id": "allocation-a",
            "render_record": {
                "ok": True,
                "status": "succeeded",
                "request_id": "render-1",
                "assets": [{"filename": "result.mp4"}],
            },
        }
        with (
            mock.patch("assets.AssetsClient") as client_type,
            mock.patch.object(bridge, "query_job", return_value=recovered),
        ):
            client_type.return_value.collect.return_value = {"ok": True}
            code, _, _ = self.cli(
                "collect", "--job", "render-1", "--destination", str(self.root / "out")
            )
        self.assertEqual(code, 0)
        self.assertEqual(client_type.call_args.kwargs["timeout"], 5.0)
        manifest = client_type.return_value.collect.call_args.args[0]
        self.assertEqual(manifest["assets"][0]["filename"], "result.mp4")
        self.assertEqual(manifest["assignment_id"], "allocation-a")

    def test_assets_remote_requests_carry_a_fixed_protection_cutoff(self):
        self.manager.authorize.return_value = 5.0
        with (
            mock.patch("assets.AssetsClient") as client_type,
            mock.patch.object(
                bridge, "execute_remote", return_value={"ok": True}
            ) as remote,
        ):

            def upload(source):
                callback = client_type.call_args.args[3]
                return callback("fixture", {"action": "asset-stage"}, 120)

            client_type.return_value.upload.side_effect = upload
            code, _, _ = self.cli("upload", str(self.root / "source.png"))
        self.assertEqual(code, 0)
        self.assertIn("not_after", remote.call_args.args[1])
        self.assertLessEqual(remote.call_args.args[2], 5.0)

    def test_request_lock_serializes_concurrent_clients(self):
        first_entered, release_first = threading.Event(), threading.Event()
        dispatched = []

        def client():
            store = ClientRequests("fixture")
            with store.lock("concurrent"):
                _, fresh = store.begin("concurrent", {"action": "render"})
                if fresh:
                    dispatched.append("submit")
                    first_entered.set()
                    release_first.wait(2)

        threads = [threading.Thread(target=client) for _ in range(2)]
        threads[0].start()
        self.assertTrue(first_entered.wait(1))
        threads[1].start()
        release_first.set()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(dispatched, ["submit"])


class RemoteJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-remote-journal-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.namespace = {}
        exec(  # noqa: S102 - trusted local remote-program fixture.
            bridge.REMOTE_HELPERS.replace(
                "/content/colab-comfyui-runtime", temporary.name
            ),
            self.namespace,
        )
        self.payload = {
            "action": "render",
            "request_id": "request-a",
            "session": "fixture",
            "assignment_id": "allocation-a",
        }

    def test_crash_after_intent_never_dispatches_again(self):
        operation = mock.Mock(
            side_effect=SystemExit("simulated abrupt controller death")
        )
        with mock.patch.dict(self.namespace, dispatch=operation):
            with self.assertRaises(SystemExit):
                self.namespace["dispatch_request"](self.payload)
            result = self.namespace["dispatch_request"](self.payload)
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(result["status"], "submission_unknown")

    def test_completed_jobs_survive_new_task_and_global_status_overwrite(self):
        with mock.patch.dict(
            self.namespace, dispatch=lambda payload: {"ok": True, "status": "started"}
        ):
            self.namespace["dispatch_request"](self.payload)
            self.namespace["finish_job"](
                self.payload,
                {
                    "ok": True,
                    "status": "succeeded",
                    "assets": [{"filename": "first.mp4"}],
                },
            )
            other = dict(self.payload, request_id="request-b")
            self.namespace["dispatch_request"](other)
            result = self.namespace["job_status"]("request-a")
        self.assertEqual(result["assets"][0]["filename"], "first.mp4")
        self.assertEqual(result["job_id"], "request-a")

    def test_instance_marker_cannot_be_rebound(self):
        self.namespace["bind_instance"](self.payload)
        with self.assertRaisesRegex(RuntimeError, "identity differs"):
            self.namespace["bind_instance"](
                dict(self.payload, assignment_id="replacement")
            )
        saved = json.loads((self.directory / "instance.json").read_text())
        self.assertEqual(saved["assignment_id"], "allocation-a")

    def test_instance_marker_rejects_symlink_parent_before_writing(self):
        outside = self.directory / "outside"
        outside.mkdir()
        linked = self.directory / "runtime-link"
        linked.symlink_to(outside, target_is_directory=True)
        namespace = {}
        exec(  # noqa: S102 - trusted local remote-program fixture.
            bridge.REMOTE_HELPERS.replace(
                "/content/colab-comfyui-runtime", str(linked)
            ),
            namespace,
        )
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            namespace["bind_instance"](self.payload)
        self.assertFalse((outside / "instance.json").exists())

    def test_render_job_query_reconciles_read_only_and_keeps_assignment(self):
        with mock.patch.dict(
            self.namespace, dispatch=lambda payload: {"ok": True, "status": "started"}
        ):
            self.namespace["dispatch_request"](self.payload)
        render_dir = self.directory / "render-jobs"
        render_dir.mkdir()
        record = dict(
            self.payload,
            job_id="request-a",
            status="submission_unknown",
            submission_attempted=True,
        )
        (render_dir / "request-a.json").write_text(json.dumps(record))
        complete = dict(
            record,
            ok=True,
            status="succeeded",
            terminal=True,
            assets=[{"filename": "recovered.mp4"}],
        )
        with (
            mock.patch.object(self.namespace["Path"], "is_file", return_value=True),
            mock.patch.object(
                self.namespace["subprocess"],
                "run",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {"ok": True, "request_id": "request-a", "result": complete}
                    ),
                    "",
                ),
            ) as run,
        ):
            result = self.namespace["job_status"]("request-a")
        self.assertEqual(result["assets"][0]["filename"], "recovered.mp4")
        self.assertIn("--query-only", run.call_args.args[0])
        self.assertEqual(result["assignment_id"], "allocation-a")

    def test_expired_budget_payload_is_recorded_without_running_task(self):
        payload = dict(self.payload, not_after=1)
        run = mock.Mock()
        with mock.patch.dict(self.namespace, run_background=run):
            result = self.namespace["dispatch_request"](payload)
        run.assert_not_called()
        self.assertEqual(result["status"], "budget_expired")

    def test_explicit_service_stop_does_not_use_stop_request_as_startup_owner(self):
        runtime = mock.Mock(return_value={"ok": True, "deployed": False})
        with mock.patch.dict(
            self.namespace,
            stop_startup=lambda: None,
            stop_background=lambda action: None,
            runtime_result=runtime,
        ):
            self.namespace["runtime_action"](
                {"action": "stop", "request_id": "stop-request"}
            )
        self.assertNotIn("request_id", runtime.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
