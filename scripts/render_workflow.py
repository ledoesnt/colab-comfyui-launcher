#!/usr/bin/env python3
"""Submit or recover an owned ComfyUI job and verify its saved output assets."""

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import stat
import sys
import tempfile
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
DEFAULT_WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/h3-i2v-api.json"
DEFAULT_RESULT = BASE / "render-result.json"
CHUNK_BYTES = 4 * 1024 * 1024
I2V_INPUT_NAME = "launcher-h3-i2v-example.png"
I2V_INPUT_URL = (
    "https://raw.githubusercontent.com/Comfy-Org/workflow_templates/"
    "0e5c5efb32ba6f3365d6da07da64aaf668157042/"
    "input/transparent_rgb_gaming_mouse.png"
)
I2V_INPUT_SIZE = 1_383_312
I2V_INPUT_SHA256 = "49696748d2fff0e8c9b63c7173c6d6282b70eac0195def5b39402f9564410e75"


def load_runtime():
    path = Path(__file__).with_name("runtime.py")
    spec = importlib.util.spec_from_file_location("launcher_runtime_for_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load_runtime()


class RenderError(Exception):
    """A safe diagnostic that does not embed prompts, server logs, or secrets."""

    def __init__(self, message, *, server_error=None, http_status=None):
        super().__init__(message)
        self.server_error = server_error
        self.http_status = http_status


def safe_server_error(value):
    """Keep node diagnostics, excluding echoed prompt values and credentials."""
    omitted = {
        "prompt",
        "current_inputs",
        "current_outputs",
        "input_value",
        "received_value",
    }
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if str(key).lower() in omitted
                or re.search(r"(?i)token|secret|password|authorization", str(key))
                else safe_server_error(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [safe_server_error(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"https?://[^\s\"'<>]+", "[redacted URL]", value)
        return re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", value)[:4000]
    return value


def durable_json(path, value):
    """Publish a private record only after its bytes and directory are synced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RenderError("Render state must not be a symlink")
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".tmp-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RequestJournal:
    """One durable request identity and one runner, including after a crash."""

    def __init__(self, directory, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", request_id
        ):
            raise RenderError(
                "request-id must contain 1–128 letters, digits, underscores or hyphens"
            )
        self.directory = Path(directory)
        self.path = self.directory / (request_id + ".json")
        self.lock = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or self.path.is_symlink():
            raise RenderError("Render job directory and records must not be symlinks")
        self.lock = os.open(
            self.path.with_suffix(".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            self.lock = None
            raise RenderError(
                "This request already has an active runner; query its recorded status"
            ) from None
        return self

    def read(self):
        if self.directory.is_symlink() or self.path.is_symlink():
            raise RenderError("Render job directory and records must not be symlinks")
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise RenderError(
                    "Render request record is unreadable; submission is blocked"
                )
            return value
        except (OSError, ValueError):
            raise RenderError(
                "Render request record is unreadable; submission is blocked"
            ) from None

    def __exit__(self, *_args):
        if self.lock is not None:
            os.close(self.lock)


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
        self.journal = None

    def update(self, **fields):
        self.value.update(fields)
        self.value["elapsed_seconds"] = round(time.monotonic() - self.start, 3)
        if self.journal is not None:
            durable_json(self.journal.path, self.value)
        durable_json(self.path, self.value)


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
        try:
            details = json.loads(error.read(1024 * 1024))
        except (OSError, ValueError):
            details = None
        raise RenderError(
            f"ComfyUI request failed (HTTP {error.code})",
            server_error=safe_server_error(details),
            http_status=error.code,
        ) from None
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


def configured_workflow(args):
    """Apply an optional versioned mapping; raw API JSON always remains valid."""
    workflow, file_sha = load_workflow(args.workflow)
    source_sha = hashlib.sha256(
        json.dumps(
            workflow, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    mapping_path = getattr(args, "mapping", None)
    parameters = getattr(args, "param", None) or []
    metadata = {"source_sha256": source_sha, "source_file_sha256": file_sha}
    if parameters and not mapping_path:
        raise RenderError("Named parameters require an explicit workflow mapping")
    if mapping_path:
        try:
            mapping_data = Path(mapping_path).read_bytes()
            mapping = json.loads(mapping_data)
        except (OSError, ValueError):
            raise RenderError("Cannot read a valid workflow mapping") from None
        if (
            not isinstance(mapping, dict)
            or mapping.get("schema_version") != 1
            or not isinstance(mapping.get("workflow_version"), str)
            or not mapping["workflow_version"]
            or mapping.get("workflow_sha256") != source_sha
            or not isinstance(mapping.get("parameters"), dict)
        ):
            raise RenderError(
                "Mapping version, workflow hash or parameter declarations do not match"
            )
        metadata.update(
            workflow_version=mapping["workflow_version"],
            mapping_sha256=hashlib.sha256(mapping_data).hexdigest(),
            mode=mapping.get("mode"),
        )
        values = {}
        for parameter in parameters:
            name, separator, raw = parameter.partition("=")
            if not separator or name in values or name not in mapping["parameters"]:
                raise RenderError(
                    "Named parameters must be unique declared NAME=JSON values"
                )
            try:
                value = json.loads(raw, parse_constant=reject_nonfinite)
            except ValueError:
                raise RenderError("Named parameter value is not finite JSON") from None
            values[name] = value
        for name, definition in mapping["parameters"].items():
            if not isinstance(definition, dict):
                raise RenderError("Workflow mapping parameter declaration is invalid")
            node = workflow.get(definition.get("node_id"))
            field = definition.get("input")
            if (
                not isinstance(node, dict)
                or node.get("class_type") != definition.get("class_type")
                or not isinstance(node.get("inputs"), dict)
                or not isinstance(field, str)
                or field not in node["inputs"]
            ):
                raise RenderError(
                    "Workflow mapping target does not match its declared node and input"
                )
            if name not in values:
                if definition.get("required") is True:
                    raise RenderError("A required named workflow parameter is missing")
                continue
            value = values[name]
            kind = definition.get("type")
            valid = {
                "string": isinstance(value, str),
                "integer": type(value) is int,
                "number": type(value) in (int, float) and math.isfinite(value),
                "boolean": type(value) is bool,
            }.get(kind, False)
            if not valid:
                raise RenderError("Named workflow parameter has an invalid type")
            if kind in ("integer", "number") and (
                ("minimum" in definition and value < definition["minimum"])
                or ("maximum" in definition and value > definition["maximum"])
            ):
                raise RenderError(
                    "Named workflow parameter is outside its declared bounds"
                )
            if "choices" in definition and value not in definition["choices"]:
                raise RenderError("Named workflow parameter is not an allowed choice")
            node["inputs"][field] = value
        metadata["applied_parameters"] = sorted(values)
    canonical = json.dumps(
        workflow, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return workflow, hashlib.sha256(canonical).hexdigest(), metadata


def reject_nonfinite(_value):
    raise ValueError("Nonfinite JSON numbers are unsupported")


def uses_example_input(workflow):
    return any(
        isinstance(node, dict)
        and node.get("class_type") == "LoadImage"
        and isinstance(node.get("inputs"), dict)
        and node["inputs"].get("image") == I2V_INPUT_NAME
        for node in workflow.values()
    )


def verify_example_input(path, deadline):
    """Read only the dedicated regular fixture; never repair or replace it."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != I2V_INPUT_SIZE:
                raise RenderError("Existing I2V example image has an unexpected size")
            digest, size = hash_stream(stream, deadline)
    except OSError:
        raise RenderError("I2V example image is not a readable regular file") from None
    if size != I2V_INPUT_SIZE or digest != I2V_INPUT_SHA256:
        raise RenderError("Existing I2V example image failed SHA256 verification")
    return {
        "filename": I2V_INPUT_NAME,
        "bytes": size,
        "sha256": digest,
        "verification": "sha256",
        "action": "reused",
    }


def prepare_example_input(workflow, output_root, deadline):
    """Prepare the pinned template asset only when its reserved name is used."""
    if not uses_example_input(workflow):
        return None
    deadline.remaining()
    root = Path(output_root).parent.resolve()
    input_root = root / "input"
    if input_root.is_symlink() or not input_root.resolve().is_relative_to(root):
        raise RenderError("Owned input directory is a symlink or escapes storage")
    try:
        input_root.mkdir(exist_ok=True)
    except OSError:
        raise RenderError("Owned input directory is unavailable") from None
    path = input_root / I2V_INPUT_NAME
    if path.is_symlink():
        raise RenderError("I2V example image must not be a symlink")
    if path.exists():
        return verify_example_input(path, deadline)
    temporary = None
    reservation = None
    try:
        descriptor, filename = tempfile.mkstemp(
            dir=input_root, prefix=".launcher-h3-i2v-", suffix=".partial"
        )
        temporary = Path(filename)
        digest = hashlib.sha256()
        size = 0
        request = urllib.request.Request(
            I2V_INPUT_URL, headers={"Accept-Encoding": "identity"}
        )
        with (
            os.fdopen(descriptor, "wb") as target,
            urllib.request.urlopen(
                request, timeout=min(15, deadline.remaining())
            ) as response,
        ):
            while True:
                deadline.remaining()
                chunk = response.read(min(CHUNK_BYTES, I2V_INPUT_SIZE - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > I2V_INPUT_SIZE:
                    raise RenderError(
                        "Downloaded I2V example image exceeds pinned size"
                    )
                digest.update(chunk)
                target.write(chunk)
            if size != I2V_INPUT_SIZE or digest.hexdigest() != I2V_INPUT_SHA256:
                raise RenderError(
                    "Downloaded I2V example image failed size/SHA256 verification"
                )
            target.flush()
            os.fsync(target.fileno())
        deadline.remaining()
        # O_EXCL protects existing filenames without relying on Drive hardlinks.
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            return verify_example_input(path, deadline)
        with os.fdopen(descriptor, "wb") as reserved:
            metadata = os.fstat(reserved.fileno())
            reservation = (metadata.st_dev, metadata.st_ino)
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != reservation:
            raise RenderError("I2V example image changed before publication")
        os.replace(temporary, path)
        temporary = None
        reservation = None
        return {
            "filename": I2V_INPUT_NAME,
            "bytes": size,
            "sha256": digest.hexdigest(),
            "verification": "sha256",
            "action": "downloaded",
        }
    except (urllib.error.URLError, OSError):
        raise RenderError("Cannot prepare the pinned I2V example image") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if reservation is not None:
            with contextlib.suppress(OSError):
                metadata = path.lstat()
                if (metadata.st_dev, metadata.st_ino) == reservation:
                    path.unlink()


def autogrow_input_schema(descriptor, prefix):
    """Match the pinned V3 Autogrow names/prefix templates, without a parent value."""
    metadata = descriptor[1] if len(descriptor) > 1 else None
    template = metadata.get("template") if isinstance(metadata, dict) else None
    if not isinstance(template, dict):
        raise RenderError("ComfyUI returned an unsupported Autogrow schema")
    names = template.get("names")
    if names is None:
        name_prefix = template.get("prefix")
        maximum = template.get("max")
        if (
            not isinstance(name_prefix, str)
            or type(maximum) is not int
            or not 1 <= maximum <= 100
        ):
            raise RenderError("ComfyUI returned an unsupported Autogrow template")
        names = [f"{name_prefix}{index}" for index in range(maximum)]
    minimum = template.get("min")
    if (
        not isinstance(names, list)
        or len(names) > 100
        or not all(isinstance(name, str) and name for name in names)
        or len(set(names)) != len(names)
        or type(minimum) is not int
        or not 0 <= minimum <= len(names)
        or not isinstance(template.get("input"), dict)
    ):
        raise RenderError("ComfyUI returned an unsupported Autogrow template")
    child = None
    template_required = False
    for category in ("required", "optional"):
        fields = template["input"].get(category, {})
        if not isinstance(fields, dict):
            raise RenderError("ComfyUI returned an unsupported Autogrow input")
        if fields:
            child = next(iter(fields.values()))
            template_required = category == "required"
            break
    if not isinstance(child, (list, tuple)) or not child:
        raise RenderError("ComfyUI returned an unsupported Autogrow input")
    result = {"required": {}, "optional": {}}
    for index, name in enumerate(names):
        category = "required" if template_required and index < minimum else "optional"
        result[category][prefix + name] = child
    return result


def finalized_input_schema(schema, inputs, prefix=""):
    """Expand the pinned V3 DynamicCombo and Autogrow API dot-separated inputs."""
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
            if descriptor[0] == "COMFY_AUTOGROW_V3":
                children = autogrow_input_schema(descriptor, full_name + ".")
                for child_category, declarations in finalized.items():
                    declarations.update(children[child_category])
                continue
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
    output_nodes = []
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
        if (
            kind
            in (
                "SaveVideo",
                "SaveImage",
                "SaveAnimatedWEBP",
                "SaveAnimatedPNG",
                "SaveAudio",
            )
            or info[kind].get("output_node") is True
        ):
            save_nodes.append(node_id)
        if node_id in save_nodes:
            output_nodes.append(node_id)
    if not save_nodes:
        raise RenderError("Workflow must contain a declared output node")

    # Official UI exports include disconnected draft nodes. ComfyUI checks all
    # node classes, but validates inputs only for ancestors of output nodes.
    reachable = set()
    pending = output_nodes[:]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        for value in workflow[node_id]["inputs"].values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and type(value[1]) is int
            ):
                if value[0] not in workflow:
                    raise RenderError("Workflow has an invalid node output link")
                pending.append(value[0])

    for node_id, node in workflow.items():
        if node_id not in reachable:
            continue
        kind = node["class_type"]
        inputs = node["inputs"]
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
                if (
                    kind in ("LoadImage", "LoadImageMask")
                    and name == "image"
                    and isinstance(value, str)
                ):
                    # Some versions list only root filenames, while their
                    # authoritative validate_inputs accepts uploaded subfolders.
                    continue
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
            # COMFY_MATCHTYPE_V3 (e.g. lazy Switch), wildcard and custom node
            # types are authoritative on the server. Do not rewrite the graph
            # or infer a generic output's type from one selected branch.
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
            detail = (
                message[1] if len(message) > 1 and isinstance(message[1], dict) else {}
            )
            raise RenderError(
                f"Workflow ended with {message[0]}",
                server_error={
                    "type": message[0],
                    **{
                        key: safe_server_error(detail[key])
                        for key in ("node_id", "node_type", "exception_type")
                        if key in detail
                    },
                },
            )
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


def output_assets(history, save_nodes, output_root, *, allow_empty=False):
    outputs = history.get("outputs", {})
    if not isinstance(outputs, dict):
        raise RenderError("Workflow history has no valid outputs")
    assets = []
    seen = set()
    for node_id in save_nodes:
        node = outputs.get(node_id, {})
        if not isinstance(node, dict):
            raise RenderError("Output node history is malformed")
        for key in ("images", "videos", "gifs", "audio", "files"):
            entries = node.get(key, [])
            if not isinstance(entries, list):
                raise RenderError("Output asset list is malformed")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise RenderError("Output asset record is malformed")
                filename = entry.get("filename")
                subfolder = entry.get("subfolder", "")
                if not isinstance(filename, str) or not isinstance(subfolder, str):
                    raise RenderError("Output asset has invalid filenames")
                if entry.get("type") != "output":
                    raise RenderError("Saved file is not an output asset")
                if (
                    filename in ("", ".", "..")
                    or "/" in filename
                    or "\\" in filename
                    or any(ord(character) < 32 for character in filename)
                ):
                    raise RenderError("Saved filename escapes its output directory")
                if (
                    "\\" in subfolder
                    or (subfolder and str(PurePosixPath(subfolder)) != subfolder)
                    or PurePosixPath(subfolder).is_absolute()
                    or any(
                        part in (".", "..") for part in PurePosixPath(subfolder).parts
                    )
                ):
                    raise RenderError("Saved subfolder escapes its output directory")
                path = (output_root / subfolder / filename).resolve()
                if not path.is_relative_to(output_root) or not path.is_file():
                    raise RenderError(
                        "Saved output is missing or outside owned storage"
                    )
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
    if not assets and not allow_empty:
        raise RenderError("Completed workflow produced no saved output asset")
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
        raise RenderError("Saved output could not be retrieved through /view") from None
    if disk != served or not disk[1]:
        raise RenderError("API output bytes do not match the persisted storage file")
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
                "frames": frames,
                "fps": fps,
            }
    return None


def media_metadata(
    path, deadline, expected=None, *, export_frames=False, output_root=None
):
    """Decode video frames to measure the output; requested geometry is separate."""
    import av  # Installed in the ComfyUI environment; no model loading occurs here.

    videos, audio, previews = [], [], []
    output_root = Path(output_root or path.parent).resolve()
    with av.open(str(path)) as container:
        targets = [
            (stream.type, stream.index)
            for stream in container.streams
            if stream.type in ("video", "audio")
        ]
        if not any(kind == "video" for kind, _ in targets):
            raise RenderError("Saved video has no video track")
    for kind, index in targets:
        deadline.remaining()
        with av.open(str(path)) as container:
            stream = container.streams[index]
            decoder = iter(container.decode(stream))
            first = next(decoder, None)
            if first is None:
                raise RenderError("A media track has no decodable first frame")
            declared_duration = (
                float(stream.duration * stream.time_base)
                if stream.duration is not None and stream.time_base is not None
                else float(container.duration / 1000000)
                if container.duration
                else None
            )
            record = {
                "codec": stream.codec_context.name,
                "container_duration_seconds": declared_duration,
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
                count, last = 1, first
                first_time = getattr(first, "time", None)
                for frame in decoder:
                    deadline.remaining()
                    if frame.width != first.width or frame.height != first.height:
                        raise RenderError(
                            "Saved video changes dimensions within a stream"
                        )
                    count += 1
                    last = frame
                last_time = getattr(last, "time", None)
                frame_duration = getattr(last, "duration", None)
                frame_time_base = getattr(last, "time_base", None)
                tail = (
                    float(frame_duration * frame_time_base)
                    if frame_duration and frame_time_base
                    else 1 / fps
                )
                timestamps = (
                    type(first_time) in (int, float)
                    and type(last_time) in (int, float)
                    and math.isfinite(first_time)
                    and math.isfinite(last_time)
                    and last_time >= first_time
                )
                duration = last_time - first_time + tail if timestamps else count / fps
                if not math.isfinite(duration) or duration <= 0:
                    raise RenderError("Saved video has no positive decoded duration")
                record.update(
                    width=first.width,
                    height=first.height,
                    fps=round(fps, 6),
                    fps_source="stream_average_rate",
                    frame_count=count,
                    reported_frames=stream.frames,
                    all_frames_decoded=True,
                    duration_seconds=round(duration, 6),
                    duration_source="decoded_timestamps"
                    if timestamps
                    else "decoded_frames_and_average_rate",
                )
                if expected:
                    record["requested"] = expected
                    record["matches_requested"] = (
                        first.width == expected.get("width")
                        and first.height == expected.get("height")
                        and abs(fps - expected.get("fps", fps)) < 0.01
                        and ("frames" not in expected or count == expected["frames"])
                    )
                if export_frames:
                    directory = Path(
                        tempfile.mkdtemp(prefix="launcher-frames-", dir=output_root)
                    )
                    for label, frame in (("first", first), ("last", last)):
                        deadline.remaining()
                        destination = directory / f"stream-{index}-{label}.png"
                        frame.to_image().save(str(destination), format="PNG")
                        with destination.open("rb") as contents:
                            checksum, size = hash_stream(contents, deadline)
                        previews.append(
                            {
                                "position": label,
                                "stream_index": index,
                                "filename": destination.name,
                                "relative_path": destination.relative_to(
                                    output_root
                                ).as_posix(),
                                "sha256": checksum,
                                "bytes": size,
                            }
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
                    duration_seconds=declared_duration,
                )
                audio.append(record)
    result = {"video": videos, "audio": audio}
    if previews:
        result["frames"] = previews
    return result


def _request_match(job, record):
    if (
        not isinstance(job, (list, tuple))
        or len(job) < 4
        or not isinstance(job[3], dict)
    ):
        return False
    metadata = job[3]
    return (
        metadata.get("launcher_request_id") == record["request_id"]
        and metadata.get("launcher_fingerprint") == record["request_fingerprint"]
    )


def reconcile_submission(record, deadline):
    """Absence is inconclusive: server queues/history can be lost or evicted."""
    matches = set()
    expected = record.get("prompt_id") or record.get("submission_prompt_id")
    if expected:
        history = http_json("/history/" + expected, deadline)
        item = history.get(expected)
        if isinstance(item, dict) and _request_match(item.get("prompt"), record):
            matches.add(expected)
    queue = http_json("/queue", deadline)
    for key in ("queue_running", "queue_pending"):
        entries = queue.get(key, [])
        if not isinstance(entries, list):
            raise RenderError("ComfyUI queue status is malformed")
        for job in entries:
            if _request_match(job, record):
                matches.add(job[1])
    # The server may have generated a different ID on an older installation.
    # Search retained history by our durable identity, without inferring absence.
    if not matches:
        history = http_json("/history", deadline)
        for prompt_id, item in history.items():
            if isinstance(item, dict) and _request_match(item.get("prompt"), record):
                matches.add(prompt_id)
    if len(matches) > 1:
        raise RenderError(
            "Multiple server jobs match this request; submission remains blocked"
        )
    if not matches:
        return None
    prompt_id = next(iter(matches))
    if not isinstance(prompt_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", prompt_id
    ):
        raise RenderError("ComfyUI returned an invalid recovered prompt_id")
    return prompt_id


def record_failure(report, error):
    unknown = report.value.get("status") in ("submitting", "submission_unknown")
    message = (
        str(error)
        if isinstance(error, RenderError)
        else (
            "Render interrupted"
            if isinstance(error, KeyboardInterrupt)
            else f"Render failed ({type(error).__name__})"
        )
    )
    fields = {
        "ok": False,
        "status": "submission_unknown" if unknown else "failed",
        "error": message,
    }
    if isinstance(error, RenderError):
        if error.server_error is not None:
            fields["server_error"] = error.server_error
        if error.http_status is not None:
            fields["http_status"] = error.http_status
    if unknown:
        fields.update(job_may_still_be_running=True, resubmit_allowed=False)
    if report.value.get("terminal"):
        fields["status"] = report.value["status"]
    report.update(**fields)


def _render_request(args, report, workflow, deadline):
    state = runtime.read_json(runtime.STATE)
    owned = state.get("comfyui")
    if not runtime.alive(owned):
        raise RenderError("Owned ComfyUI service is not alive")
    output_root = owned_storage(state)
    attempted = report.value.get("submission_attempted") is True
    if attempted:
        prompt_id = report.value.get("prompt_id")
        if not prompt_id:
            report.update(
                status="submission_unknown",
                job_may_still_be_running=True,
                resubmit_allowed=False,
            )
            prompt_id = reconcile_submission(report.value, deadline)
            if not prompt_id:
                raise RenderError(
                    "Submission outcome is unknown; no retained matching job was found and no POST was repeated"
                )
            report.update(
                status="queued", prompt_id=prompt_id, reconciled=True, error=None
            )
        save_nodes = report.value["output_nodes"]
    else:
        if uses_example_input(workflow):
            report.update(status="preparing_input")
            report.update(
                example_input=prepare_example_input(workflow, output_root, deadline)
            )
        http_json("/system_stats", deadline)
        save_nodes = validate_workflow(workflow, http_json("/object_info", deadline))
        known_savers = {
            "SaveVideo",
            "SaveImage",
            "SaveAnimatedWEBP",
            "SaveAnimatedPNG",
            "SaveAudio",
        }
        report.update(
            requested_geometry=expected_geometry(workflow),
            require_assets=any(
                workflow[node]["class_type"] in known_savers for node in save_nodes
            ),
        )
        submission_id = str(uuid.uuid4())
        # Once this intent is durable, every path after a crash is read-only
        # reconciliation. Reusing a prompt_id does NOT make ComfyUI POST idempotent.
        report.update(
            status="submitting",
            submission_attempted=True,
            submission_started_at=time.time(),
            submission_prompt_id=submission_id,
            output_nodes=save_nodes,
            resubmit_allowed=False,
            storage="ephemeral" if state["ephemeral"] else "google-drive",
        )
        payload = {
            "client_id": report.value["request_id"],
            "prompt_id": submission_id,
            "prompt": workflow,
            "extra_data": {
                "launcher_request_id": report.value["request_id"],
                "launcher_fingerprint": report.value["request_fingerprint"],
                "launcher_workflow_sha256": report.value["workflow_sha256"],
                "launcher_assignment_id": report.value.get("assignment_id"),
            },
        }
        try:
            submitted = http_json("/prompt", deadline, payload)
        except RenderError as error:
            if error.http_status is not None and 400 <= error.http_status < 500:
                report.update(
                    status="rejected", terminal=True, job_may_still_be_running=False
                )
                raise
            report.update(status="submission_unknown", job_may_still_be_running=True)
            try:
                prompt_id = reconcile_submission(report.value, deadline)
            except RenderError:
                prompt_id = None
            if not prompt_id:
                raise
            submitted = {"prompt_id": prompt_id}
            report.update(reconciled=True)
        prompt_id = submitted.get("prompt_id")
        if not isinstance(prompt_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", prompt_id
        ):
            if submitted.get("error"):
                report.update(
                    status="rejected", terminal=True, job_may_still_be_running=False
                )
                raise RenderError(
                    "ComfyUI rejected the submitted workflow",
                    server_error=safe_server_error(submitted),
                )
            raise RenderError(
                "ComfyUI returned no valid prompt_id; submission outcome is unknown",
                server_error=safe_server_error(submitted),
            )
        # ComfyUI can enqueue valid outputs while reporting other node errors.
        # Persist the accepted identity and diagnostics together before polling.
        queued = {
            "status": "queued",
            "prompt_id": prompt_id,
            "job_may_still_be_running": True,
        }
        if submitted.get("node_errors") or submitted.get("error"):
            queued["submission_warnings"] = safe_server_error(submitted)
        report.update(**queued)
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
            try:
                job_error(history)
            except RenderError:
                report.update(
                    terminal=True, status="failed", job_may_still_be_running=False
                )
                raise
            status = history.get("status", {})
            if status.get("completed") is True:
                if status.get("status_str") != "success":
                    raise RenderError("Workflow history is complete without success")
                break
        phase, position = queue_phase(http_json("/queue", deadline), prompt_id)
        if phase != last_phase or time.monotonic() - last_report >= 5:
            report.update(status=phase, queue_position=position)
            last_phase, last_report = phase, time.monotonic()
        time.sleep(min(2, deadline.remaining()))
    return collect_result(args, report, history, output_root, deadline)


def collect_result(args, report, history, output_root, deadline):
    report.update(
        status="validating", job_may_still_be_running=False, queue_position=None
    )
    records = []
    expected = report.value.get("requested_geometry")
    require_assets = report.value.get("require_assets", True)
    save_nodes = report.value["output_nodes"]
    for path, asset in output_assets(
        history, save_nodes, output_root, allow_empty=not require_assets
    ):
        sha256, size = verify_api_bytes(path, asset, deadline)
        record = {
            "filename": asset["filename"],
            "subfolder": asset["subfolder"],
            "type": "output",
            "relative_path": path.relative_to(output_root).as_posix(),
            "sha256": sha256,
            "bytes": size,
            "assignment_id": report.value.get("assignment_id"),
            "session": report.value.get("session"),
        }
        if path.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv"):
            record.update(
                media_metadata(
                    path,
                    deadline,
                    expected,
                    export_frames=getattr(args, "export_frames", False),
                    output_root=output_root,
                )
            )
            for frame in record.get("frames", []):
                frame.update(
                    assignment_id=record["assignment_id"], session=record["session"]
                )
        records.append(record)
        records.extend(
            dict(frame, role="video_" + frame["position"] + "_frame", type="output")
            for frame in record.get("frames", [])
        )
    report.update(
        ok=True, status="succeeded", terminal=True, assets=records, error=None
    )
    return report.value


def render(args, report):
    workflow, workflow_sha, version = configured_workflow(args)
    request_id = getattr(args, "request_id", None) or uuid.uuid4().hex
    identity = {
        "workflow_sha256": workflow_sha,
        "assignment_id": getattr(args, "assignment_id", None),
        "session": getattr(args, "session", None),
        "export_frames": bool(getattr(args, "export_frames", False)),
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    directory = getattr(args, "job_dir", None) or report.path.parent / "render-jobs"
    with RequestJournal(directory, request_id) as journal:
        old = journal.read()
        if old and old.get("request_fingerprint") != fingerprint:
            raise RenderError(
                "request-id already belongs to a different workflow or instance"
            )
        report.journal = journal
        try:
            if old:
                report.value = old
                report.update(replayed=True)
                if old.get("terminal"):
                    return report.value
            else:
                report.update(
                    request_id=request_id,
                    job_id=request_id,
                    **identity,
                    request_fingerprint=fingerprint,
                    workflow_version=version,
                    created_at=time.time(),
                    submission_attempted=False,
                )
            with time_budget(args.max_seconds) as deadline:
                return _render_request(args, report, workflow, deadline)
        except (Exception, KeyboardInterrupt) as error:
            record_failure(report, error)
            raise
        finally:
            report.journal = None


def query_job(request_id, job_dir, max_seconds=20, *, result_file=None):
    """Inspect/reconcile one request without loading a workflow or issuing POST."""
    journal = RequestJournal(job_dir, request_id)
    old = journal.read()
    if not old:
        raise RenderError("No recorded render request has this request-id")

    def envelope(record, **extra):
        return {
            "ok": True,
            "request_id": request_id,
            "job_id": request_id,
            "status": record.get("status", "unknown"),
            "result": record,
            **extra,
        }

    if old.get("terminal") or old.get("submission_attempted") is not True:
        return envelope(old)
    try:
        journal.__enter__()
    except RenderError as error:
        if "active runner" in str(error):
            return envelope(journal.read(), query_state="runner_active")
        raise
    try:
        old = journal.read()
        if old.get("terminal"):
            return envelope(old)
        report = Report(result_file or journal.path)
        report.value = old
        report.journal = journal
        with time_budget(max_seconds) as deadline:
            state = runtime.read_json(runtime.STATE)
            if not runtime.alive(state.get("comfyui")):
                raise RenderError(
                    "Owned ComfyUI service is not alive; retained job outcome is unchanged"
                )
            prompt_id = old.get("prompt_id") or reconcile_submission(old, deadline)
            if not prompt_id:
                report.update(
                    status="submission_unknown",
                    resubmit_allowed=False,
                    job_may_still_be_running=True,
                    last_query_at=time.time(),
                )
                return envelope(report.value, query_state="no_retained_match")
            report.update(
                prompt_id=prompt_id, reconciled=True, last_query_at=time.time()
            )
            history = http_json("/history/" + prompt_id, deadline).get(prompt_id)
            if isinstance(history, dict):
                try:
                    job_error(history)
                except RenderError as error:
                    report.update(
                        status="failed", terminal=True, job_may_still_be_running=False
                    )
                    record_failure(report, error)
                    return envelope(report.value)
                status = history.get("status", {})
                if (
                    status.get("completed") is True
                    and status.get("status_str") == "success"
                ):
                    args = argparse.Namespace(
                        export_frames=old.get("export_frames", False)
                    )
                    collect_result(
                        args, report, history, owned_storage(state), deadline
                    )
                    return envelope(report.value)
            phase, position = queue_phase(http_json("/queue", deadline), prompt_id)
            report.update(
                status=phase, queue_position=position, job_may_still_be_running=True
            )
            return envelope(report.value)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - query CLI boundary
        message = (
            str(error)
            if isinstance(error, RenderError)
            else f"Render query failed ({type(error).__name__})"
        )
        # Query failures never turn a previous unknown/running request into an
        # unsubmitted one. The journal remains available for the next query.
        return dict(
            envelope(journal.read()), ok=False, query_state="unavailable", error=message
        )
    finally:
        journal.__exit__()


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
    parser.add_argument(
        "--request-id",
        help="Stable identity; reuse to inspect/resume, never to resubmit",
    )
    parser.add_argument(
        "--job-dir", type=Path, help="Private durable render request directory"
    )
    parser.add_argument("--assignment-id", help="Owning Colab allocation identity")
    parser.add_argument("--session", help="Owning Colab session label")
    parser.add_argument(
        "--mapping", type=Path, help="Optional versioned named parameter mapping"
    )
    parser.add_argument("--param", action="append", default=[], metavar="NAME=JSON")
    parser.add_argument(
        "--export-frames",
        action="store_true",
        help="Save verified first and last video frames",
    )
    parser.add_argument(
        "--query-only",
        action="store_true",
        help="Read/reconcile request history without submitting",
    )
    report = None
    try:
        args = parser.parse_args(argv)
        if args.query_only:
            result = query_job(
                args.request_id,
                args.job_dir or args.result_file.parent / "render-jobs",
                args.max_seconds,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 1
        report = Report(args.result_file)
        report.update()
        result = render(args, report)
    # This CLI boundary must emit failed JSON for decoder and unexpected I/O errors.
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001
        result = {
            "ok": False,
            "status": "failed",
            "error": str(error) if isinstance(error, RenderError) else "Render failed",
        }
        if report:
            try:
                record_failure(report, error)
                result = report.value
            except (OSError, ValueError, TypeError):
                result = dict(report.value, ok=False)
                result["result_file_written"] = False
    # A timeout leaves the server's job subject to the caller's stop/cleanup policy.
    # Only the local runner exits; this program never unassigns a Colab session.
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] is True else 1


if __name__ == "__main__":
    sys.exit(main())
