"""Wizard orchestration contracts with local fixtures and no provider access."""

import importlib.util
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "wizard_pipeline_dashboard", ROOT / "scripts/dashboard.py"
)
dashboard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboard
spec.loader.exec_module(dashboard)


def ready_status(config=None):
    config = config or dashboard.Config(session="selected", ephemeral=True)
    mode = "local" if config.access == "local-only" else config.access
    return {
        "deployed": True,
        "configuration": {
            "ephemeral": config.ephemeral,
            "cpu": config.cpu,
            "storage_root": None if config.ephemeral else config.storage_root,
            "access_mode": mode,
        },
        "drive": {"mounted": True, "mydrive_ready": True},
        "installation": {"status": "ready"},
        "models_ready": True,
        "model_search_categories": sorted(dashboard.MODEL_SEARCH_CATEGORIES),
        "comfyui_alive": True,
        "http_ready": True,
        "access_mode": mode,
        "tunnel_alive": mode != "local",
        "url": None if mode == "local" else "https://fixture.example",
        "startup": {"ok": True, "status": "started", "access_mode": mode},
    }


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.backend = dashboard.Backend()
        self.backend.official = mock.Mock(
            return_value="[selected] VM | Hardware: G4 | Status: READY"
        )
        self.backend.bridge = mock.Mock(return_value=ready_status())
        self.backend.ssh = mock.Mock(
            return_value={"found": False, "running": True, "http_ready": True}
        )
        self.config = dashboard.Config(session="selected", cpu=True)

    def test_known_configuration_restores_typed_values_without_mutating_input(self):
        status = ready_status()
        status["configuration"].update(cpu=False, ephemeral=True)
        self.backend.bridge.return_value = status
        inspected = self.backend.inspect(self.config)
        restored = inspected["config"]
        self.assertIsNot(restored, self.config)
        self.assertTrue(restored.ephemeral)
        self.assertFalse(restored.cpu)
        self.assertEqual(restored.gpu, "G4")
        self.assertEqual(restored.access, "local-only")
        self.assertTrue(inspected["configuration_known"])
        self.assertTrue(inspected["ready"])
        self.assertFalse(self.config.ephemeral)
        self.assertTrue(self.config.cpu)
        self.backend.official.assert_called_once_with(["status", "-s", "selected"])
        self.assertEqual(self.backend.bridge.call_args.args[1], "status")
        self.assertEqual(self.backend.ssh.call_args.args[1], "status")
        self.assertEqual(
            [call.args[1] for call in self.backend.ssh.call_args_list],
            ["discover", "status"],
        )
        self.assertEqual(restored.local_port, self.config.local_port)
        self.assertEqual(restored.identity, self.config.identity)

    def test_owned_forward_restores_port_and_identity_without_mutating_input(self):
        self.config.identity = "/home/le/.ssh/previous-choice"
        self.config.create_key = True
        before = dashboard.replace(self.config)
        identity = "/home/le/.ssh/owned-forward"
        self.backend.ssh.side_effect = [
            {
                "found": True,
                "session": "selected",
                "local_port": 8189,
                "remote_port": 8188,
                "identity": identity,
            },
            {
                "running": True,
                "http_ready": True,
                "url": "http://127.0.0.1:8189",
            },
        ]
        result = self.backend.inspect(self.config)
        restored = result["config"]
        self.assertTrue(result["ready"])
        self.assertEqual(restored.local_port, 8189)
        self.assertEqual(restored.identity, identity)
        self.assertFalse(restored.create_key)
        self.assertEqual(self.config, before)
        discovery, verification = self.backend.ssh.call_args_list
        self.assertEqual(discovery.args[1], "discover")
        self.assertEqual(discovery.args[0].local_port, 8188)
        self.assertEqual(discovery.args[0].identity, before.identity)
        self.assertEqual(verification, mock.call(restored, "status"))
        self.assertEqual(self.backend.bridge.call_args.args[1], "status")

    def test_invalid_owned_forward_metadata_cannot_fall_back_or_start(self):
        valid = {
            "found": True,
            "session": "selected",
            "local_port": 8189,
            "remote_port": 8188,
            "identity": "/home/le/.ssh/owned-forward",
        }
        invalid_fields = (
            {"session": "different-runtime"},
            {"local_port": "8189"},
            {"local_port": True},
            {"local_port": 1023},
            {"local_port": 65536},
            {"remote_port": 8190},
            {"identity": None},
            {"identity": "relative-key"},
            {"identity": "/home/le/.ssh/invalid\x00key"},
            {"identity": "/home/le/.ssh/invalid\rkey"},
            {"identity": "/home/le/.ssh/invalid\nkey"},
            {"identity": "/home/le/.ssh/invalid%key"},
            {"identity": "/home/le/.ssh/invalid$key"},
        )
        before = dashboard.replace(self.config)
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                self.backend.ssh.reset_mock()
                self.backend.ssh.return_value = {**valid, **fields}
                with self.assertRaisesRegex(dashboard.DashboardError, "SSH settings"):
                    self.backend.inspect(self.config)
                self.assertEqual(self.backend.ssh.call_count, 1)
                self.assertEqual(self.backend.ssh.call_args.args[1], "discover")
                self.assertEqual(self.config, before)
        self.assertEqual(
            [call.args[1] for call in self.backend.bridge.call_args_list],
            ["status"] * len(invalid_fields),
        )

    def test_ambiguous_live_forward_discovery_does_not_fall_back_to_default_port(self):
        error = dashboard.DashboardError("Multiple live owned SSH forwards found")
        self.backend.ssh.side_effect = error
        before = dashboard.replace(self.config)
        with self.assertRaises(dashboard.DashboardError) as caught:
            self.backend.inspect(self.config)
        self.assertIs(caught.exception, error)
        self.backend.ssh.assert_called_once()
        self.assertEqual(self.backend.ssh.call_args.args[1], "discover")
        self.assertEqual(self.config, before)
        self.backend.bridge.assert_called_once()
        self.assertEqual(self.backend.bridge.call_args.args[1], "status")

    def test_null_cpu_keeps_provider_placeholder_but_requires_configuration_choice(
        self,
    ):
        self.backend.official.return_value = (
            "[selected] VM | Hardware: CPU | Status: READY"
        )
        status = ready_status(self.config)
        status["configuration"].update(cpu=None, ephemeral=False)
        self.backend.bridge.return_value = status
        self.config.cpu = False
        inspected = self.backend.inspect(self.config)
        self.assertTrue(inspected["config"].cpu)
        self.assertFalse(inspected["configuration_known"])
        self.assertFalse(inspected["ready"])
        self.assertFalse(self.config.cpu)

    def test_legacy_null_cpu_does_not_partially_restore_storage_or_access(self):
        self.config.ephemeral = True
        self.config.access = "local-only"
        status = ready_status(dashboard.Config(access="public", ephemeral=False))
        status["configuration"]["cpu"] = None
        self.backend.bridge.return_value = status
        result = self.backend.inspect(self.config)
        self.assertFalse(result["configuration_known"])
        self.assertFalse(result["ready"])
        self.assertTrue(result["config"].ephemeral)
        self.assertEqual(result["config"].access, "local-only")
        self.assertEqual(result["config"].storage_root, self.config.storage_root)

    def test_unknown_configuration_does_not_invent_a_storage_choice(self):
        for saved in (None, {}, {"ephemeral": None}, {"ephemeral": 0}):
            with self.subTest(saved=saved):
                status = ready_status()
                status["configuration"] = saved
                self.backend.bridge.return_value = status
                self.config.storage_root = "/content/drive/MyDrive/custom"
                self.config.ephemeral = False
                result = self.backend.inspect(self.config)
                self.assertFalse(result["configuration_known"])
                self.assertFalse(result["ready"])
                self.assertFalse(result["config"].ephemeral)
                self.assertEqual(
                    result["config"].storage_root, self.config.storage_root
                )
                self.assertFalse(result["config"].cpu)

    def test_healthy_services_without_a_saved_drive_root_require_storage_choice(self):
        status = ready_status(dashboard.Config(ephemeral=False))
        status["configuration"]["storage_root"] = None
        self.backend.bridge.return_value = status
        self.config.storage_root = "/content/drive/MyDrive/previous-session"
        result = self.backend.inspect(self.config)
        self.assertTrue(dashboard.services_ready(result["config"], status))
        self.assertFalse(result["configuration_known"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["config"].storage_root, self.config.storage_root)
        self.backend.bridge.assert_called_once()
        self.assertEqual(self.backend.bridge.call_args.args[1], "status")

    def test_incomplete_persistent_configuration_is_not_partially_restored(self):
        self.config.ephemeral = True
        self.config.access = "local-only"
        status = ready_status(dashboard.Config(access="public", ephemeral=False))
        status["configuration"].update(storage_root=None, cpu=True)
        self.backend.bridge.return_value = status
        result = self.backend.inspect(self.config)
        self.assertFalse(result["configuration_known"])
        self.assertTrue(result["config"].ephemeral)
        self.assertEqual(result["config"].access, "local-only")
        self.assertFalse(result["config"].cpu)
        self.assertFalse(result["ready"])

    def test_invalid_recorded_drive_roots_do_not_restore_or_start_services(self):
        for root in (
            "/content/drive/MyDrive",
            "/content/drive/MyDrive/../outside",
            "/content/local-models",
            "relative/cache",
            123,
        ):
            with self.subTest(root=root):
                status = ready_status(dashboard.Config(ephemeral=False))
                status["configuration"]["storage_root"] = root
                self.backend.bridge.return_value = status
                with self.assertRaisesRegex(dashboard.DashboardError, "invalid"):
                    self.backend.inspect(self.config)
        self.backend.ssh.assert_not_called()
        self.assertEqual(
            [call.args[1] for call in self.backend.bridge.call_args_list],
            ["status"] * 5,
        )

    def test_persistent_storage_and_public_mode_restore_from_services(self):
        status = ready_status(dashboard.Config(access="public", ephemeral=False))
        status["configuration"]["storage_root"] = "/content/drive/MyDrive/saved"
        self.backend.bridge.return_value = status
        self.backend.ssh.return_value = {"running": False, "http_ready": False}
        result = self.backend.inspect(self.config)
        self.assertEqual(result["config"].access, "public")
        self.assertEqual(result["config"].storage_root, "/content/drive/MyDrive/saved")
        self.assertTrue(result["ready"])

    def test_invalid_saved_mode_cannot_be_used_for_restart(self):
        status = ready_status()
        status["configuration"]["access_mode"] = "unrecognized"
        self.backend.bridge.return_value = status
        with self.assertRaisesRegex(dashboard.DashboardError, "invalid"):
            self.backend.inspect(self.config)
        self.backend.ssh.assert_not_called()

    def test_unverified_session_is_not_read_from_a_different_vm(self):
        self.backend.official.return_value = (
            "[somebody-else] VM | Hardware: G4 | Status: READY"
        )
        with self.assertRaisesRegex(dashboard.DashboardError, "verified"):
            self.backend.inspect(self.config)
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()

    def test_local_ready_requires_running_and_http_ready_ssh(self):
        for ssh in ({"running": True}, {"http_ready": True}, {"running": False}):
            with self.subTest(ssh=ssh):
                self.backend.ssh.return_value = ssh
                self.assertFalse(self.backend.inspect(self.config)["ready"])


class CreateTests(unittest.TestCase):
    def test_selected_gpu_reaches_cli_and_cpu_omits_gpu(self):
        for cpu, gpu in ((False, "T4"), (False, "H100"), (False, "G4"), (True, "G4")):
            with self.subTest(cpu=cpu, gpu=gpu):
                run = mock.Mock(
                    side_effect=[
                        subprocess.CompletedProcess([], 0, "Session READY.", ""),
                        subprocess.CompletedProcess(
                            [], 0, f"[selected] VM | Hardware: {gpu}", ""
                        ),
                    ]
                )
                backend = dashboard.Backend(run)
                backend.create(dashboard.Config(session="selected", cpu=cpu, gpu=gpu))
                arguments = run.call_args_list[0].args[0]
                if cpu:
                    self.assertNotIn("--gpu", arguments)
                else:
                    self.assertEqual(arguments[arguments.index("--gpu") + 1], gpu)
                self.assertEqual(arguments[:4], ["colab", "--auth=oauth2", "new", "-s"])
                self.assertEqual(
                    run.call_args_list[1].args[0][-3:], ["status", "-s", "selected"]
                )

    def test_unknown_creation_result_is_not_retried(self):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, "Connecting", "")
        )
        with self.assertRaisesRegex(dashboard.DashboardError, "unknown"):
            dashboard.Backend(run).create(dashboard.Config(session="selected"))
        run.assert_called_once()

    def test_unsupported_gpu_does_not_allocate(self):
        run = mock.Mock()
        with self.assertRaisesRegex(dashboard.DashboardError, "supported"):
            dashboard.Backend(run).create(
                dashboard.Config(session="selected", gpu="invented-GPU")
            )
        run.assert_not_called()

    def test_account_terminal_uses_read_only_sessions_and_needs_no_session_name(self):
        terminal_type = mock.Mock()
        provider_module = mock.Mock(ProviderTerminal=terminal_type)
        on_event = mock.Mock()
        with mock.patch.dict(sys.modules, {"provider_terminal": provider_module}):
            result = dashboard.Backend.account_terminal(dashboard.Config(), on_event)
        terminal_type.assert_called_once_with(
            ["colab", "--auth=oauth2", "sessions"], on_event, timeout=650
        )
        self.assertIs(result, terminal_type.return_value)

    def test_mount_terminal_is_scoped_to_the_selected_vm(self):
        terminal_type = mock.Mock()
        provider_module = mock.Mock(ProviderTerminal=terminal_type)
        on_event = mock.Mock()
        with mock.patch.dict(sys.modules, {"provider_terminal": provider_module}):
            dashboard.Backend.provider_terminal(
                dashboard.Config(session="selected"), on_event
            )
        terminal_type.assert_called_once_with(
            [
                "colab",
                "--auth=oauth2",
                "drivemount",
                "-s",
                "selected",
                "/content/drive",
            ],
            on_event,
            timeout=650,
        )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.backend = mock.Mock(spec=dashboard.Backend)
        self.backend.ssh.return_value = {"running": True, "http_ready": True}
        self.worker = dashboard.Worker(self.backend)
        self.config = dashboard.Config(session="selected", ephemeral=True)
        self.worker._status = mock.Mock(return_value=ready_status(self.config))
        self.worker._mount_embedded = mock.Mock()
        self.worker._wait = mock.Mock(return_value=ready_status(self.config))
        self.addCleanup(self.close_worker)

    def close_worker(self):
        self.worker.close()
        self.worker.thread.join(1)
        self.assertFalse(self.worker.thread.is_alive())

    def test_ready_resume_only_inspects_local_ssh(self):
        self.worker._smart_pipeline(self.config)
        self.backend.bridge.assert_not_called()
        self.worker._wait.assert_not_called()
        self.backend.ssh.assert_called_once_with(self.config, "status")
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()

    def test_missing_steps_follow_deploy_install_prepare_start_ssh_order(self):
        missing = {"deployed": False, "installation": {}, "models_ready": False}
        installed = {**missing, "deployed": True, "installation": {"status": "ready"}}
        prepared = {**installed, "models_ready": True}
        self.worker._status.return_value = missing
        self.worker._wait.side_effect = [installed, prepared, ready_status(self.config)]
        self.backend.ssh.side_effect = [
            {"running": False, "http_ready": False},
            {"running": True, "http_ready": True},
        ]
        self.worker._smart_pipeline(self.config)
        self.assertEqual(
            self.backend.bridge.call_args_list,
            [
                mock.call(self.config, "deploy"),
                mock.call(self.config, "install"),
                mock.call(self.config, "prepare", download_missing=True),
                mock.call(self.config, "start"),
            ],
        )
        self.assertEqual(
            self.worker._wait.call_args_list,
            [
                mock.call(self.config, "installation", 960),
                mock.call(self.config, "model_prepare", 1860),
                mock.call(self.config, "startup", 300),
            ],
        )
        self.assertEqual(
            self.backend.ssh.call_args_list,
            [mock.call(self.config, "status"), mock.call(self.config, "start")],
        )

    def test_install_and_model_jobs_already_running_are_not_started_again(self):
        starting = {
            "deployed": True,
            "installation": {"status": "installing"},
            "models_ready": False,
        }
        installed = {
            **starting,
            "installation": {"status": "ready"},
            "model_prepare": {"running": True, "status": "running"},
        }
        preparing = {
            **installed,
            "models_ready": True,
            "startup": {"status": "starting"},
        }
        self.worker._status.return_value = starting
        self.worker._wait.side_effect = [
            installed,
            preparing,
            ready_status(self.config),
        ]
        self.worker._smart_pipeline(self.config)
        self.backend.bridge.assert_not_called()
        self.assertEqual(self.worker._wait.call_count, 3)

    def test_only_missing_models_are_prepared(self):
        status = ready_status(self.config)
        status["models_ready"] = False
        self.worker._status.return_value = status
        self.worker._smart_pipeline(self.config)
        self.backend.bridge.assert_called_once_with(
            self.config, "prepare", download_missing=True
        )
        self.worker._wait.assert_called_once_with(self.config, "model_prepare", 1860)

    def test_cpu_does_not_download_h3_models(self):
        self.config.cpu = True
        status = ready_status(self.config)
        status["models_ready"] = False
        self.worker._status.return_value = status
        self.worker._smart_pipeline(self.config)
        self.backend.bridge.assert_not_called()
        self.worker._wait.assert_not_called()

    def test_refresh_models_deploys_and_prepares_same_vm_without_service_restart(self):
        for ephemeral in (True, False):
            with self.subTest(ephemeral=ephemeral):
                self.backend.reset_mock()
                self.worker._status.reset_mock()
                self.worker._wait.reset_mock()
                config = dashboard.replace(self.config, ephemeral=ephemeral)
                self.worker._status.return_value = ready_status(config)
                self.worker._prepare_refresh(config)
                self.assertEqual(
                    self.backend.bridge.call_args_list,
                    [
                        mock.call(config, "deploy"),
                        mock.call(config, "prepare", download_missing=True),
                    ],
                )
                self.worker._wait.assert_called_once_with(config, "model_prepare", 1860)
                self.worker._status.assert_has_calls([mock.call(config)] * 2)
                self.backend.ssh.assert_called_once_with(config, "status")
                self.backend.create.assert_not_called()
                self.backend.release.assert_not_called()
                self.worker._mount_embedded.assert_not_called()

    def test_refresh_waits_for_existing_preparation_without_redeploy_or_submit(self):
        status = ready_status(self.config)
        status["model_prepare"] = {"running": True, "status": "running"}
        self.worker._status.return_value = status
        self.worker._prepare_refresh(self.config)
        self.backend.bridge.assert_not_called()
        self.worker._wait.assert_called_once_with(self.config, "model_prepare", 1860)
        self.assertTrue(
            any(
                kind == "notice" and "not redeployed" in value
                for kind, value in self.worker.events.queue
            )
        )
        self.backend.ssh.assert_called_once_with(self.config, "status")

    def test_refresh_requires_known_matching_gpu_configuration_before_mutation(self):
        changes = (
            (
                "GPU runtime",
                {
                    "configuration": {
                        "cpu": True,
                        "ephemeral": True,
                        "access_mode": "local",
                    }
                },
            ),
            ("unknown", {"configuration": {}}),
            (
                "settings",
                {
                    "configuration": {
                        "cpu": False,
                        "ephemeral": False,
                        "storage_root": dashboard.DEFAULT_STORAGE,
                        "access_mode": "local",
                    }
                },
            ),
            (
                "settings",
                {
                    "configuration": {
                        "cpu": False,
                        "ephemeral": True,
                        "access_mode": "public",
                    }
                },
            ),
            ("uncertain", {"model_prepare": {"running": False, "status": "unknown"}}),
            (
                "uncertain",
                {"model_prepare": {"running": False, "status": "interrupted"}},
            ),
            ("already running", {"model_download": {"running": True}}),
            ("installation", {"installation": {"status": "installing"}}),
        )
        for message, fields in changes:
            with self.subTest(fields=fields):
                self.worker._status.return_value = {
                    **ready_status(self.config),
                    **fields,
                }
                with self.assertRaisesRegex(dashboard.DashboardError, message):
                    self.worker._prepare_refresh(self.config)
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()
        self.worker._wait.assert_not_called()
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()

    def test_refresh_drive_requires_ready_mount_and_dedicated_known_root(self):
        config = dashboard.replace(self.config, ephemeral=False)
        for fields, message in (
            ({"drive": {"mounted": False}}, "Drive is not ready"),
            (
                {"drive": {"mounted": True, "mydrive_ready": False}},
                "Drive is not ready",
            ),
        ):
            with self.subTest(fields=fields):
                self.worker._status.return_value = {**ready_status(config), **fields}
                with self.assertRaisesRegex(dashboard.DashboardError, message):
                    self.worker._prepare_refresh(config)
        for root in (
            "/content/models",
            "/content/drive/MyDrive",
            "/content/drive/MyDrive/a/../b",
        ):
            with self.subTest(root=root):
                candidate = dashboard.replace(config, storage_root=root)
                self.worker._status.return_value = ready_status(candidate)
                with self.assertRaisesRegex(
                    dashboard.DashboardError, "directory is invalid"
                ):
                    self.worker._prepare_refresh(candidate)
        self.backend.bridge.assert_not_called()
        self.worker._wait.assert_not_called()

    def test_refresh_requires_service_registration_for_missing_or_unknown_paths(self):
        for registered in (
            None,
            [],
            [
                category
                for category in dashboard.MODEL_SEARCH_CATEGORIES
                if category != "vae"
            ],
        ):
            with self.subTest(registered=registered):
                self.backend.reset_mock()
                status = ready_status(self.config)
                status["model_search_categories"] = registered
                self.worker._status.return_value = status
                self.worker._prepare_refresh(self.config)
                notice = list(self.worker.events.queue)[-1][1]
                self.assertIn("verified", notice)
                self.assertIn("Stop services", notice)
                self.assertIn("Continue missing startup steps", notice)
                self.assertIn("Browser refresh alone is insufficient", notice)
                self.assertEqual(
                    [call.args[1] for call in self.backend.bridge.call_args_list],
                    ["deploy", "prepare"],
                )
                self.backend.ssh.assert_called_once_with(self.config, "status")
                self.backend.create.assert_not_called()
                self.backend.release.assert_not_called()

    def test_refresh_current_registered_process_only_needs_browser_refresh(self):
        self.worker._prepare_refresh(self.config)
        notice = list(self.worker.events.queue)[-1][1]
        self.assertIn("Refresh ComfyUI", notice)
        self.assertNotIn("Stop services", notice)
        self.assertTrue(
            any(
                kind == "model_registration"
                for kind, _value in self.worker.events.queue
            )
        )

    def test_refresh_unsupported_category_or_invalid_local_list_is_rejected_before_deploy(
        self,
    ):
        reader = mock.Mock()
        reader.load_manifests.return_value = {
            "files": [{"path": "unregistered/model.safetensors"}]
        }
        with mock.patch.object(
            dashboard, "_model_manifest_module", return_value=reader
        ):
            with self.assertRaisesRegex(dashboard.DashboardError, "not registered"):
                self.worker._prepare_refresh(self.config)
            reader.load_manifests.side_effect = ValueError("Invalid fixture list")
            with self.assertRaisesRegex(
                dashboard.DashboardError, "Invalid local model list"
            ):
                self.worker._prepare_refresh(self.config)
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()
        self.worker._wait.assert_not_called()

    def test_normal_gpu_startup_preflight_rejects_bad_lists_before_transfers(self):
        with mock.patch.object(
            dashboard,
            "validate_model_plan",
            side_effect=dashboard.DashboardError("Unsupported model category"),
        ):
            for operation in (self.worker._smart_pipeline, self.worker._pipeline):
                with (
                    self.subTest(operation=operation.__name__),
                    self.assertRaisesRegex(dashboard.DashboardError, "Unsupported"),
                ):
                    operation(self.config)
        self.worker._mount_embedded.assert_not_called()
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()
        self.backend.create.assert_not_called()

    def test_cpu_startup_skips_gpu_model_preflight(self):
        config = dashboard.replace(self.config, cpu=True)
        self.worker._status.return_value = ready_status(config)
        with mock.patch.object(
            dashboard,
            "validate_model_plan",
            side_effect=AssertionError("CPU does not require model manifests"),
        ) as validation:
            self.worker._smart_pipeline(config)
        validation.assert_not_called()
        self.backend.bridge.assert_not_called()

    def test_refresh_failure_does_not_restart_or_repeat_unknown_mutations(self):
        for failure_at in ("deploy", "prepare", "wait"):
            with self.subTest(failure_at=failure_at):
                self.backend.reset_mock()
                self.worker._wait.reset_mock()
                self.worker._status.return_value = ready_status(self.config)
                error = dashboard.DashboardError(
                    "Model operation failed; outcome unknown"
                )

                def bridge(
                    _config, action, *, expected=failure_at, failure=error, **_kwargs
                ):
                    if action == expected:
                        raise failure
                    return {"ok": True}

                self.backend.bridge.side_effect = bridge
                self.worker._wait.side_effect = error if failure_at == "wait" else None
                with self.assertRaises(dashboard.DashboardError) as caught:
                    self.worker._prepare_refresh(self.config)
                self.assertIs(caught.exception, error)
                self.assertEqual(
                    [call.args[1] for call in self.backend.bridge.call_args_list],
                    ["deploy"] if failure_at == "deploy" else ["deploy", "prepare"],
                )
                self.backend.ssh.assert_not_called()
                self.backend.create.assert_not_called()
                self.backend.release.assert_not_called()

    def test_refresh_verifies_final_readiness_and_clears_failed_ssh_probe(self):
        good = ready_status(self.config)
        bad = {**good, "models_ready": False}
        self.worker._status.side_effect = [good, bad]
        with self.assertRaisesRegex(dashboard.DashboardError, "readiness changed"):
            self.worker._prepare_refresh(self.config)
        self.backend.ssh.assert_not_called()
        self.worker._status.side_effect = None
        self.worker._status.return_value = good
        self.backend.ssh.side_effect = dashboard.DashboardError("SSH status failed")
        self.worker._prepare_refresh(self.config)
        self.assertIn(
            ("ssh_error", "SSH status failed"), list(self.worker.events.queue)
        )

    def test_unhealthy_existing_service_is_not_restarted(self):
        status = ready_status(self.config)
        status["http_ready"] = False
        self.worker._status.return_value = status
        with self.assertRaisesRegex(dashboard.DashboardError, "existing service"):
            self.worker._smart_pipeline(self.config)
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()

    def test_storage_change_requires_explicit_stop_before_mount_or_install(self):
        status = ready_status(self.config)
        status["configuration"]["ephemeral"] = False
        self.worker._status.return_value = status
        with self.assertRaisesRegex(dashboard.DashboardError, "different storage"):
            self.worker._smart_pipeline(self.config)
        self.worker._mount_embedded.assert_not_called()
        self.backend.bridge.assert_not_called()
        self.backend.release.assert_not_called()

    def test_unverified_ssh_does_not_report_ready(self):
        self.backend.ssh.side_effect = [
            {"running": False},
            {"running": True, "http_ready": False},
        ]
        with self.assertRaisesRegex(dashboard.DashboardError, "SSH forwarding"):
            self.worker._smart_pipeline(self.config)
        events = list(self.worker.events.queue)
        self.assertNotIn(("notice", "ComfyUI is ready."), events)
        self.backend.bridge.assert_not_called()

    def test_unknown_installation_is_inspected_without_restarting_it(self):
        status = ready_status(self.config)
        status["installation"] = {"status": "unknown"}
        self.worker._status.return_value = status
        with self.assertRaisesRegex(dashboard.DashboardError, "unknown"):
            self.worker._smart_pipeline(self.config)
        self.backend.bridge.assert_not_called()
        self.worker._wait.assert_not_called()

    def test_uncertain_model_preparation_is_not_automatically_repeated(self):
        for phase in ("unknown", "interrupted"):
            with self.subTest(phase=phase):
                status = ready_status(self.config)
                status["models_ready"] = False
                status["model_prepare"] = {"status": phase, "running": False}
                self.worker._status.return_value = status
                with self.assertRaisesRegex(dashboard.DashboardError, "uncertain"):
                    self.worker._smart_pipeline(self.config)
                self.backend.bridge.assert_not_called()
                self.worker._wait.assert_not_called()

    def test_cancellation_between_status_and_mutation_prevents_remote_commands(self):
        for pending in ("deploy", "install", "prepare", "start"):
            with self.subTest(pending=pending):
                self.worker.operation_cancelled.clear()
                status = ready_status(self.config)
                if pending == "deploy":
                    status["deployed"] = False
                elif pending == "install":
                    status["installation"] = {}
                elif pending == "prepare":
                    status["models_ready"] = False
                else:
                    status["comfyui_alive"] = False
                    status["http_ready"] = False
                    status["startup"] = {}

                def cancel_after_read(_config, captured_status=status):
                    self.worker.operation_cancelled.set()
                    return captured_status

                self.worker._status.side_effect = cancel_after_read
                with self.assertRaises(dashboard.OperationCancelled):
                    self.worker._smart_pipeline(self.config)
                self.backend.bridge.assert_not_called()

    def test_cancellation_after_ssh_status_prevents_opening_a_new_tunnel(self):
        def cancel_after_read(_config, action):
            self.assertEqual(action, "status")
            self.worker.operation_cancelled.set()
            return {"running": False, "http_ready": False}

        self.backend.ssh.side_effect = cancel_after_read
        with self.assertRaises(dashboard.OperationCancelled):
            self.worker._smart_pipeline(self.config)
        self.backend.ssh.assert_called_once_with(self.config, "status")

    def test_close_stops_coordination_and_auth_child_without_releasing_vm(self):
        terminal = mock.Mock()
        self.worker.auth_terminal = terminal
        self.worker.close()
        terminal.close.assert_called_once_with()
        self.backend.bridge.assert_not_called()
        self.backend.ssh.assert_not_called()
        self.backend.release.assert_not_called()

    def test_actual_model_wait_requires_all_completion_evidence(self):
        partial_results = (
            {"status": "running", "running": False, "result": {"ok": True}},
            {"status": "succeeded", "result": {"ok": True}},
            {"status": "succeeded", "running": True, "result": {"ok": True}},
            {"status": "succeeded", "running": 0, "result": {"ok": True}},
            {"status": "succeeded", "running": False},
            {"status": "succeeded", "running": False, "result": {"ok": 1}},
        )
        for task in partial_results:
            with self.subTest(task=task):
                status = {"model_prepare": task, "models_ready": True}
                self.worker._status.return_value = status
                self.worker.clock = mock.Mock(side_effect=[0.0, 1.0])
                with self.assertRaisesRegex(dashboard.DashboardError, "timed out"):
                    dashboard.Worker._wait(
                        self.worker, self.config, "model_prepare", 0.5
                    )
        for models_ready in (False, None, 1):
            with self.subTest(models_ready=models_ready):
                self.worker._status.return_value = {
                    "model_prepare": {
                        "status": "succeeded",
                        "running": False,
                        "result": {"ok": True},
                    },
                    "models_ready": models_ready,
                }
                self.worker.clock = mock.Mock(side_effect=[0.0, 1.0])
                with self.assertRaisesRegex(dashboard.DashboardError, "timed out"):
                    dashboard.Worker._wait(
                        self.worker, self.config, "model_prepare", 0.5
                    )
        self.backend.bridge.assert_not_called()

    def test_actual_model_wait_reports_failed_result_before_timeout(self):
        self.worker._status.return_value = {
            "model_prepare": {
                "status": "succeeded",
                "running": False,
                "result": {"ok": False, "error": "Fixture model validation failed"},
            },
            "models_ready": True,
        }
        self.worker.clock = mock.Mock(return_value=0.0)
        with self.assertRaisesRegex(dashboard.DashboardError, "validation failed"):
            dashboard.Worker._wait(self.worker, self.config, "model_prepare", 100)
        self.worker.clock.assert_called_once_with()
        self.backend.bridge.assert_not_called()

    def test_actual_model_wait_accepts_verified_complete_local_models(self):
        status = {
            "model_prepare": {
                "status": "succeeded",
                "running": False,
                "result": {"ok": True},
            },
            "models_ready": True,
        }
        self.worker._status.return_value = status
        self.worker.clock = mock.Mock(return_value=0.0)
        self.assertIs(
            dashboard.Worker._wait(self.worker, self.config, "model_prepare", 100),
            status,
        )
        self.worker.clock.assert_called_once_with()
        self.backend.bridge.assert_not_called()


class ModelRefreshWorkerTests(unittest.TestCase):
    def test_gpu_create_preflight_rejects_before_any_vm_allocation(self):
        backend = mock.Mock(spec=dashboard.Backend)
        config = dashboard.Config(session="not-allocated", ephemeral=True)
        worker = dashboard.Worker(backend)
        worker.account_checked = True
        self.addCleanup(worker.close)
        with mock.patch.object(
            dashboard,
            "validate_model_plan",
            side_effect=dashboard.DashboardError("Invalid pinned model list"),
        ):
            for action in ("wizard_create", "new"):
                self.assertTrue(worker.submit(action, config))
                errors = []
                for _ in range(10):
                    kind, value = worker.events.get(timeout=1)
                    if kind == "error":
                        errors.append(value)
                    if kind == "done":
                        self.assertEqual(value, action)
                        break
                else:
                    self.fail("Preflight rejection did not complete")
                self.assertEqual(errors, ["Invalid pinned model list"])
        backend.create.assert_not_called()
        backend.bridge.assert_not_called()
        backend.release.assert_not_called()
        worker.close()
        worker.thread.join(1)
        self.assertFalse(worker.thread.is_alive())

    def test_real_worker_dispatch_refreshes_one_existing_vm_and_only_probes_ssh(self):
        backend = mock.Mock(spec=dashboard.Backend)
        config = dashboard.Config(session="selected", ephemeral=True)
        status = ready_status(config)
        status["model_prepare"] = {
            "running": False,
            "status": "succeeded",
            "result": {"ok": True},
        }
        backend.bridge.side_effect = lambda _config, action, **_kwargs: (
            status if action == "status" else {"ok": True, "status": "started"}
        )
        backend.ssh.return_value = {"running": True, "http_ready": True}
        worker = dashboard.Worker(backend)
        self.addCleanup(worker.close)
        self.assertTrue(worker.submit("prepare_refresh", config))
        for _ in range(15):
            kind, value = worker.events.get(timeout=1)
            if kind == "done":
                self.assertEqual(value, "prepare_refresh")
                break
        else:
            self.fail("Model refresh did not complete")
        self.assertEqual(
            [call.args[1] for call in backend.bridge.call_args_list],
            ["status", "deploy", "prepare", "status", "status"],
        )
        self.assertTrue(
            all(
                call.args[0].session == "selected" and call.args[0].ephemeral is True
                for call in backend.bridge.call_args_list
            )
        )
        backend.ssh.assert_called_once_with(config, "status")
        backend.create.assert_not_called()
        backend.release.assert_not_called()
        worker.close()
        worker.thread.join(1)
        self.assertFalse(worker.thread.is_alive())


class StatusRetryTests(unittest.TestCase):
    def setUp(self):
        self.backend = mock.Mock(spec=dashboard.Backend)
        self.worker = dashboard.Worker(self.backend)
        self.config = dashboard.Config(session="selected", ephemeral=True)
        retry_patch = mock.patch.object(
            self.worker.operation_cancelled, "wait", return_value=False
        )
        self.retry_wait = retry_patch.start()
        self.addCleanup(retry_patch.stop)
        self.addCleanup(self.close_worker)

    def close_worker(self):
        self.worker.close()
        self.worker.thread.join(1)
        self.assertFalse(self.worker.thread.is_alive())

    def assert_only_status_calls(self, count):
        self.assertEqual(
            self.backend.bridge.call_args_list,
            [mock.call(self.config, "status")] * count,
        )
        self.backend.release.assert_not_called()

    def test_third_read_recovers_same_runtime_without_repeating_startup(self):
        status = ready_status(self.config)
        self.backend.bridge.side_effect = [
            dashboard.DashboardError("WebSocketConnectionClosedException: fixture"),
            dashboard.DashboardError("Colab connection closed while checking fixture"),
            status,
        ]
        self.backend.ssh.return_value = {"running": True, "http_ready": True}
        self.worker._smart_pipeline(self.config)
        self.assert_only_status_calls(3)
        self.backend.create.assert_not_called()
        self.assertEqual(self.retry_wait.call_args_list, [mock.call(1)] * 2)
        events = list(self.worker.events.queue)
        self.assertEqual(sum(kind == "status" for kind, _value in events), 1)
        self.assertEqual(
            sum(
                kind == "notice" and "connection interrupted" in value
                for kind, value in events
            ),
            2,
        )
        self.assertIn(("notice", "ComfyUI is ready."), events)

    def test_status_timeouts_recover_on_third_read_without_startup_commands(self):
        status = ready_status(self.config)
        self.backend.bridge.side_effect = [
            dashboard.DashboardError(
                "Local Colab command timed out. Check status before repeating an operation."
            ),
            dashboard.DashboardError(
                "Command timed out; its remote outcome is unknown. Inspect status before retrying."
            ),
            status,
        ]
        self.assertIs(self.worker._status(self.config), status)
        self.assert_only_status_calls(3)
        self.backend.create.assert_not_called()
        self.assertEqual(self.retry_wait.call_args_list, [mock.call(1)] * 2)
        self.assertEqual(
            [
                value
                for kind, value in list(self.worker.events.queue)
                if kind == "status"
            ],
            [status],
        )

    def test_install_and_prepare_timeouts_do_not_retry_the_mutation(self):
        for action, message in (
            (
                "install",
                "Local Colab command timed out. Check status before repeating an operation.",
            ),
            (
                "prepare",
                "Command timed out; its remote outcome is unknown. Inspect status before retrying.",
            ),
        ):
            with self.subTest(action=action):
                self.backend.bridge.reset_mock()
                status = ready_status(self.config)
                status["models_ready"] = False
                if action == "install":
                    status["installation"] = {"status": "not_started"}
                error = dashboard.DashboardError(message)

                def bridge(
                    _config,
                    operation,
                    *,
                    expected=action,
                    failure=error,
                    current_status=status,
                    **_kwargs,
                ):
                    if operation == "status":
                        return current_status
                    self.assertEqual(operation, expected)
                    raise failure

                self.backend.bridge.side_effect = bridge
                with self.assertRaises(dashboard.DashboardError) as caught:
                    self.worker._smart_pipeline(self.config)
                self.assertIs(caught.exception, error)
                self.assertEqual(
                    self.backend.bridge.call_args_list,
                    [
                        mock.call(self.config, "status"),
                        mock.call(self.config, action, download_missing=True)
                        if action == "prepare"
                        else mock.call(self.config, action),
                    ],
                )
                self.assertNotIn(
                    ("notice", "ComfyUI is ready."), list(self.worker.events.queue)
                )
        self.retry_wait.assert_not_called()
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()

    def test_recovery_during_new_flow_creates_exactly_one_vm(self):
        self.worker.account_checked = True
        self.backend.create.return_value = {"name": "selected", "hardware": "G4"}
        self.backend.bridge.side_effect = [
            dashboard.DashboardError("WebSocketConnectionClosedException: fixture"),
            dashboard.DashboardError("WebSocketConnectionClosedException: fixture"),
            ready_status(self.config),
        ]
        self.backend.ssh.return_value = {"running": True, "http_ready": True}
        self.assertTrue(self.worker.submit("wizard_create", self.config))
        completed = False
        for _ in range(12):
            kind, value = self.worker.events.get(timeout=1)
            if kind == "done" and value == "wizard_create":
                completed = True
                break
        self.assertTrue(completed)
        self.backend.create.assert_called_once_with(self.config)
        self.assert_only_status_calls(3)

    def test_poll_recovery_does_not_repeat_install_prepare_or_service_start(self):
        missing = {
            "deployed": True,
            "installation": {"status": "not_started"},
            "models_ready": False,
        }
        installed = {
            **missing,
            "installation": {"status": "ready"},
            "model_prepare": {"status": "not_started", "running": False},
        }
        prepared = {
            **installed,
            "models_ready": True,
            "model_prepare": {
                "status": "succeeded",
                "running": False,
                "result": {"ok": True},
            },
        }
        phase = "initial"
        reads = {}
        completed = {
            "initial": missing,
            "install": installed,
            "prepare": prepared,
            "start": ready_status(self.config),
        }

        def bridge(_config, action, **_kwargs):
            nonlocal phase
            if action != "status":
                phase = action
                return {"ok": True, "status": "started"}
            reads[phase] = reads.get(phase, 0) + 1
            if phase != "initial" and reads[phase] < 3:
                raise dashboard.DashboardError(
                    "WebSocketConnectionClosedException: fixture"
                )
            return completed[phase]

        self.backend.bridge.side_effect = bridge
        self.backend.ssh.return_value = {"running": True, "http_ready": True}
        self.worker._smart_pipeline(self.config)
        self.assertEqual(
            [
                call
                for call in self.backend.bridge.call_args_list
                if call.args[1] != "status"
            ],
            [
                mock.call(self.config, "install"),
                mock.call(self.config, "prepare", download_missing=True),
                mock.call(self.config, "start"),
            ],
        )
        self.assertEqual(reads, {"initial": 1, "install": 3, "prepare": 3, "start": 3})
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()

    def test_non_connection_errors_are_not_retried(self):
        for message in (
            "Provider command timed out; remote outcome is unknown",
            "Fixture model checksum mismatch",
            "Selected session could not be verified",
        ):
            with self.subTest(message=message):
                self.backend.bridge.reset_mock()
                error = dashboard.DashboardError(message)
                self.backend.bridge.side_effect = error
                with self.assertRaises(dashboard.DashboardError) as caught:
                    self.worker._status(self.config)
                self.assertIs(caught.exception, error)
                self.assert_only_status_calls(1)
        self.retry_wait.assert_not_called()
        self.backend.create.assert_not_called()

    def test_three_failed_reads_stop_without_a_fourth_or_startup_mutation(self):
        errors = [
            dashboard.DashboardError(f"WebSocketConnectionClosedException {index}")
            for index in range(3)
        ]
        self.backend.bridge.side_effect = errors
        with self.assertRaises(dashboard.DashboardError) as caught:
            self.worker._status(self.config)
        self.assertIs(caught.exception, errors[-1])
        self.assert_only_status_calls(3)
        self.backend.create.assert_not_called()
        self.assertEqual(self.retry_wait.call_count, 2)
        self.assertFalse(
            any(kind == "status" for kind, _value in list(self.worker.events.queue))
        )

    def test_cancellation_before_status_prevents_the_first_read(self):
        self.worker.operation_cancelled.set()
        with self.assertRaises(dashboard.OperationCancelled):
            self.worker._status(self.config)
        self.assert_only_status_calls(0)
        self.retry_wait.assert_not_called()
        self.backend.create.assert_not_called()

    def test_cancellation_during_retry_prevents_the_next_read(self):
        self.backend.bridge.side_effect = dashboard.DashboardError(
            "WebSocketConnectionClosedException: fixture"
        )

        def cancel_during_wait(_seconds):
            self.worker.operation_cancelled.set()
            return True

        self.retry_wait.side_effect = cancel_during_wait
        with self.assertRaises(dashboard.OperationCancelled):
            self.worker._status(self.config)
        self.assert_only_status_calls(1)
        self.retry_wait.assert_called_once_with(1)
        self.backend.create.assert_not_called()


class ControlledTerminal:
    """Provider fixture that can finish only after an explicit human response."""

    def __init__(self, on_event, *, require_human=False, returncode=0):
        self.on_event = on_event
        self.require_human = require_human
        self.returncode = returncode
        self.started = threading.Event()
        self.closed = False
        self.joined = False
        self.responses = []

    def start(self):
        self.started.set()
        if self.require_human:
            self.on_event({"kind": "auth_url", "url": "https://authorization.fixture"})
            self.on_event(
                {"kind": "confirm_required", "text": "Press Enter after authorization"}
            )
        else:
            self.finish()
        return self

    def finish(self):
        self.on_event({"kind": "finished", "returncode": self.returncode})

    def respond(self, value=""):
        if not self.require_human or self.responses:
            return False
        self.responses.append(value)
        self.finish()
        return True

    def close(self):
        self.closed = True

    def join(self, _timeout=None):
        self.joined = True


class EmbeddedMountTests(unittest.TestCase):
    def setUp(self):
        self.backend = mock.Mock(spec=dashboard.Backend)
        self.worker = dashboard.Worker(self.backend)
        self.config = dashboard.Config(session="selected")
        self.worker._status = mock.Mock()
        self.addCleanup(self.close_worker)

    def close_worker(self):
        self.worker.close()
        self.worker.thread.join(1)
        self.assertFalse(self.worker.thread.is_alive())

    def terminal_factory(self, **kwargs):
        terminals = []

        def factory(_config, on_event, **_options):
            terminal = ControlledTerminal(on_event, **kwargs)
            terminals.append(terminal)
            return terminal

        self.backend.provider_terminal.side_effect = factory
        return terminals

    def test_ephemeral_skips_mount_status_and_authorization(self):
        self.config.ephemeral = True
        self.worker._mount_embedded(self.config)
        self.worker._status.assert_not_called()
        self.backend.provider_terminal.assert_not_called()

    def test_verified_existing_mydrive_skips_authorization(self):
        self.worker._status.return_value = {
            "drive": {"mounted": True, "mydrive_ready": True}
        }
        self.worker._mount_embedded(self.config)
        self.backend.provider_terminal.assert_not_called()

    def test_zero_exit_still_requires_real_mount_and_mydrive(self):
        for drive in (
            {"mounted": False, "mydrive_ready": True},
            {"mounted": True, "mydrive_ready": False},
            {"mounted": True},
        ):
            with self.subTest(drive=drive):
                self.worker._status.side_effect = [
                    {"drive": {"mounted": False}},
                    {"drive": drive},
                ]
                terminals = self.terminal_factory()
                with self.assertRaisesRegex(dashboard.DashboardError, "not mounted"):
                    self.worker._mount_embedded(self.config)
                self.assertTrue(terminals[0].closed)
                self.assertIsNone(self.worker.auth_terminal)
                self.assertIn(("auth_clear", None), list(self.worker.events.queue))
        self.backend.bridge.assert_not_called()
        self.backend.release.assert_not_called()

    def test_authentication_response_is_only_sent_by_explicit_ui_input(self):
        self.worker._status.side_effect = [
            {"drive": {"mounted": False}},
            {"drive": {"mounted": True, "mydrive_ready": True}},
        ]
        terminals = self.terminal_factory(require_human=True)
        finished = threading.Event()
        errors = []

        def mount():
            try:
                self.worker._mount_embedded(self.config)
            except Exception as exc:  # noqa: BLE001
                # Thread failures must reach the test runner.
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=mount)
        thread.start()
        self.addCleanup(thread.join, 1)
        auth_event = None
        for _ in range(5):
            kind, value = self.worker.events.get(timeout=1)
            if kind == "auth" and value.get("waiting"):
                auth_event = value
                break
        self.assertIsNotNone(auth_event)
        self.assertFalse(finished.is_set())
        self.assertEqual(terminals[0].responses, [])
        self.assertTrue(self.worker.send_auth())
        self.assertTrue(finished.wait(1))
        self.assertEqual(errors, [])
        self.assertEqual(terminals[0].responses, [""])
        self.assertTrue(terminals[0].closed)
        self.assertIsNone(self.worker.auth_terminal)
        self.assertFalse(self.worker.send_auth())
        events = list(self.worker.events.queue)
        self.assertIn(("auth_clear", None), events)
        self.backend.release.assert_not_called()

    def test_failed_authorization_does_not_accept_a_stale_mount(self):
        self.worker._status.return_value = {"drive": {"mounted": False}}
        terminals = self.terminal_factory(returncode=1)
        with self.assertRaisesRegex(dashboard.DashboardError, "did not complete"):
            self.worker._mount_embedded(self.config)
        self.worker._status.assert_called_once_with(self.config)
        self.assertTrue(terminals[0].closed)

    def test_no_auth_terminal_rejects_unsolicited_code(self):
        self.assertFalse(self.worker.send_auth("fixture-code"))
        self.backend.provider_terminal.assert_not_called()

    def test_account_is_checked_once_without_mounting_or_mutating_a_vm(self):
        terminals = []

        def factory(_config, on_event):
            terminal = ControlledTerminal(on_event)
            terminals.append(terminal)
            return terminal

        self.backend.account_terminal.side_effect = factory
        self.worker._ensure_account(self.config)
        self.worker._ensure_account(self.config)
        self.backend.account_terminal.assert_called_once()
        self.assertTrue(self.worker.account_checked)
        self.assertTrue(terminals[0].closed)
        self.backend.provider_terminal.assert_not_called()
        self.backend.bridge.assert_not_called()
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()

    def test_failed_account_authorization_is_not_cached_as_logged_in(self):
        self.backend.account_terminal.side_effect = lambda _config, on_event: (
            ControlledTerminal(on_event, returncode=1)
        )
        with self.assertRaisesRegex(dashboard.DashboardError, "did not complete"):
            self.worker._ensure_account(self.config)
        self.assertFalse(self.worker.account_checked)
        self.backend.create.assert_not_called()
        self.backend.release.assert_not_called()

    def test_late_provider_callback_cannot_restore_cleared_authorization(self):
        terminals = self.terminal_factory()
        self.worker._provider_embedded(
            self.config, self.backend.provider_terminal, "fixture authorization"
        )
        terminal = terminals[0]
        self.assertTrue(terminal.closed)
        self.assertTrue(terminal.joined)
        before = list(self.worker.events.queue)
        self.assertEqual(before[-1], ("auth_clear", None))
        terminal.on_event(
            {"kind": "auth_url", "url": "https://late-authorization.fixture"}
        )
        terminal.on_event({"kind": "output", "text": "late output"})
        self.assertEqual(list(self.worker.events.queue), before)
        self.assertIsNone(self.worker.auth_terminal)

    def test_queued_release_cancels_auth_and_uses_original_operation_scope(self):
        self.worker.account_checked = True
        status = ready_status(self.config)
        status["drive"] = {"mounted": False}
        # Use the real Worker status path for this queue/cleanup interaction.
        self.worker._status = dashboard.Worker._status.__get__(self.worker)
        self.backend.bridge.return_value = status
        self.backend.ssh.return_value = {"running": False, "http_ready": False}
        terminals = self.terminal_factory(require_human=True)
        self.assertTrue(self.worker.submit("wizard_resume", self.config))
        auth_waiting = False
        for _ in range(8):
            kind, value = self.worker.events.get(timeout=1)
            if kind == "auth" and value.get("waiting"):
                auth_waiting = True
                break
        self.assertTrue(auth_waiting)
        changed_settings = dashboard.Config(
            session="selected",
            storage_root="/different/path",
            local_port=9000,
            identity="/different/key",
        )
        self.assertFalse(
            self.worker.submit("release", dashboard.Config(session="unrelated"))
        )
        self.assertTrue(self.worker.submit("release", changed_settings))
        release_done = False
        for _ in range(12):
            kind, value = self.worker.events.get(timeout=1)
            if kind == "done" and value == "release":
                release_done = True
                break
        self.assertTrue(release_done)
        self.backend.release.assert_called_once()
        released_config = self.backend.release.call_args.args[0]
        self.assertEqual(released_config, self.config)
        self.assertEqual(released_config.local_port, 8188)
        self.assertTrue(terminals[0].closed)
        self.assertEqual(terminals[0].responses, [])
        self.assertEqual(
            self.backend.bridge.call_args_list,
            [
                mock.call(self.config, "status"),
                mock.call(self.config, "status"),
                mock.call(self.config, "stop"),
                mock.call(self.config, "status"),
            ],
        )


class ThemeTests(unittest.TestCase):
    def initialized(self, colors=256, change=True, color=True):
        module = dashboard.curses
        patches = [
            mock.patch.dict(dashboard.os.environ, {}, clear=True),
            mock.patch.object(module, "has_colors", return_value=True),
            mock.patch.object(module, "start_color"),
            mock.patch.object(module, "use_default_colors"),
            mock.patch.object(module, "COLORS", colors, create=True),
            mock.patch.object(module, "can_change_color", return_value=change),
            mock.patch.object(module, "init_color"),
            mock.patch.object(module, "init_pair"),
            mock.patch.object(module, "color_pair", side_effect=lambda pair: pair << 8),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        theme = dashboard.Theme(color=color)
        theme.initialize()
        return theme

    def test_brand_colors_are_exact_rgb_scaled_for_curses(self):
        theme = self.initialized()
        self.assertEqual(
            {key: theme.BRAND[key] for key in ("comfy", "title", "accent")},
            {"comfy": "#F2FF59", "title": "#E77012", "accent": "#F9AA00"},
        )
        self.assertEqual(
            dashboard.curses.init_color.call_args_list,
            [
                mock.call(229, 949, 1000, 349),
                mock.call(208, 906, 439, 71),
                mock.call(214, 976, 667, 0),
                mock.call(75, 459, 749, 1000),
                mock.call(153, 741, 875, 1000),
            ],
        )
        pairs = dashboard.curses.init_pair.call_args_list
        self.assertIn(mock.call(1, 208, -1), pairs)
        self.assertIn(mock.call(2, 214, -1), pairs)
        self.assertIn(mock.call(8, 229, -1), pairs)
        self.assertIn(mock.call(9, 255, -1), pairs)
        self.assertIn(mock.call(10, 75, -1), pairs)
        self.assertIn(mock.call(11, 153, -1), pairs)
        self.assertTrue(theme.roles["info_heading"] & dashboard.curses.A_BOLD)
        self.assertFalse(theme.roles["info"] & dashboard.curses.A_BOLD)

    def test_fixed_256_color_terminal_does_not_attempt_palette_mutation(self):
        self.initialized(change=False)
        dashboard.curses.init_color.assert_not_called()
        dashboard.curses.init_pair.assert_any_call(1, 208, -1)

    def test_basic_terminal_keeps_readable_colors(self):
        self.initialized(colors=8)
        dashboard.curses.init_color.assert_not_called()
        dashboard.curses.init_pair.assert_any_call(1, dashboard.curses.COLOR_YELLOW, -1)
        dashboard.curses.init_pair.assert_any_call(3, dashboard.curses.COLOR_GREEN, -1)
        dashboard.curses.init_pair.assert_any_call(9, dashboard.curses.COLOR_WHITE, -1)
        dashboard.curses.init_pair.assert_any_call(10, dashboard.curses.COLOR_CYAN, -1)
        dashboard.curses.init_pair.assert_any_call(11, dashboard.curses.COLOR_CYAN, -1)

    def test_monochrome_preserves_text_roles_without_curses_color_calls(self):
        theme = self.initialized(color=False)
        dashboard.curses.start_color.assert_not_called()
        dashboard.curses.init_pair.assert_not_called()
        self.assertEqual(theme.roles["input"], dashboard.curses.A_BOLD)


if __name__ == "__main__":
    unittest.main()
