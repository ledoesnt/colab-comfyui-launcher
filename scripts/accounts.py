"""Named Colab login contexts. Only the official CLI reads credential bytes.

The legacy login is 'default'. Named contexts have separate CLI caches and
launcher state. Selection is a UI preference, never an instruction to a guard.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import stat
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_current = ContextVar("colab_launcher_account", default="default")


class AccountError(ValueError):
    """A nonsecret account configuration error."""


def account_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value
    ):
        raise AccountError("Account name must use 1–32 letters, digits, _ or -.")
    return value


def current_account() -> str:
    return _current.get()


@contextmanager
def account_context(account: str):
    token = _current.set(account_id(account))
    try:
        yield
    finally:
        _current.reset(token)


def launcher_root() -> Path:
    return (
        Path(
            os.environ.get("COLAB_LAUNCHER_STATE_DIR")
            or (
                Path(
                    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
                )
                / "colab-comfyui-launcher"
            )
        )
        .expanduser()
        .absolute()
    )


def private_directory(path: Path) -> None:
    """Reject redirected paths; never change permissions on an unrelated parent."""
    path = path.absolute()
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise AccountError("Account directories must not contain symbolic links.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AccountError("Account directory must be owned by this user.")
    if info.st_mode & 0o077:
        raise AccountError("Account directory must have private permissions (700).")


def check_cli_directory(path: Path) -> None:
    private_directory(path)
    for name in (
        "token.json",
        "sessions.json",
        "settings.json",
        "colab.log",
        "history",
    ):
        target = path / name
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        expected = stat.S_ISDIR if name == "history" else stat.S_ISREG
        if (
            not expected(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise AccountError(
                "Account CLI cache has unsafe type, ownership or permissions."
            )


class AccountStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(
            root
            or os.environ.get("COLAB_LAUNCHER_ACCOUNTS_DIR")
            or launcher_root() / "accounts"
        ).absolute()

    def read(self) -> dict:
        path = self.root / "accounts.json"
        if self.root.exists() or self.root.is_symlink():
            private_directory(self.root)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {
                "schema": 1,
                "profiles": ["default"],
                "active": "default",
                "fallbacks": [],
                "auto_fallback": False,
            }
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 65536
            ):
                raise AccountError("Account preferences have unsafe metadata.")
            try:
                value = json.load(stream)
            except (ValueError, UnicodeError):
                raise AccountError("Account preferences are unreadable.") from None
        if (
            not isinstance(value, dict)
            or value.get("schema") != 1
            or set(value)
            != {"schema", "profiles", "active", "fallbacks", "auto_fallback"}
        ):
            raise AccountError("Unsupported account preferences.")
        profiles, fallbacks = value.get("profiles"), value.get("fallbacks")
        if (
            not isinstance(profiles, list)
            or not 1 <= len(profiles) <= 32
            or any(not isinstance(item, str) for item in profiles)
            or profiles[0] != "default"
            or len(set(profiles)) != len(profiles)
        ):
            raise AccountError("Invalid account list.")
        for item in profiles:
            account_id(item)
        if (
            value.get("active") not in profiles
            or not isinstance(fallbacks, list)
            or any(
                not isinstance(item, str) or item not in profiles for item in fallbacks
            )
            or len(set(fallbacks)) != len(fallbacks)
            or type(value.get("auto_fallback")) is not bool
        ):
            raise AccountError("Invalid account selection or fallback order.")
        return value

    def update(self, *, add=None, active=None, toggle=None, enabled=None) -> dict:
        private_directory(self.root)
        fd = os.open(
            self.root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self.read()
            if add is not None:
                account_id(add)
                if add in value["profiles"] or len(value["profiles"]) >= 32:
                    raise AccountError(
                        "Account already exists or the 32-account limit was reached."
                    )
                private_directory(self.root / add)
                private_directory(self.root / add / "cli")
                value["profiles"].append(add)
            if active is not None:
                if active not in value["profiles"]:
                    raise AccountError("Unknown saved account.")
                value["active"] = active
            if toggle is not None:
                if toggle not in value["profiles"]:
                    raise AccountError("Unknown fallback account.")
                if toggle in value["fallbacks"]:
                    value["fallbacks"].remove(toggle)
                else:
                    value["fallbacks"].append(toggle)
            if enabled is not None:
                value["auto_fallback"] = bool(enabled)
            fd, name = tempfile.mkstemp(prefix=".accounts-", dir=self.root)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(value, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.root / "accounts.json")
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return value

    def directory(self, account: str) -> Path:
        account_id(account)
        if account == "default" or account not in self.read()["profiles"]:
            raise AccountError("Choose an existing named account.")
        path = self.root / account
        private_directory(path)
        return path


def cli_directory(account: str | None = None) -> Path:
    account = account_id(account or current_account())
    if account == "default":
        return Path.home() / ".config/colab-cli"
    return AccountStore().directory(account) / "cli"


def state_root(account: str | None = None) -> Path:
    account = account_id(account or current_account())
    if account == "default":
        return launcher_root()
    path = AccountStore().directory(account) / "state"
    private_directory(path)
    return path


def launcher_arguments(account: str | None = None) -> list[str]:
    account = account or current_account()
    return [] if account == "default" else ["--account", account_id(account)]


def colab_command(
    arguments: list[str],
    *,
    account: str | None = None,
    executable: str = "colab",
    auth: bool = True,
) -> list[str]:
    account = account_id(account or current_account())
    arguments = (["--auth=oauth2"] if auth else []) + arguments
    if account == "default":
        return [str(executable), *arguments]
    directory = cli_directory(account)
    check_cli_directory(directory)
    binary = shutil.which(str(executable))
    if not binary:
        raise AccountError("Official Colab CLI is not installed.")
    # Use the CLI's own virtualenv interpreter, preserving its dependency set.
    # Do not resolve a virtualenv python symlink or alter HOME/PYTHONPATH.
    with open(binary, "rb") as stream:
        header = stream.readline(4096).decode("utf-8", errors="replace").strip()
    parts = shlex.split(header[2:]) if header.startswith("#!") else []
    if len(parts) == 2 and parts[0] == "/usr/bin/env":
        interpreter = shutil.which(parts[1])
    elif len(parts) == 1:
        interpreter = parts[0]
    else:
        interpreter = None
    if (
        not interpreter
        or not Path(interpreter).is_absolute()
        or not os.access(interpreter, os.X_OK)
    ):
        raise AccountError(
            "Named accounts require the official Python Colab CLI entrypoint."
        )
    return [
        str(interpreter),
        str(Path(__file__).with_name("account_cli.py")),
        str(directory),
        *arguments,
    ]


def funded_account(accounts: list[str], usage, check_cancelled=lambda: None) -> str:
    """Choose before allocation. Unknown/auth/network failures never mean zero CU."""
    for account in dict.fromkeys(accounts):
        account_id(account)
        check_cancelled()
        with account_context(account):
            result = usage()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise AccountError(
                "Account balance is unverified; no runtime was created. Check login and retry."
            )
        balance = result.get("balance")
        if (
            type(balance) not in (int, float)
            or not math.isfinite(balance)
            or balance < 0
        ):
            raise AccountError("Account balance is unverified; no runtime was created.")
        if balance > 0:
            return account
    raise AccountError("All selected accounts have zero CU; no runtime was created.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "add", "select", "fallback", "auto"))
    parser.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    try:
        store = AccountStore()
        if args.action == "list":
            value = store.read()
        elif args.action == "auto":
            if args.name not in ("on", "off"):
                raise AccountError("Use auto on or auto off.")
            value = store.update(enabled=args.name == "on")
        else:
            account_id(args.name)
            value = store.update(
                **{
                    {"add": "add", "select": "active", "fallback": "toggle"}[
                        args.action
                    ]: args.name
                }
            )
        print(json.dumps(dict(ok=True, **value)))
        return 0
    except (AccountError, OSError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Account settings could not be updated. Use a unique account name and private directories.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
