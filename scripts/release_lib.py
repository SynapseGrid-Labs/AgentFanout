#!/usr/bin/env python3
"""Shared, fail-closed helpers for AgentFanout immutable runtime releases."""

from __future__ import annotations

from contextlib import contextmanager
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - runtime installation is POSIX-only
    fcntl = None


MANIFEST_NAME = ".agentfanout-manifest.json"
PROVENANCE_NAME = ".agentfanout-provenance.json"
ARCHIVE_MANIFEST_NAME = ".agentfanout-archive-manifest.json"
STATE_NAME = ".agentfanout-release-state.json"
INSTALLER_VERSION = "agentfanout-atomic-v1"
TOOL_VERSION = "1"


class ReleaseError(RuntimeError):
    """A release invariant could not be satisfied."""


def validate_regular_directory(path: Path, label: str) -> Path:
    """Verify one existing directory without following a leaf symlink."""

    if path.is_symlink():
        raise ReleaseError(f"{label} must not be a symlink: {path}")
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseError(f"{label} is unavailable: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseError(f"{label} is not a regular directory: {path}")
    return path


def ensure_regular_directory(path: Path, label: str) -> Path:
    """Create or verify one directory without following a leaf symlink."""

    if path.is_symlink():
        raise ReleaseError(f"{label} must not be a symlink: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReleaseError(f"{label} is unavailable: {path}") from error
    return validate_regular_directory(path, label)


@contextmanager
def runtime_transaction_lock(skills_root: Path):
    """Serialize runtime activation and rollback for one skills root."""

    if fcntl is None:
        raise ReleaseError("Runtime installation requires POSIX advisory file locking")
    root = ensure_regular_directory(skills_root.expanduser().absolute(), "Skills root").resolve()
    lock_path = root / ".agentfanout-install.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ReleaseError(f"Runtime transaction lock is unavailable or unsafe: {lock_path}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseError(f"Runtime transaction lock is not a private regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseError(f"Another AgentFanout runtime transaction is active: {root}") from error
        yield root
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.write_text(canonical_json(value), encoding="utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    if path.is_symlink():
        raise ReleaseError(f"Symlinks are not release files: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ReleaseError(f"Unsafe release path: {value!r}")
    return path.as_posix()


def matches_any(relative: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def default_allowlist(root: Path) -> Path:
    return root / "release" / "public-safe-allowlist.json"


def load_allowlist(root: Path, path: Path | None = None) -> dict[str, Any]:
    allowlist_path = path or default_allowlist(root)
    try:
        raw = json.loads(allowlist_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReleaseError(f"Public-safe allowlist is missing: {allowlist_path}") from error
    except json.JSONDecodeError as error:
        raise ReleaseError(f"Public-safe allowlist is invalid JSON: {allowlist_path}") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ReleaseError("Public-safe allowlist has an unsupported schema")
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        raise ReleaseError("Public-safe allowlist has no profiles")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not isinstance(profile, dict):
            raise ReleaseError("Public-safe allowlist profile is invalid")
        for key in ("files", "directories"):
            values = profile.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ReleaseError(f"Allowlist profile {name!r} has invalid {key}")
            for value in values:
                safe_relative(value)
    excludes = raw.get("exclude", [])
    if not isinstance(excludes, list) or not all(isinstance(value, str) for value in excludes):
        raise ReleaseError("Public-safe allowlist has invalid exclusions")
    return raw


def collect_allowed_paths(root: Path, profile: str, allowlist_path: Path | None = None) -> list[str]:
    root = root.resolve()
    allowlist = load_allowlist(root, allowlist_path)
    profiles = allowlist["profiles"]
    if profile not in profiles:
        raise ReleaseError(f"Unknown release profile: {profile}")
    selected = profiles[profile]
    exclusions = tuple(allowlist.get("exclude", []))
    files: set[str] = set()

    def verified_stat(path: Path) -> os.stat_result:
        """Reject symlinks in the selected path and every ancestor below root."""

        try:
            relative_path = path.relative_to(root)
        except ValueError as exc:
            raise ReleaseError(f"Allowlisted path escapes release root: {path}") from exc
        cursor = root
        info = root.lstat()
        try:
            for component in relative_path.parts:
                cursor = cursor / component
                info = cursor.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ReleaseError(f"Symlinked release paths are forbidden: {relative_path.as_posix()}")
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ReleaseError(f"Allowlisted path is missing: {relative_path.as_posix()}") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ReleaseError(f"Allowlisted path resolves outside release root: {relative_path.as_posix()}") from exc
        return info

    def add(path: Path) -> None:
        info = verified_stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseError(f"Allowlisted release file is not regular: {path.relative_to(root)}")
        relative = safe_relative(path.relative_to(root).as_posix())
        if not matches_any(relative, exclusions):
            files.add(relative)

    def add_directory(directory: Path) -> None:
        info = verified_stat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise ReleaseError(f"Allowlisted directory is missing or unsafe: {directory.relative_to(root)}")
        for path in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
            child_info = verified_stat(path)
            if stat.S_ISDIR(child_info.st_mode):
                add_directory(path)
            elif stat.S_ISREG(child_info.st_mode):
                add(path)
            else:
                raise ReleaseError(f"Special release paths are forbidden: {path.relative_to(root)}")

    for relative in selected.get("files", []):
        add(root / safe_relative(relative))
    for relative in selected.get("directories", []):
        directory = root / safe_relative(relative)
        add_directory(directory)
    return sorted(files)


def records_for_paths(root: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in sorted(paths):
        path = root / safe_relative(relative)
        if not path.is_file() or path.is_symlink():
            raise ReleaseError(f"Manifest file is missing or unsafe: {relative}")
        records.append({"path": relative, "sha256": sha256_file(path), "size": path.stat().st_size})
    return records


def tree_digest(records: Iterable[dict[str, Any]]) -> str:
    payload = "".join(
        f"{record['path']}\0{record['sha256']}\0{record['size']}\n"
        for record in sorted(records, key=lambda item: item["path"])
    ).encode("utf-8")
    return sha256_bytes(payload)


def manifest_for(root: Path, profile: str = "runtime", allowlist_path: Path | None = None) -> dict[str, Any]:
    records = records_for_paths(root, collect_allowed_paths(root, profile, allowlist_path))
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "profile": profile,
        "files": records,
        "tree_sha256": tree_digest(records),
    }


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReleaseError(f"{label} is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise ReleaseError(f"{label} is invalid JSON: {path}") from error
    if not isinstance(raw, dict):
        raise ReleaseError(f"{label} must be a JSON object: {path}")
    return raw


def validate_manifest(root: Path, allowlist_path: Path | None = None) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    manifest = read_json_object(manifest_path, "Release manifest")
    if set(manifest) != {"schema_version", "algorithm", "profile", "files", "tree_sha256"}:
        raise ReleaseError("Release manifest fields are invalid")
    if manifest["schema_version"] != 1 or manifest["algorithm"] != "sha256" or manifest["profile"] != "runtime":
        raise ReleaseError("Release manifest schema is invalid")
    files = manifest["files"]
    if not isinstance(files, list):
        raise ReleaseError("Release manifest files is invalid")
    expected_records = records_for_paths(root, collect_allowed_paths(root, "runtime", allowlist_path))
    if files != expected_records:
        raise ReleaseError("Release manifest does not match the runtime allowlist and file hashes")
    if tree_digest(files) != manifest.get("tree_sha256"):
        raise ReleaseError("Release manifest tree digest is invalid")
    return manifest


def validate_provenance(root: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    provenance_path = root / PROVENANCE_NAME
    provenance = read_json_object(provenance_path, "Release provenance")
    required = {
        "schema_version", "installer_version", "tool_version", "release_id", "review_ref",
        "source_commit", "source_git_tree", "source_dirty", "source_dirty_paths",
        "source_tree_sha256", "release_tree_sha256", "release_manifest_sha256", "created_at",
    }
    if set(provenance) != required:
        raise ReleaseError("Release provenance fields are invalid")
    if provenance["schema_version"] != 1 or provenance["installer_version"] != INSTALLER_VERSION:
        raise ReleaseError("Release provenance schema is invalid")
    if not isinstance(provenance["source_dirty"], bool) or not isinstance(provenance["source_dirty_paths"], list):
        raise ReleaseError("Release provenance dirty state is invalid")
    for key in ("tool_version", "release_id", "review_ref", "source_commit", "source_git_tree", "created_at"):
        if not isinstance(provenance[key], str) or not provenance[key]:
            raise ReleaseError(f"Release provenance {key} is invalid")
    for key in ("source_tree_sha256", "release_tree_sha256", "release_manifest_sha256"):
        value = provenance[key]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ReleaseError(f"Release provenance {key} is invalid")
    if manifest is not None:
        if provenance["release_tree_sha256"] != manifest["tree_sha256"]:
            raise ReleaseError("Provenance release tree digest does not match manifest")
        if provenance["release_manifest_sha256"] != sha256_file(root / MANIFEST_NAME):
            raise ReleaseError("Provenance manifest digest does not match manifest")
    return provenance


def validate_frozen_release(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one exact, immutable runtime tree without following special paths."""

    try:
        root_info = root.lstat()
    except OSError as error:
        raise ReleaseError(f"Immutable release is unreadable: {root}") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseError(f"Immutable release is not a regular directory: {root}")
    if root_info.st_mode & 0o222:
        raise ReleaseError("Release is not immutable")

    manifest = validate_manifest(root)
    provenance = validate_provenance(root, manifest)
    if provenance["release_id"] != root.name:
        raise ReleaseError("Release directory name does not match provenance identity")

    expected_files = {record["path"] for record in manifest["files"]}
    expected_files.update({MANIFEST_NAME, PROVENANCE_NAME})
    expected_directories: set[str] = set()
    for relative in expected_files:
        for parent in Path(relative).parents:
            if parent != Path("."):
                expected_directories.add(parent.as_posix())

    actual_files: set[str] = set()
    actual_directories: set[str] = set()

    def inspect_directory(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise ReleaseError(f"Immutable release is unreadable: {directory}") from error
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ReleaseError(f"Immutable release path is unreadable: {relative}") from error
            if stat.S_ISLNK(info.st_mode):
                raise ReleaseError("Symlinks are forbidden in an immutable release")
            if info.st_mode & 0o222:
                raise ReleaseError("Release is not immutable")
            if stat.S_ISDIR(info.st_mode):
                actual_directories.add(relative)
                inspect_directory(path)
            elif stat.S_ISREG(info.st_mode):
                actual_files.add(relative)
            else:
                raise ReleaseError(f"Special paths are forbidden in an immutable release: {relative}")

    inspect_directory(root)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ReleaseError("Immutable release contains an unexpected or missing path")
    return manifest, provenance


def run_git(root: Path, *args: str, text: bool = True) -> str | bytes | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], check=False, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="surrogateescape").strip() if text else proc.stdout


def parse_porcelain_v2_z(payload: bytes) -> list[str]:
    """Return every path from porcelain-v2 -z, including both rename endpoints."""
    paths: list[str] = []
    records = payload.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                raise ReleaseError("Malformed git porcelain ordinary record")
            paths.append(parts[8].decode("utf-8", errors="surrogateescape"))
        elif record.startswith(b"2 "):
            parts = record.split(b" ", 9)
            if len(parts) != 10 or index >= len(records):
                raise ReleaseError("Malformed git porcelain rename record")
            paths.append(parts[9].decode("utf-8", errors="surrogateescape"))
            original = records[index]
            index += 1
            if original:
                paths.append(original.decode("utf-8", errors="surrogateescape"))
        elif record.startswith(b"u "):
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                raise ReleaseError("Malformed git porcelain unmerged record")
            paths.append(parts[10].decode("utf-8", errors="surrogateescape"))
        elif record.startswith((b"? ", b"! ")):
            paths.append(record[2:].decode("utf-8", errors="surrogateescape"))
        elif record.startswith(b"# "):
            continue
        else:
            raise ReleaseError("Unknown git porcelain record")
    return sorted(set(paths))


def git_identity(root: Path) -> dict[str, Any]:
    head = run_git(root, "rev-parse", "HEAD")
    tree = run_git(root, "rev-parse", "HEAD^{tree}")
    porcelain = run_git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--", ".", text=False)
    if not isinstance(head, str) or not isinstance(tree, str) or not isinstance(porcelain, bytes):
        raise ReleaseError(f"Source is not a readable git worktree: {root}")
    paths = parse_porcelain_v2_z(porcelain)
    return {"head": head, "tree": tree, "dirty": bool(paths), "dirty_paths": paths}


def copy_release_files(source: Path, stage: Path, allowlist_path: Path | None = None) -> dict[str, Any]:
    for relative in collect_allowed_paths(source, "runtime", allowlist_path):
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination, follow_symlinks=False)
    return manifest_for(stage, "runtime")


def make_immutable(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def make_writable(root: Path) -> None:
    for path in [root, *sorted(root.rglob("*"))]:
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)


def _legacy_archive_records(root: Path) -> list[dict[str, Any]]:
    """Hash one legacy tree without following or accepting special paths."""

    try:
        root_info = root.lstat()
    except FileNotFoundError as exc:
        raise ReleaseError(f"Legacy runtime is missing: {root}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseError(f"Legacy runtime is not a safe directory: {root}")
    records: list[dict[str, Any]] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda candidate: candidate.name)
        except OSError as exc:
            raise ReleaseError(f"Legacy runtime directory is unreadable: {directory}") from exc
        for path in entries:
            relative = path.relative_to(root).as_posix()
            try:
                info = path.lstat()
            except OSError as exc:
                raise ReleaseError(f"Legacy runtime path is unreadable: {relative}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ReleaseError(f"Legacy runtime symlinks are forbidden: {relative}")
            if stat.S_ISDIR(info.st_mode):
                walk(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"Legacy runtime special paths are forbidden: {relative}")
            if relative == ARCHIVE_MANIFEST_NAME:
                raise ReleaseError(f"Legacy runtime contains reserved archive manifest: {relative}")
            try:
                digest = sha256_file(path)
                size = path.stat().st_size
            except OSError as exc:
                raise ReleaseError(f"Legacy runtime file is unreadable: {relative}") from exc
            records.append({"path": relative, "sha256": digest, "size": size})

    walk(root)
    return sorted(records, key=lambda item: item["path"])


def archive_directory(active: Path, archives: Path) -> Path:
    """Atomically archive a prevalidated legacy runtime or leave it active."""

    try:
        archives.resolve().relative_to(active.resolve())
    except ValueError:
        pass
    else:
        raise ReleaseError("Legacy archive root must be outside the active runtime")
    if archives.is_symlink():
        raise ReleaseError(f"Legacy archive root must not be a symlink: {archives}")
    archives.mkdir(parents=True, exist_ok=True)
    if archives.is_symlink() or not archives.is_dir():
        raise ReleaseError(f"Legacy archive root is unsafe: {archives}")
    archive = archives / f"legacy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    if archive.exists() or archive.is_symlink():
        raise ReleaseError(f"Archive collision: {archive}")
    records = _legacy_archive_records(active)
    manifest = {
        "schema_version": 1, "algorithm": "sha256", "archived_at": utc_now(),
        "files": records, "tree_sha256": tree_digest(records),
    }
    temporary_manifest = archive / f".{ARCHIVE_MANIFEST_NAME}.tmp-{os.getpid()}"
    moved = False
    try:
        os.replace(active, archive)
        moved = True
        if _legacy_archive_records(archive) != records:
            raise ReleaseError("Legacy runtime changed while it was being archived")
        write_json(temporary_manifest, manifest)
        os.replace(temporary_manifest, archive / ARCHIVE_MANIFEST_NAME)
    except BaseException as error:
        if moved:
            for generated in (temporary_manifest, archive / ARCHIVE_MANIFEST_NAME):
                try:
                    if generated.exists() or generated.is_symlink():
                        generated.unlink()
                except OSError:
                    pass
            try:
                if active.exists() or active.is_symlink():
                    raise ReleaseError("Legacy archive rollback target is occupied")
                os.replace(archive, active)
            except Exception as restore_error:
                raise ReleaseError(
                    f"Legacy archive failed and active restoration also failed: {restore_error}"
                ) from error
        if isinstance(error, Exception):
            raise ReleaseError(f"Legacy archive failed; active runtime restored: {error}") from error
        raise
    return archive


def atomic_switch(active: Path, target: Path) -> None:
    temporary = active.with_name(f".{active.name}.next-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(target, temporary)
    os.replace(temporary, active)


def read_state(releases: Path) -> dict[str, Any] | None:
    state_path = releases / STATE_NAME
    if not state_path.is_file():
        return None
    return read_json_object(state_path, "Release state")


def write_state(releases: Path, active_target: Path, previous_target: Path | None) -> None:
    write_json(releases / STATE_NAME, {
        "schema_version": 1,
        "active_target": str(active_target),
        "previous_target": str(previous_target) if previous_target else None,
        "updated_at": utc_now(),
    })
