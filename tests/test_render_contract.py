"""Durable render identity, lost replies, generic nodes, mappings and real media."""

import argparse
import copy
import hashlib
import http.server
import io
import json
import multiprocessing
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import test_render_workflow as fixtures
from test_render_workflow import DATA, INFO, ROOT, WORKFLOW, renderer

REAL_URLOPEN = urllib.request.urlopen


class RequestRecoveryTests(unittest.TestCase):
    setUp = fixtures.RenderTests.setUp
    http = fixtures.RenderTests.http

    def execute(self, request_id="durable-request", **extra):
        args = argparse.Namespace(
            workflow=self.workflow_path,
            max_seconds=3.0,
            result_file=self.result_path,
            request_id=request_id,
            assignment_id="allocation-one",
            session="fixture-session",
            job_dir=self.content / "jobs",
            param=[],
            **extra,
        )
        report = renderer.Report(self.result_path)
        with (
            mock.patch.object(
                renderer,
                "verify_api_bytes",
                return_value=(hashlib.sha256(DATA).hexdigest(), len(DATA)),
            ),
            mock.patch.object(
                renderer, "media_metadata", return_value={"video": [], "audio": []}
            ),
        ):
            return renderer.render(args, report)

    def record(self, request_id="durable-request"):
        return json.loads((self.content / "jobs" / f"{request_id}.json").read_text())

    def test_intent_is_durable_before_post_and_success_replays_without_requests(self):
        def http(path, deadline, payload=None):
            if path == "/prompt":
                record = self.record()
                self.assertTrue(record["submission_attempted"])
                self.assertEqual(record["status"], "submitting")
                self.assertEqual(record["submission_prompt_id"], payload["prompt_id"])
                self.assertEqual(
                    payload["extra_data"]["launcher_request_id"], "durable-request"
                )
                self.assertEqual(
                    (self.content / "jobs/durable-request.json").stat().st_mode & 0o777,
                    0o600,
                )
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http) as calls:
            result = self.execute()
            self.assertEqual(
                sum(call.args[0] == "/prompt" for call in calls.call_args_list), 1
            )
        with mock.patch.object(renderer, "http_json") as calls:
            replay = self.execute()
        calls.assert_not_called()
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["job_id"], "durable-request")
        self.assertEqual(result["prompt_id"], "fixture-job")
        self.assertEqual(result["assets"][0]["assignment_id"], "allocation-one")

    def test_unknown_post_is_not_retried_even_after_restart_and_empty_history(self):
        posts = []

        def http(path, deadline, payload=None):
            if path == "/prompt":
                posts.append(payload)
                raise renderer.RenderError("Connection closed after request")
            if path.startswith("/history"):
                return {}
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http):
            for _ in range(2):
                with self.assertRaises(renderer.RenderError):
                    self.execute()
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.record()["status"], "submission_unknown")
        self.assertFalse(self.record()["resubmit_allowed"])
        self.assertTrue(self.record()["job_may_still_be_running"])

    def test_process_death_after_server_acceptance_recovers_without_second_post(self):
        accepted = self.content / "accepted-fixture.json"

        def crash_http(path, deadline, payload=None):
            if path == "/prompt":
                accepted.write_text(json.dumps(payload))
                os._exit(23)  # Simulate loss of the whole runner, bypassing finally.
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=crash_http):
            process = multiprocessing.get_context("fork").Process(target=self.execute)
            process.start()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
        self.assertEqual(process.exitcode, 23)
        self.assertEqual(self.record()["status"], "submitting")
        payload = json.loads(accepted.read_text())
        history = copy.deepcopy(self.history)
        history["prompt"] = [
            0,
            payload["prompt_id"],
            payload["prompt"],
            payload["extra_data"],
            ["1"],
        ]

        def recover_http(path, deadline, payload=None):
            self.assertNotEqual(path, "/prompt")
            if path.startswith("/history"):
                return {history["prompt"][1]: history}
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=recover_http):
            result = self.execute()
        self.assertTrue(result["ok"])
        self.assertTrue(result["reconciled"])

    def test_lost_reply_reconciles_retained_history_and_never_posts_twice(self):
        posted = {}
        unavailable = [True]

        def http(path, deadline, payload=None):
            if path == "/prompt":
                posted.update(payload)
                raise renderer.RenderError("Reply lost")
            if path.startswith("/history"):
                if unavailable[0]:
                    return {}
                history = copy.deepcopy(self.history)
                history["prompt"] = [
                    0,
                    posted["prompt_id"],
                    posted["prompt"],
                    posted["extra_data"],
                    ["1"],
                ]
                return {posted["prompt_id"]: history}
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http) as calls:
            with self.assertRaises(renderer.RenderError):
                self.execute()
            unavailable[0] = False
            result = self.execute()
        self.assertTrue(result["ok"])
        self.assertTrue(result["reconciled"])
        self.assertEqual(
            sum(call.args[0] == "/prompt" for call in calls.call_args_list), 1
        )

    def test_query_only_recovers_completed_job_without_original_workflow(self):
        posted = {}
        ready = [False]

        def http(path, deadline, payload=None):
            if path == "/prompt":
                posted.update(payload)
                raise renderer.RenderError("Reply lost")
            if path.startswith("/history"):
                if not ready[0]:
                    return {}
                history = copy.deepcopy(self.history)
                history["prompt"] = [
                    0,
                    posted["prompt_id"],
                    posted["prompt"],
                    posted["extra_data"],
                    ["1"],
                ]
                return {posted["prompt_id"]: history}
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http) as calls:
            with self.assertRaises(renderer.RenderError):
                self.execute()
            self.workflow_path.unlink()
            ready[0] = True
            with (
                mock.patch.object(
                    renderer,
                    "verify_api_bytes",
                    return_value=(hashlib.sha256(DATA).hexdigest(), len(DATA)),
                ),
                mock.patch.object(
                    renderer, "media_metadata", return_value={"video": [], "audio": []}
                ),
            ):
                result = renderer.query_job("durable-request", self.content / "jobs", 2)
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["ok"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            sum(call.args[0] == "/prompt" for call in calls.call_args_list), 1
        )

    def test_query_of_active_runner_returns_record_without_network(self):
        directory = self.content / "jobs"
        with renderer.RequestJournal(directory, "durable-request") as journal:
            renderer.durable_json(
                journal.path, {"status": "running", "submission_attempted": True}
            )
            with mock.patch.object(renderer, "http_json") as calls:
                result = renderer.query_job("durable-request", directory)
        calls.assert_not_called()
        self.assertEqual(result["query_state"], "runner_active")

    def test_failed_query_does_not_clear_unknown_submission(self):
        directory = self.content / "jobs"
        with renderer.RequestJournal(directory, "durable-request") as journal:
            original = {
                "status": "submission_unknown",
                "submission_attempted": True,
                "request_id": "durable-request",
                "request_fingerprint": "hash",
                "submission_prompt_id": "id",
            }
            renderer.durable_json(journal.path, original)
        with mock.patch.object(
            renderer, "http_json", side_effect=renderer.RenderError("offline")
        ):
            result = renderer.query_job("durable-request", directory)
        self.assertFalse(result["ok"])
        self.assertEqual(result["query_state"], "unavailable")
        self.assertEqual(result["result"], original)
        self.assertEqual(json.loads(journal.path.read_text()), original)

    def test_queue_reconciliation_requires_full_request_fingerprint(self):
        record = {
            "request_id": "id",
            "request_fingerprint": "hash",
            "submission_prompt_id": "known",
        }
        good = [
            0,
            "queued",
            {},
            {"launcher_request_id": "id", "launcher_fingerprint": "hash"},
        ]
        bad = [
            0,
            "other",
            {},
            {"launcher_request_id": "id", "launcher_fingerprint": "different"},
        ]
        with mock.patch.object(
            renderer, "http_json", side_effect=[{}, {"queue_pending": [bad, good]}]
        ):
            self.assertEqual(
                renderer.reconcile_submission(record, renderer.Deadline(1)), "queued"
            )
        with (
            mock.patch.object(
                renderer,
                "http_json",
                side_effect=[
                    {},
                    {"queue_running": [good, [0, "duplicate", {}, good[3]]]},
                ],
            ),
            self.assertRaisesRegex(renderer.RenderError, "Multiple"),
        ):
            renderer.reconcile_submission(record, renderer.Deadline(1))

    def test_same_id_different_workflow_is_rejected_without_altering_record(self):
        with mock.patch.object(renderer, "http_json", side_effect=self.http):
            self.execute()
        before = (self.content / "jobs/durable-request.json").read_bytes()
        graph = copy.deepcopy(WORKFLOW)
        graph["1"]["inputs"]["filename_prefix"] = "changed"
        self.workflow_path.write_text(json.dumps(graph))
        with (
            mock.patch.object(renderer, "http_json") as calls,
            self.assertRaisesRegex(renderer.RenderError, "different workflow"),
        ):
            self.execute()
        calls.assert_not_called()
        self.assertEqual(
            (self.content / "jobs/durable-request.json").read_bytes(), before
        )

    def test_locked_or_corrupt_journal_blocks_all_posts(self):
        with renderer.RequestJournal(self.content / "jobs", "durable-request"):
            with (
                mock.patch.object(renderer, "http_json") as calls,
                self.assertRaisesRegex(renderer.RenderError, "active runner"),
            ):
                self.execute()
            calls.assert_not_called()
        (self.content / "jobs/durable-request.json").write_text("truncated{")
        with (
            mock.patch.object(renderer, "http_json") as calls,
            self.assertRaisesRegex(renderer.RenderError, "unreadable"),
        ):
            self.execute()
        calls.assert_not_called()

    def test_failed_intent_write_cannot_reach_post(self):
        real_write = renderer.durable_json

        def write(path, value):
            if value.get("status") == "submitting":
                raise OSError("disk full")
            return real_write(path, value)

        with (
            mock.patch.object(renderer, "http_json", side_effect=self.http) as calls,
            mock.patch.object(renderer, "durable_json", side_effect=write),
            self.assertRaises(OSError),
        ):
            self.execute()
        self.assertFalse(
            any(call.args[0] == "/prompt" for call in calls.call_args_list)
        )

    def test_accepted_prompt_with_warnings_recovers_without_second_post(self):
        node_errors = {
            "unused-output": {
                "errors": [
                    {
                        "type": "value_not_in_list",
                        "extra_info": {"received_value": "PRIVATE-FIXTURE"},
                    }
                ]
            }
        }
        error = {"type": "partial_output_warning"}
        for index, diagnostics in enumerate(
            (
                {"node_errors": node_errors},
                {"error": error},
                {"node_errors": node_errors, "error": error},
            )
        ):
            with self.subTest(diagnostics=list(diagnostics)):
                request_id = f"accepted-warning-{index}"
                prompt_id = "fixture-job" if index < 2 else "a" * 128
                submitted = {
                    "prompt_id": prompt_id,
                    **diagnostics,
                    "prompt": {"text": "PRIVATE-FIXTURE"},
                    "authorization": "Bearer PRIVATE-FIXTURE",
                }
                unavailable = [True]

                def http(
                    path,
                    deadline,
                    payload=None,
                    *,
                    submitted=submitted,
                    prompt_id=prompt_id,
                    unavailable=unavailable,
                    request_id=request_id,
                ):
                    if path == "/prompt":
                        return submitted
                    if path == f"/history/{prompt_id}":
                        if unavailable[0]:
                            queued = self.record(request_id)
                            self.assertEqual(queued["status"], "queued")
                            self.assertEqual(queued["prompt_id"], prompt_id)
                            self.assertEqual(
                                queued["submission_warnings"],
                                renderer.safe_server_error(submitted),
                            )
                            raise renderer.RenderError(
                                "History temporarily unavailable"
                            )
                        return {prompt_id: self.history}
                    return self.http(path, deadline, payload)

                with mock.patch.object(
                    renderer, "http_json", side_effect=http
                ) as calls:
                    with self.assertRaisesRegex(renderer.RenderError, "History"):
                        self.execute(request_id)
                    record = self.record(request_id)
                    self.assertEqual(record["prompt_id"], prompt_id)
                    self.assertFalse(record.get("terminal", False))
                    self.assertTrue(record["job_may_still_be_running"])
                    self.assertFalse(record["resubmit_allowed"])
                    self.assertNotIn("PRIVATE-FIXTURE", json.dumps(record))
                    unavailable[0] = False
                    result = self.execute(request_id)
                self.assertTrue(result["ok"])
                self.assertEqual(result["prompt_id"], prompt_id)
                self.assertEqual(
                    result["submission_warnings"],
                    renderer.safe_server_error(submitted),
                )
                self.assertEqual(
                    sum(call.args[0] == "/prompt" for call in calls.call_args_list), 1
                )

    def test_node_errors_without_valid_prompt_id_remain_unknown(self):
        for index, prompt_id in enumerate(
            (None, "", "../unsafe", 123, [], {}, "a" * 129, "unsafe\n")
        ):
            with self.subTest(prompt_id=prompt_id):
                request_id = f"unknown-node-errors-{index}"
                submitted = {
                    "node_errors": {"1": {"errors": [{"type": "value_not_in_list"}]}}
                }
                if prompt_id is not None:
                    submitted["prompt_id"] = prompt_id

                def http(path, deadline, payload=None, *, submitted=submitted):
                    if path == "/prompt":
                        return submitted
                    if path.startswith("/history"):
                        return {}
                    return self.http(path, deadline, payload)

                with mock.patch.object(
                    renderer, "http_json", side_effect=http
                ) as calls:
                    for _ in range(2):
                        with self.assertRaisesRegex(renderer.RenderError, "unknown"):
                            self.execute(request_id)
                        record = self.record(request_id)
                        self.assertEqual(record["status"], "submission_unknown")
                        self.assertFalse(record.get("terminal", False))
                        self.assertTrue(record["job_may_still_be_running"])
                        self.assertFalse(record["resubmit_allowed"])
                self.assertEqual(
                    sum(call.args[0] == "/prompt" for call in calls.call_args_list), 1
                )

    def test_http_400_validation_response_is_terminal_and_not_reposted(self):
        details = {
            "error": {"type": "prompt_outputs_failed_validation"},
            "node_errors": {"1": {"errors": [{"type": "value_not_in_list"}]}},
        }
        real_http = renderer.http_json

        def http(path, deadline, payload=None):
            if path == "/prompt":
                with mock.patch.object(
                    renderer.urllib.request,
                    "urlopen",
                    side_effect=urllib.error.HTTPError(
                        renderer.ORIGIN + path,
                        400,
                        "Bad Request",
                        {},
                        io.BytesIO(json.dumps(details).encode()),
                    ),
                ):
                    return real_http(path, deadline, payload)
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http) as calls:
            with self.assertRaisesRegex(renderer.RenderError, "HTTP 400"):
                self.execute()
            replay = self.execute()
        self.assertEqual(replay["status"], "rejected")
        self.assertTrue(replay["terminal"])
        self.assertFalse(replay["job_may_still_be_running"])
        self.assertEqual(replay["http_status"], 400)
        self.assertEqual(replay["server_error"], details)
        self.assertEqual(
            sum(call.args[0] == "/prompt" for call in calls.call_args_list), 1
        )

    def test_server_node_errors_remain_structured_and_terminal(self):
        details = {
            "error": {"type": "prompt_outputs_failed_validation"},
            "node_errors": {
                "1": {
                    "errors": [
                        {
                            "type": "value_not_in_list",
                            "message": "Value not in list",
                            "details": "model unavailable",
                            "extra_info": {
                                "input_name": "model",
                                "received_value": "PRIVATE",
                            },
                        }
                    ]
                }
            },
        }

        def http(path, deadline, payload=None):
            if path == "/prompt":
                return details
            return self.http(path, deadline, payload)

        with (
            mock.patch.object(renderer, "http_json", side_effect=http),
            self.assertRaises(renderer.RenderError),
        ):
            self.execute()
        result = self.record()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            result["server_error"]["node_errors"]["1"]["errors"][0]["type"],
            "value_not_in_list",
        )
        self.assertNotIn("PRIVATE", json.dumps(result))
        with mock.patch.object(renderer, "http_json") as calls:
            replay = self.execute()
        calls.assert_not_called()
        self.assertFalse(replay["ok"])

    def test_generic_matchtype_switch_preserves_links_and_selected_branch(self):
        graph, info = copy.deepcopy(WORKFLOW), copy.deepcopy(INFO)
        graph["3"] = {
            "class_type": "ComfySwitchNode",
            "inputs": {"switch": True, "on_true": ["2", 0], "on_false": ["2", 0]},
        }
        graph["1"]["inputs"]["video"] = ["3", 0]
        info["ComfySwitchNode"] = {
            "input": {
                "required": {"switch": ["BOOLEAN"]},
                "optional": {
                    key: [
                        "COMFY_MATCHTYPE_V3",
                        {
                            "lazy": True,
                            "template": {"template_id": "switch", "allowed_types": "*"},
                        },
                    ]
                    for key in ("on_true", "on_false")
                },
            },
            "output": ["COMFY_MATCHTYPE_V3"],
            "output_matchtypes": ["switch"],
        }
        before = copy.deepcopy(graph)
        self.assertEqual(renderer.validate_workflow(graph, info), ["1"])
        self.assertEqual(graph, before)

    def test_raw_api_saveimage_collects_hash_without_video_decoder(self):
        graph = {"1": {"class_type": "SaveImage", "inputs": {}}}
        self.workflow_path.write_text(json.dumps(graph))
        image = self.output / "image.png"
        image.write_bytes(DATA)
        self.history["outputs"] = {
            "1": {
                "images": [{"filename": "image.png", "subfolder": "", "type": "output"}]
            }
        }

        def http(path, deadline, payload=None):
            if path == "/object_info":
                return {
                    "SaveImage": {
                        "input": {"required": {}},
                        "output_node": True,
                        "output": [],
                    }
                }
            return self.http(path, deadline, payload)

        with mock.patch.object(renderer, "http_json", side_effect=http):
            result = self.execute()
        self.assertEqual(result["assets"][0]["filename"], "image.png")
        self.assertNotIn("video", result["assets"][0])

    def test_real_local_http_accepts_post_then_drops_reply(self):
        calls = {"posts": 0, "payload": None}
        history = copy.deepcopy(self.history)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                calls["posts"] += 1
                calls["payload"] = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

            def do_GET(self):
                if self.path == "/system_stats":
                    data = {}
                elif self.path == "/object_info":
                    data = INFO
                elif self.path == "/queue":
                    data = {"queue_running": [], "queue_pending": []}
                elif self.path.startswith("/history"):
                    item = calls["payload"]
                    data = {}
                    if item:
                        history["prompt"] = [
                            0,
                            item["prompt_id"],
                            item["prompt"],
                            item["extra_data"],
                            ["1"],
                        ]
                        data[item["prompt_id"]] = history
                else:
                    self.send_error(404)
                    return
                encoded = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.network.side_effect = REAL_URLOPEN
            with mock.patch.object(
                renderer, "ORIGIN", f"http://127.0.0.1:{server.server_port}"
            ):
                result = self.execute()
                replay = self.execute()
            self.assertTrue(result["ok"])
            self.assertTrue(replay["ok"])
            self.assertEqual(calls["posts"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class MappingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workflow = self.root / "workflow.json"
        self.workflow.write_text(json.dumps(WORKFLOW))
        self.mapping = self.root / "mapping.json"
        self.definition = {
            "schema_version": 1,
            "workflow_version": "fixture-1",
            "workflow_sha256": hashlib.sha256(
                json.dumps(WORKFLOW, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "parameters": {
                "name": {
                    "node_id": "1",
                    "class_type": "SaveVideo",
                    "input": "filename_prefix",
                    "type": "string",
                }
            },
        }
        self.mapping.write_text(json.dumps(self.definition))
        self.args = argparse.Namespace(
            workflow=self.workflow,
            mapping=self.mapping,
            param=['name="video/parameter-test"'],
        )

    def test_mapping_pins_source_and_records_version_without_prompt_values(self):
        graph, digest, metadata = renderer.configured_workflow(self.args)
        self.assertEqual(
            graph["1"]["inputs"]["filename_prefix"], "video/parameter-test"
        )
        self.assertEqual(len(digest), 64)
        self.assertEqual(metadata["workflow_version"], "fixture-1")
        self.assertEqual(metadata["applied_parameters"], ["name"])
        self.assertNotIn("video/parameter-test", json.dumps(metadata))
        self.assertEqual(json.loads(self.workflow.read_text()), WORKFLOW)

    def test_changed_source_or_target_and_duplicate_unknown_parameters_fail(self):
        for params in (["other=1"], ['name="x"', 'name="y"'], ["name=NaN"], ["name=2"]):
            self.args.param = params
            with self.subTest(params=params), self.assertRaises(renderer.RenderError):
                renderer.configured_workflow(self.args)
        self.args.param = []
        self.workflow.write_text(json.dumps(WORKFLOW, indent=2))
        renderer.configured_workflow(
            self.args
        )  # Remote JSON serialization may change whitespace.
        changed = copy.deepcopy(WORKFLOW)
        changed["1"]["inputs"]["filename_prefix"] = "changed"
        self.workflow.write_text(json.dumps(changed))
        with self.assertRaisesRegex(renderer.RenderError, "hash"):
            renderer.configured_workflow(self.args)

    def test_supplied_mode_examples_are_hashed_and_do_not_claim_r2v_support(self):
        catalog = json.loads(
            (ROOT / "workflows/template-capabilities.json").read_text()
        )
        for item in catalog["templates"]:
            if item["workflow"] is None:
                self.assertEqual(item["mode"], "r2v")
                self.assertEqual(item["support"], "not_provided")
                continue
            args = argparse.Namespace(
                workflow=ROOT / "workflows" / item["workflow"],
                mapping=ROOT / "workflows" / item["mapping"],
                param=[],
            )
            if item["mode"] == "first_last_frame":
                args.param = ['first_frame="first.png"', 'last_frame="last.png"']
            graph, _, _ = renderer.configured_workflow(args)
            if item["mode"] == "first_last_frame":
                self.assertIn("last_frame", graph["105:104"]["inputs"])
            self.assertEqual(graph["105:122"]["class_type"], "ComfySwitchNode")


class HTTPErrorTests(unittest.TestCase):
    def test_http_validation_body_survives_as_structured_details(self):
        body = {
            "error": {"type": "invalid_prompt"},
            "node_errors": {
                "3": {
                    "errors": [
                        {"type": "missing_input", "details": "image is required"}
                    ]
                }
            },
        }
        failure = urllib.error.HTTPError(
            "http://fixture/prompt",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps(body).encode()),
        )
        with (
            mock.patch.object(renderer.urllib.request, "urlopen", side_effect=failure),
            self.assertRaises(renderer.RenderError) as raised,
        ):
            renderer.http_json("/prompt", renderer.Deadline(1), {"prompt": {}})
        self.assertEqual(raised.exception.http_status, 400)
        self.assertEqual(raised.exception.server_error, body)


if __name__ == "__main__":
    unittest.main()
