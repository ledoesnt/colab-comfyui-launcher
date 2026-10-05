"""Real local PTY fixtures; never use Colab, Google accounts, or credentials."""

import importlib.util
import json
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "provider_terminal", ROOT / "scripts/provider_terminal.py"
)
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class ProviderTerminalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="provider-pty-fixture-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.events = queue.Queue()
        self.seen = []
        self.terminals = []
        self.addCleanup(self.cleanup_terminals)

    def cleanup_terminals(self):
        for terminal in self.terminals:
            terminal.close()
            terminal.join(3)

    def fixture(self, code):
        script = self.base / f"fixture-{len(list(self.base.iterdir()))}.py"
        script.write_text(code)
        return [sys.executable, "-u", str(script)]

    def start(self, code, timeout=4):
        terminal = provider.ProviderTerminal(
            self.fixture(code), self.events.put, timeout=timeout
        ).start()
        self.terminals.append(terminal)
        return terminal

    def event(self, kind, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            self.seen.append(event)
            if event["kind"] == kind:
                return event
        self.fail(
            f"Missing {kind} event; seen event types: {[e['kind'] for e in self.seen]}"
        )

    def finished(self, terminal, timeout=4):
        event = self.event("finished", timeout)
        terminal.join(1)
        self.assertFalse(terminal.running)
        self.assertIsNone(terminal.pending)
        return event

    def text(self):
        return "\n".join(event["text"] for event in self.seen if event.get("text"))

    def test_plain_pipes_have_no_controlling_tty(self):
        argv = self.fixture("open('/dev/tty').readline()")
        result = subprocess.run(
            argv,
            input=b"\n",
            capture_output=True,
            check=False,
            start_new_session=True,
            timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"/dev/tty", result.stderr)

    def test_real_controlling_tty_waits_for_explicit_confirmation(self):
        marker = self.base / "confirmed"
        terminal = self.start(
            "import os, pathlib, sys\n"
            "assert os.isatty(0) and os.isatty(1) and os.isatty(2)\n"
            "print('https://accounts.google.com/o/oauth2/auth?fixture=not-a-credential')\n"
            "print('Press Enter after you have granted access... ', end='', flush=True)\n"
            "with open('/dev/tty') as tty: answer = tty.readline()\n"
            "assert answer == '\\n'\n"
            f"pathlib.Path({str(marker)!r}).write_text('confirmed')\n"
            "print('[colab] Credentials propagated. Resuming mount...')\n"
        )
        url = self.event("auth_url")
        self.assertEqual(
            url["url"],
            "https://accounts.google.com/o/oauth2/auth?fixture=not-a-credential",
        )
        self.event("confirm_required")
        self.assertEqual(terminal.pending, "confirm")
        self.assertFalse(marker.exists())
        self.assertTrue(terminal.running)
        self.assertFalse(terminal.respond("unexpected code"))
        self.assertTrue(terminal.respond(""))
        self.assertFalse(terminal.respond(""))
        final = self.finished(terminal)
        self.assertEqual(final["returncode"], 0)
        self.assertFalse(final["cancelled"])
        self.assertFalse(final["timed_out"])
        self.assertEqual(marker.read_text(), "confirmed")
        self.assertNotIn("fixture=", self.text())

    def test_code_is_not_terminal_echoed_or_reflected_in_events(self):
        code = "FIXTURE_SECRET_CODE_123456"
        terminal = self.start(
            "import sys\n"
            "sys.stdout.write('Enter the authorization code: '); sys.stdout.flush()\n"
            "with open('/dev/tty') as tty: code = tty.readline().strip()\n"
            "print('unexpected provider echo: ' + code)\n"
            "print('access_token=FIXTURE_TOKEN_MUST_BE_HIDDEN')\n"
        )
        self.event("code_required")
        self.assertEqual(terminal.pending, "code")
        for invalid in ("", "two\nlines", "two\rlines", "\x1bcode", "a" * 4097):
            self.assertFalse(terminal.respond(invalid))
        self.assertTrue(terminal.respond(code))
        final = self.finished(terminal)
        self.assertEqual(final["returncode"], 0)
        serialized = json.dumps(self.seen)
        self.assertNotIn(code, serialized)
        self.assertNotIn("FIXTURE_TOKEN_MUST_BE_HIDDEN", serialized)
        self.assertIn("[hidden code]", self.text())
        self.assertIn("[hidden credential]", self.text())
        self.assertEqual(terminal._secrets, [])

    def test_prompt_and_ansi_split_across_actual_pty_writes(self):
        terminal = self.start(
            "import sys, time\n"
            "for part in ['\\x1b[', '31mPress Enter after you have ', 'granted access... ', '\\x1b[0m']:\n"
            " sys.stdout.write(part); sys.stdout.flush(); time.sleep(.03)\n"
            "with open('/dev/tty') as tty: tty.readline()\n"
            "print('\\x1b]0;HIDDEN_OSC_TITLE\\x07visible done')\n"
        )
        self.event("confirm_required")
        self.assertTrue(terminal.respond())
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        self.assertIn("visible done", self.text())
        self.assertNotIn("HIDDEN_OSC_TITLE", self.text())
        self.assertNotIn("\x1b", self.text())

    def test_error_body_token_code_and_bearer_values_are_not_displayed(self):
        lines = [
            '[colab] Error propagating: 400 {"token": "FIXTURE_BARE_TOKEN"}',
            '{"code":"FIXTURE_BARE_CODE"}',
            "token=FIXTURE_TOKEN_ASSIGNMENT",
            "CODE : FIXTURE_CASE_INSENSITIVE_CODE",
            "Bearer FIXTURE_BEARER_VALUE",
            "bearer\tFIXTURE_TAB_BEARER_VALUE",
            "Authorization: Bearer FIXTURE_HEADER_VALUE",
            "barcode=ordinary_visible_value",
            "tokenizer=ordinary_visible_value",
            "code_status=ordinary_visible_value",
            "module_code=ordinary_visible_value",
            "decoding code is complete",
        ]
        terminal = self.start(f"for line in {lines!r}: print(line)\n")
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        for line in lines[:7]:
            value = line[line.index("FIXTURE_") :].strip('"}')
            self.assertNotIn(value, self.text())
        for line in lines[7:]:
            self.assertIn(line, self.text())
        self.assertIn("[hidden credential]", self.text())

    def test_pretty_printed_credential_value_on_next_line_is_hidden(self):
        terminal = self.start(
            "print('Error propagating: 400 {\\\"token\\\":')\n"
            "print('  \\\"FIXTURE_NEXT_LINE_TOKEN\\\"}')\n"
            "print('\\\"code\\\":')\n"
            "print('')\n"
            "print('  \\\"FIXTURE_NEXT_LINE_CODE\\\",')\n"
            "print('safe next provider status')\n"
        )
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        self.assertNotIn("FIXTURE_NEXT_LINE_TOKEN", self.text())
        self.assertNotIn("FIXTURE_NEXT_LINE_CODE", self.text())
        self.assertIn("safe next provider status", self.text())

    def test_plain_authorization_failure_message_is_not_a_credential(self):
        lines = [
            "Provider authorization did not complete; inspect or retry this authorization step.",
            "authorization did not complete; inspect or retry this session",
            "token refresh failed; complete account login again",
            "code entry is incomplete; no response was submitted",
        ]
        terminal = self.start(f"for line in {lines!r}: print(line)\n")
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        self.assertEqual(self.text().splitlines(), lines)
        self.assertFalse(
            any(
                event["kind"] in ("confirm_required", "code_required")
                for event in self.seen
            )
        )

    def test_non_google_url_is_redacted_not_exposed_as_auth_url(self):
        terminal = self.start(
            "print('visit https://attacker.example.invalid/login?token=fixture')\n"
            "print('https://accounts.google.com.attacker.invalid/auth')\n"
            "print('http://accounts.google.com/auth')\n"
        )
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        self.assertFalse(any(event["kind"] == "auth_url" for event in self.seen))
        self.assertNotIn("attacker", self.text())
        self.assertNotIn("token=fixture", self.text())

    def test_timeout_cancels_only_this_cli(self):
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            start_new_session=True,
        )
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.terminate)
        terminal = self.start(
            "import time\n"
            "print('Press Enter after you have granted access...', flush=True)\n"
            "with open('/dev/tty') as tty: tty.readline()\n"
            "time.sleep(15)\n",
            timeout=0.25,
        )
        self.event("confirm_required")
        final = self.finished(terminal)
        self.assertTrue(final["timed_out"])
        self.assertFalse(final["cancelled"])
        self.assertNotEqual(final["returncode"], 0)
        self.assertIsNone(unrelated.poll())

    def test_close_ends_pending_cli_without_automatic_input(self):
        marker = self.base / "unexpected-confirm"
        terminal = self.start(
            "import pathlib\n"
            "print('Press Enter after you have granted access...', flush=True)\n"
            "with open('/dev/tty') as tty: tty.readline()\n"
            f"pathlib.Path({str(marker)!r}).write_text('unexpected')\n"
        )
        self.event("confirm_required")
        terminal.close()
        final = self.finished(terminal)
        self.assertTrue(final["cancelled"])
        self.assertFalse(final["timed_out"])
        self.assertFalse(marker.exists())
        self.assertFalse(terminal.respond())
        terminal.close()

    def test_timeout_kills_cli_that_ignores_term(self):
        terminal = self.start(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('Press Enter after you have granted access...', flush=True)\n"
            "with open('/dev/tty') as tty: tty.readline()\n"
            "time.sleep(15)\n",
            timeout=0.25,
        )
        self.event("confirm_required")
        final = self.finished(terminal)
        self.assertTrue(final["timed_out"])
        self.assertEqual(final["returncode"], -9)

    def test_explicit_new_terminal_after_timeout_requires_fresh_confirmation(self):
        first = self.start(
            "print('Press Enter after you have granted access...', flush=True)\n"
            "with open('/dev/tty') as tty: tty.readline()\n"
            "print('authorization provided; remote status still needs verification')\n",
            timeout=0.2,
        )
        self.event("confirm_required")
        self.assertTrue(self.finished(first)["timed_out"])
        self.assertFalse(first.respond())
        self.assertIsNone(first.pending)
        second = provider.ProviderTerminal(
            first.argv, self.events.put, timeout=3
        ).start()
        self.terminals.append(second)
        self.event("confirm_required")
        self.assertEqual(second.pending, "confirm")
        self.assertTrue(second.running)
        self.assertFalse(first.respond())
        self.assertTrue(second.respond())
        final = self.finished(second)
        self.assertEqual(final["returncode"], 0)
        self.assertFalse(final["timed_out"])
        self.assertFalse(final["cancelled"])
        self.assertIn("remote status still needs verification", self.text())

    def test_nonzero_exit_is_preserved_not_mount_success(self):
        terminal = self.start("import sys\nprint('mount failed')\nsys.exit(7)\n")
        final = self.finished(terminal)
        self.assertEqual(final["returncode"], 7)
        self.assertIn("mount failed", self.text())
        self.assertNotIn("ok", final)

    def test_no_newline_output_is_flushed_on_exit(self):
        terminal = self.start(
            "print('last output without newline', end='', flush=True)"
        )
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        self.assertEqual(self.text(), "last output without newline")

    def test_missing_executable_is_finished_failure_without_traceback_details(self):
        terminal = provider.ProviderTerminal(
            [str(self.base / "missing-executable")], self.events.put, timeout=2
        ).start()
        self.terminals.append(terminal)
        final = self.finished(terminal)
        self.assertNotEqual(final["returncode"], 0)
        self.assertFalse(final["timed_out"])
        self.assertNotIn("Traceback", self.text())
        self.assertNotIn(str(self.base), self.text())

    def test_large_unterminated_url_drops_remainder_until_newline(self):
        terminal = self.start(
            "import sys, time\n"
            "sys.stdout.write('https://accounts.google.com/auth?fixture=' + 'x' * 20000); sys.stdout.flush()\n"
            "time.sleep(.05)\n"
            "print('UNSAFE_REMAINDER_MUST_BE_DROPPED')\n"
            "print('safe next line')\n"
        )
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        self.assertFalse(any(event["kind"] == "auth_url" for event in self.seen))
        self.assertNotIn("UNSAFE_REMAINDER", self.text())
        self.assertIn("safe next line", self.text())

    def test_inputs_and_start_lifecycle_are_validated(self):
        for argv in ([], "command", [""], ["cmd", "\x00"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                provider.ProviderTerminal(argv, self.events.put)
        for timeout in (0, -1, float("inf"), 3601):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                provider.ProviderTerminal([sys.executable], self.events.put, timeout)
        terminal = self.start("print('done')")
        with self.assertRaises(RuntimeError):
            terminal.start()
        self.assertEqual(self.finished(terminal)["returncode"], 0)

    def test_output_event_and_total_length_are_bounded(self):
        terminal = self.start("for _ in range(300): print('x' * 1024)\n")
        self.assertEqual(self.finished(terminal)["returncode"], 0)
        outputs = [event["text"] for event in self.seen if event["kind"] == "output"]
        self.assertTrue(outputs)
        self.assertLessEqual(max(map(len, outputs)), provider.MAX_EVENT_TEXT)
        self.assertLessEqual(sum(map(len, outputs)), provider.MAX_DISPLAY_TEXT)

    def test_identity_mismatch_refuses_process_group_signal(self):
        terminal = provider.ProviderTerminal([sys.executable], self.events.put)
        terminal._process = mock.Mock(pid=12345)
        terminal._identity = (12345, 12345, "1")
        with (
            mock.patch.object(
                provider, "_process_identity", return_value=(12345, 12345, "2")
            ),
            mock.patch.object(provider.os, "killpg") as kill,
        ):
            terminal.close()
        kill.assert_not_called()

    def test_callback_error_cancels_cli_instead_of_leaving_hidden_prompt(self):
        def failing_callback(event):
            if event["kind"] == "finished":
                self.events.put(event)
            else:
                raise RuntimeError("fixture failure")

        terminal = provider.ProviderTerminal(
            self.fixture(
                "print('Press Enter after you have granted access...', flush=True)\n"
                "with open('/dev/tty') as tty: tty.readline()\n"
            ),
            failing_callback,
            timeout=2,
        ).start()
        self.terminals.append(terminal)
        final = self.finished(terminal)
        self.assertTrue(final["cancelled"])
        self.assertIn("event handler failed", final["error"])


if __name__ == "__main__":
    unittest.main()
