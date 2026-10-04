#!/usr/bin/env python3
"""Own only this launcher's processes; keep assets on Drive and code on VM disk."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = Path("/content/colab-comfyui-runtime")
STATE = BASE / "services.json"
ORIGIN = "http://127.0.0.1:8188"


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def process_stamp(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def alive(proc):
    return bool(
        proc
        and isinstance(proc.get("pid"), int)
        and proc["pid"] > 1
        and proc.get("start_ticks")
        and process_stamp(proc["pid"]) == proc["start_ticks"]
    )


def port_free():
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8188))
            return True
        except OSError:
            return False


def kill_owned(proc):
    if not alive(proc):
        return
    # Our Popen uses start_new_session: process group equals its pid.
    try:
        os.killpg(proc["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        if not alive(proc):
            return
        time.sleep(0.1)
    if alive(proc):
        try:
            os.killpg(proc["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def spawn(argv, log, cwd=None):
    with log.open("ab") as out:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    stamp = process_stamp(proc.pid)
    if stamp is None:
        raise RuntimeError(
            "Spawned process exited before its identity could be recorded"
        )
    return {"pid": proc.pid, "start_ticks": stamp}


def http_json(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        ORIGIN + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def healthy():
    try:
        http_json("/system_stats")
        return True
    except (OSError, ValueError):
        return False


def storage_root(args):
    if args.ephemeral:
        root = BASE / "ephemeral-assets"
    else:
        if not os.path.ismount("/content/drive"):
            raise RuntimeError(
                "Drive is not mounted; run colab drivemount and finish consent first"
            )
        root = Path(args.storage_root).resolve()
        mydrive = Path("/content/drive/MyDrive").resolve()
        if root == mydrive or not root.is_relative_to(mydrive):
            raise ValueError(
                "storage-root must be a dedicated directory inside /content/drive/MyDrive"
            )
    for child in ("models", "input", "output", "user"):
        (root / child).mkdir(parents=True, exist_ok=True)
    probe = root / f".write-probe-{uuid.uuid4().hex}"
    probe.write_text("write probe")
    probe.unlink()
    return root


def start(args):
    public = getattr(args, "public", False)
    if bool(public) == bool(args.allowed_email):
        raise ValueError("Choose exactly one of --public or --allowed-email")
    if not public and not re.fullmatch(
        r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", args.allowed_email or ""
    ):
        raise ValueError("--allowed-email must be one explicit mailbox")
    if read_json(BASE / "install.json").get("status") != "ready":
        raise RuntimeError(
            "Installation is not ready; check install.json and install.log"
        )
    old = read_json(STATE)
    if alive(old.get("comfyui")) or alive(old.get("cloudflared")):
        raise RuntimeError(
            "Already running; use status or stop before changing settings"
        )
    if not port_free():
        raise RuntimeError(
            "Port 8188 already has a server; do not reuse an unowned server"
        )
    root = storage_root(args)
    # JSON is valid YAML; prevent values from being interpreted as YAML syntax.
    paths = {"launcher": {"base_path": str(root / "models"), "is_default": True}}
    for category in (
        "diffusion_models",
        "text_encoders",
        "vae",
        "checkpoints",
        "loras",
        "clip_vision",
    ):
        (root / "models" / category).mkdir(exist_ok=True)
        paths["launcher"][category] = category
    config = BASE / "model-paths.yaml"
    config.write_text(json.dumps(paths))
    state = {
        "storage_root": str(root),
        "ephemeral": args.ephemeral,
        "started_at": time.time(),
        "access_mode": "public" if public else "email",
    }
    comfy = [
        str(BASE / ".venv/bin/python"),
        str(BASE / "ComfyUI/main.py"),
        "--listen",
        "127.0.0.1",
        "--port",
        "8188",
        "--disable-auto-launch",
        "--disable-all-custom-nodes",
        "--enable-compress-response-body",
        "--extra-model-paths-config",
        str(config),
        "--input-directory",
        str(root / "input"),
        "--output-directory",
        str(root / "output"),
        "--user-directory",
        str(root / "user"),
    ]
    if args.cpu:
        comfy.append("--cpu")
    try:
        state["comfyui"] = spawn(
            comfy, BASE / "logs/comfyui.log", str(BASE / "ComfyUI")
        )
        write_json(STATE, state)
        deadline = time.monotonic() + 120
        while not healthy():
            if not alive(state["comfyui"]):
                raise RuntimeError("ComfyUI exited; inspect logs/comfyui.log")
            if time.monotonic() > deadline:
                raise TimeoutError("ComfyUI readiness timed out")
            time.sleep(1)
        if not alive(state["comfyui"]):
            raise RuntimeError("Owned ComfyUI exited during readiness")
        log = BASE / "logs/cloudflared.log"
        log.write_text("")
        tunnel = [
            str(BASE / "bin/cloudflared"),
            "--no-autoupdate",
            "tunnel",
            "--url",
            ORIGIN,
        ]
        if not public:
            tunnel.extend(["--allowed-mail", args.allowed_email])
        state["cloudflared"] = spawn(tunnel, log)
        write_json(STATE, state)
        deadline = time.monotonic() + 60
        while True:
            matches = re.findall(
                r"https://[a-z0-9-]+\.trycloudflare\.com",
                log.read_text(errors="replace"),
            )
            if matches:
                if not alive(state["comfyui"]) or not alive(state["cloudflared"]):
                    raise RuntimeError(
                        "An owned service exited before startup completed"
                    )
                state["url"] = matches[-1]
                write_json(STATE, state)
                return {
                    "ok": True,
                    "status": "started",
                    "url": state["url"],
                    "storage_root": str(root),
                    "access_mode": state["access_mode"],
                }
            if not alive(state["cloudflared"]):
                raise RuntimeError("cloudflared exited; inspect logs/cloudflared.log")
            if time.monotonic() > deadline:
                raise TimeoutError("Tunnel URL timed out")
            time.sleep(1)
    except BaseException:
        kill_owned(state.get("cloudflared"))
        kill_owned(state.get("comfyui"))
        state["status"] = "failed"
        write_json(STATE, state)
        raise


def status(_args):
    state = read_json(STATE)
    return {
        "ok": True,
        "installation": read_json(BASE / "install.json"),
        "comfyui_alive": alive(state.get("comfyui")),
        "tunnel_alive": alive(state.get("cloudflared")),
        "http_ready": healthy(),
        "url": state.get("url"),
        "storage_root": state.get("storage_root"),
        "access_mode": state.get("access_mode"),
    }


def stop(_args):
    state = read_json(STATE)
    kill_owned(state.get("cloudflared"))
    kill_owned(state.get("comfyui"))
    state.pop("url", None)
    state["status"] = "stopped"
    write_json(STATE, state)
    return {"ok": True, "status": "stopped", "runtime_still_running": True}


def smoke(_args):
    state = read_json(STATE)
    if not alive(state.get("comfyui")) or not healthy():
        raise RuntimeError("Owned ComfyUI is not ready")
    import websocket  # Installed as part of the ComfyUI dependency environment.

    client = uuid.uuid4().hex
    ws = websocket.create_connection(
        f"ws://127.0.0.1:8188/ws?clientId={client}", timeout=10
    )
    with contextlib.closing(ws):
        initial = json.loads(ws.recv())
        if initial.get("type") != "status":
            raise RuntimeError("WebSocket did not provide status")
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
                    "filename_prefix": f"launcher-smoke/{client}",
                },
            },
        }
        prompt_id = http_json("/prompt", {"client_id": client, "prompt": workflow})[
            "prompt_id"
        ]
        deadline = time.monotonic() + 60
        events = []
        while True:
            event = json.loads(ws.recv())
            events.append(event.get("type"))
            if event.get("type") in ("execution_error", "execution_interrupted"):
                raise RuntimeError(f"Smoke workflow failed: {event.get('type')}")
            if (
                event.get("type") == "executing"
                and event["data"].get("prompt_id") == prompt_id
                and event["data"].get("node") is None
            ):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Smoke execution timed out")
    history = http_json(f"/history/{prompt_id}")[prompt_id]
    if not history.get("status", {}).get("completed"):
        raise RuntimeError("History has no completed output")
    asset = history["outputs"]["2"]["images"][0]
    query = urllib.parse.urlencode(asset)
    with urllib.request.urlopen(ORIGIN + "/view?" + query, timeout=10) as response:
        data = response.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[16:24] != b"\x00\x00\x00@\x00\x00\x00@":
        raise RuntimeError("Output is not a 64x64 PNG")
    output_root = Path(state["storage_root"]) / "output"
    disk = (output_root / asset["subfolder"] / asset["filename"]).resolve()
    if not disk.is_relative_to(output_root.resolve()):
        raise RuntimeError("Unexpected output path")
    sha = hashlib.sha256(data).hexdigest()
    if hashlib.sha256(disk.read_bytes()).hexdigest() != sha:
        raise RuntimeError("API and storage bytes differ")
    return {
        "ok": True,
        "test": "model-free PNG pipeline",
        "sha256": sha,
        "bytes": len(data),
        "websocket_events": events,
        "drive_persistence": not state["ephemeral"],
        "asset_relative_path": str(disk.relative_to(output_root)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "smoke"))
    parser.add_argument(
        "--storage-root", default="/content/drive/MyDrive/colab-comfyui"
    )
    parser.add_argument("--ephemeral", action="store_true")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--allowed-email")
    access.add_argument(
        "--public", action="store_true", help="Explicitly expose a temporary public URL"
    )
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()
    if args.action == "start" and not (args.public or args.allowed_email):
        parser.error("start requires --public or --allowed-email")
    BASE.mkdir(exist_ok=True)
    (BASE / "logs").mkdir(exist_ok=True)
    try:
        if args.action == "status":
            result = status(args)
        else:
            with (BASE / "services.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = globals()[args.action](args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns structured errors.
        result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
