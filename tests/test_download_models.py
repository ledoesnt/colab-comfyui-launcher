"""Small deterministic HTTP fixtures; never contact Colab, Drive, or model hosts."""

import argparse
import hashlib
import importlib.util
import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "download_models", ROOT / "scripts/download_models.py"
)
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)
DATA = b"small deterministic model fixture, not model weights\n"


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}

    def geturl(self):
        return "https://fixture.invalid/blob?secret-signed-token=DO-NOT-PRINT"


class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="model-downloader-test-")
        self.addCleanup(temporary.cleanup)
        self.content = Path(temporary.name) / "content"
        self.drive = self.content / "drive"
        (self.drive / "MyDrive").mkdir(parents=True)
        self.root = self.content / "launcher" / "models"
        self.root.mkdir(parents=True)
        for name, value in (
            ("CONTENT_ROOT", self.content),
            ("DRIVE_MOUNT", self.drive),
        ):
            patch = mock.patch.object(downloader, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.item = {
            "path": "diffusion_models/tiny.safetensors",
            "size_bytes": len(DATA),
            "sha256": hashlib.sha256(DATA).hexdigest(),
        }
        self.manifest = self.content / "manifest.json"
        self.write_manifest()
        self.final = self.root / self.item["path"]
        self.final.parent.mkdir()
        self.partial = self.final.with_name(self.final.name + ".partial")
        self.args = argparse.Namespace(
            manifest=self.manifest,
            models_root=self.root,
            ephemeral=True,
            max_seconds=10.0,
        )
        # Every test defaults to a failing network mock, including unexpected calls.
        patch = mock.patch.object(
            downloader.urllib.request,
            "urlopen",
            side_effect=AssertionError("Network access prohibited in tests"),
        )
        self.http = patch.start()
        self.addCleanup(patch.stop)

    def write_manifest(self, **changes):
        value = {
            "repo_id": "Fixture-Org/Fixture-Model",
            "revision": "a" * 40,
            "files": [self.item],
            "total_size_bytes": len(DATA),
        }
        value.update(changes)
        self.manifest.write_text(json.dumps(value))

    def download(self):
        return downloader.download_file(
            self.root,
            "Fixture-Org/Fixture-Model",
            "a" * 40,
            self.item,
            downloader.Deadline(10),
        )

    def response(self, data, status=200, headers=None):
        self.http.side_effect = None
        self.http.return_value = Response(data, status, headers)

    def test_complete_200_is_verified_and_published(self):
        self.response(DATA, headers={"Content-Length": str(len(DATA))})
        result = self.download()
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(self.final.read_bytes(), DATA)
        self.assertFalse(self.partial.exists())
        request = self.http.call_args.args[0]
        self.assertTrue(
            request.full_url.startswith(
                "https://huggingface.co/Fixture-Org/Fixture-Model/resolve/"
            )
        )
        self.assertIsNone(request.get_header("Range"))

    def test_fresh_download_hashes_stream_without_re_reading_drive_model(self):
        self.response(DATA)
        with mock.patch.object(
            downloader, "hash_file", side_effect=AssertionError("No second file read")
        ):
            result = self.download()
        self.assertEqual(result["verification"], "stream_sha256")
        self.assertTrue((self.root / downloader.RECEIPT_NAME).is_file())

    def test_verified_receipt_skips_content_read_but_explicit_verify_re_reads(self):
        self.final.write_bytes(DATA)
        self.assertEqual(self.download()["verification"], "full_sha256")
        with mock.patch.object(
            downloader, "hash_file", wraps=downloader.hash_file
        ) as hashing:
            self.assertEqual(
                self.download()["verification"], "verified_receipt_metadata"
            )
            hashing.assert_not_called()
            downloader.download_file(
                self.root,
                "Fixture-Org/Fixture-Model",
                "a" * 40,
                self.item,
                downloader.Deadline(10),
                verify_cache=True,
            )
            self.assertEqual(hashing.call_count, 1)

    def test_changed_metadata_invalidates_receipt_and_corruption_is_preserved(self):
        self.final.write_bytes(DATA)
        self.download()
        self.final.write_bytes(b"X" * len(DATA))
        with self.assertRaisesRegex(downloader.DownloadError, "not overwritten"):
            self.download()
        self.assertEqual(self.final.read_bytes(), b"X" * len(DATA))

    def test_receipt_cannot_transfer_to_a_different_manifest_revision(self):
        self.final.write_bytes(DATA)
        self.download()
        changed = downloader.Receipts(self.root, "Fixture-Org/Fixture-Model", "b" * 40)
        self.assertFalse(changed.matches(self.final, self.item))

    def test_corrupt_or_symlinked_receipts_never_authorize_skip(self):
        self.final.write_bytes(DATA)
        receipt = self.root / downloader.RECEIPT_NAME
        receipt.write_text("not json")
        self.assertEqual(self.download()["verification"], "full_sha256")
        receipt.unlink()
        receipt.symlink_to(self.final)
        with self.assertRaisesRegex(downloader.DownloadError, "symlinks"):
            self.download()

    def test_resume_reads_only_the_existing_prefix_once(self):
        self.partial.write_bytes(DATA[:11])
        self.response(
            DATA[11:], 206, {"Content-Range": f"bytes 11-{len(DATA) - 1}/{len(DATA)}"}
        )
        with mock.patch.object(
            downloader, "hash_file", wraps=downloader.hash_file
        ) as hashing:
            self.assertEqual(self.download()["verification"], "stream_sha256")
        self.assertEqual(hashing.call_count, 1)
        self.assertEqual(hashing.call_args.args[-1], 11)

    def test_progress_is_local_atomic_throttled_and_records_failure(self):
        progress = downloader.Progress(None, "download", [self.item])
        with mock.patch.object(
            downloader, "atomic_json", wraps=downloader.atomic_json
        ) as writes:
            progress.update(self.item, "download", 1)
            progress.update(self.item, "download", 2)
            self.assertEqual(writes.call_count, 1)
            progress.finish(False)
        snapshot = json.loads(progress.path.read_text())
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["files"][0]["done_bytes"], 2)
        self.assertEqual(snapshot["phase"], "download")
        self.assertFalse(list(progress.root.glob("*.tmp-*")))

    def test_progress_and_model_root_symlinks_are_rejected_before_writes(self):
        with self.assertRaises(downloader.DownloadError):
            downloader.Progress(
                self.drive / "MyDrive" / "progress.json", "download", [self.item]
            )
        link = self.content / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(downloader.DownloadError, "symlinks"):
            downloader.models_root(link, True)

    def test_206_resumes_exact_range(self):
        offset = 11
        self.partial.write_bytes(DATA[:offset])
        self.response(
            DATA[offset:],
            status=206,
            headers={
                "Content-Range": f"bytes {offset}-{len(DATA) - 1}/{len(DATA)}",
                "Content-Length": str(len(DATA) - offset),
            },
        )
        self.assertEqual(self.download()["status"], "resumed")
        self.assertEqual(self.http.call_args.args[0].get_header("Range"), "bytes=11-")
        self.assertEqual(self.final.read_bytes(), DATA)

    def test_200_ignoring_range_safely_restarts(self):
        self.partial.write_bytes(DATA[:11])
        self.response(DATA, headers={"Content-Length": str(len(DATA))})
        self.assertEqual(self.download()["status"], "restarted")
        self.assertEqual(self.final.read_bytes(), DATA)

    def test_valid_final_is_skipped_without_network(self):
        self.final.write_bytes(DATA)
        self.assertEqual(self.download()["status"], "skipped")
        self.http.assert_not_called()

    def test_corrupt_final_is_preserved_without_network(self):
        corrupt = b"X" * len(DATA)
        self.final.write_bytes(corrupt)
        with self.assertRaisesRegex(downloader.DownloadError, "not overwritten"):
            self.download()
        self.assertEqual(self.final.read_bytes(), corrupt)
        self.http.assert_not_called()

    def test_complete_partial_is_verified_without_network(self):
        self.partial.write_bytes(DATA)
        self.assertEqual(self.download()["status"], "resumed")
        self.assertEqual(self.final.read_bytes(), DATA)
        self.http.assert_not_called()

    def test_overlong_partial_fails_without_network(self):
        self.partial.write_bytes(DATA + b"extra")
        with self.assertRaisesRegex(downloader.DownloadError, "longer"):
            self.download()
        self.assertEqual(self.partial.read_bytes(), DATA + b"extra")
        self.http.assert_not_called()

    def test_bad_hash_never_publishes_final(self):
        self.response(b"X" * len(DATA))
        with self.assertRaisesRegex(downloader.DownloadError, "SHA256"):
            self.download()
        self.assertFalse(self.final.exists())
        self.assertTrue(self.partial.exists())

    def test_short_body_is_retained_then_can_resume(self):
        self.response(DATA[:11])
        with self.assertRaisesRegex(downloader.DownloadError, "incomplete"):
            self.download()
        self.assertEqual(self.partial.read_bytes(), DATA[:11])
        self.assertFalse(self.final.exists())
        self.response(
            DATA[11:],
            status=206,
            headers={"Content-Range": f"bytes 11-{len(DATA) - 1}/{len(DATA)}"},
        )
        self.assertEqual(self.download()["status"], "resumed")

    def test_bad_206_headers_preserve_partial(self):
        for value in ("", "bytes 0-3/4", f"bytes 12-{len(DATA) - 1}/{len(DATA)}"):
            with self.subTest(content_range=value):
                self.partial.write_bytes(DATA[:11])
                self.response(DATA[11:], 206, {"Content-Range": value})
                with self.assertRaises(downloader.DownloadError):
                    self.download()
                self.assertEqual(self.partial.read_bytes(), DATA[:11])
                self.assertFalse(self.final.exists())

    def test_content_length_mismatch_preserves_partial(self):
        self.partial.write_bytes(DATA[:11])
        self.response(DATA, headers={"Content-Length": "1"})
        with self.assertRaisesRegex(downloader.DownloadError, "Content-Length"):
            self.download()
        self.assertEqual(self.partial.read_bytes(), DATA[:11])

    def test_overlong_response_never_publishes_final(self):
        self.response(DATA + b"extra")
        with self.assertRaisesRegex(downloader.DownloadError, "exceeds"):
            self.download()
        self.assertFalse(self.final.exists())

    def test_manifest_rejects_url_and_path_injection(self):
        for bad in (
            "../escape",
            "/absolute",
            "a//b",
            "a/./b",
            "a/../b",
            "a?token=x",
            "a#fragment",
            "a\\b",
            ".hidden/file",
            "file.partial",
        ):
            with self.subTest(path=bad):
                self.write_manifest(files=[dict(self.item, path=bad)])
                with self.assertRaises(downloader.DownloadError):
                    downloader.load_manifest(self.manifest)
        for fields in (
            {"repo_id": "https://evil.invalid/repo"},
            {"repo_id": "owner/repo?x=1"},
            {"revision": "main"},
            {"revision": "../main"},
        ):
            with self.subTest(fields=fields):
                self.write_manifest(**fields)
                with self.assertRaises(downloader.DownloadError):
                    downloader.load_manifest(self.manifest)

    def test_manifest_rejects_conflicting_paths_and_metadata(self):
        for fields in (
            {"files": [self.item, self.item]},
            {"files": [dict(self.item, path="dir"), dict(self.item, path="dir/file")]},
            {"total_size_bytes": 1},
            {"files": [dict(self.item, size_bytes=True)]},
            {"files": [dict(self.item, sha256="bad")]},
            {"files": []},
        ):
            with self.subTest(fields=fields):
                self.write_manifest(**fields)
                with self.assertRaises(downloader.DownloadError):
                    downloader.load_manifest(self.manifest)

    def test_drive_mount_is_required_and_no_directory_created_on_failure(self):
        requested = self.drive / "MyDrive" / "launcher" / "models"
        with (
            mock.patch.object(
                downloader.os.path, "ismount", return_value=False
            ) as mount,
            self.assertRaisesRegex(downloader.DownloadError, "not mounted"),
        ):
            downloader.models_root(requested, False)
        mount.assert_called_once_with(self.drive)
        self.assertFalse(requested.exists())

    def test_real_mount_check_and_mydrive_boundary(self):
        with mock.patch.object(downloader.os.path, "ismount", return_value=True):
            root = downloader.models_root(None, False)
            self.assertEqual(root, self.drive / "MyDrive" / "colab-comfyui" / "models")
            with self.assertRaisesRegex(downloader.DownloadError, "inside"):
                downloader.models_root(self.root, False)

    def test_ephemeral_root_is_explicit_and_stays_in_runtime(self):
        self.assertEqual(downloader.models_root(self.root, True), self.root)
        for path in (
            self.content,
            self.drive / "MyDrive" / "models",
            self.content.parent / "outside",
        ):
            with self.subTest(path=path), self.assertRaises(downloader.DownloadError):
                downloader.models_root(path, True)

    def test_symlinked_directory_cannot_escape_models_root(self):
        outside = self.content / "outside"
        outside.mkdir()
        link = self.root / "redirect"
        link.symlink_to(outside, target_is_directory=True)
        self.item = dict(self.item, path="redirect/stolen.safetensors")
        with self.assertRaisesRegex(downloader.DownloadError, "symlinks"):
            self.download()
        self.assertEqual(list(outside.iterdir()), [])
        self.http.assert_not_called()

    def test_symlinked_partial_and_final_are_never_followed(self):
        outside = self.content / "outside.bin"
        outside.write_bytes(b"unrelated data")
        for path in (self.partial, self.final):
            with self.subTest(path=path):
                path.symlink_to(outside)
                with self.assertRaisesRegex(downloader.DownloadError, "symlinks"):
                    self.download()
                path.unlink()
        self.assertEqual(outside.read_bytes(), b"unrelated data")
        self.http.assert_not_called()

    def test_root_lock_rejects_simultaneous_download(self):
        with (
            downloader.root_lock(self.root),
            self.assertRaisesRegex(downloader.DownloadError, "Another model download"),
            downloader.root_lock(self.root),
        ):
            self.fail("Concurrent root lock unexpectedly succeeded")

    def test_process_alarm_bounds_blocked_operation(self):
        with (
            self.assertRaisesRegex(downloader.DownloadError, "deadline"),
            downloader.time_budget(0.03),
        ):
            time.sleep(0.2)

    def test_invalid_budget_is_rejected(self):
        for seconds in (0, -1, float("nan"), float("inf")):
            with (
                self.subTest(seconds=seconds),
                self.assertRaises(downloader.DownloadError),
            ):
                downloader.Deadline(seconds)

    def test_success_main_emits_one_json_summary(self):
        self.response(DATA)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            status = downloader.main(
                [
                    "--manifest",
                    str(self.manifest),
                    "--models-root",
                    str(self.root),
                    "--ephemeral",
                    "--max-seconds",
                    "5",
                ]
            )
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(self.final.read_bytes(), DATA)

    def test_error_json_does_not_expose_signed_urls_or_tokens(self):
        self.http.side_effect = urllib.error.HTTPError(
            "https://fixture.invalid/blob?secret-signed-token=DO-NOT-PRINT",
            403,
            "private secret",
            {},
            None,
        )
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            status = downloader.main(
                [
                    "--manifest",
                    str(self.manifest),
                    "--models-root",
                    str(self.root),
                    "--ephemeral",
                ]
            )
        self.assertEqual(status, 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Model server returned HTTP 403")
        self.assertNotIn("DO-NOT-PRINT", output.getvalue())
        self.assertNotIn("https", output.getvalue())

    def test_argument_error_is_json_failure(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            status = downloader.main(["--unexpected-option"])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
