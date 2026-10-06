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
from typing import ClassVar
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
        info = dict(INFO)
        info["Loader"] = {
            "input": {"required": {"name": [["present.safetensors"]]}},
            "output": ["VIDEO"],
        }
        bad = json.loads(json.dumps(WORKFLOW))
        bad["2"] = {
            "class_type": "Loader",
            "inputs": {"name": "missing.safetensors"},
        }
        with self.assertRaisesRegex(renderer.RenderError, "unavailable model"):
            renderer.validate_workflow(bad, info)
        bad = json.loads(json.dumps(WORKFLOW))
        bad["1"]["inputs"]["video"] = ["2", 9]
        with self.assertRaisesRegex(renderer.RenderError, "invalid node output"):
            renderer.validate_workflow(bad, INFO)

    def test_disconnected_draft_input_is_allowed_but_reachable_input_is_required(self):
        workflow = json.loads(json.dumps(WORKFLOW))
        info = dict(INFO)
        info["DraftVideo"] = {
            "input": {"required": {"image": ["IMAGE"]}},
            "output": ["VIDEO"],
        }
        workflow["119"] = {"class_type": "DraftVideo", "inputs": {}}
        self.assertEqual(renderer.validate_workflow(workflow, info), ["1"])
        workflow["1"]["inputs"]["video"] = ["119", 0]
        with self.assertRaisesRegex(renderer.RenderError, "required workflow input"):
            renderer.validate_workflow(workflow, info)

    def test_disconnected_unavailable_or_malformed_node_still_fails(self):
        for node in ({"class_type": "Unavailable", "inputs": {}}, {"inputs": {}}):
            workflow = json.loads(json.dumps(WORKFLOW))
            workflow["119"] = node
            with self.subTest(node=node), self.assertRaises(renderer.RenderError):
                renderer.validate_workflow(workflow, INFO)

    def test_all_declared_outputs_require_valid_inputs_and_missing_links_fail(self):
        workflow = json.loads(json.dumps(WORKFLOW))
        info = dict(INFO)
        info["OtherOutput"] = {
            "input": {"required": {"image": ["IMAGE"]}},
            "output": [],
            "output_node": True,
        }
        workflow["3"] = {"class_type": "OtherOutput", "inputs": {}}
        with self.assertRaisesRegex(renderer.RenderError, "required workflow input"):
            renderer.validate_workflow(workflow, info)
        del workflow["3"]
        workflow["1"]["inputs"]["video"] = ["absent", 0]
        with self.assertRaisesRegex(renderer.RenderError, "invalid node output"):
            renderer.validate_workflow(workflow, info)

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

    def test_example_is_prepared_before_object_info_and_submission(self):
        workflow = json.loads(json.dumps(WORKFLOW))
        workflow["3"] = {
            "class_type": "LoadImage",
            "inputs": {"image": renderer.I2V_INPUT_NAME},
        }
        self.workflow_path.write_text(json.dumps(workflow))
        info = dict(INFO)
        info["LoadImage"] = {
            "input": {"required": {"image": [[renderer.I2V_INPUT_NAME]]}},
            "output": ["IMAGE", "MASK"],
        }
        calls = []

        def prepare(graph, output, deadline):
            calls.append("prepare")
            self.assertEqual(graph, workflow)
            self.assertEqual(output, self.output.resolve())
            deadline.remaining()
            return {"action": "reused", "filename": renderer.I2V_INPUT_NAME}

        def http(path, deadline, payload=None):
            calls.append(path)
            return (
                info if path == "/object_info" else self.http(path, deadline, payload)
            )

        with (
            mock.patch.object(renderer, "prepare_example_input", side_effect=prepare),
            mock.patch.object(renderer, "http_json", side_effect=http),
            mock.patch.object(
                renderer,
                "verify_api_bytes",
                return_value=(hashlib.sha256(DATA).hexdigest(), len(DATA)),
            ),
            mock.patch.object(
                renderer, "media_metadata", return_value={"video": [], "audio": []}
            ),
        ):
            status, result = self.main()
        self.assertEqual(status, 0)
        self.assertEqual(result["example_input"]["action"], "reused")
        self.assertLess(calls.index("prepare"), calls.index("/object_info"))
        self.assertLess(calls.index("/object_info"), calls.index("/prompt"))
        self.network.assert_not_called()

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


class PopularTemplateSchemaTests(unittest.TestCase):
    # Object-info metadata from pinned nodes_math.py and _io.Autogrow.TemplateNames.
    MATH_SCHEMA: ClassVar[dict] = {
        "input": {
            "required": {
                "expression": ["STRING"],
                "values": [
                    "COMFY_AUTOGROW_V3",
                    {
                        "template": {
                            "input": {
                                "required": {
                                    "value": ["FLOAT,INT,BOOLEAN", {"forceInput": True}]
                                }
                            },
                            "names": ["a", "b", "c"],
                            "min": 1,
                        }
                    },
                ],
            }
        },
        "output": ["FLOAT", "INT", "BOOLEAN"],
    }

    def graph(self):
        workflow = json.loads(json.dumps(WORKFLOW))
        workflow["105:111"] = {
            "class_type": "PrimitiveFloat",
            "inputs": {"value": 5.0},
        }
        workflow["105:107"] = {
            "class_type": "ComfyMathExpression",
            "inputs": {
                "expression": "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17",
                "values.a": ["105:111", 0],
            },
        }
        workflow["2"]["inputs"]["length"] = ["105:107", 1]
        info = dict(INFO)
        info["FixtureVideo"] = {
            "input": {"required": {"length": ["INT"]}},
            "output": ["VIDEO"],
        }
        info.update(
            ComfyMathExpression=self.MATH_SCHEMA,
            PrimitiveFloat={
                "input": {"required": {"value": ["FLOAT"]}},
                "output": ["FLOAT"],
            },
        )
        return workflow, info

    def test_official_math_dotted_inputs_and_subgraph_ids_are_valid(self):
        workflow, info = self.graph()
        self.assertEqual(renderer.validate_workflow(workflow, info), ["1"])
        workflow["105:107"]["inputs"]["values.b"] = 1.0
        self.assertEqual(renderer.validate_workflow(workflow, info), ["1"])

    def test_autogrow_required_first_child_and_declared_names_remain_strict(self):
        for updates in ({"values.b": 5.0}, {"values.a": 5.0, "values.unknown": 2}):
            workflow, info = self.graph()
            workflow["105:107"]["inputs"].pop("values.a")
            workflow["105:107"]["inputs"].update(updates)
            with self.subTest(updates=updates), self.assertRaises(renderer.RenderError):
                renderer.validate_workflow(workflow, info)

    def test_nested_autogrow_parent_is_not_an_api_child(self):
        workflow, info = self.graph()
        workflow["105:107"]["inputs"]["values"] = {"a": 5.0}
        with self.assertRaisesRegex(renderer.RenderError, "undeclared"):
            renderer.validate_workflow(workflow, info)

    def test_prefix_template_uses_bounded_numbered_children_without_parent(self):
        descriptor = [
            "COMFY_AUTOGROW_V3",
            {
                "template": {
                    "input": {"required": {"image": ["IMAGE"]}},
                    "prefix": "image_",
                    "min": 1,
                    "max": 2,
                }
            },
        ]
        result = renderer.autogrow_input_schema(descriptor, "images.")
        self.assertEqual(result["required"], {"images.image_0": ["IMAGE"]})
        self.assertEqual(result["optional"], {"images.image_1": ["IMAGE"]})
        descriptor[1]["template"]["max"] = 1000000
        with self.assertRaisesRegex(renderer.RenderError, "Autogrow"):
            renderer.autogrow_input_schema(descriptor, "images.")

    def test_official_lazy_switch_fields_need_no_dynamic_expansion(self):
        workflow, info = self.graph()
        info["ComfySwitchNode"] = {
            "input": {
                "required": {"switch": ["BOOLEAN"]},
                "optional": {"on_false": ["*"], "on_true": ["*"]},
            },
            "output": ["*"],
        }
        workflow["105:123"] = {
            "class_type": "ComfySwitchNode",
            "inputs": {"switch": False, "on_false": 20, "on_true": 8},
        }
        workflow["2"]["inputs"]["length"] = ["105:123", 0]
        self.assertEqual(renderer.validate_workflow(workflow, info), ["1"])
        workflow["105:123"]["inputs"]["on_true"] = ["105:107", 1]
        self.assertEqual(renderer.validate_workflow(workflow, info), ["1"])


class ExampleInputTests(unittest.TestCase):
    IMAGE = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
    )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="i2v-input-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "owned"
        self.output = self.root / "output"
        self.input = self.root / "input"
        self.output.mkdir(parents=True)
        self.input.mkdir()
        self.path = self.input / renderer.I2V_INPUT_NAME
        self.workflow = {
            "3": {
                "class_type": "LoadImage",
                "inputs": {"image": renderer.I2V_INPUT_NAME},
            }
        }
        for patch in (
            mock.patch.object(renderer, "I2V_INPUT_SIZE", len(self.IMAGE)),
            mock.patch.object(
                renderer, "I2V_INPUT_SHA256", hashlib.sha256(self.IMAGE).hexdigest()
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            renderer.urllib.request,
            "urlopen",
            side_effect=AssertionError("Network prohibited"),
        )
        self.network = patch.start()
        self.addCleanup(patch.stop)

    def prepare(self, deadline=None):
        return renderer.prepare_example_input(
            self.workflow, self.output, deadline or renderer.Deadline(2)
        )

    def response(self, data):
        self.network.side_effect = None
        self.network.return_value = io.BytesIO(data)

    def test_t2v_and_user_images_never_download_or_change_input_files(self):
        user_image = self.input / "my-image.png"
        user_image.write_bytes(b"user image")
        before = user_image.stat()
        for workflow in (
            WORKFLOW,
            {"3": {"class_type": "LoadImage", "inputs": {"image": "my-image.png"}}},
            {
                "3": {
                    "class_type": "OtherLoader",
                    "inputs": {"image": renderer.I2V_INPUT_NAME},
                }
            },
        ):
            with self.subTest(workflow=workflow):
                result = renderer.prepare_example_input(
                    workflow, self.output, renderer.Deadline(2)
                )
                self.assertIsNone(result)
        self.network.assert_not_called()
        self.assertEqual(user_image.stat(), before)
        self.assertEqual(list(self.input.iterdir()), [user_image])

    def test_pinned_download_is_verified_and_published_without_extra_files(self):
        self.response(self.IMAGE)
        result = self.prepare()
        self.assertEqual(self.path.read_bytes(), self.IMAGE)
        self.assertEqual(result["action"], "downloaded")
        self.assertEqual(result["sha256"], hashlib.sha256(self.IMAGE).hexdigest())
        self.assertEqual(result["bytes"], len(self.IMAGE))
        self.assertEqual(list(self.input.iterdir()), [self.path])
        request = self.network.call_args.args[0]
        self.assertEqual(request.full_url, renderer.I2V_INPUT_URL)
        self.assertLessEqual(self.network.call_args.kwargs["timeout"], 2)

    def test_valid_readonly_existing_image_is_reused_without_modification(self):
        self.path.write_bytes(self.IMAGE)
        self.path.chmod(0o400)
        before = self.path.stat()
        result = self.prepare()
        self.assertEqual(result["action"], "reused")
        after = self.path.stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(after.st_mode, before.st_mode)
        self.network.assert_not_called()

    def test_invalid_existing_image_is_never_overwritten_or_redownloaded(self):
        for data in (b"different size", b"x" * len(self.IMAGE)):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                before = self.path.stat()
                with self.assertRaises(renderer.RenderError):
                    self.prepare()
                self.assertEqual(self.path.read_bytes(), data)
                self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.network.assert_not_called()

    def test_symlinked_directory_or_fixture_never_reads_or_writes_outside(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        user_image = outside / renderer.I2V_INPUT_NAME
        user_image.write_bytes(self.IMAGE)
        self.path.symlink_to(user_image)
        with self.assertRaisesRegex(renderer.RenderError, "symlink"):
            self.prepare()
        self.path.unlink()
        self.input.rmdir()
        self.input.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(renderer.RenderError, "symlink"):
            self.prepare()
        self.assertEqual(user_image.read_bytes(), self.IMAGE)
        self.network.assert_not_called()

    def test_truncated_oversized_and_wrong_hash_downloads_leave_no_input(self):
        for data in (self.IMAGE[:-1], self.IMAGE + b"x", b"x" * len(self.IMAGE)):
            with self.subTest(data=data):
                self.response(data)
                with self.assertRaises(renderer.RenderError):
                    self.prepare()
                self.assertEqual(list(self.input.iterdir()), [])

    def test_expired_deadline_never_starts_the_download(self):
        deadline = mock.Mock()
        deadline.remaining.side_effect = renderer.RenderError("deadline exceeded")
        with self.assertRaisesRegex(renderer.RenderError, "deadline"):
            self.prepare(deadline)
        self.assertEqual(list(self.input.iterdir()), [])
        self.network.assert_not_called()

    def test_mid_stream_deadline_discards_partial_image(self):
        class InterruptedImage(io.BytesIO):
            def read(self, _size):
                chunk = super().read(4)
                if self.tell() > 4:
                    raise renderer.RenderError("deadline exceeded")
                return chunk

        self.network.side_effect = None
        self.network.return_value = InterruptedImage(self.IMAGE)
        with self.assertRaisesRegex(renderer.RenderError, "deadline"):
            self.prepare()
        self.assertEqual(list(self.input.iterdir()), [])

    def test_concurrent_existing_destination_is_verified_not_overwritten(self):
        self.response(self.IMAGE)
        real_open = renderer.os.open

        def reserve(path, flags, *args, **kwargs):
            if Path(path) == self.path and flags & renderer.os.O_EXCL:
                self.path.write_bytes(b"someone else's image")
            return real_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(renderer.os, "open", side_effect=reserve),
            self.assertRaises(renderer.RenderError),
        ):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), b"someone else's image")
        self.assertEqual(list(self.input.iterdir()), [self.path])

    def test_publication_failure_cleans_only_its_own_reserved_file(self):
        self.response(self.IMAGE)
        with (
            mock.patch.object(renderer.os, "replace", side_effect=OSError("failure")),
            self.assertRaises(renderer.RenderError),
        ):
            self.prepare()
        self.assertEqual(list(self.input.iterdir()), [])


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
