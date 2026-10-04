#!/usr/bin/env python3
"""Explicit public-tunnel API/WS/PNG diagnostic; does not verify browser UI."""

import argparse
import hashlib
import json
import math
import re
import signal
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

BODY_LIMIT = 16 * 1024 * 1024


class CheckError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _fp, _code, _message, _headers, _url):
        raise CheckError("Unexpected HTTP redirect; use an explicit public tunnel")


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise CheckError("Invalid arguments; see --help")


def read_body(response, remaining):
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding not in ("", "identity", "gzip"):
        raise CheckError("Public response used an unsupported content encoding")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    body = bytearray()
    while True:
        remaining()
        chunk = response.read(min(65536, BODY_LIMIT - len(body) + 1))
        if not chunk:
            break
        if decoder is not None:
            chunk = decoder.decompress(chunk, BODY_LIMIT - len(body) + 1)
        body.extend(chunk)
        if len(body) > BODY_LIMIT:
            raise CheckError("Public response exceeded the diagnostic size limit")
        if decoder is not None and decoder.unused_data:
            raise CheckError("Public response contained unexpected gzip trailing data")
    if decoder is not None and not decoder.eof:
        raise CheckError("Public response contained incomplete gzip data")
    return bytes(body)


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com", parsed.netloc
        )
    ):
        raise CheckError(
            "url must be an HTTPS trycloudflare root URL without credentials"
        )
    return "https://" + parsed.netloc


def run(url, seconds, result):
    import websocket  # Install explicitly with uv --with; import never installs packages.

    end = time.monotonic() + seconds
    opener = urllib.request.build_opener(NoRedirect())

    def remaining():
        left = end - time.monotonic()
        if left <= 0:
            raise CheckError("Overall diagnostic deadline exceeded")
        return min(30, left)

    def request(route, payload=None, binary=False):
        route_name = (
            "/api/history/{prompt_id}"
            if route.startswith("/api/history/")
            else route.split("?", 1)[0]
        )
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url + route,
            data=data,
            headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
        )
        try:
            with opener.open(req, timeout=remaining()) as response:
                result["routes"][route_name] = response.status
                body = read_body(response, remaining)
        except urllib.error.HTTPError as error:
            result["routes"][route_name] = error.code
            raise CheckError(
                "Public API returned an unsuccessful HTTP status"
            ) from None
        if binary:
            return body
        value = json.loads(body)
        if not isinstance(value, dict):
            raise CheckError("Public API returned an unexpected JSON shape")
        return value

    for route in (
        "features",
        "users",
        "settings",
        "i18n",
        "object_info",
        "queue",
        "system_stats",
    ):
        result["stage"] = "/api/" + route
        request(result["stage"])
    client = uuid.uuid4().hex
    result["stage"] = "websocket"
    ws = websocket.create_connection(
        url.replace("https://", "wss://", 1) + "/ws?clientId=" + client,
        timeout=remaining(),
        redirect_limit=0,
    )
    try:
        workflow = {
            "1": {
                "class_type": "EmptyImage",
                "inputs": {
                    "width": 64,
                    "height": 64,
                    "batch_size": 1,
                    "color": 3368601,
                },
            },
            "2": {
                "class_type": "SaveImage",
                "inputs": {
                    "images": ["1", 0],
                    "filename_prefix": "public_check_" + client,
                },
            },
        }
        result["stage"] = "prompt"
        submitted = request("/api/prompt", {"client_id": client, "prompt": workflow})
        prompt = submitted.get("prompt_id")
        if (
            submitted.get("error")
            or submitted.get("node_errors")
            or not isinstance(prompt, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prompt)
        ):
            raise CheckError("Public workflow was rejected")
        result["job_may_still_be_running"] = True
        history, success = None, False
        next_history_poll = time.monotonic() + 5
        result["stage"] = "execution"
        while not (history and success):
            force_history_poll = False
            ws.settimeout(min(1, remaining()))
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                raw = None
            if isinstance(raw, str) and raw:
                event = json.loads(raw)
                kind, data = event.get("type"), event.get("data", {})
                if kind in {
                    "status",
                    "executing",
                    "executed",
                    "execution_success",
                    "execution_error",
                    "execution_interrupted",
                    "execution_cached",
                    "execution_start",
                }:
                    result["ws_events"] = sorted(set(result["ws_events"]) | {kind})
                if isinstance(data, dict) and data.get("prompt_id") == prompt:
                    if kind in ("execution_error", "execution_interrupted"):
                        raise CheckError("Public workflow execution failed")
                    if kind == "execution_success":
                        force_history_poll = not success
                        success = True
            if not force_history_poll and time.monotonic() < next_history_poll:
                continue
            candidate = request("/api/history/" + prompt).get(prompt)
            next_history_poll = time.monotonic() + 5
            if candidate:
                status = candidate.get("status", {})
                if status.get("status_str") == "error" or any(
                    message[0] in ("execution_error", "execution_interrupted")
                    for message in status.get("messages", [])
                    if isinstance(message, list) and message
                ):
                    raise CheckError("Public workflow history reports failure")
                if (
                    status.get("completed") is True
                    and status.get("status_str") == "success"
                ):
                    history = candidate
        result["job_may_still_be_running"] = False
        assets = history.get("outputs", {}).get("2", {}).get("images", [])
        if len(assets) != 1 or assets[0].get("type") != "output":
            raise CheckError("Public workflow produced no unique output PNG")
        result["stage"] = "output"
        asset = assets[0]
        query = urllib.parse.urlencode(
            {name: asset[name] for name in ("filename", "subfolder", "type")}
        )
        png = request("/api/view?" + query, binary=True)
        if (
            len(png) < 24
            or png[:8] != b"\x89PNG\r\n\x1a\n"
            or png[12:16] != b"IHDR"
            or struct.unpack(">II", png[16:24]) != (64, 64)
        ):
            raise CheckError("Public output is not a 64x64 PNG")
        result.update(
            ok=True,
            stage="complete",
            png={
                "bytes": len(png),
                "sha256": hashlib.sha256(png).hexdigest(),
                "width": 64,
                "height": 64,
            },
        )
    finally:
        ws.close()


def main(argv=None):
    result = {"ok": False, "browser_ui_verified": False, "routes": {}, "ws_events": []}
    old_handler, old_timer = (
        signal.getsignal(signal.SIGALRM),
        signal.getitimer(signal.ITIMER_REAL),
    )
    started = time.monotonic()

    def expired(_signum, _frame):
        raise CheckError("Overall diagnostic deadline exceeded")

    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--url", required=True)
        parser.add_argument("--max-seconds", type=float, default=60)
        args = parser.parse_args(argv)
        url = origin(args.url)
        if not math.isfinite(args.max_seconds) or not 0 < args.max_seconds <= 60:
            raise CheckError("max-seconds must be positive and no more than 60")
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, args.max_seconds)
        run(url, args.max_seconds, result)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - safe CLI JSON boundary.
        result.update(
            ok=False,
            error=str(error)
            if isinstance(error, CheckError)
            else "Public diagnostic failed (" + type(error).__name__ + ")",
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, old_timer[0] - (time.monotonic() - started)),
                old_timer[1],
            )
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
