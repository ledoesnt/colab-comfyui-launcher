"""Public diagnostics fail closed without allocating any external resource."""

import contextlib
import gzip
import importlib.util
import io
import json
import struct
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "check_public", Path(__file__).resolve().parents[1] / "scripts/check_public.py"
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
URL = "https://fixture-public.trycloudflare.com"
PNG = (
    b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 64, 64)
)


class CheckPublicTests(unittest.TestCase):
    def setUp(self):
        self.socket = mock.Mock()
        self.socket.recv.return_value = json.dumps(
            {"type": "execution_success", "data": {"prompt_id": "fixture-job"}}
        )
        self.ws = SimpleNamespace(
            create_connection=mock.Mock(return_value=self.socket),
            WebSocketTimeoutException=TimeoutError,
        )
        self.history = {
            "fixture-job": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "2": {
                        "images": [
                            {
                                "filename": "fixture.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                },
            }
        }
        self.png = PNG
        self.requests = []

    def request(self, request, timeout):
        route = request.full_url.removeprefix(URL)
        self.requests.append(route)
        value = (
            {"prompt_id": "fixture-job"}
            if route == "/api/prompt"
            else self.history
            if route.startswith("/api/history/")
            else {}
        )
        response = io.BytesIO(
            self.png if route.startswith("/api/view?") else json.dumps(value).encode()
        )
        response.status = 200
        response.headers = {}
        return response

    def call(self, args=None, handler=None):
        stdout = io.StringIO()
        opener = SimpleNamespace(open=mock.Mock(side_effect=handler or self.request))
        with (
            mock.patch.dict("sys.modules", {"websocket": self.ws}),
            mock.patch.object(
                checker.urllib.request, "build_opener", return_value=opener
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = checker.main(args or ["--url", URL])
        return code, json.loads(stdout.getvalue()), opener

    def test_url_guards_and_budget_prevent_any_network(self):
        for args in (
            ["--url", "http://fixture.trycloudflare.com"],
            ["--url", URL + "/assets/"],
            ["--url", "https://fixture.trycloudflare.com.evil.invalid"],
            ["--url", "https://secret@fixture.trycloudflare.com"],
            ["--url", URL, "--max-seconds", "61"],
        ):
            with self.subTest(args=args):
                code, result, opener = self.call(args)
                self.assertEqual(code, 1)
                self.assertFalse(result["ok"])
                opener.open.assert_not_called()
                self.ws.create_connection.assert_not_called()
                self.assertNotIn("secret", json.dumps(result))

    def test_http_or_redirect_failure_never_submits_and_hides_url(self):
        failures = [
            urllib.error.HTTPError(URL, 401, "private URL", {}, None),
            checker.CheckError("Unexpected HTTP redirect"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                code, result, _ = self.call(handler=mock.Mock(side_effect=failure))
                self.assertEqual(code, 1)
                self.assertNotIn(URL, json.dumps(result))
                self.assertNotIn("/api/prompt", self.requests)
        with self.assertRaises(checker.CheckError):
            checker.NoRedirect().redirect_request(
                None, None, 302, None, {}, "https://login.example.invalid"
            )

    def test_ws_and_history_failure_are_not_png_success(self):
        self.socket.recv.return_value = json.dumps(
            {
                "type": "execution_error",
                "data": {"prompt_id": "fixture-job", "exception_message": URL},
            }
        )
        code, result, _ = self.call()
        self.assertEqual(code, 1)
        self.assertTrue(result["job_may_still_be_running"])
        self.assertNotIn(URL, json.dumps(result))
        self.assertFalse(any(route.startswith("/api/view") for route in self.requests))
        self.socket.close.assert_called_once()

    def test_wrong_png_dimensions_fail_after_successful_job(self):
        self.png = PNG[:16] + struct.pack(">II", 32, 32)
        code, result, _ = self.call()
        self.assertEqual(code, 1)
        self.assertFalse(result["job_may_still_be_running"])
        self.assertFalse(result["browser_ui_verified"])

    def test_overall_alarm_interrupts_blocked_network_call(self):
        def blocked_request(_request, timeout):
            time.sleep(0.1)
            self.fail("Deadline must interrupt this call")

        code, result, _ = self.call(
            ["--url", URL, "--max-seconds", "0.02"], handler=blocked_request
        )
        self.assertEqual(code, 1)
        self.assertIn("Overall diagnostic deadline", result["error"])
        self.ws.create_connection.assert_not_called()

    def test_complete_public_flow_does_not_claim_browser_ui(self):
        code, result, _ = self.call()
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["browser_ui_verified"])
        self.assertEqual(result["png"]["width"], 64)
        self.assertIn("execution_success", result["ws_events"])
        self.assertEqual(result["routes"]["/api/users"], 200)
        self.assertNotIn(URL, json.dumps(result))
        self.assertNotIn("fixture-job", json.dumps(result))

    def test_gzip_json_and_png_responses_complete_public_flow(self):
        class ChunkedResponse(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 7))

        def compressed_request(request, timeout):
            self.assertEqual(request.get_header("Accept-encoding"), "gzip")
            original = self.request(request, timeout)
            response = ChunkedResponse(gzip.compress(original.getvalue()))
            response.status = original.status
            response.headers = {"Content-Encoding": "gzip"}
            return response

        code, result, _ = self.call(handler=compressed_request)
        self.assertEqual(code, 0)
        self.assertEqual(result["png"]["bytes"], len(PNG))
        self.assertIn("execution_success", result["ws_events"])
        self.assertFalse(result["browser_ui_verified"])

    def test_gzip_expansion_over_limit_fails_before_submission(self):
        response = io.BytesIO(gzip.compress(b" " * (checker.BODY_LIMIT + 1)))
        response.status = 200
        response.headers = {"Content-Encoding": "gzip"}
        code, result, _ = self.call(handler=lambda _request, timeout: response)
        self.assertEqual(code, 1)
        self.assertIn("size limit", result["error"])
        self.assertFalse(any(route == "/api/prompt" for route in self.requests))
        self.ws.create_connection.assert_not_called()

    def test_truncated_gzip_fails_before_submission(self):
        response = io.BytesIO(gzip.compress(b"{}")[:-4])
        response.status = 200
        response.headers = {"Content-Encoding": "gzip"}
        code, result, _ = self.call(handler=lambda _request, timeout: response)
        self.assertEqual(code, 1)
        self.assertIn("incomplete gzip", result["error"])
        self.ws.create_connection.assert_not_called()

    def test_buffered_ws_events_are_drained_before_slow_http_history(self):
        self.socket.recv.side_effect = [
            json.dumps({"type": kind, "data": {"prompt_id": "fixture-job"}})
            for kind in ["status"] * 8
            + ["execution_start", "execution_cached", "executed", "execution_success"]
        ]

        def request_after_drain(request, timeout):
            if "/api/history/" in request.full_url:
                self.assertEqual(self.socket.recv.call_count, 12)
            return self.request(request, timeout)

        code, result, _ = self.call(handler=request_after_drain)
        self.assertEqual(code, 0)
        self.assertEqual(
            sum(route.startswith("/api/history/") for route in self.requests), 1
        )
        self.assertIn("execution_start", result["ws_events"])

    def test_completed_history_without_ws_success_still_fails_at_deadline(self):
        now = [0.0]

        def status_frame():
            now[0] += 1
            return json.dumps({"type": "status", "data": {}})

        self.socket.recv.side_effect = status_frame
        with mock.patch.object(checker.time, "monotonic", side_effect=lambda: now[0]):
            code, result, _ = self.call(["--url", URL, "--max-seconds", "10"])
        self.assertEqual(code, 1)
        self.assertIn("deadline", result["error"])
        self.assertEqual(
            sum(route.startswith("/api/history/") for route in self.requests), 1
        )
        self.assertFalse(any(route.startswith("/api/view?") for route in self.requests))


if __name__ == "__main__":
    unittest.main()
