"""Local startup wizard, with an optional advanced command dashboard.

Provider authentication stays interactive inside a dedicated terminal.
Quit retains remote resources; release explicitly stops only the selected VM.
"""

from __future__ import annotations

import argparse
import curses
import importlib.util
import json
import os
import queue
import re
import select
import stat
import subprocess
import sys
import termios
import textwrap
import threading
import time
import unicodedata
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORAGE = "/content/drive/MyDrive/colab-comfyui-launcher-test"
DEFAULT_IDENTITY = Path.home() / ".ssh/colab_comfyui_launcher"
REFRESH_SECONDS = 10.0
ACCOUNT_ACTIONS = frozenset(
    ("account_status", "account_login", "account_logout", "account_switch")
)
MODEL_SEARCH_CATEGORIES = frozenset(
    (
        "diffusion_models",
        "text_encoders",
        "vae",
        "checkpoints",
        "loras",
        "clip_vision",
        "embeddings",
        "controlnet",
        "upscale_models",
        "style_models",
    )
)
NOT_DEPLOYED_NOTICE = "VM ready; launcher not deployed. Use d + Enter to deploy, or f + Enter for full startup."


class DashboardError(RuntimeError):
    """An action failed or its result could not be verified."""


class AccountCacheChanged(DashboardError):
    """OAuth cache changed, but final local sign-out could not be verified."""


class OperationCancelled(DashboardError):
    """Pipeline coordination yielded to an explicitly selected cleanup action."""


class FormCancelled(DashboardError):
    """The user cancelled an in-screen form without changing configuration."""


def safe_error(value: object) -> str:
    if "WebSocketConnectionClosedException" in str(value):
        return "Colab connection closed while checking this instance. Inspect its current state before continuing; no creation was retried."
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    text = re.sub(r"https?://\S+", "[URL removed]", text)
    text = re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", "[email removed]", text)
    text = re.sub(
        r"(?i)\b(?:access_token|refresh_token|authorization|code|token)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)|\bbearer\s+\S+",
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
    gpu: str = "G4"
    download_workers: int = 2
    model_url: str = ""
    model_path: str = ""


def require_session(config: Config) -> None:
    if (
        not config.session
        or config.session.startswith("-")
        or any(
            character.isspace() or ord(character) < 32 for character in config.session
        )
    ):
        raise DashboardError("Choose a valid session name first.")


def services_ready(config: Config, status: dict[str, Any]) -> bool:
    startup = status.get("startup") or {}
    mode = "local" if config.access == "local-only" else config.access
    return (
        status.get("comfyui_alive") is True
        and status.get("http_ready") is True
        and status.get("access_mode") == mode
        and startup.get("ok") is True
        and startup.get("status") == "started"
        and startup.get("access_mode") == mode
        and (
            status.get("tunnel_alive") is False
            if config.access == "local-only"
            else status.get("tunnel_alive") is True and bool(status.get("url"))
        )
    )


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
            if config.ephemeral:
                raise DashboardError(
                    "Temporary GPU models download directly during preparation; use Full startup / Prepare instead of a separate cache download."
                )
            argv.extend(["--max-seconds", "1800"])
            argv.extend(["--workers", str(config.download_workers)])
            argv.extend(["--models-root", config.storage_root.rstrip("/") + "/models"])
        elif action == "prepare":
            argv.extend(["--max-seconds", "1800"])
            argv.extend(["--workers", str(config.download_workers)])
            argv.extend(
                ["--ephemeral"]
                if config.ephemeral
                else ["--cache-root", config.storage_root.rstrip("/") + "/models"]
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

    def official(self, arguments: list[str], *, timeout: int = 150) -> str:
        """Capture ordinary provider commands; authentication alone inherits a TTY."""
        try:
            completed = self.run(
                ["colab", "--auth=oauth2", *arguments],
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DashboardError(
                "Provider command timed out; outcome is unknown. Inspect sessions before retrying."
            ) from exc
        except OSError as exc:
            raise DashboardError(safe_error(exc)) from exc
        if completed.returncode:
            raise DashboardError(safe_error(completed.stderr or completed.stdout))
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", completed.stdout)

    @staticmethod
    def session_rows(output: str) -> list[dict[str, str]]:
        rows = []
        for line in output.splitlines():
            match = re.match(r"^\[([^\]\s]+)\].+\|\s*Hardware:\s*([^|]+)", line)
            if not match or match[1] == "?":
                continue
            row = {"name": match[1], "hardware": match[2].strip()}
            for part in line.split("|")[2:]:
                key, separator, value = part.strip().partition(":")
                if separator and key in ("Shape", "Variant", "Status"):
                    row[key.lower()] = value.strip()
            rows.append(row)
        return rows

    def sessions(self) -> list[dict[str, str]]:
        output = self.official(["sessions"])
        rows = self.session_rows(output)
        if not rows and "No active sessions" not in output:
            raise DashboardError(
                "Provider session list could not be verified; no action was retried."
            )
        return rows

    def create(self, config: Config) -> dict[str, str]:
        require_session(config)
        arguments = ["new", "-s", config.session]
        if not config.cpu:
            if config.gpu not in ("T4", "L4", "G4", "H100", "A100"):
                raise DashboardError("Choose a GPU variant supported by this CLI.")
            arguments.extend(["--gpu", config.gpu])
        output = self.official(arguments, timeout=300)
        if "Session READY." not in output:
            raise DashboardError(
                "Creation outcome is unknown. Inspect official sessions before retrying."
            )
        rows = self.session_rows(self.official(["status", "-s", config.session]))
        selected = next((row for row in rows if row["name"] == config.session), None)
        if selected is None:
            raise DashboardError(
                "Created session could not be verified; inspect sessions before retrying."
            )
        return selected

    def inspect(self, config: Config) -> dict[str, Any]:
        """Read and restore known configuration, without creating/stopping anything."""
        require_session(config)
        rows = self.session_rows(self.official(["status", "-s", config.session]))
        provider = next((row for row in rows if row["name"] == config.session), None)
        if provider is None:
            raise DashboardError("Selected session could not be verified.")
        candidate = replace(
            config,
            cpu=provider["hardware"] == "CPU",
            gpu=provider["hardware"] if provider["hardware"] != "CPU" else config.gpu,
        )
        status = self.bridge(candidate, "status")
        saved = status.get("configuration") or {}
        known = (
            isinstance(saved, dict)
            and type(saved.get("ephemeral")) is bool
            and type(saved.get("cpu")) is bool
        )
        if known and not saved["ephemeral"]:
            root = saved.get("storage_root")
            if root is None:
                known = False
            elif (
                not isinstance(root, str)
                or Path("/content/drive/MyDrive") not in Path(root).parents
                or ".." in Path(root).parts
            ):
                raise DashboardError(
                    "Stored Drive directory is invalid; choose a dedicated Drive directory before restarting."
                )
        if known:
            mode = saved.get("access_mode")
            if mode not in ("local", "public", "email"):
                raise DashboardError(
                    "Stored access configuration is invalid; inspect this session before restarting."
                )
            candidate.ephemeral = saved["ephemeral"]
            if type(saved.get("cpu")) is bool:
                candidate.cpu = saved["cpu"]
            candidate.access = "local-only" if mode == "local" else mode
            if not candidate.ephemeral and isinstance(saved.get("storage_root"), str):
                candidate.storage_root = saved["storage_root"]
        if candidate.access == "local-only":
            forward = self.ssh(candidate, "discover")
            if forward.get("found") is True:
                port = forward.get("local_port")
                identity = forward.get("identity")
                if (
                    forward.get("session") != candidate.session
                    or type(port) is not int
                    or not 1024 <= port <= 65535
                    or forward.get("remote_port") != 8188
                    or not isinstance(identity, str)
                    or not Path(identity).is_absolute()
                    or any(character in identity for character in "\x00\r\n%$")
                ):
                    raise DashboardError(
                        "Owned SSH settings are invalid; inspect before reconnecting."
                    )
                candidate = replace(
                    candidate, local_port=port, identity=identity, create_key=False
                )
        ssh = self.ssh(candidate, "status")
        ready = (
            known
            and services_ready(candidate, status)
            and (
                candidate.access != "local-only"
                or (ssh.get("running") is True and ssh.get("http_ready") is True)
            )
        )
        return {
            "config": candidate,
            "status": status,
            "ssh": ssh,
            "provider": provider,
            "configuration_known": known,
            "ready": ready,
        }

    def account_status(self) -> dict[str, str]:
        """Verify OAuth through a read-only CLI request without answering login.

        The provider owns its OAuth cache and may refresh it. This check never
        reads credentials itself, starts a VM, opens a browser, or retains the
        CLI's raw output/authorization link. Network failure is not logout.
        """
        command = ["colab", "--auth=oauth2", "sessions"]
        try:
            result = self.run(
                command,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {
                "state": "unavailable",
                "reason": "timeout",
                "message": "Colab login check timed out. Check the connection and retry; login has not been disproved.",
            }
        except OSError:
            return {
                "state": "unavailable",
                "reason": "cli_unavailable",
                "message": "Colab CLI could not run. Check its installation before checking login again.",
            }
        output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout or "")
        diagnostic = (output + "\n" + (result.stderr or "")).lower()
        if (
            "to authorize colab-cli, visit this url" in diagnostic
            and "enter the authorization code:" in diagnostic
        ):
            return {
                "state": "not_authenticated",
                "reason": "login_required",
                "message": "Colab login is required. Choose Sign in from Colab login; no runtime has been created.",
            }
        listed = "No active sessions found on server." in output or re.search(
            r"(?m)^\[[^\]\s]+\].+\|\s*Hardware:\s*[^|\n]+", output
        )
        if result.returncode == 0 and listed:
            return {
                "state": "authenticated",
                "reason": "verified_sessions",
                "message": "Colab login verified by the official read-only session request.",
            }
        return {
            "state": "unavailable",
            "reason": "request_failed" if result.returncode else "unverified_response",
            "message": "Colab login could not be verified. Check the network and CLI, then retry; this does not mean you are signed out.",
        }

    @staticmethod
    def account_terminal(
        _config: Config, on_event: Callable[[dict[str, Any]], None]
    ) -> Any:
        from provider_terminal import ProviderTerminal

        # Resolve interactive account login using a read-only operation, before
        # issuing any allocation command whose outcome must never be retried.
        return ProviderTerminal(
            ["colab", "--auth=oauth2", "sessions"], on_event, timeout=650
        )

    def account_logout(
        self, *, check_cancelled: Callable[[], None] | None = None
    ) -> dict[str, str]:
        """Forget only the standard CLI 0.7.4 local OAuth cache.

        That official version has no logout command. Its public auth.py fixes
        TOKEN_CONFIG_PATH to this path regardless of XDG_CONFIG_HOME/config.
        Never read credentials or query sessions here: a session query after
        removal would immediately start the provider's interactive login.
        Browser login, Google grants, ADC, Drive, SSH and VM state are retained.
        """
        try:
            version = self.run(
                ["colab", "version"],
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            raise DashboardError(
                "Colab CLI version check timed out; local sign-out was not performed."
            ) from exc
        except OSError as exc:
            raise DashboardError(
                "Colab CLI could not run; local sign-out was not performed."
            ) from exc
        if version.returncode != 0 or version.stdout.strip() != "Version: 0.7.4":
            raise DashboardError(
                "Local sign-out supports the verified OAuth cache of Colab CLI 0.7.4 only; no credential file was changed."
            )
        if check_cancelled is not None:
            check_cancelled()
        home = Path.home()
        cache = home / ".config" / "colab-cli" / "token.json"
        removed = False
        try:
            # Refuse unusual layouts instead of following links into another
            # credential store. Missing parents also prove this cache absent.
            for directory in (home / ".config", cache.parent):
                try:
                    metadata = directory.lstat()
                except FileNotFoundError:
                    break
                if not stat.S_ISDIR(metadata.st_mode):
                    raise DashboardError(
                        "Local sign-out refused a nonstandard OAuth cache directory; no credential file was changed."
                    )
            try:
                metadata = cache.lstat()
            except FileNotFoundError:
                metadata = None
            if metadata is not None:
                if not stat.S_ISREG(metadata.st_mode):
                    raise DashboardError(
                        "Local sign-out refused a nonregular OAuth cache; no credential file was changed."
                    )
                if check_cancelled is not None:
                    check_cancelled()
                cache.unlink()
                removed = True
            try:
                cache.lstat()
            except FileNotFoundError:
                pass
            else:
                error_type = AccountCacheChanged if removed else DashboardError
                raise error_type(
                    "The Colab OAuth cache is still present or was recreated; local sign-out is not verified. Close other CLI login attempts and retry."
                )
        except OSError as exc:
            error_type = AccountCacheChanged if removed else DashboardError
            raise error_type(
                "The local Colab OAuth cache could not be cleared or checked; sign-out is not verified."
            ) from exc
        return {
            "state": "not_authenticated",
            "reason": "local_oauth_cache_removed"
            if removed
            else "local_oauth_cache_absent",
            "message": "Local Colab CLI sign-out verified. Browser login and Google authorization remain; existing VMs, Drive and SSH are retained.",
        }

    @staticmethod
    def provider_terminal(
        config: Config, on_event: Callable[[dict[str, Any]], None]
    ) -> Any:
        from provider_terminal import ProviderTerminal

        require_session(config)
        return ProviderTerminal(
            [
                "colab",
                "--auth=oauth2",
                "drivemount",
                "-s",
                config.session,
                "/content/drive",
            ],
            on_event,
            timeout=650,
        )

    def release(self, config: Config) -> None:
        require_session(config)
        output = self.official(["stop", "-s", config.session])
        if "Session terminated." not in output:
            raise DashboardError(
                "VM release was not verified; inspect the selected session before retrying."
            )


class Worker:
    """Exactly one command at a time, including periodic status refreshes."""

    def __init__(self, backend: Backend, clock: Callable[[], float] = time.monotonic):
        self.backend = backend
        self.clock = clock
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.tasks: queue.Queue[tuple[str, Config]] = queue.Queue(maxsize=1)
        self.cancelled = threading.Event()
        self.operation_cancelled = threading.Event()
        self.account_cancelled = threading.Event()
        self.lock = threading.Lock()
        self.busy = False
        self.active_action: str | None = None
        self.active_config: Config | None = None
        self.pending_action: str | None = None
        self.auth_terminal: Any = None
        self.account_checked = False
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
                if self.active_action in (
                    "pipeline",
                    "wizard_create",
                    "wizard_resume",
                    "mount",
                    "prepare_refresh",
                ) and action in ("stop", "release"):
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
            if action in ACCOUNT_ACTIONS:
                self.account_cancelled.clear()
            self.pending_action = action
            self.tasks.put_nowait((action, replace(config)))
            return True

    def close(self) -> None:
        # Do not issue cleanup or VM stop when the UI closes.
        self.cancelled.set()
        self.operation_cancelled.set()
        if self.auth_terminal is not None:
            self.auth_terminal.close()

    def send_auth(self, text: str = "") -> bool:
        terminal = self.auth_terminal
        if terminal is None:
            return False
        return bool(terminal.respond(text))

    def cancel_account(self) -> bool:
        """Cancel account coordination without stopping runtime resources."""
        with self.lock:
            if (
                self.active_action not in ACCOUNT_ACTIONS
                and self.pending_action not in ACCOUNT_ACTIONS
            ):
                return False
            self.account_cancelled.set()
            self.operation_cancelled.set()
            terminal = self.auth_terminal
        if terminal is not None:
            terminal.close()
        return True

    def _provider_embedded(
        self,
        config: Config,
        factory: Callable[[Config, Callable[[dict[str, Any]], None]], Any],
        label: str,
    ) -> None:
        self._check_cancelled()
        self.events.put(("stage", label))
        completed = threading.Event()
        accepting = threading.Event()
        accepting.set()
        callback_lock = threading.Lock()
        outcome: dict[str, Any] = {}
        auth = {
            "url": None,
            "text": label,
            "waiting": False,
            "needs_code": False,
        }

        def accept_event(event: dict[str, Any]) -> None:
            if not accepting.is_set():
                return
            kind = event.get("kind") or event.get("type")
            if kind == "finished":
                outcome.update(event)
                completed.set()
            elif kind == "auth_url":
                auth["url"] = event.get("url")
                self.events.put(("auth", dict(auth)))
            elif kind in ("confirm_required", "code_required"):
                auth.update(
                    text=event.get("text", "Complete authorization in your browser"),
                    waiting=True,
                    needs_code=kind == "code_required",
                )
                self.events.put(("auth", dict(auth)))
            elif kind == "output":
                auth["text"] = event.get("text", "")
                pending = getattr(self.auth_terminal, "pending", None)
                auth.update(
                    waiting=pending in ("confirm", "code"), needs_code=pending == "code"
                )
                self.events.put(("auth", dict(auth)))

        def on_event(event: dict[str, Any]) -> None:
            with callback_lock:
                accept_event(event)

        terminal = factory(config, on_event)
        self.auth_terminal = terminal
        try:
            terminal.start()
            while not completed.wait(0.2):
                self._check_cancelled()
            self._check_cancelled()
            if (
                outcome.get("returncode") != 0
                or outcome.get("timed_out")
                or outcome.get("cancelled")
            ):
                raise DashboardError(
                    "Provider authorization did not complete; inspect or retry this authorization step."
                )
        finally:
            with callback_lock:
                accepting.clear()
            terminal.close()
            terminal.join(2)
            auth.clear()
            self.auth_terminal = None
            self.events.put(("auth_clear", None))

    def _check_account(self) -> dict[str, str]:
        self._check_cancelled()
        self.events.put(("stage", "checking Colab login"))
        result = self.backend.account_status()
        if not isinstance(result, dict) or result.get("state") not in (
            "authenticated",
            "not_authenticated",
            "unavailable",
        ):
            result = {
                "state": "unavailable",
                "message": "Colab login response was not verified. Retry the read-only check.",
            }
        self._check_cancelled()
        self.account_checked = result["state"] == "authenticated"
        self.events.put(("account", result))
        return result

    def _authorize_account(self, config: Config) -> None:
        self.account_checked = False
        checked = self._check_account()
        if checked["state"] == "authenticated":
            return
        if checked["state"] != "not_authenticated":
            raise DashboardError(checked["message"])
        self._provider_embedded(
            config, self.backend.account_terminal, "authorizing Colab account"
        )
        verified = self._check_account()
        if verified["state"] != "authenticated":
            raise DashboardError(
                "Authorization ended, but Colab login is not verified. Check login before choosing a runtime."
            )

    def _logout_account(self, action: str) -> None:
        self._check_cancelled()
        self.account_checked = False
        self.events.put(("stage", "signing out of local Colab CLI"))
        try:
            result = self.backend.account_logout(check_cancelled=self._check_cancelled)
            if (
                not isinstance(result, dict)
                or result.get("state") != "not_authenticated"
                or result.get("reason")
                not in ("local_oauth_cache_removed", "local_oauth_cache_absent")
            ):
                raise DashboardError("Local Colab CLI sign-out was not verified.")
        except OperationCancelled:
            raise
        except DashboardError as exc:
            unavailable = {
                "state": "unavailable",
                "reason": "logout_unverified",
                "message": "Local Colab CLI sign-out could not be verified. Check the error before retrying; no replacement account was authorized.",
            }
            if isinstance(exc, AccountCacheChanged):
                self.events.put(
                    ("account_invalidated", {"action": action, **unavailable})
                )
            self.events.put(("account", unavailable))
            raise
        # A cancellation racing after removal must not hide the credential
        # change. Clear old UI account/runtime proofs before any new login.
        self.events.put(("account_signed_out", {"action": action, **result}))
        self.events.put(("account", result))
        self._check_cancelled()

    def _switch_account(self, config: Config) -> None:
        self._logout_account("account_switch")
        self._provider_embedded(
            config,
            self.backend.account_terminal,
            "signing in to Colab; choose the Google account in your browser",
        )
        verified = self._check_account()
        if verified["state"] != "authenticated":
            raise DashboardError(
                "Replacement login ended, but Colab login is not verified. Check login before choosing a runtime."
            )

    def _ensure_account(self, config: Config, *, fresh: bool = False) -> None:
        if self.account_checked and not fresh:
            return
        checked = self._check_account()
        if checked["state"] != "authenticated":
            raise DashboardError(checked["message"])

    def _mount_embedded(self, config: Config) -> None:
        if config.ephemeral:
            return
        current_drive = self._status(config).get("drive") or {}
        if (
            current_drive.get("mounted") is True
            and current_drive.get("mydrive_ready") is True
        ):
            return
        self._provider_embedded(
            config, self.backend.provider_terminal, "authorizing Google Drive"
        )
        drive = self._status(config).get("drive") or {}
        if drive.get("mounted") is not True or drive.get("mydrive_ready") is not True:
            raise DashboardError(
                "Provider command ended but Drive is not mounted; complete authorization and explicitly retry."
            )
        self.events.put(
            ("notice", "Google Drive and MyDrive were verified. Continuing startup.")
        )

    def _smart_pipeline(self, config: Config) -> None:
        self.events.put(("stage", "checking current runtime state"))
        status = self._status(config)
        saved = status.get("configuration") or {}
        if (
            saved
            and (
                saved.get("ephemeral") != config.ephemeral
                or (
                    not config.ephemeral
                    and saved.get("storage_root") != config.storage_root
                )
                or saved.get("access_mode")
                != ("local" if config.access == "local-only" else config.access)
            )
            and status.get("comfyui_alive") is True
        ):
            raise DashboardError(
                "Existing services use different storage/access settings. Stop them explicitly before reconfiguring."
            )
        if not config.cpu:
            validate_model_plan()
        self._mount_embedded(config)
        if status.get("deployed") is not True:
            self._check_cancelled()
            self.events.put(("stage", "deploying launcher"))
            self.backend.bridge(config, "deploy")
            status = self._status(config)
        install = status.get("installation") or {}
        if install.get("status") != "ready":
            if install.get("status") == "unknown":
                raise DashboardError(
                    "Installation state is unknown; inspect this session before restarting installation."
                )
            if install.get("status") not in ("installing", "starting", "running"):
                self._check_cancelled()
                self.events.put(("stage", "installing ComfyUI environment"))
                self.backend.bridge(config, "install")
            self.events.put(("stage", "waiting for installation"))
            status = self._wait(config, "installation", 960)
        if not config.cpu and status.get("models_ready") is not True:
            task = status.get("model_prepare") or {}
            if not task.get("running"):
                if task.get("status") in ("unknown", "interrupted"):
                    raise DashboardError(
                        "Model preparation state is uncertain; inspect this session before restarting it."
                    )
                self._check_cancelled()
                self.events.put(
                    (
                        "stage",
                        "downloading models to VM"
                        if config.ephemeral
                        else "preparing local models from Drive cache",
                    )
                )
                self.backend.bridge(config, "prepare", download_missing=True)
            else:
                self.events.put(
                    ("stage", "waiting for existing local model preparation")
                )
            status = self._wait(config, "model_prepare", 1860)
        if not services_ready(config, status):
            startup = status.get("startup") or {}
            if (
                status.get("comfyui_alive") is True
                and startup.get("status") != "starting"
            ):
                raise DashboardError(
                    "An existing service is not ready for this configuration; inspect or stop it before restarting."
                )
            if startup.get("status") != "starting":
                self._check_cancelled()
                self.events.put(("stage", "starting ComfyUI"))
                self.backend.bridge(config, "start")
            status = self._wait(config, "startup", 300)
        if config.access == "local-only":
            self._check_cancelled()
            self.events.put(("stage", "connecting local SSH"))
            ssh = self.backend.ssh(config, "status")
            if not (ssh.get("running") is True and ssh.get("http_ready") is True):
                self._check_cancelled()
                ssh = self.backend.ssh(config, "start")
            if not (ssh.get("running") is True and ssh.get("http_ready") is True):
                raise DashboardError(
                    "SSH forwarding was not verified ready; inspect the selected connection."
                )
            self.events.put(("ssh", ssh))
        self.events.put(("notice", "ComfyUI is ready."))

    def _prepare_refresh(self, config: Config) -> None:
        """Apply current pinned model lists without changing the VM or services."""
        self.events.put(("stage", "checking current runtime before model refresh"))
        status = self._status(config)
        saved = status.get("configuration") or {}
        if not (
            isinstance(saved, dict)
            and type(saved.get("cpu")) is bool
            and type(saved.get("ephemeral")) is bool
            and saved.get("access_mode") in ("local", "public", "email")
            and (saved["ephemeral"] or isinstance(saved.get("storage_root"), str))
        ):
            raise DashboardError(
                "Runtime storage configuration is unknown. Inspect this runtime before refreshing models."
            )
        if config.cpu or saved["cpu"]:
            raise DashboardError(
                "Model preparation requires a GPU runtime; the CPU editor skips H3 models."
            )
        if (
            saved["ephemeral"] != config.ephemeral
            or (not config.ephemeral and saved["storage_root"] != config.storage_root)
            or saved["access_mode"]
            != ("local" if config.access == "local-only" else config.access)
        ):
            raise DashboardError(
                "Saved runtime settings differ from the selected settings. Inspect this runtime before refreshing models."
            )
        if not config.ephemeral and (
            Path("/content/drive/MyDrive") not in Path(saved["storage_root"]).parents
            or ".." in Path(saved["storage_root"]).parts
        ):
            raise DashboardError(
                "Saved Drive directory is invalid. Inspect this runtime before refreshing models."
            )
        manifest = validate_model_plan()
        required_categories = {
            item["path"].split("/", 1)[0] for item in manifest["files"]
        }
        task = status.get("model_prepare") or {}
        existing_task = task.get("running") is True
        if existing_task:
            self.events.put(("stage", "waiting for existing local model preparation"))
        else:
            if task.get("status") in ("unknown", "interrupted"):
                raise DashboardError(
                    "Model preparation state is uncertain; inspect this runtime before submitting another task."
                )
            if (status.get("model_download") or {}).get("running") is True:
                raise DashboardError(
                    "A model download is already running. Wait for it before refreshing models."
                )
            if (status.get("installation") or {}).get("status") != "ready":
                raise DashboardError(
                    "ComfyUI installation is not ready. Continue the startup steps before refreshing models."
                )
            drive = status.get("drive") or {}
            if not config.ephemeral and not (
                drive.get("mounted") is True and drive.get("mydrive_ready") is True
            ):
                raise DashboardError(
                    "Google Drive is not ready. Use Authorize / check Google Drive before refreshing models."
                )
            self._check_cancelled()
            self.events.put(("stage", "deploying updated model lists to this runtime"))
            self.backend.bridge(config, "deploy")
            self._check_cancelled()
            self.events.put(("stage", "preparing added models on VM disk"))
            self.backend.bridge(config, "prepare", download_missing=True)
        self._wait(config, "model_prepare", 1860)
        final_status = self._status(config)
        if final_status.get("models_ready") is not True:
            raise DashboardError(
                "Local model readiness changed after preparation. Inspect the runtime before submitting a workflow."
            )
        if config.access == "local-only":
            self._check_cancelled()
            try:
                self.events.put(("ssh", self.backend.ssh(config, "status")))
            except DashboardError as exc:
                self.events.put(("ssh_error", safe_error(exc)))
        registered = final_status.get("model_search_categories")
        known_paths = isinstance(registered, list) and all(
            isinstance(category, str) for category in registered
        )
        missing = required_categories - set(registered) if known_paths else set()
        registration_needed = final_status.get("comfyui_alive") is True and (
            not known_paths or bool(missing)
        )
        self.events.put(("model_registration", sorted(required_categories)))
        notice = (
            "Existing preparation finished. Edited local lists were not redeployed; choose Prepare / refresh models again to apply them."
            if existing_task
            else "Model files prepared and verified; existing services were retained."
        )
        if registration_needed:
            notice += (
                " Running ComfyUI search paths are unknown."
                if not known_paths
                else " Running ComfyUI has no registered paths for: "
                + ", ".join(sorted(missing))
                + "."
            )
            notice += " Use Stop services, then Continue missing startup steps to register the model directories. Browser refresh alone is insufficient."
        elif not existing_task:
            notice += (
                " Refresh ComfyUI to see new models."
                if final_status.get("comfyui_alive") is True
                else " ComfyUI is stopped; Continue missing startup steps to start it."
            )
        self.events.put(
            (
                "notice",
                notice,
            )
        )

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
        for attempt in range(3):
            self._check_cancelled()
            try:
                status = self.backend.bridge(config, "status")
                break
            except DashboardError as exc:
                if attempt == 2 or not any(
                    marker in str(exc)
                    for marker in (
                        "WebSocketConnectionClosedException",
                        "Colab connection closed while checking",
                        "Local Colab command timed out",
                        "Command timed out; its remote outcome is unknown",
                    )
                ):
                    raise
                self.events.put(
                    (
                        "notice",
                        "Colab connection interrupted; checking the same runtime again. No startup command was repeated.",
                    )
                )
                if self.operation_cancelled.wait(1):
                    self._check_cancelled()
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
                done = (
                    value.get("status") == "succeeded"
                    and value.get("running") is False
                    and result.get("ok") is True
                    and status.get("models_ready") is True
                )
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
        if not config.cpu:
            validate_model_plan()
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

    def _stop(self, config: Config) -> str:
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
        return safe_error(warnings[0]) if warnings else ""

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
                if action in ACCOUNT_ACTIONS and self.account_cancelled.is_set():
                    self.operation_cancelled.set()
            try:
                self._check_cancelled()
                self.events.put(("stage", action))
                if action in (
                    "wizard_create",
                    "wizard_resume",
                    "inspect",
                    "sessions",
                    "new",
                ):
                    self._ensure_account(
                        config, fresh=action in ("wizard_create", "new")
                    )
                    self._check_cancelled()
                if action == "account_status":
                    self._check_account()
                elif action == "account_login":
                    self._authorize_account(config)
                elif action == "account_logout":
                    self._logout_account(action)
                elif action == "account_switch":
                    self._switch_account(config)
                elif action in ("wizard_create", "wizard_resume"):
                    if action == "wizard_create":
                        if not config.cpu:
                            validate_model_plan()
                        self.events.put(("stage", "creating and verifying runtime"))
                        provider = self.backend.create(config)
                        self.events.put(("session", (config, provider)))
                    self._smart_pipeline(config)
                elif action == "inspect":
                    self.events.put(("inspection", self.backend.inspect(config)))
                elif action == "mount":
                    self._mount_embedded(config)
                elif action == "prepare_refresh":
                    self._prepare_refresh(config)
                elif action == "model_add":
                    module = _model_catalog_module()
                    metadata = module.lookup(config.model_url)
                    name = module.add(
                        _model_manifest_module(),
                        metadata,
                        config.model_path,
                        MODEL_SEARCH_CATEGORIES,
                    )
                    self.events.put(("model_added", (config.model_path, name)))
                elif action == "pipeline":
                    self._pipeline(config)
                elif action == "sessions":
                    self.events.put(("sessions", self.backend.sessions()))
                elif action == "new":
                    if not config.cpu:
                        validate_model_plan()
                    provider = self.backend.create(config)
                    self.events.put(("session", (config, provider)))
                    self._status(config)
                elif action == "release":
                    cleanup_error = ""
                    try:
                        cleanup_error = self._stop(config)
                    except DashboardError as exc:
                        cleanup_error = safe_error(exc)
                    self.backend.release(config)
                    self.events.put(("released", (config, cleanup_error)))
                elif action == "stop":
                    self._stop(config)
                elif action.startswith("ssh_"):
                    self.events.put(("ssh", self.backend.ssh(config, action[4:])))
                    # A live local tunnel must be reconciled with current remote
                    # service state, rather than the UI's pre-start snapshot.
                    self._status(config)
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


@lru_cache(maxsize=1)
def _model_manifest_module() -> Any:
    specification = importlib.util.spec_from_file_location(
        "launcher_dashboard_models", ROOT / "scripts/download_models.py"
    )
    if specification is None or specification.loader is None:
        raise DashboardError("Cannot load the model manifest reader")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def validate_model_plan() -> dict[str, Any]:
    try:
        module = _model_manifest_module()
        manifest = module.load_manifests(module.DEFAULT_MANIFEST)
    except Exception as exc:
        raise DashboardError("Invalid local model list: " + safe_error(exc)) from exc
    required = {item["path"].split("/", 1)[0] for item in manifest["files"]}
    unsupported = sorted(required - MODEL_SEARCH_CATEGORIES)
    if unsupported:
        raise DashboardError(
            "Model categories are not registered by this launcher: "
            + ", ".join(unsupported)
            + ". Update the runtime directory mapping before preparing these files."
        )
    return manifest


@lru_cache(maxsize=1)
def _model_catalog_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "launcher_model_catalog", ROOT / "scripts/model_catalog.py"
    )
    if spec is None or spec.loader is None:
        raise DashboardError("Cannot load model catalog management")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def model_catalog_entries() -> list[dict[str, Any]]:
    try:
        return _model_catalog_module().entries(_model_manifest_module())
    except Exception as exc:
        raise DashboardError("Invalid model catalog: " + safe_error(exc)) from exc


def toggle_model_choice(path: str) -> bool:
    try:
        return _model_catalog_module().toggle(_model_manifest_module(), path)
    except Exception as exc:
        raise DashboardError("Model choice was not saved: " + safe_error(exc)) from exc


def parse_model_url(value: str) -> tuple[str, str, str]:
    try:
        return _model_catalog_module().parse_url(value)
    except ValueError as exc:
        raise DashboardError(str(exc)) from exc


def pinned_model_summary(cpu: bool = False) -> str:
    if cpu:
        return "Skipped in CPU mode"
    try:
        manifest = validate_model_plan()
    except DashboardError as exc:
        return safe_error(exc)
    return (
        f"{len(manifest['files'])} pinned model files · approximately "
        f"{manifest['total_size_bytes'] / 1_000_000_000:.2f} GB"
    )


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


class DemoBackend:
    """Entirely local fixture. No subprocess, browser, credential or file access."""

    def __init__(self, state: str = "prepare"):
        self.state = state

    def bridge(self, _config: Config, action: str, **_kwargs: Any) -> dict[str, Any]:
        if action != "status":
            raise DashboardError(
                "Demo is read-only; no command or resource is created."
            )
        ready = self.state == "ready"
        files = [
            (
                "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
                20_970_379_616,
                0.43,
            ),
            (
                "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                15_687_142_551,
                1.0,
            ),
            ("vae/minimax_h3_video_vae_int8_convrot.safetensors", 2_811_065_184, 0.12),
            ("vae/minimax_h3_audio_vae_fp32.safetensors", 605_254_808, 0.0),
        ]
        items = [
            {
                "path": path,
                "phase": "verified"
                if ready or fraction == 1
                else ("copy" if fraction else "queued"),
                "done_bytes": size if ready else int(size * fraction),
                "total_bytes": size,
                "rate_bytes_per_second": 72 * 1024**2
                if fraction not in (0, 1) and not ready
                else 0,
                "verification": "stream_sha256" if ready or fraction == 1 else None,
            }
            for path, size, fraction in files
        ]
        result = {
            "ok": True,
            "runtime": {
                "gpu_name": "RTX PRO 6000 Blackwell",
                "gpu_memory_mib": 97887,
                "python": "3.13.15",
            },
            "drive": {
                "mounted": True,
                "mydrive_ready": True,
                "path": "/content/drive",
            },
            "installation": {"status": "ready", "phase": "dependency_check"},
            "comfyui_alive": ready,
            "http_ready": ready,
            "tunnel_alive": False,
            "models_ready": ready,
            "access_mode": "local",
            "model_loading": "local_disk",
            "startup": {
                "ok": True,
                "status": "started" if ready else "starting",
                "access_mode": "local",
                "progress": {
                    "status": "ready" if ready else "starting",
                    "phase": "local_only",
                },
            },
            "model_prepare": {
                "running": not ready,
                "status": "succeeded" if ready else "running",
                "progress": {"phase": "verified" if ready else "copy", "files": items},
            },
            "model_download": {"running": False, "status": "succeeded"},
            "render": {"running": False, "status": "succeeded" if ready else "idle"},
        }
        if self.state == "error":
            result["model_prepare"] = {
                "running": False,
                "status": "failed",
                "result": {
                    "ok": False,
                    "error": "Demo fixture: model checksum mismatch; no real file was accessed.",
                },
            }
        return result

    def ssh(self, config: Config, action: str) -> dict[str, Any]:
        if action != "status":
            raise DashboardError("Demo is read-only; no SSH command is executed.")
        return {
            "ok": True,
            "running": self.state == "ready",
            "http_ready": self.state == "ready",
            "url": f"http://127.0.0.1:{config.local_port}",
        }

    def interactive(self, _arguments: list[str]) -> int:
        raise DashboardError(
            "Demo is read-only; authentication and provider commands are disabled."
        )


def clip_cells(value: object, width: int) -> str:
    """Clip by terminal cells; control characters must not reshape the screen."""
    result, occupied = [], 0
    for character in str(value):
        if unicodedata.category(character).startswith("C"):
            continue
        cells = (
            0
            if unicodedata.combining(character)
            else (2 if unicodedata.east_asian_width(character) in ("W", "F") else 1)
        )
        if occupied + cells > width:
            break
        result.append(character)
        occupied += cells
    return "".join(result)


class Theme:
    """ComfyUI/Colab brand palette with monochrome/basic-color fallbacks."""

    BRAND: ClassVar[dict[str, str]] = {
        "comfy": "#F2FF59",
        "title": "#E77012",
        "accent": "#F9AA00",
        "info_heading": "#75BFFF",
        "info": "#BDDFFF",
        "progress_complete": "#8ED98C",
    }

    def __init__(self, color: bool = True):
        self.color = color
        self.roles = {
            "title": curses.A_BOLD,
            "accent": curses.A_BOLD,
            "good": curses.A_BOLD,
            "warn": curses.A_BOLD,
            "bad": curses.A_BOLD,
            "muted": curses.A_DIM,
            "normal": curses.A_NORMAL,
            "comfy": curses.A_BOLD,
            "input": curses.A_BOLD,
            "info_heading": curses.A_BOLD,
            "info": curses.A_NORMAL,
            "progress_complete": curses.A_BOLD,
        }
        self.unicode = "utf" in (sys.stdout.encoding or "").lower()

    def initialize(self) -> None:
        if not self.color or os.environ.get("NO_COLOR") is not None:
            return
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            background = curses.COLOR_BLACK
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                pass
            extended = curses.COLORS >= 256
            if extended and curses.can_change_color():
                for slot, value in (
                    (229, self.BRAND["comfy"]),
                    (208, self.BRAND["title"]),
                    (214, self.BRAND["accent"]),
                    (75, self.BRAND["info_heading"]),
                    (153, self.BRAND["info"]),
                    (114, self.BRAND["progress_complete"]),
                ):
                    rgb = tuple(
                        round(int(value[index : index + 2], 16) * 1000 / 255)
                        for index in (1, 3, 5)
                    )
                    try:
                        curses.init_color(slot, *rgb)
                    except curses.error:
                        pass
            colors = [
                ("title", 208 if extended else curses.COLOR_YELLOW),
                ("accent", 214 if extended else curses.COLOR_YELLOW),
                ("good", 229 if extended else curses.COLOR_GREEN),
                ("warn", 214 if extended else curses.COLOR_YELLOW),
                ("bad", 203 if extended else curses.COLOR_RED),
                ("muted", 245 if extended else curses.COLOR_WHITE),
                ("normal", 252 if extended else curses.COLOR_WHITE),
                ("comfy", 229 if extended else curses.COLOR_YELLOW),
                ("input", 255 if extended else curses.COLOR_WHITE),
                ("info_heading", 75 if extended else curses.COLOR_CYAN),
                ("info", 153 if extended else curses.COLOR_CYAN),
                ("progress_complete", 114 if extended else curses.COLOR_GREEN),
            ]
            for pair, (role, foreground) in enumerate(colors, 1):
                curses.init_pair(pair, foreground, background)
                self.roles[role] = curses.color_pair(pair) | (
                    curses.A_NORMAL if role in ("muted", "info") else curses.A_BOLD
                )
        except curses.error:
            # Partial/basic terminal capabilities never prevent local control.
            return


@dataclass(frozen=True)
class Rect:
    row: int
    column: int
    height: int
    width: int


def action_columns(card_width: int) -> int:
    return 3 if card_width >= 76 else 2


def card_layout(height: int, width: int, show_menu: bool) -> dict[str, Rect]:
    if height < 22 or width < 60:
        return {}
    if width >= 110 and height >= 28:
        left = max(43, int((width - 3) * 0.43))
        body = height - 5
        return {
            "overview": Rect(2, 0, 10, left),
            "actions": Rect(13, 0, body - 11, left),
            "models": Rect(2, left + 1, body, width - left - 2),
        }
    menu_columns = action_columns(width - 1)
    menu_height = (
        ((len(MENU) + menu_columns - 1) // menu_columns) + 2 if show_menu else 3
    )
    body = height - 4
    overview = 6
    # At 60x22 use compact fallback rather than silently cut menu rows.
    if body - overview - menu_height < 4:
        return {}
    return {
        "overview": Rect(2, 0, overview, width - 1),
        "models": Rect(2 + overview, 0, body - overview - menu_height, width - 1),
        "actions": Rect(height - 2 - menu_height, 0, menu_height, width - 1),
    }


def status_label(value: object) -> tuple[str, str]:
    if value is True:
        return "READY", "good"
    if value is False:
        return "OFF", "muted"
    return "UNKNOWN", "warn"


def ssh_label(snapshot: dict[str, Any]) -> tuple[str, str]:
    if snapshot.get("running") is True and snapshot.get("http_ready") is not True:
        return "STARTING", "warn"
    return status_label(snapshot.get("running"))


SHORT_MENU = [
    ("e", "Session"),
    ("n", "New G4"),
    ("C", "New CPU"),
    ("m", "Drive"),
    ("c", "Settings"),
    ("f", "Full startup"),
    ("d", "Deploy"),
    ("i", "Install"),
    ("w", "Cache"),
    ("p", "Prepare"),
    ("a", "Start"),
    ("h", "SSH on"),
    ("H", "SSH off"),
    ("o", "Open UI"),
    ("r", "Render H3"),
    ("t", "PNG smoke"),
    ("s", "Stop services"),
    ("X", "Release VM"),
    ("q", "Keep & quit"),
]


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
        self.theme = Theme()
        self.demo = isinstance(backend, DemoBackend)
        self.action_input = ""
        self.pending_sessions: list[dict[str, str]] | None = None

    def _terminal(self, screen: Any, operation: Callable[[], Any]) -> Any:
        if screen is None:
            try:
                return operation()
            finally:
                if sys.stdin.isatty():
                    try:
                        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
                    except (termios.error, OSError, ValueError):
                        pass
        curses.def_prog_mode()
        curses.endwin()
        try:
            return operation()
        finally:
            curses.reset_prog_mode()
            curses.flushinp()
            screen.clear()
            screen.refresh()

    def _configure(self, ask: Callable[[str], str] | None = None) -> None:
        ask = ask or input
        candidate = replace(self.config)
        storage = "ephemeral" if candidate.ephemeral else candidate.storage_root
        value = ask(f"Storage [{storage}] (or ephemeral): ").strip()
        if value:
            candidate.ephemeral = value == "ephemeral"
            if not candidate.ephemeral:
                candidate.storage_root = value
        value = ask(
            f"Access public / local-only / email [{candidate.access}]: "
        ).strip()
        if value:
            if value not in ("public", "local-only", "email"):
                raise DashboardError("Access must be public, local-only or email.")
            candidate.access = value
        if candidate.access == "email":
            value = ask(
                f"Allowed email [{candidate.email}] (OTP remains in your browser): "
            ).strip()
            if value:
                candidate.email = value
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", candidate.email):
                raise DashboardError("Enter a valid allowed email.")
        value = (
            ask(f"CPU mode yes/no [{'yes' if candidate.cpu else 'no'}]: ")
            .strip()
            .lower()
        )
        if value:
            if value not in ("yes", "no"):
                raise DashboardError("CPU choice must be yes or no.")
            candidate.cpu = value == "yes"
        if candidate.access == "local-only":
            value = ask(f"SSH local port [{candidate.local_port}]: ").strip()
            proposed_port = candidate.local_port
            if value:
                port = int(value)
                if not 1024 <= port <= 65535:
                    raise DashboardError("Port must be between 1024 and 65535.")
                proposed_port = port
            value = ask(
                f"SSH identity path [{candidate.identity or DEFAULT_IDENTITY}]: "
            ).strip()
            proposed_identity = candidate.identity
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
            candidate.local_port = proposed_port
            candidate.identity = proposed_identity
            identity = Path(candidate.identity or DEFAULT_IDENTITY)
            if not identity.exists():
                value = (
                    ask(
                        f"Dedicated key absent. Create it on SSH start? yes/no [{'yes' if candidate.create_key else 'no'}]: "
                    )
                    .strip()
                    .lower()
                )
                if value:
                    if value not in ("yes", "no"):
                        raise DashboardError("Key creation choice must be yes or no.")
                    candidate.create_key = value == "yes"
                if not candidate.create_key:
                    raise DashboardError(
                        "No SSH key selected; choose key creation or public/email access before full startup."
                    )
        value = (
            ask(
                f"Rehash Drive model cache during prepare? yes/no [{'yes' if candidate.verify_cache else 'no'}]: "
            )
            .strip()
            .lower()
        )
        if value:
            if value not in ("yes", "no"):
                raise DashboardError("Cache audit choice must be yes or no.")
            candidate.verify_cache = value == "yes"
        if not candidate.ephemeral:
            root, mydrive = Path(candidate.storage_root), Path("/content/drive/MyDrive")
            if (
                not root.is_absolute()
                or root == mydrive
                or mydrive not in root.parents
                or ".." in root.parts
            ):
                raise DashboardError(
                    "Use a dedicated directory inside /content/drive/MyDrive."
                )
        self.config = candidate

    def _select(self, screen: Any, rows: list[dict[str, str]]) -> None:
        if not rows:
            self.message = (
                "No active named sessions; C + Enter creates CPU, n + Enter creates G4."
            )
            return
        choices = [
            f"{index}. {row['name']} ({row['hardware']})"
            for index, row in enumerate(rows, 1)
        ]
        name = self._prompt(
            screen, "Select a session number or exact name (blank cancels):", choices
        ).strip()
        if name.isdecimal() and 1 <= int(name) <= len(rows):
            name = rows[int(name) - 1]["name"]
        if name:
            selected = next((row for row in rows if row["name"] == name), None)
            if selected is None:
                raise DashboardError(
                    "Select one named session from the displayed list."
                )
            candidate = replace(
                self.config, session=name, cpu=selected["hardware"] == "CPU"
            )
            require_session(candidate)
            self.config = candidate
            self.status, self.ssh = {}, {}
            self.updated, self.last_refresh = 0.0, 0.0
            self.message = "Session selected; status will show deployment and services."

    def _new(self, cpu: bool) -> None:
        name = (
            "launcher-"
            + ("cpu-" if cpu else "g4-")
            + time.strftime("%Y%m%d-%H%M%S-")
            + uuid.uuid4().hex[:6]
        )
        candidate = replace(self.config, session=name, cpu=cpu)
        if self.worker.submit("new", candidate):
            self.message = f"Creating one {'CPU' if cpu else 'G4'} session; waiting for provider status."

    def _mount(self) -> None:
        require_session(self.config)
        if self.config.ephemeral:
            self.message = "Ephemeral storage selected; no Drive mount is needed."
            return
        print(
            "Drive authorization uses the provider terminal/browser. The dashboard resumes afterward."
        )
        code = self.backend.interactive(["drivemount", "-s", self.config.session])
        if code:
            raise DashboardError(
                "Drive mount did not succeed. Finish provider authorization, then mount again explicitly."
            )

    def _full_inputs(self, ask: Callable[[str], str] | None = None) -> None:
        require_session(self.config)
        self._configure(ask)
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
                self.status, self.updated = value, time.monotonic()
                deployment = (value.get("deployment") or {}).get("status")
                if deployment == "not_deployed":
                    self.message = NOT_DEPLOYED_NOTICE
                elif deployment == "deployed" and self.message == NOT_DEPLOYED_NOTICE:
                    installation = value.get("installation") or {}
                    self.message = (
                        f"Launcher deployed; installation {installation.get('status', 'unknown')}"
                        f" / {installation.get('phase') or self.stage}."
                    )
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
            elif kind == "session":
                config, provider = value
                self.action_input = ""
                self.config = config
                self.status = {
                    "provider": provider,
                    "runtime": {"gpu_name": provider["hardware"]},
                }
                self.ssh = {}
                self.updated, self.last_refresh = 0.0, 0.0
                self.message = (
                    "Created selected session; no launcher services have been started."
                )
            elif kind == "sessions":
                self.pending_sessions = value
            elif kind == "released":
                config, cleanup_error = value
                self.message = "Selected VM release verified."
                if cleanup_error:
                    self.message += " Service cleanup warning: " + safe_error(
                        cleanup_error
                    )
                if self.config.session == config.session:
                    self.action_input = ""
                    self.config.session = ""
                    self.status, self.ssh = {}, {}
                    self.updated = 0.0

    def _prompt(
        self, screen: Any, prompt: str, choices: list[str] | None = None
    ) -> str:
        if screen is None:
            if choices:
                print("\n".join(choices))
            return input(prompt)
        value = ""
        while True:
            self._draw(screen)
            self._draw_prompt(screen, prompt, value, choices or [])
            screen.refresh()
            try:
                key = screen.getkey()
            except curses.error:
                continue
            if key in ("\n", "\r", "KEY_ENTER"):
                return value
            if key == "\x1b":
                raise FormCancelled("Form cancelled; configuration unchanged.")
            if key in ("KEY_BACKSPACE", "\b", "\x7f"):
                value = value[:-1]
            elif len(key) == 1 and key.isprintable() and len(value) < 300:
                value += key

    def _draw_prompt(
        self, screen: Any, prompt: str, value: str, choices: list[str]
    ) -> None:
        height, width = screen.getmaxyx()
        card_width = max(8, min(88, width - 2))
        lines = textwrap.wrap(prompt, max(1, card_width - 4))
        available = max(0, height - len(lines) - 7)
        visible_choices = choices[:available]
        if len(visible_choices) < len(choices) and available:
            visible_choices[-1] = "More sessions: enter their exact name."
        card_height = min(height - 2, len(lines) + len(visible_choices) + 5)
        rect = Rect(
            max(0, (height - card_height) // 2),
            max(0, (width - card_width) // 2),
            card_height,
            card_width,
        )
        for row in range(rect.row, rect.row + rect.height):
            self._write(screen, row, rect.column, " " * rect.width, rect.width)
        self._card(screen, rect, "INPUT / Enter confirms / Esc cancels", "title")
        for offset, text in enumerate([*visible_choices, *lines], 1):
            if offset < rect.height - 3:
                self._write(
                    screen, rect.row + offset, rect.column + 2, text, rect.width - 4
                )
        self._write(
            screen,
            rect.row + rect.height - 3,
            rect.column + 2,
            "> " + value[-max(1, rect.width - 7) :] + "_",
            rect.width - 4,
            "accent",
        )
        self._write(
            screen,
            rect.row + rect.height - 2,
            rect.column + 2,
            "Blank Enter keeps default; Esc cancels.",
            rect.width - 4,
            "muted",
        )

    def _write(
        self,
        screen: Any,
        row: int,
        column: int,
        text: object,
        width: int,
        role: str = "normal",
    ) -> None:
        height, columns = screen.getmaxyx()
        if row < 0 or row >= height or column < 0 or column >= columns - 1:
            return
        clipped = clip_cells(text, max(0, min(width, columns - column - 1)))
        try:
            screen.addnstr(row, column, clipped, len(clipped), self.theme.roles[role])
        except (curses.error, UnicodeError):
            # A terminal's advertised encoding can exceed its actual capability.
            try:
                screen.addnstr(
                    row,
                    column,
                    clipped.encode("ascii", "replace").decode(),
                    len(clipped),
                    self.theme.roles[role],
                )
            except curses.error:
                pass

    def _card(self, screen: Any, rect: Rect, title: str, role: str = "accent") -> None:
        corners = (
            ("╭", "╮", "╰", "╯", "─", "│")
            if self.theme.unicode
            else ("+", "+", "+", "+", "-", "|")
        )
        tl, tr, bl, br, horizontal, vertical = corners
        self._write(
            screen,
            rect.row,
            rect.column,
            tl + horizontal * (rect.width - 2) + tr,
            rect.width,
            "muted",
        )
        self._write(
            screen,
            rect.row + rect.height - 1,
            rect.column,
            bl + horizontal * (rect.width - 2) + br,
            rect.width,
            "muted",
        )
        for row in range(rect.row + 1, rect.row + rect.height - 1):
            self._write(screen, row, rect.column, vertical, 1, "muted")
            self._write(screen, row, rect.column + rect.width - 1, vertical, 1, "muted")
        self._write(
            screen, rect.row, rect.column + 2, " " + title + " ", rect.width - 4, role
        )

    def _overview(self, screen: Any, rect: Rect) -> None:
        self._card(screen, rect, "WORKSPACE", "title")
        runtime, drive = (
            self.status.get("runtime") or {},
            self.status.get("drive") or {},
        )
        install = self.status.get("installation") or {}
        gpu = str(
            runtime.get("gpu_name") or ("CPU" if self.config.cpu else "not connected")
        )
        memory = human_bytes(float(runtime.get("gpu_memory_mib", 0) or 0) * 1024**2)
        comfy, http, tunnel = (
            status_label(value)[0]
            for value in (
                self.status.get("comfyui_alive"),
                self.status.get("http_ready"),
                self.status.get("tunnel_alive"),
            )
        )
        ssh = ssh_label(self.ssh)[0]
        mounted = (
            "MOUNTED"
            if drive.get("mounted")
            else ("EPHEMERAL" if self.config.ephemeral else "UNMOUNTED")
        )
        install_state = str(install.get("status", "unknown")).upper()
        mode = self.status.get("access_mode") or self.config.access
        model_state = (
            "SKIPPED (CPU)"
            if self.config.cpu
            else ("READY" if self.status.get("models_ready") else "waiting")
        )
        if rect.height <= 6:
            rows = [
                (
                    f"GPU {compact_name(gpu, 26)}  VRAM {memory}  Python {runtime.get('python', '?')}",
                    "normal",
                ),
                (
                    f"Drive {mounted}   Install {install_state} / {compact_name(install.get('phase', '?'), 20)}",
                    "bad"
                    if install.get("status") == "failed"
                    else ("good" if drive.get("mounted") else "warn"),
                ),
                (
                    f"Comfy {comfy}   HTTP {http}   Cloudflare {tunnel}   SSH {ssh}",
                    "good" if self.status.get("http_ready") else "warn",
                ),
                (
                    f"Access {mode}   Models {model_state}   URL {self._url() or 'not ready'}",
                    "accent",
                ),
            ]
        else:
            rows = [
                (f"GPU       {gpu}", "normal"),
                (f"VRAM      {memory}  Python {runtime.get('python', '?')}", "muted"),
                (f"Drive     {mounted}", "good" if drive.get("mounted") else "warn"),
                (
                    f"Install   {install_state} / {install.get('phase', '?')}",
                    "bad" if install.get("status") == "failed" else "good",
                ),
                (
                    f"Comfy     {comfy}   HTTP {http}",
                    "good" if self.status.get("http_ready") else "warn",
                ),
                (f"Access    {mode}   Cloudflare {tunnel}", "normal"),
                (
                    f"SSH       {ssh}   Models {model_state}",
                    "good" if self.ssh.get("http_ready") else "muted",
                ),
                (f"URL       {self._url() or 'not ready'}", "accent"),
            ]
        for offset, (text, role) in enumerate(rows[: rect.height - 2], 1):
            self._write(
                screen, rect.row + offset, rect.column + 2, text, rect.width - 4, role
            )

    def _model_rows(self, width: int) -> list[tuple[str, str]]:
        if (self.status.get("deployment") or {}).get("status") == "not_deployed":
            return [
                ("Deployment NOT_DEPLOYED", "warn"),
                ("d + Enter deploys; f + Enter runs setup.", "normal"),
            ]
        jobs = [
            ("model_prepare", "PREPARE"),
            ("model_download", "DOWNLOAD"),
            ("render", "RENDER"),
        ]
        jobs.sort(key=lambda item: not (self.status.get(item[0]) or {}).get("running"))
        chosen = next(
            (
                item
                for item in jobs
                if (self.status.get(item[0]) or {}).get("progress")
                or (self.status.get(item[0]) or {}).get("running")
                or (self.status.get(item[0]) or {}).get("status") == "failed"
            ),
            jobs[0],
        )
        task = self.status.get(chosen[0]) or {}
        progress = task.get("progress") or {}
        state, phase = task.get("status", "waiting"), progress.get("phase", "")
        rows = [
            (
                f"{chosen[1]}  {state.upper()}" + (f" / {phase}" if phase else ""),
                "bad" if state == "failed" else "accent",
            )
        ]
        files = progress.get("files") or []
        for index, item in enumerate(files[:6], 1):
            if not isinstance(item, dict):
                continue
            phase = str(item.get("phase", item.get("status", "queued")))[:10]
            filename = compact_name(
                item.get("path", "file"), max(12, width - len(phase) - 9)
            )
            rows.append((f"{index:02d}  {filename} / {phase}", "muted"))
            done, total, rate = (
                human_bytes(item.get(key))
                for key in ("done_bytes", "total_bytes", "rate_bytes_per_second")
            )
            try:
                fraction = max(
                    0,
                    min(
                        1,
                        float(item.get("done_bytes", 0))
                        / float(item.get("total_bytes", 0)),
                    ),
                )
                percent = f"{fraction * 100:.0f}%"
            except (TypeError, ValueError, ZeroDivisionError):
                fraction, percent = 0.0, "?%"
            metrics = f"{percent:>4} {done}/{total} {rate}/s"
            length = max(4, min(12, width - len(metrics) - 3))
            filled = round(fraction * length)
            full, empty = ("█", "░") if self.theme.unicode else ("#", "-")
            meter = full * filled + empty * (length - filled)
            rows.append((f"{meter}  {metrics}", "good" if fraction == 1 else "title"))
        if not files:
            result = task.get("result") or {}
            if result.get("ok") is False:
                rows.append(
                    (
                        safe_error(result.get("error", "Task failed; inspect status.")),
                        "bad",
                    )
                )
            elif not self.config.session:
                rows.extend(
                    [
                        ("Your GPU workspace starts here.", "title"),
                        ("Choose [n] New G4 or [e] an existing session.", "muted"),
                        ("Then [f] handles setup and preparation.", "normal"),
                    ]
                )
            else:
                rows.extend(
                    [
                        (
                            f"Download {(self.status.get('model_download') or {}).get('status', 'unknown')}   Render {(self.status.get('render') or {}).get('status', 'unknown')}",
                            "muted",
                        ),
                        (
                            "Temporary VM assets; releasing the VM deletes them."
                            if self.config.ephemeral
                            else "Models load from VM disk; assets stay in Drive.",
                            "muted",
                        ),
                    ]
                )
        return rows

    def _job_badges(self) -> tuple[str, str]:
        badges = []
        failed = False
        for key, label in (
            ("model_download", "C"),
            ("model_prepare", "P"),
            ("render", "R"),
        ):
            state = (self.status.get(key) or {}).get("status", "?")
            failed = failed or state in ("failed", "interrupted")
            short = {
                "succeeded": "OK",
                "running": "RUN",
                "starting": "RUN",
                "failed": "FAIL",
                "interrupted": "FAIL",
            }.get(state, state[:5].upper())
            badges.append(f"{label}:{short}")
        return " ".join(badges), "bad" if failed else "accent"

    def _models(self, screen: Any, rect: Rect) -> None:
        room = rect.height - 2
        rows = self._model_rows(rect.width - 4)
        pages = max(1, (len(rows) + room - 1) // room)
        self.progress_page %= pages
        badges, role = self._job_badges()
        self._card(
            screen,
            rect,
            f"MODELS  {badges}  {self.progress_page + 1}/{pages}",
            role,
        )
        for offset, (text, role) in enumerate(
            rows[self.progress_page * room : (self.progress_page + 1) * room], 1
        ):
            self._write(
                screen, rect.row + offset, rect.column + 2, text, rect.width - 4, role
            )

    def _actions(self, screen: Any, rect: Rect) -> None:
        self._card(screen, rect, "ACTIONS  code + Enter", "title")
        if not self.show_menu:
            self._write(
                screen,
                rect.row + 1,
                rect.column + 2,
                "Tab: all actions   f: startup   s: stop   X: release   q: keep",
                rect.width - 4,
                "muted",
            )
            return
        columns = action_columns(rect.width)
        slot = (rect.width - 4) // columns
        for index, (key, label) in enumerate(SHORT_MENU):
            row, column = (
                rect.row + 1 + index // columns,
                rect.column + 2 + index % columns * slot,
            )
            if row >= rect.row + rect.height - 1:
                break
            role = "bad" if key == "X" else ("accent" if key == "f" else "title")
            self._write(screen, row, column, f"[{key}]", 3, role)
            self._write(screen, row, column + 4, label, slot - 5)

    def _compact(self, screen: Any) -> None:
        height, width = screen.getmaxyx()
        runtime, install = (
            self.status.get("runtime") or {},
            self.status.get("installation") or {},
        )
        http, ssh = (
            status_label(self.status.get("http_ready"))[0],
            ssh_label(self.ssh)[0],
        )
        self._write(
            screen,
            2,
            0,
            f"GPU {compact_name(runtime.get('gpu_name', '?'), 12)} HTTP {http} SSH {ssh}",
            width - 1,
        )
        self._write(
            screen,
            3,
            0,
            f"Drive {'MOUNTED' if (self.status.get('drive') or {}).get('mounted') else 'UNKNOWN'} Install {install.get('status', '?')}",
            width - 1,
            "accent",
        )
        badges, role = self._job_badges()
        self._write(
            screen,
            4,
            0,
            f"{badges} | Resize 80x24" if width >= 32 else "Resize to 80x24; PgDn",
            width - 1,
            role,
        )
        columns = max(1, min(4, (width - 1) // 9))
        room = max(1, height - 7)
        pages = max(1, (len(SHORT_MENU) + room * columns - 1) // (room * columns))
        self.progress_page %= pages
        slot = max(1, (width - 1) // columns)
        start = self.progress_page * room * columns
        for index, (key, label) in enumerate(
            SHORT_MENU[start : start + room * columns]
        ):
            self._write(
                screen,
                5 + index // columns,
                index % columns * slot,
                f"[{key}] {label}",
                slot - 1,
                "bad" if key == "X" else "title",
            )

    def _draw(self, screen: Any) -> None:
        height, width = screen.getmaxyx()
        screen.erase()
        badge = "DEMO / OFFLINE" if self.demo else "GPU WORKSPACE"
        self._write(
            screen, 0, 0, "> COLAB / COMFYUI", max(0, width - len(badge) - 3), "title"
        )
        self._write(
            screen, 0, max(0, width - len(badge) - 1), badge, len(badge), "accent"
        )
        age = time.monotonic() - self.updated if self.updated else None
        stale = age is not None and (age > 2 * REFRESH_SECONDS or bool(self.error))
        freshness = (
            "WAITING"
            if age is None
            else (f"[STALE] {age:.0f}s" if stale else f"LIVE {age:.0f}s")
        )
        self._write(
            screen,
            1,
            0,
            f"{compact_name(self.config.session or 'choose a session', 24)}  /  {self.stage}",
            max(0, width - len(freshness) - 3),
            "normal",
        )
        self._write(
            screen,
            1,
            max(0, width - len(freshness) - 1),
            freshness,
            len(freshness),
            "warn" if stale else "muted",
        )
        layout = card_layout(height, width, self.show_menu)
        if layout:
            self._overview(screen, layout["overview"])
            self._models(screen, layout["models"])
            self._actions(screen, layout["actions"])
        else:
            self._compact(screen)
        failed_result = next(
            (
                safe_error(
                    ((self.status.get(key) or {}).get("result") or {}).get(
                        "error", "Task failed; inspect status."
                    )
                )
                for key in ("model_prepare", "model_download", "render")
                if ((self.status.get(key) or {}).get("result") or {}).get("ok") is False
            ),
            "",
        )
        notice = self.error or failed_result or self.message
        # Keep a ready endpoint copyable even when the workspace card is narrow.
        if (
            not self.demo
            and not self.worker.busy
            and not (self.error or failed_result)
            and self._url()
        ):
            notice = f"[o] {self._url()}"
        self._write(
            screen,
            height - 2,
            0,
            ("! " if self.error or failed_result else "> ") + notice,
            width - 1,
            "bad" if self.error or failed_result else "accent",
        )
        footer = (
            f"Action> {self.action_input or '_'}  Enter executes | q keep resources | X release VM | Tab details"
            if width >= 76
            else f"Action> {self.action_input or '_'} Enter | q keep | X release"
        )
        if width < 40:
            footer = "q keep | X release"
        self._write(screen, height - 1, 0, footer, width - 1, "muted")
        screen.refresh()

    def _action(self, screen: Any, key: str) -> None:
        if self.demo:
            self.message = "Offline demo: action keys are disabled; Tab/PgDn explore, q + Enter exits."
            return
        if self.worker.busy and key in ("e", "n", "C", "m", "c", "f", "o"):
            self.message = "Wait for the worker before using interactive actions. q retains resources."
            return
        self.error = ""
        if key == "e":
            if self.worker.submit("sessions", self.config):
                self.message = "Loading named sessions for in-screen selection."
        elif key in ("n", "C"):
            self._new(key == "C")
        elif key in ("c", "f"):
            try:
                ask = lambda prompt: self._prompt(screen, prompt)
                if key == "f":
                    self._full_inputs(ask)
                    if not self.config.ephemeral:
                        self.message = "Drive authorization requires the provider terminal/browser; returning to the TUI afterward."
                        self._terminal(screen, self._mount)
                    self.worker.submit("pipeline", self.config)
                else:
                    self._configure(ask)
                    self.message = "Settings updated in memory."
            finally:
                if screen is not None:
                    curses.flushinp()
        elif key == "m":
            if self.config.ephemeral:
                self._mount()
            else:
                self.message = "Drive authorization uses the provider terminal/browser, then returns here."
                self._terminal(screen, self._mount)
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

    def _key(self, screen: Any, key: str) -> bool:
        """Typing never executes an action. Enter submits exactly one command."""
        if key == "\t":
            self.show_menu = not self.show_menu
            self.progress_page = 0
        elif key in ("KEY_NPAGE", "KEY_PPAGE"):
            self.progress_page += 1 if key == "KEY_NPAGE" else -1
        elif key == "\x1b":
            self.action_input = ""
        elif key in ("KEY_BACKSPACE", "\b", "\x7f"):
            self.action_input = self.action_input[:-1]
        elif key in ("\n", "\r", "KEY_ENTER"):
            action, self.action_input = self.action_input, ""
            if action == "q":
                return False
            if action:
                if action not in dict(MENU):
                    raise DashboardError(
                        "Enter one action code from the menu; Esc clears input."
                    )
                self._action(screen, action)
        elif len(key) == 1 and key.isprintable() and len(self.action_input) < 8:
            self.action_input += key
        return True

    def _session_form(self, screen: Any) -> None:
        if self.pending_sessions is None or self.worker.busy:
            return
        rows, self.pending_sessions = self.pending_sessions, None
        self.action_input = ""
        try:
            self._select(screen, rows)
        except FormCancelled as exc:
            self.message = str(exc)
        except DashboardError as exc:
            self.error = safe_error(exc)
        finally:
            self.action_input = ""
            if screen is not None:
                curses.flushinp()

    def run(self, screen: Any) -> None:
        self.theme.initialize()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(200)
        recovering = False
        try:
            while True:
                self._events(screen)
                self._session_form(screen)
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
                try:
                    if not self._key(screen, key):
                        break
                except FormCancelled as exc:
                    self.message = str(exc)
                except (DashboardError, ValueError, EOFError) as exc:
                    self.error = safe_error(exc)
        except curses.error:
            recovering = True
            raise
        finally:
            if not recovering:
                self.worker.close()

    def _plain_snapshot(self) -> str:
        runtime, installation = (
            self.status.get("runtime") or {},
            self.status.get("installation") or {},
        )
        age = time.monotonic() - self.updated if self.updated else None
        freshness = (
            "unknown"
            if age is None
            else f"{age:.0f}s ago"
            + (" STALE" if age > 2 * REFRESH_SECONDS or self.error else "")
        )
        lines = [
            "COLAB / COMFYUI" + (" [DEMO / OFFLINE]" if self.demo else ""),
            f"Session: {self.config.session or '(none)'} | Stage: {self.stage} | Updated: {freshness}",
            f"GPU: {runtime.get('gpu_name', 'unknown')} | Install: {installation.get('status', 'unknown')} | Drive: {(self.status.get('drive') or {}).get('mounted', 'unknown')}",
            f"HTTP: {self.status.get('http_ready', 'unknown')} | SSH: {self.ssh.get('running', 'unknown')} | URL: {self._url() or '(not ready)'}",
            *progress_lines(self.status),
            self.error or self.message,
            " | ".join(f"[{key}] {label}" for key, label in SHORT_MENU),
            "q keeps resources; X releases selected VM; s stops services. Type a key + Enter.",
        ]
        return "\n".join(lines)

    def run_plain(self) -> None:
        """Canonical-input fallback, without ANSI or curses initialization."""
        print("Text mode: this terminal cannot display the full-screen interface.")
        previous = None
        try:
            while True:
                self._events(None)
                self._session_form(None)
                now = time.monotonic()
                if (
                    self.config.session
                    and now - self.last_refresh >= REFRESH_SECONDS
                    and not self.worker.busy
                    and self.worker.submit("status", self.config)
                ):
                    self.last_refresh = now
                current = (
                    self.updated,
                    self.stage,
                    self.error,
                    self.message,
                    self.worker.busy,
                )
                if current != previous:
                    print("\n" + self._plain_snapshot(), flush=True)
                    previous = current
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                line = sys.stdin.readline()
                if not line:
                    break
                key = line.strip()
                if key:
                    try:
                        self.action_input = key
                        if not self._key(None, "\n"):
                            break
                    except FormCancelled as exc:
                        self.message = str(exc)
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
        help="Explicit VM-local models/assets, without Drive persistence",
    )
    parser.add_argument(
        "--gpu", default="G4", choices=("G4", "H100", "A100", "L4", "T4")
    )
    parser.add_argument(
        "--classic", action="store_true", help="Open the advanced action-code dashboard"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Offline read-only visual fixture; never execute provider, SSH or browser actions",
    )
    parser.add_argument(
        "--demo-state",
        choices=("prepare", "ready", "error"),
        default="prepare",
        help="State shown by --demo (default: prepare)",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Use monochrome styling"
    )
    args = parser.parse_args(argv)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive and not args.demo:
        parser.error(
            "Run the dashboard in an interactive terminal; Drive authentication needs its TTY."
        )
    config = Config(
        session=args.session or ("demo-g4" if args.demo else ""),
        storage_root=args.storage_root,
        cpu=args.cpu,
        ephemeral=args.ephemeral,
        gpu=args.gpu,
    )
    backend = DemoBackend(args.demo_state) if args.demo else Backend()
    if not args.classic:
        sys.modules.setdefault("dashboard", sys.modules[__name__])
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        from wizard import Wizard

        wizard = Wizard(config, backend)
        wizard.theme = Theme(color=not args.no_color)
        try:
            if not interactive:
                print(wizard.snapshot())
            elif os.environ.get("TERM", "dumb") == "dumb":
                wizard.plain()
            else:
                try:
                    curses.wrapper(wizard.run)
                except curses.error:
                    wizard.plain()
        except KeyboardInterrupt:
            pass
        finally:
            wizard.worker.close()
        print(
            "Wizard closed. Any retained VM, services and SSH forwarding remain running."
        )
        return 0
    dashboard = Dashboard(config, backend)
    dashboard.theme = Theme(color=not args.no_color)
    if args.demo:
        dashboard.status = backend.bridge(config, "status")
        dashboard.ssh = backend.ssh(config, "status")
        dashboard.updated = time.monotonic()
        dashboard.message = (
            "Offline demo: no VM, credentials, browser or commands are used."
        )
    try:
        if not interactive:
            print(dashboard._plain_snapshot())
        elif os.environ.get("TERM", "dumb") == "dumb":
            dashboard.run_plain()
        else:
            try:
                curses.wrapper(dashboard.run)
            except curses.error:
                dashboard.run_plain()
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.worker.close()
    print(
        "Offline demo closed; no resources were created."
        if args.demo
        else "Dashboard closed. Remote resources were retained; explicitly release the selected VM when finished."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
