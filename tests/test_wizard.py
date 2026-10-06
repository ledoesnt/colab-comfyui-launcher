"""Deterministic wizard behavior and terminal frames; no provider is contacted."""

import fcntl
import importlib.util
import os
import pty
import queue
import select
import struct
import subprocess
import sys
import termios
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if "dashboard" not in sys.modules:
    dashboard_spec = importlib.util.spec_from_file_location(
        "dashboard", ROOT / "scripts/dashboard.py"
    )
    dashboard = importlib.util.module_from_spec(dashboard_spec)
    sys.modules[dashboard_spec.name] = dashboard
    dashboard_spec.loader.exec_module(dashboard)
else:
    dashboard = sys.modules["dashboard"]
spec = importlib.util.spec_from_file_location("wizard", ROOT / "scripts/wizard.py")
wizard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wizard
spec.loader.exec_module(wizard)


class LocalWorker:
    def __init__(self):
        self.events = queue.Queue()
        self.busy = False
        self.accept = True
        self.submitted = []
        self.responses = []
        self.closed = False

    def submit(self, action, config):
        if not self.accept:
            return False
        self.submitted.append((action, replace(config)))
        return True

    def send_auth(self, text=""):
        self.responses.append(text)
        return self.accept

    def close(self):
        self.closed = True


class Frame:
    def __init__(self, height=36, width=120):
        self.height, self.width = height, width
        self.updates = 0
        self.refreshes = 0
        self.erase()

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows = [[" "] * self.width for _ in range(self.height)]
        self.attributes = [[0] * self.width for _ in range(self.height)]

    def addnstr(self, row, column, text, maximum, attribute):
        if not 0 <= row < self.height:
            raise AssertionError("Row out of bounds")
        for character in text[:maximum]:
            if not 0 <= column < self.width:
                raise AssertionError("Column out of bounds")
            self.rows[row][column] = character
            self.attributes[row][column] = attribute
            column += 1

    def noutrefresh(self):
        self.updates += 1

    def refresh(self):
        self.refreshes += 1

    @property
    def text(self):
        return "\n".join("".join(row) for row in self.rows)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)

    def choose(self, key):
        keys = [choice.key for choice in self.ui._choices()]
        self.ui.selected = keys.index(key)
        return self.ui._key("\n")

    def new_cpu_summary(self):
        self.choose("new")
        self.choose("cpu")
        self.choose("ephemeral")
        self.assertEqual(self.ui.page, "summary")

    def emit(self, kind, value):
        self.worker.events.put((kind, value))
        self.ui._events()

    def ready_status(self):
        self.ui.status = {
            "http_ready": True,
            "comfyui_alive": True,
            "tunnel_alive": False,
            "access_mode": "local",
            "startup": {"ok": True, "status": "started", "access_mode": "local"},
        }
        self.ui.ssh = {
            "running": True,
            "http_ready": True,
            "url": "http://127.0.0.1:8188",
        }

    def test_initial_navigation_and_typing_do_not_allocate(self):
        for key in ("C", "q", "e", "KEY_DOWN", "KEY_UP", "KEY_NPAGE"):
            self.assertTrue(self.ui._key(key))
        self.assertEqual(self.ui.page, "home")
        self.assertEqual(self.worker.submitted, [])

    def test_cpu_temp_configuration_precedes_explicit_creation(self):
        self.new_cpu_summary()
        self.assertTrue(self.ui.config.cpu)
        self.assertTrue(self.ui.config.ephemeral)
        self.assertEqual(self.ui.config.session, "")
        self.assertEqual(self.worker.submitted, [])
        with mock.patch.object(Path, "is_file", return_value=True):
            self.choose("start")
        action, config = self.worker.submitted[0]
        self.assertEqual(action, "wizard_create")
        self.assertTrue(config.session.startswith("launcher-cpu-"))
        self.assertEqual(config.access, "local-only")
        self.assertEqual(self.ui.page, "pipeline")

    def test_gpu_model_selection_is_preserved_at_creation(self):
        self.choose("new")
        self.choose("gpu")
        self.assertEqual(self.ui.page, "gpu")
        self.assertEqual(
            [choice.key for choice in self.ui._choices()], list(wizard.GPU_CHOICES)
        )
        self.assertIn("tested", self.ui._choices()[0].label)
        self.assertIn("not verified", self.ui._choices()[1].label)
        self.choose("L4")
        self.choose("ephemeral")
        with mock.patch.object(Path, "is_file", return_value=True):
            self.choose("start")
        action, config = self.worker.submitted[0]
        self.assertEqual(action, "wizard_create")
        self.assertEqual(config.gpu, "L4")
        self.assertTrue(config.ephemeral)
        self.assertFalse(config.cpu)

    def test_missing_key_requires_separate_explicit_consent(self):
        self.new_cpu_summary()
        with mock.patch.object(Path, "is_file", return_value=False):
            self.choose("start")
        self.assertEqual(self.ui.page, "key_confirm")
        self.assertFalse(self.ui.config.create_key)
        self.assertEqual(self.worker.submitted, [])
        self.choose("create_key")
        self.assertEqual(self.ui.page, "summary")
        self.assertTrue(self.ui.config.create_key)
        self.assertEqual(self.worker.submitted, [])
        with mock.patch.object(Path, "is_file", return_value=False):
            self.choose("start")
        self.assertTrue(self.worker.submitted[0][1].create_key)

    def test_new_creation_double_enter_cannot_repeat_creation(self):
        self.new_cpu_summary()
        with mock.patch.object(Path, "is_file", return_value=True):
            self.choose("start")
        self.ui._key("\n")
        self.assertEqual([item[0] for item in self.worker.submitted], ["wizard_create"])

    def test_provider_list_is_async_and_keeps_existing_runtime(self):
        self.ui.config.session = "keep-current"
        self.choose("existing")
        self.assertEqual(self.ui.page, "listing")
        self.assertEqual(self.worker.submitted[0][0], "sessions")
        self.assertEqual(self.ui.config.session, "keep-current")
        self.emit("sessions", [{"name": "target", "hardware": "CPU"}])
        self.choose("session:target")
        action, config = self.worker.submitted[-1]
        self.assertEqual(action, "inspect")
        self.assertEqual(config.session, "target")
        self.assertTrue(config.cpu)
        self.assertNotIn("release", [item[0] for item in self.worker.submitted])

    def test_runtime_list_wait_is_visible_above_menu_and_in_detail_panel(self):
        self.choose("existing")
        self.worker.busy = True
        self.ui.operation_started = 30
        self.ui.theme.roles["accent"] = 123
        screen = Frame()
        with (
            mock.patch.object(wizard.time, "monotonic", return_value=42),
            mock.patch.object(wizard.curses, "doupdate"),
        ):
            self.ui._draw(screen)
        banner = "".join(screen.rows[2])
        self.assertIn("Loading runtimes from Colab", banner)
        self.assertIn("12s elapsed", banner)
        self.assertEqual(screen.attributes[2][1], 123)
        right = "\n".join("".join(row[44:]) for row in screen.rows[:12])
        self.assertIn("IN PROGRESS", right)
        self.assertIn("Waiting for the provider reply", right)
        self.assertEqual([action for action, _ in self.worker.submitted], ["sessions"])

    def test_selected_choice_help_uses_distinct_blue_heading_and_body_roles(self):
        self.ui.theme.roles.update(info_heading=123, info=456, accent=789)
        screen = Frame()
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
        for label, expected in (
            ("ABOUT THIS CHOICE", 123),
            ("Choose compute and storage", 456),
            ("Next / status", 789),
        ):
            match = [
                (index, "".join(row).index(label))
                for index, row in enumerate(screen.rows)
                if label in "".join(row)
            ]
            self.assertEqual(len(match), 1)
            row, column = match[0]
            self.assertEqual(screen.attributes[row][column], expected)

    def test_existing_runtime_named_quit_is_inspected_instead_of_exiting(self):
        self.emit("sessions", [{"name": "quit", "hardware": "G4"}])
        self.assertTrue(self.choose("session:quit"))
        self.assertEqual(self.worker.submitted[-1][0], "inspect")
        self.assertEqual(self.worker.submitted[-1][1].session, "quit")

    def test_empty_runtime_list_returns_to_create_without_allocation(self):
        self.emit("sessions", [])
        self.assertIn("No active", self.ui.notice)
        self.choose("back")
        self.assertEqual(self.ui.page, "home")
        self.assertEqual(self.worker.submitted, [])

    def test_verified_list_absence_clears_runtime_proof_but_retains_settings(self):
        for rows in ([], [{"name": "other-runtime", "hardware": "CPU"}]):
            with self.subTest(rows=rows):
                config = dashboard.Config(
                    session="closed-runtime", ephemeral=True, local_port=8288
                )
                self.ui.config = replace(config)
                self.ready_status()
                self.ui.status.update(
                    installation={"status": "ready"}, models_ready=True
                )
                self.ui.provider = {"name": config.session, "hardware": "G4"}
                self.ui.updated = 42
                self.ui.resume = self.ui.observe_runtime = True
                self.ui.required_model_categories = {"embeddings"}
                self.ui.model_paths_need_restart = True
                self.ui.model_registration_notice = "Old paths require registration."
                self.ui.cleanup_warning = "Previous local cleanup needs attention."

                self.emit("sessions", rows)

                self.assertEqual(self.ui.config, config)
                self.assertEqual(self.ui.status, {})
                self.assertEqual(self.ui.ssh, {})
                self.assertEqual(self.ui.provider, {})
                self.assertEqual(self.ui.updated, 0)
                self.assertFalse(self.ui.resume)
                self.assertFalse(self.ui.observe_runtime)
                self.assertIsNone(self.ui.required_model_categories)
                self.assertFalse(self.ui.model_paths_need_restart)
                self.assertEqual(self.ui.model_registration_notice, "")
                self.assertIn("cleanup", self.ui.cleanup_warning)
                self.assertEqual(self.ui._complete_steps(), set())
                self.assertFalse(self.ui._ready())
                self.assertEqual(self.worker.submitted, [])

    def test_verified_list_with_selected_runtime_retains_current_proof(self):
        self.ui.config.session = "still-running"
        self.ui.config.ephemeral = True
        self.ready_status()
        self.ui.provider = {"name": "still-running", "hardware": "G4"}
        self.ui.updated = 42
        self.ui.resume = self.ui.observe_runtime = True
        self.ui.required_model_categories = {"vae"}
        self.ui.model_registration_notice = "Retain current context."
        status, ssh, provider = self.ui.status, self.ui.ssh, self.ui.provider

        self.emit("sessions", [{"name": "still-running", "hardware": "G4"}])

        self.assertIs(self.ui.status, status)
        self.assertIs(self.ui.ssh, ssh)
        self.assertIs(self.ui.provider, provider)
        self.assertEqual(self.ui.updated, 42)
        self.assertTrue(self.ui.resume)
        self.assertTrue(self.ui.observe_runtime)
        self.assertEqual(self.ui.required_model_categories, {"vae"})
        self.assertEqual(self.ui.model_registration_notice, "Retain current context.")
        self.assertTrue(self.ui._ready())
        self.assertEqual(self.worker.submitted, [])

    def test_failed_session_list_does_not_withdraw_previous_runtime_proof(self):
        self.ui.config.session = "unknown-result"
        self.ready_status()
        self.ui.updated = 42
        status, ssh = self.ui.status, self.ui.ssh

        self.emit("error", "Provider session list could not be verified.")

        self.assertIs(self.ui.status, status)
        self.assertIs(self.ui.ssh, ssh)
        self.assertEqual(self.ui.updated, 42)
        self.assertEqual(self.ui.config.session, "unknown-result")
        self.assertIn("could not be verified", self.ui.error)
        self.assertEqual(self.worker.submitted, [])

    def test_ready_inspection_restores_configuration_without_starting(self):
        config = dashboard.Config(
            session="existing", cpu=True, ephemeral=True, local_port=8288
        )
        self.ready_status()
        self.emit(
            "inspection",
            {
                "config": config,
                "status": self.ui.status,
                "ssh": self.ui.ssh,
                "configuration_known": True,
                "ready": True,
            },
        )
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual(self.ui.config.local_port, 8288)
        self.assertEqual(self.worker.submitted, [])

    def test_existing_unknown_storage_requires_choice_then_resume_only(self):
        config = dashboard.Config(session="existing", cpu=True)
        self.emit(
            "inspection",
            {
                "config": config,
                "status": {},
                "ssh": {},
                "configuration_known": False,
                "ready": False,
            },
        )
        self.assertEqual(self.ui.page, "storage")
        self.assertNotIn("storage", self.ui._complete_steps())
        self.choose("ephemeral")
        self.assertIn("storage", self.ui._complete_steps())
        self.assertNotIn("not yet known", self.ui.notice)
        with mock.patch.object(Path, "is_file", return_value=True):
            self.choose("start")
        self.assertEqual(self.worker.submitted[0][0], "wizard_resume")
        self.assertEqual(self.worker.submitted[0][1].session, "existing")

    def test_unknown_storage_never_enters_ready_even_if_services_are_healthy(self):
        self.ready_status()
        self.emit(
            "inspection",
            {
                "config": dashboard.Config(session="existing"),
                "status": self.ui.status,
                "ssh": self.ui.ssh,
                "configuration_known": False,
                "ready": True,
            },
        )
        self.assertEqual(self.ui.page, "storage")
        self.assertEqual(self.worker.submitted, [])

    def test_existing_known_partial_configuration_goes_to_resume_summary(self):
        self.emit(
            "inspection",
            {
                "config": dashboard.Config(session="existing", ephemeral=True),
                "status": {"installation": {"status": "ready"}},
                "ssh": {},
                "known_config": True,
                "ready": False,
            },
        )
        self.assertEqual(self.ui.page, "summary")
        self.assertTrue(self.ui.resume)
        self.assertIn("installation", self.ui._complete_steps())

    def test_inputs_commit_only_on_enter_and_esc_rolls_back(self):
        self.ui._input("port", "summary")
        for character in "8288":
            self.ui._key(character)
        self.assertEqual(self.ui.config.local_port, 8188)
        self.ui._key("\x1b")
        self.assertEqual(self.ui.config.local_port, 8188)
        self.assertEqual(self.ui.input_value, "")
        self.ui._input("port", "summary")
        for character in "8288":
            self.ui._key(character)
        self.ui._key("\n")
        self.assertEqual(self.ui.config.local_port, 8288)

    def test_invalid_port_or_drive_root_does_not_commit(self):
        for field, value in (
            ("port", "80"),
            ("storage_root", "/content/drive/MyDrive"),
        ):
            with self.subTest(field=field):
                self.ui._input(field, "summary")
                self.ui.input_value = value
                self.ui._key("\n")
                self.assertEqual(self.ui.page, "input")
                self.assertTrue(self.ui.error)
        self.assertEqual(self.ui.config.local_port, 8188)
        self.assertEqual(self.ui.config.storage_root, dashboard.DEFAULT_STORAGE)

    def test_unknown_existing_ssh_blocks_port_change_without_blocking_the_ui(self):
        self.ui.resume = True
        self.ui._input("port", "summary")
        self.ui.input_value = "8288"
        self.ui._key("\n")
        self.assertEqual(self.ui.config.local_port, 8188)
        self.assertIn("Stop and verify", self.ui.error)
        self.assertEqual(self.worker.submitted, [])
        self.ui.ssh = {"running": False}
        self.ui._key("\n")
        self.assertEqual(self.ui.config.local_port, 8288)

    def test_authorization_is_displayed_without_terminal_handoff(self):
        self.emit(
            "auth",
            {
                "url": "https://accounts.example.test/authorization",
                "text": "Authorize in the browser",
                "waiting": True,
                "needs_code": False,
            },
        )
        self.assertEqual(self.ui.page, "pipeline")
        self.assertIn("auth_open", [choice.key for choice in self.ui._choices()])
        self.assertEqual(self.worker.responses, [])
        self.choose("auth_continue")
        self.assertEqual(self.worker.responses, [""])
        self.emit("auth_clear", None)
        self.assertEqual(self.ui.auth, {})

    def test_authorization_code_is_masked_and_never_in_snapshot(self):
        self.emit("auth", {"waiting": True, "needs_code": True})
        self.choose("auth_code")
        for character in "sensitive-example-code":
            self.ui._key(character)
        screen = Frame()
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
        self.assertNotIn("sensitive-example-code", screen.text)
        self.assertIn("****************", screen.text)
        self.assertNotIn("sensitive-example-code", self.ui.snapshot())
        self.assertEqual(self.worker.responses, [])
        self.ui._key("\n")
        self.assertEqual(self.worker.responses, ["sensitive-example-code"])
        self.assertEqual(self.ui.input_value, "")

    def test_authorization_code_esc_never_sends_input(self):
        self.emit("auth", {"waiting": True, "needs_code": True})
        self.choose("auth_code")
        self.ui.input_value = "do-not-send"
        self.ui._key("\x1b")
        self.assertEqual(self.ui.input_value, "")
        self.assertEqual(self.worker.responses, [])

    def test_input_placeholder_disappears_and_text_uses_normal_contrast(self):
        self.ui.theme.roles["input"] = 123
        self.ui.theme.roles["muted"] = 456
        self.ui._input("port", "summary")
        screen = Frame()
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
            self.assertIn("> 8188", screen.text)
            self.assertEqual(screen.attributes[16][2], 456)
            self.ui._key("9")
            self.ui._draw(screen)
        self.assertIn("> 9", "".join(screen.rows[16]))
        self.assertNotIn("8188", "".join(screen.rows[16]))
        self.assertEqual(screen.attributes[16][2], 123)

    def test_frame_is_committed_once_and_never_calls_immediate_refresh(self):
        for page in (
            "home",
            "hardware",
            "gpu",
            "storage",
            "summary",
            "sessions",
            "pipeline",
            "ready",
            "input",
        ):
            with self.subTest(page=page):
                self.ui.page = page
                screen = Frame()
                with mock.patch.object(wizard.curses, "doupdate") as update:
                    self.ui._draw(screen)
                self.assertEqual(screen.updates, 1)
                self.assertEqual(screen.refreshes, 0)
                update.assert_called_once_with()

    def test_empty_separator_does_not_call_curses_with_zero_characters(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (36, 120)
        self.ui._write(screen, 3, 44, "", 60)
        screen.addnstr.assert_not_called()

    def test_real_curses_frame_renders_blank_separators_in_local_pty(self):
        """An actual ncurses draw, using only the offline backend fixture."""
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        environment.pop("NO_COLOR", None)
        code = """
import curses
import sys
sys.path.insert(0, 'scripts')
import dashboard
from wizard import Wizard
ui = Wizard(dashboard.Config(), dashboard.DemoBackend('ready'))
def draw(screen):
    ui.theme.initialize()
    ui._write(screen, 3, 44, '', 60)
    ui._draw(screen)
try:
    curses.wrapper(draw)
    print('EMPTY_FRAME_OK', flush=True)
finally:
    ui.worker.close()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 8
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                if process.poll() is not None and not readable:
                    break
            self.assertEqual(
                process.wait(timeout=2), 0, output.decode(errors="replace")
            )
            self.assertIn(b"EMPTY_FRAME_OK", output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)

    def test_multisize_frames_never_write_out_of_bounds(self):
        for height, width in ((36, 120), (24, 80), (22, 60), (18, 50), (12, 34)):
            for page in ("gpu", "sessions", "summary", "pipeline", "input"):
                with self.subTest(size=(height, width), page=page):
                    self.ui.page = page
                    screen = Frame(height, width)
                    with mock.patch.object(wizard.curses, "doupdate"):
                        self.ui._draw(screen)
                    self.assertIn("COMFYUI", screen.text)

    def test_resize_view_preserves_validation_error_for_small_terminals(self):
        for height, width in ((18, 50), (20, 80), (24, 80)):
            with self.subTest(size=(height, width)):
                self.ui._input("port", "summary")
                self.ui.error = "Use a port between 1024 and 65535."
                screen = Frame(height, width)
                with mock.patch.object(wizard.curses, "doupdate"):
                    self.ui._draw(screen)
                self.assertIn(self.ui.error, screen.text)

    def test_pipeline_right_panel_identifies_selected_runtime(self):
        self.ui.config.session = "the-runtime-being-prepared"
        self.ui._page("pipeline")
        screen = Frame()
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
        right = "\n".join("".join(row[44:]) for row in screen.rows)
        self.assertIn("Runtime: the-runtime-being-prepared", right)

    def test_business_notice_lives_in_right_panel_away_from_field(self):
        self.ui._input("port", "summary")
        self.ui.notice = "A business notice belongs in the detail panel"
        screen = Frame()
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
        left = "\n".join("".join(row[:44]) for row in screen.rows)
        right = "\n".join("".join(row[44:]) for row in screen.rows)
        self.assertNotIn("business notice", left)
        self.assertIn("business notice", right)

    def test_100_percent_transfer_is_not_claimed_as_verified(self):
        self.ui.status = {
            "model_prepare": {
                "status": "running",
                "progress": {
                    "files": [
                        {
                            "path": "model.safetensors",
                            "done_bytes": 100,
                            "total_bytes": 100,
                            "phase": "copy",
                        }
                    ]
                },
            }
        }
        text = "\n".join(value for value, role in self.ui._model_rows(60))
        self.assertIn("100%", text)
        self.assertIn("final validation pending", text)
        self.assertNotIn("SHA256 verified", text)
        self.ui.status["model_prepare"]["progress"]["files"][0]["phase"] = "complete"
        self.ui.status["model_prepare"]["progress"]["files"][0]["verification"] = (
            "stream_sha256"
        )
        text = "\n".join(value for value, role in self.ui._model_rows(60))
        self.assertIn("new streamed SHA256 verified", text)

    def test_unknown_model_total_never_fabricates_percent(self):
        self.ui.status = {
            "model_download": {
                "progress": {
                    "files": [{"path": "file", "done_bytes": 10, "total_bytes": None}]
                }
            }
        }
        text = "\n".join(value for value, role in self.ui._model_rows(60))
        self.assertIn("?%", text)
        self.assertNotIn("100%", text)

    def test_ready_requires_verified_local_http_not_just_remote_http(self):
        self.ready_status()
        self.ui.ssh = {
            "running": True,
            "http_ready": False,
            "url": "http://127.0.0.1:8188",
        }
        self.assertFalse(self.ui._ready())
        self.assertIsNone(self.ui._url())
        self.ui.ssh["http_ready"] = True
        self.assertTrue(self.ui._ready())

    def test_failed_startup_or_different_access_mode_is_never_ready(self):
        self.ready_status()
        self.ui.status["startup"]["status"] = "failed"
        self.assertFalse(self.ui._ready())
        self.ready_status()
        self.ui.status["access_mode"] = "public"
        self.assertFalse(self.ui._ready())

    def test_drive_step_needs_mounted_and_mydrive_ready(self):
        self.ui.status = {"drive": {"mounted": True, "mydrive_ready": False}}
        self.assertNotIn("mount", self.ui._complete_steps())
        self.ui.status["drive"]["mydrive_ready"] = True
        self.assertIn("mount", self.ui._complete_steps())

    def test_drive_authorization_highlights_mount_while_verifying(self):
        self.ui.stage = "authorizing Google Drive"
        self.ui.page = "pipeline"
        self.assertEqual(self.ui._active_step(), "mount")

    def test_render_started_does_not_claim_verified_output(self):
        self.ui.status = {
            "render": {"status": "running", "running": True, "result": {"ok": True}}
        }
        text = "\n".join(row[0] for row in self.ui._details(76))
        self.assertIn("H3 render", text)
        self.assertNotIn("Verified output", text)

    def test_render_verified_output_requires_final_success_and_stopped_controller(self):
        self.ui.status = {
            "render": {
                "status": "succeeded",
                "running": False,
                "result": {
                    "ok": True,
                    "storage": "google-drive",
                    "assets": [{"relative_path": "video/test.mp4", "bytes": 575001}],
                },
            }
        }
        text = "\n".join(row[0] for row in self.ui._details(76))
        self.assertIn("Verified output", text)
        self.assertIn("video/test.mp4", text)
        self.ui.status["render"]["running"] = True
        text = "\n".join(row[0] for row in self.ui._details(76))
        self.assertNotIn("Verified output", text)

    def test_receipt_reuse_does_not_claim_a_new_hash(self):
        self.ui.status = {
            "model_prepare": {
                "progress": {
                    "files": [
                        {
                            "path": "model",
                            "done_bytes": 10,
                            "total_bytes": 10,
                            "phase": "complete",
                            "verification": "verified_receipt_metadata",
                        }
                    ]
                }
            }
        }
        text = "\n".join(value for value, role in self.ui._model_rows(60))
        self.assertIn("receipt metadata reused; no new SHA256", text)
        self.assertNotIn("SHA256 verified", text)

    def test_created_name_does_not_mark_unverified_provider_creation_complete(self):
        self.ui.config.session = "not-yet-confirmed-by-provider"
        self.assertNotIn("session", self.ui._complete_steps())
        self.ui.provider = {"name": self.ui.config.session}
        self.assertIn("session", self.ui._complete_steps())

    def test_pipeline_only_enters_ready_when_real_results_allow_it(self):
        self.ui._page("pipeline")
        self.emit("done", "wizard_create")
        self.assertEqual(self.ui.page, "summary")
        self.ready_status()
        self.ui._page("pipeline")
        self.emit("done", "wizard_resume")
        self.assertEqual(self.ui.page, "ready")

    def test_failure_never_retries_creation_without_inspection(self):
        self.ui.config.session = "creation-outcome-unknown"
        self.emit("error", "Provider timed out; outcome unknown")
        self.assertEqual(self.ui.page, "failure")
        self.assertNotIn("start", [choice.key for choice in self.ui._choices()])
        self.choose("inspect")
        self.assertEqual(self.worker.submitted[0][0], "inspect")

    def test_existing_partial_or_failed_runtime_can_stop_through_advanced(self):
        for page in ("summary", "failure"):
            with self.subTest(page=page):
                self.ui.config.session = "existing"
                self.ui.resume = True
                self.ui._page(page)
                self.choose("advanced")
                self.assertEqual(self.ui.page, "advanced")
                self.choose("stop")
                self.assertEqual(self.worker.submitted[-1][0], "stop")
                self.assertEqual(self.worker.submitted[-1][1].session, "existing")

    def test_unverified_connection_leaves_ready_without_automatic_restart(self):
        self.ui._page("ready")
        self.ready_status()
        self.ui.ssh["http_ready"] = False
        self.emit("done", "status")
        self.assertEqual(self.ui.page, "summary")
        self.assertTrue(self.ui.resume)
        self.assertIn("no longer verified", self.ui.notice)
        self.assertEqual(self.worker.submitted, [])

    def test_overviews_refresh_known_runtime_without_restarting_services(self):
        self.ui.config.session = "selected"
        self.ui.observe_runtime = True
        for page in ("summary", "advanced", "ready"):
            with self.subTest(page=page):
                self.ui._page(page)
                self.ui.last_refresh = 0
                with mock.patch.object(wizard.time, "monotonic", return_value=42):
                    self.ui._refresh()
                self.assertEqual(self.worker.submitted[-1][0], "status")
                self.assertEqual(self.ui.page, page)
        self.assertEqual(
            [action for action, _ in self.worker.submitted], ["status"] * 3
        )

    def test_unknown_configuration_and_busy_list_never_poll_or_promote_ready(self):
        self.ui.config.session = "selected"
        self.ready_status()
        for page in ("summary", "advanced", "storage", "listing", "failure"):
            with self.subTest(page=page):
                self.ui._page(page)
                self.ui._refresh()
                self.emit("done", "status")
                self.assertEqual(self.ui.page, page)
        self.assertEqual(self.worker.submitted, [])

    def test_fresh_remote_and_ssh_events_mark_startup_complete_without_reload(self):
        self.ui.config.session = "selected"
        self.ui.observe_runtime = True
        self.ui.resume = True
        self.ui._page("summary")
        self.ready_status()
        ready = self.ui.status
        ssh = self.ui.ssh
        self.ui.status, self.ui.ssh = {"http_ready": False}, {"running": False}
        self.assertNotIn("startup", self.ui._complete_steps())
        self.emit("ssh", ssh)
        self.assertNotIn("startup", self.ui._complete_steps())
        self.emit("status", ready)
        self.emit("done", "status")
        self.assertEqual(self.ui.page, "ready")
        self.assertIn("startup", self.ui._complete_steps())
        self.ui.theme.roles["comfy"] = 123
        screen = Frame()
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
        self.assertIn("+ ComfyUI + SSH", "".join(screen.rows[11]))
        self.assertEqual(screen.attributes[11][2], 123)
        self.assertEqual(self.worker.submitted, [])

    def test_ssh_probe_failure_invalidates_previous_ready_snapshot(self):
        self.ui.observe_runtime = True
        self.ui._page("ready")
        self.ready_status()
        self.emit("ssh_error", "fixture status check failed")
        self.assertFalse(self.ui._ready())
        self.assertNotIn("startup", self.ui._complete_steps())
        self.emit("done", "status")
        self.assertEqual(self.ui.page, "summary")
        self.assertIn("no longer verified", self.ui.notice)

    def test_background_poll_does_not_replace_the_last_startup_stage(self):
        self.ui.stage = "connecting local SSH"
        self.emit("stage", "status")
        self.assertEqual(self.ui.stage, "connecting local SSH")

    def test_editing_configuration_disables_old_snapshot_promotion(self):
        self.ui.config.session = "selected"
        self.ui.observe_runtime = True
        self.ui.config.storage_root = "/content/drive/MyDrive/old"
        self.ready_status()
        self.ui._input("storage_root", "settings")
        self.ui.input_value = "/content/drive/MyDrive/new"
        self.ui._key("\n")
        self.assertFalse(self.ui.observe_runtime)
        self.ui._page("summary")
        self.emit("done", "status")
        self.assertEqual(self.ui.page, "summary")

    def test_run_shortens_escape_delay_and_preserves_arrow_navigation(self):
        screen = Frame()
        screen.timeout = mock.Mock()
        screen.getkey = mock.Mock(side_effect=["KEY_DOWN", "KEY_DOWN", "\n"])
        with (
            mock.patch.object(self.ui.theme, "initialize"),
            mock.patch.object(wizard.curses, "set_escdelay") as delay,
            mock.patch.object(wizard.curses, "curs_set"),
            mock.patch.object(wizard.curses, "doupdate"),
        ):
            self.ui.run(screen)
        delay.assert_called_once_with(50)
        self.assertTrue(self.ui.quitting)
        self.assertTrue(self.worker.closed)
        self.assertEqual(self.worker.submitted, [])

    def test_release_defaults_to_cancel_and_requires_its_own_confirmation(self):
        self.ui.config.session = "selected"
        self.ui._page("ready")
        self.choose("release")
        self.assertEqual(self.ui.page, "release_confirm")
        self.assertEqual(self.ui.selected, 1)
        self.assertEqual(self.worker.submitted, [])
        self.ui._key("\n")
        self.assertEqual(self.ui.page, "ready")
        self.choose("release")
        self.choose("confirm_release")
        self.assertEqual(self.worker.submitted[-1][0], "release")
        self.assertEqual(self.worker.submitted[-1][1].session, "selected")

    def test_release_completion_clears_only_matching_runtime(self):
        self.ui.config.session = "selected"
        self.emit("released", (dashboard.Config(session="other"), ""))
        self.assertEqual(self.ui.config.session, "selected")
        self.emit("released", (dashboard.Config(session="selected"), ""))
        self.assertEqual(self.ui.config.session, "")
        self.assertEqual(self.ui.page, "home")

    def test_quit_requires_enter_and_never_releases(self):
        self.assertTrue(self.ui._key("q"))
        self.assertFalse(self.choose("quit"))
        self.assertEqual(self.worker.submitted, [])

    def test_worker_rejection_keeps_summary_without_creating_session(self):
        self.new_cpu_summary()
        self.worker.accept = False
        with mock.patch.object(Path, "is_file", return_value=True):
            self.choose("start")
        self.assertEqual(self.ui.page, "summary")
        self.assertEqual(self.ui.config.session, "")
        self.assertEqual(self.worker.submitted, [])

    def test_demo_navigation_never_creates_ssh_browser_or_provider_calls(self):
        ui = wizard.Wizard(
            dashboard.Config(), dashboard.DemoBackend("ready"), self.worker
        )
        self.ui = ui
        self.choose("open")
        self.assertIn("no browser", ui.notice)
        ui._key("\x1b")
        self.new_cpu_summary()
        with mock.patch.object(Path, "is_file", return_value=True):
            self.choose("start")
        self.assertEqual(ui.page, "summary")
        self.assertEqual(self.worker.submitted, [])

    def test_new_flow_replaces_old_session_list_notice_and_step_context(self):
        self.ui.notice = (
            "No active named runtimes. Return and choose Create a new runtime."
        )
        self.ui.stage = "listing sessions"
        self.choose("new")
        self.assertEqual(self.ui.page, "hardware")
        self.assertNotIn("No active", self.ui.notice)
        self.assertEqual(self.ui.stage, "Choose compute hardware")
        self.choose("gpu")
        self.assertIn("GPU type", self.ui.notice)
        self.choose("G4")
        self.assertIn("models and outputs", self.ui.notice)
        self.choose("ephemeral")
        self.assertIn("Review", self.ui.notice)

    def test_cleanup_warning_survives_new_flow_notice_with_released_runtime_name(self):
        self.ui.config.session = "previous-runtime"
        self.emit("released", (replace(self.ui.config), "Service cleanup timed out"))
        self.choose("new")
        details = "\n".join(text for text, _role in self.ui._details(60))
        self.assertIn("Previous cleanup warning", details)
        self.assertIn("Released previous-runtime", details)
        self.assertIn("Service cleanup timed out", details)
        self.assertNotIn("Selected VM released", self.ui.notice)

    def test_refresh_models_uses_enter_and_explains_same_runtime_operation_in_blue(
        self,
    ):
        self.ui.config = replace(self.ui.config, session="selected", cpu=False)
        self.ui.observe_runtime = True
        self.ui._page("advanced")
        keys = [choice.key for choice in self.ui._choices()]
        self.ui.selected = keys.index("prepare_refresh")
        self.assertEqual(self.worker.submitted, [])
        details = self.ui._details(80)
        self.assertTrue(
            any(
                role == "info_heading" and "Prepare / refresh models" in text
                for text, role in details
            )
        )
        self.assertIn("extra.json", "\n".join(text for text, _role in details))
        self.ui._key("\n")
        self.assertEqual(self.worker.submitted[-1][0], "prepare_refresh")
        self.assertEqual(self.worker.submitted[-1][1].session, "selected")
        self.assertEqual(self.ui.page, "pipeline")

    def test_refresh_models_requires_known_gpu_runtime_before_submission(self):
        for cpu, observed, session in (
            (True, True, "selected"),
            (False, False, "selected"),
            (False, True, ""),
        ):
            with self.subTest(cpu=cpu, observed=observed, session=session):
                self.ui.config = replace(self.ui.config, session=session, cpu=cpu)
                self.ui.observe_runtime = observed
                self.ui._page("advanced")
                self.choose("prepare_refresh")
                self.assertEqual(self.worker.submitted, [])
                self.assertIn("GPU runtime" if cpu else "Inspect", self.ui.notice)

    def test_model_refresh_busy_does_not_show_ready_from_previous_service_snapshot(
        self,
    ):
        self.ready_status()
        self.assertTrue(self.ui._ready())
        self.ui.last_operation = "prepare_refresh"
        self.worker.busy = True
        self.assertFalse(self.ui._ready())
        self.assertIsNone(self.ui._url())
        self.ui.status["models_ready"] = True
        self.ui.status["model_prepare"] = {"running": True}
        self.assertNotIn("models", self.ui._complete_steps())
        self.worker.busy = False
        self.ui.status["model_prepare"]["running"] = False
        self.ui._page("pipeline")
        self.emit(
            "notice",
            "Model lists prepared and verified. Refresh ComfyUI to see new models.",
        )
        self.emit("done", "prepare_refresh")
        self.assertEqual(self.ui.page, "ready")
        self.assertIn("Refresh ComfyUI", self.ui.notice)

    def test_cpu_models_step_is_explicitly_skipped_instead_of_verified(self):
        self.ui.config.cpu = True
        self.ui.status["models_ready"] = True
        self.assertNotIn("models", self.ui._complete_steps())
        screen = Frame()
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
        self.assertIn("Models · skipped on CPU", screen.text)
        self.ui._page("summary")
        self.assertIn(
            "Models: Skipped in CPU mode",
            "\n".join(text for text, _role in self.ui._details(80)),
        )

    def test_verified_files_with_missing_model_registration_require_explicit_restart(
        self,
    ):
        self.ready_status()
        self.ui._page("pipeline")
        self.ui.status["model_search_categories"] = ["loras"]
        self.emit("model_registration", ["loras", "embeddings"])
        self.emit(
            "notice",
            "Files verified; Stop services then Continue missing startup steps.",
        )
        self.emit("done", "prepare_refresh")
        self.assertEqual(self.ui.page, "summary")
        self.assertFalse(self.ui._ready())
        self.assertTrue(self.ui.model_paths_need_restart)
        self.assertIn("embeddings", self.ui.model_registration_notice)
        self.assertIn("Stop services", self.ui.notice)
        self.assertEqual(self.worker.submitted, [])
        self.emit(
            "status",
            {
                **self.ui.status,
                "comfyui_alive": False,
                "http_ready": False,
                "model_search_categories": None,
            },
        )
        self.assertIn(
            "Continue missing startup steps", self.ui.model_registration_notice
        )
        self.assertNotIn("Choose Stop services", self.ui.model_registration_notice)
        status = {
            **self.ui.status,
            "comfyui_alive": True,
            "http_ready": True,
            "model_search_categories": ["loras", "embeddings"],
        }
        self.emit("status", status)
        self.assertFalse(self.ui.model_paths_need_restart)
        self.assertEqual(self.ui.model_registration_notice, "")
        self.assertTrue(self.ui._ready())

    def test_unknown_old_process_model_paths_are_not_claimed_ready_and_scope_resets(
        self,
    ):
        self.ready_status()
        self.ui.config.session = "old-runtime"
        self.emit("model_registration", ["vae"])
        self.assertFalse(self.ui._ready())
        self.assertIn("unknown", self.ui.model_registration_notice)
        self.ui._page("home")
        self.choose("new")
        self.assertFalse(self.ui.model_paths_need_restart)
        self.assertIsNone(self.ui.required_model_categories)
        self.assertEqual(self.ui.model_registration_notice, "")

    def test_bad_gpu_model_list_is_shown_before_allocating_a_name_or_submitting_startup(
        self,
    ):
        self.choose("new")
        self.choose("gpu")
        self.choose("G4")
        self.choose("ephemeral")
        with mock.patch.object(
            wizard,
            "validate_model_plan",
            side_effect=wizard.DashboardError("Unsupported local model category"),
        ):
            self.choose("start")
        self.assertEqual(self.ui.page, "summary")
        self.assertEqual(self.ui.config.session, "")
        self.assertEqual(self.worker.submitted, [])
        self.assertIn("Unsupported", self.ui.error)
        self.assertIn("No startup action", self.ui.notice)

    def test_gpu_summary_displays_current_merged_model_count_and_bytes(self):
        self.ui._page("summary")
        module = dashboard._model_manifest_module()
        manifest = module.load_manifests(module.DEFAULT_MANIFEST)
        details = "\n".join(text for text, _role in self.ui._details(80))
        self.assertIn(f"{len(manifest['files'])} pinned model files", details)
        self.assertIn(f"{manifest['total_size_bytes'] / 1_000_000_000:.2f} GB", details)


if __name__ == "__main__":
    unittest.main()
