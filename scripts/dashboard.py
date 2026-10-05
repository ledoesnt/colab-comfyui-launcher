"""Local, standard-library dashboard. Selecting an action executes that action.

Authentication stays in the real terminal. Nothing is written to a dashboard log.
Quit retains remote resources; release explicitly stops only the selected VM.
"""

from __future__ import annotations

import argparse
import curses
import json
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORAGE = "/content/drive/MyDrive/colab-comfyui-launcher-test"
DEFAULT_IDENTITY = Path.home() / ".ssh/colab_comfyui_launcher"
REFRESH_SECONDS = 10.0


class DashboardError(RuntimeError):
    """An action failed or its result could not be verified."""


class OperationCancelled(DashboardError):
    """Pipeline coordination yielded to an explicitly selected cleanup action."""


def safe_error(value: object) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    text = re.sub(r"https?://\S+", "[URL removed]", text)
    text = re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", "[email removed]", text)
    text = re.sub(
        r"(?i)(?:access_token|refresh_token|authorization|bearer|code|token)[=: ]+\S+",
        "[credential removed]",
        text,
    )
    return (
        " ".join(text.split())[-400:]
        or "Command failed; inspect the official CLI interactively."
    )


@dataclass
class Config:
    session: str = ""
    storage_root: str = DEFAULT_STORAGE
    cpu: bool = False
    ephemeral: bool = False
    access: str = "local-only"
    email: str = ""
    local_port: int = 8188
    identity: str = ""
    create_key: bool = False
    verify_cache: bool = False


def require_session(config: Config) -> None:
    if (
        not config.session
        or config.session.startswith("-")
        or any(
            character.isspace() or ord(character) < 32 for character in config.session
        )
    ):
        raise DashboardError("Choose a valid session name first.")


class Backend:
    def __init__(self, run: Callable[..., Any] = subprocess.run):
        self.run = run

    def json_command(self, argv: list[str]) -> dict[str, Any]:
        try:
            completed = self.run(argv, text=True, capture_output=True, timeout=150)
        except subprocess.TimeoutExpired as exc:
            raise DashboardError(
                "Command timed out; its remote outcome is unknown. Inspect status before retrying."
            ) from exc
        except OSError as exc:
            raise DashboardError(safe_error(exc)) from exc
        if completed.returncode:
            raise DashboardError(safe_error(completed.stderr or completed.stdout))
        try:
            result = json.loads(completed.stdout)
        except (ValueError, TypeError) as exc:
            raise DashboardError(
                "Command did not return one JSON object; outcome is unknown."
            ) from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DashboardError(
                safe_error(
                    result.get("error", "Action rejected")
                    if isinstance(result, dict)
                    else result
                )
            )
        return result

    def bridge(
        self, config: Config, action: str, *, download_missing: bool = False
    ) -> dict[str, Any]:
        require_session(config)
        argv = [
            sys.executable,
            str(ROOT / "scripts/colabctl.py"),
            "-s",
            config.session,
            action,
        ]
        if action == "start":
            argv.extend(
                ["--ephemeral"]
                if config.ephemeral
                else ["--storage-root", config.storage_root]
            )
            if config.access == "email":
                if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config.email):
                    raise DashboardError(
                        "Enter an allowed email in access settings first."
                    )
                argv.extend(["--allowed-email", config.email])
            elif config.access in ("public", "local-only"):
                argv.append("--" + config.access)
            else:
                raise DashboardError("Select public, email or local-only access.")
            if config.cpu:
                argv.append("--cpu")
        elif action == "download":
            argv.extend(["--max-seconds", "1800"])
            argv.extend(
                ["--ephemeral"]
                if config.ephemeral
                else ["--models-root", config.storage_root.rstrip("/") + "/models"]
            )
        elif action == "prepare":
            if config.ephemeral:
                raise DashboardError(
                    "Model preparation needs a persistent Drive cache; disable ephemeral mode."
                )
            argv.extend(
                [
                    "--cache-root",
                    config.storage_root.rstrip("/") + "/models",
                    "--max-seconds",
                    "1800",
                ]
            )
            if config.verify_cache:
                argv.append("--verify-cache")
            if download_missing:
                argv.append("--download-missing")
        elif action == "render":
            if config.cpu:
                raise DashboardError(
                    "H3 rendering requires a GPU. Use PNG smoke for CPU testing."
                )
            argv.extend(["--max-seconds", "900"])
        return self.json_command(argv)

    def ssh(self, config: Config, action: str) -> dict[str, Any]:
        require_session(config)
        argv = [
            sys.executable,
            str(ROOT / "scripts/ssh_forward.py"),
            "-s",
            config.session,
            action,
            "--local-port",
            str(config.local_port),
        ]
        if config.identity:
            argv.extend(["--identity", config.identity])
        if action == "start" and config.create_key:
            argv.append("--create-key")
        return self.json_command(argv)

    def interactive(self, arguments: list[str]) -> int:
        """No captured authentication text, cookies or tokens."""
        try:
            return self.run(
                ["colab", "--auth=oauth2", *arguments], timeout=600
            ).returncode
        except subprocess.TimeoutExpired:
            print(
                "Timed out. The provider outcome is unknown; inspect before retrying."
            )
            return 1
        except OSError as exc:
            print(safe_error(exc))
            return 1


class Worker:
    """Exactly one command at a time, including periodic status refreshes."""

    def __init__(self, backend: Backend, clock: Callable[[], float] = time.monotonic):
        self.backend = backend
        self.clock = clock
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.tasks: queue.Queue[tuple[str, Config]] = queue.Queue(maxsize=1)
        self.cancelled = threading.Event()
        self.operation_cancelled = threading.Event()
        self.lock = threading.Lock()
        self.busy = False
        self.active_action: str | None = None
        self.active_config: Config | None = None
        self.pending_action: str | None = None
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name="launcher-dashboard"
        )
        self.thread.start()

    def submit(self, action: str, config: Config) -> bool:
        with self.lock:
            if self.cancelled.is_set():
                return False
            if self.busy:
                if self.pending_action is not None:
                    return False
                if self.active_action == "pipeline" and action in ("stop", "release"):
                    # Cleanup uses the operation's original session/SSH settings.
                    if (
                        self.active_config is None
                        or config.session != self.active_config.session
                    ):
                        return False
                    config = self.active_config
                    self.operation_cancelled.set()
                elif self.active_action != "status" or action == "status":
                    return False
            self.busy = True
            self.pending_action = action
            self.tasks.put_nowait((action, replace(config)))
            return True

    def close(self) -> None:
        # Do not issue cleanup or VM stop when the UI closes.
        self.cancelled.set()
        self.operation_cancelled.set()

    def _check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise DashboardError(
                "UI coordination stopped; remote resources remain running."
            )
        if self.operation_cancelled.is_set():
            raise OperationCancelled(
                "Startup coordination cancelled; scoped cleanup is queued."
            )

    def _status(self, config: Config) -> dict[str, Any]:
        self._check_cancelled()
        status = self.backend.bridge(config, "status")
        self.events.put(("status", status))
        return status

    def _wait(self, config: Config, stage: str, seconds: float) -> dict[str, Any]:
        deadline = self.clock() + seconds
        while True:
            status = self._status(config)
            key = (
                "model_prepare"
                if stage == "model_prepare"
                else ("installation" if stage == "installation" else "startup")
            )
            value = status.get(key) or {}
            result = value.get("result") or {}
            progress = value.get("progress") or {}
            failures = ("failed", "interrupted", "stopped")
            if (
                value.get("ok") is False
                or value.get("status") in failures
                or result.get("ok") is False
                or result.get("status") in failures
                or progress.get("status") in failures
            ):
                raise DashboardError(
                    safe_error(
                        value.get("error")
                        or result.get("error")
                        or f"{stage} failed; inspect status."
                    )
                )
            if stage == "installation":
                done = value.get("status") == "ready"
            elif stage == "model_prepare":
                done = value.get("status") == "succeeded" and not value.get("running")
            else:
                supervisor_ready = (
                    value.get("ok") is True and value.get("status") == "started"
                )
                expected_mode = (
                    "local" if config.access == "local-only" else config.access
                )
                actual_modes = (status.get("access_mode"), value.get("access_mode"))
                if supervisor_ready and any(
                    mode is not None and mode != expected_mode for mode in actual_modes
                ):
                    raise DashboardError(
                        "Startup access mode does not match the requested mode; stop services before changing settings."
                    )
                done = (
                    supervisor_ready
                    and all(mode == expected_mode for mode in actual_modes)
                    and status.get("http_ready") is True
                    and status.get("comfyui_alive") is True
                )
                if config.access == "local-only":
                    done = done and status.get("tunnel_alive") is False
                else:
                    done = (
                        done
                        and status.get("tunnel_alive") is True
                        and bool(status.get("url"))
                    )
            if done:
                return status
            if self.clock() >= deadline:
                raise DashboardError(
                    f"Waiting for {stage} timed out; state is unknown. No automatic retry."
                )
            if self.operation_cancelled.wait(REFRESH_SECONDS):
                self._check_cancelled()

    def _pipeline(self, config: Config) -> None:
        for action in ("deploy", "install"):
            self._check_cancelled()
            self.events.put(("stage", action))
            self.backend.bridge(config, action)
        self.events.put(("stage", "waiting for installation"))
        self._wait(config, "installation", 960)
        if not config.cpu:
            self._check_cancelled()
            self.events.put(("stage", "preparing local models from Drive cache"))
            self.backend.bridge(config, "prepare", download_missing=True)
            self._wait(config, "model_prepare", 1860)
        self._check_cancelled()
        self.events.put(("stage", "starting services"))
        self.backend.bridge(config, "start")
        self._wait(config, "startup", 300)
        if config.access == "local-only":
            self._check_cancelled()
            self.events.put(("stage", "starting local SSH forwarding"))
            self.events.put(("ssh", self.backend.ssh(config, "start")))

    def _stop(self, config: Config) -> None:
        warnings = []
        try:
            self.events.put(("ssh", self.backend.ssh(config, "stop")))
        except DashboardError as exc:
            warnings.append(str(exc))
        self.backend.bridge(config, "stop")
        self._status(config)
        if warnings:
            self.events.put(
                (
                    "notice",
                    "Services stopped; local SSH cleanup warning: "
                    + safe_error(warnings[0]),
                )
            )

    def _loop(self) -> None:
        while not self.cancelled.is_set():
            try:
                action, config = self.tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            with self.lock:
                self.active_action = action
                self.active_config = config
                self.pending_action = None
                self.operation_cancelled.clear()
            try:
                self._check_cancelled()
                self.events.put(("stage", action))
                if action == "pipeline":
                    self._pipeline(config)
                elif action in ("stop", "release"):
                    try:
                        self._stop(config)
                    finally:
                        if action == "release":
                            self.events.put(("release", config))
                elif action.startswith("ssh_"):
                    self.events.put(("ssh", self.backend.ssh(config, action[4:])))
                elif action == "status":
                    self._status(config)
                    try:
                        self.events.put(("ssh", self.backend.ssh(config, "status")))
                    except DashboardError as exc:
                        self.events.put(("ssh_error", safe_error(exc)))
                else:
                    self._check_cancelled()
                    result = self.backend.bridge(config, action)
                    self.events.put(
                        (
                            "notice",
                            f"{action}: {result.get('status', 'accepted')} (status refresh follows)",
                        )
                    )
                    self._status(config)
            except OperationCancelled as exc:
                self.events.put(("notice", str(exc)))
            # Keep the UI responsive if a worker bug occurs; never retry its action.
            except Exception as exc:  # noqa: BLE001
                self.events.put(("error", safe_error(exc)))
            finally:
                with self.lock:
                    self.active_action = None
                    self.active_config = None
                    self.busy = self.pending_action is not None
                self.events.put(("done", action))
                self.tasks.task_done()


def human_bytes(value: object) -> str:
    try:
        amount = float(value)
    except (ValueError, TypeError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f}{unit}"
        amount /= 1024
    return "?"


def compact_name(path: object, limit: int = 28) -> str:
    name = Path(str(path)).name
    if len(name) > limit and name.endswith(".safetensors"):
        name = name.removesuffix(".safetensors")
    if len(name) > limit:
        head = (limit - 1) // 2
        name = name[:head] + "~" + name[-(limit - head - 1) :]
    return name


def progress_lines(status: dict[str, Any]) -> list[str]:
    lines = []
    tasks = [
        ("model_download", "Download"),
        ("model_prepare", "Prepare"),
        ("render", "Render"),
    ]
    tasks.sort(key=lambda task: not (status.get(task[0]) or {}).get("running"))
    for key, label in tasks:
        task = status.get(key) or {}
        lines.append(
            f"{label}: {task.get('status', 'unknown')}"
            + (" (running)" if task.get("running") else "")
        )
        progress = task.get("progress") or {}
        files = progress.get("files", []) if isinstance(progress, dict) else []
        for item in files[:6]:
            if not isinstance(item, dict):
                continue
            name = compact_name(item.get("path", "file"))
            done, total = (
                human_bytes(item.get("done_bytes")),
                human_bytes(item.get("total_bytes")),
            )
            rate = human_bytes(item.get("rate_bytes_per_second"))
            try:
                done_bytes, total_bytes = (
                    float(item.get("done_bytes")),
                    float(item.get("total_bytes")),
                )
                percent = (
                    f"{min(100, max(0, done_bytes / total_bytes * 100)):.0f}%"
                    if total_bytes > 0
                    else "?%"
                )
            except (TypeError, ValueError):
                percent = "?%"
            phase = str(item.get("phase", item.get("status", "?")))[:10]
            lines.append(f"  {percent:>4} {done}/{total} {rate}/s {phase} {name}")
        result = task.get("result") or {}
        if isinstance(result, dict) and result.get("ok") is False:
            lines.append("  " + safe_error(result.get("error", "failed")))
    return lines


MENU = [
    ("e", "Select existing session"),
    ("n", "Create new G4"),
    ("C", "Create new CPU"),
    ("m", "Mount Drive (interactive)"),
    ("c", "Configure storage/access/SSH"),
    ("f", "Full startup pipeline"),
    ("d", "Deploy scripts"),
    ("i", "Install"),
    ("w", "Download Drive model cache"),
    ("p", "Prepare / copy models to VM"),
    ("a", "Start configured services"),
    ("h", "Start SSH forwarding"),
    ("H", "Stop SSH forwarding"),
    ("o", "Open UI in browser"),
    ("r", "Render H3 API workflow"),
    ("t", "PNG smoke test"),
    ("s", "Stop services (keep VM)"),
    ("X", "Release selected VM"),
    ("q", "Quit UI (keep resources)"),
]


class Dashboard:
    def __init__(self, config: Config, backend: Backend):
        self.config, self.backend = config, backend
        self.worker = Worker(backend)
        self.status: dict[str, Any] = {}
        self.ssh: dict[str, Any] = {}
        self.updated = 0.0
        self.last_refresh = 0.0
        self.stage = "idle"
        self.message = "Select a session, or create a new one. q retains all resources."
        self.error = ""
        self.progress_page = 0
        self.show_menu = True

    def _terminal(self, screen: Any, operation: Callable[[], Any]) -> Any:
        curses.def_prog_mode()
        curses.endwin()
        try:
            return operation()
        finally:
            curses.reset_prog_mode()
            curses.flushinp()
            screen.clear()
            screen.refresh()

    def _configure(self) -> None:
        print("Configuration is kept in memory only. No credentials or URLs are saved.")
        value = input(f"Storage [{self.config.storage_root}] (or ephemeral): ").strip()
        if value:
            self.config.ephemeral = value == "ephemeral"
            if not self.config.ephemeral:
                self.config.storage_root = value
        value = input(
            f"Access public / local-only / email [{self.config.access}]: "
        ).strip()
        if value:
            if value not in ("public", "local-only", "email"):
                raise DashboardError("Access must be public, local-only or email.")
            self.config.access = value
        if self.config.access == "email":
            self.config.email = input(
                "Allowed email (provider OTP remains in your browser): "
            ).strip()
        value = (
            input(f"CPU mode yes/no [{'yes' if self.config.cpu else 'no'}]: ")
            .strip()
            .lower()
        )
        if value:
            if value not in ("yes", "no"):
                raise DashboardError("CPU choice must be yes or no.")
            self.config.cpu = value == "yes"
        if self.config.access == "local-only":
            print(
                "Background SSH needs a dedicated key without a passphrase; existing keys are never overwritten."
            )
            value = input(f"SSH local port [{self.config.local_port}]: ").strip()
            proposed_port = self.config.local_port
            if value:
                port = int(value)
                if not 1024 <= port <= 65535:
                    raise DashboardError("Port must be between 1024 and 65535.")
                proposed_port = port
            value = input(
                f"SSH identity path [{self.config.identity or DEFAULT_IDENTITY}]: "
            ).strip()
            proposed_identity = self.config.identity
            if value:
                path = Path(value).expanduser()
                if not path.is_absolute() or any(
                    char in str(path) for char in "\x00\r\n%$"
                ):
                    raise DashboardError(
                        "Identity path must be absolute without control/%/$ characters."
                    )
                proposed_identity = str(path)
            if proposed_port != self.config.local_port or Path(
                proposed_identity or DEFAULT_IDENTITY
            ) != Path(self.config.identity or DEFAULT_IDENTITY):
                try:
                    current = self.backend.ssh(self.config, "status")
                except DashboardError as exc:
                    raise DashboardError(
                        "Could not verify owned SSH forwarding; press H to stop or inspect it before changing port/identity."
                    ) from exc
                if current.get("running") is True:
                    raise DashboardError(
                        "An owned SSH forward is still running; press H to stop it before changing port or identity."
                    )
                if current.get("running") is not False:
                    raise DashboardError(
                        "SSH ownership status is unknown; keep the current port/identity and inspect before changing them."
                    )
            self.config.local_port = proposed_port
            self.config.identity = proposed_identity
            identity = Path(self.config.identity or DEFAULT_IDENTITY)
            if not identity.exists():
                value = (
                    input(
                        "Dedicated key is absent. Create it on SSH start? yes/no [no]: "
                    )
                    .strip()
                    .lower()
                )
                self.config.create_key = value == "yes"
                if not self.config.create_key:
                    raise DashboardError(
                        "No SSH key selected; choose key creation or public/email access before full startup."
                    )
        self.config.verify_cache = (
            input("Rehash Drive model cache during prepare? yes/no [no]: ")
            .strip()
            .lower()
            == "yes"
        )

    def _select(self) -> None:
        self.backend.interactive(["sessions"])
        name = input("Select ONE existing session name (blank cancels): ").strip()
        if name:
            candidate = replace(self.config, session=name)
            require_session(candidate)
            self.config = candidate
            self.status, self.ssh = {}, {}
            self.updated, self.last_refresh = 0.0, 0.0
            self.message = "Session selected; set CPU mode in configuration if needed."

    def _new(self, cpu: bool) -> None:
        name = (
            "launcher-"
            + ("cpu-" if cpu else "g4-")
            + time.strftime("%Y%m%d-%H%M%S-")
            + uuid.uuid4().hex[:6]
        )
        arguments = ["new", "-s", name]
        if not cpu:
            arguments.extend(["--gpu", "G4"])
        print(f"Creating only this session: {name}")
        code = self.backend.interactive(arguments)
        if code:
            raise DashboardError(
                "Creation did not complete successfully. Inspect official sessions before retrying."
            )
        self.config.session, self.config.cpu = name, cpu
        self.status, self.ssh = {}, {}
        self.updated, self.last_refresh = 0.0, 0.0
        self.message = (
            "Creation command completed; deployment/status will verify the runtime."
        )

    def _mount(self) -> None:
        require_session(self.config)
        if self.config.ephemeral:
            print("Ephemeral storage selected; no Drive mount is needed.")
            return
        code = self.backend.interactive(["drivemount", "-s", self.config.session])
        if code:
            raise DashboardError(
                "Drive mount did not succeed. Finish provider authorization, then mount again explicitly."
            )

    def _full_inputs(self) -> None:
        require_session(self.config)
        self._configure()
        if self.config.ephemeral and not self.config.cpu:
            raise DashboardError(
                "Full H3 startup requires a Drive model cache; ephemeral mode is for CPU smoke testing."
            )
        if not self.config.ephemeral:
            root = Path(self.config.storage_root)
            mydrive = Path("/content/drive/MyDrive")
            if (
                not root.is_absolute()
                or root == mydrive
                or mydrive not in root.parents
                or ".." in root.parts
            ):
                raise DashboardError(
                    "Use a dedicated directory inside /content/drive/MyDrive."
                )
        self._mount()

    def _release(self, screen: Any, config: Config) -> None:
        code = self._terminal(
            screen, lambda: self.backend.interactive(["stop", "-s", config.session])
        )
        self.message = (
            "VM release command completed for selected session."
            if code == 0
            else "VM release failed or is unknown; select the session and inspect before retrying."
        )
        if code == 0:
            self.config.session = ""
            self.status, self.ssh = {}, {}
            self.updated = 0.0

    def _url(self) -> str | None:
        if self.config.access == "local-only":
            return (
                self.ssh.get("url")
                if self.ssh.get("running") and self.ssh.get("http_ready")
                else None
            )
        return (
            self.status.get("url")
            if self.status.get("http_ready") and self.status.get("tunnel_alive")
            else None
        )

    def _events(self, screen: Any) -> None:
        while True:
            try:
                kind, value = self.worker.events.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                self.status, self.updated, self.error = value, time.monotonic(), ""
            elif kind == "ssh":
                self.ssh = value
            elif kind == "ssh_error":
                self.ssh = {"error": value}
            elif kind == "stage":
                self.stage = value
            elif kind == "error":
                self.error = value
            elif kind == "notice":
                self.message = value
            elif kind == "done":
                self.stage = "idle"
                self.last_refresh = time.monotonic()
            elif kind == "release":
                self._release(screen, value)

    def _draw(self, screen: Any) -> None:
        height, width = screen.getmaxyx()
        screen.erase()
        age = time.monotonic() - self.updated if self.updated else None
        freshness = (
            "never updated"
            if age is None
            else f"updated {age:.0f}s ago"
            + (" [STALE]" if age > 2 * REFRESH_SECONDS or self.error else "")
        )
        runtime, drive = (
            self.status.get("runtime") or {},
            self.status.get("drive") or {},
        )
        installation = self.status.get("installation") or {}
        startup = self.status.get("startup") or {}
        startup_progress = startup.get("progress") or {}
        lines = [
            "Colab ComfyUI Launcher — selecting a menu item executes it",
            f"Session: {self.config.session or '(none)'}   Stage: {self.stage}   {freshness}",
            f"GPU: {runtime.get('gpu_name', 'unknown')}  VRAM: {runtime.get('gpu_memory_mib', '?')}MiB  Python: {runtime.get('python', '?')}",
            f"Drive: {'mounted' if drive.get('mounted') else 'unknown / unmounted'}  Storage: {'ephemeral' if self.config.ephemeral else self.config.storage_root}",
            f"Install: {installation.get('status', 'unknown')}/{installation.get('phase', '?')}  Comfy: {self.status.get('comfyui_alive', '?')}  HTTP: {self.status.get('http_ready', '?')}  Cloudflare: {self.status.get('tunnel_alive', '?')}",
            f"Models ready: {self.status.get('models_ready', '?')}  Access: {self.status.get('access_mode', self.config.access)}  SSH: {self.ssh.get('running', 'unknown')}",
            "URL: " + str(self._url() or "(not ready)"),
        ]
        progress = progress_lines(self.status)
        # Keep the menu visible at 80x24; page only the changing job/file details.
        progress.insert(
            0,
            "Startup phase: "
            + str(
                startup_progress.get(
                    "phase", self.status.get("loading_phase", "unknown")
                )
            ),
        )
        menu_rows = (len(MENU) + 1) // 2 if self.show_menu else 1
        room = max(1, height - len(lines) - menu_rows - 4)
        pages = max(1, (len(progress) + room - 1) // room)
        self.progress_page %= pages
        start = self.progress_page * room
        lines.extend(progress[start : start + room])
        lines.append(
            f"10s refresh | Tab menu/details | PgUp/PgDn {self.progress_page + 1}/{pages}"
        )
        column = max(1, (width - 2) // 2)
        if self.show_menu:
            for index in range(0, len(MENU), 2):
                left = f"[{MENU[index][0]}] {MENU[index][1]}"
                right = (
                    f"[{MENU[index + 1][0]}] {MENU[index + 1][1]}"
                    if index + 1 < len(MENU)
                    else ""
                )
                lines.append(f"{left[:column]:<{column}} {right[:column]}")
        else:
            lines.append(
                "Tab: all actions | f: full startup | s: stop services | X: release VM | q: keep"
            )
        lines.extend(
            [
                "",
                "q closes this UI only. VM, services and forwarding remain; X explicitly releases the selected VM.",
                self.error or self.message,
            ]
        )
        for row, line in enumerate(lines[: max(0, height - 1)]):
            try:
                screen.addnstr(row, 0, line, max(0, width - 1))
            except curses.error:
                pass
        # Preserve the cleanup warning and current error even on a small terminal.
        if height >= 2:
            try:
                screen.addnstr(
                    height - 2,
                    0,
                    "q: keep resources | X: release selected VM | "
                    + (self.error or self.message),
                    max(0, width - 1),
                    curses.A_REVERSE,
                )
            except curses.error:
                pass
        screen.refresh()

    def _action(self, screen: Any, key: str) -> None:
        if self.worker.busy and key in ("e", "n", "C", "m", "c", "f", "o"):
            self.message = "Wait for the worker before using interactive actions. q retains resources."
            return
        if key in ("e", "n", "C", "m", "c", "f"):
            operations = {
                "e": self._select,
                "n": lambda: self._new(False),
                "C": lambda: self._new(True),
                "m": self._mount,
                "c": self._configure,
            }
            if key == "f":
                self._terminal(screen, self._full_inputs)
                self.worker.submit("pipeline", self.config)
            else:
                self._terminal(screen, operations[key])
                self.last_refresh = 0.0
        elif key == "o":
            url = self._url()
            if not isinstance(url, str) or not (
                url.startswith("https://")
                or re.fullmatch(r"http://127\.0\.0\.1:\d+/?", url)
            ):
                raise DashboardError("No verified UI URL is available yet.")
            if not webbrowser.open(url):
                self.message = (
                    "Browser could not be opened; use the URL displayed above."
                )
        else:
            actions = {
                "d": "deploy",
                "i": "install",
                "w": "download",
                "p": "prepare",
                "a": "start",
                "h": "ssh_start",
                "H": "ssh_stop",
                "r": "render",
                "t": "smoke",
                "s": "stop",
                "X": "release",
            }
            if key in actions:
                require_session(self.config)
                was_busy = self.worker.busy
                if self.worker.submit(actions[key], self.config):
                    self.message = (
                        f"{actions[key]} queued after the current command."
                        if was_busy
                        else f"{actions[key]} requested."
                    )
                else:
                    self.message = "A manual action is running or already queued; no additional action was submitted."

    def run(self, screen: Any) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(200)
        try:
            while True:
                self._events(screen)
                now = time.monotonic()
                if (
                    self.config.session
                    and now - self.last_refresh >= REFRESH_SECONDS
                    and not self.worker.busy
                    and self.worker.submit("status", self.config)
                ):
                    self.last_refresh = now
                self._draw(screen)
                try:
                    key = screen.getkey()
                except curses.error:
                    continue
                if key == "q":
                    break
                if key == "\t":
                    self.show_menu = not self.show_menu
                    self.progress_page = 0
                    continue
                if key in ("KEY_NPAGE", "KEY_PPAGE"):
                    self.progress_page += 1 if key == "KEY_NPAGE" else -1
                    continue
                try:
                    self._action(screen, key)
                except (DashboardError, ValueError, EOFError) as exc:
                    self.error = safe_error(exc)
        finally:
            self.worker.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session",
        "-s",
        default="",
        help="Select an existing session; no runtime is created automatically",
    )
    parser.add_argument(
        "--storage-root",
        default=DEFAULT_STORAGE,
        help="Dedicated Drive directory containing the existing model cache",
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Selected existing session is CPU; full startup skips H3 model preparation",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="Explicit VM-local assets for disposable CPU smoke tests",
    )
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error(
            "Run the dashboard in an interactive terminal; Drive authentication needs its TTY."
        )
    config = Config(
        session=args.session,
        storage_root=args.storage_root,
        cpu=args.cpu,
        ephemeral=args.ephemeral,
    )
    dashboard = Dashboard(config, Backend())
    try:
        curses.wrapper(dashboard.run)
    except KeyboardInterrupt:
        dashboard.worker.close()
    print(
        "Dashboard closed. Remote resources were retained; explicitly release the selected VM when finished."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
