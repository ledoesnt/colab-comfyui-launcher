"""Keyboard and footer usability without provider calls or credentials."""

import unittest
from unittest import mock

from test_wizard import Frame, LocalWorker, dashboard, wizard


class FooterTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)
        self.ui.account = {
            "state": "authenticated",
            "message": "Fixture login verified.",
        }
        self.ui.notice = self.ui.account["message"]
        self.ui.selected = next(
            i for i, choice in enumerate(self.ui._choices()) if choice.key == "existing"
        )

    def draw(self, height=24, width=80):
        screen = Frame(height, width)
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(screen)
        return screen

    def test_choice_help_is_fixed_above_keyboard_hints_in_both_layouts(self):
        self.ui.theme.roles.update(info_heading=123, info=456)
        for height, width in ((24, 50), (24, 80), (36, 120)):
            with self.subTest(height=height, width=width):
                screen = self.draw(height, width)
                help_rows = self.ui._footer_help(width - 2)
                top = height - 2 - len(help_rows)
                self.assertEqual(screen.text.count("ABOUT THIS CHOICE"), 1)
                self.assertIn("ABOUT THIS CHOICE", "".join(screen.rows[top]))
                self.assertEqual(screen.attributes[top][1], 123)
                self.assertEqual(screen.attributes[top + 1][1], 456)
                self.assertIn("Enter confirm", "".join(screen.rows[height - 2]))
                self.assertNotIn("details", "".join(screen.rows[height - 2]))
                self.assertNotIn(
                    "ABOUT THIS CHOICE",
                    "\n".join("".join(row) for row in screen.rows[:top]),
                )
        self.assertEqual(self.worker.submitted, [])

    def test_short_and_empty_details_have_no_range_or_scroll_hint(self):
        for page in ("home", "hardware"):
            self.ui._page(page)
            if page == "hardware":
                self.ui.notice = ""
            for height, width in ((24, 80), (36, 120)):
                with self.subTest(page=page, width=width):
                    text = self.draw(height, width).text
                    self.assertNotIn("Details ", text)
                    self.assertNotIn("more details", text)
                    self.assertNotIn("previous details", text)
                    self.assertNotIn("PgUp", text)
                    self.assertNotIn("PgDn", text)

    def test_arrow_paging_keeps_footer_selection_and_does_not_submit(self):
        self.ui.error = " ".join(f"diagnostic-{i:02d}" for i in range(120))
        before = self.draw()
        self.assertNotIn("more details", before.text)
        original = (self.ui.page, self.ui.selected, self.ui.config)
        self.ui._key("KEY_RIGHT")
        after = self.draw()
        self.assertNotEqual(before.text, after.text)
        self.assertGreater(self.ui.detail_offset, 0)
        self.assertEqual((self.ui.page, self.ui.selected, self.ui.config), original)
        for screen in (before, after):
            self.assertIn("ABOUT THIS CHOICE", "".join(screen.rows[-4]))
            self.assertIn("Inspect an existing runtime before continuing.", screen.text)
        self.ui._key("KEY_LEFT")
        self.assertEqual(self.ui.detail_offset, 0)
        self.assertEqual(self.worker.submitted, [])

    def test_paging_clamps_at_ends_and_new_page_resets_scroll(self):
        self.ui.error = " ".join(f"diagnostic-{i:02d}" for i in range(120))
        self.ui._key("KEY_LEFT")
        self.assertEqual(self.ui.detail_offset, 0)
        for _ in range(100):
            self.ui._key("KEY_RIGHT")
            self.draw()
        end = self.ui.detail_offset
        self.assertGreater(end, 0)
        self.assertNotIn("previous details", self.draw().text)
        self.ui._key("KEY_RIGHT")
        self.draw()
        self.assertEqual(self.ui.detail_offset, end)
        self.ui._page("hardware")
        self.assertEqual(self.ui.detail_offset, 0)
        self.assertEqual(self.worker.submitted, [])

    def test_small_detail_pages_can_reach_every_line_without_skipping(self):
        self.ui.error = " ".join(f"diagnostic-{i:02d}" for i in range(120))
        seen = []
        for _ in range(100):
            seen.append(self.draw().text)
            self.ui._key("KEY_RIGHT")
        visible = "\n".join(seen)
        for index in range(120):
            self.assertIn(f"diagnostic-{index:02d}", visible)
        self.assertEqual(self.worker.submitted, [])

    def test_page_keys_remain_compatible_without_being_required(self):
        for key, expected in (("KEY_NPAGE", 8), ("KEY_PPAGE", 0)):
            self.ui._key(key)
            self.assertEqual(self.ui.detail_offset, expected)
        self.assertEqual(self.worker.submitted, [])

    def test_arrows_do_not_scroll_or_submit_url_or_sensitive_input(self):
        for field in ("model_url", "auth_code"):
            with self.subTest(field=field):
                self.ui._input(field, "home")
                self.ui.input_value = "example-input"
                for key in ("KEY_LEFT", "KEY_RIGHT"):
                    self.ui._key(key)
                self.assertEqual(self.ui.detail_offset, 0)
                self.assertEqual(self.ui.input_value, "example-input")
                self.ui._key("a")
                self.assertEqual(self.ui.input_value, "example-inputa")
                screen = self.draw()
                self.assertNotIn("ABOUT THIS CHOICE", screen.text)
                self.assertIn("Enter submit", screen.text)
        self.assertEqual(self.worker.submitted, [])
        self.assertEqual(self.worker.responses, [])

    def test_plain_snapshot_keeps_help_once_after_menu(self):
        text = self.ui.snapshot()
        self.assertEqual(text.count("ABOUT THIS CHOICE"), 1)
        self.assertLess(
            text.index("Manage Colab account"), text.index("ABOUT THIS CHOICE")
        )
        self.assertNotIn(
            "ABOUT THIS CHOICE", "\n".join(value for value, _ in self.ui._details(80))
        )

    def test_model_metadata_and_footer_instruction_each_appear_once(self):
        self.ui._page("models")
        self.ui.model_entries = [
            {
                "path": "loras/style.safetensors",
                "size_bytes": 100,
                "manifest": "models/extra.json",
                "auto_download": True,
            }
        ]
        text = self.draw().text
        self.assertEqual(text.count("Path: loras/style.safetensors"), 1)
        self.assertEqual(text.count("Enter toggles and saves."), 1)
        self.assertIn("Auto-download: enabled", text)
        self.assertEqual(self.worker.submitted, [])

    def test_overflow_paging_hint_is_at_bottom_and_keeps_input(self):
        self.ui._input("model_url", "home")
        self.ui.input_value = (
            "https://huggingface.co/owner/model/blob/main/style.safetensors"
        )
        before = self.draw()
        self.assertNotIn("Ctrl+F", before.text)
        self.assertNotIn("→ more details", before.text)
        self.ui._key("\x06")
        after = self.draw()
        self.assertGreater(self.ui.detail_offset, 0)
        self.assertNotEqual(before.text, after.text)
        self.assertIn(self.ui.input_value, after.text)
        self.assertIn("Ctrl+B/F details", "".join(after.rows[-2]))
        self.assertEqual(after.text.count("Ctrl+B/F details"), 1)
        self.ui._key("\x02")
        self.assertEqual(self.ui.detail_offset, 0)
        self.assertEqual(self.worker.submitted, [])
        self.assertEqual(self.worker.responses, [])

    def test_long_help_does_not_consume_menu_or_keyboard_rows(self):
        self.ui._page("advanced")
        self.ui.selected = next(
            i
            for i, choice in enumerate(self.ui._choices())
            if choice.key == "prepare_refresh"
        )
        screen = self.draw(24, 50)
        self.assertIn("> Prepare / refresh models", screen.text)
        self.assertIn("extra.json", screen.text)
        self.assertIn("Enter confirm", "".join(screen.rows[-2]))
        self.assertIn("Exit retains resources", "".join(screen.rows[-1]))
        self.assertEqual(self.worker.submitted, [])


if __name__ == "__main__":
    unittest.main()
