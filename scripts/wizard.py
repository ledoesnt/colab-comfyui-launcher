"""Beginner startup flow, rendered as one terminal frame with no TTY handoff.

The worker owns every provider operation. This module owns only navigation,
explicit confirmation, and transient display of provider authentication.
"""

from __future__ import annotations

import curses
import queue
import select
import sys
import textwrap
import time
import uuid
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dashboard import (
    DEFAULT_IDENTITY,
    REFRESH_SECONDS,
    Config,
    DashboardError,
    Theme,
    Worker,
    clip_cells,
    human_bytes,
    safe_error,
    services_ready,
)

GPU_CHOICES = ("G4", "H100", "A100", "L4", "T4")
STEPS = (
    ("session", "Session"),
    ("hardware", "Compute"),
    ("storage", "Storage"),
    ("mount", "Drive authorization"),
    ("installation", "ComfyUI environment"),
    ("models", "Local models"),
    ("startup", "ComfyUI + SSH"),
)


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    detail: str = ""


class Wizard:
    """A nonblocking UI. Arrow movement never invokes a provider operation."""

    def __init__(self, config: Config, backend: Any, worker: Any = None):
        self.config = replace(config)
        self.backend = backend
        self.worker = worker if worker is not None else Worker(backend)
        self.theme = Theme()
        self.demo = backend.__class__.__name__ == "DemoBackend"
        self.page = "home"
        self.selected = 0
        self.detail_offset = 0
        self.status: dict[str, Any] = {}
        self.ssh: dict[str, Any] = {}
        self.provider: dict[str, Any] = {}
        self.sessions: list[dict[str, str]] = []
        self.stage = "Choose how to start"
        self.notice = "Choose a new runtime, or inspect an existing one."
        self.error = ""
        self.updated = 0.0
        self.last_refresh = 0.0
        self.resume = False
        self.auth: dict[str, Any] = {}
        self.input_value = ""
        self.input_field = ""
        self.input_return = "summary"
        self.last_operation = ""
        self.quitting = False
        if self.demo:
            self.config.session = self.config.session or "offline-demo"
            self.status = backend.bridge(self.config, "status")
            self.ssh = backend.ssh(self.config, "status")
            self.updated = time.monotonic()
            state = getattr(backend, "state", "prepare")
            self.page = "ready" if state == "ready" else "pipeline"
            if state == "error":
                self.page = "failure"
                self.error = "Offline fixture: model checksum mismatch."
            self.stage = "Offline demonstration; no provider commands"
        elif self.config.session:
            self.resume = True
            self.page = "inspect"
            self._submit("inspect")

    def _page(self, page: str, selected: int = 0) -> None:
        self.page, self.selected = page, selected
        self.detail_offset = 0
        self.input_value = ""

    def _submit(self, action: str, config: Config | None = None) -> bool:
        if self.demo:
            self.notice = (
                "Offline demo: creating, changing or releasing resources is disabled."
            )
            return False
        target = config if config is not None else self.config
        if self.worker.submit(action, target):
            self.last_operation = action
            self.error = ""
            return True
        self.notice = "An operation is running; no additional operation was submitted."
        return False

    def _ready(self) -> bool:
        if not services_ready(self.config, self.status):
            return False
        if self.config.access == "local-only":
            return (
                self.ssh.get("running") is True
                and self.ssh.get("http_ready") is True
                and bool(self.ssh.get("url"))
            )
        return self.status.get("tunnel_alive") is True and bool(self.status.get("url"))

    def _url(self) -> str | None:
        if not self._ready():
            return None
        value = (
            self.ssh.get("url")
            if self.config.access == "local-only"
            else self.status.get("url")
        )
        return str(value) if value else None

    def _choices(self) -> list[Choice]:
        if self.page == "home":
            return [
                Choice(
                    "new",
                    "Create a new runtime",
                    "Choose compute and storage before creation.",
                ),
                Choice(
                    "existing",
                    "View existing runtimes",
                    "Inspect an existing runtime before continuing.",
                ),
                Choice(
                    "quit",
                    "Exit and keep resources",
                    "Exiting this UI does not stop any runtime.",
                ),
            ]
        if self.page == "hardware":
            return [
                Choice(
                    "gpu",
                    "GPU · render video",
                    "Next: choose a GPU model. G4 has been tested.",
                ),
                Choice(
                    "cpu",
                    "CPU · test the web editor",
                    "Skip H3 weights; install ComfyUI and test PNG output.",
                ),
            ]
        if self.page == "gpu":
            return [
                Choice(
                    gpu,
                    gpu
                    + (" · tested configuration" if gpu == "G4" else " · not verified"),
                    "The CLI accepts this model; allocation depends on your account. Only G4 has been tested with this H3 profile.",
                )
                for gpu in GPU_CHOICES
            ]
        if self.page == "storage":
            return [
                Choice(
                    "drive",
                    "Google Drive · persistent",
                    "Keep models and outputs on Drive; copy verified models to each VM.",
                ),
                Choice(
                    "ephemeral",
                    "VM disk · temporary",
                    "Skip Drive. Download directly to VM disk. Models and outputs disappear when the VM is released.",
                ),
            ]
        if self.page == "summary":
            choices = [
                Choice(
                    "start",
                    "Continue this runtime" if self.resume else "Confirm and create",
                    "Enter starts only the selected runtime. Arrow keys do not create anything.",
                ),
                Choice(
                    "settings",
                    "SSH and storage settings",
                    "Reuse the dedicated key and local port unless they need changing.",
                ),
            ]
            if self.config.session:
                choices.append(
                    Choice(
                        "advanced",
                        "Advanced actions",
                        "Inspect, stop or manage individual steps before continuing.",
                    )
                )
            choices.append(
                Choice(
                    "back",
                    "Back",
                    "Return to storage selection without starting a command.",
                )
            )
            return choices
        if self.page == "settings":
            choices = [
                Choice("port", "Local SSH port"),
                Choice("identity", "Dedicated SSH key path"),
            ]
            if not self.config.ephemeral:
                choices.append(Choice("storage_root", "Dedicated Drive directory"))
            choices.append(Choice("back", "Return to configuration summary"))
            return choices
        if self.page == "key_confirm":
            return [
                Choice(
                    "create_key",
                    "Create a dedicated SSH key",
                    "Create this key on SSH startup only if absent. Existing keys are never overwritten.",
                ),
                Choice("choose_key", "Choose an existing key path"),
                Choice("back", "Back to summary"),
            ]
        if self.page == "sessions":
            choices = [
                Choice(
                    "session:" + row["name"],
                    row["name"],
                    f"{row['hardware']} · {row.get('status', 'provider status')} · Enter inspects this runtime; it does not stop the current runtime.",
                )
                for row in self.sessions
            ]
            choices.extend(
                [
                    Choice("refresh_list", "Refresh runtime list"),
                    Choice("back", "Back to start"),
                ]
            )
            return choices
        if self.page == "ready":
            return [
                Choice(
                    "open",
                    "Open ComfyUI in browser",
                    self._url() or "The local connection must be ready first.",
                ),
                Choice(
                    "quit",
                    "Exit and keep resources",
                    "VM, services, local SSH and running tasks remain running.",
                ),
                Choice(
                    "release",
                    "End this VM",
                    "Review the selected runtime before releasing it.",
                ),
                Choice(
                    "existing",
                    "View other runtimes",
                    "The currently selected runtime keeps running.",
                ),
                Choice(
                    "advanced",
                    "Advanced actions",
                    "Inspect or manage individual steps when needed.",
                ),
            ]
        if self.page == "release_confirm":
            return [
                Choice(
                    "confirm_release",
                    "Confirm: end the selected VM",
                    "Stop its owned SSH and services, release this VM, and verify the result. Drive cache and the SSH key remain.",
                ),
                Choice("cancel_release", "Cancel and keep it running"),
            ]
        if self.page == "advanced":
            return [
                Choice("inspect", "Inspect current runtime"),
                Choice("mount", "Authorize / check Google Drive"),
                Choice("smoke", "Run a PNG smoke test"),
                Choice("render", "Render the H3 API test"),
                Choice("stop", "Stop services · retain VM"),
                Choice("ssh_start", "Start local SSH forwarding"),
                Choice("ssh_stop", "Stop local SSH forwarding"),
                Choice("wizard_resume", "Continue missing startup steps"),
                Choice("back", "Return to runtime overview"),
            ]
        if self.page == "failure":
            choices = []
            if self.config.session:
                choices.append(
                    Choice(
                        "inspect",
                        "Inspect before retrying",
                        "Check real state first; a timeout never triggers duplicate creation.",
                    )
                )
                choices.append(
                    Choice(
                        "advanced",
                        "Advanced actions",
                        "Inspect or stop existing services before changing their configuration.",
                    )
                )
                choices.append(Choice("release", "End the selected VM"))
            choices.extend(
                [
                    Choice("existing", "View runtimes"),
                    Choice("quit", "Exit and keep resources"),
                ]
            )
            return choices
        if self.page == "pipeline":
            choices = []
            if self.auth.get("url"):
                choices.append(Choice("auth_open", "Open authorization in browser"))
            if self.auth.get("waiting"):
                choices.append(
                    Choice(
                        "auth_code" if self.auth.get("needs_code") else "auth_continue",
                        "Enter provider authorization code"
                        if self.auth.get("needs_code")
                        else "I have authorized · continue",
                    )
                )
            if self.demo:
                choices.append(Choice("home", "Preview startup choices"))
            choices.append(
                Choice(
                    "quit",
                    "Exit and keep resources",
                    "Stop coordinating future steps; already running resources are retained.",
                )
            )
            if self.config.session and not self.demo:
                choices.append(Choice("release", "End the selected VM"))
            return choices
        return [Choice("quit", "Exit and keep resources")]

    def _new_name(self) -> str:
        hardware = (
            "cpu" if self.config.cpu else getattr(self.config, "gpu", "G4").lower()
        )
        return (
            "launcher-"
            + hardware
            + "-"
            + time.strftime("%Y%m%d-%H%M%S-")
            + uuid.uuid4().hex[:6]
        )

    def _begin(self) -> None:
        identity = Path(self.config.identity or DEFAULT_IDENTITY).expanduser()
        if (
            self.config.access == "local-only"
            and not identity.is_file()
            and not self.config.create_key
        ):
            self._page("key_confirm")
            self.notice = "A dedicated SSH key is required. Choose explicit creation or an existing key."
            return
        candidate = replace(self.config)
        if not self.resume:
            candidate.session = self._new_name()
        action = "wizard_resume" if self.resume else "wizard_create"
        if self._submit(action, candidate):
            self.config = candidate
            self._page("pipeline")
            self.notice = "Startup runs in the background. Completed steps will be verified and skipped."

    def _open(self, value: object) -> None:
        address = str(value or "")
        if urlsplit(address).scheme not in ("http", "https"):
            raise DashboardError("No verified browser address is available.")
        if self.demo:
            self.notice = "Offline demo: no browser is opened."
            return
        if not webbrowser.open(address):
            self.notice = "The browser did not accept the address. Open the displayed URL manually."

    def _input(self, field: str, return_page: str) -> None:
        self.input_field, self.input_return = field, return_page
        self._page("input")

    def _confirm_input(self) -> None:
        value = self.input_value.strip()
        field = self.input_field
        if field == "auth_code":
            if not value:
                self.error = "Enter the provider code, or Esc to cancel."
                return
            accepted = self.worker.send_auth(value)
            if accepted:
                self.auth.update(waiting=False, needs_code=False)
            self.input_value = ""
            self._page("pipeline")
            self.notice = (
                "Provider input submitted; waiting for verification."
                if accepted
                else "The provider is no longer waiting for input."
            )
            return
        candidate = replace(self.config)
        if field == "port" and value:
            if not value.isdecimal() or not 1024 <= int(value) <= 65535:
                self.error = "Use a port between 1024 and 65535."
                return
            candidate.local_port = int(value)
        elif field == "identity" and value:
            path = Path(value).expanduser()
            if not path.is_absolute() or any(
                char in str(path) for char in "\x00\r\n%$"
            ):
                self.error = (
                    "Use an absolute key path without control, % or $ characters."
                )
                return
            candidate.identity = str(path)
            candidate.create_key = False
        elif field == "storage_root" and value:
            path, drive = Path(value), Path("/content/drive/MyDrive")
            if (
                not path.is_absolute()
                or drive not in path.parents
                or ".." in path.parts
            ):
                self.error = "Use a dedicated directory inside /content/drive/MyDrive."
                return
            candidate.storage_root = value
        changed_ssh = candidate.local_port != self.config.local_port or Path(
            candidate.identity or DEFAULT_IDENTITY
        ) != Path(self.config.identity or DEFAULT_IDENTITY)
        if self.resume and changed_ssh and self.ssh.get("running") is not False:
            self.error = "Stop and verify the existing SSH forward before changing its port or key."
            return
        self.config = candidate
        self.error = ""
        self._page(self.input_return)

    def _activate(self, choice: Choice) -> bool:
        key = choice.key
        if key == "quit":
            self.quitting = True
            self.auth.clear()
            self.input_value = ""
            return False
        if key == "new":
            if self.worker.busy:
                self.notice = (
                    "Wait for the current operation before choosing another runtime."
                )
                return True
            self.resume = False
            self.config = replace(
                self.config, session="", access="local-only", email=""
            )
            self.status, self.ssh, self.provider = {}, {}, {}
            self._page("hardware")
        elif key in ("existing", "refresh_list"):
            if self.demo:
                self.sessions = [
                    {
                        "name": "offline-demo",
                        "hardware": "G4",
                        "status": "read-only fixture",
                    }
                ]
                self._page("sessions")
            elif self._submit("sessions"):
                self._page("listing")
                self.notice = (
                    "Listing runtimes. The previously selected runtime stays running."
                )
        elif self.page == "hardware":
            self.config.cpu = key == "cpu"
            self._page(
                "storage" if self.config.cpu else "gpu",
                int(self.config.ephemeral) if self.config.cpu else 0,
            )
        elif self.page == "gpu":
            self.config.gpu = key
            self._page("storage", int(self.config.ephemeral))
        elif self.page == "storage":
            self.config.ephemeral = key == "ephemeral"
            self._page("summary")
            self.notice = "Storage selected. Review the configuration, then continue the missing steps."
        elif self.page == "sessions" and key.startswith("session:"):
            name = key.removeprefix("session:")
            row = next(row for row in self.sessions if row["name"] == name)
            candidate = replace(
                self.config,
                session=name,
                cpu=row["hardware"] == "CPU",
                create_key=False,
            )
            if row["hardware"] in GPU_CHOICES:
                candidate.gpu = row["hardware"]
            if self._submit("inspect", candidate):
                self.config = candidate
                self.resume = True
                self.status, self.ssh = {}, {}
                self._page("inspect")
        elif key == "start":
            self._begin()
        elif key == "settings":
            self._page("settings")
        elif self.page == "settings" and key != "back":
            self._input(key, "settings")
        elif key == "create_key":
            self.config.create_key = True
            self._page("summary")
            self.notice = "Dedicated key creation is explicitly allowed; review the summary before starting."
        elif key == "choose_key":
            self._input("identity", "summary")
        elif key == "open":
            self._open(self._url())
        elif key == "auth_open":
            self._open(self.auth.get("url"))
        elif key == "auth_continue":
            accepted = self.worker.send_auth("")
            if accepted:
                self.auth.update(waiting=False, needs_code=False)
            self.notice = (
                "Provider input submitted; waiting for actual mount verification."
                if accepted
                else "The provider is no longer waiting for input."
            )
        elif key == "auth_code":
            self._input("auth_code", "pipeline")
        elif key == "release":
            self.input_return = self.page
            self._page("release_confirm", 1)
        elif key == "cancel_release":
            self._page(self.input_return)
        elif key == "confirm_release":
            if self._submit("release"):
                self._page("pipeline")
                self.notice = (
                    "Releasing only the selected runtime and its owned resources."
                )
        elif key == "advanced":
            self._page("advanced")
        elif key in (
            "inspect",
            "mount",
            "smoke",
            "render",
            "stop",
            "ssh_start",
            "ssh_stop",
            "wizard_resume",
        ):
            if self._submit(key):
                self._page("inspect" if key == "inspect" else "pipeline")
        elif key in ("back", "home"):
            self._back()
        return True

    def _back(self) -> None:
        parents = {
            "hardware": "home",
            "gpu": "hardware",
            "storage": "sessions"
            if self.resume
            else ("hardware" if self.config.cpu else "gpu"),
            "summary": "storage",
            "settings": "summary",
            "key_confirm": "summary",
            "sessions": "home",
            "advanced": "ready" if self._ready() else "summary",
        }
        if self.page in ("input", "release_confirm"):
            self._page(self.input_return)
        elif self.demo and self.page in ("pipeline", "ready", "failure"):
            self._page("home")
        elif self.page in parents:
            target = parents[self.page]
            self._page(target, int(self.config.ephemeral) if target == "storage" else 0)

    def _key(self, key: str) -> bool:
        if key in ("KEY_NPAGE", "KEY_PPAGE"):
            self.detail_offset = max(
                0, self.detail_offset + (8 if key == "KEY_NPAGE" else -8)
            )
            return True
        if key == "\x1b":
            self.error = ""
            self._back()
            return True
        if self.page == "input":
            if key in ("\r", "\n", "KEY_ENTER"):
                self._confirm_input()
            elif key in ("\b", "\x7f", "KEY_BACKSPACE"):
                self.input_value = self.input_value[:-1]
            elif len(key) == 1 and key.isprintable() and len(self.input_value) < 1024:
                self.input_value += key
            return True
        choices = self._choices()
        if key == "KEY_UP":
            self.selected = (self.selected - 1) % len(choices)
        elif key == "KEY_DOWN":
            self.selected = (self.selected + 1) % len(choices)
        elif key in ("\r", "\n", "KEY_ENTER"):
            return self._activate(choices[min(self.selected, len(choices) - 1)])
        return True

    def _events(self) -> None:
        while True:
            try:
                kind, value = self.worker.events.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                self.status, self.updated = value, time.monotonic()
            elif kind == "ssh":
                self.ssh = value
            elif kind == "config":
                self.config = replace(value)
            elif kind == "session":
                config, provider = value
                self.config, self.provider = replace(config), provider
            elif kind == "inspection":
                self.config = replace(value["config"])
                self.status, self.ssh = value.get("status", {}), value.get("ssh", {})
                self.provider, self.resume = value.get("provider", {}), True
                self.updated = time.monotonic()
                self.error = ""
                known_config = (
                    value.get("known_config", value.get("configuration_known", False))
                    is True
                )
                if known_config and value.get("ready") is True and self._ready():
                    self._page("ready")
                    self.notice = "Existing services and browser access were verified; no startup was repeated."
                elif known_config:
                    self._page("summary")
                    self.notice = "Saved configuration restored. Continue only the missing verified steps."
                else:
                    self._page("storage", int(self.config.ephemeral))
                    self.notice = "Runtime selected; storage configuration is not yet known. Choose storage before continuing."
            elif kind == "sessions":
                self.sessions = value
                self._page("sessions")
                self.notice = (
                    "Select a runtime to inspect it."
                    if value
                    else "No active named runtimes. Return and choose Create a new runtime."
                )
            elif kind == "stage":
                self.stage = str(value)
            elif kind == "notice":
                self.notice = str(value)
            elif kind == "auth":
                self.auth.update(value)
                if self.page != "input":
                    self._page("pipeline")
            elif kind == "auth_clear":
                self.auth.clear()
                if self.page == "input" and self.input_field == "auth_code":
                    self._page("pipeline")
            elif kind == "error":
                self.auth.clear()
                self.input_value = ""
                self.error = safe_error(value)
                self._page("failure")
            elif kind == "ssh_error":
                self.notice = "Local SSH status: " + safe_error(value)
            elif kind == "released":
                released, warning = value
                if released.session == self.config.session:
                    self.config = replace(self.config, session="", create_key=False)
                    self.status, self.ssh, self.provider = {}, {}, {}
                    self.auth.clear()
                    self.resume = False
                    self._page("home")
                    self.notice = "Selected VM released and verified." + (
                        " Cleanup warning: " + safe_error(warning) if warning else ""
                    )
            elif kind == "done":
                self.auth.clear()
                if value == "status" and self.page == "ready" and not self._ready():
                    self.resume = True
                    self._page("summary")
                    self.notice = "The connection is no longer verified ready. Inspect before continuing missing steps."
                if self.page == "pipeline" and value not in (
                    "status",
                    "sessions",
                    "release",
                    "inspect",
                ):
                    if self._ready():
                        self._page("ready")
                        self.notice = "ComfyUI and its local connection are ready."
                    else:
                        self.resume = True
                        self._page("summary")
                        self.notice = "The operation completed; inspect or continue any remaining startup steps."

    def _refresh(self) -> None:
        now = time.monotonic()
        if (
            self.page == "ready"
            and not self.demo
            and not self.worker.busy
            and now - self.last_refresh >= REFRESH_SECONDS
            and self.worker.submit("status", self.config)
        ):
            self.last_refresh = now

    def _title(self) -> str:
        return {
            "home": "Start a ComfyUI session",
            "hardware": "Choose compute",
            "gpu": "Choose GPU model",
            "storage": "Choose model and output storage",
            "summary": "Review before continuing"
            if self.resume
            else "Review before creating",
            "settings": "Connection settings",
            "key_confirm": "Choose an SSH key",
            "sessions": "Choose an existing runtime",
            "listing": "Loading runtime list",
            "inspect": "Inspecting selected runtime",
            "pipeline": "Preparing your ComfyUI session",
            "ready": "ComfyUI is ready",
            "failure": "An operation needs attention",
            "release_confirm": "Confirm VM release",
            "advanced": "Advanced runtime actions",
            "input": "Enter provider code"
            if self.input_field == "auth_code"
            else "Edit " + self.input_field.replace("_", " "),
        }.get(self.page, "ComfyUI startup")

    def _complete_steps(self) -> set[str]:
        complete = set()
        if self.config.session and (self.provider or self.status or self.resume):
            complete.update(("session", "hardware"))
            if self.page != "storage":
                complete.add("storage")
        drive = self.status.get("drive") or {}
        if (self.config.ephemeral and self.page != "storage") or (
            drive.get("mounted") is True and drive.get("mydrive_ready") is True
        ):
            complete.add("mount")
        if (self.status.get("installation") or {}).get("status") == "ready":
            complete.add("installation")
        if self.config.cpu or self.status.get("models_ready") is True:
            complete.add("models")
        if self._ready():
            complete.add("startup")
        return complete

    def _active_step(self) -> str:
        if self.page in ("hardware", "gpu"):
            return "hardware"
        if (
            self.page in ("storage", "summary", "settings", "key_confirm", "input")
            and not self.auth
        ):
            return "storage"
        if (
            self.auth
            or "mount" in self.stage.lower()
            or "drive authorization" in self.stage.lower()
            or "authorizing google drive" in self.stage.lower()
        ):
            return "mount"
        if any(
            value in self.stage.lower()
            for value in ("deploy", "install", "environment")
        ):
            return "installation"
        if any(
            value in self.stage.lower()
            for value in ("model", "copy", "download", "prepare")
        ):
            return "models"
        if self.page in ("pipeline", "ready"):
            return "startup"
        return "session"

    def _model_rows(self, width: int) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        tasks = (("model_prepare", "Prepare"), ("model_download", "Download"))
        displayed = set()
        for key, label in tasks:
            task = self.status.get(key) or {}
            progress = task.get("progress") or {}
            files = progress.get("files", []) if isinstance(progress, dict) else []
            files = [
                item
                for item in files
                if isinstance(item, dict)
                and str(item.get("path", "model")) not in displayed
            ]
            if not files:
                continue
            rows.append((f"{label} · {task.get('status', 'unknown')}", "accent"))
            for item in files:
                if not isinstance(item, dict):
                    continue
                displayed.add(str(item.get("path", "model")))
                rows.append((str(item.get("path", "model")), "normal"))
                done, total = item.get("done_bytes"), item.get("total_bytes")
                try:
                    fraction = (
                        min(1.0, max(0.0, float(done) / float(total)))
                        if float(total) > 0
                        else None
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    fraction = None
                size = max(4, min(22, width - 12))
                if fraction is None:
                    bar, percent = "-" * size, " ?%"
                else:
                    filled = int(size * fraction)
                    bar, percent = (
                        "#" * filled + "-" * (size - filled),
                        f"{fraction * 100:3.0f}%",
                    )
                rows.append((f"[{bar}] {percent}", "comfy"))
                phase = str(item.get("phase", item.get("status", "queued")))
                verification = item.get("verification")
                checked = {
                    "stream_sha256": "new streamed SHA256 verified",
                    "full_sha256": "new full SHA256 verified",
                    "verified_receipt_metadata": "receipt metadata reused; no new SHA256",
                }
                verified = verification in checked
                suffix = (
                    " · " + checked[verification]
                    if verified
                    else (" · final validation pending" if fraction == 1 else "")
                )
                rows.append(
                    (
                        f"{human_bytes(done)} / {human_bytes(total)} · {human_bytes(item.get('rate_bytes_per_second'))}/s · {phase}{suffix}",
                        "good" if verified else "normal",
                    )
                )
            rows.append(("", "normal"))
        return rows

    def _details(self, width: int) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        if self.error:
            rows.extend((("Attention", "bad"), (self.error, "normal"), ("", "normal")))
        if self.page in (
            "summary",
            "settings",
            "key_confirm",
            "release_confirm",
            "ready",
            "advanced",
            "input",
        ):
            rows.extend(
                [
                    ("Selected configuration", "accent"),
                    (
                        "Runtime: " + (self.config.session or "Not created yet"),
                        "normal",
                    ),
                    (
                        "Compute: "
                        + (
                            "CPU · editor / PNG test"
                            if self.config.cpu
                            else getattr(self.config, "gpu", "G4") + " · H3 video"
                        ),
                        "normal",
                    ),
                    (
                        "Storage: "
                        + (
                            "Temporary VM disk"
                            if self.config.ephemeral
                            else "Google Drive"
                        ),
                        "normal",
                    ),
                    (
                        "Models: "
                        + (
                            "Skipped in CPU mode"
                            if self.config.cpu
                            else "4 pinned H3 files · approximately 40.07 GB"
                        ),
                        "normal",
                    ),
                    (
                        f"Browser: SSH · 127.0.0.1:{self.config.local_port}"
                        if self.config.access == "local-only"
                        else f"Browser access: {self.config.access}",
                        "normal",
                    ),
                ]
            )
            if not self.config.ephemeral:
                rows.append(("Drive directory: " + self.config.storage_root, "normal"))
            if self.config.access == "local-only":
                rows.append(
                    (
                        "SSH key: " + str(self.config.identity or DEFAULT_IDENTITY),
                        "normal",
                    )
                )
            if self.config.ephemeral:
                rows.append(("Models and outputs do not survive VM release.", "warn"))
            rows.append(("", "normal"))
        if self.page in ("pipeline", "ready", "inspect", "failure", "advanced"):
            if self.page == "pipeline":
                rows.append(
                    (
                        "Runtime: "
                        + (self.config.session or "Awaiting provider confirmation"),
                        "normal",
                    )
                )
            rows.append(("Current step: " + self.stage, "accent"))
            installation = self.status.get("installation") or {}
            if installation:
                rows.append(
                    (
                        f"Environment: {installation.get('status', 'unknown')} · {installation.get('phase', '')}",
                        "normal",
                    )
                )
            rows.extend(
                [
                    (
                        "Drive: "
                        + (
                            "skipped · temporary storage"
                            if self.config.ephemeral
                            else str(
                                (self.status.get("drive") or {}).get(
                                    "mounted", "unknown"
                                )
                            )
                        ),
                        "normal",
                    ),
                    (
                        f"ComfyUI HTTP: {self.status.get('http_ready', 'unknown')} · SSH: {self.ssh.get('running', 'unknown')}",
                        "normal",
                    ),
                ]
            )
            if self.updated:
                age = time.monotonic() - self.updated
                rows.append(
                    (
                        f"Last verified status: {age:.0f}s ago"
                        + (" · stale" if age > 2 * REFRESH_SECONDS else ""),
                        "warn" if age > 2 * REFRESH_SECONDS else "muted",
                    )
                )
            if self._url():
                rows.extend(
                    (
                        ("Open in your local browser", "comfy"),
                        (self._url() or "", "normal"),
                    )
                )
            rows.append(("", "normal"))
        if self.auth:
            rows.extend(
                (
                    ("Provider authorization · transient", "accent"),
                    (
                        str(
                            self.auth.get("text")
                            or "Follow the provider instructions in your browser."
                        ),
                        "normal",
                    ),
                )
            )
            if self.auth.get("url"):
                rows.append((str(self.auth["url"]), "normal"))
            rows.extend(
                (
                    (
                        "Provider input is sent only after your Enter confirmation.",
                        "normal",
                    ),
                    ("", "normal"),
                )
            )
        render = self.status.get("render") or {}
        if render.get("status") not in (None, "not_started"):
            result = render.get("result") or {}
            complete = (
                render.get("status") == "succeeded"
                and render.get("running") is False
                and result.get("ok") is True
            )
            role = "comfy" if complete else "accent"
            rows.append(("H3 render · " + str(render.get("status")), role))
            if result.get("ok") is False:
                rows.append(
                    (
                        safe_error(
                            result.get("error")
                            or "Render failed; inspect before repeating."
                        ),
                        "bad",
                    )
                )
            if complete:
                rows.append(
                    (
                        "Verified output · " + str(result.get("storage", "unknown")),
                        "comfy",
                    )
                )
                for asset in result.get("assets") or []:
                    rows.append(
                        (
                            str(asset.get("relative_path", "output"))
                            + " · "
                            + str(asset.get("bytes", "?"))
                            + " bytes",
                            "normal",
                        )
                    )
            rows.append(("", "normal"))
        # During transfer the per-model bars take precedence over menu help.
        # Authorization controls remain above them when user input is required.
        if self.page == "pipeline":
            rows.extend(self._model_rows(width))
        if self.notice:
            rows.extend(
                (("Next / status", "accent"), (self.notice, "normal"), ("", "normal"))
            )
        choices = self._choices()
        if self.page != "input" and choices:
            selected = choices[min(self.selected, len(choices) - 1)]
            if selected.detail:
                rows.extend(
                    (
                        (selected.label, "accent"),
                        (selected.detail, "normal"),
                        ("", "normal"),
                    )
                )
        if self.page != "pipeline":
            rows.extend(self._model_rows(width))
        wrapped = []
        for value, role in rows:
            for line in textwrap.wrap(
                value, max(8, width), replace_whitespace=True, drop_whitespace=True
            ) or [""]:
                wrapped.append((line, role))
        return wrapped

    def _input_default(self) -> str:
        if self.input_field == "port":
            return str(self.config.local_port)
        if self.input_field == "identity":
            return str(self.config.identity or DEFAULT_IDENTITY)
        if self.input_field == "storage_root":
            return self.config.storage_root
        return "Paste provider code; it will be masked"

    def _write(
        self,
        screen: Any,
        row: int,
        column: int,
        value: str,
        width: int,
        role: str = "normal",
    ) -> None:
        height, total_width = screen.getmaxyx()
        if not 0 <= row < height or not 0 <= column < total_width:
            return
        allowed = min(width, total_width - column - (1 if row == height - 1 else 0))
        if allowed <= 0:
            return
        text = clip_cells(value, allowed)
        if not text:
            # Some real ncurses/Python versions reject addnstr('', 0).
            # erase() already supplied the blank separator cells for this frame.
            return
        attribute = self.theme.roles.get(
            role, self.theme.roles["good"] if role == "comfy" else curses.A_NORMAL
        )
        screen.addnstr(row, column, text, len(text), attribute)

    def _draw(self, screen: Any) -> None:
        """Compose a whole frame before a single terminal update."""
        height, width = screen.getmaxyx()
        screen.erase()
        self._write(
            screen,
            0,
            1,
            "COMFYUI / COLAB" + ("  · OFFLINE DEMO" if self.demo else "  · STARTUP"),
            width - 2,
            "title",
        )
        self._write(screen, 1, 1, self._title(), width - 2, "comfy")
        if height < 24 or width < 50:
            self._write(
                screen,
                3,
                1,
                "Resize to at least 50 columns x 24 rows.",
                width - 2,
                "warn",
            )
            if self.error:
                for row, line in enumerate(
                    textwrap.wrap(self.error, max(8, width - 2))[: max(1, height - 10)],
                    5,
                ):
                    self._write(screen, row, 1, line, width - 2, "bad")
            else:
                self._write(
                    screen,
                    5,
                    1,
                    "No provider action is triggered by resizing.",
                    width - 2,
                )
            choices = self._choices()
            if choices:
                choice = choices[min(self.selected, len(choices) - 1)]
                self._write(
                    screen, height - 4, 1, "> " + choice.label, width - 2, "accent"
                )
        else:
            split = width >= 86
            left = max(30, min(46, int(width * 0.37))) if split else width - 2
            body_bottom = height - 3
            self._write(screen, 3, 1, "YOUR STARTUP STEPS", left - 2, "accent")
            complete, active = self._complete_steps(), self._active_step()
            for index, (key, title) in enumerate(STEPS if split else ()):
                marker = "+" if key in complete else (">" if key == active else "o")
                self._write(
                    screen,
                    5 + index,
                    2,
                    marker + " " + title,
                    left - 3,
                    "comfy"
                    if key in complete
                    else ("accent" if key == active else "muted"),
                )
            if not split:
                self._write(
                    screen, 5, 2, "Session > Compute > Storage", left - 3, "muted"
                )
                self._write(
                    screen,
                    6,
                    2,
                    "Drive > Environment > Models > SSH",
                    left - 3,
                    "muted",
                )
            choice_top = 14 if split else 9
            self._write(
                screen, choice_top - 1, 1, "CHOOSE / CONFIRM", left - 2, "accent"
            )
            if self.page == "input":
                label = (
                    "Provider code"
                    if self.input_field == "auth_code"
                    else self.input_field.replace("_", " ").title()
                )
                self._write(screen, choice_top, 2, label, left - 3, "normal")
                value = (
                    "*" * len(self.input_value)
                    if self.input_field == "auth_code"
                    else self.input_value
                )
                self._write(
                    screen,
                    choice_top + 2,
                    2,
                    "> " + (value if value else self._input_default()),
                    left - 3,
                    "input" if value else "muted",
                )
            else:
                choices = self._choices()
                available = max(1, (body_bottom - choice_top) if split else 4)
                offset = max(
                    0,
                    min(
                        self.selected - available + 1, max(0, len(choices) - available)
                    ),
                )
                for index, choice in enumerate(
                    choices[offset : offset + available], offset
                ):
                    self._write(
                        screen,
                        choice_top + index - offset,
                        2,
                        ("> " if self.selected == index else "  ") + choice.label,
                        left - 3,
                        "accent" if self.selected == index else "normal",
                    )
            if split:
                for row in range(3, body_bottom):
                    self._write(screen, row, left, "|", 1, "muted")
                detail_top, detail_column, detail_width = 3, left + 3, width - left - 5
            else:
                detail_top, detail_column, detail_width = 15, 2, width - 4
            rows = self._details(detail_width)
            count = max(1, body_bottom - detail_top)
            offset = min(self.detail_offset, max(0, len(rows) - count))
            self.detail_offset = offset
            for row, (value, role) in enumerate(
                rows[offset : offset + count], detail_top
            ):
                self._write(screen, row, detail_column, value, detail_width, role)
            if len(rows) > count:
                self._write(
                    screen,
                    height - 3,
                    detail_column,
                    f"Details {offset + 1}-{min(offset + count, len(rows))}/{len(rows)} · PgUp/PgDn",
                    detail_width,
                    "muted",
                )
        self._write(
            screen,
            height - 2,
            1,
            "Enter submit   Esc cancel"
            if self.page == "input"
            else "Up/Down choose   Enter confirm   Esc back   PgUp/PgDn details",
            width - 2,
            "muted",
        )
        if self.worker.busy:
            spinner = "|/-\\"[int(time.monotonic() * 5) % 4]
            self._write(
                screen,
                height - 1,
                1,
                spinner
                + " Working · future steps stop when you exit; running resources remain",
                width - 2,
                "accent",
            )
        else:
            self._write(
                screen,
                height - 1,
                1,
                "Exit retains resources. End VM requires its own confirmation.",
                width - 2,
                "muted",
            )
        screen.noutrefresh()
        curses.doupdate()

    def run(self, screen: Any) -> None:
        self.theme.initialize()
        try:
            # Local arrow sequences arrive together; avoid ncurses' 1s Esc pause.
            curses.set_escdelay(50)
        except (AttributeError, curses.error):
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(150)
        try:
            while True:
                self._events()
                self._refresh()
                self._draw(screen)
                try:
                    key = screen.getkey()
                except curses.error:
                    continue
                try:
                    if not self._key(key):
                        break
                except (DashboardError, ValueError) as exc:
                    self.error = safe_error(exc)
        finally:
            self.auth.clear()
            self.input_value = ""
            self.worker.close()

    def _plain_snapshot(self) -> str:
        choices = self._choices()
        return "\n".join(
            [
                "COMFYUI / COLAB" + (" · OFFLINE DEMO" if self.demo else ""),
                self._title(),
                *(value for value, _role in self._details(100)),
                *(
                    f"{index}. {choice.label}"
                    for index, choice in enumerate(choices, 1)
                ),
                "Enter a choice number, then Enter. 'back' returns. Exit retains resources.",
            ]
        )

    def run_plain(self) -> None:
        """Number + Enter fallback; provider code entry is never echoed."""
        print("Text mode: use a choice number and Enter.", flush=True)
        previous = None
        try:
            while True:
                self._events()
                self._refresh()
                snapshot = self._plain_snapshot()
                if snapshot != previous:
                    print("\n" + snapshot, flush=True)
                    previous = snapshot
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                line = sys.stdin.readline()
                if not line:
                    break
                value = line.strip()
                try:
                    if value == "back":
                        self._back()
                    elif self.page == "input" and self.input_field == "auth_code":
                        self.error = "Enter sensitive provider codes only in the full-screen interface."
                    elif self.page == "input":
                        self.input_value = value
                        self._confirm_input()
                    elif value.isdecimal():
                        choices = self._choices()
                        index = int(value) - 1
                        if not 0 <= index < len(choices):
                            raise DashboardError("Choose a listed number.")
                        self.selected = index
                        if not self._activate(choices[index]):
                            break
                except (DashboardError, ValueError) as exc:
                    self.error = safe_error(exc)
        finally:
            self.auth.clear()
            self.input_value = ""
            self.worker.close()

    def plain(self) -> None:
        self.run_plain()

    def snapshot(self) -> str:
        """A read-only text view, including for noninteractive offline demos."""
        return self._plain_snapshot()
