"""Local safety regressions; no Colab, Drive, GPU, tunnel, or model access."""

import argparse
import importlib.util
import io
import json
import signal
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load_script("runtime")
bridge = load_script("colabctl")
real_port_free = runtime.port_free


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-runtime-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        (self.base / "logs").mkdir()
        patches = [
            mock.patch.object(runtime, "BASE", self.base),
            mock.patch.object(runtime, "STATE", self.base / "services.json"),
            mock.patch.object(runtime, "port_free", return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.args = argparse.Namespace(
            ephemeral=True,
            storage_root=str(self.base / "unexpected-drive-path"),
            allowed_email="tester@example.com",
            public=False,
            cpu=True,
        )
        runtime.write_json(self.base / "install.json", {"status": "ready"})

    def test_reused_pid_is_never_signalled(self):
        old = {"pid": 12345, "start_ticks": "old-start"}
        with (
            mock.patch.object(runtime, "process_stamp", return_value="new-start"),
            mock.patch.object(runtime.os, "killpg") as kill,
        ):
            self.assertFalse(runtime.alive(old))
            runtime.kill_owned(old)
        kill.assert_not_called()

    def test_missing_start_stamp_is_never_considered_alive(self):
        old = {"pid": 12345, "start_ticks": None}
        with (
            mock.patch.object(runtime, "process_stamp", return_value=None),
            mock.patch.object(runtime.os, "killpg") as kill,
        ):
            self.assertFalse(runtime.alive(old))
            runtime.kill_owned(old)
        kill.assert_not_called()

    def test_missing_process_is_not_alive(self):
        with mock.patch.object(runtime, "process_stamp", return_value=None):
            self.assertFalse(runtime.alive({"pid": 12345, "start_ticks": "old-start"}))
            self.assertFalse(runtime.alive(None))

    def test_reserved_and_invalid_pids_are_not_alive(self):
        with mock.patch.object(runtime, "process_stamp", return_value="owned-start"):
            for pid in (0, 1, -1, "12345", None):
                with self.subTest(pid=pid):
                    self.assertFalse(
                        runtime.alive({"pid": pid, "start_ticks": "owned-start"})
                    )

    def test_process_stat_handles_spaces_and_parentheses_in_name(self):
        fields = ["S", *(["0"] * 18), "67890", "0"]
        text = "12345 (name with (parentheses) and spaces) " + " ".join(fields)
        path = mock.Mock()
        path.read_text.return_value = text
        with mock.patch.object(runtime, "Path", return_value=path):
            self.assertEqual(runtime.process_stamp(12345), "67890")
        path.read_text.return_value = text.replace(") S ", ") Z ")
        with mock.patch.object(runtime, "Path", return_value=path):
            self.assertIsNone(runtime.process_stamp(12345))

    def test_spawn_rejects_process_without_recordable_identity(self):
        process = mock.Mock(pid=12345)
        with (
            mock.patch.object(runtime.subprocess, "Popen", return_value=process),
            mock.patch.object(runtime, "process_stamp", return_value=None),
            self.assertRaisesRegex(RuntimeError, "identity"),
        ):
            runtime.spawn(["unused-test-command"], self.base / "logs" / "test.log")

    def test_owned_process_receives_term_and_no_unnecessary_kill(self):
        owned = {"pid": 12345, "start_ticks": "owned-start"}
        with (
            mock.patch.object(
                runtime, "process_stamp", side_effect=["owned-start", None]
            ),
            mock.patch.object(runtime.os, "killpg") as kill,
        ):
            runtime.kill_owned(owned)
        kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_process_disappearing_before_signal_is_harmless(self):
        owned = {"pid": 12345, "start_ticks": "owned-start"}
        with (
            mock.patch.object(runtime, "process_stamp", return_value="owned-start"),
            mock.patch.object(runtime.os, "killpg", side_effect=ProcessLookupError),
        ):
            runtime.kill_owned(owned)

    def test_no_drive_mount_never_creates_storage_directories(self):
        self.args.ephemeral = False
        with (
            mock.patch.object(runtime.os.path, "ismount", return_value=False),
            self.assertRaisesRegex(RuntimeError, "Drive is not mounted"),
        ):
            runtime.storage_root(self.args)
        self.assertFalse(Path(self.args.storage_root).exists())

    def test_mounted_drive_does_not_allow_an_outside_storage_path(self):
        self.args.ephemeral = False
        with (
            mock.patch.object(runtime.os.path, "ismount", return_value=True),
            self.assertRaisesRegex(ValueError, "inside /content/drive/MyDrive"),
        ):
            runtime.storage_root(self.args)
        self.assertFalse(Path(self.args.storage_root).exists())

    def test_ephemeral_storage_is_explicit_and_probe_is_removed(self):
        with mock.patch.object(
            runtime.os.path, "ismount", side_effect=AssertionError("Drive checked")
        ):
            root = runtime.storage_root(self.args)
        self.assertEqual(root, self.base / "ephemeral-assets")
        self.assertEqual(
            {p.name for p in root.iterdir()}, {"models", "input", "output", "user"}
        )

    def test_mounted_drive_requires_a_dedicated_subdirectory(self):
        self.args.ephemeral = False
        self.args.storage_root = "/content/drive/MyDrive"
        mydrive = self.base / "mock-mydrive"
        mydrive.mkdir()
        with (
            mock.patch.object(runtime.os.path, "ismount", return_value=True),
            mock.patch.object(runtime, "Path", return_value=mydrive),
            self.assertRaisesRegex(ValueError, "dedicated directory"),
        ):
            runtime.storage_root(self.args)
        self.assertEqual(list(mydrive.iterdir()), [])

    def test_existing_owned_service_is_not_replaced(self):
        runtime.write_json(
            runtime.STATE, {"comfyui": {"pid": 12345, "start_ticks": "owned"}}
        )
        with (
            mock.patch.object(runtime, "alive", return_value=True),
            mock.patch.object(runtime, "spawn") as spawn,
            mock.patch.object(runtime, "kill_owned") as kill,
            self.assertRaisesRegex(RuntimeError, "Already running"),
        ):
            runtime.start(self.args)
        spawn.assert_not_called()
        kill.assert_not_called()

    def test_failed_start_scoped_cleanup_preserves_previous_services(self):
        old = {
            "startup_request_id": "previous-request",
            "access_mode": "public",
            "comfyui": {"pid": 12345, "start_ticks": "owned"},
        }
        runtime.write_json(runtime.STATE, old)
        self.args.request_id = "new-request"
        with mock.patch.object(runtime, "kill_owned") as kill:
            result = runtime.stop(self.args)
        self.assertEqual(result["status"], "cleanup_skipped")
        self.assertEqual(runtime.read_json(runtime.STATE), old)
        kill.assert_not_called()

    def test_scoped_cleanup_stops_only_matching_startup_request(self):
        comfy = {"pid": 12345, "start_ticks": "owned"}
        runtime.write_json(
            runtime.STATE, {"startup_request_id": "same-request", "comfyui": comfy}
        )
        self.args.request_id = "same-request"
        with mock.patch.object(runtime, "kill_owned") as kill:
            result = runtime.stop(self.args)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(kill.call_args_list, [mock.call(None), mock.call(comfy)])

    def test_scoped_cleanup_never_stops_legacy_services_without_request_identity(self):
        old = {"comfyui": {"pid": 12345, "start_ticks": "owned"}}
        runtime.write_json(runtime.STATE, old)
        self.args.request_id = "new-request"
        with mock.patch.object(runtime, "kill_owned") as kill:
            self.assertEqual(runtime.stop(self.args)["status"], "cleanup_skipped")
        self.assertEqual(runtime.read_json(runtime.STATE), old)
        kill.assert_not_called()

    def test_unowned_healthy_server_is_not_reused(self):
        with (
            mock.patch.object(runtime, "healthy", return_value=True),
            mock.patch.object(runtime, "port_free", return_value=False),
            mock.patch.object(runtime, "spawn") as spawn,
            self.assertRaisesRegex(RuntimeError, "unowned"),
        ):
            runtime.start(self.args)
        spawn.assert_not_called()

    def test_occupied_port_with_unhealthy_server_is_not_reused(self):
        # Bind only loopback, without accepting connections or serving any data.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            try:
                occupied.bind(("127.0.0.1", 8188))
            except OSError as error:
                self.skipTest(f"Local port 8188 is already in use: {error}")
            occupied.listen(1)
            with (
                mock.patch.object(runtime, "healthy", return_value=False),
                mock.patch.object(runtime, "port_free", side_effect=real_port_free),
                mock.patch.object(runtime, "alive", return_value=False),
                mock.patch.object(
                    runtime,
                    "spawn",
                    return_value={"pid": 12345, "start_ticks": "owned"},
                ) as spawn,
                mock.patch.object(runtime, "kill_owned"),
                self.assertRaises(RuntimeError),
            ):
                runtime.start(self.args)
            spawn.assert_not_called()

    def test_failed_tunnel_start_cleans_only_this_start_processes(self):
        comfy = {"pid": 12345, "start_ticks": "comfy-owned"}
        stale = {"pid": 67890, "start_ticks": "stale-start"}
        runtime.write_json(runtime.STATE, {"comfyui": stale})
        with (
            mock.patch.object(runtime, "alive", side_effect=lambda proc: proc == comfy),
            mock.patch.object(runtime, "healthy", return_value=True),
            mock.patch.object(
                runtime, "spawn", side_effect=[comfy, OSError("Tunnel spawn failed")]
            ),
            mock.patch.object(runtime, "kill_owned") as kill,
            self.assertRaisesRegex(OSError, "Tunnel spawn failed"),
        ):
            runtime.start(self.args)
        self.assertEqual(kill.call_args_list, [mock.call(None), mock.call(comfy)])
        self.assertNotIn(mock.call(stale), kill.call_args_list)
        self.assertEqual(runtime.read_json(runtime.STATE)["status"], "failed")

    def test_successful_start_keeps_owned_services_and_records_url(self):
        comfy = {"pid": 12345, "start_ticks": "comfy-owned"}
        tunnel = {"pid": 67890, "start_ticks": "tunnel-owned"}

        def spawn(argv, log, cwd=None):
            if "--allowed-mail" in argv:
                self.assertEqual(argv[-2:], ["--allowed-mail", "tester@example.com"])
                log.write_text("Created https://unit-test-example.trycloudflare.com\n")
                return tunnel
            return comfy

        with (
            mock.patch.object(runtime, "healthy", return_value=True),
            mock.patch.object(runtime, "alive", side_effect=bool),
            mock.patch.object(runtime, "spawn", side_effect=spawn),
            mock.patch.object(runtime, "kill_owned") as kill,
        ):
            result = runtime.start(self.args)
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://unit-test-example.trycloudflare.com")
        self.assertEqual(runtime.read_json(runtime.STATE)["comfyui"], comfy)
        self.assertEqual(runtime.read_json(runtime.STATE)["cloudflared"], tunnel)
        self.assertEqual(result["access_mode"], "email")
        kill.assert_not_called()

    def test_explicit_public_start_omits_email_gate_and_reports_mode(self):
        self.args.public = True
        self.args.allowed_email = None
        commands = []

        def spawn(argv, log, cwd=None):
            commands.append(argv)
            if "--url" in argv:
                log.write_text("Created https://unit-test-example.trycloudflare.com\n")
                return {"pid": 67890, "start_ticks": "tunnel-owned"}
            return {"pid": 12345, "start_ticks": "comfy-owned"}

        with (
            mock.patch.object(runtime, "healthy", return_value=True),
            mock.patch.object(runtime, "alive", side_effect=bool),
            mock.patch.object(runtime, "spawn", side_effect=spawn),
        ):
            result = runtime.start(self.args)
            status = runtime.status(self.args)
        self.assertTrue(result["ok"])
        self.assertIn("--enable-compress-response-body", commands[0])
        self.assertNotIn("--allowed-mail", commands[-1])
        self.assertEqual(result["access_mode"], "public")
        self.assertEqual(status["access_mode"], "public")
        self.assertEqual(runtime.read_json(runtime.STATE)["access_mode"], "public")

    def test_local_only_uses_vm_models_without_spawning_cloudflare(self):
        self.args.allowed_email = None
        self.args.local_only = True
        with (
            mock.patch.object(runtime, "healthy", return_value=True),
            mock.patch.object(runtime, "alive", side_effect=bool),
            mock.patch.object(
                runtime, "spawn", return_value={"pid": 12345, "start_ticks": "owned"}
            ) as spawn,
        ):
            result = runtime.start(self.args)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(result["access_mode"], "local")
        self.assertIsNone(result["url"])
        state = runtime.read_json(runtime.STATE)
        self.assertIs(state["cpu"], True)
        self.assertEqual(
            runtime.recorded_configuration(state),
            {
                "source": "services",
                "cpu": True,
                "ephemeral": True,
                "storage_root": str(self.base / "ephemeral-assets"),
                "access_mode": "local",
            },
        )
        paths = json.loads((self.base / "model-paths.yaml").read_text())
        self.assertEqual(paths["launcher"]["base_path"], str(self.base / "models"))
        self.assertNotIn("ephemeral-assets", paths["launcher"]["base_path"])
        self.assertEqual(paths["launcher"]["embeddings"], "embeddings")

    def test_embedding_search_path_cannot_redirect_model_loading_to_storage(self):
        model_root = self.base / "models"
        model_root.mkdir()
        external = self.base / "external-model-storage"
        external.mkdir()
        (model_root / "embeddings").symlink_to(external, target_is_directory=True)
        self.args.allowed_email = None
        self.args.local_only = True
        with (
            mock.patch.object(runtime, "spawn") as spawn,
            self.assertRaisesRegex(ValueError, "must not be symlinks"),
        ):
            runtime.start(self.args)
        spawn.assert_not_called()

    def test_configuration_recovery_never_guesses_missing_or_legacy_booleans(self):
        self.assertIsNone(runtime.recorded_configuration({}))
        result = runtime.recorded_configuration(
            {"storage_root": "/content/drive/MyDrive/launcher", "access_mode": "local"}
        )
        self.assertIsNone(result["cpu"])
        self.assertIsNone(result["ephemeral"])
        self.assertEqual(result["access_mode"], "local")
        result = runtime.recorded_configuration(
            {
                "storage_root": "relative/path",
                "cpu": "false",
                "ephemeral": 1,
                "access_mode": "unknown",
            }
        )
        self.assertIsNone(result["storage_root"])
        self.assertIsNone(result["cpu"])
        self.assertIsNone(result["ephemeral"])
        self.assertIsNone(result["access_mode"])

    def test_status_mount_readiness_requires_real_mydrive_and_mount(self):
        mydrive = self.base / "drive" / "MyDrive"

        def path(value):
            return mydrive if value == "/content/drive/MyDrive" else Path(value)

        with (
            mock.patch.object(runtime, "Path", side_effect=path),
            mock.patch.object(runtime, "healthy", return_value=False),
            mock.patch.object(runtime, "local_models_ready", return_value=False),
            mock.patch.object(runtime, "hardware_info", return_value={}),
            mock.patch.object(
                runtime.os.path, "ismount", return_value=False
            ) as mounted,
        ):
            mydrive.mkdir(parents=True)
            self.assertFalse(runtime.status(self.args)["drive"]["mydrive_ready"])
            mounted.return_value = True
            self.assertTrue(runtime.status(self.args)["drive"]["mydrive_ready"])
            mydrive.rmdir()
            self.assertFalse(runtime.status(self.args)["drive"]["mydrive_ready"])
            outside = self.base / "outside-drive"
            outside.mkdir()
            mydrive.symlink_to(outside, target_is_directory=True)
            self.assertFalse(runtime.status(self.args)["drive"]["mydrive_ready"])

    def test_fresh_missing_install_status_has_no_fabricated_configuration(self):
        (self.base / "install.json").unlink()
        with (
            mock.patch.object(runtime, "healthy", return_value=False),
            mock.patch.object(runtime, "local_models_ready", return_value=False),
            mock.patch.object(runtime, "hardware_info", return_value={}),
            mock.patch.object(runtime.os.path, "ismount", return_value=False),
        ):
            result = runtime.status(self.args)
        self.assertIsNone(result["configuration"])
        self.assertEqual(result["installation"], {})
        self.assertFalse(result["comfyui_alive"])

    def test_gpu_start_requires_verified_local_models(self):
        self.args.cpu = False
        with (
            mock.patch.object(runtime, "healthy", return_value=False),
            mock.patch.object(runtime, "port_free", return_value=True),
            mock.patch.object(runtime, "local_models_ready", return_value=False),
            mock.patch.object(runtime, "spawn") as spawn,
            self.assertRaisesRegex(RuntimeError, "run prepare"),
        ):
            runtime.start(self.args)
        spawn.assert_not_called()

    def test_access_mode_cannot_be_implicit_or_conflicting(self):
        for public, email in ((False, None), (True, "tester@example.com")):
            self.args.public = public
            self.args.allowed_email = email
            with (
                self.subTest(public=public, email=email),
                mock.patch.object(runtime, "spawn") as spawn,
                mock.patch.object(runtime, "storage_root") as storage,
                self.assertRaisesRegex(ValueError, "exactly one"),
            ):
                runtime.start(self.args)
            spawn.assert_not_called()
            storage.assert_not_called()

    def test_cli_start_requires_one_access_mode(self):
        for arguments in (
            ("start",),
            ("start", "--public", "--allowed-email", "tester@example.com"),
        ):
            with (
                self.subTest(arguments=arguments),
                mock.patch.object(runtime.sys, "argv", ["runtime.py", *arguments]),
                mock.patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                runtime.main()
            self.assertEqual(raised.exception.code, 2)

    def test_stop_ignores_reused_pids_and_removes_url(self):
        runtime.write_json(
            runtime.STATE,
            {
                "comfyui": {"pid": 12345, "start_ticks": "old-start"},
                "cloudflared": {"pid": 67890, "start_ticks": "old-start"},
                "url": "https://unit-test-example.trycloudflare.com",
            },
        )
        with (
            mock.patch.object(runtime, "process_stamp", return_value="new-start"),
            mock.patch.object(runtime.os, "killpg") as kill,
        ):
            result = runtime.stop(self.args)
        self.assertTrue(result["runtime_still_running"])
        self.assertNotIn("url", runtime.read_json(runtime.STATE))
        kill.assert_not_called()


class BridgeResultTests(unittest.TestCase):
    def test_exec_exit_zero_with_error_sentinel_is_a_failure(self):
        result = subprocess.CompletedProcess(
            ["colab", "exec"],
            0,
            stdout='LAUNCHER_RESULT={"ok": false, "error": "remote operation failed"}\n',
            stderr="",
        )
        with (
            mock.patch.object(bridge, "_run_colab", return_value=result),
            self.assertRaisesRegex(bridge.BridgeError, "remote operation failed"),
        ):
            bridge.execute_remote("existing-test-session", {"action": "status"}, 1)

    def test_success_sentinel_does_not_override_cli_failure(self):
        result = subprocess.CompletedProcess(
            ["colab", "exec"],
            1,
            stdout='LAUNCHER_RESULT={"ok": true}\n',
            stderr="CLI failed",
        )
        with (
            mock.patch.object(bridge, "_run_colab", return_value=result),
            self.assertRaisesRegex(bridge.BridgeError, "exited unsuccessfully"),
        ):
            bridge.execute_remote("existing-test-session", {"action": "status"}, 1)

    def test_missing_duplicate_and_invalid_sentinels_fail_closed(self):
        for output in (
            "Some ordinary CLI output\n",
            'LAUNCHER_RESULT={"ok": true}\nLAUNCHER_RESULT={"ok": true}\n',
            'LAUNCHER_RESULT={"ok": "true"}\n',
            'LAUNCHER_RESULT={"ok": 1}\n',
            "LAUNCHER_RESULT=[]\n",
            "LAUNCHER_RESULT=not-json\n",
        ):
            with self.subTest(output=output), self.assertRaises(bridge.BridgeError):
                bridge.parse_result(output)

    def test_valid_sentinel_amid_cli_noise_is_accepted(self):
        result = bridge.parse_result(
            '[colab] executing\nLAUNCHER_RESULT={"ok": true, "status": "ready"}\n'
        )
        self.assertEqual(result["status"], "ready")

    def test_remote_runtime_error_cannot_hide_behind_returncode_zero(self):
        namespace = {}
        # Execute this repository's trusted helper constant, never remote/user input.
        exec(bridge.REMOTE_HELPERS, namespace)  # noqa: S102
        result = subprocess.CompletedProcess(
            ["python", "runtime.py"],
            0,
            stdout=json.dumps({"ok": False, "error": "Runtime rejected this action"}),
            stderr="",
        )
        with (
            mock.patch.object(namespace["Path"], "is_file", return_value=True),
            mock.patch.object(namespace["subprocess"], "run", return_value=result),
            self.assertRaisesRegex(RuntimeError, "Runtime rejected"),
        ):
            namespace["runtime_action"]({"action": "status"})

    def test_control_actions_work_before_venv_without_changing_model_actions(self):
        namespace = {}
        exec(bridge.REMOTE_HELPERS, namespace)  # noqa: S102
        venv_python = "/content/colab-comfyui-runtime/.venv/bin/python"
        result = subprocess.CompletedProcess(
            ["python", "runtime.py"], 0, stdout='{"ok": true}', stderr=""
        )
        for installed in (False, True):
            for action in ("status", "stop", "start", "smoke"):
                with (
                    self.subTest(installed=installed, action=action),
                    mock.patch.object(
                        namespace["Path"],
                        "is_file",
                        autospec=True,
                        side_effect=lambda path, installed=installed: (
                            str(path).endswith("/scripts/runtime.py") or installed
                        ),
                    ),
                    mock.patch.object(
                        namespace["subprocess"], "run", return_value=result
                    ) as run,
                ):
                    payload = {
                        "action": action,
                        "ephemeral": True,
                        "allowed_email": "tester@example.invalid",
                        "cpu": True,
                    }
                    namespace["runtime_result"](payload)
                    expected = (
                        namespace["sys"].executable
                        if not installed and action in ("status", "stop")
                        else venv_python
                    )
                    self.assertEqual(run.call_args.args[0][0], expected)


if __name__ == "__main__":
    unittest.main()
