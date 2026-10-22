"""Optional local PyAV/ffmpeg fixtures; no Colab, model or external media."""

import hashlib
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_render_workflow import renderer

HAS_MEDIA_TOOLS = (
    importlib.util.find_spec("av") is not None and shutil.which("ffmpeg") is not None
)


@unittest.skipUnless(
    HAS_MEDIA_TOOLS, "Optional PyAV + ffmpeg are needed for local codec integration"
)
class RealMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-decoder-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.video = self.root / "fixture.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=96x64:rate=12",
                "-frames:v",
                "7",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(self.video),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )

    def test_decode_counts_actual_frames_and_hashes_optional_first_last_png(self):
        requested = {
            "width": 96,
            "height": 64,
            "fps": 12,
            "frames": 8,
            "duration": 8 / 12,
        }
        result = renderer.media_metadata(
            self.video,
            renderer.Deadline(10),
            requested,
            export_frames=True,
            output_root=self.root,
        )
        video = result["video"][0]
        self.assertEqual((video["width"], video["height"]), (96, 64))
        self.assertEqual(video["fps"], 12)
        self.assertEqual(video["frame_count"], 7)
        self.assertAlmostEqual(video["duration_seconds"], 7 / 12, places=5)
        self.assertFalse(video["matches_requested"])
        self.assertEqual(result["audio"], [])
        self.assertEqual(
            [frame["position"] for frame in result["frames"]], ["first", "last"]
        )
        for frame in result["frames"]:
            data = (self.root / frame["relative_path"]).read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(hashlib.sha256(data).hexdigest(), frame["sha256"])
            self.assertEqual(len(data), frame["bytes"])

    def test_default_media_inspection_creates_no_preview_files(self):
        before = set(self.root.iterdir())
        result = renderer.media_metadata(self.video, renderer.Deadline(10))
        self.assertEqual(result["video"][0]["frame_count"], 7)
        self.assertNotIn("frames", result)
        self.assertEqual(set(self.root.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
