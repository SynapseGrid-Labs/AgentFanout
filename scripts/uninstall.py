#!/usr/bin/env python3
"""Safely deactivate a managed AgentFanout public installation."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
from pathlib import Path


LAUNCHER_MARKER = "# Managed by AgentFanout public installer v1"


class UninstallError(RuntimeError):
    pass


def _data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(value).expanduser() if value else Path.home() / ".local" / "share"


def deactivate(prefix: Path, launcher: Path, *, dry_run: bool = False) -> dict:
    prefix = prefix.expanduser()
    launcher = launcher.expanduser()
    if not prefix.is_absolute() or not launcher.is_absolute():
        raise UninstallError("Install prefix and launcher must be absolute paths")
    active = prefix / "runtime" / "AgentFanout"
    if prefix.is_symlink() or (prefix.exists() and not prefix.is_dir()):
        raise UninstallError(f"Install prefix is unsafe: {prefix}")
    if launcher.is_symlink():
        raise UninstallError(f"Managed launcher must not be a symlink: {launcher}")
    remove_launcher = False
    if launcher.exists():
        if not stat.S_ISREG(launcher.lstat().st_mode):
            raise UninstallError(f"Managed launcher is not a regular file: {launcher}")
        info = launcher.lstat()
        if (
            info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise UninstallError(f"Managed launcher ownership or mode is unsafe: {launcher}")
        content = launcher.read_text(encoding="utf-8", errors="strict")
        expected_fragments = (
            LAUNCHER_MARKER,
            shlex.quote(str(prefix / "venv" / "bin" / "python")),
            shlex.quote(str(active / "scripts" / "agentfanout.py")),
            "--prefix " + shlex.quote(str(prefix)),
            "--launcher " + shlex.quote(str(launcher)),
        )
        if any(fragment not in content for fragment in expected_fragments):
            raise UninstallError(f"Refusing to remove an unmanaged launcher: {launcher}")
        remove_launcher = True
    if active.exists() and not active.is_symlink():
        raise UninstallError(f"Managed active runtime is not a symlink: {active}")
    result = {
        "schema": "AgentFanoutPublicUninstallV1", "status": "dry_run" if dry_run else "deactivated",
        "launcher_removed": remove_launcher, "active_runtime_removed": active.is_symlink(),
        "preserved_release_root": str(prefix / "runtime"),
        "preserved_virtual_environment": str(prefix / "venv"),
        "note": "Verified immutable releases and the isolated environment are preserved for rollback or manual archival.",
    }
    if dry_run:
        return result
    if remove_launcher:
        launcher.unlink()
    if active.is_symlink():
        active.unlink()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--prefix", type=Path, default=_data_home() / "agentfanout")
    parser.add_argument("--launcher", type=Path, default=Path.home() / ".local" / "bin" / "agentfanout")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = deactivate(args.prefix, args.launcher, dry_run=args.dry_run)
        code = 0
    except (UninstallError, OSError) as exc:
        result = {"schema": "AgentFanoutPublicUninstallV1", "status": "error", "error": str(exc)}
        code = 2
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
