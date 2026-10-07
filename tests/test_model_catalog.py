"""Model preferences survive restart and deployment without downloading weights."""

import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from test_wizard import LocalWorker, dashboard, wizard

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "catalog_test", ROOT / "scripts/model_catalog.py"
)
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.base = self.directory / "h3.json"
        self.base.write_bytes((ROOT / "models/h3.json").read_bytes())
        self.module = dashboard._model_manifest_module()
        patch = mock.patch.multiple(
            self.module,
            DEFAULT_MANIFEST=self.base,
            DEFAULT_EXTRA_MANIFEST=self.directory / "extra.json",
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.metadata = {
            "repo_id": "owner/model",
            "revision": "a" * 40,
            "source_path": "weights/example.safetensors",
            "size_bytes": 8,
            "sha256": "b" * 64,
        }

    def test_default_six_enabled_and_toggle_persists(self):
        self.assertEqual(len(catalog.entries(self.module)), 6)
        self.assertEqual(len(self.module.load_manifests(self.base)["files"]), 6)
        path = catalog.entries(self.module)[0]["path"]
        self.assertFalse(catalog.toggle(self.module, path))
        self.assertEqual(len(self.module.load_manifests(self.base)["files"]), 5)
        self.assertEqual(len(catalog.entries(self.module)), 6)
        self.assertFalse(json.loads(self.base.read_text())["files"][0]["auto_download"])
        self.assertTrue(catalog.toggle(self.module, path))
        self.assertEqual(len(self.module.load_manifests(self.base)["files"]), 6)

    def test_all_disabled_allows_editor_and_retains_catalog(self):
        for row in catalog.entries(self.module):
            catalog.toggle(self.module, row["path"])
        manifest = self.module.load_manifests(self.base)
        self.assertEqual(manifest["files"], [])
        self.assertEqual(manifest["total_size_bytes"], 0)
        self.assertEqual(len(catalog.entries(self.module)), 6)

    def test_saved_choices_are_uploaded_and_reused_in_a_new_deployment(self):
        from test_bridge import bridge

        root = self.directory / "checkout"
        (root / "scripts").mkdir(parents=True)
        (root / "models").mkdir()
        (root / "workflows").mkdir()
        for name in (
            "bootstrap.sh",
            "runtime.py",
            "download_models.py",
            "prepare_models.py",
            "render_workflow.py",
        ):
            (root / "scripts" / name).write_text("fixture")
        first = catalog.entries(self.module)[0]["path"]
        catalog.toggle(self.module, first)
        (root / "models/h3.json").write_bytes(self.base.read_bytes())
        catalog.add(
            self.module,
            self.metadata,
            "loras/example.safetensors",
            dashboard.MODEL_SEARCH_CATEGORIES,
        )
        for source in self.directory.glob("extra-*.json"):
            (root / "models" / source.name).write_bytes(source.read_bytes())
        archive = self.directory / "deployment.zip"
        with mock.patch.object(bridge, "PROJECT_ROOT", root):
            bridge.build_archive(archive)
        deployed = self.directory / "deployed"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(deployed)
        with mock.patch.multiple(
            self.module,
            DEFAULT_MANIFEST=deployed / "models/h3.json",
            DEFAULT_EXTRA_MANIFEST=deployed / "models/extra.json",
        ):
            selected = self.module.load_manifests(self.module.DEFAULT_MANIFEST)["files"]
        self.assertEqual(len(selected), 6)
        self.assertNotIn(first, [row["path"] for row in selected])
        self.assertIn("loras/example.safetensors", [row["path"] for row in selected])

    def test_add_pins_repository_and_later_default_preparation_selects_it(self):
        name = catalog.add(
            self.module,
            self.metadata,
            "loras/example.safetensors",
            dashboard.MODEL_SEARCH_CATEGORIES,
        )
        saved = json.loads((self.directory / name).read_text())
        self.assertEqual(saved["revision"], "a" * 40)
        self.assertTrue(saved["files"][0]["auto_download"])
        self.assertEqual(
            saved["files"][0]["source_path"], "weights/example.safetensors"
        )
        selected = self.module.load_manifests(self.base)["files"]
        self.assertEqual(len(selected), 7)
        self.assertEqual(selected[-1]["repo_id"], "owner/model")
        catalog.toggle(self.module, "loras/example.safetensors")
        self.assertEqual(len(self.module.load_manifests(self.base)["files"]), 6)
        self.assertEqual(len(catalog.entries(self.module)), 7)

    def test_disabled_destination_still_cannot_be_duplicated(self):
        row = catalog.entries(self.module)[0]
        catalog.toggle(self.module, row["path"])
        before = {p.name: p.read_bytes() for p in self.directory.glob("*.json")}
        with self.assertRaisesRegex(self.module.DownloadError, "Duplicate"):
            catalog.add(
                self.module,
                self.metadata,
                row["path"],
                dashboard.MODEL_SEARCH_CATEGORIES,
            )
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.directory.glob("*.json")}
        )

    def test_appending_updates_declared_total_before_atomic_publication(self):
        name = catalog.add(
            self.module,
            self.metadata,
            "loras/example.safetensors",
            dashboard.MODEL_SEARCH_CATEGORIES,
        )
        path = self.directory / name
        saved = json.loads(path.read_text())
        saved["total_size_bytes"] = 8
        path.write_text(json.dumps(saved))
        metadata = dict(
            self.metadata, source_path="weights/second.safetensors", size_bytes=4
        )
        catalog.add(
            self.module,
            metadata,
            "loras/second.safetensors",
            dashboard.MODEL_SEARCH_CATEGORIES,
        )
        self.assertEqual(self.module.load_manifest(path)["total_size_bytes"], 12)
        self.assertEqual(len(self.module.load_manifests(self.base)["files"]), 8)

    def test_invalid_proposed_write_keeps_previous_catalog(self):
        original = self.base.read_bytes()
        with self.assertRaises(self.module.DownloadError):
            catalog.write(
                self.base, {"repo_id": "not-pinned"}, self.module.load_manifest
            )
        self.assertEqual(self.base.read_bytes(), original)
        self.assertEqual(list(self.directory.glob(".catalog-*")), [])

    def test_unsupported_category_does_not_save(self):
        with self.assertRaisesRegex(catalog.CatalogError, "registered"):
            catalog.add(
                self.module,
                self.metadata,
                "unknown/example.safetensors",
                dashboard.MODEL_SEARCH_CATEGORIES,
            )
        self.assertEqual(list(self.directory.glob("extra-*.json")), [])

    def test_symlink_manifest_rejected_without_overwrite(self):
        original = self.base.read_bytes()
        other = self.directory / "original.json"
        self.base.rename(other)
        self.base.symlink_to(other)
        with self.assertRaises((catalog.CatalogError, self.module.DownloadError)):
            catalog.toggle(self.module, json.loads(original)["files"][0]["path"])
        self.assertEqual(other.read_bytes(), original)

    def test_public_metadata_lookup_pins_commit_size_and_sha_without_weights(self):
        response = io.BytesIO(
            json.dumps(
                {
                    "sha": "a" * 40,
                    "siblings": [
                        {
                            "rfilename": "weights/example.safetensors",
                            "size": 8,
                            "lfs": {"sha256": "b" * 64, "size": 8},
                        }
                    ],
                }
            ).encode()
        )
        response.geturl = lambda: (
            "https://huggingface.co/api/models/owner/model/revision/main"
        )
        with mock.patch.object(
            catalog.urllib.request, "urlopen", return_value=response
        ) as opened:
            self.assertEqual(
                catalog.lookup(
                    "https://huggingface.co/owner/model/blob/main/weights/example.safetensors"
                ),
                self.metadata,
            )
        self.assertIn("/api/models/", opened.call_args.args[0].full_url)
        self.assertNotIn("Authorization", opened.call_args.args[0].headers)

    def test_no_trusted_hash_refuses_metadata(self):
        response = io.BytesIO(
            json.dumps(
                {
                    "sha": "a" * 40,
                    "siblings": [{"rfilename": "model.safetensors", "size": 8}],
                }
            ).encode()
        )
        response.geturl = lambda: "https://huggingface.co/api/models/owner/model"
        with (
            mock.patch.object(catalog.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(catalog.CatalogError, "LFS SHA256"),
        ):
            catalog.lookup(
                "https://huggingface.co/owner/model/resolve/main/model.safetensors"
            )

    def test_urls_with_credentials_unknown_hosts_and_traversal_rejected_before_io(self):
        for value in (
            "https://huggingface.co/owner/model/resolve/main/a?token=secret",
            "https://other.example/owner/model/blob/main/a",
            "http://huggingface.co/owner/model/blob/main/a",
            "https://huggingface.co/owner/model/blob/main/%2e%2e/a",
            "https://user:pass@huggingface.co/owner/model/blob/main/a",
        ):
            with (
                self.subTest(value=value),
                mock.patch.object(catalog.urllib.request, "urlopen") as opened,
                self.assertRaises(catalog.CatalogError),
            ):
                catalog.lookup(value)
            opened.assert_not_called()

    def test_menu_enter_toggles_without_allocating_and_new_ui_restores_choices(self):
        worker = LocalWorker()
        ui = wizard.Wizard(dashboard.Config(), object(), worker)
        ui._activate(wizard.Choice("models", "models"))
        self.assertEqual(ui.page, "models")
        self.assertTrue(ui._choices()[0].label.startswith("[✓]"))
        ui._key("KEY_DOWN")
        self.assertEqual(len(self.module.load_manifests(self.base)["files"]), 6)
        ui._key("\n")
        self.assertEqual(len(self.module.load_manifests(self.base)["files"]), 5)
        second = wizard.Wizard(dashboard.Config(), object(), LocalWorker())
        second._activate(wizard.Choice("models", "models"))
        self.assertTrue(second._choices()[1].label.startswith("[×]"))
        self.assertEqual(worker.submitted, [])

    def test_add_requires_url_category_and_separate_enter_confirmation(self):
        worker = LocalWorker()
        ui = wizard.Wizard(dashboard.Config(), object(), worker)
        ui._activate(wizard.Choice("models", "models"))
        ui._activate(wizard.Choice("model_add_url", "add"))
        ui.input_value = (
            "https://huggingface.co/owner/model/blob/main/weights/example.safetensors"
        )
        ui._key("\n")
        self.assertEqual(ui.page, "model_category")
        self.assertEqual(worker.submitted, [])
        ui._activate(wizard.Choice("category:loras", "loras"))
        self.assertEqual(ui.page, "model_add_confirm")
        self.assertEqual(worker.submitted, [])
        ui._key("\n")
        self.assertEqual(worker.submitted[-1][0], "model_add")
        self.assertEqual(
            worker.submitted[-1][1].model_path, "loras/example.safetensors"
        )


if __name__ == "__main__":
    unittest.main()
