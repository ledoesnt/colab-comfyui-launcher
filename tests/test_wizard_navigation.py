"""Return paths and inert loading menus; no provider or browser is used."""

import importlib.util
import queue
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dashboard = sys.modules.get("dashboard") or load_module(
    "dashboard", ROOT / "scripts/dashboard.py"
)
wizard = load_module("navigation_wizard", ROOT / "scripts/wizard.py")


class LocalWorker:
    def __init__(self):
        self.events = queue.Queue()
        self.busy = False
        self.submitted = []
        self.login_cancelled = False

    def submit(self, action, config):
        if self.busy:
            return False
        self.busy = True
        self.submitted.append((action, replace(config)))
        return True

    def cancel_account(self):
        self.login_cancelled = True
        return True


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)
        self.ui.account = {
            "state": "authenticated",
            "message": "Local fixture login verified.",
        }

    def choose(self, key):
        choices = self.ui._choices()
        self.ui.selected = [choice.key for choice in choices].index(key)
        return self.ui._key("\n")

    def emit(self, kind, value):
        self.worker.events.put((kind, value))
        self.ui._events()

    def finish(self, action):
        self.worker.busy = False
        self.emit("done", action)

    def test_home_has_one_login_entry_without_check_or_sign_in_commands(self):
        choices = self.ui._choices()
        entries = [choice for choice in choices if choice.key.startswith("account")]
        self.assertEqual(
            [(choice.key, choice.label) for choice in entries],
            [("account", "Colab login")],
        )
        self.choose("account")
        self.assertEqual(self.ui.page, "account")
        self.assertEqual(self.worker.submitted, [])
        self.choose("back")
        self.assertEqual(self.ui.page, "home")
        self.assertFalse(self.ui.quitting)

    def test_verified_account_offers_only_read_only_recheck_and_back(self):
        self.choose("account")
        choices = self.ui._choices()
        self.assertEqual(
            [(row.key, row.label) for row in choices],
            [("account_check", "Recheck login"), ("back", "Back")],
        )
        self.choose("account_check")
        self.assertNotIn("verified", self.ui.account["message"])
        self.assertIn("read-only", self.ui.account["message"])
        self.assertEqual(
            [action for action, _ in self.worker.submitted], ["account_status"]
        )
        self.assertNotIn("account_login", [choice.key for choice in self.ui._choices()])

    def test_unverified_account_distinguishes_check_from_sign_in(self):
        for state in ("unknown", "unavailable", "not_authenticated"):
            with self.subTest(state=state):
                self.ui.account = {"state": state}
                self.ui._page("account")
                self.assertEqual(
                    [(choice.key, choice.label) for choice in self.ui._choices()],
                    [
                        ("account_check", "Check login status"),
                        ("account_login", "Sign in"),
                        ("back", "Back"),
                    ],
                )
        self.choose("account_check")
        self.assertEqual(self.worker.submitted[-1][0], "account_status")
        self.emit("account", {"state": "not_authenticated"})
        self.finish("account_status")
        self.choose("account_login")
        self.assertEqual(self.worker.submitted[-1][0], "account_login")

    def test_all_waiting_menus_keep_back_and_never_offer_exit(self):
        self.worker.busy = True
        for page in ("listing", "inspect", "account", "model_adding", "unknown_page"):
            with self.subTest(page=page):
                self.ui._page(page)
                choices = self.ui._choices()
                self.assertEqual(
                    [(row.key, row.enabled) for row in choices],
                    [("loading", False), ("back", True)],
                )
                self.assertEqual(choices[0].label, "Loading...")
                self.assertEqual(choices[self.ui.selected].key, "back")
                self.ui.selected = 0
                self.assertTrue(self.ui._key("\n"))
                self.assertEqual(self.ui.page, page)
                self.assertEqual(self.worker.submitted, [])
                self.ui._key("KEY_DOWN")
                self.assertEqual(self.ui._choices()[self.ui.selected].key, "back")
                self.ui._key("KEY_UP")
                self.assertEqual(self.ui._choices()[self.ui.selected].key, "back")

    def test_authenticated_listing_back_returns_to_ready_without_releasing(self):
        self.ui.config.session = "keep-current"
        self.ui._page("ready")
        self.choose("existing")
        self.assertEqual(self.ui.page, "listing")
        self.choose("back")
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual(self.ui.config.session, "keep-current")
        self.emit("sessions", [{"name": "keep-current", "hardware": "G4"}])
        self.emit("error", "Local fixture list failed")
        self.finish("sessions")
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual([action for action, _ in self.worker.submitted], ["sessions"])

    def test_completed_session_list_back_returns_to_its_real_origin(self):
        self.ui._page("ready")
        self.choose("existing")
        self.emit("sessions", [{"name": "fixture", "hardware": "CPU"}])
        self.finish("sessions")
        self.assertEqual(self.ui.page, "sessions")
        self.choose("back")
        self.assertEqual(self.ui.page, "ready")

    def test_refresh_list_back_keeps_cached_list_and_late_result_does_not_reenter(self):
        self.ui._page("ready")
        self.choose("existing")
        self.emit("sessions", [{"name": "fixture", "hardware": "CPU"}])
        self.finish("sessions")
        self.choose("refresh_list")
        self.assertEqual(self.ui.page, "listing")
        self.choose("back")
        self.assertEqual(self.ui.page, "sessions")
        self.choose("back")
        self.assertEqual(self.ui.page, "ready")
        self.emit("sessions", [{"name": "late-fixture", "hardware": "CPU"}])
        self.finish("sessions")
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual(self.ui.sessions[0]["name"], "late-fixture")

    def test_inspection_back_returns_to_list_and_discards_late_runtime_context(self):
        self.ui.sessions = [{"name": "fixture", "hardware": "CPU"}]
        self.ui._page("sessions")
        self.choose("session:fixture")
        self.assertEqual(self.ui.page, "inspect")
        self.choose("back")
        self.assertEqual(self.ui.page, "sessions")
        self.emit(
            "inspection",
            {
                "config": dashboard.Config(session="fixture", cpu=True),
                "status": {"installation": {"status": "ready"}},
                "configuration_known": True,
                "ready": False,
            },
        )
        self.emit("error", "Local fixture inspection failed")
        self.finish("inspect")
        self.assertEqual(self.ui.page, "sessions")
        self.assertEqual(self.ui.status, {})
        self.assertEqual(self.ui.config.session, "")
        self.assertEqual([action for action, _ in self.worker.submitted], ["inspect"])

    def test_login_gate_preserves_listing_origin_and_back_dismisses_intent(self):
        self.ui.account = {"state": "not_authenticated"}
        self.ui._page("ready")
        self.choose("existing")
        self.assertEqual(self.ui.page, "account")
        self.choose("account_login")
        self.choose("back")
        self.assertEqual(self.ui.page, "ready")
        self.emit("account", {"state": "authenticated"})
        self.finish("account_login")
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual(
            [action for action, _ in self.worker.submitted], ["account_login"]
        )

    def test_background_login_check_error_does_not_hijack_home(self):
        self.ui.last_operation = "account_status"
        self.emit("error", "Local fixture network unavailable")
        self.finish("account_status")
        self.assertEqual(self.ui.page, "home")
        self.assertEqual(self.ui.account["state"], "unavailable")

    def test_unknown_login_gate_defaults_to_back_after_starting_check(self):
        self.ui.account = {"state": "unknown", "message": "Old fixture status"}
        self.choose("new")
        self.assertEqual(self.ui.page, "account")
        self.assertTrue(self.worker.busy)
        self.assertEqual(self.ui._choices()[self.ui.selected].key, "back")
        self.assertIn("read-only", self.ui.account["message"])
        self.ui._key("\n")
        self.assertEqual(self.ui.page, "home")
        self.assertIsNone(self.ui.account_intent)

    def test_selection_pages_use_consistent_back_without_changing_configuration(self):
        for page, parent in (
            ("hardware", "home"),
            ("gpu", "hardware"),
            ("storage", "gpu"),
            ("summary", "storage"),
            ("settings", "summary"),
            ("key_confirm", "summary"),
        ):
            with self.subTest(page=page):
                self.ui._page(page)
                old = replace(self.ui.config)
                back = next(
                    choice for choice in self.ui._choices() if choice.key == "back"
                )
                self.assertEqual(back.label, "Back")
                self.choose("back")
                self.assertEqual(self.ui.page, parent)
                self.assertEqual(self.ui.config, old)
        self.assertEqual(self.worker.submitted, [])

    def test_back_from_cross_session_inspection_restores_original_ready_runtime(self):
        self.ui.config.session = "original-runtime"
        self.ui.config.ephemeral = True
        self.ui.status = {
            "comfyui_alive": True,
            "http_ready": True,
            "access_mode": "local",
            "tunnel_alive": False,
            "startup": {"ok": True, "status": "started", "access_mode": "local"},
        }
        self.ui.ssh = {
            "running": True,
            "http_ready": True,
            "url": "http://127.0.0.1:8188",
        }
        self.ui.provider = {"name": "original-runtime"}
        self.ui.resume = self.ui.observe_runtime = True
        self.ui.updated = 42
        self.ui._page("ready")
        original_config = replace(self.ui.config)
        original_status, original_ssh, original_provider = (
            self.ui.status,
            self.ui.ssh,
            self.ui.provider,
        )
        self.choose("existing")
        self.emit(
            "sessions",
            [
                {"name": "original-runtime", "hardware": "G4"},
                {"name": "other-runtime", "hardware": "G4"},
            ],
        )
        self.finish("sessions")
        self.choose("session:other-runtime")
        self.assertEqual(self.ui.config.session, "other-runtime")
        self.choose("back")
        self.choose("back")
        self.assertEqual(self.ui.page, "ready")
        self.emit(
            "inspection",
            {
                "config": dashboard.Config(session="other-runtime", cpu=True),
                "status": original_status,
                "ssh": original_ssh,
                "provider": {"name": "other-runtime"},
                "configuration_known": True,
                "ready": True,
            },
        )
        self.finish("inspect")
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual(self.ui.config, original_config)
        self.assertIs(self.ui.status, original_status)
        self.assertIs(self.ui.ssh, original_ssh)
        self.assertIs(self.ui.provider, original_provider)
        self.assertTrue(self.ui.observe_runtime)
        self.assertTrue(self.ui._ready())
        self.assertEqual(self.ui.updated, 42)

    def test_back_clears_old_choice_notice_without_losing_provider_status(self):
        self.ui.status = {"models_ready": True}
        self.ui._page("gpu")
        self.ui.notice = "Choose a GPU type for the new runtime."
        self.choose("back")
        self.assertEqual(self.ui.page, "hardware")
        self.assertEqual(self.ui.notice, "")
        self.choose("back")
        self.assertEqual(self.ui.page, "home")
        self.assertEqual(self.ui.notice, "")
        self.assertEqual(self.ui.status, {"models_ready": True})

    def test_advanced_and_model_pages_return_to_actual_origin(self):
        self.ui.config.session = "retain-fixture-runtime"
        self.ui._page("failure")
        self.choose("advanced")
        self.choose("back")
        self.assertEqual(self.ui.page, "failure")
        for origin in ("home", "summary", "advanced"):
            with (
                self.subTest(origin=origin),
                mock.patch.object(wizard, "model_catalog_entries", return_value=[]),
            ):
                self.ui._page(origin)
                entry = next(
                    choice for choice in self.ui._choices() if choice.key == "models"
                )
                self.assertEqual(entry.label, "Manage models")
                self.choose("models")
                self.choose("back")
                self.assertEqual(self.ui.page, origin)

    def test_model_confirmation_back_preserves_category_and_source_form(self):
        self.ui.model_entries = []
        self.ui.config.model_url = (
            "https://huggingface.co/fixture/model/blob/main/weight.safetensors"
        )
        self.ui.config.model_path = "loras/weight.safetensors"
        self.ui._page("model_add_confirm")
        self.choose("back")
        self.assertEqual(self.ui.page, "model_category")
        selected = self.ui._choices()[self.ui.selected]
        self.assertEqual(selected.key, "category:loras")
        self.choose("back")
        self.assertEqual(self.ui.page, "input")
        self.assertEqual(self.ui.input_value, self.ui.config.model_url)
        self.ui._key("\x1b")
        self.assertEqual(self.ui.page, "models")
        self.assertEqual(self.ui.config.model_url, "")
        self.assertEqual(self.ui.config.model_path, "")

    def test_existing_key_form_escape_returns_to_key_confirmation(self):
        self.ui._page("key_confirm")
        self.choose("choose_key")
        self.assertEqual(self.ui.page, "input")
        self.ui._key("\x1b")
        self.assertEqual(self.ui.page, "key_confirm")
        self.assertEqual(self.worker.submitted, [])

    def test_model_metadata_save_back_continues_and_late_result_preserves_new_form(
        self,
    ):
        self.ui.config.model_url = (
            "https://huggingface.co/fixture/old/blob/main/old.safetensors"
        )
        self.ui.config.model_path = "loras/old.safetensors"
        self.ui._page("model_add_confirm")
        self.choose("model_add_save")
        self.assertEqual(self.ui.page, "model_adding")
        self.assertFalse(self.ui._choices()[0].enabled)
        back = next(choice for choice in self.ui._choices() if choice.key == "back")
        self.assertIn("continues", back.detail)
        self.choose("back")
        self.assertEqual(self.ui.page, "models")
        self.assertTrue(self.worker.busy)
        self.ui._input("model_url", "models")
        self.ui.input_value = "next-source-being-typed"
        self.ui.config.model_url = "next-source"
        self.ui.config.model_path = "loras/next.safetensors"
        entries = [{"path": "loras/old.safetensors", "auto_download": True}]
        with mock.patch.object(wizard, "model_catalog_entries", return_value=entries):
            self.emit("model_added", ("loras/old.safetensors", "extra.json"))
        self.finish("model_add")
        self.assertEqual(self.ui.page, "input")
        self.assertEqual(self.ui.input_value, "next-source-being-typed")
        self.assertEqual(self.ui.config.model_url, "next-source")
        self.assertEqual(self.ui.config.model_path, "loras/next.safetensors")
        self.assertEqual(self.ui.model_entries, entries)
        self.assertEqual([action for action, _ in self.worker.submitted], ["model_add"])

    def test_unknown_page_loading_back_returns_home_without_exit(self):
        self.ui._page("unknown_page")
        self.choose("back")
        self.assertEqual(self.ui.page, "home")
        self.assertFalse(self.ui.quitting)
        self.assertEqual(self.worker.submitted, [])


if __name__ == "__main__":
    unittest.main()
