"""Local Git remotes and file fixtures only; no Colab, credentials or external network."""

import fcntl
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import startup_update as update


def command(root, *args):
    return (
        subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL
        )
        .decode()
        .strip()
    )


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.remote = self.base / "remote.git"
        self.writer = self.base / "writer"
        self.repo = self.base / "checkout"
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "init", "-b", "main", str(self.writer)],
            check=True,
            capture_output=True,
        )
        for key, value in [
            ("user.name", "fixture"),
            ("user.email", "fixture@example.invalid"),
        ]:
            command(self.writer, "config", key, value)
        (self.writer / "script.py").write_text("print('fixture')\n")
        (self.writer / ".gitignore").write_text("models/extra-added-*.json\n")
        command(self.writer, "add", "script.py", ".gitignore")
        command(self.writer, "commit", "-m", "initial fixture")
        command(self.writer, "remote", "add", "origin", str(self.remote))
        command(self.writer, "push", "-u", "origin", "main")
        command(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.repo)],
            check=True,
            capture_output=True,
        )
        self.initial = command(self.repo, "rev-parse", "HEAD")

    def advance(self):
        (self.writer / "next.py").write_text("print('next fixture')\n")
        command(self.writer, "add", "next.py")
        command(self.writer, "commit", "-m", "next fixture")
        command(self.writer, "push")
        return command(self.writer, "rev-parse", "HEAD")

    def test_clean_pull_fast_forwards_before_start(self):
        expected = self.advance()
        result, lease = update.prepare_source(self.repo)
        with lease:
            self.assertEqual(result["commit"], expected)
            self.assertTrue(result["changed"])
            self.assertTrue((self.repo / "next.py").exists())

    def test_dirty_untracked_stops_without_pull(self):
        self.advance()
        p = self.repo / "user-change.txt"
        p.write_text("preserve this fixture")
        with self.assertRaises(update.UpdateError):
            update.prepare_source(self.repo)
        self.assertEqual(command(self.repo, "rev-parse", "HEAD"), self.initial)
        self.assertEqual(p.read_text(), "preserve this fixture")

    def test_tracked_change_stops(self):
        (self.repo / "script.py").write_text("user change\n")
        with self.assertRaises(update.UpdateError):
            update.prepare_source(self.repo)

    def test_divergence_stops_without_reset(self):
        self.advance()
        command(self.repo, "config", "user.name", "fixture")
        command(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "local.py").write_text("local fixture")
        command(self.repo, "add", "local.py")
        command(self.repo, "commit", "-m", "local fixture")
        local = command(self.repo, "rev-parse", "HEAD")
        with self.assertRaises(update.UpdateError):
            update.prepare_source(self.repo)
        self.assertEqual(command(self.repo, "rev-parse", "HEAD"), local)

    def test_running_version_lease_blocks_update(self):
        with (
            update.read_version_lease(self.repo),
            self.assertRaises(update.UpdateError),
        ):
            update.prepare_source(self.repo)

    def test_failed_update_does_not_start_old_version(self):
        self.remote.rename(self.base / "unavailable.git")
        with self.assertRaises(update.UpdateError):
            update.prepare_source(self.repo)
        self.assertEqual(command(self.repo, "rev-parse", "HEAD"), self.initial)

    def test_snapshot_matches_git_commit_and_reuses_verified_files(self):
        snapshot = self.base / "snapshot"
        result, lease = update.prepare_launch(self.repo, snapshot=snapshot)
        with lease:
            self.assertFalse(result["snapshot"]["reused"])
            meta = json.loads((snapshot / ".launcher-snapshot.json").read_text())
            self.assertEqual(meta["commit"], self.initial)
            update.verify_snapshot(snapshot, meta)
        result, lease = update.prepare_launch(self.repo, snapshot=snapshot)
        with lease:
            self.assertTrue(result["snapshot"]["reused"])

    def test_legacy_snapshot_needs_hashes_and_keeps_recoverable_backup(self):
        snapshot = self.base / "snapshot"
        snapshot.mkdir()
        p = snapshot / "legacy.py"
        p.write_text("legacy fixture")
        with self.assertRaises(update.UpdateError):
            update.prepare_launch(self.repo, snapshot=snapshot)
        manifest = {"files": {"legacy.py": hashlib.sha256(p.read_bytes()).hexdigest()}}
        result, lease = update.prepare_launch(
            self.repo, snapshot=snapshot, expected_manifest=manifest
        )
        with lease:
            backup = Path(result["snapshot"]["backup"])
            self.assertEqual((backup / "legacy.py").read_text(), "legacy fixture")
            self.assertTrue((snapshot / "script.py").exists())

    def test_snapshot_drift_stops_and_preserves_current(self):
        snapshot = self.base / "snapshot"
        _, lease = update.prepare_launch(self.repo, snapshot=snapshot)
        lease.close()
        (snapshot / "script.py").write_text("preserve changed fixture")
        with self.assertRaises(update.UpdateError):
            update.prepare_launch(self.repo, snapshot=snapshot)
        self.assertEqual(
            (snapshot / "script.py").read_text(), "preserve changed fixture"
        )

    def test_active_project_lock_blocks_snapshot_replacement(self):
        snapshot = self.base / "snapshot"
        lock = self.base / "project.lock"
        with lock.open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(update.UpdateError):
                update.prepare_launch(self.repo, snapshot=snapshot, run_lock=lock)
        self.assertFalse(snapshot.exists())

    def test_live_snapshot_reader_blocks_replacement(self):
        snapshot = self.base / "snapshot"
        _, lease = update.prepare_launch(self.repo, snapshot=snapshot)
        lease.close()
        with update.read_version_lease(snapshot), self.assertRaises(update.UpdateError):
            update.prepare_launch(self.repo, snapshot=snapshot)

    def test_generated_catalog_preserved_but_unknown_model_file_blocks(self):
        models = self.repo / "models"
        models.mkdir()
        generated = models / "extra-added-fixture.json"
        generated.write_text('{"fixture":true}')
        _, lease = update.prepare_source(self.repo)
        lease.close()
        self.assertEqual(generated.read_text(), '{"fixture":true}')
        (models / "unknown-user.json").write_text('{"preserve":true}')
        with self.assertRaises(update.UpdateError):
            update.prepare_source(self.repo)

    def test_source_and_snapshot_overlap_is_rejected(self):
        with self.assertRaises(update.UpdateError):
            update.prepare_launch(self.repo, snapshot=self.repo / "nested")
        self.assertEqual(command(self.repo, "rev-parse", "HEAD"), self.initial)

    def test_default_opt_in_does_not_update_unrelated_checkout(self):
        self.advance()
        self.assertIsNone(update.guard_entry(self.repo, []))
        self.assertEqual(command(self.repo, "rev-parse", "HEAD"), self.initial)

    def test_opt_in_and_expected_branch(self):
        command(self.repo, "config", "launcher.updateBeforeStart", "true")
        command(self.repo, "config", "launcher.updateBranch", "different")
        with self.assertRaises(update.UpdateError):
            update.guard_entry(self.repo, [])

    def test_demo_help_never_update(self):
        for flag in ("--demo", "--help", "-h"):
            self.assertIsNone(update.guard_entry(self.repo, [flag]))


if __name__ == "__main__":
    unittest.main()
