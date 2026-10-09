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
    MODEL_SEARCH_CATEGORIES,
    REFRESH_SECONDS,
    Config,
    DashboardError,
    Theme,
    Worker,
    clip_cells,
    human_bytes,
    model_catalog_entries,
    parse_model_url,
    pinned_model_summary,
    safe_error,
    services_ready,
    toggle_model_choice,
    validate_model_plan,
)

GPU_CHOICES = ("G4", "H100", "A100", "L4", "T4")
ACCOUNT_ACTIONS = (
    "account_status",
    "account_login",
    "account_logout",
    "account_switch",
)
LOGIN_ACTIONS = ("account_login", "account_switch")
STEPS = (
    ("account", "Colab login"),
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
    enabled: bool = True


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
        self.detail_page_size = 8
        self.status: dict[str, Any] = {}
        self.ssh: dict[str, Any] = {}
        self.provider: dict[str, Any] = {}
        self.sessions: list[dict[str, str]] = []
        self.stage = "Choose how to start"
        self.notice = "Choose a new runtime, or inspect an existing one."
        self.error = ""
        self.cleanup_warning = ""
        self.required_model_categories: set[str] | None = None
        self.model_paths_need_restart = False
        self.model_registration_notice = ""
        self.updated = 0.0
        self.last_refresh = 0.0
        self.resume = False
        self.auth: dict[str, Any] = {}
        self.account: dict[str, Any] = {
            "state": "unknown",
            "message": "Colab login has not been checked.",
        }
        self.account_intent: str | None = None
        self.account_return_page = "home"
        self.account_dismissed = False
        self.account_change_action = ""
        self.confirmed_steps: set[str] = set()
        self.runtime_events_allowed = True
        self.detached_runtime_notice = ""
        self.listing_return_page = "home"
        self.sessions_return_page = "home"
        self.listing_dismissed = False
        self.inspect_return_page = "home"
        self.inspect_dismissed = False
        self.inspect_previous: dict[str, Any] | None = None
        self.advanced_return_page = "summary"
        self.input_value = ""
        self.input_field = ""
        self.input_return = "summary"
        self.input_back_page = "summary"
        self.last_operation = ""
        self.operation_started = 0.0
        self.observe_runtime = False
        self.quitting = False
        self.model_entries: list[dict[str, Any]] = []
        self.models_return = "home"
        self.model_adding_dismissed = False
        self.details_overflow = False
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
            self._require_account("inspect")
        elif hasattr(backend, "account_status"):
            self._account_checking()
            self._submit("account_status")

    def _page(self, page: str, selected: int = 0) -> None:
        self.page, self.selected = page, selected
        self.detail_offset = 0
        self.input_value = ""
        choices = self._choices()
        if choices:
            self.selected = min(self.selected, len(choices) - 1)
            if not choices[self.selected].enabled:
                self.selected = next(
                    (index for index, choice in enumerate(choices) if choice.enabled),
                    self.selected,
                )

    def _submit(self, action: str, config: Config | None = None) -> bool:
        if self.demo:
            self.notice = (
                "Offline demo: creating, changing or releasing resources is disabled."
            )
            return False
        target = config if config is not None else self.config
        if self.worker.submit(action, target):
            self.last_operation = action
            self.operation_started = time.monotonic()
            self.error = ""
            return True
        self.notice = "An operation is running; no additional operation was submitted."
        return False

    def _require_account(self, intent: str) -> bool:
        if self.demo or self.account.get("state") == "authenticated":
            return True
        if self.worker.busy and self.last_operation not in ACCOUNT_ACTIONS:
            self.notice = (
                "Wait for the current operation; runtime resources are retained."
            )
            return False
        self.account_intent = intent
        self.account_dismissed = False
        if self.page != "account":
            self.account_return_page = self.page
        self._page("account")
        self.notice = "Verify Colab login before choosing a runtime. No runtime is created by login."
        if not self.worker.busy and self.account.get("state") != "not_authenticated":
            self._account_checking()
            if self._submit("account_status"):
                self._page("account")
        return False

    def _account_checking(self, signing_in: bool = False) -> None:
        self.account = {
            "state": "checking",
            "message": "Checking Colab login before sign-in. Browser authorization will be requested only if needed."
            if signing_in
            else "Checking Colab login with a read-only session request. No runtime is created.",
        }

    def _finish_account(self) -> None:
        if self.account_dismissed:
            return
        if self.page in ("home", "account") or self.account_intent:
            self.notice = str(self.account.get("message", "Colab login is unverified."))
        if self.account.get("state") != "authenticated" or not self.account_intent:
            return
        intent, self.account_intent = self.account_intent, None
        if intent == "begin":
            self._page("summary")
            self.notice = "Colab login verified. Review and confirm startup; no runtime was created by authorization."
        elif intent == "inspect":
            self._start_inspection(return_page=self.account_return_page)
        elif intent == "existing":
            self._start_listing(return_page=self.account_return_page)
        elif intent == "new":
            self._activate(Choice("new", "Create a new runtime"))

    def _start_listing(self, return_page: str | None = None) -> bool:
        origin = return_page or self.page
        if not self._submit("sessions"):
            return False
        self.listing_return_page = origin
        if origin != "sessions":
            self.sessions_return_page = origin
        self.listing_dismissed = False
        self._page("listing")
        self.notice = "Listing runtimes. The previously selected runtime stays running."
        return True

    def _start_inspection(
        self, candidate: Config | None = None, return_page: str | None = None
    ) -> bool:
        origin = return_page or self.page
        if not self._submit("inspect", candidate):
            return False
        self.inspect_return_page = origin
        self.inspect_dismissed = False
        self.inspect_previous = {
            field: getattr(self, field)
            for field in (
                "config",
                "status",
                "ssh",
                "provider",
                "resume",
                "observe_runtime",
                "updated",
                "required_model_categories",
                "model_paths_need_restart",
                "model_registration_notice",
                "confirmed_steps",
            )
        }
        self.inspect_previous["config"] = replace(self.config)
        if candidate is not None:
            self.config = candidate
            self.resume = True
            self.observe_runtime = False
            self.status, self.ssh = {}, {}
            self.confirmed_steps = set()
        self.runtime_events_allowed = True
        self._page("inspect")
        return True

    def _ready(self) -> bool:
        if self.model_paths_need_restart:
            return False
        if self.worker.busy and self.last_operation == "prepare_refresh":
            return False
        if not services_ready(self.config, self.status):
            return False
        if self.config.access == "local-only":
            return (
                self.ssh.get("running") is True
                and self.ssh.get("http_ready") is True
                and bool(self.ssh.get("url"))
            )
        return self.status.get("tunnel_alive") is True and bool(self.status.get("url"))

    def _check_model_registration(self) -> None:
        if self.required_model_categories is None:
            return
        registered = self.status.get("model_search_categories")
        known = isinstance(registered, list) and all(
            isinstance(category, str) for category in registered
        )
        missing = self.required_model_categories - set(registered) if known else set()
        self.model_paths_need_restart = not known or bool(missing)
        if not self.model_paths_need_restart:
            self.model_registration_notice = ""
        elif self.status.get("comfyui_alive") is not True:
            self.model_registration_notice = "Model files are verified. Choose Continue missing startup steps to start ComfyUI with the registered model directories."
        else:
            reason = (
                "Running ComfyUI model paths are unknown."
                if not known
                else "Running ComfyUI has no model paths for: "
                + ", ".join(sorted(missing))
                + "."
            )
            self.model_registration_notice = (
                reason
                + " Choose Stop services, then Continue missing startup steps. Refreshing the browser alone does not register directories."
            )

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
                    "models",
                    "Manage models",
                    "Add model files and save which models are automatically prepared. No runtime is required.",
                ),
                Choice(
                    "account",
                    "Manage Colab account",
                    "Check login, sign in, switch accounts or sign out. No VM is created.",
                ),
                Choice(
                    "quit",
                    "Exit and keep resources",
                    "Exiting this UI does not stop any runtime.",
                ),
            ]
        if self.page == "account":
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
            if not self.worker.busy:
                choices.append(
                    Choice(
                        "account_check",
                        "Recheck login"
                        if self.account.get("state") == "authenticated"
                        else "Check login status",
                        "Read-only session request. Does not start interactive authorization or create a runtime.",
                    )
                )
                if self.account.get("state") != "authenticated":
                    choices.append(
                        Choice(
                            "account_login",
                            "Sign in",
                            "Use the provider's browser authorization and masked code input if login is required. Existing login is retained.",
                        )
                    )
                choices.extend(
                    [
                        Choice(
                            "account_switch",
                            "Switch account",
                            "Sign out of the local Colab CLI, then choose an account on Google's sign-in page. Existing VMs and SSH keep running.",
                        ),
                        Choice(
                            "account_logout",
                            "Sign out",
                            "Remove only the Colab CLI's local OAuth cache. This does not sign out your browser or stop any VM.",
                        ),
                    ]
                )
            elif not self.auth.get("waiting"):
                choices.append(
                    Choice(
                        "loading",
                        "Loading...",
                        "Waiting for Colab login verification. Back retains all runtime resources.",
                        enabled=False,
                    )
                )
            choices.append(
                Choice(
                    "back",
                    "Back",
                    "Return to the previous page. Cancel this login request while retaining existing VM, services and SSH.",
                )
            )
            return choices
        if self.page == "account_confirm":
            return [
                Choice(
                    "account_cancel_change",
                    "Cancel",
                    "Keep the current Colab login and return to its menu.",
                ),
                Choice(
                    "account_confirm_change",
                    "Confirm · switch account"
                    if self.account_change_action == "account_switch"
                    else "Confirm · sign out",
                    "Clear only local Colab CLI login. Existing VMs, Drive data and SSH resources are retained.",
                ),
            ]
        if self.page in ("listing", "inspect", "model_adding"):
            descriptions = {
                "listing": "Waiting for the provider's runtime list.",
                "inspect": "Inspecting the selected runtime without starting or stopping it.",
                "model_adding": "Reading public model metadata and saving the confirmed download choice. No weights or runtime are requested.",
            }
            return [
                Choice(
                    "loading",
                    "Loading...",
                    descriptions[self.page],
                    enabled=False,
                ),
                Choice(
                    "back",
                    "Back",
                    "Return to models. The confirmed metadata save continues in the background."
                    if self.page == "model_adding"
                    else "Return while the read-only request finishes.",
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
                Choice("back", "Back"),
            ]
        if self.page == "gpu":
            return [
                Choice(
                    gpu,
                    gpu
                    + (
                        " · project-tested H3 profile"
                        if gpu == "G4"
                        else " · H3 profile untested"
                    ),
                    "Project test coverage is a fixed label, not this runtime's status. Starting a GPU does not prove H3 rendering compatibility. Allocation depends on your account.",
                )
                for gpu in GPU_CHOICES
            ] + [Choice("back", "Back")]
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
                Choice("back", "Back"),
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
                Choice(
                    "models",
                    "Manage models",
                    "Choose which pinned files are automatically prepared, or add a public model file. Choices are saved for later runtimes.",
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
                Choice(
                    "download_workers",
                    "Parallel downloads · " + str(self.config.download_workers),
                    "Choose 1–4 file downloads; default 2. Drive-to-VM copying stays sequential.",
                ),
            ]
            if not self.config.ephemeral:
                choices.append(Choice("storage_root", "Dedicated Drive directory"))
            choices.append(Choice("back", "Back"))
            return choices
        if self.page == "key_confirm":
            return [
                Choice(
                    "create_key",
                    "Create a dedicated SSH key",
                    "Create this key on SSH startup only if absent. Existing keys are never overwritten.",
                ),
                Choice("choose_key", "Choose an existing key path"),
                Choice("back", "Back"),
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
                    Choice("back", "Back"),
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
                Choice(
                    "models",
                    "Manage models",
                    "Saved checkbox choices control later preparation. Editing the list does not download immediately.",
                ),
                Choice(
                    "prepare_refresh",
                    "Prepare / refresh models",
                    "Apply this checkout's h3.json, extra.json and extra-*.json to the selected GPU runtime, prepare missing VM files, and keep existing ComfyUI / SSH services. Refresh the browser after success. An already running preparation is waited for without redeployment.",
                ),
                Choice("smoke", "Run a PNG smoke test"),
                Choice("render", "Render the H3 API test"),
                Choice("stop", "Stop services · retain VM"),
                Choice("ssh_start", "Start local SSH forwarding"),
                Choice("ssh_stop", "Stop local SSH forwarding"),
                Choice("wizard_resume", "Continue missing startup steps"),
                Choice("back", "Back"),
            ]
        if self.page == "models":
            choices = [
                Choice(
                    "model:" + row["path"],
                    ("[✓] " if row.get("auto_download", True) else "[×] ")
                    + Path(row["path"]).name,
                    "Enter toggles automatic preparation and saves it immediately. "
                    + row["path"]
                    + " · "
                    + human_bytes(row["size_bytes"])
                    + " · "
                    + Path(row["manifest"]).name,
                )
                for row in self.model_entries
            ]
            choices.append(
                Choice(
                    "model_add_url",
                    "Add model · Hugging Face file URL",
                    "Choose a public file URL and its ComfyUI category. Resolve the commit, file size and SHA256; save a pinned extra manifest. Download happens during preparation.",
                )
            )
            choices.append(
                Choice(
                    "back",
                    "Back",
                    "Unchecking a model keeps any existing weight file. Disabled required models can leave a workflow with Missing Models.",
                )
            )
            return choices
        if self.page == "model_category":
            from dashboard import MODEL_SEARCH_CATEGORIES

            return [
                Choice(
                    "category:" + category,
                    category,
                    "ComfyUI model directory category. The file keeps its original name.",
                )
                for category in sorted(MODEL_SEARCH_CATEGORIES)
            ] + [Choice("back", "Back")]
        if self.page == "model_add_confirm":
            return [
                Choice(
                    "model_add_save",
                    "Confirm · save model to download list",
                    "Read public metadata only, pin the source and save the new model with auto-download enabled. No GPU is allocated.",
                ),
                Choice("back", "Back"),
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
                choices.append(Choice("home", "Back"))
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
        return [
            Choice(
                "loading", "Loading...", "Waiting for the current page.", enabled=False
            ),
            Choice(
                "back", "Back", "Return to the start page without changing resources."
            ),
        ]

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
        if not self._require_account("begin"):
            return
        if not self.config.cpu:
            try:
                validate_model_plan()
            except DashboardError as exc:
                self.error = safe_error(exc)
                self.notice = "Fix the local model lists before starting or resuming this GPU runtime. No startup action was submitted."
                return
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
            self.runtime_events_allowed = True
            self.config = candidate
            self.observe_runtime = True
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
        self.input_back_page = self.page
        self._page("input")

    def _confirm_input(self) -> None:
        value = self.input_value.strip()
        field = self.input_field
        if field == "model_url":
            try:
                _, _, source = parse_model_url(value)
            except DashboardError as exc:
                self.error = str(exc)
                return
            self.config.model_url = value
            categories = sorted(MODEL_SEARCH_CATEGORIES)
            prefix = source.split("/", 1)[0]
            category = prefix if prefix in categories else categories[0]
            self.config.model_path = category + "/" + Path(source).name
            self.error = ""
            self._page("model_category", categories.index(category))
            self.notice = "Choose the directory required by this model's loader node."
            return
        if field == "auth_code":
            if not value:
                self.error = "Enter the provider code, or Esc to cancel."
                return
            accepted = self.worker.send_auth(value)
            if accepted:
                self.auth.update(waiting=False, needs_code=False)
            self.input_value = ""
            self._page(
                "account" if self.last_operation in LOGIN_ACTIONS else "pipeline"
            )
            self.notice = (
                "Provider input submitted; waiting for verification."
                if accepted
                else "The provider is no longer waiting for input."
            )
            return
        candidate = replace(self.config)
        if field == "download_workers":
            if not value.isdecimal() or not 1 <= int(value) <= 4:
                self.error = "Choose 1 to 4 parallel model downloads."
                return
            candidate.download_workers = int(value)
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
        if candidate != self.config:
            self.observe_runtime = False
        self.config = candidate
        self.error = ""
        self._page(self.input_return)

    def _activate(self, choice: Choice) -> bool:
        if not choice.enabled:
            return True
        key = choice.key
        if key in ("back", "home", "account_back"):
            self._back()
            return True
        if key == "quit":
            self.quitting = True
            self.auth.clear()
            self.input_value = ""
            return False
        if key == "new":
            if not self._require_account("new"):
                return True
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
            self.confirmed_steps = {"session"}
            self.required_model_categories = None
            self.model_paths_need_restart = False
            self.model_registration_notice = ""
            self.observe_runtime = False
            self.stage = "Choose compute hardware"
            self.notice = "Choose the compute hardware for the new runtime."
            self._page("hardware")
        elif key in ("existing", "refresh_list"):
            if self.demo:
                if self.page != "sessions":
                    self.sessions_return_page = self.page
                self.listing_dismissed = False
                self.sessions = [
                    {
                        "name": "offline-demo",
                        "hardware": "G4",
                        "status": "read-only fixture",
                    }
                ]
                self._page("sessions")
            elif not self._require_account("existing"):
                return True
            else:
                self._start_listing()
        elif self.page == "hardware":
            self.config.cpu = key == "cpu"
            if self.config.cpu:
                self.confirmed_steps.add("hardware")
            self.notice = (
                "Choose where models and outputs are saved."
                if self.config.cpu
                else "Choose a GPU type for the new runtime."
            )
            self._page(
                "storage" if self.config.cpu else "gpu",
                int(self.config.ephemeral) if self.config.cpu else 0,
            )
        elif self.page == "gpu":
            self.config.gpu = key
            self.confirmed_steps.add("hardware")
            self.notice = "Choose where models and outputs are saved."
            self._page("storage", int(self.config.ephemeral))
        elif self.page == "storage":
            if self.config.ephemeral != (key == "ephemeral"):
                self.observe_runtime = False
            self.config.ephemeral = key == "ephemeral"
            self.confirmed_steps.add("storage")
            self.notice = "Review the selected settings before confirming startup."
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
            self._start_inspection(candidate)
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
            self._input(
                "auth_code",
                "account" if self.last_operation in LOGIN_ACTIONS else "pipeline",
            )
        elif key == "account":
            if self.page != "account":
                self.account_return_page = self.page
                self.account_intent = None
            self.account_dismissed = False
            self._page("account")
            self.notice = str(self.account.get("message", "Colab login is unverified."))
        elif key in ("account_check", "account_login"):
            if not self.worker.busy:
                if self.page != "account":
                    self.account_return_page = self.page
                if self._submit(
                    "account_status" if key == "account_check" else "account_login"
                ):
                    self.account_dismissed = False
                    self._account_checking(signing_in=key == "account_login")
                    self._page("account")
        elif key in ("account_logout", "account_switch"):
            if not self.worker.busy:
                self.account_change_action = key
                self._page("account_confirm")
        elif key == "account_cancel_change":
            self._page("account")
        elif key == "account_confirm_change":
            if self.account_change_action not in ("account_logout", "account_switch"):
                return True
            if self._submit(self.account_change_action):
                self.account_intent = None
                self.account_dismissed = False
                self.account = {
                    "state": "checking",
                    "message": "Switching local Colab login; existing runtimes remain running."
                    if self.account_change_action == "account_switch"
                    else "Signing out of the local Colab CLI; existing runtimes remain running.",
                }
                self.notice = self.account["message"]
                self._page("account")
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
        elif key == "models":
            if self.worker.busy:
                self.notice = (
                    "Wait for the current preparation before editing its model list."
                )
                return True
            self.models_return = self.page
            self._load_models()
        elif self.page == "models" and key.startswith("model:"):
            if self.demo:
                self.notice = "Offline demo: model choices are read-only."
                return True
            if self.worker.busy:
                self.notice = "Wait for the current operation before changing saved model choices."
                return True
            try:
                enabled = toggle_model_choice(key.removeprefix("model:"))
                self.model_entries = model_catalog_entries()
                self.notice = "Saved: " + (
                    "automatic preparation enabled."
                    if enabled
                    else "automatic preparation disabled; existing weights are retained."
                )
                self.error = ""
            except DashboardError as exc:
                self.error = safe_error(exc)
        elif key == "model_add_url":
            if self.demo:
                self.notice = "Offline demo: adding models is disabled."
            else:
                self._input("model_url", "models")
                self.notice = (
                    "Paste a public Hugging Face blob/resolve file URL, then Enter."
                )
        elif self.page == "model_category" and key.startswith("category:"):
            self.config.model_path = (
                key.removeprefix("category:") + "/" + Path(self.config.model_path).name
            )
            self._page("model_add_confirm")
        elif key == "model_add_save":
            if self._submit("model_add"):
                self.model_adding_dismissed = False
                self._page("model_adding")
                self.notice = "Reading file metadata and saving the list; no weights are downloaded yet."
        elif key == "advanced":
            self.advanced_return_page = self.page
            self._page("advanced")
        elif key == "inspect":
            if self._require_account("inspect"):
                self._start_inspection()
        elif key in (
            "mount",
            "prepare_refresh",
            "smoke",
            "render",
            "stop",
            "ssh_start",
            "ssh_stop",
            "wizard_resume",
        ):
            if key == "prepare_refresh":
                if self.config.cpu:
                    self.notice = "Model preparation requires a GPU runtime; the CPU editor skips H3 models."
                    return True
                if not self.observe_runtime or not self.config.session:
                    self.notice = "Inspect the selected runtime to restore its storage settings before refreshing models."
                    return True
            if self._submit(key):
                self._page("pipeline")
        return True

    def _load_models(self, *, navigate: bool = True) -> None:
        try:
            self.model_entries = model_catalog_entries()
            if navigate:
                self.error = ""
                self._page("models")
                self.notice = "[✓] = automatically prepare · [×] = skip. Enter saves a toggle; existing weights are kept."
        except DashboardError as exc:
            if navigate:
                self.error = safe_error(exc)

    def _back(self) -> None:
        self.notice = ""
        if self.page == "account":
            cancel = getattr(self.worker, "cancel_account", None)
            if callable(cancel):
                cancel()
            if self.account.get("state") == "checking" or (
                self.worker.busy and self.last_operation in LOGIN_ACTIONS
            ):
                self.account = {
                    "state": "unknown",
                    "message": "Colab login request cancelled. Login is unverified; check login status before choosing a runtime. Existing resources are retained.",
                }
            self.account_intent = None
            self.account_dismissed = True
            self.auth.clear()
            self._page(
                self.account_return_page
                if self.account_return_page not in ("input", "account")
                else "home"
            )
            return
        if self.page == "listing":
            self.listing_dismissed = True
            self._page(self.listing_return_page)
            return
        if self.page == "inspect":
            self.inspect_dismissed = True
            if self.inspect_previous is not None:
                for field, value in self.inspect_previous.items():
                    setattr(self, field, value)
                self.inspect_previous = None
            self._page(self.inspect_return_page)
            return
        if self.page == "sessions":
            self.listing_dismissed = True
            self._page(self.sessions_return_page)
            return
        if self.page == "models":
            self._page(self.models_return)
            self.config.model_url = ""
            self.config.model_path = ""
            return
        if self.page == "model_adding":
            self.model_adding_dismissed = True
            self.config.model_url = ""
            self.config.model_path = ""
            self._page("models")
            return
        if self.page == "model_category":
            self._input("model_url", "models")
            self.input_back_page = "models"
            self.input_value = self.config.model_url
            return
        if self.page == "model_add_confirm":
            category = self.config.model_path.split("/", 1)[0]
            categories = sorted(MODEL_SEARCH_CATEGORIES)
            self._page(
                "model_category",
                categories.index(category) if category in categories else 0,
            )
            return
        parents = {
            "account_confirm": "account",
            "hardware": "home",
            "gpu": "hardware",
            "storage": "sessions"
            if self.resume
            else ("hardware" if self.config.cpu else "gpu"),
            "summary": "storage",
            "settings": "summary",
            "key_confirm": "summary",
            "advanced": self.advanced_return_page,
        }
        if self.page in ("input", "release_confirm"):
            target = self.input_back_page if self.page == "input" else self.input_return
            if self.page == "input" and self.input_field == "model_url":
                self.config.model_url = ""
                self.config.model_path = ""
            self._page(target)
        elif self.demo and self.page in ("pipeline", "ready", "failure"):
            self._page("home")
        elif self.page in parents:
            target = parents[self.page]
            self._page(target, int(self.config.ephemeral) if target == "storage" else 0)
        elif self.page not in ("home", "ready", "pipeline", "failure"):
            self._page("home")

    def _detach_account_runtime(self) -> None:
        """Withdraw account-specific UI evidence without stopping any resource."""
        if self.config.session:
            self.detached_runtime_notice = (
                "Previous runtime "
                + self.config.session
                + " was not stopped. Sign back into its account to inspect it."
            )
        self.config = replace(self.config, session="", create_key=False)
        self.status, self.ssh, self.provider = {}, {}, {}
        self.sessions = []
        self.confirmed_steps.clear()
        self.required_model_categories = None
        self.model_paths_need_restart = False
        self.model_registration_notice = ""
        self.updated = self.last_refresh = 0.0
        self.observe_runtime = self.resume = False
        self.runtime_events_allowed = False
        self.listing_dismissed = self.inspect_dismissed = True
        self.inspect_previous = None
        self.account_intent = None
        self.account_return_page = "home"
        if self.account_dismissed and self.page in (
            "ready",
            "summary",
            "advanced",
            "pipeline",
            "failure",
            "sessions",
        ):
            self._page("home")

    def _key(self, key: str) -> bool:
        paging = {"KEY_NPAGE": 1, "KEY_PPAGE": -1, "\x06": 1, "\x02": -1}
        if self.page != "input":
            paging.update(KEY_RIGHT=1, KEY_LEFT=-1)
        if key in paging:
            self.detail_offset = max(
                0, self.detail_offset + paging[key] * self.detail_page_size
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
        if not choices:
            return True
        if key == "KEY_UP":
            for _ in choices:
                self.selected = (self.selected - 1) % len(choices)
                if choices[self.selected].enabled:
                    break
        elif key == "KEY_DOWN":
            for _ in choices:
                self.selected = (self.selected + 1) % len(choices)
                if choices[self.selected].enabled:
                    break
        elif key in ("\r", "\n", "KEY_ENTER"):
            return self._activate(choices[min(self.selected, len(choices) - 1)])
        return True

    def _events(self) -> None:
        while True:
            try:
                kind, value = self.worker.events.get_nowait()
            except queue.Empty:
                return
            if not self.runtime_events_allowed and kind in (
                "status",
                "ssh",
                "ssh_error",
                "provider",
                "config",
                "inspection",
                "model_registration",
                "created",
                "session",
                "released",
            ):
                continue
            if kind == "status":
                self.status, self.updated = value, time.monotonic()
                self._check_model_registration()
            elif kind == "model_registration":
                self.required_model_categories = set(value)
                self._check_model_registration()
            elif kind == "model_added":
                path, manifest = value
                navigate = (
                    not self.model_adding_dismissed and self.page == "model_adding"
                )
                if navigate:
                    self.config.model_url = ""
                    self.config.model_path = ""
                self._load_models(navigate=navigate)
                if navigate or self.page == "models":
                    self.notice = f"Saved {path} in models/{manifest}. Automatically prepared on the next startup or Prepare / refresh models."
            elif kind == "ssh":
                self.ssh = value
            elif kind == "config":
                self.config = replace(value)
            elif kind == "session":
                config, provider = value
                self.config, self.provider = replace(config), provider
                self.observe_runtime = True
            elif kind == "inspection":
                if self.inspect_dismissed:
                    continue
                self.inspect_previous = None
                if value["config"].session != self.config.session:
                    self.required_model_categories = None
                    self.model_paths_need_restart = False
                    self.model_registration_notice = ""
                self.config = replace(value["config"])
                self.status, self.ssh = value.get("status", {}), value.get("ssh", {})
                self._check_model_registration()
                self.provider, self.resume = value.get("provider", {}), True
                self.updated = time.monotonic()
                self.error = ""
                known_config = (
                    value.get("known_config", value.get("configuration_known", False))
                    is True
                )
                self.observe_runtime = known_config
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
                if not self.runtime_events_allowed and self.listing_dismissed:
                    continue
                self.sessions = value
                if self.config.session and not any(
                    row["name"] == self.config.session for row in value
                ):
                    # This event is emitted only after a verified provider list.
                    # Retain settings for inspection, but withdraw old VM proof.
                    self.status, self.ssh, self.provider = {}, {}, {}
                    self.confirmed_steps.clear()
                    self.updated = 0.0
                    self.resume = False
                    self.observe_runtime = False
                    self.required_model_categories = None
                    self.model_paths_need_restart = False
                    self.model_registration_notice = ""
                if not self.listing_dismissed:
                    self._page("sessions")
                    self.notice = (
                        "Select a runtime to inspect it."
                        if value
                        else "No active named runtimes. Return and choose Create a new runtime."
                    )
            elif kind == "stage":
                if value != "status":
                    self.stage = str(value)
            elif kind == "account":
                self.account = dict(value)
                if not self.account_dismissed and self.page in ("home", "account"):
                    self.notice = str(
                        value.get("message", "Colab login is unverified.")
                    )
            elif kind in ("account_signed_out", "account_invalidated"):
                self.account = dict(value)
                self.auth.clear()
                self.input_value = ""
                self._detach_account_runtime()
            elif kind == "notice":
                self.notice = str(value)
            elif kind == "auth":
                if self.last_operation in LOGIN_ACTIONS and self.account_dismissed:
                    continue
                self.auth.update(value)
                if self.page != "input":
                    self._page(
                        "account"
                        if self.last_operation in LOGIN_ACTIONS
                        else "pipeline"
                    )
            elif kind == "auth_clear":
                self.auth.clear()
                if self.page == "input" and self.input_field == "auth_code":
                    self._page(
                        "account"
                        if self.last_operation in LOGIN_ACTIONS
                        else "pipeline"
                    )
            elif kind == "error":
                if self.last_operation == "model_add" and self.model_adding_dismissed:
                    if self.page == "models":
                        self.error = safe_error(value)
                    continue
                if (
                    self.last_operation == "sessions"
                    and self.listing_dismissed
                    or self.last_operation == "inspect"
                    and self.inspect_dismissed
                ):
                    continue
                if self.last_operation in ACCOUNT_ACTIONS and self.account_dismissed:
                    self.account = {
                        "state": "unavailable",
                        "message": safe_error(value),
                    }
                    continue
                self.auth.clear()
                self.input_value = ""
                self.error = safe_error(value)
                if self.last_operation in ACCOUNT_ACTIONS:
                    self.account = {"state": "unavailable", "message": self.error}
                    if not self.account_dismissed and self.page == "account":
                        self._page("account")
                else:
                    self._page("models" if self.page == "model_adding" else "failure")
            elif kind == "ssh_error":
                # A failed probe cannot keep advertising its previous successful
                # tunnel snapshot as current evidence of browser readiness.
                self.ssh = {"error": safe_error(value)}
                self.notice = "Local SSH status: " + safe_error(value)
            elif kind == "released":
                released, warning = value
                if released.session == self.config.session:
                    self.cleanup_warning = (
                        "Released " + released.session + ": " + safe_error(warning)
                        if warning
                        else ""
                    )
                    self.config = replace(self.config, session="", create_key=False)
                    self.status, self.ssh, self.provider = {}, {}, {}
                    self.confirmed_steps.clear()
                    self.required_model_categories = None
                    self.model_paths_need_restart = False
                    self.model_registration_notice = ""
                    self.auth.clear()
                    self.resume = False
                    self.observe_runtime = False
                    self._page("home")
                    self.notice = "Selected VM released and verified." + (
                        " Cleanup warning: " + safe_error(warning) if warning else ""
                    )
            elif kind == "done":
                self.auth.clear()
                if value in ACCOUNT_ACTIONS:
                    self._finish_account()
                    continue
                if value == "status" and self.page == "ready" and not self._ready():
                    self.resume = True
                    self._page("summary")
                    self.notice = "The connection is no longer verified ready. Inspect before continuing missing steps."
                elif (
                    value == "status"
                    and self.page == "summary"
                    and self.observe_runtime
                    and self._ready()
                ):
                    self._page("ready")
                    self.notice = "ComfyUI and SSH were verified ready; no startup command was repeated."
                if self.page == "pipeline" and value not in (
                    "status",
                    "sessions",
                    "release",
                    "inspect",
                ):
                    if self._ready():
                        self._page("ready")
                        if value != "prepare_refresh":
                            self.notice = "ComfyUI and its local connection are ready."
                    else:
                        self.resume = True
                        self._page("summary")
                        if value != "prepare_refresh":
                            self.notice = "The operation completed; inspect or continue any remaining startup steps."

    def _refresh(self) -> None:
        now = time.monotonic()
        if (
            (
                self.page == "ready"
                or self.page in ("summary", "advanced")
                and self.observe_runtime
                and bool(self.config.session)
            )
            and not self.demo
            and self.runtime_events_allowed
            and bool(self.config.session)
            and not self.worker.busy
            and now - self.last_refresh >= REFRESH_SECONDS
            and self.worker.submit("status", self.config)
        ):
            self.last_refresh = now
            self.last_operation = "status"
            self.operation_started = now

    def _busy_text(self) -> str:
        action = (
            getattr(self.worker, "active_action", None)
            or getattr(self.worker, "pending_action", None)
            or self.last_operation
        )
        label = {
            "account_status": "Checking Colab login",
            "account_login": "Signing in to Colab",
            "account_logout": "Signing out of local Colab login",
            "account_switch": "Switching Colab account",
            "sessions": "Loading runtimes from Colab",
            "inspect": "Inspecting selected runtime",
            "status": "Refreshing ComfyUI + SSH status",
            "ssh_start": "Connecting local SSH",
            "ssh_stop": "Stopping local SSH forwarding",
            "release": "Releasing selected VM",
            "mount": "Checking Google Drive authorization",
            "prepare_refresh": "Preparing updated model lists",
            "model_add": "Reading and pinning model metadata",
            "render": "Submitting H3 render",
            "smoke": "Running PNG smoke test",
            "stop": "Stopping services; retaining VM",
        }.get(action, self.stage)
        elapsed = max(0, time.monotonic() - self.operation_started)
        spinner = "|/-\\"[int(time.monotonic() * 5) % 4]
        return f"{spinner} {label} · {elapsed:.0f}s elapsed"

    def _title(self) -> str:
        return {
            "home": "Start a ComfyUI session",
            "account": "Manage Colab account",
            "account_confirm": "Confirm Colab account change",
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
            "models": "Saved model download list",
            "model_category": "Choose the model directory category",
            "model_add_confirm": "Review model addition",
            "model_adding": "Adding a pinned model to your list",
            "input": "Enter provider code"
            if self.input_field == "auth_code"
            else "Edit " + self.input_field.replace("_", " "),
        }.get(self.page, "ComfyUI startup")

    def _complete_steps(self) -> set[str]:
        complete = set()
        if self.account.get("state") == "authenticated":
            complete.add("account")
        runtime_known = bool(
            self.config.session and (self.provider or self.status or self.resume)
        )
        if runtime_known:
            complete.update(("session", "hardware"))
            if self.page != "storage":
                complete.add("storage")
        drive = self.status.get("drive") or {}
        if (runtime_known and self.config.ephemeral and self.page != "storage") or (
            drive.get("mounted") is True and drive.get("mydrive_ready") is True
        ):
            complete.add("mount")
        if (self.status.get("installation") or {}).get("status") == "ready":
            complete.add("installation")
        if (
            not self.config.cpu
            and self.status.get("models_ready") is True
            and not (self.worker.busy and self.last_operation == "prepare_refresh")
            and not any(
                (self.status.get(task) or {}).get("running") is True
                for task in ("model_prepare", "model_download")
            )
        ):
            complete.add("models")
        if self._ready():
            complete.add("startup")
        return complete

    def _active_step(self) -> str:
        if (
            self.page in ("account", "account_confirm")
            or (self.auth and self.last_operation in LOGIN_ACTIONS)
            or (self.worker.busy and self.last_operation in ACCOUNT_ACTIONS)
        ):
            return "account"
        if self.page == "home":
            return (
                "session" if self.account.get("state") == "authenticated" else "account"
            )
        if self.page in ("sessions", "listing", "inspect"):
            return "session"
        if self.page in (
            "models",
            "model_category",
            "model_add_confirm",
            "model_adding",
        ) or (self.page == "input" and self.input_field == "model_url"):
            return "models"
        if self.page in ("hardware", "gpu"):
            return "hardware"
        if self.page == "ready":
            return "startup"
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
            rows.append(
                (
                    f"{label.upper()}  {str(task.get('status', 'unknown')).upper()}",
                    "info_heading",
                )
            )
            for index, item in enumerate(files, 1):
                if not isinstance(item, dict):
                    continue
                displayed.add(str(item.get("path", "model")))
                phase = str(item.get("phase", item.get("status", "queued")))
                rows.append(
                    (f"{index:02d}  {item.get('path', 'model')} / {phase}", "normal")
                )
                done, total = item.get("done_bytes"), item.get("total_bytes")
                try:
                    fraction = (
                        min(1.0, max(0.0, float(done) / float(total)))
                        if float(total) > 0
                        else None
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    fraction = None
                metrics = f"{human_bytes(done)}/{human_bytes(total)} {human_bytes(item.get('rate_bytes_per_second'))}/s"
                size = max(4, min(12, width - len(metrics) - 9))
                full, empty = ("█", "░") if self.theme.unicode else ("#", "-")
                if fraction is None:
                    bar, percent = empty * size, " ?%"
                else:
                    filled = int(size * fraction)
                    bar, percent = (
                        full * filled + empty * (size - filled),
                        f"{fraction * 100:3.0f}%",
                    )
                verification = item.get("verification")
                checked = {
                    "stream_sha256": "new streamed SHA256 verified",
                    "full_sha256": "new full SHA256 verified",
                    "verified_receipt_metadata": "receipt metadata reused; no new SHA256",
                }
                verified = verification in checked
                rows.append(
                    (
                        f"{bar}  {percent} {metrics}",
                        "progress_complete"
                        if verified
                        else ("bad" if phase in ("failed", "error") else "title"),
                    )
                )
                if verified:
                    rows.append((checked[verification], "progress_complete"))
                elif fraction == 1:
                    rows.append(("final validation pending", "warn"))
            rows.append(("", "normal"))
        return rows

    def _step_style(self, key: str) -> tuple[str, str]:
        """Use identical verified/current/skipped states in both layouts."""
        if (key == "models" and self.config.cpu) or (
            key == "mount" and self.config.ephemeral
        ):
            return "-", "muted"
        if self.page == "ready" and key in self._complete_steps():
            return "+", "comfy"
        if key == self._active_step():
            return ">", "title"
        if key in self._complete_steps() or key in self.confirmed_steps:
            return "+", "comfy"
        return "o", "muted"

    def _details(
        self, width: int, *, omit: frozenset[str] = frozenset()
    ) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        if "selected_model" not in omit:
            rows.extend(self._selected_model_details())
        if self.page == "account_confirm":
            rows.extend(
                [
                    (
                        "Switch account"
                        if self.account_change_action == "account_switch"
                        else "Sign out",
                        "title",
                    ),
                    (
                        "Remove only the local Colab CLI OAuth cache. No Google browser sign-out or authorization revocation.",
                        "normal",
                    ),
                    (
                        "Existing VMs, local SSH and Drive data are retained. Their running resources are not released.",
                        "warn",
                    ),
                    (
                        "Choose another account on Google's sign-in page."
                        if self.account_change_action == "account_switch"
                        else "You can sign in again later from Manage Colab account.",
                        "info",
                    ),
                    ("", "normal"),
                ]
            )
        if self.detached_runtime_notice:
            rows.extend([(self.detached_runtime_notice, "warn"), ("", "normal")])
        if self.page == "model_add_confirm" and "model_add" not in omit:
            rows.extend(
                (
                    ("MODEL ADDITION · review before Enter", "accent"),
                    ("Source: " + self.config.model_url, "normal"),
                    ("Destination: " + self.config.model_path, "comfy"),
                    (
                        "Auto-download: enabled after saving. Read public metadata and save the pinned list; prepare weights separately.",
                        "normal",
                    ),
                    ("", "normal"),
                )
            )
        if "account" not in omit and (
            self.page in ("home", "account")
            or (
                self.page == "input"
                and self.input_field == "auth_code"
                and self.last_operation in LOGIN_ACTIONS
            )
        ):
            state = str(self.account.get("state", "unknown"))
            rows.extend(
                (
                    (
                        "COLAB LOGIN · " + state.replace("_", " ").upper(),
                        "good" if state == "authenticated" else "warn",
                    ),
                    (
                        str(self.account.get("message", "Login is unverified.")),
                        "normal",
                    ),
                    (
                        "Login checks and authorization never allocate or release a runtime.",
                        "muted",
                    ),
                    ("", "normal"),
                )
            )
        if "catalog" not in omit and (
            self.page
            in (
                "models",
                "model_category",
                "model_add_confirm",
                "model_adding",
            )
            or (self.page == "input" and self.input_field == "model_url")
        ):
            selected = sum(row.get("auto_download", True) for row in self.model_entries)
            rows.extend(
                (
                    ("MODEL AUTO-DOWNLOAD · saved locally", "accent"),
                    (
                        f"{selected} selected / {len(self.model_entries)} models",
                        "normal",
                    ),
                    (
                        "Saved inside models/h3.json and extra-*.json. The list is reused by later runtimes and deployments.",
                        "normal",
                    ),
                    (
                        "Editing choices never deletes existing weights or changes a running preparation. Disabling required weights can leave workflows with Missing Models.",
                        "warn",
                    ),
                    ("", "normal"),
                )
            )
            if self.config.model_url and self.page != "model_add_confirm":
                rows.extend(
                    (
                        ("Source file", "accent"),
                        (self.config.model_url, "normal"),
                        ("Destination: " + self.config.model_path, "normal"),
                        (
                            "Only public metadata is read when you confirm. No model weights or GPU are requested here.",
                            "muted",
                        ),
                        ("", "normal"),
                    )
                )
        if self.worker.busy:
            rows.extend(
                (
                    ("IN PROGRESS · " + self._busy_text(), "accent"),
                    (
                        "Waiting for the provider reply; this screen remains active. No duplicate action is submitted.",
                        "muted",
                    ),
                    ("", "normal"),
                )
            )
        if self.error and "attention" not in omit:
            rows.extend((("Attention", "bad"), (self.error, "normal"), ("", "normal")))
        if self.cleanup_warning:
            rows.extend(
                (
                    ("Previous cleanup warning", "warn"),
                    (self.cleanup_warning, "normal"),
                    ("", "normal"),
                )
            )
        if self.model_registration_notice:
            rows.extend(
                (
                    ("Model directory registration", "warn"),
                    (self.model_registration_notice, "normal"),
                    ("", "normal"),
                )
            )
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
                        "field",
                    ),
                    (
                        "Compute: "
                        + (
                            "CPU · editor / PNG test"
                            if self.config.cpu
                            else getattr(self.config, "gpu", "G4") + " · H3 video"
                        ),
                        "field",
                    ),
                    (
                        "Storage: "
                        + (
                            "Temporary VM disk"
                            if self.config.ephemeral
                            else "Google Drive"
                        ),
                        "field",
                    ),
                    (
                        "Models: " + pinned_model_summary(self.config.cpu),
                        "field",
                    ),
                    (
                        f"Parallel downloads: {self.config.download_workers} · Drive copy: 1",
                        "field",
                    ),
                    (
                        f"Browser: SSH · 127.0.0.1:{self.config.local_port}"
                        if self.config.access == "local-only"
                        else f"Browser access: {self.config.access}",
                        "field",
                    ),
                ]
            )
            if not self.config.ephemeral:
                rows.append(("Drive directory: " + self.config.storage_root, "field"))
            if self.config.access == "local-only":
                rows.append(
                    (
                        "SSH key: " + str(self.config.identity or DEFAULT_IDENTITY),
                        "field",
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
                        "field",
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
            if self._url() and "url" not in omit:
                rows.extend(
                    (
                        ("Open in your local browser", "comfy"),
                        (self._url() or "", "normal"),
                    )
                )
            rows.append(("", "normal"))
        if self.auth and "auth" not in omit:
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
        if (
            self.notice
            and "notice" not in omit
            and self.notice != self.account.get("message")
        ):
            rows.extend(
                (("Next / status", "accent"), (self.notice, "normal"), ("", "normal"))
            )
        if self.page in ("ready", "failure", "advanced", "inspect"):
            rows.extend(self._model_rows(width))
        wrapped = []
        for value, role in rows:
            for index, line in enumerate(
                textwrap.wrap(
                    value, max(8, width), replace_whitespace=True, drop_whitespace=True
                )
                or [""]
            ):
                wrapped.append((line, "normal" if role == "field" and index else role))
        return wrapped

    def _selected_model_details(self) -> list[tuple[str, str]]:
        if self.page != "models":
            return []
        choices = self._choices()
        if not choices:
            return []
        selected = choices[min(self.selected, len(choices) - 1)]
        if not selected.key.startswith("model:"):
            return []
        path = selected.key.removeprefix("model:")
        item = next((item for item in self.model_entries if item["path"] == path), {})
        enabled = item.get("auto_download", True)
        return [
            ("ABOUT THIS MODEL", "info_heading"),
            ("Name: " + Path(path).name, "normal"),
            ("Path: " + path, "normal"),
            (
                "Auto-download: "
                + ("enabled" if enabled else "disabled")
                + " · "
                + human_bytes(item.get("size_bytes"))
                + " · "
                + Path(item.get("manifest", "manifest")).name,
                "good" if enabled else "warn",
            ),
            ("", "normal"),
        ]

    @staticmethod
    def _menu_label(value: str, width: int) -> str:
        """Leave an explicit truncation marker within the available cell width."""
        if clip_cells(value, width) == value:
            return value
        return clip_cells(value, max(0, width - 3)) + "." * min(3, width)

    def _input_default(self) -> str:
        if self.input_field == "model_url":
            return "https://huggingface.co/OWNER/REPO/blob/main/model.safetensors"
        if self.input_field == "download_workers":
            return str(self.config.download_workers) + " (1-4; default 2)"
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
        if role == "field":
            label, separator, body = value.partition(":")
            if separator:
                prefix = label + separator
                self._write(screen, row, column, prefix, allowed, "comfy")
                prefix_width = len(prefix)
                self._write(
                    screen,
                    row,
                    column + prefix_width,
                    body,
                    allowed - prefix_width,
                    "normal",
                )
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

    def _compact_model_rows(self, width: int) -> list[tuple[str, str]]:
        """One row per model; full paths and verification remain in details."""
        rows: list[tuple[str, str]] = []
        displayed: set[str] = set()
        for key, label in (
            ("model_prepare", "Prepare"),
            ("model_download", "Download"),
        ):
            task = self.status.get(key) or {}
            progress = task.get("progress") or {}
            files = progress.get("files", []) if isinstance(progress, dict) else []
            items = [
                item
                for item in files
                if isinstance(item, dict)
                and str(item.get("path", "model")) not in displayed
            ]
            if not items:
                continue
            rows.append(
                (
                    f"LOCAL MODELS · {label.lower()} {task.get('status', 'unknown')}",
                    "info_heading",
                )
            )
            for item in items:
                path = str(item.get("path", "model"))
                displayed.add(path)
                try:
                    total = float(item.get("total_bytes"))
                    fraction = (
                        min(1.0, max(0.0, float(item.get("done_bytes")) / total))
                        if total > 0
                        else None
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    fraction = None
                bar_width = 8 if width < 85 else 12
                filled = int(bar_width * fraction) if fraction is not None else 0
                full, empty = ("█", "░") if self.theme.unicode else ("#", "-")
                bar = "[" + full * filled + empty * (bar_width - filled) + "]"
                percent = f"{fraction * 100:3.0f}%" if fraction is not None else "  ?%"
                verification = item.get("verification")
                phase = str(item.get("phase", item.get("status", "queued")))
                if verification == "verified_receipt_metadata":
                    phase, role = "receipt reuse", "progress_complete"
                elif verification in ("stream_sha256", "full_sha256"):
                    phase, role = "SHA verified", "progress_complete"
                elif phase in ("failed", "error"):
                    role = "bad"
                elif fraction == 1:
                    phase, role = "validation pending", "warn"
                else:
                    role = "title"
                amount = f"{human_bytes(item.get('done_bytes'))}/{human_bytes(item.get('total_bytes'))}"
                metrics = f"{bar} {percent} {amount} {phase}"
                name_width = max(10, width - len(metrics) - 2)
                name = self._menu_label(path.rsplit("/", 1)[-1], name_width)
                rows.append((name.ljust(name_width) + "  " + metrics, role))
        return rows

    def _compact_details(self, width: int) -> list[tuple[str, str]]:
        """Prioritize actionable errors, authorization and model progress."""
        rows: list[tuple[str, str]] = []
        omitted = {"account", "attention", "auth", "url", "selected_model"}
        if self.page == "model_add_confirm":
            omitted.update(("model_add", "catalog"))
            rows.extend(
                (
                    ("MODEL ADDITION · review before Enter", "accent"),
                    ("Source: " + self.config.model_url, "normal"),
                    ("Destination: " + self.config.model_path, "comfy"),
                    (
                        "Auto-download: enabled · metadata only; prepare weights separately.",
                        "normal",
                    ),
                )
            )
        if self.error:
            rows.extend((("Attention", "bad"), (self.error, "bad")))
        if self.auth:
            rows.append(("Provider authorization · transient", "accent"))
            rows.append(
                (
                    str(
                        self.auth.get("text")
                        or "Complete provider authorization in your browser."
                    ),
                    "normal",
                )
            )
            if self.auth.get("url"):
                rows.append((str(self.auth["url"]), "normal"))
        if self._url():
            rows.append(("Open ComfyUI: " + (self._url() or ""), "comfy"))
        if self.page == "account":
            account = getattr(self, "account", {})
            rows.append((str(account.get("message", "Login is unverified.")), "normal"))
        rows.extend(self._selected_model_details())
        if self.page in ("pipeline", "ready", "failure", "advanced", "inspect"):
            rows.extend(self._compact_model_rows(width))
        wrapped = []
        for value, role in rows:
            wrapped.extend(
                (line, role) for line in textwrap.wrap(value, max(8, width)) or [""]
            )
        details = []
        for value, role in self._details(width, omit=frozenset(omitted)):
            # Compact rows already carry each meter; retain the full metrics
            # and paths in paged details without repeating the solid bar.
            meter, separator, metrics = value.partition("  ")
            if meter and set(meter) <= {"█", "░", "#", "-"}:
                if separator:
                    details.append((metrics, role))
            else:
                details.append((value, role))
        if not any(value for value, _ in details):
            return wrapped
        return wrapped + [("", "normal")] + details

    def _choice_help(self, width: int) -> list[tuple[str, str]]:
        """Keep contextual help separate from scrolling execution evidence."""
        choices = self._choices()
        if self.page == "input" or not choices:
            return []
        choice = choices[min(self.selected, len(choices) - 1)]
        if not choice.detail:
            return []
        detail = choice.detail
        if self.page == "models" and choice.key.startswith("model:"):
            # Paths and metadata already appear in the main model panel.
            detail = "Enter toggles and saves. Existing files are retained."
        return [("ABOUT THIS CHOICE", "info_heading")] + [
            (line, "info") for line in textwrap.wrap(detail, max(8, width))
        ]

    def _footer_help(self, width: int) -> list[tuple[str, str]]:
        rows = self._choice_help(width)
        # Wrap the complete built-in explanations, even on 50x24 terminals.
        # Keep unusually long provider descriptions from consuming the menu.
        if len(rows) > 7:
            rows = rows[:7]
            rows[-1] = (clip_cells(rows[-1][0], max(8, width) - 1) + "…", "info")
        return rows

    def _draw_compact(self, screen: Any) -> None:
        """Keep navigation and six model meters visible on ordinary terminals."""
        height, width = screen.getmaxyx()
        content_width = width - 4
        help_rows = self._footer_help(width - 2)
        body_bottom = height - 3 - len(help_rows)
        stage = (
            self.stage
            if self.page in ("pipeline", "inspect", "listing", "model_adding")
            else self._title()
        )
        if self.worker.busy:
            self._write(screen, 2, 1, self._busy_text(), width - 2, "accent")
        elif self.page in ("pipeline", "inspect", "listing", "model_adding"):
            self._write(screen, 2, 1, "Now: " + stage, width - 2, "accent")
        elif self.page in ("home", "account", "account_confirm"):
            state = self.account.get("state", "unknown")
            if self.page != "home" or state != "authenticated":
                label = {
                    "authenticated": "signed in",
                    "not_authenticated": "signed out · sign in to continue",
                    "checking": "checking login",
                    "unavailable": "login unverified · check failed",
                }.get(state, "login unverified")
                self._write(
                    screen,
                    2,
                    1,
                    "Colab · " + label,
                    width - 2,
                    "comfy" if state == "authenticated" else "warn",
                )
        self._write(screen, 3, 1, "STARTUP FLOW", content_width, "accent")
        row, column = 4, 2
        labels = ("Colab", "VM", "Compute", "Disk", "Drive", "Env", "Models", "SSH")
        for index, ((key, _), label) in enumerate(zip(STEPS, labels)):
            marker, role = self._step_style(key)
            value = marker + " " + label
            separator = " → " if index else ""
            if column + len(separator) + len(value) > width - 2:
                row, column = row + 1, 2
                separator = "→ "
            if separator:
                self._write(screen, row, column, separator, len(separator), "muted")
                column += len(separator)
            self._write(screen, row, column, value, len(value), role)
            column += len(value)
        choice_top = row + 2
        if self.page == "input":
            label = (
                "Provider code"
                if self.input_field == "auth_code"
                else self.input_field.replace("_", " ").title()
            )
            self._write(
                screen, choice_top, 1, "EDIT / ENTER TO SUBMIT", content_width, "accent"
            )
            self._write(screen, choice_top + 1, 2, label, content_width, "normal")
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
                content_width,
                "input" if value else "muted",
            )
            detail_top = choice_top + 4
        else:
            choices = self._choices()
            reserved = (
                (14 if self.page == "models" else 11)
                + (choice_top - 4)
                + len(help_rows)
            )
            available = min(len(choices), max(1, height - reserved))
            offset = max(
                0, min(self.selected - available + 1, max(0, len(choices) - available))
            )
            heading = "CHOOSE / CONFIRM"
            if len(choices) > available:
                heading += f" · {offset + 1}-{offset + available}/{len(choices)}"
            self._write(screen, choice_top, 1, heading, content_width, "accent")
            for index, choice in enumerate(
                choices[offset : offset + available], offset
            ):
                enabled = getattr(choice, "enabled", True)
                self._write(
                    screen,
                    choice_top + 1 + index - offset,
                    2,
                    ("> " if self.selected == index and enabled else "  ")
                    + self._menu_label(choice.label, content_width - 2),
                    content_width,
                    "muted"
                    if not enabled
                    else ("accent" if self.selected == index else "normal"),
                )
            detail_top = choice_top + 2 + available
        rows = self._compact_details(content_width)
        count = max(1, body_bottom - detail_top)
        self.details_overflow = len(rows) > count
        self.detail_page_size = max(1, count - 1)
        offset = min(self.detail_offset, max(0, len(rows) - count))
        self.detail_offset = offset
        for row, (value, role) in enumerate(rows[offset : offset + count], detail_top):
            self._write(screen, row, 2, value, content_width, role)

    def _draw(self, screen: Any) -> None:
        """Compose a whole frame before a single terminal update."""
        height, width = screen.getmaxyx()
        self.details_overflow = False
        help_rows = self._footer_help(width - 2) if height >= 24 and width >= 50 else []
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
        if self.worker.busy:
            self._write(screen, 2, 1, self._busy_text(), width - 2, "accent")
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
        elif width < 110 or height < 32:
            self._draw_compact(screen)
        else:
            split = width >= 86
            left = max(30, min(46, int(width * 0.37))) if split else width - 2
            body_bottom = height - 3 - len(help_rows)
            self._write(screen, 3, 1, "YOUR STARTUP STEPS", left - 2, "accent")
            for index, (key, title) in enumerate(STEPS if split else ()):
                marker, role = self._step_style(key)
                if key == "models" and self.config.cpu:
                    title = "Models · skipped on CPU"
                elif key == "mount" and self.config.ephemeral:
                    title = "Drive · skipped on VM disk"
                self._write(
                    screen,
                    5 + index,
                    2,
                    marker + " " + title,
                    left - 3,
                    role,
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
            choice_top = len(STEPS) + 8 if split else 9
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
                    enabled = getattr(choice, "enabled", True)
                    self._write(
                        screen,
                        choice_top + index - offset,
                        2,
                        ("> " if self.selected == index and enabled else "  ")
                        + self._menu_label(choice.label, left - 5),
                        left - 3,
                        "muted"
                        if not enabled
                        else ("accent" if self.selected == index else "normal"),
                    )
            if split:
                for row in range(3, body_bottom):
                    self._write(screen, row, left, "|", 1, "muted")
                detail_top, detail_column, detail_width = 3, left + 3, width - left - 5
            else:
                detail_top, detail_column, detail_width = 15, 2, width - 4
            rows = self._details(detail_width)
            count = max(1, body_bottom - detail_top)
            self.details_overflow = len(rows) > count
            self.detail_page_size = max(1, count - 1)
            offset = min(self.detail_offset, max(0, len(rows) - count))
            self.detail_offset = offset
            for row, (value, role) in enumerate(
                rows[offset : offset + count], detail_top
            ):
                self._write(screen, row, detail_column, value, detail_width, role)
        for row, (value, role) in enumerate(help_rows, height - 2 - len(help_rows)):
            self._write(screen, row, 1, value, width - 2, role)
        keyboard_hint = (
            "Enter submit  Esc cancel"
            if self.page == "input"
            else "↑/↓ choose  Enter confirm  Esc back"
        )
        if self.details_overflow:
            keyboard_hint += (
                "  Ctrl+B/F details" if self.page == "input" else "  ←/→ details"
            )
        self._write(
            screen,
            height - 2,
            1,
            keyboard_hint,
            width - 2,
            "muted",
        )
        if self.worker.busy:
            self._write(
                screen,
                height - 1,
                1,
                "Working · Exit stops future steps; running resources remain",
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
                if key == "KEY_RESIZE":
                    # Repaint retained cells and attributes after switching layouts.
                    screen.clearok(True)
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
                *(value for value, _role in self._choice_help(100)),
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
