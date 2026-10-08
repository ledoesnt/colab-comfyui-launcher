"""Account-change navigation and proof withdrawal without any provider calls."""

import unittest
from dataclasses import replace
from unittest import mock

from test_wizard import Frame, LocalWorker, dashboard, wizard

AUTHENTICATED = {"state": "authenticated", "message": "Fixture login verified."}
SIGNED_OUT = {
    "state": "not_authenticated",
    "reason": "local_oauth_cache_removed",
    "message": "Fixture local sign-out verified.",
}


class AccountSwitchUITests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)
        self.ui.account = dict(AUTHENTICATED)

    def choose(self, key):
        choices = self.ui._choices()
        self.ui.selected = [choice.key for choice in choices].index(key)
        return self.ui._key("\n")

    def emit(self, kind, value):
        self.worker.events.put((kind, value))
        self.ui._events()

    def draw(self, height=24, width=80):
        frame = Frame(height, width)
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(frame)
        return frame

    def prove_old_runtime(self):
        self.ui.config = dashboard.Config(
            session="previous-account-runtime",
            storage_root="/content/drive/MyDrive/fixture-launcher",
            gpu="G4",
            ephemeral=True,
            local_port=8288,
            identity="/fixture/dedicated-key",
            create_key=True,
            verify_cache=True,
            download_workers=3,
        )
        self.ui.status = {
            "comfyui_alive": True,
            "http_ready": True,
            "tunnel_alive": False,
            "access_mode": "local",
            "models_ready": True,
            "startup": {"ok": True, "status": "started", "access_mode": "local"},
        }
        self.ui.ssh = {
            "running": True,
            "http_ready": True,
            "url": "http://127.0.0.1:8288",
        }
        self.ui.provider = {"name": self.ui.config.session, "hardware": "G4"}
        self.ui.sessions = [dict(self.ui.provider)]
        self.ui.confirmed_steps = {"account", "session", "storage", "models"}
        self.ui.required_model_categories = {"loras"}
        self.ui.model_registration_notice = "Fixture registration proof."
        self.ui.updated = self.ui.last_refresh = 42.0
        self.ui.observe_runtime = self.ui.resume = True
        self.assertTrue(self.ui._ready())
        return replace(self.ui.config)

    def confirm_change(self, action):
        self.ui._page("home")
        self.choose("account")
        self.choose(action)
        self.choose("account_confirm_change")

    def assert_detached(self, original):
        self.assertEqual(
            self.ui.config, replace(original, session="", create_key=False)
        )
        self.assertEqual(self.ui.status, {})
        self.assertEqual(self.ui.ssh, {})
        self.assertEqual(self.ui.provider, {})
        self.assertEqual(self.ui.sessions, [])
        self.assertEqual(self.ui.confirmed_steps, set())
        self.assertIsNone(self.ui.required_model_categories)
        self.assertFalse(self.ui.model_paths_need_restart)
        self.assertEqual(self.ui.model_registration_notice, "")
        self.assertEqual(self.ui.updated, 0)
        self.assertEqual(self.ui.last_refresh, 0)
        self.assertFalse(self.ui.observe_runtime)
        self.assertFalse(self.ui.resume)
        self.assertFalse(self.ui.runtime_events_allowed)
        self.assertFalse(self.ui._ready())
        self.assertIsNone(self.ui._url())
        self.assertIn(original.session, self.ui.detached_runtime_notice)
        self.assertIn("was not stopped", self.ui.detached_runtime_notice)

    def test_home_places_single_account_entry_before_runtime_choices(self):
        choices = self.ui._choices()
        self.assertEqual(choices[0].key, "account")
        self.assertEqual(choices[0].label, "Colab login")
        self.assertEqual(choices[1].key, "new")
        self.assertEqual(choices[2].key, "existing")
        self.assertEqual(sum(choice.key.startswith("account") for choice in choices), 1)
        self.choose("account")
        self.assertEqual(self.ui.page, "account")
        self.assertEqual(self.worker.submitted, [])

    def test_authenticated_account_has_ordered_read_switch_signout_and_back(self):
        self.choose("account")
        self.assertEqual(
            [(choice.key, choice.label) for choice in self.ui._choices()],
            [
                ("account_check", "Recheck login"),
                ("account_switch", "Switch account"),
                ("account_logout", "Sign out"),
                ("back", "Back"),
            ],
        )
        self.assertEqual(self.worker.submitted, [])

    def test_unverified_account_places_signin_before_switch_and_signout(self):
        for state in ("unknown", "unavailable", "not_authenticated"):
            with self.subTest(state=state):
                self.ui.account = {"state": state}
                self.ui._page("account")
                self.assertEqual(
                    [choice.key for choice in self.ui._choices()],
                    [
                        "account_check",
                        "account_login",
                        "account_switch",
                        "account_logout",
                        "back",
                    ],
                )
        self.assertEqual(self.worker.submitted, [])

    def test_account_changes_default_to_independent_cancel_without_submission(self):
        for action in ("account_logout", "account_switch"):
            with self.subTest(action=action):
                self.ui._page("account")
                self.choose(action)
                self.assertEqual(self.ui.page, "account_confirm")
                self.assertEqual(self.ui.account_change_action, action)
                choices = self.ui._choices()
                self.assertEqual(choices[self.ui.selected].key, "account_cancel_change")
                self.assertEqual(choices[self.ui.selected].label, "Cancel")
                self.assertEqual(self.worker.submitted, [])
                self.ui._key("\n")
                self.assertEqual(self.ui.page, "account")
                self.assertEqual(self.ui.account, AUTHENTICATED)
                self.assertEqual(self.worker.submitted, [])

    def test_esc_from_confirmation_retains_account_and_resource_proofs(self):
        original = self.prove_old_runtime()
        for action in ("account_logout", "account_switch"):
            with self.subTest(action=action):
                self.ui._page("account")
                self.choose(action)
                self.ui._key("\x1b")
                self.assertEqual(self.ui.page, "account")
                self.assertEqual(self.ui.config, original)
                self.assertTrue(self.ui._ready())
                self.assertEqual(self.worker.submitted, [])

    def test_confirmation_frames_explain_local_scope_and_keep_cancel_visible(self):
        for action in ("account_logout", "account_switch"):
            self.ui._page("account")
            self.choose(action)
            for height, width in ((24, 80), (36, 120)):
                with self.subTest(action=action, width=width):
                    text = self.draw(height, width).text
                    self.assertIn("> Cancel", text)
                    self.assertIn("Confirm ·", text)
                    self.assertIn("local Colab CLI", text)
                    self.assertIn("Enter confirm", text)
            self.assertEqual(self.worker.submitted, [])

    def test_explicit_confirmation_submits_only_selected_account_action(self):
        for action in ("account_logout", "account_switch"):
            with self.subTest(action=action):
                self.worker.submitted.clear()
                self.ui.account = dict(AUTHENTICATED)
                self.confirm_change(action)
                self.assertEqual([item[0] for item in self.worker.submitted], [action])
                self.assertEqual(self.ui.page, "account")
                self.assertEqual(self.ui.account["state"], "checking")
                self.assertIsNone(self.ui.account_intent)

    def test_signed_out_event_withdraws_proofs_only_after_backend_success(self):
        for action in ("account_logout", "account_switch"):
            with self.subTest(action=action):
                self.worker.submitted.clear()
                self.ui.account = dict(AUTHENTICATED)
                original = self.prove_old_runtime()
                self.ui.auth = {"waiting": True, "url": "https://fixture.invalid/auth"}
                self.ui.input_value = "fixture-private-input"
                self.confirm_change(action)
                self.assertEqual(self.ui.config, original)
                self.assertTrue(self.ui.observe_runtime)
                self.emit("account_signed_out", {"action": action, **SIGNED_OUT})
                self.assert_detached(original)
                self.assertEqual(self.ui.auth, {})
                self.assertEqual(self.ui.input_value, "")
                self.assertEqual(self.ui.account["state"], "not_authenticated")
                self.assertEqual([item[0] for item in self.worker.submitted], [action])

    def test_logout_error_retains_runtime_ssh_and_settings_without_stop(self):
        original = self.prove_old_runtime()
        old_status, old_ssh, old_provider = (
            self.ui.status,
            self.ui.ssh,
            self.ui.provider,
        )
        old_sessions = self.ui.sessions
        old_steps = set(self.ui.confirmed_steps)
        self.confirm_change("account_logout")
        self.emit("error", "Fixture sign-out could not be verified.")
        self.emit("done", "account_logout")
        self.assertEqual(self.ui.page, "account")
        self.assertEqual(self.ui.account["state"], "unavailable")
        self.assertEqual(self.ui.config, original)
        self.assertIs(self.ui.status, old_status)
        self.assertIs(self.ui.ssh, old_ssh)
        self.assertIs(self.ui.provider, old_provider)
        self.assertIs(self.ui.sessions, old_sessions)
        self.assertEqual(self.ui.confirmed_steps, old_steps)
        self.assertTrue(self.ui.observe_runtime)
        self.assertTrue(self.ui.runtime_events_allowed)
        self.assertEqual(
            [item[0] for item in self.worker.submitted], ["account_logout"]
        )

    def test_changed_cache_with_unverified_logout_withdraws_proof_without_claiming_success(
        self,
    ):
        original = self.prove_old_runtime()
        self.confirm_change("account_switch")
        self.emit(
            "account_invalidated",
            {
                "state": "unavailable",
                "reason": "logout_unverified",
                "message": "Fixture cache changed; final sign-out could not be verified.",
            },
        )
        self.assert_detached(original)
        self.assertEqual(self.ui.account["state"], "unavailable")
        self.assertNotIn("signed out", self.draw().text)
        self.assertEqual(
            [item[0] for item in self.worker.submitted], ["account_switch"]
        )

    def test_back_before_late_signedout_still_detaches_without_reopening_login(self):
        original = self.prove_old_runtime()
        self.confirm_change("account_switch")
        self.choose("back")
        self.assertEqual(self.ui.page, "home")
        self.assertTrue(self.ui.account_dismissed)
        self.emit("account_signed_out", {"action": "account_switch", **SIGNED_OUT})
        self.emit("account", dict(SIGNED_OUT))
        self.emit("done", "account_switch")
        self.assertEqual(self.ui.page, "home")
        self.assert_detached(original)
        self.assertEqual(
            [item[0] for item in self.worker.submitted], ["account_switch"]
        )

    def test_detach_from_ready_origin_returns_home_after_late_signout(self):
        original = self.prove_old_runtime()
        self.ui._page("ready")
        self.ui.account = {"state": "not_authenticated"}
        self.choose("existing")
        self.assertEqual(self.ui.page, "account")
        self.choose("account_logout")
        self.choose("account_confirm_change")
        self.choose("back")
        self.assertEqual(self.ui.page, "ready")
        self.emit("account_signed_out", {"action": "account_logout", **SIGNED_OUT})
        self.assertEqual(self.ui.page, "home")
        self.assert_detached(original)
        self.assertEqual(
            [item[0] for item in self.worker.submitted], ["account_logout"]
        )

    def test_busy_account_hides_mutating_commands_and_cannot_repeat_change(self):
        self.confirm_change("account_switch")
        self.worker.busy = True
        choices = self.ui._choices()
        self.assertEqual(
            [(choice.key, choice.enabled) for choice in choices],
            [("loading", False), ("back", True)],
        )
        self.ui.selected = 0
        self.ui._key("\n")
        self.assertEqual(
            [item[0] for item in self.worker.submitted], ["account_switch"]
        )

    def test_switch_authorization_uses_masked_account_input_then_returns_to_login(self):
        self.confirm_change("account_switch")
        self.worker.busy = True
        self.emit(
            "auth",
            {
                "url": "https://fixture.invalid/authorization",
                "waiting": True,
                "needs_code": True,
            },
        )
        self.assertEqual(self.ui.page, "account")
        self.choose("auth_code")
        self.assertEqual(self.ui.page, "input")
        self.ui.input_value = "fixture-sensitive-code"
        text = self.draw().text
        self.assertNotIn(self.ui.input_value, text)
        self.assertIn("*" * len(self.ui.input_value), text)
        self.assertEqual(self.worker.responses, [])
        self.ui._key("\n")
        self.assertEqual(self.worker.responses, ["fixture-sensitive-code"])
        self.assertEqual(self.ui.page, "account")
        self.assertEqual(self.ui.input_value, "")

    def test_back_from_switch_discards_late_authorization_without_reopening(self):
        self.confirm_change("account_switch")
        self.choose("back")
        self.emit(
            "auth",
            {
                "url": "https://fixture.invalid/late-authorization",
                "waiting": True,
                "needs_code": True,
            },
        )
        self.assertEqual(self.ui.page, "home")
        self.assertEqual(self.ui.auth, {})
        self.assertEqual(self.worker.responses, [])

    def test_late_old_runtime_events_cannot_restore_detached_proof(self):
        original = self.prove_old_runtime()
        old_status, old_ssh = self.ui.status, self.ui.ssh
        old_provider = self.ui.provider
        old_sessions = self.ui.sessions
        self.confirm_change("account_logout")
        self.emit("account_signed_out", {"action": "account_logout", **SIGNED_OUT})
        events = (
            ("status", old_status),
            ("ssh", old_ssh),
            ("ssh_error", "Fixture old SSH probe failed."),
            ("config", original),
            ("session", (original, old_provider)),
            ("provider", old_provider),
            ("sessions", old_sessions),
            ("model_registration", {"loras"}),
            (
                "inspection",
                {
                    "config": original,
                    "status": old_status,
                    "ssh": old_ssh,
                    "provider": old_provider,
                    "configuration_known": True,
                    "ready": True,
                },
            ),
            ("released", (original, "")),
        )
        for kind, value in events:
            with self.subTest(kind=kind):
                self.emit(kind, value)
                self.assert_detached(original)
                self.assertEqual(self.ui.page, "account")
        self.assertEqual(
            [item[0] for item in self.worker.submitted], ["account_logout"]
        )

    def test_signed_in_replacement_does_not_implicitly_restore_previous_runtime(self):
        original = self.prove_old_runtime()
        self.confirm_change("account_switch")
        self.emit("account_signed_out", {"action": "account_switch", **SIGNED_OUT})
        self.emit("account", dict(AUTHENTICATED))
        self.emit("done", "account_switch")
        self.assertEqual(self.ui.account["state"], "authenticated")
        self.assertEqual(self.ui.page, "account")
        self.assert_detached(original)
        self.ui._refresh()
        self.assertEqual(
            [item[0] for item in self.worker.submitted], ["account_switch"]
        )

    def test_explicit_new_account_listing_and_inspection_restore_verified_context(self):
        original = self.prove_old_runtime()
        old_status, old_ssh = self.ui.status, self.ui.ssh
        self.confirm_change("account_switch")
        self.emit("account_signed_out", {"action": "account_switch", **SIGNED_OUT})
        self.emit("account", dict(AUTHENTICATED))
        self.emit("done", "account_switch")
        self.choose("back")
        self.choose("existing")
        self.assertEqual(self.ui.page, "listing")
        self.assertFalse(self.ui.runtime_events_allowed)
        fresh = [{"name": "replacement-account-runtime", "hardware": "G4"}]
        self.emit("sessions", fresh)
        self.emit("done", "sessions")
        self.assertEqual(self.ui.page, "sessions")
        self.assertEqual(self.ui.sessions, fresh)
        self.choose("session:replacement-account-runtime")
        self.assertEqual(self.ui.page, "inspect")
        self.assertTrue(self.ui.runtime_events_allowed)
        candidate = replace(original, session="replacement-account-runtime")
        self.emit(
            "inspection",
            {
                "config": candidate,
                "status": old_status,
                "ssh": old_ssh,
                "provider": fresh[0],
                "configuration_known": True,
                "ready": True,
            },
        )
        self.emit("done", "inspect")
        self.assertEqual(self.ui.page, "ready")
        self.assertEqual(self.ui.config, candidate)
        self.assertTrue(self.ui.observe_runtime)
        self.assertTrue(self.ui._ready())
        self.assertEqual(
            [item[0] for item in self.worker.submitted],
            ["account_switch", "sessions", "inspect"],
        )


if __name__ == "__main__":
    unittest.main()
