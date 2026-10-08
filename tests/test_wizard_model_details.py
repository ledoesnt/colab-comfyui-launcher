"""Model names, paths and field colors in real-width terminal frames."""

import unittest
from unittest import mock

from test_wizard import Frame, LocalWorker, dashboard, wizard


class ModelDetailTests(unittest.TestCase):
    def setUp(self):
        self.worker = LocalWorker()
        self.ui = wizard.Wizard(dashboard.Config(), object(), self.worker)
        self.ui.account = {"state": "authenticated"}
        self.ui.page = "models"
        self.name = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
        self.path = "loras/" + self.name
        self.ui.model_entries = [
            {
                "path": self.path,
                "size_bytes": 1820000000,
                "manifest": "models/h3.json",
                "auto_download": True,
            }
        ]

    def draw(self, height=36, width=120):
        frame = Frame(height, width)
        with mock.patch.object(wizard.curses, "doupdate"):
            self.ui._draw(frame)
        return frame

    def test_wide_menu_truncates_with_ellipsis_and_right_shows_full_metadata(self):
        for width in (110, 120, 180):
            with self.subTest(width=width):
                frame = self.draw(width=width)
                divider = next(row.index("|") for row in frame.rows if "|" in row)
                menu = "\n".join("".join(row[:divider]) for row in frame.rows)
                self.assertIn("...", menu)
                self.assertNotIn(self.name, menu)
                right = "".join(
                    "".join(row[divider + 1 :]).strip() for row in frame.rows
                )
                self.assertIn("Name: " + self.name, right)
                self.assertIn(self.path, right)
                self.assertIn("Path:", right)
                self.assertIn("Auto-download: enabled", right)
                self.assertEqual(frame.text.count("ABOUT THIS MODEL"), 1)
        self.assertEqual(self.worker.submitted, [])

    def test_compact_metadata_is_complete_and_reachable_with_bottom_paging(self):
        for width in (50, 80, 100):
            with self.subTest(width=width):
                self.ui.detail_offset = 0
                pages = []
                for _ in range(25):
                    frame = self.draw(height=24, width=width)
                    pages.append(frame.text)
                    if self.ui.details_overflow:
                        self.assertIn("←/→ details", "".join(frame.rows[-2]))
                    self.ui._key("KEY_RIGHT")
                rows = self.ui._compact_details(width - 4)
                joined = "".join(value for value, _ in rows)
                self.assertIn("Name: " + self.name, joined)
                self.assertIn("Path: " + self.path, joined)
                for value, _ in rows:
                    if value:
                        self.assertTrue(any(value in page for page in pages), value)
        self.assertEqual(self.worker.submitted, [])

    def test_add_or_back_choice_does_not_keep_previous_model_metadata(self):
        for key in ("model_add_url", "back"):
            self.ui.selected = next(
                index
                for index, choice in enumerate(self.ui._choices())
                if choice.key == key
            )
            frame = self.draw()
            self.assertNotIn("ABOUT THIS MODEL", frame.text)
            self.assertNotIn("Path: " + self.path, frame.text)

    def test_configuration_field_labels_are_green_and_values_white(self):
        self.ui.page = "summary"
        self.ui.config.session = "fixture-runtime"
        self.ui.theme.roles.update(comfy=123, normal=456)
        labels = (
            "Runtime:",
            "Compute:",
            "Storage:",
            "Models:",
            "Parallel downloads:",
            "Browser:",
            "Drive directory:",
            "SSH key:",
        )
        for height, width in ((36, 120), (36, 80)):
            frame = self.draw(height, width)
            for label in labels:
                row = next(
                    index
                    for index, cells in enumerate(frame.rows)
                    if label in "".join(cells)
                )
                text = "".join(frame.rows[row])
                column = text.index(label)
                self.assertEqual(
                    frame.attributes[row][column : column + len(label)],
                    [123] * len(label),
                )
                value_column = column + len(label) + 1
                self.assertEqual(frame.attributes[row][value_column], 456)

    def test_wrapped_field_value_with_colon_does_not_gain_label_color(self):
        self.ui.page = "summary"
        self.ui.config.storage_root = "/content/drive/MyDrive/" + "a" * 60 + ":value"
        rows = self.ui._details(30)
        first = next(
            index
            for index, (value, _) in enumerate(rows)
            if value.startswith("Drive directory:")
        )
        self.assertEqual(rows[first][1], "field")
        for value, role in rows[first + 1 :]:
            if value.startswith("SSH key:"):
                break
            self.assertEqual(role, "normal")

    def test_confirmed_configuration_is_green_current_stage_orange_in_both_layouts(
        self,
    ):
        self.ui._page("home")
        for key in ("new", "gpu", "G4"):
            choice = next(choice for choice in self.ui._choices() if choice.key == key)
            self.ui._activate(choice)
        self.assertEqual(self.ui.page, "storage")
        self.assertEqual(self.ui._step_style("session"), ("+", "comfy"))
        self.assertEqual(self.ui._step_style("hardware"), ("+", "comfy"))
        self.assertEqual(self.ui._step_style("storage"), (">", "title"))
        # These are completed choices, not proof of an allocated VM.
        self.assertNotIn("session", self.ui._complete_steps())
        self.ui.theme.roles.update(comfy=123, title=456)
        for height, width in ((24, 80), (36, 120)):
            frame = self.draw(height, width)
            for marker in (
                "+ Colab",
                "+ VM" if width == 80 else "+ Session",
                "+ Compute",
            ):
                row = next(
                    index
                    for index, cells in enumerate(frame.rows)
                    if marker in "".join(cells)
                )
                column = "".join(frame.rows[row]).index(marker)
                self.assertEqual(frame.attributes[row][column], 123)
            current = "> Disk" if width == 80 else "> Storage"
            row = next(
                index
                for index, cells in enumerate(frame.rows)
                if current in "".join(cells)
            )
            self.assertEqual(
                frame.attributes[row]["".join(frame.rows[row]).index(current)], 456
            )

    def test_releasing_runtime_withdraws_confirmed_configuration_markers(self):
        self.ui.config.session = "fixture-runtime"
        self.ui.confirmed_steps = {"session", "hardware", "storage"}
        self.worker.events.put(("released", (self.ui.config, "")))
        self.ui._events()
        self.assertEqual(self.ui.confirmed_steps, set())
        self.assertEqual(self.ui.config.session, "")


if __name__ == "__main__":
    unittest.main()
