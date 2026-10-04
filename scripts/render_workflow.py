#!/usr/bin/env python3
"""Submit an owned ComfyUI job and verify its saved audio/video asset."""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

BASE = Path("/content/colab-comfyui-runtime")
CONTENT_ROOT = Path("/content")
DRIVE_MOUNT = CONTENT_ROOT / "drive"
ORIGIN = "http://127.0.0.1:8188"
DEFAULT_WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/h3-api.json"
DEFAULT_RESULT = BASE / "render-result.json"
CHUNK_BYTES = 4 * 1024 * 1024


def load_runtime():
    path = Path(__file__).with_name("runtime.py")
    spec = importlib.util.spec_from_file_location("launcher_runtime_for_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load_runtime()


class RenderError(Exception):
    """A safe diagnostic that does not embed prompts, server logs, or secrets."""


class Deadline:
    def __init__(self, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            raise RenderError("max-seconds must be finite and positive")
        self.end = time.monotonic() + seconds

    def remaining(self):
        seconds = self.end - time.monotonic()
        if seconds <= 0:
            raise RenderError("Overall render deadline exceeded")
        return seconds


@contextlib.contextmanager
def time_budget(seconds):
    deadline = Deadline(seconds)

    def expired(_signum, _frame):
        raise RenderError("Overall render deadline exceeded")

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    start = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield deadline
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            remaining = max(0.000001, old_timer[0] - (time.monotonic() - start))
            signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])


class Report:
    def __init__(self, path):
        self.path = Path(path)
        self.start = time.monotonic()
        self.value = {"ok": False, "status": "preparing"}

    def update(self, **fields):
        self.value.update(fields)
        self.value["elapsed_seconds"] = round(time.monotonic() - self.start, 3)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self.value, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, self.path)


def http_json(path, deadline, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        ORIGIN + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=min(10, deadline.remaining())
        ) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        raise RenderError(f"ComfyUI request failed (HTTP {error.code})") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RenderError("ComfyUI request did not return valid JSON") from None
    if not isinstance(value, dict):
        raise RenderError("ComfyUI returned an unexpected JSON shape")
    return value


def owned_storage(state):
    value = state.get("storage_root")
    if not isinstance(value, str) or not value:
        raise RenderError("Owned service state has no storage root")
    root = Path(value).resolve()
    if state.get("ephemeral") is True:
        if not root.is_relative_to(CONTENT_ROOT.resolve()) or root.is_relative_to(
            DRIVE_MOUNT.resolve()
        ):
            raise RenderError(
                "Ephemeral output storage is outside its runtime directory"
            )
    elif state.get("ephemeral") is False:
        if not os.path.ismount(DRIVE_MOUNT):
            raise RenderError(
                "Drive is not mounted; persisted output cannot be verified"
            )
        mydrive = DRIVE_MOUNT / "MyDrive"
        if mydrive.is_symlink() or not root.is_relative_to(mydrive.resolve()):
            raise RenderError("Persistent output storage is outside MyDrive")
    else:
        raise RenderError("Owned service state has no explicit persistence mode")
    output = root / "output"
    if not output.is_dir() or not output.resolve().is_relative_to(root):
        raise RenderError(
            "Owned output directory is missing or escapes its storage root"
        )
    return output.resolve()


def load_workflow(path):
    try:
        data = Path(path).read_bytes()
        workflow = json.loads(data)
    except (OSError, ValueError):
        raise RenderError("Cannot read a valid JSON workflow") from None
    if not isinstance(workflow, dict) or not workflow:
        raise RenderError("Workflow must be a nonempty ComfyUI API-format object")
    return workflow, hashlib.sha256(data).hexdigest()


def finalized_input_schema(schema, inputs, prefix=""):
    """Expand selected V3 DynamicCombo branches using API dot-separated inputs."""
    finalized = {"required": {}, "optional": {}}
    for category in ("required", "optional"):
        fields = schema.get(category, {})
        if not isinstance(fields, dict):
            raise RenderError("ComfyUI returned an unsupported input schema")
        for name, descriptor in fields.items():
            if (
                not isinstance(name, str)
                or not isinstance(descriptor, (list, tuple))
                or not descriptor
            ):
                raise RenderError("ComfyUI returned an unsupported input descriptor")
            full_name = prefix + name
            finalized[category][full_name] = descriptor
            if descriptor[0] != "COMFY_DYNAMICCOMBO_V3":
                continue
            if full_name not in inputs:
                continue
            selected = inputs[full_name]
            if not isinstance(selected, str):
                raise RenderError(
                    "DynamicCombo API selections must be strings with dot-separated child inputs"
                )
            metadata = descriptor[1] if len(descriptor) > 1 else None
            options = metadata.get("options") if isinstance(metadata, dict) else None
            if not isinstance(options, list):
                raise RenderError("ComfyUI returned an unsupported DynamicCombo schema")
            branch = next(
                (
                    option
                    for option in options
                    if isinstance(option, dict) and option.get("key") == selected
                ),
                None,
            )
            if branch is None or not isinstance(branch.get("inputs"), dict):
                raise RenderError(
                    "Workflow references an unavailable DynamicCombo option"
                )
            children = finalized_input_schema(branch["inputs"], inputs, full_name + ".")
            for child_category, declarations in finalized.items():
                declarations.update(children[child_category])
    return finalized


def validate_workflow(workflow, info):
    save_nodes = []
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise RenderError("Workflow node IDs and objects are invalid")
        kind = node.get("class_type")
        inputs = node.get("inputs")
        if (
            not isinstance(kind, str)
            or kind not in info
            or not isinstance(inputs, dict)
        ):
            raise RenderError("A required workflow node is unavailable or malformed")
        schema = finalized_input_schema(info[kind].get("input", {}), inputs)
        required = schema.get("required", {})
        optional = schema.get("optional", {})
        if not isinstance(required, dict) or not isinstance(optional, dict):
            raise RenderError("ComfyUI returned an unsupported input schema")
        if any(name not in inputs for name in required):
            raise RenderError("A required workflow input is missing")
        for name, value in inputs.items():
            descriptor = required.get(name, optional.get(name))
            if descriptor is None:
                raise RenderError("Workflow contains an undeclared node input")
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and type(value[1]) is int
            ):
                linked = workflow.get(value[0])
                linked_info = (
                    info.get(linked.get("class_type"), {})
                    if isinstance(linked, dict)
                    else {}
                )
                outputs = linked_info.get("output", [])
                if not 0 <= value[1] < len(outputs):
                    raise RenderError("Workflow has an invalid node output link")
                continue
            field_type = (
                descriptor[0]
                if isinstance(descriptor, (list, tuple)) and descriptor
                else None
            )
            if isinstance(field_type, list) and value not in field_type:
                raise RenderError(
                    "Workflow references an unavailable model or COMBO option"
                )
            if field_type == "INT" and type(value) is not int:
                raise RenderError("Workflow integer input has an invalid type")
            if field_type == "FLOAT" and (
                type(value) not in (int, float) or not math.isfinite(value)
            ):
                raise RenderError("Workflow float input has an invalid type")
            if field_type == "STRING" and not isinstance(value, str):
                raise RenderError("Workflow string input has an invalid type")
            # DynamicCombo objects are validated authoritatively by POST /prompt.
        if kind == "SaveVideo":
            save_nodes.append(node_id)
    if not save_nodes:
        raise RenderError("Workflow must contain a SaveVideo output node")
    return save_nodes


def job_error(history):
    status = history.get("status", {})
    if not isinstance(status, dict):
        raise RenderError("History status is malformed")
    for message in status.get("messages", []):
        if (
            isinstance(message, (list, tuple))
            and message
            and message[0] in ("execution_error", "execution_interrupted")
        ):
            raise RenderError(f"Workflow ended with {message[0]}")
    if status.get("status_str") == "error":
        raise RenderError("Workflow history reports an execution error")


def queue_phase(queue, prompt_id):
    for field, phase in (("queue_running", "running"), ("queue_pending", "queued")):
        jobs = queue.get(field, [])
        if not isinstance(jobs, list):
            raise RenderError("ComfyUI queue status is malformed")
        for position, job in enumerate(jobs):
            if isinstance(job, (list, tuple)) and len(job) >= 2 and job[1] == prompt_id:
                return phase, position
    return "waiting_for_history", None


def output_assets(history, save_nodes, output_root):
    outputs = history.get("outputs", {})
    if not isinstance(outputs, dict):
        raise RenderError("Workflow history has no valid outputs")
    assets = []
    seen = set()
    for node_id in save_nodes:
        node = outputs.get(node_id, {})
        if not isinstance(node, dict):
            raise RenderError("SaveVideo history output is malformed")
        for key in ("images", "videos", "gifs"):
            entries = node.get(key, [])
            if not isinstance(entries, list):
                raise RenderError("SaveVideo asset list is malformed")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise RenderError("SaveVideo asset record is malformed")
                filename = entry.get("filename")
                subfolder = entry.get("subfolder", "")
                if not isinstance(filename, str) or not isinstance(subfolder, str):
                    raise RenderError("SaveVideo asset has invalid filenames")
                if not filename.lower().endswith(".mp4"):
                    continue
                if entry.get("type") != "output":
                    raise RenderError("Saved video is not an output asset")
                if (
                    filename in ("", ".", "..")
                    or "/" in filename
                    or "\\" in filename
                    or any(ord(character) < 32 for character in filename)
                ):
                    raise RenderError(
                        "Saved video filename escapes its output directory"
                    )
                if (
                    "\\" in subfolder
                    or (subfolder and str(PurePosixPath(subfolder)) != subfolder)
                    or PurePosixPath(subfolder).is_absolute()
                    or any(
                        part in (".", "..") for part in PurePosixPath(subfolder).parts
                    )
                ):
                    raise RenderError(
                        "Saved video subfolder escapes its output directory"
                    )
                path = (output_root / subfolder / filename).resolve()
                if not path.is_relative_to(output_root) or not path.is_file():
                    raise RenderError("Saved video is missing or outside owned storage")
                relative = path.relative_to(output_root).as_posix()
                if relative not in seen:
                    seen.add(relative)
                    assets.append(
                        (
                            path,
                            {
                                "filename": filename,
                                "subfolder": subfolder,
                                "type": "output",
                            },
                        )
                    )
    if not assets:
        raise RenderError("Completed workflow produced no saved MP4")
    return assets


def hash_stream(stream, deadline):
    digest = hashlib.sha256()
    size = 0
    while True:
        deadline.remaining()
        chunk = stream.read(CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def verify_api_bytes(path, asset, deadline):
    with path.open("rb") as stream:
        disk = hash_stream(stream, deadline)
    url = ORIGIN + "/view?" + urllib.parse.urlencode(asset)
    try:
        with urllib.request.urlopen(
            url, timeout=min(10, deadline.remaining())
        ) as response:
            served = hash_stream(response, deadline)
    except (urllib.error.URLError, OSError):
        raise RenderError("Saved video could not be retrieved through /view") from None
    if disk != served or not disk[1]:
        raise RenderError("API video bytes do not match the persisted storage file")
    return disk


def expected_geometry(workflow):
    h3 = [
        node["inputs"]
        for node in workflow.values()
        if node.get("class_type") == "MiniMaxH3ImageToVideo"
    ]
    video = [
        node["inputs"]
        for node in workflow.values()
        if node.get("class_type") == "CreateVideo"
    ]
    if len(h3) == 1 and len(video) == 1:
        width, height, frames = (
            h3[0].get(name) for name in ("width", "height", "length")
        )
        fps = video[0].get("fps")
        if (
            all(type(value) is int and value > 0 for value in (width, height, frames))
            and type(fps) in (int, float)
            and fps > 0
        ):
            return {
                "width": width,
                "height": height,
                "duration": frames / fps,
                "fps": fps,
            }
    return None


def media_metadata(path, deadline, expected=None):
    import av  # Installed in the ComfyUI environment; no model loading occurs here.

    videos, audio = [], []
    with av.open(str(path)) as container:
        targets = [
            (stream.type, stream.index)
            for stream in container.streams
            if stream.type in ("video", "audio")
        ]
        if not any(kind == "video" for kind, _ in targets) or not any(
            kind == "audio" for kind, _ in targets
        ):
            raise RenderError("Saved MP4 must contain video and audio tracks")
    for kind, index in targets:
        deadline.remaining()
        # Reopen so every track is checked from its beginning, without exporting media.
        with av.open(str(path)) as container:
            stream = container.streams[index]
            first = next(container.decode(stream), None)
            if first is None:
                raise RenderError("An MP4 track has no decodable first frame")
            duration = (
                float(stream.duration * stream.time_base)
                if stream.duration is not None and stream.time_base is not None
                else float(container.duration / 1000000)
                if container.duration
                else 0.0
            )
            if not math.isfinite(duration) or duration <= 0:
                raise RenderError("An MP4 track has no positive duration")
            record = {
                "codec": stream.codec_context.name,
                "duration_seconds": round(duration, 6),
                "first_frame_decoded": True,
            }
            if kind == "video":
                fps = float(stream.average_rate) if stream.average_rate else 0.0
                if (
                    first.width <= 0
                    or first.height <= 0
                    or not math.isfinite(fps)
                    or fps <= 0
                ):
                    raise RenderError(
                        "Saved video has invalid dimensions or frame rate"
                    )
                record.update(
                    width=first.width,
                    height=first.height,
                    fps=round(fps, 6),
                    reported_frames=stream.frames,
                )
                if expected and (
                    first.width != expected["width"]
                    or first.height != expected["height"]
                    or abs(fps - expected["fps"]) > 0.01
                    or abs(duration - expected["duration"]) > 2 / expected["fps"]
                ):
                    raise RenderError(
                        "Saved video geometry or duration differs from its workflow"
                    )
                videos.append(record)
            else:
                channels = len(first.layout.channels)
                if channels <= 0 or first.sample_rate <= 0 or first.samples <= 0:
                    raise RenderError("Saved audio has invalid channels or sample data")
                record.update(
                    channels=channels,
                    sample_rate=first.sample_rate,
                    layout=first.layout.name,
                    first_frame_samples=first.samples,
                )
                audio.append(record)
    return {"video": videos, "audio": audio}


def render(args, report):
    with time_budget(args.max_seconds) as deadline:
        state = runtime.read_json(runtime.STATE)
        owned = state.get("comfyui")
        if not runtime.alive(owned):
            raise RenderError("Owned ComfyUI service is not alive")
        output_root = owned_storage(state)
        workflow, workflow_sha = load_workflow(args.workflow)
        http_json("/system_stats", deadline)
        save_nodes = validate_workflow(workflow, http_json("/object_info", deadline))
        report.update(
            status="submitting",
            workflow_sha256=workflow_sha,
            storage="ephemeral" if state["ephemeral"] else "google-drive",
        )
        submitted = http_json(
            "/prompt", deadline, {"client_id": uuid.uuid4().hex, "prompt": workflow}
        )
        prompt_id = submitted.get("prompt_id")
        if submitted.get("node_errors") or submitted.get("error"):
            raise RenderError("ComfyUI rejected the submitted workflow")
        if not isinstance(prompt_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", prompt_id
        ):
            raise RenderError("ComfyUI returned no valid prompt_id")
        report.update(
            status="queued", prompt_id=prompt_id, job_may_still_be_running=True
        )
        last_report = 0.0
        last_phase = None
        while True:
            deadline.remaining()
            if not runtime.alive(owned):
                raise RenderError("Owned ComfyUI exited before the job completed")
            history = http_json("/history/" + prompt_id, deadline).get(prompt_id)
            if history is not None:
                if not isinstance(history, dict):
                    raise RenderError("Workflow history is malformed")
                job_error(history)
                status = history.get("status", {})
                if status.get("completed") is True:
                    if status.get("status_str") != "success":
                        raise RenderError(
                            "Workflow history is complete without success"
                        )
                    break
            phase, position = queue_phase(http_json("/queue", deadline), prompt_id)
            if phase != last_phase or time.monotonic() - last_report >= 5:
                report.update(status=phase, queue_position=position)
                last_phase, last_report = phase, time.monotonic()
            time.sleep(min(2, deadline.remaining()))
        report.update(
            status="validating", job_may_still_be_running=False, queue_position=None
        )
        records = []
        expected = expected_geometry(workflow)
        for path, asset in output_assets(history, save_nodes, output_root):
            sha256, size = verify_api_bytes(path, asset, deadline)
            records.append(
                {
                    "relative_path": path.relative_to(output_root).as_posix(),
                    "sha256": sha256,
                    "bytes": size,
                    **media_metadata(path, deadline, expected),
                }
            )
        report.update(ok=True, status="succeeded", assets=records)
        return report.value


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise RenderError("Invalid command arguments; see --help")


def main(argv=None):
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=900,
        help="Overall deadline including media validation (default: 900)",
    )
    parser.add_argument("--result-file", type=Path, default=DEFAULT_RESULT)
    report = None
    try:
        args = parser.parse_args(argv)
        report = Report(args.result_file)
        report.update()
        result = render(args, report)
    # This CLI boundary must emit failed JSON for decoder and unexpected I/O errors.
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001
        message = (
            str(error)
            if isinstance(error, RenderError)
            else (
                "Render interrupted"
                if isinstance(error, KeyboardInterrupt)
                else f"Render failed ({type(error).__name__})"
            )
        )
        result = dict(report.value) if report else {}
        result.update(ok=False, status="failed", error=message)
        if report:
            try:
                report.update(**result)
                result = report.value
            except (OSError, ValueError, TypeError):
                result["result_file_written"] = False
    # A timeout leaves the server's job subject to the caller's stop/cleanup policy.
    # Only the local runner exits; this program never unassigns a Colab session.
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] is True else 1


if __name__ == "__main__":
    sys.exit(main())
