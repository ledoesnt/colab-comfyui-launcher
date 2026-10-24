"""Run pinned official Colab CLI with only its dedicated cache redirected.

This adapter runs in the CLI's interpreter. HOME, SSH paths and proxy variables
are preserved. No credential file is read, copied, swapped or parsed here.
"""

from __future__ import annotations

import importlib.metadata
import os
import runpy
import sys
from pathlib import Path

from accounts import AccountError, check_cli_directory


def run(directory: Path, arguments: list[str]) -> None:
    if importlib.metadata.version("google-colab-cli") != "0.7.4":
        raise AccountError("Named accounts require verified google-colab-cli==0.7.4.")
    check_cli_directory(directory)
    os.umask(0o077)
    expanduser = os.path.expanduser

    def profile_path(path):
        # Exact CLI-owned namespace only; client OAuth config and SSH retain
        # their normal paths. All CLI token/session/settings/history/log paths
        # in 0.7.4 use this function before accessing their caches.
        prefix = "~/.config/colab-cli"
        if isinstance(path, str) and (path == prefix or path.startswith(prefix + "/")):
            return str(directory) + path[len(prefix) :]
        return expanduser(path)

    os.path.expanduser = profile_path
    sys.argv = ["colab", *arguments]
    try:
        runpy.run_module("colab_cli.cli", run_name="__main__")
    finally:
        os.path.expanduser = expanduser


if __name__ == "__main__":
    try:
        if len(sys.argv) < 3:
            raise AccountError("Missing account context or command.")
        run(Path(sys.argv[1]), sys.argv[2:])
    except (AccountError, importlib.metadata.PackageNotFoundError):
        print(
            "Colab account context could not start; check private cache permissions and google-colab-cli==0.7.4.",
            file=sys.stderr,
        )
        raise SystemExit(2)
