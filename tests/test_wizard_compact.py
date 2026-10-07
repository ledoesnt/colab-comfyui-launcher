"""Small-terminal usability regressions; fixtures never contact Colab."""

import unittest
from unittest import mock

from test_wizard import Frame, LocalWorker, dashboard, wizard

SIZES = ((24, 80), (30, 80), (28, 100))


class CompactWizardTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)
        self.ui.config.session = "compact-layout-test"
        self.ui.page = "pipeline"
        self.ui.stage = "Preparing verified local models"
        files = [
            {
                "path": "diffusion_models/video-model.safetensors",
                "done_bytes": 25,
                "total_bytes": 100,
                "phase": "copy",
            },
            {
                "path": "text_encoders/text-model.safetensors",
                "done_bytes": 100,
                "total_bytes": 100,
                "phase": "verified",
                "verification": "stream_sha256",
            },
            {
                "path": "vae/video-vae.safetensors",
                "done_bytes": 0,
                "total_bytes": 100,
                "phase": "queued",
            },
            {
                "path": "vae/audio-vae.safetensors",
                "done_bytes": 83,
                "total_bytes": 100,
                "phase": "download",
            },
            {
                "path": "loras/turbo-lora.safetensors",
                "done_bytes": 63,
                "total_bytes": 100,
                "phase": "copy",
            },
            {
                "path": "embeddings/style-token.safetensors",
                "done_bytes": 100,
                "total_bytes": 100,
                "phase": "flush",
            },
        ]
        self.ui.status = {
            "drive": {"mounted": True, "mydrive_ready": True},
            "installation": {"status": "ready"},
            "model_prepare": {
                "running": True,
                "status": "running",
                "progress": {"files": files},
            },
        }

    def draw(self, size):
        screen = Frame(*size)
        with mock.patch.object(wizard.curses, "doupdate") as update:
            self.ui._draw(screen)
        self.assertEqual(screen.updates, 1)
        self.assertEqual(screen.refreshes, 0)
        update.assert_called_once_with()
        return screen

    def test_first_frame_shows_stage_and_all_six_model_meters(self):
        for size in SIZES:
            with self.subTest(size=size):
                self.ui.detail_offset = 0
                screen = self.draw(size)
                self.assertIn("Preparing verified local models", screen.text)
                self.assertEqual(screen.text.count("["), 6)
                for name in (
                    "video-model",
                    "text-model",
                    "video-vae",
                    "audio-vae",
                    "turbo-lora",
                    "style-token",
                ):
                    self.assertIn(name, screen.text)
                self.assertIn("25%", screen.text)
                self.assertIn("83%", screen.text)
                self.assertIn("SHA verified", screen.text)
                self.assertIn("validation pending", screen.text)
                self.assertIn("Enter confirm", screen.text)

    def test_model_addition_review_shows_source_and_destination_without_paging(self):
        self.ui.page = "model_add_confirm"
        self.ui.config.model_url = (
            "https://huggingface.co/owner/model/blob/main/style.safetensors"
        )
        self.ui.config.model_path = "embeddings/style.safetensors"
        screen = self.draw((24, 80))
        self.assertIn(
            "Source: https://huggingface.co/owner/model/blob/main/style.safetensors",
            screen.text,
        )
        self.assertIn("Destination: embeddings/style.safetensors", screen.text)
        self.assertIn("Auto-download: enabled", screen.text)
        self.assertEqual(self.worker.submitted, [])

    def test_all_advanced_options_fit_and_remain_visible_while_details_scroll(self):
        self.ui.page = "advanced"
        for size in SIZES:
            with self.subTest(size=size):
                self.ui.detail_offset = 0
                before = self.draw(size)
                self.ui._key("KEY_NPAGE")
                after = self.draw(size)
                for choice in self.ui._choices():
                    self.assertIn(choice.label, before.text)
                    self.assertIn(choice.label, after.text)
                self.assertNotEqual(before.text, after.text)
                self.assertEqual(self.worker.submitted, [])

    def test_menu_movement_does_not_execute_or_hide_any_gpu_choice(self):
        self.ui.page = "gpu"
        for size in SIZES:
            with self.subTest(size=size):
                before = self.draw(size)
                self.ui._key("KEY_DOWN")
                after = self.draw(size)
                for choice in self.ui._choices():
                    self.assertIn(choice.label, before.text)
                    self.assertIn(choice.label, after.text)
                self.assertIn("Enter confirm", after.text)
                self.assertEqual(self.worker.submitted, [])

    def test_full_path_and_receipt_explanation_are_available_by_paging(self):
        item = self.ui.status["model_prepare"]["progress"]["files"][1]
        item["verification"] = "verified_receipt_metadata"
        self.ui.notice = "Detailed status remains available while transfer runs."
        seen = set()
        for _ in range(14):
            screen = self.draw((24, 80))
            seen.update(screen.text.splitlines())
            self.assertIn("Exit and keep resources", screen.text)
            self.ui._key("KEY_NPAGE")
        text = "\n".join(seen)
        self.assertIn(item["path"], text)
        self.assertIn("receipt metadata reused; no new SHA256", text)
        self.assertIn(self.ui.notice, text)

    def test_compact_input_masks_code_and_hides_placeholder_after_typing(self):
        self.ui._input("port", "summary")
        self.ui.theme.roles["input"] = 123
        self.ui.theme.roles["muted"] = 456
        for size in SIZES:
            with self.subTest(size=size):
                self.ui.input_value = ""
                before = self.draw(size)
                self.assertIn("> 8188", before.text)
                self.assertEqual(before.attributes[6][2], 456)
                self.ui.input_value = "9001"
                after = self.draw(size)
                field = "".join(after.rows[6])
                self.assertIn("> 9001", field)
                self.assertNotIn("8188", field)
                self.assertEqual(after.attributes[6][2], 123)
        self.ui.input_field = "auth_code"
        self.ui.input_value = "private-provider-example"
        screen = self.draw((24, 80))
        self.assertNotIn(self.ui.input_value, screen.text)
        self.assertIn("********", screen.text)
        self.assertEqual(self.worker.responses, [])

    def test_attention_and_provider_instructions_precede_models(self):
        self.ui.error = "Mount result is unknown; inspect before retrying."
        self.ui.auth = {"text": "Complete the provider step, then confirm here."}
        screen = self.draw((24, 80))
        self.assertLess(
            screen.text.index(self.ui.error), screen.text.index("LOCAL MODELS")
        )
        self.assertLess(
            screen.text.index(self.ui.auth["text"]), screen.text.index("LOCAL MODELS")
        )
        self.assertEqual(self.worker.responses, [])

    def test_readiness_link_remains_visible_before_historical_model_receipts(self):
        self.ui.page = "ready"
        with mock.patch.object(self.ui, "_url", return_value="http://127.0.0.1:8188"):
            screen = self.draw((24, 80))
        self.assertIn("Open ComfyUI: http://127.0.0.1:8188", screen.text)
        for choice in self.ui._choices():
            self.assertIn(choice.label, screen.text)

    def test_home_shows_login_status_and_every_option_before_historical_models(self):
        self.ui.page = "home"
        self.ui.account = {
            "state": "not_authenticated",
            "message": "Authorize Colab before creating a runtime.",
        }
        screen = self.draw((24, 80))
        self.assertIn("COLAB LOGIN · NOT AUTHENTICATED", screen.text)
        self.assertIn(self.ui.account["message"], screen.text)
        self.assertEqual(screen.text.count("COLAB LOGIN · NOT AUTHENTICATED"), 1)
        self.assertIn("Manage models", screen.text)
        for choice in self.ui._choices():
            self.assertIn(choice.label, screen.text)
        self.assertNotIn("LOCAL MODELS", screen.text)
        self.assertEqual(self.worker.submitted, [])

    def test_idle_menu_header_does_not_report_a_completed_provider_operation(self):
        self.ui.stage = "Checking Colab login"
        self.ui.page = "gpu"
        screen = self.draw((24, 80))
        self.assertIn("Now: Choose GPU model", screen.text)
        self.assertNotIn("Now: Checking Colab login", screen.text)
        self.assertEqual(self.worker.submitted, [])

    def test_model_catalog_shows_selected_full_path_size_and_saved_flag_first(self):
        self.ui.page = "models"
        files = self.ui.status["model_prepare"]["progress"]["files"]
        self.ui.model_entries = [
            {
                "path": item["path"],
                "size_bytes": 100,
                "manifest": "models/h3.json",
                "auto_download": index != 1,
            }
            for index, item in enumerate(files)
        ] + [
            {
                "path": "loras/extra-one.safetensors",
                "size_bytes": 100,
                "manifest": "models/extra.json",
                "auto_download": True,
            },
            {
                "path": "loras/extra-two.safetensors",
                "size_bytes": 100,
                "manifest": "models/extra.json",
                "auto_download": True,
            },
        ]
        self.ui.selected = 1
        screen = self.draw((24, 80))
        self.assertIn("ABOUT THIS MODEL", screen.text)
        self.assertIn("Path: " + files[1]["path"], screen.text)
        self.assertIn("Auto-download: disabled · 100.0B · h3.json", screen.text)
        self.assertIn("Enter toggles and saves", screen.text)
        self.assertIn("Add model · Hugging Face file URL", screen.text)
        self.assertIn("Return · choices are saved", screen.text)
        self.assertEqual(self.worker.submitted, [])

    def test_large_catalog_preserves_selected_metadata_and_scrollable_navigation(self):
        self.ui.page = "models"
        self.ui.model_entries = [
            {
                "path": f"loras/additional-model-{index:02d}.safetensors",
                "size_bytes": 100,
                "manifest": "models/extra.json",
                "auto_download": True,
            }
            for index in range(18)
        ]
        self.ui.selected = 13
        screen = self.draw((24, 80))
        self.assertIn("additional-model-13.safetensors", screen.text)
        self.assertIn("Auto-download: enabled · 100.0B · extra.json", screen.text)
        self.assertIn("Enter toggles and saves", screen.text)
        self.assertIn("/20", screen.text)
        self.assertEqual(self.worker.submitted, [])


if __name__ == "__main__":
    unittest.main()
