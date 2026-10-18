#!/usr/bin/env python3
"""Exercise public install, launcher, root handoff, and uninstall in a clean tree."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CleanroomError(RuntimeError):
    pass


def _run(
    command: list[str], *, cwd: Path, timeout: float = 1200,
    allowed: frozenset[int] = frozenset({0}),
) -> tuple[int, str, str]:
    process = subprocess.run(
        command, cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, umask=0o022,
    )
    if process.returncode not in allowed:
        raise CleanroomError(
            f"Command failed ({' '.join(command)}):\n" +
            (process.stdout + "\n" + process.stderr)[-16000:]
        )
    return process.returncode, process.stdout, process.stderr


def _json(stdout: str, label: str) -> dict:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CleanroomError(f"{label} did not return JSON") from exc
    if not isinstance(value, dict):
        raise CleanroomError(f"{label} did not return an object")
    return value


def run(source: Path) -> dict:
    if os.name != "posix" or not sys.platform.startswith("linux") or sys.version_info < (3, 11):
        raise CleanroomError("clean-room test requires Linux (POSIX) and Python 3.11+")
    source = source.resolve(strict=True)
    git = shutil.which("git")
    if not git:
        raise CleanroomError("git is required for the clean-room test")
    git = str(Path(git).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="agentfanout-cleanroom-") as temp:
        root = Path(temp)
        checkout = root / "checkout"
        _run([git, "clone", "--local", "--no-hardlinks", str(source), str(checkout)], cwd=root)
        prefix = root / "install"
        bin_dir = root / "bin"
        _, install_stdout, _ = _run([
            sys.executable, str(checkout / "scripts/install.py"),
            "--source", str(checkout), "--prefix", str(prefix),
            "--bin-dir", str(bin_dir),
        ], cwd=checkout)
        install = _json(install_stdout, "installer")
        if install.get("status") != "installed" or not install.get("doctor_schema_verified"):
            raise CleanroomError("installer did not verify its activated runtime")
        _, reinstall_stdout, _ = _run([
            sys.executable, str(checkout / "scripts/install.py"),
            "--source", str(checkout), "--prefix", str(prefix),
            "--bin-dir", str(bin_dir),
        ], cwd=checkout)
        reinstall = _json(reinstall_stdout, "idempotent installer")
        if (
            reinstall.get("status") != "installed"
            or reinstall.get("runtime", {}).get("status") != "already_active"
        ):
            raise CleanroomError("reinstall was not an idempotent verified activation")

        fixture = root / "fixture-project"
        fixture.mkdir()
        (fixture / "README.md").write_text("AgentFanout clean-room fixture\n", encoding="utf-8")
        _run([git, "init", "-q", "--initial-branch=main"], cwd=fixture)
        launcher = bin_dir / "agentfanout"
        code, doctor_stdout, _ = _run([
            str(launcher), "doctor", "--discover-current-worktree",
            "--provider", "Codex", "--binary-only",
        ], cwd=fixture, allowed=frozenset({0, 2}))
        doctor = _json(doctor_stdout, "installed doctor")
        if doctor.get("schema") != "AgentFanoutDoctorV1":
            raise CleanroomError("installed launcher returned the wrong doctor schema")
        if doctor.get("checks", {}).get("project_binding", {}).get("resolved_root") != str(fixture.resolve()):
            raise CleanroomError("installed launcher did not bind the exact fixture worktree")

        _, uninstall_stdout, _ = _run([
            str(launcher), "uninstall-runtime",
        ], cwd=checkout)
        uninstall = _json(uninstall_stdout, "uninstaller")
        if uninstall.get("status") != "deactivated" or launcher.exists():
            raise CleanroomError("uninstall did not safely deactivate the launcher")
        return {
            "schema": "AgentFanoutCleanroomV1", "status": "passed",
            "source_commit": install["source_commit"],
            "install_status": install["status"], "doctor_returncode": code,
            "reinstall_runtime_status": reinstall["runtime"]["status"],
            "doctor_schema": doctor["schema"], "exact_root_bound": True,
            "uninstall_status": uninstall["status"],
            "releases_preserved": (prefix / "runtime" / ".agentfanout-releases").is_dir(),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        value, code = run(args.source), 0
    except (CleanroomError, OSError, subprocess.SubprocessError) as exc:
        value, code = {"schema": "AgentFanoutCleanroomV1", "status": "failed", "error": str(exc)}, 2
    print(json.dumps(value, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
