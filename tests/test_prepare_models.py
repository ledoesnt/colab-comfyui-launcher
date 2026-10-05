"""Deterministic cache preparation tests: no network, Drive, Colab, or weights."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CACHE_SPEC = importlib.util.spec_from_file_location(
    "download_models", ROOT / "scripts/download_models.py"
)
cache = importlib.util.module_from_spec(CACHE_SPEC)
CACHE_SPEC.loader.exec_module(cache)
PREPARE_SPEC = importlib.util.spec_from_file_location(
    "prepare_models", ROOT / "scripts/prepare_models.py"
)
preparer = importlib.util.module_from_spec(PREPARE_SPEC)
with mock.patch.dict(sys.modules, {"download_models": cache}):
    PREPARE_SPEC.loader.exec_module(preparer)
DATA = b"deterministic cache fixture, not a real model\n"


class PrepareModelsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="model-prepare-test-")
        self.addCleanup(temporary.cleanup)
        self.content = Path(temporary.name) / "content"
        self.drive = self.content / "drive"
        self.source_root = self.drive / "MyDrive" / "launcher-test" / "models"
        self.local_root = self.content / "colab-comfyui-runtime" / "models"
        self.source_root.mkdir(parents=True)
        for target, name, value in (
            (cache, "CONTENT_ROOT", self.content),
            (cache, "DRIVE_MOUNT", self.drive),
            (cache.os.path, "ismount", mock.Mock(return_value=True)),
            (preparer, "boot_id", mock.Mock(return_value="fixture-boot")),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            cache.urllib.request, "urlopen", side_effect=AssertionError("No network")
        )
        self.http = patch.start()
        self.addCleanup(patch.stop)
        self.item = {
            "path": "diffusion_models/tiny.safetensors",
            "size_bytes": len(DATA),
            "sha256": hashlib.sha256(DATA).hexdigest(),
        }
        self.manifest = self.content / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "repo_id": "Fixture-Org/Fixture-Model",
                    "revision": "a" * 40,
                    "files": [self.item],
                    "total_size_bytes": len(DATA),
                }
            )
        )
        self.source = self.source_root / self.item["path"]
        self.source.parent.mkdir()
        self.source.write_bytes(DATA)
        self.final = self.local_root / self.item["path"]
        self.partial = self.final.with_name(self.final.name + ".partial")
        self.args = argparse.Namespace(
            manifest=self.manifest,
            cache_root=self.source_root,
            local_models_root=self.local_root,
            max_seconds=10,
            verify_cache=False,
            progress_file=None,
        )

    def run_prepare(self):
        return preparer.run(self.args)

    def test_legacy_cache_is_copied_once_and_both_receipts_are_published(self):
        opened = []
        original = os.open

        def tracking_open(path, flags, *args, **kwargs):
            if Path(path) == self.source:
                opened.append(flags)
            return original(path, flags, *args, **kwargs)

        with (
            mock.patch.object(cache.os, "open", side_effect=tracking_open),
            mock.patch.object(
                cache,
                "hash_file",
                side_effect=AssertionError("Legacy first copy must not pre-hash Drive"),
            ),
        ):
            result = self.run_prepare()
        self.assertEqual(len(opened), 1)
        self.assertEqual(result["files"][0]["verification"], "stream_sha256")
        self.assertEqual(self.final.read_bytes(), DATA)
        self.assertFalse(self.partial.exists())
        self.assertTrue((self.source_root / cache.RECEIPT_NAME).is_file())
        self.assertTrue(preparer.local_cache_ready(self.local_root, self.manifest))
        self.http.assert_not_called()

    def test_opt_in_mixed_missing_and_legacy_cache_does_not_pre_hash_legacy(self):
        item = dict(self.item, path="text_encoders/missing.safetensors")
        manifest = json.loads(self.manifest.read_text())
        manifest["files"].append(item)
        manifest["total_size_bytes"] += item["size_bytes"]
        self.manifest.write_text(json.dumps(manifest))
        response = io.BytesIO(DATA)
        response.status = 200
        response.headers = {"Content-Length": str(len(DATA))}
        response.geturl = lambda: "https://fixture.invalid/blob"
        self.http.side_effect = None
        self.http.return_value = response
        self.args.download_missing = True
        original = os.open
        legacy_reads = []

        def tracking_open(path, flags, *args, **kwargs):
            if Path(path) == self.source:
                legacy_reads.append(flags)
            return original(path, flags, *args, **kwargs)

        phases = []
        copy_verifications = []
        update = cache.Progress.update

        def tracking_progress(progress, item, phase, *args, **kwargs):
            phases.append(phase)
            result = update(progress, item, phase, *args, **kwargs)
            if phase == "copy":
                copy_verifications.append(
                    next(
                        value["verification"]
                        for value in progress.value["files"]
                        if value["path"] == item["path"]
                    )
                )
            return result

        with (
            mock.patch.object(cache.os, "open", side_effect=tracking_open),
            mock.patch.object(
                cache,
                "hash_file",
                side_effect=AssertionError("No pre-hash for legacy or fresh download"),
            ),
            mock.patch.object(cache.Progress, "update", tracking_progress),
        ):
            result = self.run_prepare()
        self.assertEqual(len(legacy_reads), 1)
        self.http.assert_called_once()
        self.assertTrue(self.http.call_args.args[0].full_url.endswith(item["path"]))
        self.assertEqual(
            [entry["cache_status"] for entry in result["files"]],
            ["existing", "downloaded"],
        )
        self.assertTrue(
            all(entry["verification"] == "stream_sha256" for entry in result["files"])
        )
        self.assertEqual((self.local_root / item["path"]).read_bytes(), DATA)
        self.assertIn("download", phases)
        self.assertIn("copy", phases)
        self.assertTrue(copy_verifications)
        self.assertTrue(all(value is None for value in copy_verifications))
        snapshot = json.loads(
            (self.local_root.parent / "prepare-progress.json").read_text()
        )
        self.assertEqual(snapshot["operation"], "prepare")
        self.assertEqual(snapshot["status"], "succeeded")

    def test_missing_cache_without_opt_in_has_no_network_side_effect(self):
        self.source.unlink()
        with self.assertRaisesRegex(cache.DownloadError, "missing"):
            self.run_prepare()
        self.http.assert_not_called()

    def test_download_missing_does_not_replace_wrong_size_cache(self):
        self.args.download_missing = True
        self.source.write_bytes(DATA[:-1])
        with self.assertRaisesRegex(cache.DownloadError, "wrong size"):
            self.run_prepare()
        self.assertEqual(self.source.read_bytes(), DATA[:-1])
        self.http.assert_not_called()

    def test_same_vm_skip_and_download_reuse_never_read_model_contents(self):
        self.run_prepare()
        with (
            mock.patch.object(
                cache, "hash_file", side_effect=AssertionError("No content re-read")
            ),
            mock.patch.object(preparer.os, "fdopen", wraps=os.fdopen) as opened,
        ):
            result = self.run_prepare()
            self.assertEqual(
                result["files"][0]["verification"], "verified_receipt_metadata"
            )
            self.assertFalse(
                any(call.args[1] == "rb" for call in opened.call_args_list)
            )
            outcome = cache.download_file(
                self.source_root,
                "Fixture-Org/Fixture-Model",
                "a" * 40,
                self.item,
                cache.Deadline(10),
            )
            self.assertEqual(outcome["verification"], "verified_receipt_metadata")

    def test_new_vm_rejects_old_local_receipt_until_reverified(self):
        self.run_prepare()
        with mock.patch.object(preparer, "boot_id", return_value="different-boot"):
            self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))
            self.assertEqual(
                self.run_prepare()["files"][0]["verification"], "full_sha256"
            )
            self.assertTrue(preparer.local_cache_ready(self.local_root, self.manifest))

    def test_local_metadata_change_requires_a_real_hash_not_silent_skip(self):
        self.run_prepare()
        current = self.final.stat()
        os.utime(self.final, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))
        with mock.patch.object(cache, "hash_file", wraps=cache.hash_file) as hashing:
            result = self.run_prepare()
        self.assertEqual(hashing.call_count, 1)
        self.assertEqual(result["files"][0]["verification"], "full_sha256")

    def test_explicit_cache_verification_re_reads_drive_even_with_local_ready(self):
        self.run_prepare()
        self.args.verify_cache = True
        with mock.patch.object(cache, "hash_file", wraps=cache.hash_file) as hashing:
            self.run_prepare()
        self.assertEqual(hashing.call_count, 1)
        self.assertEqual(hashing.call_args.args[0], self.source)

    def test_invalid_source_size_fails_before_copy_and_does_not_change_cache(self):
        self.source.write_bytes(DATA[:-1])
        with self.assertRaisesRegex(cache.DownloadError, "wrong size"):
            self.run_prepare()
        self.assertFalse(self.final.exists())
        self.assertFalse(self.partial.exists())
        progress = json.loads(
            (self.local_root.parent / "prepare-progress.json").read_text()
        )
        self.assertEqual(progress["status"], "failed")

    def test_invalid_source_hash_never_publishes_model_or_verified_receipt(self):
        self.source.write_bytes(b"X" * len(DATA))
        with self.assertRaisesRegex(cache.DownloadError, "SHA256"):
            self.run_prepare()
        self.assertFalse(self.final.exists())
        self.assertTrue(self.partial.exists())
        self.assertFalse((self.source_root / cache.RECEIPT_NAME).exists())
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))

    def test_changed_source_during_copy_is_rejected_even_if_bytes_were_valid(self):
        update = cache.Progress.update

        def change_source(progress, item, phase, done, *args, **kwargs):
            if phase == "copy" and done:
                self.source.write_bytes(b"X" * len(DATA))
            return update(progress, item, phase, done, *args, **kwargs)

        with (
            mock.patch.object(cache.Progress, "update", change_source),
            self.assertRaisesRegex(cache.DownloadError, "changed during copy"),
        ):
            self.run_prepare()
        self.assertFalse(self.final.exists())

    def test_corrupt_existing_local_file_is_preserved(self):
        self.run_prepare()
        self.final.write_bytes(b"X" * len(DATA))
        with self.assertRaisesRegex(cache.DownloadError, "not overwritten"):
            self.run_prepare()
        self.assertEqual(self.final.read_bytes(), b"X" * len(DATA))

    def test_local_partial_is_restarted_and_overlong_partial_is_rejected(self):
        self.final.parent.mkdir(parents=True)
        self.partial.write_bytes(b"discarded partial")
        self.run_prepare()
        self.assertEqual(self.final.read_bytes(), DATA)
        self.final.unlink()
        self.partial.write_bytes(DATA + b"extra")
        with self.assertRaisesRegex(cache.DownloadError, "longer"):
            self.run_prepare()

    def test_drive_unmounted_and_local_boundary_guard_do_not_copy(self):
        with (
            mock.patch.object(cache.os.path, "ismount", return_value=False),
            self.assertRaisesRegex(cache.DownloadError, "not mounted"),
        ):
            self.run_prepare()
        self.assertFalse(self.local_root.exists())
        self.args.local_models_root = self.content / "other-models"
        with self.assertRaisesRegex(cache.DownloadError, "dedicated"):
            self.run_prepare()
        self.assertFalse(self.args.local_models_root.exists())

    def test_symlinked_source_and_local_paths_are_never_followed(self):
        outside = self.content / "outside.bin"
        outside.write_bytes(DATA)
        self.source.unlink()
        self.source.symlink_to(outside)
        with self.assertRaisesRegex(cache.DownloadError, "symlinks"):
            self.run_prepare()
        self.source.unlink()
        self.source.write_bytes(DATA)
        self.final.parent.mkdir(parents=True)
        self.partial.symlink_to(outside)
        with self.assertRaisesRegex(cache.DownloadError, "symlinks"):
            self.run_prepare()
        self.assertEqual(outside.read_bytes(), DATA)

    def test_source_lock_blocks_prepare_before_any_copy(self):
        with (
            cache.root_lock(self.source_root),
            self.assertRaisesRegex(cache.DownloadError, "Another model"),
        ):
            self.run_prepare()
        self.assertFalse(self.final.exists())

    def test_readiness_is_read_only_and_bound_to_manifest_and_stat(self):
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))
        self.assertFalse(self.local_root.exists())
        self.run_prepare()
        with (
            mock.patch.object(
                cache, "hash_file", side_effect=AssertionError("No model read")
            ),
            mock.patch.object(
                cache, "atomic_json", side_effect=AssertionError("No readiness write")
            ),
        ):
            self.assertTrue(preparer.local_cache_ready(self.local_root, self.manifest))
            altered = json.loads(self.manifest.read_text())
            altered["revision"] = "b" * 40
            self.manifest.write_text(json.dumps(altered))
            self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))

    def test_cli_outputs_one_redacted_json_and_deadline_bounds_copy(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            status = preparer.main(
                [
                    "--manifest",
                    str(self.manifest),
                    "--cache-root",
                    str(self.source_root),
                ]
            )
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.final.unlink()

        def blocked(*args, **kwargs):
            import time

            time.sleep(0.1)

        self.args.max_seconds = 0.02
        with (
            mock.patch.object(preparer, "copy_file", side_effect=blocked),
            self.assertRaisesRegex(cache.DownloadError, "deadline"),
        ):
            self.run_prepare()
        self.assertFalse(self.final.exists())


class EphemeralModelsTests(unittest.TestCase):
    def setUp(self):
        PrepareModelsTests.setUp(self)
        self.args.ephemeral = True
        self.args.cache_root = None
        self.args.download_missing = True
        patch = mock.patch.object(
            cache.os.path,
            "ismount",
            side_effect=AssertionError("No Drive mount checks"),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def run_prepare(self):
        return preparer.run(self.args)

    def response(self, data=DATA, status=200, headers=None):
        response = io.BytesIO(data)
        response.status = status
        response.headers = headers or {"Content-Length": str(len(data))}
        response.geturl = lambda: "https://fixture.invalid/blob"
        self.http.side_effect = None
        self.http.return_value = response

    def progress(self):
        return json.loads(
            (self.local_root.parent / "prepare-progress.json").read_text()
        )

    def test_downloads_once_to_final_models_without_copy_hash_or_drive_receipt(self):
        self.response()
        with (
            mock.patch.object(
                preparer, "copy_file", side_effect=AssertionError("No copy")
            ),
            mock.patch.object(
                cache, "hash_file", side_effect=AssertionError("No second hash")
            ),
        ):
            result = self.run_prepare()
        self.assertTrue(result["ephemeral"])
        self.assertTrue(result["models_ready"])
        self.assertIsNone(result["cache_root"])
        self.assertEqual(result["models_root"], str(self.local_root))
        self.assertEqual(result["files"][0]["verification"], "stream_sha256")
        self.assertEqual(self.final.read_bytes(), DATA)
        self.assertEqual(self.source.read_bytes(), DATA)
        self.assertFalse((self.source_root / cache.RECEIPT_NAME).exists())
        self.assertFalse((self.local_root.parent / "ephemeral-assets").exists())
        self.assertTrue(preparer.local_cache_ready(self.local_root, self.manifest))
        entry = self.progress()["files"][0]
        self.assertEqual(entry["phase"], "complete")
        self.assertEqual(entry["done_bytes"], len(DATA))
        self.assertEqual(entry["verification"], "stream_sha256")
        receipt = json.loads((self.local_root / cache.RECEIPT_NAME).read_text())
        self.assertEqual(
            next(iter(receipt["records"].values()))["boot_id"], "fixture-boot"
        )

    def test_same_vm_receipt_reuse_does_not_read_content_or_network(self):
        self.response()
        self.run_prepare()
        self.http.reset_mock()
        self.http.side_effect = AssertionError("No network on receipt reuse")
        with mock.patch.object(
            cache, "hash_file", side_effect=AssertionError("No model reads")
        ):
            result = self.run_prepare()
        self.assertEqual(
            result["files"][0]["verification"], "verified_receipt_metadata"
        )
        self.assertEqual(
            self.progress()["files"][0]["verification"], "verified_receipt_metadata"
        )
        self.http.assert_not_called()

    def test_boot_change_requires_actual_local_hash_then_refreshes_receipt(self):
        self.response()
        self.run_prepare()
        self.http.reset_mock()
        with (
            mock.patch.object(preparer, "boot_id", return_value="new-boot"),
            mock.patch.object(cache, "hash_file", wraps=cache.hash_file) as hashing,
        ):
            self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))
            result = self.run_prepare()
            self.assertTrue(preparer.local_cache_ready(self.local_root, self.manifest))
        hashing.assert_called_once()
        self.assertEqual(hashing.call_args.args[0], self.final)
        self.assertEqual(result["files"][0]["verification"], "full_sha256")
        self.http.assert_not_called()

    def test_explicit_audit_hashes_local_bytes_without_touching_drive(self):
        self.response()
        self.run_prepare()
        self.http.reset_mock()
        self.args.verify_cache = True
        with mock.patch.object(cache, "hash_file", wraps=cache.hash_file) as hashing:
            result = self.run_prepare()
        hashing.assert_called_once()
        self.assertEqual(hashing.call_args.args[0], self.final)
        self.assertEqual(result["files"][0]["verification"], "full_sha256")
        self.http.assert_not_called()

    def test_missing_file_without_explicit_download_fails_before_network(self):
        self.args.download_missing = False
        with self.assertRaisesRegex(cache.DownloadError, "download-missing"):
            self.run_prepare()
        self.assertEqual(self.progress()["status"], "failed")
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))
        self.http.assert_not_called()

    def test_corrupt_body_stays_partial_without_verified_receipt(self):
        self.response(b"X" * len(DATA))
        with self.assertRaisesRegex(cache.DownloadError, "SHA256"):
            self.run_prepare()
        self.assertFalse(self.final.exists())
        self.assertTrue(self.partial.exists())
        self.assertFalse((self.local_root / cache.RECEIPT_NAME).exists())
        self.assertEqual(self.progress()["status"], "failed")
        self.assertIsNone(self.progress()["files"][0]["verification"])
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))

    def test_resume_hashes_retained_prefix_then_streams_remaining_bytes(self):
        offset = 10
        self.final.parent.mkdir(parents=True)
        self.partial.write_bytes(DATA[:offset])
        self.response(
            DATA[offset:],
            206,
            {
                "Content-Length": str(len(DATA) - offset),
                "Content-Range": f"bytes {offset}-{len(DATA) - 1}/{len(DATA)}",
            },
        )
        with mock.patch.object(cache, "hash_file", wraps=cache.hash_file) as hashing:
            result = self.run_prepare()
        hashing.assert_called_once()
        self.assertEqual(hashing.call_args.args[0], self.partial)
        self.assertEqual(self.http.call_args.args[0].get_header("Range"), "bytes=10-")
        self.assertEqual(result["files"][0]["status"], "resumed")
        self.assertEqual(self.final.read_bytes(), DATA)
        self.assertTrue(preparer.local_cache_ready(self.local_root, self.manifest))

    def test_partial_body_never_publishes_a_final_model(self):
        self.response(DATA[:-1], headers={"Content-Length": str(len(DATA))})
        with self.assertRaisesRegex(cache.DownloadError, "incomplete"):
            self.run_prepare()
        self.assertFalse(self.final.exists())
        self.assertEqual(self.partial.read_bytes(), DATA[:-1])
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))

    def test_last_model_failure_leaves_overall_readiness_false(self):
        item = dict(self.item, path="vae/other.safetensors")
        manifest = json.loads(self.manifest.read_text())
        manifest["files"].append(item)
        manifest["total_size_bytes"] += len(DATA)
        self.manifest.write_text(json.dumps(manifest))

        def response(request, **kwargs):
            stream = io.BytesIO(
                DATA
                if request.full_url.endswith(self.item["path"])
                else b"X" * len(DATA)
            )
            stream.status = 200
            stream.headers = {"Content-Length": str(len(DATA))}
            stream.geturl = lambda: "https://fixture.invalid/blob"
            return stream

        self.http.side_effect = response
        with self.assertRaisesRegex(cache.DownloadError, "SHA256"):
            self.run_prepare()
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))
        progress = self.progress()
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["files"][0]["verification"], "stream_sha256")
        self.assertEqual(progress["files"][1]["status"], "failed")
        self.assertIsNone(progress["files"][1]["verification"])

    def test_existing_corrupt_local_file_is_not_replaced_or_downloaded(self):
        self.final.parent.mkdir(parents=True)
        self.final.write_bytes(b"X" * len(DATA))
        with self.assertRaisesRegex(cache.DownloadError, "not overwritten"):
            self.run_prepare()
        self.assertEqual(self.final.read_bytes(), b"X" * len(DATA))
        self.http.assert_not_called()

    def test_symlinks_and_non_dedicated_local_paths_are_rejected(self):
        outside = self.content / "outside-model.bin"
        outside.write_bytes(DATA)
        self.final.parent.mkdir(parents=True)
        self.final.symlink_to(outside)
        with self.assertRaisesRegex(cache.DownloadError, "symlinks"):
            self.run_prepare()
        self.assertEqual(outside.read_bytes(), DATA)
        self.args.local_models_root = self.content / "other-models"
        with self.assertRaisesRegex(cache.DownloadError, "dedicated"):
            self.run_prepare()
        self.assertFalse(self.args.local_models_root.exists())
        self.http.assert_not_called()

    def test_cache_root_conflict_fails_before_local_directory_or_network(self):
        self.args.cache_root = self.source_root
        with self.assertRaisesRegex(cache.DownloadError, "cannot be combined"):
            self.run_prepare()
        self.assertFalse(self.local_root.exists())
        self.http.assert_not_called()

    def test_local_model_lock_blocks_download_before_http(self):
        self.local_root.mkdir(parents=True)
        with (
            cache.root_lock(self.local_root),
            self.assertRaisesRegex(cache.DownloadError, "Another model"),
        ):
            self.run_prepare()
        self.http.assert_not_called()

    def test_cli_ephemeral_interface_returns_actual_readiness(self):
        self.response()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            status = preparer.main(
                ["--manifest", str(self.manifest), "--ephemeral", "--download-missing"]
            )
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(result["models_ready"])
        self.assertIsNone(result["cache_root"])

    def test_ephemeral_deadline_bounds_blocked_download_and_keeps_partial(self):
        self.response()
        self.args.max_seconds = 0.02

        def blocked_read(*args, **kwargs):
            time.sleep(0.1)
            return DATA

        self.http.return_value.read = blocked_read
        with self.assertRaisesRegex(cache.DownloadError, "deadline"):
            self.run_prepare()
        self.assertFalse(self.final.exists())
        self.assertTrue(self.partial.exists())
        self.assertFalse(preparer.local_cache_ready(self.local_root, self.manifest))
        self.assertEqual(self.progress()["status"], "failed")
        self.assertIsNone(self.progress()["files"][0]["verification"])


if __name__ == "__main__":
    unittest.main()
