#!/usr/bin/env python3
"""Atomically roll AgentFanout back to a previously verified immutable release."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from release_lib import (
    ReleaseError, atomic_switch, read_state, runtime_transaction_lock,
    validate_frozen_release, validate_regular_directory, write_state,
)


def default_skills_root() -> Path:
    pai_dir = Path(os.environ.get("PAI_DIR") or os.environ.get("PAI_HOME") or Path.home() / ".pai")
    return pai_dir / "skills"


def _rollback_locked(
    skills_root: Path,
    release: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, str | bool | None]:
    root = validate_regular_directory(
        skills_root.expanduser().absolute(), "Skills root",
    ).resolve()
    releases = validate_regular_directory(
        root / ".agentfanout-releases", "Release root",
    ).resolve()
    active = root / "AgentFanout"
    state = read_state(releases)
    if release is None:
        if not state or not state.get("previous_target"):
            raise ReleaseError("No previous release target is recorded")
        target = Path(str(state["previous_target"]))
    else:
        target = release if release.is_absolute() else releases / release
    target = Path(os.path.abspath(target))
    try:
        relative_target = target.relative_to(releases)
    except ValueError as error:
        raise ReleaseError("Rollback target must be inside the configured release root") from error
    if len(relative_target.parts) != 1:
        raise ReleaseError("Rollback target must be one direct release directory")

    _, provenance = validate_frozen_release(target)
    if provenance["review_ref"] == "diagnostic-stage-only":
        raise ReleaseError("Diagnostic-only stages cannot be activated by rollback")
    if provenance["source_dirty"] or provenance["source_dirty_paths"]:
        raise ReleaseError("Dirty-source stages cannot be activated by rollback")

    current: Path | None = None
    if active.is_symlink():
        try:
            current = active.resolve(strict=True)
        except OSError as error:
            raise ReleaseError("Active runtime link is unreadable") from error
    elif active.exists():
        raise ReleaseError("Active runtime must be a managed symlink before rollback")

    changed = current != target
    result = {
        "active": str(active), "from": str(current) if current else None,
        "to": str(target), "changed": changed,
    }
    if dry_run:
        return result
    if not changed:
        return result
    try:
        atomic_switch(active, target)
        validate_frozen_release(target)
        write_state(releases, target, current)
    except Exception as error:
        if current is not None:
            atomic_switch(active, current)
        elif active.is_symlink():
            active.unlink()
        raise ReleaseError(f"Rollback activation failed; prior runtime restored: {error}") from error
    return result


def rollback(
    skills_root: Path,
    release: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, str | bool | None]:
    if dry_run:
        return _rollback_locked(skills_root, release, dry_run=True)
    with runtime_transaction_lock(skills_root) as locked_root:
        return _rollback_locked(locked_root, release, dry_run=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", type=Path, default=default_skills_root())
    parser.add_argument("--release", type=Path, help="Verified immutable release to activate.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = rollback(args.skills_root, args.release, dry_run=args.dry_run)
    except ReleaseError as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2 if args.pretty else None, sort_keys=True))
        return 2
    print(json.dumps({"status": "rolled_back" if not args.dry_run else "dry_run", **result}, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
