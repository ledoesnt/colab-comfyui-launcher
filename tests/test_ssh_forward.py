"""Offline SSH forward safety tests: no keys, OAuth or Colab connections."""

import argparse
import importlib.util
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ssh_forward", ROOT / "scripts/ssh_forward.py"
)
forward = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forward)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ssh-forward-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.args = forward.parser().parse_args(
            [
                "-s",
                "fixture-session",
                "start",
                "--state-dir",
                str(self.base / "state"),
                "--config",
                str(self.base / "sessions.json"),
                "--identity",
                str(self.base / "key"),
            ]
        )
        self.owned = {
            "pid": 43210,
            "pgid": 43210,
            "sid": 43210,
            "start_ticks": "6789",
            "boot_id": "fixture-boot",
        }
        self.entry = {
            "name": self.args.session,
            "endpoint": "fixture-endpoint",
            "url": "https://example.invalid",
            "token": "fixture-token",
        }

    def test_proxy_uses_official_cli_and_explicit_loopback_only(self):
        command = forward.build_command(
            self.args,
            Path("/fixture/colab"),
            Path("/fixture/ssh"),
            self.base / "snapshot.json",
            self.base / "known hosts",
            "abc",
        )
        self.assertEqual(
            command[command.index("-L") + 1], "127.0.0.1:8188:127.0.0.1:8188"
        )
        self.assertIn("StrictHostKeyChecking=accept-new", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("GatewayPorts=no", command)
        self.assertIn("ControlMaster=no", command)
        self.assertIn("HostKeyAlias=colab-runtime-abc", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)
        self.assertNotIn("UserKnownHostsFile=/dev/null", command)
        proxy = next(item for item in command if item.startswith("ProxyCommand="))
        self.assertIn("--config", proxy)
        self.assertIn("ssh --proxy-mode -s fixture-session -i", proxy)
        for unexpected in ("--rm", "--gpu", "--tpu", " new "):
            self.assertNotIn(unexpected, proxy)

    def test_proxy_shell_quotes_paths_with_spaces_and_metacharacters(self):
        self.args.identity = self.base / "key with ';' spaces"
        command = forward.build_command(
            self.args,
            Path("/fixture/colab tool"),
            Path("/fixture/ssh"),
            self.base / "snapshot with spaces",
            self.base / "known hosts",
            "abc",
        )
        proxy = next(
            item.split("=", 1)[1]
            for item in command
            if item.startswith("ProxyCommand=")
        )
        self.assertEqual(forward.shlex.split(proxy)[0], "/fixture/colab tool")
        self.assertEqual(forward.shlex.split(proxy)[-1], str(self.args.identity))

    def test_relative_and_openssh_expansion_paths_are_rejected(self):
        for path in ("relative", "/tmp/%h", "/tmp/${HOME}", "/tmp/new\nline"):
            with self.subTest(path=path), self.assertRaises(argparse.ArgumentTypeError):
                forward.absolute_path(path)

    def test_missing_session_does_not_even_call_cli(self):
        with (
            mock.patch.object(forward.subprocess, "run") as run,
            self.assertRaisesRegex(forward.ForwardError, "absent"),
        ):
            forward.existing_session(self.args, Path("/fixture/colab"))
        run.assert_not_called()

    def test_status_zero_for_removed_session_still_fails(self):
        with (
            mock.patch.object(
                forward, "load_json", side_effect=[{self.args.session: self.entry}, {}]
            ),
            mock.patch.object(
                forward.subprocess, "run", return_value=mock.Mock(returncode=0)
            ),
            self.assertRaisesRegex(forward.ForwardError, "not active"),
        ):
            forward.existing_session(self.args, Path("/fixture/colab"))

    def test_existing_session_status_is_read_only_and_explicit(self):
        self.args.config.write_text(json.dumps({self.args.session: self.entry}))
        with mock.patch.object(
            forward.subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as run:
            self.assertEqual(
                forward.existing_session(self.args, Path("/fixture/colab")), self.entry
            )
        self.assertEqual(
            run.call_args.args[0][-3:], ["status", "-s", self.args.session]
        )

    def test_reused_pid_or_boot_never_signalled(self):
        for info, boot in (
            ({**self.owned, "start_ticks": "9876"}, "fixture-boot"),
            (self.owned, "new-boot"),
            ({**self.owned, "pgid": 3}, "fixture-boot"),
            (None, "fixture-boot"),
        ):
            with (
                self.subTest(info=info, boot=boot),
                mock.patch.object(forward, "process_info", return_value=info),
                mock.patch.object(forward, "boot_id", return_value=boot),
                mock.patch.object(forward.os, "killpg") as kill,
            ):
                self.assertFalse(forward.kill_owned(self.owned))
                kill.assert_not_called()

    def test_invalid_identity_never_signalled(self):
        for owned in (
            None,
            {},
            {**self.owned, "start_ticks": None},
            {**self.owned, "start_ticks": "not-numeric"},
            {**self.owned, "pid": 0},
        ):
            with (
                self.subTest(owned=owned),
                mock.patch.object(forward.os, "killpg") as kill,
            ):
                self.assertFalse(forward.kill_owned(owned))
                kill.assert_not_called()

    def test_owned_group_term_does_not_escalate_after_exit(self):
        with (
            mock.patch.object(forward, "alive", side_effect=[True, False, False]),
            mock.patch.object(forward.os, "killpg") as kill,
        ):
            self.assertTrue(forward.kill_owned(self.owned))
        kill.assert_called_once_with(self.owned["pid"], signal.SIGTERM)

    def test_owned_group_exit_race_is_harmless(self):
        with (
            mock.patch.object(forward, "alive", return_value=True),
            mock.patch.object(forward.os, "killpg", side_effect=ProcessLookupError),
        ):
            self.assertTrue(forward.kill_owned(self.owned))

    def test_process_stat_parses_parentheses_and_rejects_zombies(self):
        fields = ["S", "0", "43210", "43210", *(["0"] * 15), "6789"]
        proc = mock.MagicMock()
        proc.stat.return_value.st_uid = forward.os.getuid()
        proc.__truediv__.return_value.read_text.return_value = (
            "43210 (name with (parentheses) and spaces) " + " ".join(fields)
        )
        with mock.patch.object(forward, "Path", return_value=proc):
            result = forward.process_info(43210)
        self.assertEqual(result["start_ticks"], "6789")
        self.assertEqual(result["pgid"], 43210)
        proc.__truediv__.return_value.read_text.return_value = (
            "43210 (exited) " + " ".join(["Z", *fields[1:]])
        )
        with mock.patch.object(forward, "Path", return_value=proc):
            self.assertIsNone(forward.process_info(43210))

    def test_process_owned_by_different_user_is_not_ours(self):
        proc = mock.MagicMock()
        proc.stat.return_value.st_uid = forward.os.getuid() + 1
        with mock.patch.object(forward, "Path", return_value=proc):
            self.assertIsNone(forward.process_info(43210))

    def test_listener_must_be_loopback_and_belong_to_recorded_pid(self):
        fds = mock.Mock()
        fds.iterdir.return_value = [Path("/fixture/fd")]
        tcp = mock.Mock()
        tcp.read_text.return_value = (
            "header\n0: 0100007F:1FFC 00000000:0000 0A 0:0 00:0 0 1000 0 123 1\n"
        )

        def fixture_path(value):
            return fds if str(value).endswith("/fd") else tcp

        with (
            mock.patch.object(forward, "alive", return_value=True),
            mock.patch.object(forward, "Path", side_effect=fixture_path),
            mock.patch.object(forward.os, "readlink", return_value="socket:[123]"),
        ):
            self.assertTrue(forward.owned_listener(self.owned, 8188))
            tcp.read_text.return_value = tcp.read_text.return_value.replace(
                "0100007F", "00000000"
            )
            self.assertFalse(forward.owned_listener(self.owned, 8188))
            tcp.read_text.return_value = tcp.read_text.return_value.replace(
                "00000000:1FFC", "0100007F:1FFC"
            )
            with mock.patch.object(forward.os, "readlink", return_value="socket:[987]"):
                self.assertFalse(forward.owned_listener(self.owned, 8188))

    def test_missing_key_not_generated_implicitly(self):
        with (
            mock.patch.object(forward.subprocess, "run") as run,
            self.assertRaisesRegex(forward.ForwardError, "explicitly"),
        ):
            forward.ensure_key(self.args.identity, False)
        run.assert_not_called()

    def test_existing_encrypted_key_is_unchanged(self):
        self.args.identity.write_bytes(b"fixture-encrypted-key")
        self.args.identity.chmod(0o600)
        with (
            mock.patch.object(
                forward, "executable", return_value=Path("/fixture/ssh-keygen")
            ),
            mock.patch.object(
                forward.subprocess, "run", return_value=mock.Mock(returncode=1)
            ),
            self.assertRaisesRegex(
                forward.ForwardError, "encrypted keys are left unchanged"
            ),
        ):
            forward.ensure_key(self.args.identity, True)
        self.assertEqual(self.args.identity.read_bytes(), b"fixture-encrypted-key")

    def test_partial_key_pair_never_overwritten(self):
        Path(str(self.args.identity) + ".pub").write_text("existing-public")
        with (
            mock.patch.object(forward.subprocess, "run") as run,
            self.assertRaisesRegex(forward.ForwardError, "overwrite"),
        ):
            forward.ensure_key(self.args.identity, True)
        run.assert_not_called()

    def test_world_readable_private_key_is_rejected(self):
        self.args.identity.write_bytes(b"fixture-key")
        self.args.identity.chmod(0o644)
        with (
            mock.patch.object(forward.subprocess, "run") as run,
            self.assertRaisesRegex(forward.ForwardError, "mode 600"),
        ):
            forward.ensure_key(self.args.identity, False)
        run.assert_not_called()

    def test_create_key_only_permitted_on_start(self):
        self.args.action, self.args.create_key = "stop", True
        with (
            mock.patch.object(forward, "ensure_key") as key,
            self.assertRaisesRegex(forward.ForwardError, "only valid"),
        ):
            forward.execute(self.args)
        key.assert_not_called()

    def test_occupied_unowned_port_never_spawns_or_signals(self):
        probe = mock.MagicMock()
        probe.__enter__.return_value.bind.side_effect = OSError("fixture-busy")
        with (
            mock.patch.object(forward.socket, "socket", return_value=probe),
            mock.patch.object(forward.subprocess, "Popen") as spawn,
            mock.patch.object(forward, "kill_owned") as kill,
            self.assertRaisesRegex(forward.ForwardError, "occupied"),
        ):
            forward.start(self.args, self.base, self.base / "forward.json")
        spawn.assert_not_called()
        kill.assert_not_called()

    def test_status_never_claims_unowned_local_server(self):
        with (
            mock.patch.object(forward, "alive", return_value=True),
            mock.patch.object(forward, "owned_listener", return_value=False),
            mock.patch.object(forward, "healthy") as healthy,
        ):
            result = forward.status(self.args, {"process": self.owned})
        self.assertTrue(result["running"])
        self.assertFalse(result["http_ready"])
        healthy.assert_not_called()

    def test_private_snapshot_permissions_and_no_token_in_public_state(self):
        snapshot = self.base / "proxy-session.json"
        forward.private_write(snapshot, {self.args.session: self.entry})
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(snapshot.read_text())), {self.args.session})
        public = forward.status(self.args, {})
        self.assertNotIn("fixture-token", json.dumps(public))
        self.assertEqual(public["url"], "http://127.0.0.1:8188")

    def test_private_state_rejects_symlink(self):
        victim = self.base / "victim"
        victim.write_text("untouched")
        linked = self.base / "linked"
        linked.symlink_to(victim)
        with self.assertRaises(OSError):
            forward.private_write(linked, {})
        self.assertEqual(victim.read_text(), "untouched")

    def test_stop_cleans_snapshot_but_retains_keys_and_known_hosts(self):
        forward.private_dir(self.args.state_dir)
        name = forward.hashlib.sha256(f"{self.args.session}:8188".encode()).hexdigest()[
            :24
        ]
        directory = self.args.state_dir / name
        forward.private_dir(directory)
        forward.private_write(directory / "forward.json", {"process": self.owned})
        forward.private_write(
            directory / "proxy-session.json", {self.args.session: self.entry}
        )
        self.args.identity.write_text("persistent-key")
        hosts = self.args.state_dir / "known-hosts"
        forward.private_dir(hosts)
        (hosts / "fixture.hosts").write_text("persistent-host-key")
        self.args.action = "stop"
        with (
            mock.patch.object(forward, "kill_owned", return_value=True) as kill,
            mock.patch.object(forward, "alive", return_value=False),
            mock.patch.object(forward.subprocess, "run") as run,
        ):
            result = forward.execute(self.args)
        kill.assert_called_once_with(self.owned)
        run.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["running"])
        self.assertFalse((directory / "proxy-session.json").exists())
        self.assertEqual(self.args.identity.read_text(), "persistent-key")
        self.assertEqual((hosts / "fixture.hosts").read_text(), "persistent-host-key")

    def test_start_failure_only_cleans_its_recorded_process(self):
        process = mock.Mock(pid=self.owned["pid"])
        process.poll.return_value = 255
        with (
            mock.patch.object(forward.socket, "socket"),
            mock.patch.object(
                forward,
                "executable",
                side_effect=[Path("/fixture/colab"), Path("/fixture/ssh")],
            ),
            mock.patch.object(forward, "existing_session", return_value=self.entry),
            mock.patch.object(forward, "ensure_key"),
            mock.patch.object(forward.subprocess, "Popen", return_value=process),
            mock.patch.object(forward, "process_info", return_value=self.owned),
            mock.patch.object(forward, "boot_id", return_value="fixture-boot"),
            mock.patch.object(forward, "kill_owned") as kill,
            self.assertRaisesRegex(forward.ForwardError, "SSH exited"),
        ):
            forward.start(self.args, self.base, self.base / "forward.json")
        kill.assert_called_once_with(self.owned)
        self.assertFalse((self.base / "proxy-session.json").exists())

    def test_main_does_not_print_provider_error_details(self):
        output = io.StringIO()
        with (
            mock.patch.object(
                forward, "execute", side_effect=RuntimeError("fixture-secret-token")
            ),
            mock.patch.object(forward.sys, "stdout", output),
        ):
            code = forward.main(["-s", "fixture-session", "status"])
        self.assertEqual(code, 1)
        self.assertNotIn("fixture-secret-token", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
