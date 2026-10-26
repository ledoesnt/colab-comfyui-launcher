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

    def first_name_required(self) -> bool:
        """A fresh installation names its first login before authorization."""
        return (
            not (self.root / "accounts.json").exists()
            and not (Path.home() / ".config/colab-cli/token.json").exists()
            and not any(
                (launcher_root() / name).exists()
                for name in ("budgets", "requests", "ssh")
            )
        )

    def _save(self, value: dict) -> None:
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

    def legacy_name_required(self) -> bool:
        """Offer the upgrade once, when an actual legacy login/state exists."""
        return "default" in self.read()["profiles"] and not self.first_name_required()

    def update(
        self, *, add=None, active=None, toggle=None, enabled=None, first=False
    ) -> dict:
        private_directory(self.root)
        fd = os.open(
            self.root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self.read()
            if first:
                if not self.first_name_required() or add == "default":
                    raise AccountError("The first login needs a unique local name.")
                if add is None or active != add:
                    raise AccountError("Name and select the first account together.")
                value["profiles"] = []
            if add is not None:
                account_id(add)
                if add == "default":
                    raise AccountError(
                        "default is reserved for legacy login compatibility."
                    )
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
            self._save(value)
            return value

    def directory(self, account: str) -> Path:
        account_id(account)
        if account == "default" or account not in self.read()["profiles"]:
            raise AccountError("Choose an existing named account.")
        path = self.root / account
        private_directory(path)
        return path

    def migrate_default(self, name: str) -> dict:
        """Move legacy caches and state; credential files are never opened.

        All launcher/CLI processes must be closed first. A compatibility link
        keeps standalone `colab` using the same physical credential directory.
        Budget identity and deadlines survive; only the local account label is
        changed. Directory renames require the same filesystem.
        """
        account_id(name)
        if name == "default":
            raise AccountError("Choose a new name for the legacy account.")
        private_directory(self.root)
        fd = os.open(
            self.root / ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self.read()
            legacy = Path.home() / ".config/colab-cli"
            destination = self.root / name
            sources = [(legacy, destination / "cli")]
            sources.extend(
                (launcher_root() / part, destination / "state" / part)
                for part in ("budgets", "requests", "ssh")
            )
            if "default" not in value["profiles"]:
                if (
                    value["profiles"][0] == name
                    and legacy.is_symlink()
                    and legacy.readlink() == destination / "cli"
                    and not any(source.exists() for source, _ in sources[1:])
                ):
                    return value
                raise AccountError("No legacy default account is available to migrate.")
            if (
                name in value["profiles"]
                or destination.exists()
                or destination.is_symlink()
            ):
                raise AccountError(
                    "Migration destination already exists; nothing was replaced."
                )
            require_idle_accounts()
            # Validate every entry before moving anything or tightening modes.
            trees = []
            for source, target in sources:
                if source.is_symlink():
                    raise AccountError(
                        "Migration refuses symbolic links in legacy storage."
                    )
                for parent in source.parents:
                    if parent.is_symlink():
                        raise AccountError(
                            "Migration refuses redirected legacy storage."
                        )
                if not source.exists():
                    continue
                if not source.is_dir():
                    raise AccountError("Migration source must be a directory.")
                if source.stat().st_dev != self.root.stat().st_dev:
                    raise AccountError(
                        "Migration requires storage on the same filesystem."
                    )
                entries = [source, *source.rglob("*")]
                for entry in entries:
                    info = entry.lstat()
                    if (
                        info.st_uid != os.getuid()
                        or not (
                            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                        )
                        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
                    ):
                        raise AccountError(
                            "Migration refuses unsafe legacy storage metadata."
                        )
                trees.append((source, target, entries))
            originals = []
            budgets = launcher_root() / "budgets"
            if budgets.exists():
                for path in budgets.rglob("*.json"):
                    with path.open() as stream:
                        record = json.load(stream)
                    if (
                        not isinstance(record, dict)
                        or record.get("schema") != 1
                        or record.get("account", "default") != "default"
                    ):
                        raise AccountError(
                            "Legacy budget account could not be verified."
                        )
                    originals.append((path.relative_to(budgets), record))
            moved = []
            linked = False
            private_directory(destination)
            private_directory(destination / "state")
            try:
                for source, target, entries in trees:
                    source.rename(target)
                    moved.append((source, target))
                    for entry in entries:
                        relocated = target / entry.relative_to(source)
                        relocated.chmod(0o700 if relocated.is_dir() else 0o600)
                private_directory(destination / "cli")
                for relative, record in originals:
                    atomic_state_json(
                        destination / "state/budgets" / relative,
                        {**record, "account": name},
                    )
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.symlink_to(destination / "cli", target_is_directory=True)
                linked = True
                value["profiles"] = [
                    name if item == "default" else item for item in value["profiles"]
                ]
                value["active"] = (
                    name if value["active"] == "default" else value["active"]
                )
                value["fallbacks"] = [
                    name if item == "default" else item for item in value["fallbacks"]
                ]
                self._save(value)
            except BaseException:
                if linked:
                    legacy.unlink()
                if any(source == budgets for source, _ in moved):
                    for relative, record in originals:
                        atomic_state_json(
                            destination / "state/budgets" / relative, record
                        )
                for source, target in reversed(moved):
                    if source.exists() or source.is_symlink():
                        raise AccountError(
                            "Legacy storage was recreated during migration; both directories were retained for inspection."
                        ) from None
                    target.rename(source)
                for path in (destination / "cli", destination / "state", destination):
                    if path.is_dir() and not any(path.iterdir()):
                        path.rmdir()
                raise
            return value


def atomic_state_json(path: Path, value: dict) -> None:
    """Write noncredential migration metadata atomically."""
    fd, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_idle_accounts() -> None:
    """Read process metadata only; migration must not strand a live controller."""
    programs = {
        "dashboard.py",
        "colabctl.py",
        "budget_guard.py",
        "ssh_forward.py",
        "account_cli.py",
        "colab",
    }
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if any(
                Path(os.fsdecode(argument)).name in programs
                for argument in arguments
                if argument
            ):
                raise AccountError(
                    "Close dashboard, CLI, budget guards and SSH forwards before migration; retain remote resources."
                )
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue


def resolve_account(name: str) -> str:
    """CLI compatibility: omitted/default refers to the original first account.

    This is independent of the UI's active account, including after migration.
    Library contexts remain explicit and do not consult UI preferences.
    """
    account_id(name)
    if name == "default":
        profiles = AccountStore().read()["profiles"]
        return "default" if "default" in profiles else profiles[0]
    return name


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
    parser.add_argument(
        "action",
        choices=("list", "add", "select", "fallback", "auto", "migrate-default"),
    )
    parser.add_argument("name", nargs="?")
    args = parser.parse_args(argv)
    try:
        store = AccountStore()
        if args.action == "list":
            value = store.read()
        elif args.action == "migrate-default":
            value = store.migrate_default(args.name)
        elif args.action == "auto":
            if args.name not in ("on", "off"):
                raise AccountError("Use auto on or auto off.")
            value = store.update(enabled=args.name == "on")
        else:
            account_id(args.name)
            first = args.action == "add" and store.first_name_required()
            changes = {
                {"add": "add", "select": "active", "fallback": "toggle"}[
                    args.action
                ]: args.name
            }
            if first:
                changes["active"] = args.name
            value = store.update(
                first=first,
                **changes,
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
