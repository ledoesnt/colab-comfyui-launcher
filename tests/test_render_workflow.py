"""Exercise failed jobs, stale ownership, output boundaries, and media validation."""

import argparse
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "render_workflow", ROOT / "scripts/render_workflow.py"
)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
DATA = b"fixture container bytes, not generated media"
WORKFLOW = {
    "1": {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["2", 0],
            "filename_prefix": "video/test",
            "format": "mp4",
            "format.codec": "h264",
        },
    },
    "2": {"class_type": "FixtureVideo", "inputs": {}},
}
# Necessary object_info shape from ComfyUI f1072eb nodes_video.py + _io.py.
# V3 API fields are flattened; execute receives the rebuilt nested dictionary.
CODEC_DESCRIPTOR = [
    "COMFY_DYNAMICCOMBO_V3",
    {
        "options": [
            {"key": "auto", "inputs": {"required": {}}},
            {
                "key": "h264",
                "inputs": {
                    "required": {},
                    "optional": {
                        "encoding": [
                            "COMFY_DYNAMICCOMBO_V3",
                            {
                                "options": [
                                    {"key": "auto", "inputs": {"required": {}}},
                                    {
                                        "key": "re-encode",
                                        "inputs": {
                                            "required": {
                                                "crf": ["FLOAT", {"default": 18.0}]
                                            }
                                        },
                                    },
                                ]
                            },
                        ],
                    },
                },
            },
        ]
    },
]
INFO = {
    "SaveVideo": {
        "input": {
            "required": {
                "video": ["VIDEO"],
                "filename_prefix": ["STRING"],
                "format": [
                    "COMFY_DYNAMICCOMBO_V3",
                    {
                        "options": [
                            {
                                "key": "mp4",
                                "inputs": {"required": {"codec": CODEC_DESCRIPTOR}},
                            },
                            {
                                "key": "webm",
                                "inputs": {
                                    "required": {
                                        "codec": [
                                            "COMFY_DYNAMICCOMBO_V3",
                                            {
                                                "options": [
                                                    {
                                                        "key": "auto",
                                                        "inputs": {"required": {}},
                                                    },
                                                ]
                                            },
                                        ]
                                    }
                                },
                            },
                        ]
                    },
                ],
            },
            "optional": {"codec": CODEC_DESCRIPTOR},
        },
        "output": ["VIDEO"],
    },
    "FixtureVideo": {"input": {"required": {}}, "output": ["VIDEO"]},
}


class RenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workflow-render-test-")
        self.addCleanup(temporary.cleanup)
        self.content = Path(temporary.name)
        self.root = self.content / "runtime-assets"
        self.output = self.root / "output"
        (self.output / "video").mkdir(parents=True)
        self.path = self.output / "video" / "test.mp4"
        self.path.write_bytes(DATA)
        self.workflow_path = self.content / "workflow.json"
        self.workflow_path.write_text(json.dumps(WORKFLOW))
        self.result_path = self.content / "render-result.json"
        self.args = argparse.Namespace(
            workflow=self.workflow_path, max_seconds=5.0, result_file=self.result_path
        )
        self.state = {
            "comfyui": {"pid": 12345, "start_ticks": "owned"},
            "storage_root": str(self.root),
            "ephemeral": True,
        }
        self.history = {
            "status": {"completed": True, "status_str": "success", "messages": []},
            "outputs": {
                "1": {
                    "images": [
                        {"filename": "test.mp4", "subfolder": "video", "type": "output"}
                    ]
                }
            },
        }
        patches = [
            mock.patch.object(renderer, "CONTENT_ROOT", self.content),
            mock.patch.object(renderer, "DRIVE_MOUNT", self.content / "drive"),
            mock.patch.object(renderer.runtime, "read_json", return_value=self.state),
            mock.patch.object(renderer.runtime, "alive", return_value=True),
            mock.patch.object(
                renderer.urllib.request,
                "urlopen",
                side_effect=AssertionError("Network prohibited"),
            ),
        ]
        for patch in patches:
            value = patch.start()
            self.addCleanup(patch.stop)
            if patch.attribute == "alive":
                self.alive = value
            if patch.attribute == "urlopen":
                self.network = value

    def http(self, path, deadline, payload=None):
        if path == "/system_stats":
            return {"system": {}}
        if path == "/object_info":
            return INFO
        if path == "/prompt":
            return {"prompt_id": "fixture-job", "node_errors": {}}
        if path == "/history/fixture-job":
            return {"fixture-job": self.history}
        if path == "/queue":
            return {"queue_running": [], "queue_pending": []}
        self.fail(f"Unexpected HTTP path: {path}")

    def main(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            status = renderer.main(
                [
                    "--workflow",
                    str(self.workflow_path),
                    "--max-seconds",
                    "5",
                    "--result-file",
                    str(self.result_path),
                ]
            )
        return status, json.loads(output.getvalue())

    def test_stale_service_never_submits_to_a_healthy_foreign_server(self):
        self.alive.return_value = False
        with mock.patch.object(renderer, "http_json") as http:
            status, result = self.main()
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        http.assert_not_called()
        self.assertEqual(json.loads(self.result_path.read_text())["status"], "failed")

    def test_server_execution_error_is_not_success_even_with_cli_exit_zero(self):
        self.history["status"] = {
            "completed": False,
            "status_str": "error",
            "messages": [["execution_error", {"exception_message": "PRIVATE PROMPT"}]],
        }
        with mock.patch.object(renderer, "http_json", side_effect=self.http):
            status, result = self.main()
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertNotIn("PRIVATE PROMPT", json.dumps(result))

    def test_completed_history_still_requires_a_real_saved_video(self):
        self.history["outputs"] = {}
        with mock.patch.object(renderer, "http_json", side_effect=self.http):
            status, result = self.main()
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["job_may_still_be_running"])

    def test_missing_core_node_and_required_input_fail_before_submission(self):
        for workflow in (
            {"1": {"class_type": "Unavailable", "inputs": {}}},
            {"1": {"class_type": "SaveVideo", "inputs": {}}},
        ):
            with (
                self.subTest(workflow=workflow),
                self.assertRaises(renderer.RenderError),
            ):
                renderer.validate_workflow(workflow, INFO)

    def test_model_combo_and_invalid_link_are_rejected(self):
        info = {
            "Loader": {
                "input": {"required": {"name": [["present.safetensors"]]}},
                "output": ["VIDEO"],
            }
        }
        with self.assertRaisesRegex(renderer.RenderError, "unavailable model"):
            renderer.validate_workflow(
                {
                    "1": {
                        "class_type": "Loader",
                        "inputs": {"name": "missing.safetensors"},
                    }
                },
                info,
            )
        bad = json.loads(json.dumps(WORKFLOW))
        bad["1"]["inputs"]["video"] = ["2", 9]
        with self.assertRaisesRegex(renderer.RenderError, "invalid node output"):
            renderer.validate_workflow(bad, INFO)

    def test_savevideo_dot_fields_match_selected_official_dynamic_schema(self):
        self.assertEqual(renderer.validate_workflow(WORKFLOW, INFO), ["1"])

    def test_nested_dynamic_object_is_rejected_before_expensive_submission(self):
        bad = json.loads(json.dumps(WORKFLOW))
        bad["1"]["inputs"].pop("format.codec")
        bad["1"]["inputs"]["format"] = {"format": "mp4", "codec": {"codec": "h264"}}
        with self.assertRaisesRegex(renderer.RenderError, "dot-separated"):
            renderer.validate_workflow(bad, INFO)

    def test_dynamic_children_are_required_and_only_selected_branch_is_allowed(self):
        for updates in (
            {"format": "webm"},
            {"format.codec": "bogus"},
            {"format.undeclared": "h264"},
        ):
            bad = json.loads(json.dumps(WORKFLOW))
            bad["1"]["inputs"].update(updates)
            with self.subTest(updates=updates), self.assertRaises(renderer.RenderError):
                renderer.validate_workflow(bad, INFO)
        bad = json.loads(json.dumps(WORKFLOW))
        bad["1"]["inputs"].pop("format.codec")
        with self.assertRaisesRegex(renderer.RenderError, "required workflow input"):
            renderer.validate_workflow(bad, INFO)

    def test_recursive_dynamic_encoding_requires_declared_crf(self):
        workflow = json.loads(json.dumps(WORKFLOW))
        workflow["1"]["inputs"]["format.codec.encoding"] = "re-encode"
        with self.assertRaisesRegex(renderer.RenderError, "required workflow input"):
            renderer.validate_workflow(workflow, INFO)
        workflow["1"]["inputs"]["format.codec.encoding.crf"] = 18.0
        self.assertEqual(renderer.validate_workflow(workflow, INFO), ["1"])

    def test_output_path_traversal_and_symlink_escape_are_rejected(self):
        for fields in (
            {"filename": "../test.mp4"},
            {"subfolder": "../outside"},
            {"type": "input"},
        ):
            with self.subTest(fields=fields):
                history = json.loads(json.dumps(self.history))
                history["outputs"]["1"]["images"][0].update(fields)
                with self.assertRaises(renderer.RenderError):
                    renderer.output_assets(history, ["1"], self.output)
        outside = self.content / "outside.mp4"
        outside.write_bytes(DATA)
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(renderer.RenderError):
            renderer.output_assets(self.history, ["1"], self.output)

    def test_api_bytes_must_match_persisted_asset(self):
        self.network.side_effect = None
        self.network.return_value = io.BytesIO(b"different")
        with self.assertRaisesRegex(renderer.RenderError, "do not match"):
            renderer.verify_api_bytes(
                self.path, {"filename": "test.mp4"}, renderer.Deadline(1)
            )

    def test_success_requires_byte_and_media_verification_and_persists_summary(self):
        media = {
            "video": [{"width": 864, "height": 480, "first_frame_decoded": True}],
            "audio": [
                {"channels": 2, "sample_rate": 32000, "first_frame_decoded": True}
            ],
        }
        self.network.side_effect = None
        self.network.return_value = io.BytesIO(DATA)
        with (
            mock.patch.object(renderer, "http_json", side_effect=self.http),
            mock.patch.object(
                renderer, "media_metadata", return_value=media
            ) as inspect,
        ):
            status, result = self.main()
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        asset = result["assets"][0]
        self.assertEqual(asset["relative_path"], "video/test.mp4")
        self.assertEqual(asset["sha256"], hashlib.sha256(DATA).hexdigest())
        self.assertEqual(result, json.loads(self.result_path.read_text()))
        inspect.assert_called_once()
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_polling_progress_has_queue_phase_then_verified_completion(self):
        reports = []
        report = renderer.Report(self.result_path)
        real_update = report.update

        def update(**fields):
            real_update(**fields)
            reports.append(report.value.copy())

        histories = iter([{}, {"fixture-job": self.history}])

        def http(path, deadline, payload=None):
            if path.startswith("/history"):
                return next(histories)
            if path == "/queue":
                return {"queue_running": [[0, "fixture-job"]], "queue_pending": []}
            return self.http(path, deadline, payload)

        with (
            mock.patch.object(renderer, "http_json", side_effect=http),
            mock.patch.object(renderer.time, "sleep"),
            mock.patch.object(
                renderer,
                "verify_api_bytes",
                return_value=(hashlib.sha256(DATA).hexdigest(), len(DATA)),
            ),
            mock.patch.object(
                renderer, "media_metadata", return_value={"video": [], "audio": []}
            ),
            mock.patch.object(report, "update", side_effect=update),
        ):
            renderer.render(self.args, report)
        self.assertIn("running", [item["status"] for item in reports])
        self.assertEqual(reports[-1]["status"], "succeeded")

    def test_timeout_is_failed_and_flags_server_job_for_caller_cleanup(self):
        def http(path, deadline, payload=None):
            if path.startswith("/history"):
                raise renderer.RenderError("Overall render deadline exceeded")
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http):
            status, result = self.main()
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["job_may_still_be_running"])


class MediaValidationTests(unittest.TestCase):
    def container(self, audio=True, dimensions=(864, 480), decode=True):
        video = SimpleNamespace(
            type="video",
            index=0,
            duration=124,
            time_base=1 / 24,
            average_rate=24,
            frames=124,
            codec_context=SimpleNamespace(name="h264"),
        )
        sound = SimpleNamespace(
            type="audio",
            index=1,
            duration=32000 * 5,
            time_base=1 / 32000,
            codec_context=SimpleNamespace(name="aac"),
        )
        streams = [video, sound] if audio else [video]
        frame = SimpleNamespace(
            width=dimensions[0],
            height=dimensions[1],
            sample_rate=32000,
            samples=1024,
            layout=SimpleNamespace(channels=(1, 2), name="stereo"),
        )
        container = mock.MagicMock()
        container.__enter__.return_value = container
        container.streams = streams
        container.duration = 5166667
        container.decode.side_effect = lambda stream: iter([frame] if decode else [])
        return container

    def test_no_audio_track_or_undecodable_track_is_a_failure(self):
        for fields in ({"audio": False}, {"decode": False}):
            with self.subTest(fields=fields):
                av = SimpleNamespace(
                    open=mock.Mock(
                        side_effect=lambda path, fields=fields: self.container(**fields)
                    )
                )
                with (
                    mock.patch.dict("sys.modules", {"av": av}),
                    self.assertRaises(renderer.RenderError),
                ):
                    renderer.media_metadata(Path("fixture.mp4"), renderer.Deadline(1))

    def test_expected_workflow_dimensions_and_duration_are_checked(self):
        expected = {"width": 864, "height": 480, "duration": 124 / 24, "fps": 24}
        av = SimpleNamespace(
            open=mock.Mock(side_effect=lambda path: self.container(dimensions=(64, 64)))
        )
        with (
            mock.patch.dict("sys.modules", {"av": av}),
            self.assertRaisesRegex(renderer.RenderError, "differs"),
        ):
            renderer.media_metadata(Path("fixture.mp4"), renderer.Deadline(1), expected)

    def test_first_frame_of_each_track_is_decoded(self):
        av = SimpleNamespace(open=mock.Mock(side_effect=lambda path: self.container()))
        with mock.patch.dict("sys.modules", {"av": av}):
            result = renderer.media_metadata(Path("fixture.mp4"), renderer.Deadline(1))
        self.assertEqual(result["video"][0]["width"], 864)
        self.assertEqual(result["audio"][0]["channels"], 2)
        self.assertTrue(result["audio"][0]["first_frame_decoded"])
        self.assertEqual(av.open.call_count, 3)


if __name__ == "__main__":
    unittest.main()
