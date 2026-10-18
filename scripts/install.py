#!/usr/bin/env python3
"""Install AgentFanout into an isolated, atomically managed user runtime."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_MARKER = "# Managed by AgentFanout public installer v1"


class InstallError(RuntimeError):
    pass


def _data_home() -> Path:
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".local" / "share"


def _prefix() -> Path:
    return _data_home() / "agentfanout"


def _bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def _regular_directory(path: Path, label: str) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise InstallError(f"{label} must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise InstallError(f"{label} is not a directory: {path}")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise InstallError(f"{label} must not be writable by group or others: {path}")
    return path.resolve()


def _git_identity(source: Path, *, require_clean: bool) -> tuple[str, bool]:
    git = shutil.which("git")
    if not git:
        raise InstallError("git is required for public installation")
    git = str(Path(git).resolve(strict=True))
    completed = subprocess.run(
        [git, "rev-parse", "HEAD"], cwd=source, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
    )
    head = completed.stdout.strip()
    if completed.returncode or len(head) != 40 or any(ch not in "0123456789abcdef" for ch in head):
        raise InstallError(
            "The public installer currently requires a clean Git checkout; "
            "release-archive installation is not yet supported"
        )
    status = subprocess.run(
        [git, "status", "--porcelain=v2", "--untracked-files=all", "--", "."],
        cwd=source, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30,
    )
    if status.returncode:
        raise InstallError("Git could not verify the source worktree")
    dirty = bool(status.stdout)
    if require_clean and dirty:
        raise InstallError("Refusing to install from a dirty or unreviewed source worktree")
    return head, not dirty


def _run(
    command: list[str], *, cwd: Path | None = None,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> dict:
    completed = subprocess.run(
        command, cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=900,
    )
    if completed.returncode not in allowed_returncodes:
        detail = (completed.stdout + "\n" + completed.stderr)[-12000:]
        raise InstallError(f"Command failed ({' '.join(command)}):\n{detail}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(f"Installer child returned invalid JSON: {completed.stdout[-1000:]}") from exc
    if not isinstance(value, dict):
        raise InstallError("Installer child returned a non-object result")
    return value


def _managed_launcher(path: Path, python: Path, active: Path) -> str:
    runtime_root = active.parent
    prefix = runtime_root.parent
    return (
        "#!/bin/sh\n"
        f"{LAUNCHER_MARKER}\n"
        "case \"${1-}\" in\n"
        "  rollback-runtime)\n"
        "    shift\n"
        f"    exec {shlex.quote(str(python))} "
        f"{shlex.quote(str(active / 'scripts' / 'rollback-runtime.py'))} "
        f"--skills-root {shlex.quote(str(runtime_root))} \"$@\"\n"
        "    ;;\n"
        "  uninstall-runtime)\n"
        "    shift\n"
        f"    exec {shlex.quote(str(python))} "
        f"{shlex.quote(str(active / 'scripts' / 'uninstall.py'))} "
        f"--prefix {shlex.quote(str(prefix))} --launcher {shlex.quote(str(path))} \"$@\"\n"
        "    ;;\n"
        "esac\n"
        f"exec {shlex.quote(str(python))} "
        f"{shlex.quote(str(active / 'scripts' / 'agentfanout.py'))} \"$@\"\n"
    )


def _write_launcher(path: Path, content: str) -> None:
    if path.is_symlink():
        raise InstallError(f"Launcher path must not be a symlink: {path}")
    if path.exists():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise InstallError(f"Existing launcher ownership or mode is unsafe: {path}")
        existing = path.read_text(encoding="utf-8", errors="strict")
        if not existing.startswith("#!/bin/sh\n" + LAUNCHER_MARKER + "\n"):
            raise InstallError(f"Refusing to replace an unmanaged launcher: {path}")
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o755)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o755)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def install(source: Path, prefix: Path, bin_dir: Path, *, dry_run: bool = False) -> dict:
    if os.name != "posix" or not sys.platform.startswith("linux") or sys.version_info < (3, 11):
        raise InstallError("AgentFanout public runtime requires Linux (POSIX) and Python 3.11+")
    prefix = prefix.expanduser()
    bin_dir = bin_dir.expanduser()
    if not prefix.is_absolute() or not bin_dir.is_absolute():
        raise InstallError("Install prefix and launcher directory must be absolute paths")
    source = source.expanduser().resolve(strict=True)
    head, source_clean = _git_identity(source, require_clean=not dry_run)
    version = (source / "VERSION").read_text(encoding="utf-8").strip()
    if not version or len(version) > 100:
        raise InstallError("VERSION metadata is missing or invalid")
    plan = {
        "schema": "AgentFanoutPublicInstallV1", "source": str(source),
        "source_commit": head, "version": version, "prefix": str(prefix),
        "source_clean": source_clean,
        "launcher": str(bin_dir / "agentfanout"),
        "platform_support": "linux-posix-python-3.11+",
    }
    if dry_run:
        return {"status": "dry_run", **plan}

    prefix = _regular_directory(prefix, "Install prefix")
    bin_dir = _regular_directory(bin_dir, "Launcher directory")
    venv_root = prefix / "venv"
    if venv_root.is_symlink():
        raise InstallError(f"Virtual environment must not be a symlink: {venv_root}")
    python = venv_root / "bin" / "python"
    if not python.exists():
        try:
            venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(venv_root)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError(
                "Python venv/ensurepip bootstrap is unavailable; install the host's "
                "Python venv component for this interpreter and retry"
            ) from exc
    requirements = source / "requirements.lock"
    if requirements.is_symlink() or not requirements.is_file():
        raise InstallError("requirements.lock must be a regular source file")
    prefix_probe = subprocess.run(
        [str(python), "-I", "-c", "import sys; print(sys.prefix)"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    if (
        prefix_probe.returncode
        or Path(prefix_probe.stdout.strip()).resolve() != venv_root.resolve()
    ):
        raise InstallError("Existing virtual environment failed its isolated prefix check")
    dependency_install = subprocess.run(
        [
            str(python), "-m", "pip", "install", "--isolated", "--no-input",
            "--disable-pip-version-check", "--require-hashes", "--only-binary=:all:",
            "--index-url", "https://pypi.org/simple", "-r", str(requirements),
        ],
        cwd=source, timeout=900, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if dependency_install.returncode:
        raise InstallError(
            "Dependency installation failed:\n" + dependency_install.stdout[-12000:]
        )
    dependency_check = subprocess.run(
        [str(python), "-I", "-m", "pip", "check"], cwd=source, timeout=120,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if dependency_check.returncode:
        raise InstallError("Dependency verification failed:\n" + dependency_check.stdout[-12000:])
    runtime_root = prefix / "runtime"
    result = _run([
        str(python), str(source / "scripts" / "install-runtime.py"),
        "--source", str(source), "--skills-root", str(runtime_root),
        "--review-ref", f"public-source-commit:{head}",
    ], cwd=source)
    active = runtime_root / "AgentFanout"
    launcher = bin_dir / "agentfanout"
    # Verify the activated runtime before exposing a user command.
    verification = _run([
        str(python), str(active / "scripts" / "agentfanout.py"),
        "doctor", "--discover-current-worktree", "--provider", "Codex", "--binary-only",
    ], cwd=source, allowed_returncodes=frozenset({0, 2}))
    # Codex binary-only is intentionally not ready, but this verifies that the
    # managed launcher/runtime emits its stable doctor schema.
    if verification.get("schema") != "AgentFanoutDoctorV1":
        raise InstallError("Installed runtime doctor schema verification failed")
    _write_launcher(launcher, _managed_launcher(launcher, python, active))
    written_launcher_identity = launcher.stat(follow_symlinks=False)
    try:
        launcher_verification = _run([
            str(launcher), "doctor", "--discover-current-worktree",
            "--provider", "Codex", "--binary-only",
        ], cwd=source, allowed_returncodes=frozenset({0, 2}))
        if launcher_verification.get("schema") != "AgentFanoutDoctorV1":
            raise InstallError("Managed launcher doctor schema verification failed")
    except Exception:
        try:
            current = launcher.stat(follow_symlinks=False)
            if (
                stat.S_ISREG(current.st_mode)
                and (current.st_dev, current.st_ino)
                == (written_launcher_identity.st_dev, written_launcher_identity.st_ino)
            ):
                launcher.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "status": "installed", **plan, "runtime": result,
        "doctor_schema_verified": True,
        "next_command": f"{bin_dir / 'agentfanout'} doctor --discover-current-worktree --provider Grok --grok-home /secure/dedicated/grok-home",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--prefix", type=Path, default=_prefix())
    parser.add_argument("--bin-dir", type=Path, default=_bin_dir())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install(args.source, args.prefix, args.bin_dir, dry_run=args.dry_run)
        code = 0
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        result = {"schema": "AgentFanoutPublicInstallV1", "status": "error", "error": str(exc)}
        code = 2
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
