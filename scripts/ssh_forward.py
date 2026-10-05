#!/usr/bin/env python3
"""Own a loopback-only OpenSSH forward over the official Colab WebSocket bridge.

This Linux helper never creates or stops a Colab runtime. The CLI's proxy mode
can auto-create a missing session, so we preflight an existing session and give
the proxy a private, single-session snapshot. A missing/expired entry then fails
closed rather than falling through to automatic creation. Runtime host keys use
accept-new and an endpoint-specific known_hosts file; changed keys are rejected.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


class ForwardError(Exception):
    """A deliberately nonsecret diagnostic suitable for JSON output."""


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or any(char in str(path) for char in "\x00\r\n%$"):
        raise argparse.ArgumentTypeError(
            "Use an absolute path without control/%/$ characters"
        )
    return path


def port(value):
    number = int(value)
    if not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1024 and 65535")
    return number


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("-s", "--session", required=True)
    cli.add_argument(
        "action", choices=("start", "status", "stop", "cleanup", "discover")
    )
    cli.add_argument("--local-port", type=port, default=8188)
    cli.add_argument("--remote-port", type=port, default=8188)
    cli.add_argument(
        "--identity",
        type=absolute_path,
        default=Path.home() / ".ssh/colab_comfyui_launcher",
    )
    cli.add_argument(
        "--create-key",
        action="store_true",
        help="Explicitly create a dedicated passphrase-free Ed25519 key if absent; never overwrite",
    )
    cli.add_argument(
        "--state-dir",
        type=absolute_path,
        default=Path.home() / ".local/state/colab-comfyui-launcher/ssh",
    )
    cli.add_argument(
        "--config",
        type=absolute_path,
        default=Path.home() / ".config/colab-cli/sessions.json",
    )
    cli.add_argument("--colab-bin", type=absolute_path)
    cli.add_argument("--ssh-bin", type=absolute_path)
    cli.add_argument("--startup-seconds", type=float, default=30)
    return cli


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ForwardError(
            "Local state/key directory must be owned by you and mode 700"
        )


def inspect_private_dir(path):
    """Validate existing metadata without creating directories or changing modes."""
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ForwardError(
            "Local SSH state directory is not your private regular directory"
        )


def load_private_state(path):
    """Read only the tool's small metadata; never a key or provider snapshot."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 1024 * 1024
        ):
            raise ForwardError(
                "Local SSH state file is not your private regular metadata"
            )
        try:
            value = json.load(stream)
        except (ValueError, UnicodeError):
            raise ForwardError(
                "Local SSH state metadata is unreadable; inspect before reconnecting"
            ) from None
    if not isinstance(value, dict):
        raise ForwardError("Local SSH state metadata must be an object")
    return value


def state_name(session, local_port):
    return hashlib.sha256(f"{session}:{local_port}".encode()).hexdigest()[:24]


def private_write(path, value):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise ForwardError("Unsafe local state file")
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def load_json(path):
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ForwardError("Local metadata is too large")
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ForwardError("Local metadata could not be read") from error
    if not isinstance(value, dict):
        raise ForwardError("Local metadata must be an object")
    return value


def executable(explicit, name):
    path = explicit or shutil.which(name)
    if not path:
        raise ForwardError(f"Required local executable is unavailable: {name}")
    result = absolute_path(str(Path(path).resolve()))
    if not result.is_file() or not os.access(result, os.X_OK):
        raise ForwardError(f"Required local executable is unavailable: {name}")
    return result


def ensure_key(identity, create):
    public = Path(str(identity) + ".pub")
    if not identity.exists():
        if not create:
            raise ForwardError(
                "Dedicated SSH key is absent; use start --create-key explicitly"
            )
        if public.exists() or identity.is_symlink() or public.is_symlink():
            raise ForwardError("Refusing to overwrite existing SSH key material")
        private_dir(identity.parent)
        keygen = executable(None, "ssh-keygen")
        result = subprocess.run(
            [
                str(keygen),
                "-t",
                "ed25519",
                "-f",
                str(identity),
                "-N",
                "",
                "-C",
                "colab-comfyui-launcher",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ForwardError(
                "Dedicated key creation failed; no existing key is overwritten"
            )
    info = identity.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ForwardError("SSH private key must be your regular file with mode 600")
    keygen = executable(None, "ssh-keygen")
    # CLI 0.7.4 derives a public key from -i inside the background ProxyCommand.
    # It cannot reliably prompt there. Reject encrypted keys without modifying
    # them, and never send/print the private key or a passphrase.
    result = subprocess.run(
        [str(keygen), "-y", "-P", "", "-f", str(identity)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise ForwardError(
            "Key must work with an empty passphrase; existing encrypted keys are left unchanged"
        )
    public_text = result.stdout.decode("ascii", errors="replace").strip()
    if not public_text.startswith(
        (
            "ssh-ed25519 ",
            "ecdsa-sha2-nistp256 ",
            "ecdsa-sha2-nistp384 ",
            "ecdsa-sha2-nistp521 ",
        )
    ):
        raise ForwardError("Colab SSH requires an Ed25519 or supported ECDSA key")


def existing_session(args, colab):
    # status in CLI 0.7.4 can exit zero for a missing session. Check the store
    # both before and after its server-side assignment sync; don't trust rc=0.
    if args.session not in load_json(args.config):
        raise ForwardError(
            "Session is absent from the local CLI store; create it separately"
        )
    result = subprocess.run(
        [
            str(colab),
            "--auth=oauth2",
            "--config",
            str(args.config),
            "status",
            "-s",
            args.session,
        ],
        stdin=None,
        stdout=sys.stderr,
        stderr=sys.stderr,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ForwardError(
            "Colab status failed; complete official CLI OAuth interactively first"
        )
    entry = load_json(args.config).get(args.session)
    if not isinstance(entry, dict) or entry.get("name") != args.session:
        raise ForwardError("Session is not active; no runtime will be created")
    if not all(
        isinstance(entry.get(key), str) and entry[key]
        for key in ("endpoint", "url", "token")
    ):
        raise ForwardError("Existing session metadata is incomplete")
    return entry


def process_info(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        proc = Path(f"/proc/{pid}")
        if proc.stat().st_uid != os.getuid():
            return None
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return None
        return {
            "pid": pid,
            "pgid": int(fields[2]),
            "sid": int(fields[3]),
            "start_ticks": fields[19],
        }
    except (OSError, IndexError, ValueError):
        return None


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def alive(owned):
    if (
        not isinstance(owned, dict)
        or not isinstance(owned.get("start_ticks"), str)
        or not owned["start_ticks"].isdigit()
    ):
        return False
    if owned.get("boot_id") != boot_id():
        return False
    current = process_info(owned.get("pid"))
    return bool(
        current
        and current["pgid"] == current["sid"] == current["pid"]
        and current["start_ticks"] == owned["start_ticks"]
    )


def kill_owned(owned):
    if not alive(owned):
        return False
    try:
        os.killpg(owned["pid"], signal.SIGTERM)
        end = time.monotonic() + 3
        while alive(owned) and time.monotonic() < end:
            time.sleep(0.05)
        if alive(owned):
            os.killpg(owned["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def owned_listener(owned, local_port):
    if not alive(owned):
        return False
    try:
        links = {
            os.readlink(path) for path in Path(f"/proc/{owned['pid']}/fd").iterdir()
        }
        address = f"0100007F:{local_port:04X}"
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            fields = line.split()
            if (
                fields[1] == address
                and fields[3] == "0A"
                and f"socket:[{fields[9]}]" in links
            ):
                return True
    except (OSError, IndexError):
        pass
    return False


def healthy(local_port):
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{local_port}/system_stats")
        # Ignore HTTP proxy environment variables for the strictly local origin.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            body = response.read(512 * 1024 + 1)
            if response.status != 200 or len(body) > 512 * 1024:
                return False
        data = json.loads(body)
        return isinstance(data, dict) and "system" in data and "devices" in data
    except (OSError, ValueError):
        return False


def build_command(args, colab, ssh, snapshot, known_hosts, endpoint_hash):
    proxy = shlex.join(
        [
            str(colab),
            "--auth=oauth2",
            "--config",
            str(snapshot),
            "ssh",
            "--proxy-mode",
            "-s",
            args.session,
            "-i",
            str(args.identity),
        ]
    )
    quoted_hosts = (
        '"' + str(known_hosts).replace("\\", "\\\\").replace('"', '\\"') + '"'
    )
    return [
        str(ssh),
        "-F",
        "/dev/null",
        "-N",
        "-T",
        "-a",
        "-L",
        f"127.0.0.1:{args.local_port}:127.0.0.1:{args.remote_port}",
        "-i",
        str(args.identity),
        "-o",
        f"ProxyCommand={proxy}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={quoted_hosts}",
        "-o",
        f"HostKeyAlias=colab-runtime-{endpoint_hash}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "GatewayPorts=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout=15",
        "root@colab-runtime",
    ]


def status(args, state):
    owned = state.get("process")
    running = alive(owned)
    listening = running and owned_listener(owned, args.local_port)
    return {
        "ok": True,
        "running": running,
        "url": f"http://127.0.0.1:{args.local_port}",
        "local_port": args.local_port,
        "remote_port": state.get("remote_port", args.remote_port),
        "http_ready": bool(listening and healthy(args.local_port)),
        "listening_owned": bool(listening),
    }


def discover(args):
    """Restore one live, owned forward for this session, with no state mutation."""
    missing = {
        "ok": True,
        "found": False,
        "session": args.session,
        "running": False,
        "http_ready": False,
        "listening_owned": False,
    }
    try:
        inspect_private_dir(args.state_dir)
    except FileNotFoundError:
        return missing
    matches = []
    for directory in args.state_dir.iterdir():
        if not re.fullmatch(r"[0-9a-f]{24}", directory.name):
            continue
        inspect_private_dir(directory)
        state = load_private_state(directory / "forward.json")
        local_port = state.get("local_port")
        if type(local_port) is not int or not 1024 <= local_port <= 65535:
            if state.get("session") == args.session:
                raise ForwardError(
                    "Recorded SSH port is invalid; inspect before reconnecting"
                )
            continue
        if directory.name != state_name(args.session, local_port):
            if state.get("session") == args.session:
                raise ForwardError(
                    "Recorded SSH session/port does not match its state directory; inspect before reconnecting"
                )
            continue
        owned = state.get("process")
        if not alive(owned):
            continue
        if state.get("session") != args.session or not isinstance(
            state.get("identity"), str
        ):
            raise ForwardError(
                "Live legacy SSH metadata lacks session/key settings; stop the old forward with its known port, then explicitly restart it"
            )
        try:
            identity = absolute_path(state["identity"])
        except argparse.ArgumentTypeError:
            raise ForwardError(
                "Recorded SSH key path is invalid; inspect before reconnecting"
            ) from None
        remote_port = state.get("remote_port")
        if type(remote_port) is not int or not 1024 <= remote_port <= 65535:
            raise ForwardError(
                "Recorded SSH remote port is invalid; inspect before reconnecting"
            )
        if not owned_listener(owned, local_port):
            raise ForwardError(
                "Recorded live SSH process has no owned listener; inspect or stop it before reconnecting"
            )
        matches.append(
            {
                "ok": True,
                "found": True,
                "session": args.session,
                "local_port": local_port,
                "remote_port": remote_port,
                "identity": str(identity),
                "running": True,
                "listening_owned": True,
                "url": f"http://127.0.0.1:{local_port}",
            }
        )
    if len(matches) > 1:
        raise ForwardError(
            "Multiple owned SSH forwards exist for this session; choose or stop them explicitly before reconnecting"
        )
    if not matches:
        return missing
    result = matches[0]
    result["http_ready"] = healthy(result["local_port"])
    return result


def start(args, directory, state_path):
    if alive(load_json(state_path).get("process")):
        raise ForwardError("This local forward is already running; use status or stop")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.local_port))
        except OSError as error:
            raise ForwardError(
                "Local loopback port is occupied; refusing to adopt another process"
            ) from error
    colab, ssh = executable(args.colab_bin, "colab"), executable(args.ssh_bin, "ssh")
    entry = existing_session(args, colab)
    ensure_key(args.identity, args.create_key)
    endpoint_hash = hashlib.sha256(entry["endpoint"].encode()).hexdigest()[:24]
    hosts_dir = args.state_dir / "known-hosts"
    private_dir(hosts_dir)
    known_hosts = hosts_dir / (endpoint_hash + ".hosts")
    if known_hosts.exists() and (
        known_hosts.is_symlink() or known_hosts.stat().st_uid != os.getuid()
    ):
        raise ForwardError("Unsafe dedicated known_hosts file")
    snapshot = directory / "proxy-session.json"
    private_write(snapshot, {args.session: entry})
    command = build_command(args, colab, ssh, snapshot, known_hosts, endpoint_hash)
    owned = None
    process = None
    try:
        log_fd = os.open(
            directory / "ssh.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(log_fd, "ab") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        info = process_info(process.pid)
        if not info or info["pgid"] != process.pid or info["sid"] != process.pid:
            raise ForwardError("Could not record an owned SSH process identity")
        owned = {**info, "boot_id": boot_id()}
        state = {
            "session": args.session,
            "identity": str(args.identity),
            "process": owned,
            "local_port": args.local_port,
            "remote_port": args.remote_port,
        }
        private_write(state_path, state)
        end = time.monotonic() + args.startup_seconds
        while time.monotonic() < end:
            if process.poll() is not None or not alive(owned):
                raise ForwardError(
                    "SSH exited; check the private local ssh.log and host-key/auth state"
                )
            result = status(args, state)
            if result["http_ready"]:
                return result
            time.sleep(0.2)
        raise ForwardError(
            "SSH/ComfyUI readiness timed out; only this local forward is cleaned up"
        )
    except BaseException:
        if owned:
            kill_owned(owned)
        elif process is not None:
            # An unreaped Popen child cannot have its PID recycled. Never signal
            # a group whose start identity was not recorded.
            process.terminate()
        snapshot.unlink(missing_ok=True)
        raise


def execute(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.session):
        raise ForwardError("Use an explicit simple session name")
    if args.create_key and args.action != "start":
        raise ForwardError("--create-key is only valid with explicit start")
    if not math.isfinite(args.startup_seconds) or not 0 < args.startup_seconds <= 120:
        raise ForwardError("startup-seconds must be positive and no more than 120")
    if args.action == "discover":
        return discover(args)
    private_dir(args.state_dir)
    name = state_name(args.session, args.local_port)
    directory = args.state_dir / name
    private_dir(directory)
    lock_fd = os.open(
        directory / "controller.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(lock_fd, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state_path = directory / "forward.json"
        if args.action == "start":
            return start(args, directory, state_path)
        state = load_json(state_path)
        if args.action in ("stop", "cleanup"):
            signalled = kill_owned(state.get("process"))
            if alive(state.get("process")):
                raise ForwardError("Owned SSH process is still running")
            state_path.unlink(missing_ok=True)
            (directory / "proxy-session.json").unlink(missing_ok=True)
            return {**status(args, {}), "stopped": signalled}
        return status(args, state)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - sanitized CLI boundary.
        result = {
            "ok": False,
            "running": False,
            "local_port": args.local_port,
            "url": f"http://127.0.0.1:{args.local_port}",
            "error": str(error)
            if isinstance(error, ForwardError)
            else "Local SSH helper failed (" + type(error).__name__ + ")",
        }
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
