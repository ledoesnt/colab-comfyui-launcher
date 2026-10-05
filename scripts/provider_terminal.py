"""Keep an interactive provider CLI inside the dashboard's own screen.

The official CLI opens ``/dev/tty`` during Drive authorization, so ordinary
stdin/stdout pipes are insufficient. A small exec wrapper acquires a separate
controlling PTY without using Python's unsafe ``preexec_fn`` in worker threads.
This module never allocates/releases a runtime, opens a browser, or writes a
transcript. The provider CLI still manages its own authentication and history.

Events and authorization URLs are in-memory UI data, not safe log records.
Consumers must mask code input and exclude authorization screens from captures.
Successful CLI exit is not proof that Drive mounted; verify remote status next.
"""

from __future__ import annotations

import codecs
import errno
import fcntl
import math
import os
import re
import selectors
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

Event = dict[str, Any]
MAX_LINE = 16384
MAX_EVENT_TEXT = 2048
MAX_DISPLAY_TEXT = 131072
URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
CONFIRM = re.compile(
    r"press\s+(?:the\s+)?(?:enter|return)\s+after\s+you\s+have\s+granted\s+access",
    re.IGNORECASE,
)
CODE = re.compile(
    r"(?:enter|paste)\s+(?:the\s+|your\s+)?(?:authorization|authentication|verification|oauth)\s+code\s*:",
    re.IGNORECASE,
)
CREDENTIAL_PREFIX = (
    r"(?i)(\b(?:access[_ -]?token|refresh[_ -]?token|id[_ -]?token|client[_ -]?secret|"
    r"authorization|cookie|token|code)\b[\"']?[ \t]*[:=][ \t]*)"
)
CREDENTIAL = re.compile(CREDENTIAL_PREFIX + r"[^\r\n]+")
CREDENTIAL_VALUE_NEXT_LINE = re.compile(CREDENTIAL_PREFIX + r"$")
BEARER = re.compile(r"(?i)(\bbearer[ \t]+)[^\s\"'<>;,]+")


def _process_identity(pid: int) -> tuple[int, int, str] | None:
    """Read only process metadata, never the CLI's credential store."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[2]), int(fields[3]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


class _TerminalText:
    """Incrementally strip CSI/OSC and terminal controls across read chunks."""

    def __init__(self) -> None:
        self.state = "text"

    def feed(self, value: str) -> str:
        result: list[str] = []
        for character in value:
            if self.state == "escape":
                self.state = (
                    "csi" if character == "[" else "osc" if character == "]" else "text"
                )
            elif self.state == "csi":
                if "@" <= character <= "~":
                    self.state = "text"
            elif self.state == "osc":
                if character == "\x07":
                    self.state = "text"
                elif character == "\x1b":
                    self.state = "osc_escape"
            elif self.state == "osc_escape":
                self.state = "text" if character == "\\" else "osc"
            elif character == "\x1b":
                self.state = "escape"
            elif character == "\n":
                result.append(character)
            elif character == "\t":
                result.append(" ")
            elif character.isprintable():
                result.append(character)
        return "".join(result)


class ProviderTerminal:
    """An asynchronous, bounded interactive CLI with explicit user responses.

    ``start`` returns this instance. ``on_event`` runs on a reader thread and
    should normally put events into the UI queue. ``pending`` is ``confirm``,
    ``code`` or ``None``. ``respond('')`` confirms browser consent, while a
    nonempty value is accepted only for a pending code prompt. No input is sent
    automatically. ``close`` cancels this CLI process group, preserving the VM.
    """

    def __init__(
        self,
        argv: Sequence[str],
        on_event: Callable[[Event], None],
        timeout: float = 650,
    ) -> None:
        if (
            isinstance(argv, (str, bytes))
            or not argv
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
            or not argv[0]
        ):
            raise ValueError("Provider command must be a nonempty argument list.")
        if not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("Provider timeout must be between 0 and 3600 seconds.")
        self.argv = tuple(argv)
        self.on_event = on_event
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._identity: tuple[int, int, str] | None = None
        self._master: int | None = None
        self._pending: str | None = None
        self._secrets: list[str] = []
        self._urls: set[str] = set()
        self._line = ""
        self._discard_line = False
        self._filter = _TerminalText()
        self._hide_next_value = False
        self._displayed = 0
        self._callback_failed = False
        self._finished = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and not self._finished

    @property
    def pending(self) -> str | None:
        with self._lock:
            return self._pending

    def start(self) -> ProviderTerminal:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Provider terminal can only be started once.")
            self._thread = threading.Thread(
                target=self._run, name="provider-terminal", daemon=True
            )
            self._thread.start()
        return self

    def respond(self, value: str = "") -> bool:
        """Send only a response requested by the provider, never raw UI keys."""
        with self._lock:
            if self._pending is None or self._master is None or self._finished:
                return False
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                return False
            if self._pending == "confirm" and value:
                return False
            if self._pending == "code" and (
                not value or len(value) > 2048 or not value.isprintable()
            ):
                return False
            # Retain just enough in memory to suppress an unexpected CLI echo.
            if value:
                self._secrets.append(value)
                self._secrets = self._secrets[-8:]
            try:
                data = (value + "\n").encode("utf-8")
                while data:
                    written = os.write(self._master, data)
                    data = data[written:]
            except OSError:
                return False
            self._pending = None
            return True

    def close(self) -> None:
        self._cancel.set()
        self._terminate()

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _emit(self, event: Event) -> None:
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 - callback failures must cancel owned CLI
            # A broken UI handler must not strand an invisible auth process.
            self._callback_failed = True
            self._cancel.set()

    def _safe_text(self, text: str) -> str:
        with self._lock:
            secrets = tuple(self._secrets)
        for secret in secrets:
            text = text.replace(secret, "[hidden code]")
        if self._hide_next_value:
            if text.strip():
                self._hide_next_value = False
                return "[hidden credential]"
            return ""
        if CREDENTIAL_VALUE_NEXT_LINE.search(text) and not CODE.search(text):
            # A provider error body can pretty-print its token/code value on the
            # next line. Never emit that otherwise unlabelled credential.
            self._hide_next_value = True
            return text.rstrip() + " [hidden credential]"
        text = CREDENTIAL.sub(r"\1[hidden credential]", text)
        return BEARER.sub(r"\1[hidden credential]", text)

    def _output(self, text: str) -> None:
        text = self._safe_text(text).strip()
        if not text or self._displayed >= MAX_DISPLAY_TEXT:
            return
        amount = min(MAX_EVENT_TEXT, MAX_DISPLAY_TEXT - self._displayed)
        text = text[:amount]
        self._displayed += len(text)
        self._emit({"kind": "output", "text": text})

    def _handle_line(self, text: str, *, prompt: bool = False) -> None:
        # URLs, including query credentials, never appear in general output.
        for match in URL.finditer(text):
            url = match.group().rstrip(".,;)")
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                google = host == "google.com" or host.endswith(".google.com")
                allowed = (
                    parsed.scheme == "https"
                    and google
                    and parsed.username is None
                    and parsed.password is None
                    and len(url) <= MAX_LINE
                )
            except ValueError:
                allowed = False
            if allowed and url not in self._urls and len(self._urls) < 8:
                self._urls.add(url)
                self._emit({"kind": "auth_url", "url": url})
        sanitized = URL.sub("[authorization link shown separately]", text)
        code = CODE.search(sanitized)
        confirm = CONFIRM.search(sanitized)
        pending = "code" if code else "confirm" if confirm else None
        if pending:
            with self._lock:
                previous = self._pending
                self._pending = pending
            if previous != pending:
                self._emit(
                    {
                        "kind": f"{pending}_required",
                        "text": (
                            "Enter the authorization code in the masked input."
                            if code
                            else "Finish authorization in the browser, then press Enter."
                        ),
                    }
                )
        if not prompt or not pending:
            self._output(sanitized)

    def _consume(self, value: str) -> None:
        clean = self._filter.feed(value)
        if self._discard_line:
            if "\n" not in clean:
                return
            _, clean = clean.split("\n", 1)
            self._discard_line = False
        self._line += clean
        while "\n" in self._line:
            line, self._line = self._line.split("\n", 1)
            if len(line) <= MAX_LINE:
                self._handle_line(line)
            else:
                self._output("Provider output exceeded the display line limit.")
        # The official /dev/tty prompt deliberately has no trailing newline.
        if CONFIRM.search(self._line) or CODE.search(self._line):
            line, self._line = self._line, ""
            self._handle_line(line, prompt=True)
        elif len(self._line) > MAX_LINE:
            # Drop huge unterminated records instead of leaking a partial URL.
            self._line = ""
            self._discard_line = True
            self._output("Provider output exceeded the display line limit.")

    def _owned_members(self) -> dict[int, tuple[int, int, str]]:
        process, expected = self._process, self._identity
        if process is None or expected is None:
            return {}
        current = _process_identity(process.pid)
        if current is not None and current != expected:
            return {}
        members: dict[int, tuple[int, int, str]] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            identity = _process_identity(pid)
            if (
                identity is not None
                and identity[:2] == (process.pid, process.pid)
                and int(identity[2]) >= int(expected[2])
            ):
                members[pid] = identity
        return members

    def _terminate(self) -> None:
        # Serialize external cancellation against timeout/reader cleanup.
        with self._cleanup_lock:
            process = self._process
            if process is None:
                return
            members = self._owned_members()
            if not members:
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                if not any(
                    _process_identity(pid) == identity
                    for pid, identity in members.items()
                ):
                    return
                time.sleep(0.025)
            # The leader may have exited while an owned descendant ignored TERM.
            # Refuse a recycled leader; otherwise require a still-matching member.
            leader = _process_identity(process.pid)
            if leader is not None and leader != self._identity:
                return
            if any(
                _process_identity(pid) == identity for pid, identity in members.items()
            ):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def _run(self) -> None:
        master: int | None = None
        slave: int | None = None
        timed_out = False
        error: str | None = None
        returncode: int | None = None
        try:
            master, slave = os.openpty()
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--provider-tty-exec",
                    *self.argv,
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave)
            slave = None
            with self._lock:
                self._process = process
                self._identity = _process_identity(process.pid)
                self._master = master
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            deadline = time.monotonic() + self.timeout
            with selectors.DefaultSelector() as selector:
                selector.register(master, selectors.EVENT_READ)
                while True:
                    if self._cancel.is_set():
                        self._terminate()
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._terminate()
                        break
                    ready = selector.select(
                        max(0.0, min(0.1, deadline - time.monotonic()))
                    )
                    if not ready:
                        if process.poll() is not None:
                            break
                        continue
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as exception:
                        if exception.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    self._consume(decoder.decode(chunk))
            self._consume(decoder.decode(b"", final=True))
            if self._line:
                self._handle_line(self._line)
                self._line = ""
            try:
                returncode = process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._terminate()
                returncode = process.wait(timeout=1)
        except Exception:  # noqa: BLE001 - reader failures must clean up owned CLI
            error = (
                "Provider terminal could not complete; verify CLI and session status."
            )
            self._terminate()
        finally:
            for descriptor in (slave, master):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            with self._lock:
                self._master = None
                self._pending = None
                self._finished = True
                self._secrets.clear()
                self._urls.clear()
                self._line = ""
            if self._callback_failed:
                error = "Provider event handler failed; the CLI was cancelled."
            self._emit(
                {
                    "kind": "finished",
                    "returncode": returncode,
                    "cancelled": self._cancel.is_set(),
                    "timed_out": timed_out,
                    **({"error": error} if error else {}),
                }
            )


def _exec_in_controlling_terminal(argv: list[str]) -> None:
    """Runs after exec in a clean interpreter, never as a fork preexec hook."""
    if not argv:
        raise SystemExit(2)
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execvp(argv[0], argv)
    except OSError:
        print("Provider executable could not be started.", file=sys.stderr)
        raise SystemExit(127) from None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--provider-tty-exec":
        _exec_in_controlling_terminal(sys.argv[2:])
    else:
        raise SystemExit("Import ProviderTerminal from the dashboard.")
