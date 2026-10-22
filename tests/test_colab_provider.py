"""Offline official CLI protocol and release verification fault injection."""

import importlib.util
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "provider_under_test", ROOT / "scripts/colab_provider.py"
)
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)

EMPTY = "[colab] No active sessions found on server.\n"
BANNER = (
    "\n[colab] A new version of Colab CLI is available: 0.8.0 (current: 0.7.4)\n"
    "[colab] You can run 'colab update --install' to upgrade in place.\n"
    "[colab] Run 'uv tool install -U google-colab-cli' to update.\n"
    '[colab] To silence this check, set "enable_update_check": false in '
    "~/.config/colab-cli/settings.json\n\n"
)


def session(name="selected", assignment="allocation-a", hardware="T4"):
    return f"[{name}] {assignment} | Hardware: {hardware} | Shape: Standard | Variant: GPU\n"


def response(stdout, stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ProviderParserTests(unittest.TestCase):
    def test_lists_named_and_multiple_orphan_allocations(self):
        rows = provider.parse_sessions(
            session()
            + session("?", "orphan-a", "A100")
            + session("?", "orphan-b", "G4")
        )
        self.assertEqual([row["name"] for row in rows], ["selected", "?", "?"])
        self.assertEqual(
            rows[0],
            {
                "name": "selected",
                "assignment_id": "allocation-a",
                "hardware": "T4",
                "shape": "Standard",
                "variant": "GPU",
            },
        )

    def test_verified_empty_server_list_and_pruning(self):
        self.assertEqual(provider.parse_sessions(EMPTY), [])
        self.assertEqual(
            provider.parse_sessions(
                "[colab] Pruned 2 stale local session(s).\n" + EMPTY
            ),
            [],
        )

    def test_banner_and_ansi_do_not_pollute_protocol(self):
        rows = provider.parse_sessions(BANNER + "\x1b[32m" + session() + "\x1b[0m")
        self.assertEqual(rows[0]["assignment_id"], "allocation-a")

    def test_partial_unknown_or_local_only_listing_is_rejected(self):
        cases = [
            "",
            BANNER,
            "[colab] No active sessions.\n",
            session() + "unexpected diagnostic\n",
            session() + "[broken] endpoint | Hardware: T4\n",
            "[colab] Session 'selected' not found.\n",
            session().replace(" | Variant: GPU", " | Variant: GPU | Status: IDLE"),
            "[bad] https://private.invalid/token | Hardware: T4 | Shape: Standard | Variant: GPU\n",
            session().replace("allocation-a", "allocation-a\x07"),
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(provider.ProviderError):
                provider.parse_sessions(value)

    def test_conflicting_and_duplicate_listings_are_rejected(self):
        for value in (
            EMPTY + EMPTY,
            EMPTY + session(),
            session() * 2,
            session() + session("another", "allocation-a"),
            session() + session("selected", "allocation-b"),
        ):
            with self.subTest(value=value), self.assertRaises(provider.ProviderError):
                provider.parse_sessions(value)

    def test_account_usage_is_explicitly_account_scoped(self):
        self.assertEqual(
            provider.parse_usage(
                BANNER
                + "Current balance: 123.50 compute units\nUsage rate: 5.25/hr\nActive assignments: 2\n"
            ),
            {
                "balance": 123.5,
                "rate_hourly": 5.25,
                "assignments_count": 2,
                "scope": "account",
            },
        )
        self.assertEqual(
            provider.parse_usage(
                "Current balance: 0.00 compute units\nUsage rate: 0.00/hr\nActive assignments: 0\n"
            )["rate_hourly"],
            0,
        )

    def test_usage_unknown_nonfinite_duplicate_and_negative_fail(self):
        base = "Current balance: 3.00 compute units\nUsage rate: 1.50/hr\nActive assignments: 1\n"
        for value in (
            base.replace("3.00", "NaN"),
            base.replace("1.50", "-1.50"),
            base.replace("1.50", "9" * 400),
            base + "Usage rate: 1.50/hr\n",
            base.replace("Active assignments: 1\n", ""),
            base + "unrecognized\n",
        ):
            with self.subTest(value=value), self.assertRaises(provider.ProviderError):
                provider.parse_usage(value)


class ProviderCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="launcher-provider-test-")
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name) / "oauth-fixture.json"
        self.run = mock.Mock()
        self.api = provider.ColabProvider(run=self.run, oauth_cache=self.cache)

    def test_error_metadata_rejects_untrusted_classification_and_returncode(self):
        for code, returncode in (
            ("refresh_token=fixture-private-value", "fixture-private-value"),
            ("unknown_future_classification", True),
            (None, 1.5),
            ({"token": "fixture-private-value"}, []),
        ):
            with self.subTest(code_type=type(code).__name__):
                error = provider.ProviderError(
                    "Safe failure.", code=code, returncode=returncode
                )
                self.assertEqual(error.code, "provider_error")
                self.assertIsNone(error.returncode)
                self.assertNotIn("fixture-private", repr(vars(error)))
        self.run.assert_not_called()

    def test_read_queries_are_bounded_and_noninteractive(self):
        self.run.return_value = response(session())
        self.assertEqual(self.api.sessions()[0]["hardware"], "T4")
        self.run.assert_called_once_with(
            ["colab", "--auth=oauth2", "sessions"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )

    def test_upgrade_message_goes_to_stderr_only(self):
        self.run.return_value = response(BANNER + session())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.api.sessions()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(), "Colab CLI reported an available upgrade.\n"
        )

    def test_provider_failure_never_exposes_response_or_subprocess_secrets(self):
        secret = "refresh_token=fixture-private-value https://private.invalid/oauth"
        for failure, code, returncode in (
            (response(secret, secret, 1), "command_failed", 1),
            (
                subprocess.TimeoutExpired(["colab"], 20, output=secret, stderr=secret),
                "timeout",
                None,
            ),
            (OSError(secret), "unavailable", None),
        ):
            self.run.reset_mock()
            self.run.side_effect = failure if isinstance(failure, Exception) else None
            self.run.return_value = failure
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(provider.ProviderError) as caught,
            ):
                self.api.sessions()
            self.assertNotIn("fixture-private", str(caught.exception))
            self.assertNotIn("private.invalid", str(caught.exception))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.returncode, returncode)
            self.assertNotIn("fixture-private", repr(vars(caught.exception)))
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.run.assert_called_once()

    def test_failed_auth_cannot_masquerade_as_released_empty_list(self):
        self.run.return_value = response(
            EMPTY, "To authorize colab-cli, visit https://private.invalid/oauth\n"
        )
        with self.assertRaises(provider.ProviderError) as caught:
            self.api.sessions()
        self.assertEqual(caught.exception.code, "provider_diagnostics")
        self.assertEqual(caught.exception.returncode, 0)

    def test_failed_creation_retains_only_safe_process_diagnostics(self):
        secret = (
            "Authorization: Bearer fixture-private-value\nrefresh_token=fixture-secret"
        )
        for returncode in (1, 23, -15):
            with self.subTest(returncode=returncode):
                self.run.reset_mock()
                self.run.return_value = response(secret, secret, returncode)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                    self.assertRaises(provider.ProviderError) as caught,
                ):
                    self.api.create("selected", "T4")
                error = caught.exception
                self.assertEqual(
                    vars(error),
                    {
                        "code": "command_failed",
                        "outcome_unknown": True,
                        "returncode": returncode,
                    },
                )
                self.assertNotIn("fixture-private", str(error))
                self.assertNotIn("fixture-secret", str(error))
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")
                self.run.assert_called_once()

    def test_timeout_creation_is_unknown_and_never_retried(self):
        self.run.side_effect = subprocess.TimeoutExpired(["colab"], 300)
        with self.assertRaises(provider.ProviderError) as caught:
            self.api.create("selected", "T4")
        self.assertTrue(caught.exception.outcome_unknown)
        self.run.assert_called_once()

    def test_creation_uses_requested_hardware_and_requires_exact_success(self):
        self.run.return_value = response(
            "[colab] Creating session 'selected'...\n[colab] Session READY.\n"
        )
        self.assertIsNone(self.api.create("selected", "T4"))
        self.assertEqual(self.run.call_args.args[0][-2:], ["--gpu", "T4"])
        self.run.return_value = response("[colab] Session READY.\n")
        with self.assertRaises(provider.ProviderError) as caught:
            self.api.create("selected", "T4")
        self.assertTrue(caught.exception.outcome_unknown)

    def test_cpu_creation_omits_gpu_option(self):
        self.run.return_value = response(
            "[colab] Creating session 'selected'...\n[colab] Session READY.\n"
        )
        self.api.create("selected")
        self.assertEqual(
            self.run.call_args.args[0],
            ["colab", "--auth=oauth2", "new", "-s", "selected"],
        )

    def test_invalid_selection_never_invokes_cli(self):
        for name in ("", "-option", "white space", "a/b", "a\n", "?", "a" * 129):
            with self.subTest(name=name), self.assertRaises(provider.ProviderError):
                self.api.create(name, "T4")
        with self.assertRaises(provider.ProviderError):
            self.api.create("selected", "unknown")
        self.run.assert_not_called()

    def test_stop_rechecks_allocation_and_only_issues_scoped_command(self):
        self.run.side_effect = [
            response(session()),
            response(
                "[colab] Stopping session 'selected'...\n[colab] Session terminated.\n"
            ),
        ]
        self.assertIsNone(self.api.stop("selected", "allocation-a"))
        self.assertEqual(
            self.run.call_args.args[0],
            ["colab", "--auth=oauth2", "stop", "-s", "selected"],
        )
        self.assertEqual(self.run.call_count, 2)

    def test_reused_or_orphaned_name_is_never_stopped(self):
        for listing in (
            session("selected", "replacement"),
            session("?"),
            session("renamed"),
            EMPTY,
        ):
            self.run.reset_mock()
            self.run.return_value = response(listing)
            with (
                self.subTest(listing=listing),
                self.assertRaises(provider.ProviderError),
            ):
                self.api.stop("selected", "allocation-a")
            self.run.assert_called_once()

    def test_release_success_requires_subsequent_platform_absence(self):
        self.run.side_effect = [
            response(session()),
            response(session()),
            response(
                "[colab] Stopping session 'selected'...\n[colab] Session terminated.\n"
            ),
            response(EMPTY),
        ]
        result = self.api.verified_release("selected", "allocation-a")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["release_requested"])
        self.assertEqual(result["status"], "released")
        self.assertEqual(self.run.call_args.args[0][-1], "sessions")

    def test_stop_success_text_is_not_release_proof(self):
        self.run.side_effect = [
            response(session()),
            response(session()),
            response(
                "[colab] Stopping session 'selected'...\n[colab] Session terminated.\n"
            ),
            response(session()),
        ]
        result = self.api.verified_release("selected", "allocation-a")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "release_pending")

    def test_timeout_still_queries_and_can_verify_actual_release(self):
        self.run.side_effect = [
            response(session()),
            response(session()),
            subprocess.TimeoutExpired(["colab"], 30),
            response(EMPTY),
        ]
        result = self.api.verified_release("selected", "allocation-a")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(self.run.call_count, 4)

    def test_release_rejection_is_retryable_and_followed_by_query(self):
        self.run.side_effect = [
            response(session()),
            response(session()),
            response("private backend error", returncode=1),
            response(session()),
        ]
        result = self.api.verified_release("selected", "allocation-a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "release_failed")
        self.assertEqual(self.run.call_count, 4)

    def test_release_query_failure_is_unknown_without_second_stop(self):
        self.run.side_effect = [
            response(session()),
            response(session()),
            response(
                "[colab] Stopping session 'selected'...\n[colab] Session terminated.\n"
            ),
            subprocess.TimeoutExpired(["colab"], 20),
        ]
        result = self.api.verified_release("selected", "allocation-a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "release_unknown")
        stop_calls = [
            call for call in self.run.call_args_list if "stop" in call.args[0]
        ]
        self.assertEqual(len(stop_calls), 1)

    def test_already_absent_allocation_does_not_stop_same_name_replacement(self):
        self.run.return_value = response(session("selected", "replacement"))
        result = self.api.verified_release("selected", "allocation-a")
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_absent"])
        self.assertFalse(result["release_requested"])
        self.run.assert_called_once()

    def test_orphan_still_present_cannot_be_claimed_released(self):
        self.run.return_value = response(session("?"))
        result = self.api.verified_release("selected", "allocation-a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "identity_mismatch")
        self.run.assert_called_once()

    def test_account_scope_uses_metadata_without_opening_contents(self):
        self.cache.write_text("fixture bytes")
        with mock.patch.object(
            Path, "open", side_effect=AssertionError("must not open credential bytes")
        ):
            first = self.api.account_scope()
            second = self.api.account_scope()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("stat:"))
        self.assertNotIn("fixture bytes", first)
        self.cache.write_text("different fixture length")
        self.assertNotEqual(first, self.api.account_scope())
        self.cache.unlink()
        self.assertEqual(self.api.account_scope(), "absent")

    def test_account_scope_rejects_symlinks_and_stat_failures(self):
        self.cache.symlink_to(self.cache.parent / "nonexistent")
        self.assertEqual(self.api.account_scope(), "unknown")
        with mock.patch.object(Path, "lstat", side_effect=PermissionError):
            self.assertEqual(self.api.account_scope(), "unknown")

    def test_scope_change_during_initial_absence_does_not_verify_release(self):
        self.run.return_value = response(EMPTY)
        with mock.patch.object(
            self.api, "account_scope", side_effect=["account-a", "account-b"]
        ):
            result = self.api.verified_release("selected", "allocation-a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "scope_changed")

    def test_scope_changed_before_release_cannot_use_different_accounts_absence(self):
        self.run.return_value = response(EMPTY)
        with mock.patch.object(self.api, "account_scope", return_value="account-b"):
            result = self.api.verified_release(
                "selected", "allocation-a", expected_scope="account-a"
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["release_requested"])
        self.assertEqual(result["status"], "scope_changed")
        self.run.assert_not_called()

    def test_expected_scope_allows_platform_absence_when_unchanged(self):
        self.run.return_value = response(EMPTY)
        with mock.patch.object(self.api, "account_scope", return_value="account-a"):
            result = self.api.verified_release(
                "selected", "allocation-a", expected_scope="account-a"
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "released")

    def test_unknown_expected_scope_blocks_query_and_stop(self):
        with mock.patch.object(self.api, "account_scope", return_value="unknown"):
            result = self.api.verified_release(
                "selected", "allocation-a", expected_scope="unknown"
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "scope_changed")
        self.run.assert_not_called()

    def test_scope_change_between_presence_and_stop_is_blocked(self):
        self.run.return_value = response(session())
        with (
            mock.patch.object(
                self.api, "account_scope", side_effect=["account-a", "account-b"]
            ),
            self.assertRaises(provider.ProviderError) as caught,
        ):
            self.api.stop("selected", "allocation-a")
        self.assertEqual(caught.exception.code, "scope_changed")
        self.run.assert_called_once()

    def test_scope_change_during_final_absence_does_not_verify_release(self):
        self.run.side_effect = [response(session()), response(EMPTY)]
        with (
            mock.patch.object(self.api, "stop"),
            mock.patch.object(
                self.api,
                "account_scope",
                side_effect=["account-a", "account-a", "account-b"],
            ),
        ):
            result = self.api.verified_release("selected", "allocation-a")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "scope_changed")


if __name__ == "__main__":
    unittest.main()
