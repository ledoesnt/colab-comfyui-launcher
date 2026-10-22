"""Offline transfer faults using small files; never invokes Colab or credentials."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "launcher_assets", ROOT / "scripts/assets.py"
)
assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assets)


class AssetsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-asset-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.content = self.root / "content"
        self.base = self.content / "runtime"
        self.storage = self.content / "storage"
        self.base.mkdir(parents=True)
        (self.storage / "input").mkdir(parents=True)
        (self.storage / "output").mkdir()
        self.session = "fixture-session"
        self.assignment = "fixture-assignment-one"
        self.current_assignment = self.assignment
        (self.base / "instance.json").write_text(
            json.dumps(
                {
                    "session": self.session,
                    "assignment_id": self.assignment,
                }
            )
        )
        (self.base / "services.json").write_text(
            json.dumps(
                {
                    "storage_root": str(self.storage),
                    "ephemeral": True,
                }
            )
        )
        self.transfers = []
        self.operations = []
        self.transfer_fault = None
        self.client = assets.AssetsClient(
            self.session,
            self.assignment,
            self.transfer,
            self.remote,
            lambda session: self.current_assignment,
        )
        self.destination = self.root / "collected"

    def transfer(self, arguments, timeout):
        self.transfers.append(arguments)
        self.assertGreater(timeout, 0)
        action, flag, session, source, target = arguments
        self.assertEqual(flag, "-s")
        self.assertEqual(session, self.session)
        if self.transfer_fault:
            return self.transfer_fault(action, source, target)
        if action == "upload":
            shutil.copyfile(source, "/" + target)
        else:
            self.assertEqual(action, "download")
            shutil.copyfile("/" + source, target)
        return subprocess.CompletedProcess(arguments, 0, "upgrade banner\n", "")

    def dispatch(self, payload):
        return assets.remote_action(
            payload,
            base=self.base,
            content_root=self.content,
            drive_mount=self.content / "drive",
        )

    def remote(self, session, payload, timeout):
        self.assertEqual(session, self.session)
        self.assertGreater(timeout, 0)
        self.operations.append(payload)
        return self.dispatch(payload)

    def source(self, data=b"fixture input", name="image.png"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def manifest(self, data=b"fixture video", relative="video/result.mp4", **fields):
        path = self.storage / "output" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {
            "session": self.session,
            "assignment_id": self.assignment,
            "job_id": "fixture-job",
            "assets": [
                {
                    "relative_path": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "video": [{"width": 864, "height": 480, "decoded_frames": 121}],
                    **fields,
                }
            ],
        }

    def test_upload_returns_instance_and_loadimage_name_after_sha_verification(self):
        data = b"fixture input, not a real image"
        result = self.client.upload(self.source(data))
        self.assertTrue(result["ok"])
        self.assertEqual(result["assignment_id"], self.assignment)
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["bytes"], len(data))
        self.assertEqual(result["comfyui_name"], result["relative_path"])
        self.assertEqual(Path(result["comfyui_name"]).parent, Path("."))
        target = self.storage / "input" / result["comfyui_name"]
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(
            [p["action"] for p in self.operations], ["asset-stage", "asset-commit"]
        )
        self.assertEqual(self.transfers[0][0], "upload")
        self.assertFalse(
            Path(self.transfers[0][3]).exists(), "temporary snapshot is removed"
        )

    def test_same_name_with_changed_bytes_does_not_overwrite_prior_input(self):
        first = self.client.upload(self.source(b"first"))
        second = self.client.upload(self.source(b"second"))
        self.assertNotEqual(first["relative_path"], second["relative_path"])
        self.assertEqual(Path(first["path"]).read_bytes(), b"first")
        self.assertEqual(Path(second["path"]).read_bytes(), b"second")

    def test_same_content_reuses_input_and_commit_can_be_recovered(self):
        first = self.client.upload(self.source())
        repeated = self.client.upload(self.source())
        self.assertEqual(first["relative_path"], repeated["relative_path"])
        self.assertTrue(repeated["reused"])
        recovered = self.dispatch(
            {
                "action": "asset-commit",
                "stage_id": first["stage_id"],
                "session": self.session,
                "assignment_id": self.assignment,
            }
        )
        self.assertTrue(recovered["reused"])

    def test_upload_checksum_failure_never_publishes_input(self):
        def wrong_bytes(action, source, target):
            Path("/" + target).write_bytes(b"truncated")
            return subprocess.CompletedProcess([], 0, "", "")

        self.transfer_fault = wrong_bytes
        with self.assertRaisesRegex(assets.AssetsError, "SHA256"):
            self.client.upload(self.source(b"expected source bytes"))
        self.assertEqual(list((self.storage / "input").rglob("*.png")), [])
        self.assertEqual(list((self.storage / "input").rglob("*.partial*")), [])
        self.assertEqual(len(self.transfers), 1)

    def test_upload_snapshots_original_before_transfer(self):
        source = self.source(b"original")

        def mutate_original(action, snapshot, target):
            source.write_bytes(b"edited while uploading")
            shutil.copyfile(snapshot, "/" + target)
            return subprocess.CompletedProcess([], 0, "", "")

        self.transfer_fault = mutate_original
        result = self.client.upload(source)
        self.assertEqual(Path(result["path"]).read_bytes(), b"original")

    def test_remote_marker_mismatch_stops_before_staging(self):
        (self.base / "instance.json").write_text(
            json.dumps(
                {
                    "session": self.session,
                    "assignment_id": "a-replacement-instance",
                }
            )
        )
        with self.assertRaisesRegex(assets.AssetsError, "identity"):
            self.client.upload(self.source())
        self.assertEqual(self.transfers, [])

    def test_name_reassigned_during_upload_never_commits(self):
        def replace_instance(action, source, target):
            shutil.copyfile(source, "/" + target)
            self.current_assignment = "a-replacement-instance"
            return subprocess.CompletedProcess([], 0, "", "")

        self.transfer_fault = replace_instance
        with self.assertRaisesRegex(assets.AssetsError, "different instance"):
            self.client.upload(self.source())
        self.assertEqual([p["action"] for p in self.operations], ["asset-stage"])
        self.assertEqual(list((self.storage / "input").iterdir()), [])

    def test_collect_preserves_media_metadata_and_validates_download(self):
        manifest = self.manifest()
        result = self.client.collect(manifest, self.destination)
        record = result["assets"][0]
        self.assertEqual(record["video"], manifest["assets"][0]["video"])
        self.assertEqual(record["assignment_id"], self.assignment)
        self.assertEqual(Path(record["local_path"]).read_bytes(), b"fixture video")
        self.assertEqual(json.loads(Path(result["manifest_path"]).read_text()), result)
        self.assertIn(assets.instance_namespace(self.assignment), record["local_path"])
        self.assertEqual(record["verification"], "sha256")

    def test_long_original_filenames_remain_usable_after_hash_prefixes(self):
        uploaded = self.client.upload(self.source(name="i" * 240 + ".png"))
        self.assertLessEqual(len(Path(uploaded["path"]).name.encode()), 255)
        manifest = self.manifest(relative="video/" + "v" * 240 + ".mp4")
        result = self.client.collect(manifest, self.destination)
        self.assertLessEqual(
            len(Path(result["assets"][0]["local_path"]).name.encode()), 255
        )

    def test_remote_file_changed_since_manifest_is_rejected_before_download(self):
        manifest = self.manifest()
        path = self.storage / "output" / manifest["assets"][0]["relative_path"]
        path.write_bytes(b"changed after render")
        with self.assertRaisesRegex(assets.AssetsError, "SHA256"):
            self.client.collect(manifest, self.destination)
        self.assertEqual(self.transfers, [])

    def test_reassignment_during_download_never_publishes_old_file_as_new_instance(
        self,
    ):
        manifest = self.manifest()

        def reassign(action, source, target):
            shutil.copyfile("/" + source, target)
            self.current_assignment = "a-replacement-instance"
            return subprocess.CompletedProcess([], 0, "", "")

        self.transfer_fault = reassign
        with self.assertRaisesRegex(assets.AssetsError, "different instance"):
            self.client.collect(manifest, self.destination)
        self.assertEqual(list(self.destination.rglob("*.mp4")), [])
        self.assertEqual(list(self.destination.rglob("manifest.json")), [])

    def test_collect_retry_rehashes_published_file_without_redownload(self):
        manifest = self.manifest()
        first = self.client.collect(manifest, self.destination)
        second = self.client.collect(manifest, self.destination)
        self.assertEqual(len(self.transfers), 1)
        self.assertTrue(second["assets"][0]["reused"])
        Path(first["assets"][0]["local_path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(assets.AssetsError, "SHA256"):
            self.client.collect(manifest, self.destination)
        self.assertEqual(len(self.transfers), 1)

    def test_bad_or_partial_download_does_not_publish_manifest_or_asset(self):
        manifest = self.manifest()
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):

                def broken(action, source, target, code=returncode):
                    Path(target).write_bytes(b"partial")
                    return subprocess.CompletedProcess(
                        [], code, "upgrade banner", "private diagnostic"
                    )

                self.transfer_fault = broken
                with self.assertRaises(assets.AssetsError) as error:
                    self.client.collect(manifest, self.destination)
                self.assertNotIn("private diagnostic", str(error.exception))
                self.assertEqual(list(self.destination.rglob("*.mp4")), [])
                self.assertEqual(list(self.destination.rglob("manifest.json")), [])
                self.assertEqual(list(self.destination.rglob("*.partial-*")), [])
        self.assertEqual(
            len(self.transfers), 2, "one attempt per call, never an automatic retry"
        )

    def test_download_timeout_cleans_partial_and_preserves_existing_files(self):
        manifest = self.manifest()
        sentinel = self.destination / "user-file"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"retain")

        def timeout(action, source, target):
            Path(target).write_bytes(b"partial")
            raise subprocess.TimeoutExpired("fixture", 1)

        self.transfer_fault = timeout
        with self.assertRaises(subprocess.TimeoutExpired):
            self.client.collect(manifest, self.destination)
        self.assertEqual(sentinel.read_bytes(), b"retain")
        self.assertEqual(list(self.destination.rglob("*.partial-*")), [])

    def test_wrong_instance_in_manifest_or_asset_rejected_before_remote_read(self):
        manifest = self.manifest()
        for level in (manifest, manifest["assets"][0]):
            with self.subTest(level=level):
                level["assignment_id"] = "another-instance"
                with self.assertRaisesRegex(assets.AssetsError, "instance"):
                    self.client.collect(manifest, self.destination)
                level["assignment_id"] = self.assignment
        self.assertEqual(self.operations, [])
        self.assertEqual(self.transfers, [])

    def test_output_same_basename_and_different_subfolders_are_distinct(self):
        manifest = self.manifest(b"one", "first/result.mp4")
        manifest["assets"] += self.manifest(b"two", "second/result.mp4")["assets"]
        result = self.client.collect(manifest, self.destination)
        self.assertEqual(
            [Path(a["local_path"]).read_bytes() for a in result["assets"]],
            [b"one", b"two"],
        )
        self.assertEqual(len({a["local_path"] for a in result["assets"]}), 2)

    def test_traversal_duplicate_and_invalid_hash_rejected(self):
        for path in (
            "../outside.mp4",
            "/absolute.mp4",
            "foo//bar.mp4",
            "foo/./bar.mp4",
            "foo\\bar.mp4",
            "foo\nbar.mp4",
        ):
            manifest = self.manifest()
            manifest["assets"][0]["relative_path"] = path
            with self.subTest(path=path), self.assertRaises(assets.AssetsError):
                self.client.collect(manifest, self.destination)
        manifest = self.manifest()
        manifest["assets"] *= 2
        with self.assertRaisesRegex(assets.AssetsError, "duplicate"):
            self.client.collect(manifest, self.destination)
        manifest = self.manifest(sha256="invalid")
        with self.assertRaisesRegex(assets.AssetsError, "SHA256"):
            self.client.collect(manifest, self.destination)
        self.assertEqual(self.operations, [])

    def test_local_source_symlink_and_fifo_are_refused(self):
        source = self.source()
        linked = self.root / "linked.png"
        linked.symlink_to(source)
        with self.assertRaisesRegex(assets.AssetsError, "symlink"):
            self.client.upload(linked)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(assets.AssetsError, "regular"):
            self.client.upload(fifo)
        self.assertEqual(self.operations, [])

    def test_output_symlink_and_local_destination_symlink_are_refused(self):
        manifest = self.manifest()
        video = self.storage / "output" / manifest["assets"][0]["relative_path"]
        source = self.source(video.read_bytes(), "private-source")
        video.unlink()
        video.symlink_to(source)
        with self.assertRaisesRegex(assets.AssetsError, "symlink"):
            self.client.collect(manifest, self.destination)
        video.unlink()
        video.write_bytes(source.read_bytes())
        target = self.root / "elsewhere"
        target.mkdir()
        self.destination.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(assets.AssetsError, "symlink"):
            self.client.collect(manifest, self.destination)
        self.assertEqual(self.transfers, [])

    def test_unmounted_drive_and_mydrive_root_are_refused(self):
        drive = self.content / "drive"
        mydrive = drive / "MyDrive"
        mydrive.mkdir(parents=True)
        (self.base / "services.json").write_text(
            json.dumps({"storage_root": str(mydrive), "ephemeral": False})
        )
        for mounted in (False, True):
            with (
                self.subTest(mounted=mounted),
                mock.patch.object(assets.os.path, "ismount", return_value=mounted),
                self.assertRaisesRegex(assets.AssetsError, "Drive"),
            ):
                self.client.upload(self.source())
        self.assertEqual(self.transfers, [])

    def test_timeout_and_unknown_job_are_refused_without_network(self):
        for seconds in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(seconds=seconds), self.assertRaises(assets.AssetsError):
                assets.Deadline(seconds)
        manifest = self.manifest()
        manifest.pop("job_id")
        with self.assertRaisesRegex(assets.AssetsError, "stable job"):
            self.client.collect(manifest, self.destination)
        self.assertEqual(self.operations, [])


if __name__ == "__main__":
    unittest.main()
