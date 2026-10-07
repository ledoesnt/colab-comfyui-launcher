"""Small deterministic HTTP fixtures; never contact Colab, Drive, or model hosts."""

import argparse
import hashlib
import http.server
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


class LoopbackDownloads:
    """Real local HTTP transport; only the production HTTPS URL is redirected.

    No external hostname is contacted and no TLS/CDN performance is inferred.
    Responses, Range handling, partial writes and overlapping sockets are real.
    """

    def __init__(self, module, files, *, slots=2, slow_paths=(), corrupt_paths=()):
        self.module, self.files, self.slots = module, files, slots
        self.slow_paths, self.corrupt_paths = set(slow_paths), set(corrupt_paths)
        context = module.multiprocessing.get_context("fork")
        self.lock = context.Lock()
        self.metrics = context.RawArray("q", [0] * 515)
        self.paths = list(files)
        self.overlap = context.Event()
        self.release = context.Event()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("/resolve/", 1)[1].split("/", 1)[1]
                offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
                with fixture.lock:
                    fixture.active += 1
                    fixture.peak = max(fixture.peak, fixture.active)
                    index = fixture.metrics[2]
                    fixture.metrics[3 + 2 * index] = fixture.paths.index(path)
                    fixture.metrics[4 + 2 * index] = offset
                    fixture.metrics[2] = index + 1
                    if fixture.active >= fixture.slots:
                        fixture.overlap.set()
                try:
                    data = fixture.files[path]
                    body = data[offset:]
                    self.send_response(206 if offset else 200)
                    self.send_header("Content-Length", str(len(body)))
                    if offset:
                        self.send_header(
                            "Content-Range",
                            f"bytes {offset}-{len(data) - 1}/{len(data)}",
                        )
                    self.end_headers()
                    fixture.overlap.wait(0.3)
                    if path in fixture.slow_paths:
                        fixture.release.wait(5)
                    if path in fixture.corrupt_paths:
                        body = b"X" * len(body)
                    time.sleep(0.03)
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with fixture.lock:
                        fixture.active -= 1

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.process = context.Process(
            target=lambda: self.server.serve_forever(poll_interval=0.02),
            name="loopback-model-fixture",
            daemon=True,
        )
        self.opener = module.urllib.request.build_opener(
            module.urllib.request.ProxyHandler({})
        )

    @property
    def active(self):
        return self.metrics[0]

    @active.setter
    def active(self, value):
        self.metrics[0] = value

    @property
    def peak(self):
        return self.metrics[1]

    @peak.setter
    def peak(self, value):
        self.metrics[1] = value

    @property
    def requests(self):
        with self.lock:
            return [
                (self.paths[self.metrics[3 + 2 * index]], self.metrics[4 + 2 * index])
                for index in range(self.metrics[2])
            ]

    def __enter__(self):
        # The server's request threads live in its owned process. The download
        # controller stays single-threaded, like the standalone production CLI.
        self.process.start()

        def route(request, timeout):
            # The manifest still generates a fixed production HTTPS URL. The
            # fixture translates only its origin to the loopback HTTP server.
            url = (
                "http://127.0.0.1:"
                + str(self.server.server_port)
                + self.module.urllib.parse.urlsplit(request.full_url).path
            )
            local = self.module.urllib.request.Request(
                url, headers=dict(request.header_items())
            )
            response = self.opener.open(local, timeout=timeout)
            response.geturl = lambda: request.full_url
            return response

        self.patch = mock.patch.object(
            self.module.urllib.request, "urlopen", side_effect=route
        )
        self.patch.start()
        return self

    def __exit__(self, *args):
        self.patch.stop()
        self.release.set()
        self.process.terminate()
        self.process.join(2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(0.5)
        self.process.close()
        self.server.server_close()


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
            workers=1,
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

    def test_drive_writer_timestamp_transition_preserves_stream_sha(self):
        self.root = self.drive / "MyDrive" / "owned-model-cache"
        self.final = self.root / self.item["path"]
        self.final.parent.mkdir(parents=True)
        self.partial = self.final.with_name(self.final.name + ".partial")
        self.response(DATA)
        original = downloader.file_identity

        def fuse_metadata(path):
            value = original(path)
            if path == self.partial and value is not None:
                return dict(
                    value,
                    mtime_ns=value["mtime_ns"] + 1,
                    ctime_ns=value["ctime_ns"] + 1,
                )
            return value

        with (
            mock.patch.object(downloader.os.path, "ismount", return_value=True),
            mock.patch.object(downloader, "file_identity", side_effect=fuse_metadata),
            mock.patch.object(
                downloader,
                "hash_file",
                side_effect=AssertionError("No second Drive SHA read"),
            ),
        ):
            self.assertEqual(self.download()["verification"], "stream_sha256")
        self.assertEqual(self.final.read_bytes(), DATA)
        receipts = downloader.Receipts(self.root, "Fixture-Org/Fixture-Model", "a" * 40)
        self.assertTrue(receipts.matches(self.final, self.item))

    def test_vm_writer_timestamp_change_still_prevents_publication(self):
        self.response(DATA)
        original = downloader.file_identity

        def changed(path):
            value = original(path)
            if path == self.partial and value is not None:
                return dict(value, ctime_ns=value["ctime_ns"] + 1)
            return value

        with (
            mock.patch.object(downloader, "file_identity", side_effect=changed),
            self.assertRaisesRegex(downloader.DownloadError, "Partial changed"),
        ):
            self.download()
        self.assertFalse(self.final.exists())

    def test_drive_writer_never_accepts_replaced_inode_device_or_size(self):
        path = self.drive / "MyDrive" / "owned.partial"
        path.write_bytes(DATA)
        expected = downloader.file_identity(path)
        for key in ("device", "inode", "size"):
            with (
                self.subTest(key=key),
                mock.patch.object(downloader.os.path, "ismount", return_value=True),
            ):
                current = dict(
                    expected,
                    mtime_ns=expected["mtime_ns"] + 1,
                    ctime_ns=expected["ctime_ns"] + 1,
                )
                current[key] += 1
                with mock.patch.object(
                    downloader, "file_identity", return_value=current
                ):
                    self.assertFalse(downloader.written_partial_matches(path, expected))

    def test_unmounted_drive_does_not_relax_writer_timestamps(self):
        path = self.drive / "MyDrive" / "owned.partial"
        path.write_bytes(DATA)
        expected = downloader.file_identity(path)
        changed = dict(expected, ctime_ns=expected["ctime_ns"] + 1)
        with (
            mock.patch.object(downloader.os.path, "ismount", return_value=False),
            mock.patch.object(downloader, "file_identity", return_value=changed),
        ):
            self.assertFalse(downloader.written_partial_matches(path, expected))

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

    def extra_manifest(self, item=None, **changes):
        path = self.content / "extra.json"
        value = {
            "repo_id": "Fixture-Org/Other-Model",
            "revision": "b" * 40,
            "files": [
                item
                or dict(
                    self.item,
                    path="loras/extra.safetensors",
                    source_path="weights/extra.safetensors",
                )
            ],
        }
        value.update(changes)
        path.write_text(json.dumps(value))
        return path

    def test_extra_manifest_downloads_correct_source_to_comfyui_destination(self):
        self.args.extra_manifest = [self.extra_manifest()]
        self.http.side_effect = lambda *_args, **_kwargs: Response(DATA)
        result = downloader.run(self.args)
        self.assertEqual(result["total_size_bytes"], 2 * len(DATA))
        self.assertEqual(len(result["sources"]), 2)
        self.assertEqual((self.root / "loras/extra.safetensors").read_bytes(), DATA)
        self.assertEqual(
            self.http.call_args.args[0].full_url,
            "https://huggingface.co/Fixture-Org/Other-Model/resolve/"
            + "b" * 40
            + "/weights/extra.safetensors",
        )
        self.assertFalse((self.root / "weights").exists())
        self.http.reset_mock(side_effect=True)
        with mock.patch.object(
            downloader, "hash_file", side_effect=AssertionError("Receipt reuse")
        ):
            again = downloader.run(self.args)
        self.assertTrue(
            all(
                item["verification"] == "verified_receipt_metadata"
                for item in again["files"]
            )
        )
        self.http.assert_not_called()

    def test_extra_sources_preserve_existing_base_receipts(self):
        self.response(DATA)
        self.download()
        self.args.extra_manifest = [self.extra_manifest()]
        self.http.side_effect = lambda *_args, **_kwargs: Response(DATA)
        with mock.patch.object(
            downloader, "hash_file", side_effect=AssertionError("No base reread")
        ):
            result = downloader.run(self.args)
        self.assertEqual(
            result["files"][0]["verification"], "verified_receipt_metadata"
        )
        self.assertEqual(result["files"][1]["verification"], "stream_sha256")
        self.assertEqual(self.http.call_count, 2)
        legacy = downloader.Receipts(self.root, "Fixture-Org/Fixture-Model", "a" * 40)
        self.assertTrue(legacy.matches(self.final, self.item))

    def test_auto_extra_manifest_only_applies_to_the_default_base(self):
        extra = self.extra_manifest()
        with (
            mock.patch.object(downloader, "DEFAULT_MANIFEST", self.manifest),
            mock.patch.object(downloader, "DEFAULT_EXTRA_MANIFEST", extra),
        ):
            self.assertEqual(len(downloader.load_manifests(self.manifest)["files"]), 2)
            other = self.content / "other.json"
            other.write_bytes(self.manifest.read_bytes())
            self.assertEqual(len(downloader.load_manifests(other)["files"]), 1)
            self.assertEqual(
                len(downloader.load_manifests(self.manifest, [extra])["files"]),
                2,
            )
            self.extra_manifest(files=[])
            self.assertEqual(len(downloader.load_manifests(self.manifest)["files"]), 1)

    def test_default_discovers_multiple_repositories_sorted_and_reuses_base_receipts(
        self,
    ):
        self.response(DATA)
        self.download()
        extra = self.extra_manifest(files=[])
        for filename, repo, revision, path in (
            ("extra-z.json", "Fixture-Org/Z-Model", "c" * 40, "loras/z.safetensors"),
            ("extra-a.json", "Fixture-Org/A-Model", "b" * 40, "vae/a.safetensors"),
        ):
            (self.content / filename).write_text(
                json.dumps(
                    {
                        "repo_id": repo,
                        "revision": revision,
                        "files": [dict(self.item, path=path)],
                    }
                )
            )
        # Other JSON documents are not model manifests selected by the pattern.
        (self.content / "manifest.schema.json").write_text("not a model manifest")
        (self.content / "h3-i2v-metadata.json").write_text("not a model manifest")
        (self.content / "selected-extra-manifests.json").write_text(
            json.dumps({"version": 1, "files": ["extra-z.json", "extra-a.json"]})
        )
        self.http.reset_mock()
        self.http.side_effect = lambda *_args, **_kwargs: Response(DATA)
        with (
            mock.patch.object(downloader, "DEFAULT_MANIFEST", self.manifest),
            mock.patch.object(downloader, "DEFAULT_EXTRA_MANIFEST", extra),
            mock.patch.object(
                downloader, "hash_file", side_effect=AssertionError("No base reread")
            ),
        ):
            result = downloader.run(self.args)
            self.assertEqual(
                [item["path"] for item in result["files"]],
                [self.item["path"], "vae/a.safetensors", "loras/z.safetensors"],
            )
            self.assertEqual(
                result["files"][0]["verification"], "verified_receipt_metadata"
            )
            self.assertEqual(self.http.call_count, 2)
            self.assertEqual(
                [source["repo_id"] for source in result["sources"]],
                [
                    "Fixture-Org/Fixture-Model",
                    "Fixture-Org/A-Model",
                    "Fixture-Org/Z-Model",
                ],
            )
            self.assertEqual(
                len(
                    downloader.load_manifests(
                        self.manifest, [self.content / "extra-a.json"]
                    )["files"]
                ),
                3,
            )
            self.http.reset_mock()
            again = downloader.run(self.args)
            self.assertTrue(
                all(
                    item["verification"] == "verified_receipt_metadata"
                    for item in again["files"]
                )
            )
            self.http.assert_not_called()

    def test_invalid_or_colliding_discovered_manifest_fails_before_network(self):
        extra = self.extra_manifest(files=[])
        additional = self.content / "extra-bad.json"
        for value in (
            "not valid JSON",
            json.dumps(
                {"repo_id": "Fixture-Org/Other", "revision": "main", "files": []}
            ),
            json.dumps(
                {
                    "repo_id": "Fixture-Org/Other",
                    "revision": "b" * 40,
                    "files": [self.item],
                }
            ),
        ):
            with self.subTest(value=value):
                additional.write_text(value)
                with (
                    mock.patch.object(downloader, "DEFAULT_MANIFEST", self.manifest),
                    mock.patch.object(downloader, "DEFAULT_EXTRA_MANIFEST", extra),
                    self.assertRaises(downloader.DownloadError),
                ):
                    downloader.run(self.args)
                self.assertFalse(self.final.exists())
                self.assertFalse((self.root / downloader.RECEIPT_NAME).exists())
        self.http.assert_not_called()

    def test_deployment_registry_selects_exact_lists_ignoring_stale_bad_files(self):
        self.response(DATA)
        self.download()
        common = self.extra_manifest(files=[])
        selected = self.content / "extra-current.json"
        selected.write_text(
            json.dumps(
                {
                    "repo_id": "Fixture-Org/Current",
                    "revision": "c" * 40,
                    "files": [dict(self.item, path="loras/current.safetensors")],
                }
            )
        )
        common.write_text("unselected common manifest is invalid")
        (self.content / "extra-stale.json").write_text(
            "unselected stale manifest is invalid"
        )
        registry = self.content / "selected-extra-manifests.json"
        registry.write_text(json.dumps({"version": 1, "files": [selected.name]}))
        self.http.reset_mock()
        self.http.side_effect = lambda *_args, **_kwargs: Response(DATA)
        with (
            mock.patch.object(downloader, "DEFAULT_MANIFEST", self.manifest),
            mock.patch.object(downloader, "DEFAULT_EXTRA_MANIFEST", common),
            mock.patch.object(
                downloader, "hash_file", side_effect=AssertionError("No core reread")
            ),
        ):
            result = downloader.run(self.args)
            self.assertEqual(
                [item["path"] for item in result["files"]],
                [self.item["path"], "loras/current.safetensors"],
            )
            self.assertEqual(
                result["files"][0]["verification"], "verified_receipt_metadata"
            )
            self.assertEqual(result["sources"][1]["repo_id"], "Fixture-Org/Current")
            self.assertEqual(self.http.call_count, 1)
            registry.write_text(json.dumps({"version": 1, "files": []}))
            self.assertEqual(len(downloader.load_manifests(self.manifest)["files"]), 1)
            self.assertEqual(
                (self.root / "loras/current.safetensors").read_bytes(), DATA
            )

    def test_deployment_registry_schema_and_names_fail_before_network(self):
        registry = self.content / "selected-extra-manifests.json"
        for value in (
            "invalid JSON",
            json.dumps([]),
            json.dumps({"version": True, "files": []}),
            json.dumps({"version": 2, "files": []}),
            json.dumps({"version": 1, "files": "extra.json"}),
            json.dumps({"version": 1, "files": [None]}),
            json.dumps({"version": 1, "files": [{}]}),
            json.dumps({"version": 1, "files": ["extra.json", "extra.json"]}),
            *(
                json.dumps({"version": 1, "files": [name]})
                for name in (
                    "../extra-x.json",
                    "/extra.json",
                    "extrafoo.json",
                    "manifest.schema.json",
                    "h3-i2v.json",
                    "extra-missing.json",
                )
            ),
        ):
            with self.subTest(value=value):
                registry.write_text(value)
                with (
                    mock.patch.object(downloader, "DEFAULT_MANIFEST", self.manifest),
                    self.assertRaises(downloader.DownloadError),
                ):
                    downloader.run(self.args)
        self.http.assert_not_called()
        self.assertFalse(self.final.exists())

    def test_deployment_registry_rejects_selected_directories_and_symlinks(self):
        registry = self.content / "selected-extra-manifests.json"
        directory = self.content / "extra-directory.json"
        directory.mkdir()
        linked = self.content / "extra-link.json"
        linked.symlink_to(self.manifest)
        for selected in (directory, linked):
            registry.write_text(json.dumps({"version": 1, "files": [selected.name]}))
            with (
                self.subTest(name=selected.name),
                mock.patch.object(downloader, "DEFAULT_MANIFEST", self.manifest),
                self.assertRaises(downloader.DownloadError),
            ):
                downloader.run(self.args)
        registry.unlink()
        registry.symlink_to(self.manifest)
        with (
            mock.patch.object(downloader, "DEFAULT_MANIFEST", self.manifest),
            self.assertRaises(downloader.DownloadError),
        ):
            downloader.run(self.args)
        self.http.assert_not_called()

    def test_extra_manifest_collisions_fail_before_download(self):
        for item in (
            self.item,
            dict(self.item, path="diffusion_models"),
            dict(self.item, path=self.item["path"] + "/nested"),
        ):
            with self.subTest(path=item["path"]):
                self.args.extra_manifest = [self.extra_manifest(item)]
                with self.assertRaises(downloader.DownloadError):
                    downloader.run(self.args)
        self.http.assert_not_called()
        self.assertFalse(self.final.exists())

    def test_source_path_is_validated_and_direct_file_urls_are_rejected(self):
        for fields in (
            {"source_path": "../escape"},
            {"source_path": "https://fixture.invalid/model"},
            {"source_path": "weights/model?token=x"},
            {"source_path": "weights//model"},
            {"url": "https://fixture.invalid/model?secret=value"},
            {"repo_id": "Other-Org/Other-Model"},
            {"revision": "main"},
        ):
            with self.subTest(fields=fields):
                self.write_manifest(files=[dict(self.item, **fields)])
                with self.assertRaises(downloader.DownloadError) as raised:
                    downloader.load_manifest(self.manifest)
                self.assertNotIn("secret=value", str(raised.exception))

    def test_repeatable_extra_manifest_cli_preserves_json_boundary(self):
        extra = self.extra_manifest()
        self.http.side_effect = lambda *_args, **_kwargs: Response(DATA)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = downloader.main(
                [
                    "--manifest",
                    str(self.manifest),
                    "--extra-manifest",
                    str(extra),
                    "--extra-manifest",
                    str(extra),
                    "--models-root",
                    str(self.root),
                    "--ephemeral",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output.getvalue())["files"]), 2)

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


class ConcurrentDownloadTests(unittest.TestCase):
    write_manifest = ModelDownloadTests.write_manifest

    def setUp(self):
        ModelDownloadTests.setUp(self)
        self.args.workers = 2
        self.children_before = {
            child.pid for child in downloader.multiprocessing.active_children()
        }

    def multi_manifest(self, count=4):
        self.files = {
            f"diffusion_models/file-{index}.bin": bytes([65 + index]) * (32 * 1024)
            for index in range(count)
        }
        self.items = [
            {
                "path": path,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for path, data in self.files.items()
        ]
        self.write_manifest(
            files=self.items,
            total_size_bytes=sum(len(data) for data in self.files.values()),
        )

    def assert_no_workers(self):
        self.assertEqual(
            {
                child.pid
                for child in downloader.multiprocessing.active_children()
                if child.name != "loopback-model-fixture"
            },
            self.children_before,
        )

    def test_default_two_workers_overlap_real_http_and_publish_every_receipt(self):
        self.multi_manifest()
        del self.args.workers
        with LoopbackDownloads(downloader, self.files) as server:
            result = downloader.run(self.args)
            self.assertEqual(server.peak, 2)
            self.assertEqual(len(server.requests), len(self.items))
        self.assertEqual(result["workers"], 2)
        self.assertEqual([item["path"] for item in result["files"]], list(self.files))
        receipts = downloader.Receipts(self.root, "Fixture-Org/Fixture-Model", "a" * 40)
        for item in self.items:
            self.assertEqual(
                (self.root / item["path"]).read_bytes(), self.files[item["path"]]
            )
            self.assertTrue(receipts.matches(self.root / item["path"], item))
        self.assertEqual(len(receipts.records), len(self.items))
        progress = json.loads(
            (self.content / "colab-comfyui-runtime/model-progress.json").read_text()
        )
        self.assertEqual(progress["status"], "succeeded")
        self.assertTrue(
            all(item["verification"] == "stream_sha256" for item in progress["files"])
        )
        self.assert_no_workers()

    def test_worker_limits_one_and_four_match_actual_socket_overlap(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                self.multi_manifest()
                self.args.workers = workers
                self.args.models_root = self.content / f"models-{workers}"
                with LoopbackDownloads(downloader, self.files, slots=workers) as server:
                    downloader.run(self.args)
                    self.assertEqual(server.peak, workers)
                self.assert_no_workers()

    def test_parallel_receipt_reuse_never_reopens_http_or_model_contents(self):
        self.multi_manifest()
        with LoopbackDownloads(downloader, self.files):
            downloader.run(self.args)
        with mock.patch.object(
            downloader,
            "hash_file",
            side_effect=AssertionError("No reread on verified metadata"),
        ):
            result = downloader.run(self.args)
        self.assertTrue(
            all(
                item["verification"] == "verified_receipt_metadata"
                for item in result["files"]
            )
        )
        self.http.assert_not_called()
        self.assert_no_workers()

    def test_parallel_real_http_range_resume_keeps_complete_hash_and_receipts(self):
        self.multi_manifest(2)
        offset = 1001
        for item in self.items:
            final = self.root / item["path"]
            final.parent.mkdir(parents=True, exist_ok=True)
            final.with_name(final.name + ".partial").write_bytes(
                self.files[item["path"]][:offset]
            )
        with LoopbackDownloads(downloader, self.files) as server:
            result = downloader.run(self.args)
            self.assertEqual(server.peak, 2)
            self.assertTrue(all(start == offset for _path, start in server.requests))
        self.assertTrue(all(item["status"] == "resumed" for item in result["files"]))
        for item in self.items:
            self.assertEqual(
                (self.root / item["path"]).read_bytes(), self.files[item["path"]]
            )
        self.assert_no_workers()

    def test_deadline_kills_only_owned_workers_even_while_http_reads_stall(self):
        self.multi_manifest(3)
        self.args.max_seconds = 0.5
        with LoopbackDownloads(
            downloader, self.files, slow_paths=list(self.files)
        ) as server:
            started = time.monotonic()
            with self.assertRaisesRegex(downloader.DownloadError, "deadline"):
                downloader.run(self.args)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(server.peak, 2)
            self.assertEqual(len(server.requests), 2)
            self.assert_no_workers()
        self.assertFalse((self.root / downloader.RECEIPT_NAME).exists())
        self.assertFalse(
            any((self.root / item["path"]).exists() for item in self.items)
        )
        with downloader.root_lock(self.root):
            pass

    def test_bad_hash_cancels_stalled_peer_and_does_not_start_pending_files(self):
        self.multi_manifest(3)
        bad, slow, pending = list(self.files)
        with LoopbackDownloads(
            downloader, self.files, slow_paths=[slow], corrupt_paths=[bad]
        ) as server:
            started = time.monotonic()
            with self.assertRaisesRegex(downloader.DownloadError, "SHA256"):
                downloader.run(self.args)
            self.assertLess(time.monotonic() - started, 2)
            self.assertNotIn(pending, [path for path, _offset in server.requests])
            self.assert_no_workers()
        self.assertFalse((self.root / bad).exists())
        self.assertTrue((self.root / bad).with_name("file-0.bin.partial").exists())
        progress = json.loads(
            (self.content / "colab-comfyui-runtime/model-progress.json").read_text()
        )
        self.assertEqual(progress["status"], "failed")
        self.assertFalse(any(item["status"] == "running" for item in progress["files"]))
        self.assertTrue(all(item["verification"] is None for item in progress["files"]))

    def test_abrupt_child_exit_fails_closed_and_releases_every_owned_worker(self):
        self.multi_manifest(3)

        def crash(*args):
            downloader.os._exit(17)

        started = time.monotonic()
        with (
            mock.patch.object(downloader, "download_worker", side_effect=crash),
            self.assertRaisesRegex(downloader.DownloadError, "verified result"),
        ):
            downloader.run(self.args)
        self.assertLess(time.monotonic() - started, 2)
        self.assert_no_workers()
        self.http.assert_not_called()
        self.assertFalse((self.root / downloader.RECEIPT_NAME).exists())
        progress = json.loads(
            (self.content / "colab-comfyui-runtime/model-progress.json").read_text()
        )
        self.assertEqual(progress["status"], "failed")
        self.assertFalse(any(item["status"] == "running" for item in progress["files"]))
        with downloader.root_lock(self.root):
            pass

    def test_disabled_selection_can_be_empty_but_catalog_keeps_every_file(self):
        self.write_manifest(files=[dict(self.item, auto_download=False)])
        selected = downloader.load_manifests(self.manifest)
        self.assertEqual(selected["files"], [])
        self.assertEqual(selected["total_size_bytes"], 0)
        catalog = downloader.load_manifests(self.manifest, include_disabled=True)
        self.assertEqual(catalog["files"][0]["path"], self.item["path"])
        self.assertFalse(catalog["files"][0]["auto_download"])
        self.write_manifest(files=[])
        with self.assertRaisesRegex(downloader.DownloadError, "nonempty"):
            downloader.load_manifests(self.manifest)

    def test_selection_flag_is_strict_and_receipt_identity_does_not_change(self):
        for value in (0, 1, "false", None):
            with self.subTest(value=value):
                self.write_manifest(files=[dict(self.item, auto_download=value)])
                with self.assertRaisesRegex(downloader.DownloadError, "boolean"):
                    downloader.load_manifest(self.manifest)
        receipts = downloader.Receipts(self.root, "Fixture-Org/Fixture-Model", "a" * 40)
        self.assertEqual(
            receipts.key(dict(self.item, auto_download=False)),
            receipts.key(dict(self.item, auto_download=True)),
        )

    def test_disabled_collision_or_bad_metadata_cannot_be_hidden_by_selection(self):
        extra = self.content / "disabled-extra.json"
        extra.write_text(
            json.dumps(
                {
                    "repo_id": "Fixture-Org/Other-Model",
                    "revision": "b" * 40,
                    "files": [dict(self.item, auto_download=False)],
                }
            )
        )
        with self.assertRaisesRegex(downloader.DownloadError, "Duplicate"):
            downloader.load_manifests(self.manifest, [extra])
        value = json.loads(extra.read_text())
        value["files"][0].update(path="vae/bad.bin", sha256="invalid")
        extra.write_text(json.dumps(value))
        with self.assertRaisesRegex(downloader.DownloadError, "sha256"):
            downloader.load_manifests(self.manifest, [extra])

    def test_worker_limits_fail_before_filesystem_or_network_side_effects(self):
        for value in (0, 5, True, "two", 1.5):
            with self.subTest(value=value):
                self.args.workers = value
                with self.assertRaisesRegex(downloader.DownloadError, "workers"):
                    downloader.run(self.args)
        self.http.assert_not_called()
        self.assertFalse((self.root / ".download.lock").exists())
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(downloader.main(["--workers", "5"]), 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_progress_keeps_independent_rates_and_fails_every_active_file(self):
        self.multi_manifest(2)
        with mock.patch.object(
            downloader.time, "monotonic", side_effect=[0, 1, 2, 3, 4, 5]
        ):
            progress = downloader.Progress(None, "download", self.items)
            progress.update(self.items[0], "download", 0)
            progress.update(self.items[1], "download", 0)
            progress.update(self.items[0], "download", 1024)
            progress.update(self.items[1], "download", 1024)
            progress.finish(False)
        for entry in progress.value["files"]:
            self.assertEqual(entry["elapsed_seconds"], 2)
            self.assertEqual(entry["rate_bytes_per_second"], 512)
            self.assertEqual(entry["status"], "failed")


if __name__ == "__main__":
    unittest.main()
