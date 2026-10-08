"""Transfer bars keep their visual style without confusing bytes with verification."""

import unittest

from test_wizard import LocalWorker, dashboard, wizard


class ProgressStyleTests(unittest.TestCase):
    def setUp(self):
        self.ui = wizard.Wizard(dashboard.Config(), object(), LocalWorker())
        self.ui.theme.unicode = True
        self.files = [
            {
                "path": "loras/running.safetensors",
                "done_bytes": 43,
                "total_bytes": 100,
                "phase": "copy",
                "rate_bytes_per_second": 72,
            },
            {
                "path": "vae/pending.safetensors",
                "done_bytes": 100,
                "total_bytes": 100,
                "phase": "flush",
            },
            {
                "path": "vae/verified.safetensors",
                "done_bytes": 100,
                "total_bytes": 100,
                "phase": "verified",
                "verification": "stream_sha256",
            },
        ]
        self.ui.status = {
            "model_prepare": {"status": "running", "progress": {"files": self.files}}
        }

    def test_solid_and_shaded_bars_only_turn_green_after_verification(self):
        rows = self.ui._model_rows(70)
        self.assertEqual(rows[0][1], "info_heading")
        running = next((text, role) for text, role in rows if "43%" in text)
        pending = next((text, role) for text, role in rows if "100%" in text)
        verified = next(
            (text, role)
            for text, role in rows
            if "100%" in text and role == "progress_complete"
        )
        self.assertIn("█", running[0])
        self.assertIn("░", running[0])
        self.assertIn("72.0B/s", running[0])
        self.assertEqual(running[1], "title")
        self.assertEqual(pending[1], "title")
        self.assertIn("█", verified[0])
        self.assertNotIn("░", verified[0])
        self.assertIn(("final validation pending", "warn"), rows)
        self.assertIn(("new streamed SHA256 verified", "progress_complete"), rows)

    def test_compact_bars_keep_all_files_and_receipt_reuse_has_distinct_text(self):
        self.files[-1]["verification"] = "verified_receipt_metadata"
        rows = self.ui._compact_model_rows(76)
        self.assertEqual(len(rows), 4)
        self.assertIn("█", rows[1][0])
        self.assertIn("░", rows[1][0])
        self.assertEqual(rows[1][1], "title")
        self.assertIn("validation pending", rows[2][0])
        self.assertIn("receipt reuse", rows[3][0])
        self.assertEqual(rows[3][1], "progress_complete")

    def test_ascii_fallback_and_unknown_total_do_not_invent_completion(self):
        self.ui.theme.unicode = False
        self.files[0]["total_bytes"] = None
        for rows in (self.ui._model_rows(70), self.ui._compact_model_rows(76)):
            unknown = next((text, role) for text, role in rows if "?%" in text)
            self.assertIn("-", unknown[0])
            self.assertNotIn("█", unknown[0])
            self.assertNotEqual(unknown[1], "progress_complete")


if __name__ == "__main__":
    unittest.main()
