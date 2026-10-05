"""Background bridge fixtures; never call Colab or create real processes."""

import ast
import fcntl
import importlib.util
import io
import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "bridge_background", ROOT / "scripts/colabctl.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BackgroundBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-background-test-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.namespace = {}
        exec(bridge.REMOTE_HELPERS, self.namespace)  # noqa: S102 - trusted local code.
        self.namespace["BACKGROUND_SUPERVISOR"] = "trusted supervisor placeholder"

        def paths(action):
            name = {
                "download": "model-download",
                "prepare": "model-prepare",
                "render": "render",
            }[action]
            return (
                self.state,
                self.state / f"{name}-process.json",
                self.state / f"{name}-result.json",
                name,
            )

        self.namespace["background_paths"] = paths
        self.namespace["background_arguments"] = lambda payload: ["python", "runner.py"]
        self.payload = {"action": "download", "max_seconds": 10, "ephemeral": True}

    def test_background_start_records_identity_and_retains_lock_for_controller(self):
        with (
            mock.patch.object(
                self.namespace["subprocess"], "Popen", return_value=mock.Mock(pid=12345)
            ) as spawn,
            mock.patch.dict(self.namespace, process_stamp=lambda pid: "owned-start"),
        ):
            result = self.namespace["run_background"](self.payload)
        self.assertEqual(result["status"], "started")
        process = json.loads((self.state / "model-download-process.json").read_text())
        self.assertEqual(process, {"pid": 12345, "stamp": "owned-start"})
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(len(spawn.call_args.kwargs["pass_fds"]), 1)

    def test_live_controller_lock_prevents_duplicate_start(self):
        with (self.state / "model-download-launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(self.namespace["subprocess"], "Popen") as spawn:
                result = self.namespace["run_background"](self.payload)
        self.assertTrue(result["already_running"])
        spawn.assert_not_called()

    def supervisor_body(self, name="BACKGROUND_SUPERVISOR"):
        program = ast.parse(bridge.remote_program(self.payload))
        assignment = next(
            node
            for node in program.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        )
        source = ast.literal_eval(assignment.value)
        self.assertTrue(source.startswith(bridge.REMOTE_HELPERS))
        return source[len(bridge.REMOTE_HELPERS) :]

    def test_failed_start_cleanup_is_scoped_to_its_request(self):
        payload = {"action": "start", "request_id": "new-start-request"}
        results = []
        runtime = mock.Mock(side_effect=[RuntimeError("Already running"), {"ok": True}])
        with (
            mock.patch.object(
                self.namespace["sys"], "argv", ["controller", json.dumps(payload)]
            ),
            mock.patch.dict(
                self.namespace,
                runtime_result=runtime,
                atomic_json=lambda path, value: results.append(value),
            ),
        ):
            exec(self.supervisor_body("START_SUPERVISOR"), self.namespace)  # noqa: S102
        self.assertFalse(results[0]["ok"])
        self.assertEqual(
            runtime.call_args_list,
            [
                mock.call(payload, timeout=210),
                mock.call(
                    {"action": "stop", "request_id": "new-start-request"}, timeout=20
                ),
            ],
        )

    def test_successful_start_supervisor_does_not_issue_cleanup(self):
        payload = {"action": "start", "request_id": "new-start-request"}
        results = []
        runtime = mock.Mock(return_value={"ok": True, "status": "started"})
        with (
            mock.patch.object(
                self.namespace["sys"], "argv", ["controller", json.dumps(payload)]
            ),
            mock.patch.dict(
                self.namespace,
                runtime_result=runtime,
                atomic_json=lambda path, value: results.append(value),
            ),
        ):
            exec(self.supervisor_body("START_SUPERVISOR"), self.namespace)  # noqa: S102
        runtime.assert_called_once_with(payload, timeout=210)
        self.assertTrue(results[0]["ok"])

    def test_runtime_cleanup_request_identity_is_passed_to_child(self):
        completed = subprocess.CompletedProcess(
            ["runtime"], 0, stdout='{"ok": true}', stderr=""
        )
        with (
            mock.patch.object(self.namespace["Path"], "is_file", return_value=True),
            mock.patch.object(
                self.namespace["subprocess"], "run", return_value=completed
            ) as run,
        ):
            self.namespace["runtime_result"](
                {"action": "stop", "request_id": "new-start-request"}
            )
        self.assertEqual(
            run.call_args.args[0][-2:], ["--request-id", "new-start-request"]
        )

    def execute_supervisor(self, completed=None, error=None):
        with (
            mock.patch.object(
                self.namespace["sys"], "argv", ["controller", json.dumps(self.payload)]
            ),
            mock.patch.object(
                self.namespace["subprocess"],
                "run",
                return_value=completed,
                side_effect=error,
            ) as run,
        ):
            exec(self.supervisor_body(), self.namespace)  # noqa: S102 - trusted local code.
        result = json.loads((self.state / "model-download-result.json").read_text())
        return result, run

    def test_child_error_json_is_failure_even_with_exit_zero(self):
        result, _ = self.execute_supervisor(
            subprocess.CompletedProcess(
                ["python"],
                0,
                stdout='{"ok": false, "error": "checksum failed"}',
                stderr="",
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")

    def test_invalid_json_and_timeout_are_recorded_failures(self):
        for output in ("not JSON", '{"ok": true}\n{"ok": true}', '{"ok": "true"}'):
            with self.subTest(output=output):
                result, _ = self.execute_supervisor(
                    subprocess.CompletedProcess(
                        ["python"], 0, stdout=output, stderr="private diagnostics"
                    )
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failed")
        result, _ = self.execute_supervisor(
            error=subprocess.TimeoutExpired(["python"], 25)
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")

    def test_child_exit_failure_cannot_report_success(self):
        result, _ = self.execute_supervisor(
            subprocess.CompletedProcess(
                ["python"], 1, stdout='{"ok": true}', stderr="private diagnostics"
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")

    def test_verified_success_has_bounded_deadline_and_sanitized_result(self):
        result, run = self.execute_supervisor(
            subprocess.CompletedProcess(
                ["python"],
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "note": "https://example.invalid/signed?token=private",
                        "files": [],
                    }
                ),
                stderr="",
            )
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(run.call_args.kwargs["timeout"], 25)
        self.assertNotIn("private", json.dumps(result))

    def test_status_reports_live_final_failed_and_interrupted_states(self):
        process = self.state / "model-download-process.json"
        result_path = self.state / "model-download-result.json"
        process.write_text(json.dumps({"pid": 12345, "stamp": "owned-start"}))
        result_path.write_text('{"ok": true, "status": "starting"}')
        with mock.patch.dict(self.namespace, process_stamp=lambda pid: "owned-start"):
            self.assertTrue(self.namespace["background_status"]("download")["running"])
        with mock.patch.dict(self.namespace, process_stamp=lambda pid: None):
            interrupted = self.namespace["background_status"]("download")
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertFalse(interrupted["result"]["ok"])
            result_path.write_text(
                '{"ok": false, "status": "failed", "error": "https://example.invalid/private"}'
            )
            status = self.namespace["background_status"]("download")
        self.assertEqual(status["status"], "failed")
        self.assertNotIn("example.invalid", json.dumps(status))

    def test_status_includes_download_prepare_and_render(self):
        with mock.patch.dict(
            self.namespace,
            runtime_result=lambda payload: {"ok": True},
            read_startup=lambda: None,
        ):
            result = self.namespace["runtime_action"]({"action": "status"})
        self.assertEqual(result["model_download"]["status"], "not_started")
        self.assertEqual(result["model_prepare"]["status"], "not_started")
        self.assertEqual(result["render"]["status"], "not_started")

    def test_stop_never_signals_reused_pid_and_stops_owned_group(self):
        process = self.state / "model-download-process.json"
        process.write_text(json.dumps({"pid": 12345, "stamp": "owned-start"}))
        with (
            mock.patch.dict(self.namespace, process_stamp=lambda pid: "other-start"),
            mock.patch.object(self.namespace["os"], "killpg") as kill,
        ):
            self.namespace["stop_background"]("download")
        kill.assert_not_called()
        with (
            mock.patch.dict(
                self.namespace,
                process_stamp=mock.Mock(side_effect=["owned-start", None, None]),
            ),
            mock.patch.object(self.namespace["os"], "killpg") as kill,
        ):
            self.namespace["stop_background"]("download")
        kill.assert_called_once_with(12345, signal.SIGTERM)
        result = json.loads((self.state / "model-download-result.json").read_text())
        self.assertEqual(result["status"], "stopped")

    def test_stop_controllers_before_services(self):
        calls = []
        with mock.patch.dict(
            self.namespace,
            stop_startup=lambda: calls.append("startup"),
            stop_background=lambda action: calls.append(action),
            runtime_result=lambda payload: calls.append("services") or {"ok": True},
            atomic_json=lambda path, value: None,
        ):
            self.namespace["runtime_action"]({"action": "stop"})
        self.assertEqual(
            calls, ["startup", "download", "prepare", "render", "services"]
        )


class UndeployedRuntimeTests(unittest.TestCase):
    """Map every remote path to actual temporary files; execute no child process."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-undeployed-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.namespace = {}
        exec(bridge.REMOTE_HELPERS, self.namespace)  # noqa: S102 - trusted local code.

        def remote_path(value):
            path = Path(value)
            return self.root / (str(path).lstrip("/") if path.is_absolute() else path)

        self.namespace["Path"] = remote_path
        self.runtime = remote_path("/content/colab-comfyui-runtime")
        self.launcher = remote_path("/content/colab-comfyui-launcher")
        self.script = self.launcher / "scripts/runtime.py"
        self.services = self.runtime / "services.json"
        self.run = self.patch(
            self.namespace["subprocess"],
            "run",
            side_effect=AssertionError(
                "A fixture must explicitly provide its child result"
            ),
        )
        self.spawn = self.patch(self.namespace["subprocess"], "Popen")
        self.kill = self.patch(self.namespace["os"], "kill")
        self.killpg = self.patch(self.namespace["os"], "killpg")
        self.patch(self.namespace["os"].path, "ismount", return_value=False)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            if path.is_file()
            else None
            for path in self.root.rglob("*")
        }

    def assert_no_process_action(self):
        self.run.assert_not_called()
        self.spawn.assert_not_called()
        self.kill.assert_not_called()
        self.killpg.assert_not_called()

    def create_services_record(self):
        self.runtime.mkdir(parents=True)
        self.services.write_text(
            json.dumps({"comfyui": {"pid": 12345, "stamp": "historical-start"}})
        )

    def deploy_fixture_script(self):
        self.script.parent.mkdir(parents=True)
        self.script.write_text("# Child execution is stubbed by this fixture.\n")

    def test_fresh_status_is_readable_without_creating_remote_directories(self):
        before = self.snapshot()
        result = self.namespace["runtime_action"]({"action": "status"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deployed"])
        self.assertEqual(result["deployment"]["status"], "not_deployed")
        self.assertEqual(result["installation"]["status"], "not_started")
        for field in ("comfyui_alive", "tunnel_alive", "http_ready", "models_ready"):
            self.assertIs(result[field], False)
        self.assertIsNone(result["startup"])
        for task in ("model_download", "model_prepare", "render"):
            self.assertFalse(result[task]["running"])
            self.assertEqual(result[task]["status"], "not_started")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "content").exists())
        self.assert_no_process_action()

    def test_historical_service_record_keeps_liveness_unknown_without_runtime_code(
        self,
    ):
        self.create_services_record()
        before = self.snapshot()
        result = self.namespace["runtime_action"]({"action": "status"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deployed"])
        self.assertEqual(result["installation"]["status"], "unknown")
        for field in ("comfyui_alive", "tunnel_alive", "http_ready"):
            self.assertIsNone(result[field])
        self.assertFalse(result["models_ready"])
        self.assertIsNone(result["url"])
        self.assertEqual(self.snapshot(), before)
        self.assert_no_process_action()

    def test_historical_installation_does_not_claim_ready_without_runtime_code(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / "install.json").write_text(json.dumps({"status": "ready"}))
        before = self.snapshot()
        result = self.namespace["runtime_action"]({"action": "status"})
        self.assertFalse(result["deployed"])
        self.assertEqual(result["installation"]["status"], "unknown")
        for field in ("comfyui_alive", "tunnel_alive", "http_ready", "models_ready"):
            self.assertIs(result[field], False)
        self.assertEqual(self.snapshot(), before)
        self.assert_no_process_action()

    def test_fresh_stop_is_noop_without_state_files_or_signals(self):
        before = self.snapshot()
        result = self.namespace["runtime_action"]({"action": "stop"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["deployed"])
        self.assertTrue(result["cleanup_skipped"])
        self.assertTrue(result["runtime_still_running"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.runtime / "start-result.json").exists())
        self.assert_no_process_action()

    def test_historical_services_without_runtime_code_refuse_stop(self):
        self.create_services_record()
        before = self.snapshot()
        with self.assertRaisesRegex(
            RuntimeError, "[Ee]xisting service state cannot be cleaned"
        ):
            self.namespace["runtime_action"]({"action": "stop"})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.runtime / "start-result.json").exists())
        self.assert_no_process_action()

    def test_deployed_runtime_keeps_real_child_failure_instead_of_fresh_success(self):
        self.deploy_fixture_script()
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess(
            ["runtime"],
            0,
            stdout=json.dumps(
                {"ok": False, "error": "Model preparation checksum failed"}
            ),
            stderr="",
        )
        with self.assertRaisesRegex(RuntimeError, "Model preparation checksum failed"):
            self.namespace["runtime_action"]({"action": "status"})
        self.run.assert_called_once()
        self.assertEqual(
            self.run.call_args.args[0][-2:],
            ["/content/colab-comfyui-launcher/scripts/runtime.py", "status"],
        )
        self.assertFalse((self.runtime / "start-result.json").exists())
        self.spawn.assert_not_called()
        self.killpg.assert_not_called()

    def test_deployed_status_adds_deployment_marker_without_replacing_real_status(self):
        self.deploy_fixture_script()
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess(
            ["runtime"],
            0,
            stdout=json.dumps(
                {"ok": True, "http_ready": False, "installation": {"status": "failed"}}
            ),
            stderr="",
        )
        result = self.namespace["runtime_action"]({"action": "status"})
        self.assertTrue(result["deployed"])
        self.assertEqual(result["deployment"]["status"], "deployed")
        self.assertFalse(result["http_ready"])
        self.assertEqual(result["installation"]["status"], "failed")
        self.run.assert_called_once()

    def test_missing_session_and_exec_failure_are_not_undeployed_success(self):
        fresh_response = bridge.RESULT_PREFIX + json.dumps(
            {"ok": True, "deployed": False}
        )
        for output, error in (
            ("", "Session missing was not found"),
            (fresh_response, "Remote exec failed"),
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(error=error),
                mock.patch.object(
                    bridge,
                    "_run_colab",
                    return_value=subprocess.CompletedProcess(
                        ["colab", "exec"], 1, stdout=output, stderr=error
                    ),
                ) as command,
                mock.patch("sys.stdout", stdout),
                mock.patch("sys.stderr", stderr),
            ):
                self.assertEqual(bridge.main(["-s", "missing", "status"]), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn(error, stderr.getvalue())
            self.assertEqual(command.call_args.args[0][:3], ["exec", "-s", "missing"])
        self.assert_no_process_action()


class BackgroundArgumentsTests(unittest.TestCase):
    def test_start_requires_one_access_mode(self):
        for access in ([], ["--public", "--allowed-email", "tester@example.invalid"]):
            with (
                self.subTest(access=access),
                mock.patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit) as failed,
            ):
                bridge.parser().parse_args(["-s", "owned", "start", *access])
            self.assertEqual(failed.exception.code, 2)

    def test_public_start_payload_and_private_runtime_compatibility(self):
        with (
            mock.patch.object(
                bridge, "execute_remote", return_value={"ok": True}
            ) as execute,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(bridge.main(["-s", "owned", "start", "--public"]), 0)
        self.assertTrue(execute.call_args.args[1]["public"])
        namespace = {}
        exec(bridge.REMOTE_HELPERS, namespace)  # noqa: S102
        for public in (False, True):
            payload = {
                "action": "start",
                "ephemeral": True,
                "cpu": True,
                "allowed_email": "tester@example.invalid",
            }
            if public:
                payload["public"] = True
            with (
                mock.patch.object(namespace["Path"], "is_file", return_value=True),
                mock.patch.object(
                    namespace["subprocess"],
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["python"], 0, stdout='{"ok": true}', stderr=""
                    ),
                ) as run,
            ):
                namespace["runtime_result"](payload)
            arguments = run.call_args.args[0]
            self.assertEqual("--public" in arguments, public)
            self.assertEqual("--allowed-email" in arguments, not public)

    def test_local_download_passes_explicit_paths_and_deadline_without_colab_calls(
        self,
    ):
        with (
            mock.patch.object(
                bridge, "execute_remote", return_value={"ok": True, "status": "started"}
            ) as execute,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            code = bridge.main(
                [
                    "-s",
                    "owned",
                    "download",
                    "--max-seconds",
                    "120",
                    "--models-root",
                    "/content/dedicated/models",
                    "--ephemeral",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            execute.call_args.args[1],
            {
                "action": "download",
                "max_seconds": 120.0,
                "models_root": "/content/dedicated/models",
                "ephemeral": True,
                "verify_cache": False,
            },
        )

    def test_parser_defaults_and_fixed_render_workflow(self):
        download = bridge.parser().parse_args(["-s", "owned", "download"])
        render = bridge.parser().parse_args(["-s", "owned", "render"])
        self.assertEqual(download.max_seconds, 1800)
        self.assertEqual(render.max_seconds, 900)
        namespace = {}
        exec(bridge.REMOTE_HELPERS, namespace)  # noqa: S102
        with mock.patch.object(namespace["Path"], "is_file", return_value=True):
            arguments = namespace["background_arguments"](
                {"action": "render", "max_seconds": 90}
            )
        self.assertIn(
            "/content/colab-comfyui-launcher/workflows/h3-api.json", arguments
        )
        self.assertIn("/content/colab-comfyui-runtime/.venv/bin/python", arguments)

    def test_prepare_and_local_only_contracts(self):
        with (
            mock.patch.object(
                bridge, "execute_remote", return_value={"ok": True}
            ) as execute,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(
                bridge.main(
                    [
                        "-s",
                        "owned",
                        "prepare",
                        "--cache-root",
                        "/content/drive/MyDrive/cache/models",
                        "--download-missing",
                    ]
                ),
                0,
            )
        payload = execute.call_args.args[1]
        self.assertTrue(payload["download_missing"])
        namespace = {}
        exec(bridge.REMOTE_HELPERS, namespace)  # noqa: S102
        with mock.patch.object(namespace["Path"], "is_file", return_value=True):
            arguments = namespace["background_arguments"](payload)
        self.assertIn("--download-missing", arguments)
        self.assertIn("/content/drive/MyDrive/cache/models", arguments)
        with (
            mock.patch.object(
                bridge, "execute_remote", return_value={"ok": True}
            ) as execute,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(bridge.main(["-s", "owned", "start", "--local-only"]), 0)
        self.assertTrue(execute.call_args.args[1]["local_only"])


if __name__ == "__main__":
    unittest.main()
