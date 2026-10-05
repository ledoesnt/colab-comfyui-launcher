"""Dashboard semantics using mocks only; never connect to a provider."""

import importlib.util
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dashboard", ROOT / "scripts/dashboard.py"
)
dashboard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboard
spec.loader.exec_module(dashboard)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.run = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', "")
        )
        self.backend = dashboard.Backend(self.run)
        self.config = dashboard.Config(session="chosen-existing-session")

    def command(self):
        return self.run.call_args.args[0]

    def test_start_access_and_storage_are_explicit_argv(self):
        self.backend.bridge(self.config, "start")
        argv = self.command()
        self.assertIn("--local-only", argv)
        self.assertNotIn("--public", argv)
        self.assertEqual(
            argv[argv.index("--storage-root") + 1], dashboard.DEFAULT_STORAGE
        )
        self.config.access, self.config.cpu, self.config.ephemeral = (
            "public",
            True,
            True,
        )
        self.backend.bridge(self.config, "start")
        self.assertIn("--public", self.command())
        self.assertIn("--cpu", self.command())
        self.assertIn("--ephemeral", self.command())
        self.assertNotIn("--storage-root", self.command())

    def test_prepare_reuses_existing_drive_cache(self):
        self.config.verify_cache = True
        self.backend.bridge(self.config, "prepare")
        argv = self.command()
        self.assertEqual(
            argv[argv.index("--cache-root") + 1], dashboard.DEFAULT_STORAGE + "/models"
        )
        self.assertIn("--verify-cache", argv)
        self.assertEqual(argv[argv.index("--max-seconds") + 1], "1800")
        self.assertNotIn("--download-missing", argv)
        self.backend.bridge(self.config, "prepare", download_missing=True)
        self.assertIn("--download-missing", self.command())
        self.config.ephemeral = True
        with self.assertRaisesRegex(dashboard.DashboardError, "persistent"):
            self.backend.bridge(self.config, "prepare")

    def test_download_and_email_are_not_shell_interpolated(self):
        self.config.storage_root += "/space and $(not-a-shell)"
        self.backend.bridge(self.config, "download")
        argv = self.command()
        self.assertEqual(
            argv[argv.index("--models-root") + 1], self.config.storage_root + "/models"
        )
        self.assertNotIn("shell", self.run.call_args.kwargs)
        self.config.access, self.config.email = "email", "allowed@example.com"
        self.backend.bridge(self.config, "start")
        argv = self.command()
        self.assertEqual(argv[argv.index("--allowed-email") + 1], self.config.email)
        self.assertNotIn("--public", argv)

    def test_ssh_key_creation_only_when_explicit(self):
        self.config.identity = "/tmp/example key path"
        self.backend.ssh(self.config, "start")
        self.assertNotIn("--create-key", self.command())
        self.config.create_key = True
        self.backend.ssh(self.config, "start")
        self.assertIn("--create-key", self.command())
        self.backend.ssh(self.config, "status")
        self.assertNotIn("--create-key", self.command())
        self.assertEqual(
            self.command()[self.command().index("--identity") + 1], self.config.identity
        )

    def test_json_exit_code_and_ok_must_both_succeed(self):
        for code, output in (
            (1, '{"ok":true}'),
            (0, '{"ok":false}'),
            (0, '{"ok":1}'),
            (0, "[]"),
            (0, '{"ok":true}\n{"ok":true}'),
        ):
            with self.subTest(code=code, output=output):
                self.run.return_value = subprocess.CompletedProcess(
                    [], code, output, "failure"
                )
                with self.assertRaises(dashboard.DashboardError):
                    self.backend.bridge(self.config, "status")

    def test_timeout_is_unknown_without_mutation_retry(self):
        self.run.side_effect = subprocess.TimeoutExpired(["fixture"], 1)
        with self.assertRaisesRegex(dashboard.DashboardError, "unknown"):
            self.backend.bridge(self.config, "install")
        self.run.assert_called_once()

    def test_official_authentication_inherits_tty(self):
        self.assertEqual(
            self.backend.interactive(["drivemount", "-s", self.config.session]), 0
        )
        self.assertEqual(
            self.command(),
            ["colab", "--auth=oauth2", "drivemount", "-s", self.config.session],
        )
        self.assertNotIn("capture_output", self.run.call_args.kwargs)
        self.assertNotIn("stdin", self.run.call_args.kwargs)
        self.assertNotIn("stdout", self.run.call_args.kwargs)

    def test_invalid_session_and_cpu_render_do_not_call_commands(self):
        for name in ("", "--other-session", "two sessions", "bad\nname"):
            self.config.session = name
            with self.assertRaises(dashboard.DashboardError):
                self.backend.bridge(self.config, "stop")
        self.config.session, self.config.cpu = "chosen-session", True
        with self.assertRaisesRegex(dashboard.DashboardError, "GPU"):
            self.backend.bridge(self.config, "render")
        self.run.assert_not_called()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.backend = mock.Mock()
        self.backend.bridge.return_value = {"ok": True}
        self.backend.ssh.return_value = {"ok": True, "running": False}
        self.worker = dashboard.Worker(self.backend)
        self.addCleanup(self.worker.close)
        self.config = dashboard.Config(session="only-selected-session")

    def finish(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not self.worker.busy:
                return
            time.sleep(0.005)
        self.fail("Fixture worker did not finish")

    def events(self):
        events = []
        while not self.worker.events.empty():
            events.append(self.worker.events.get_nowait())
        return events

    def test_refresh_cannot_overlap_an_action_and_config_is_copied(self):
        entered, resume = threading.Event(), threading.Event()

        def block(config, action):
            entered.set()
            resume.wait(2)
            return {"ok": True}

        self.backend.bridge.side_effect = block
        self.assertTrue(self.worker.submit("install", self.config))
        self.assertTrue(entered.wait(1))
        self.config.session = "changed-after-submit"
        self.assertFalse(self.worker.submit("status", self.config))
        self.assertFalse(self.worker.submit("render", self.config))
        resume.set()
        self.finish()
        self.assertEqual(
            self.backend.bridge.call_args_list[0].args[0].session,
            "only-selected-session",
        )

    def test_one_manual_render_waits_for_status_without_overlap_or_duplicate(self):
        entered, resume = threading.Event(), threading.Event()
        observed = []

        def command(config, action):
            observed.append((action, config.session))
            if len(observed) == 1:
                entered.set()
                resume.wait(2)
            return {"ok": True}

        self.backend.bridge.side_effect = command
        self.worker.submit("status", self.config)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.worker.submit("render", self.config))
        self.assertFalse(self.worker.submit("render", self.config))
        self.assertFalse(self.worker.submit("stop", self.config))
        self.assertFalse(self.worker.submit("status", self.config))
        self.assertTrue(self.worker.busy)
        self.config.session = "changed-after-render-was-queued"
        self.assertEqual(observed, [("status", "only-selected-session")])
        resume.set()
        self.finish()
        self.assertEqual(
            observed,
            [
                ("status", "only-selected-session"),
                ("render", "only-selected-session"),
                ("status", "only-selected-session"),
            ],
        )

    def test_quit_discards_pending_action_after_current_status_finishes(self):
        entered, resume = threading.Event(), threading.Event()

        def command(config, action):
            entered.set()
            resume.wait(2)
            return {"ok": True}

        self.backend.bridge.side_effect = command
        self.worker.submit("status", self.config)
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.worker.submit("render", self.config))
        self.worker.close()
        resume.set()
        self.worker.thread.join(1)
        self.assertFalse(self.worker.thread.is_alive())
        self.assertEqual(
            [call.args[1] for call in self.backend.bridge.call_args_list], ["status"]
        )

    def test_release_wakes_pipeline_wait_and_cleans_once_without_starting_services(
        self,
    ):
        waiting, cleanup_started, cleanup_resume = (threading.Event() for _ in range(3))
        statuses = 0

        def command(config, action, **kwargs):
            nonlocal statuses
            if action == "status":
                statuses += 1
                if statuses == 1:
                    return {"ok": True, "installation": {"status": "ready"}}
                if statuses == 2:
                    return {
                        "ok": True,
                        "model_prepare": {"status": "running", "running": True},
                    }
            if action == "stop":
                cleanup_started.set()
                cleanup_resume.wait(2)
            return {"ok": True}

        original_wait = self.worker.operation_cancelled.wait

        def wait_for_cleanup(seconds):
            waiting.set()
            return original_wait(2)

        self.backend.bridge.side_effect = command
        with mock.patch.object(
            self.worker.operation_cancelled, "wait", side_effect=wait_for_cleanup
        ):
            self.worker.submit("pipeline", self.config)
            self.assertTrue(waiting.wait(1))
            self.assertFalse(self.worker.submit("render", self.config))
            self.assertTrue(self.worker.submit("release", self.config))
            self.assertTrue(
                cleanup_started.wait(1), "Cleanup did not wake the pipeline wait"
            )
            self.assertFalse(self.worker.submit("release", self.config))
            self.assertFalse(self.worker.submit("stop", self.config))
            self.assertFalse(self.worker.submit("render", self.config))
            cleanup_resume.set()
            self.finish()
        actions = [call.args[1] for call in self.backend.bridge.call_args_list]
        self.assertEqual(
            actions,
            ["deploy", "install", "status", "prepare", "status", "stop", "status"],
        )
        self.backend.ssh.assert_called_once()
        self.assertEqual(self.backend.ssh.call_args.args[1], "stop")
        releases = [config for kind, config in self.events() if kind == "release"]
        self.assertEqual(
            [config.session for config in releases], ["only-selected-session"]
        )
        self.assertFalse(self.worker.cancelled.is_set())

    def test_pipeline_cleanup_cannot_target_another_session_or_run_after_quit(self):
        entered, resume = threading.Event(), threading.Event()

        def command(config, action, **kwargs):
            entered.set()
            resume.wait(2)
            return {"ok": True}

        self.backend.bridge.side_effect = command
        self.worker.submit("pipeline", self.config)
        self.assertTrue(entered.wait(1))
        other = dashboard.Config(session="unrelated-session")
        self.assertFalse(self.worker.submit("release", other))
        self.assertTrue(self.worker.submit("stop", self.config))
        self.assertFalse(self.worker.submit("release", self.config))
        self.worker.close()
        resume.set()
        self.worker.thread.join(1)
        self.assertFalse(self.worker.thread.is_alive())
        self.assertEqual(
            [call.args[1] for call in self.backend.bridge.call_args_list], ["deploy"]
        )
        self.backend.ssh.assert_not_called()

    def test_full_flow_waits_and_does_not_duplicate_mutations(self):
        statuses = iter(
            [
                {"ok": True, "installation": {"status": "installing"}},
                {"ok": True, "installation": {"status": "ready"}},
                {"ok": True, "model_prepare": {"status": "running", "running": True}},
                {
                    "ok": True,
                    "model_prepare": {"status": "succeeded", "running": False},
                },
                {
                    "ok": True,
                    "http_ready": True,
                    "comfyui_alive": True,
                    "tunnel_alive": False,
                    "access_mode": "local",
                    "startup": {
                        "ok": True,
                        "status": "started",
                        "access_mode": "local",
                        "progress": {"status": "ready", "phase": "local_only"},
                    },
                },
            ]
        )
        self.backend.bridge.side_effect = lambda config, action, **kwargs: (
            next(statuses) if action == "status" else {"ok": True}
        )
        with mock.patch.object(dashboard, "REFRESH_SECONDS", 0.001):
            self.worker.submit("pipeline", self.config)
            self.finish()
        actions = [call.args[1] for call in self.backend.bridge.call_args_list]
        self.assertEqual(
            [action for action in actions if action != "status"],
            ["deploy", "install", "prepare", "start"],
        )
        prepare_call = next(
            call
            for call in self.backend.bridge.call_args_list
            if call.args[1] == "prepare"
        )
        self.assertEqual(prepare_call.kwargs, {"download_missing": True})
        self.backend.ssh.assert_called_once()
        self.assertEqual(self.backend.ssh.call_args.args[1], "start")
        self.assertTrue(any(kind == "status" for kind, _ in self.events()))

    def test_cpu_full_flow_skips_models_and_public_needs_tunnel_url(self):
        self.config.cpu, self.config.access = True, "public"
        statuses = iter(
            [
                {"ok": True, "installation": {"status": "ready"}},
                {
                    "ok": True,
                    "http_ready": True,
                    "comfyui_alive": True,
                    "tunnel_alive": True,
                    "access_mode": "public",
                    "startup": {
                        "ok": True,
                        "status": "started",
                        "access_mode": "public",
                        "progress": {"status": "ready", "phase": "cloudflare"},
                    },
                },
                {
                    "ok": True,
                    "http_ready": True,
                    "comfyui_alive": True,
                    "tunnel_alive": True,
                    "url": "https://unit-test-example.trycloudflare.com",
                    "access_mode": "public",
                    "startup": {
                        "ok": True,
                        "status": "started",
                        "access_mode": "public",
                        "progress": {"status": "ready", "phase": "cloudflare"},
                    },
                },
            ]
        )
        self.backend.bridge.side_effect = lambda config, action: (
            next(statuses) if action == "status" else {"ok": True}
        )
        with mock.patch.object(dashboard, "REFRESH_SECONDS", 0.001):
            self.worker.submit("pipeline", self.config)
            self.finish()
        actions = [call.args[1] for call in self.backend.bridge.call_args_list]
        self.assertNotIn("prepare", actions)
        self.assertEqual(actions.count("status"), 3)
        self.backend.ssh.assert_not_called()

    def test_old_healthy_services_cannot_hide_start_failure_or_wrong_mode(self):
        self.config.cpu = True
        scenarios = [
            (
                {"ok": False, "status": "failed", "error": "Already running"},
                "public",
                True,
                "Already running",
            ),
            (
                {
                    "ok": True,
                    "status": "started",
                    "access_mode": "local",
                    "result": {"ok": False, "error": "supervisor failed"},
                },
                "local",
                False,
                "supervisor failed",
            ),
            (
                {"ok": True, "status": "started", "access_mode": "public"},
                "public",
                True,
                "access mode",
            ),
        ]
        for startup, mode, tunnel, error in scenarios:
            with self.subTest(startup=startup):
                self.backend.reset_mock()
                statuses = iter(
                    [
                        {"ok": True, "installation": {"status": "ready"}},
                        {
                            "ok": True,
                            "http_ready": True,
                            "comfyui_alive": True,
                            "tunnel_alive": tunnel,
                            "url": "https://unit-test-example.trycloudflare.com",
                            "access_mode": mode,
                            "startup": startup,
                        },
                    ]
                )
                self.backend.bridge.side_effect = (
                    lambda config, action, statuses=statuses: (
                        next(statuses) if action == "status" else {"ok": True}
                    )
                )
                self.worker.submit("pipeline", self.config)
                self.finish()
                self.assertEqual(
                    [call.args[1] for call in self.backend.bridge.call_args_list],
                    ["deploy", "install", "status", "start", "status"],
                )
                self.backend.ssh.assert_not_called()
                self.assertTrue(
                    any(
                        kind == "error" and error in message
                        for kind, message in self.events()
                    )
                )

    def test_startup_unknown_or_local_tunnel_cannot_be_reported_ready(self):
        base = {
            "ok": True,
            "http_ready": True,
            "comfyui_alive": True,
            "tunnel_alive": False,
            "access_mode": "local",
        }
        for changes in (
            {},
            {"startup": {"ok": True, "status": "starting", "access_mode": "local"}},
            {"startup": {"ok": True, "status": "started"}},
            {
                "startup": {"ok": True, "status": "started", "access_mode": "local"},
                "tunnel_alive": True,
            },
            {
                "startup": {"ok": True, "status": "started", "access_mode": "local"},
                "tunnel_alive": None,
            },
        ):
            with self.subTest(changes=changes):
                self.backend.bridge.return_value = {**base, **changes}
                worker = dashboard.Worker(self.backend, clock=iter([0, 2]).__next__)
                self.addCleanup(worker.close)
                with self.assertRaisesRegex(dashboard.DashboardError, "unknown"):
                    worker._wait(self.config, "startup", 1)
        self.backend.ssh.assert_not_called()

    def test_failure_stops_pipeline_without_start_or_retry(self):
        self.backend.bridge.side_effect = lambda config, action: (
            {"ok": True, "installation": {"status": "failed"}}
            if action == "status"
            else {"ok": True}
        )
        self.worker.submit("pipeline", self.config)
        self.finish()
        self.assertEqual(
            [call.args[1] for call in self.backend.bridge.call_args_list],
            ["deploy", "install", "status"],
        )
        self.assertTrue(any(kind == "error" for kind, _ in self.events()))

    def test_unknown_wait_timeout_does_not_restart_install(self):
        worker = dashboard.Worker(self.backend, clock=iter([0, 1000]).__next__)
        self.addCleanup(worker.close)
        with self.assertRaisesRegex(dashboard.DashboardError, "unknown"):
            worker._wait(self.config, "installation", 960)
        self.assertEqual(
            [call.args[1] for call in self.backend.bridge.call_args_list], ["status"]
        )

    def test_quit_does_not_cleanup_and_rejects_new_actions(self):
        self.worker.close()
        self.assertFalse(self.worker.submit("stop", self.config))
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()
        self.backend.interactive.assert_not_called()

    def test_release_event_is_for_selected_session_even_if_cleanup_fails(self):
        self.backend.bridge.side_effect = dashboard.DashboardError("not deployed")
        self.worker.submit("release", self.config)
        self.finish()
        releases = [value for kind, value in self.events() if kind == "release"]
        self.assertEqual(
            [value.session for value in releases], ["only-selected-session"]
        )
        self.backend.interactive.assert_not_called()


class DisplayTests(unittest.TestCase):
    def app(self):
        app = dashboard.Dashboard(
            dashboard.Config(session="fixture-session"), mock.Mock()
        )
        self.addCleanup(app.worker.close)
        return app

    def test_file_progress_includes_phase_size_and_rate(self):
        status = {
            "model_prepare": {
                "status": "running",
                "running": True,
                "progress": {
                    "files": [
                        {
                            "path": "checkpoints/fixture.safetensors",
                            "phase": "copy",
                            "done_bytes": 1024,
                            "total_bytes": 2048,
                            "rate_bytes_per_second": 512,
                        }
                    ]
                },
            }
        }
        lines = dashboard.progress_lines(status)
        self.assertTrue(
            any(
                "50% 1.0KiB/2.0KiB 512.0B/s copy fixture.safetensors" in line
                for line in lines
            )
        )

    def test_active_file_progress_is_shown_before_idle_jobs(self):
        lines = dashboard.progress_lines(
            {"model_prepare": {"status": "running", "running": True}}
        )
        self.assertTrue(lines[0].startswith("Prepare: running"))

    def test_urls_only_open_after_owned_services_are_ready(self):
        app = self.app()
        app.ssh = {
            "url": "http://127.0.0.1:8188",
            "running": False,
            "http_ready": False,
        }
        self.assertIsNone(app._url())
        app.ssh.update(running=True, http_ready=True)
        self.assertEqual(app._url(), "http://127.0.0.1:8188")
        app.config.access = "public"
        app.status = {
            "url": "https://unit-test-example.trycloudflare.com",
            "http_ready": True,
        }
        self.assertIsNone(app._url())
        app.status["tunnel_alive"] = True
        self.assertEqual(app._url(), "https://unit-test-example.trycloudflare.com")

    def test_small_terminal_keeps_cleanup_and_all_action_keys_visible(self):
        app = self.app()
        app.updated = time.monotonic() - 25
        screen = mock.Mock()
        screen.getmaxyx.return_value = (24, 80)
        app._draw(screen)
        rendered = "\n".join(
            call.args[2][: call.args[3]] for call in screen.addnstr.call_args_list
        )
        for key in ("[f]", "[p]", "[h]", "[r]", "[s]", "[X]", "[q]"):
            self.assertIn(key, rendered)
        self.assertIn("[STALE]", rendered)
        self.assertIn("keep resources", rendered)

    def test_long_model_names_preserve_metrics_at_80_columns(self):
        path = "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"
        item = {
            "path": path,
            "phase": "copying",
            "done_bytes": 952 * 1024**2,
            "total_bytes": 21 * 1024**3,
            "rate_bytes_per_second": 50 * 1024**2,
        }
        status = {"model_prepare": {"running": True, "progress": {"files": [item]}}}
        line = next(
            line for line in dashboard.progress_lines(status) if "copying" in line
        )
        self.assertLessEqual(len(line), 79)
        self.assertIn("4%", line)
        self.assertIn("952.0MiB/21.0GiB", line)
        self.assertIn("50.0MiB/s", line)
        self.assertIn("minimax_h3", line)
        self.assertTrue(line.endswith("int8_convrot"))

    def test_detail_view_shows_four_files_without_hiding_cleanup_controls(self):
        app = self.app()
        app.show_menu = False
        app.status = {
            "model_prepare": {
                "running": True,
                "progress": {
                    "files": [
                        {
                            "path": f"model-{index}.safetensors",
                            "phase": "copy",
                            "done_bytes": 1,
                            "total_bytes": 2,
                            "rate_bytes_per_second": 1,
                        }
                        for index in range(4)
                    ]
                },
            }
        }
        screen = mock.Mock()
        screen.getmaxyx.return_value = (24, 80)
        app._draw(screen)
        rendered = "\n".join(
            call.args[2][: call.args[3]] for call in screen.addnstr.call_args_list
        )
        for index in range(4):
            self.assertIn(f"model-{index}.safetensors", rendered)
        self.assertIn("X: release", rendered)
        self.assertIn("q: keep", rendered)

    def test_full_flow_missing_ssh_key_fails_before_any_model_work(self):
        app = self.app()
        with (
            mock.patch(
                "builtins.input", side_effect=["", "local-only", "no", "", "", "no"]
            ),
            mock.patch("builtins.print"),
            mock.patch.object(dashboard.Path, "exists", return_value=False),
            self.assertRaisesRegex(dashboard.DashboardError, "No SSH key"),
        ):
            app._full_inputs()
        app.backend.interactive.assert_not_called()
        app.backend.bridge.assert_not_called()

    def test_ssh_settings_change_checks_live_ownership_with_original_config(self):
        for new_port, new_identity in (("8189", ""), ("", "/tmp/fixture-new-key")):
            with self.subTest(port=new_port, identity=new_identity):
                app = self.app()
                app.config.identity = "/tmp/fixture-original-key"
                app.ssh = {"running": False}  # Deliberately stale UI snapshot.
                app.backend.ssh.return_value = {"ok": True, "running": True}
                with (
                    mock.patch(
                        "builtins.input",
                        side_effect=["", "", "", new_port, new_identity],
                    ),
                    mock.patch("builtins.print"),
                    self.assertRaisesRegex(dashboard.DashboardError, "press H"),
                ):
                    app._configure()
                self.assertEqual(app.config.local_port, 8188)
                self.assertEqual(app.config.identity, "/tmp/fixture-original-key")
                app.backend.ssh.assert_called_once_with(app.config, "status")
                self.assertEqual(app.backend.ssh.call_args.args[0].local_port, 8188)

    def test_unknown_ssh_ownership_keeps_settings_and_known_stopped_allows_change(self):
        app = self.app()
        app.backend.ssh.return_value = {"ok": True}
        with (
            mock.patch("builtins.input", side_effect=["", "", "", "8189", ""]),
            mock.patch("builtins.print"),
            self.assertRaisesRegex(dashboard.DashboardError, "unknown"),
        ):
            app._configure()
        self.assertEqual(app.config.local_port, 8188)
        app.backend.ssh.return_value = {"ok": True, "running": False}
        with (
            mock.patch("builtins.input", side_effect=["", "", "", "8189", "", ""]),
            mock.patch("builtins.print"),
            mock.patch.object(dashboard.Path, "exists", return_value=True),
        ):
            app._configure()
        self.assertEqual(app.config.local_port, 8189)

    def test_ssh_status_failure_does_not_modify_port(self):
        app = self.app()
        app.backend.ssh.side_effect = dashboard.DashboardError("fixture state rejected")
        with (
            mock.patch("builtins.input", side_effect=["", "", "", "8189", ""]),
            mock.patch("builtins.print"),
            self.assertRaisesRegex(dashboard.DashboardError, "press H"),
        ):
            app._configure()
        self.assertEqual(app.config.local_port, 8188)

    def test_release_executes_only_selected_session_official_stop(self):
        app = self.app()
        app.backend.interactive.return_value = 0
        with mock.patch.object(
            app, "_terminal", side_effect=lambda screen, operation: operation()
        ):
            app._release(mock.Mock(), app.config)
        app.backend.interactive.assert_called_once_with(
            ["stop", "-s", "fixture-session"]
        )
        self.assertEqual(app.config.session, "")

    def test_failures_redact_urls_email_and_credential_text(self):
        error = dashboard.safe_error(
            "https://fixture.invalid/auth?code=private allowed@example.com bearer secret-value"
        )
        self.assertNotIn("fixture.invalid", error)
        self.assertNotIn("allowed@example.com", error)
        self.assertNotIn("secret-value", error)

    def test_help_exits_without_tty_or_provider_calls(self):
        with (
            mock.patch.object(dashboard.sys, "stdout"),
            self.assertRaises(SystemExit) as raised,
        ):
            dashboard.main(["--help"])
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
