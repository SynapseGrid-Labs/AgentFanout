"""Portable, host-owned project binding and bounded-input policy.

The project root is an authorization decision, not an ambient working-directory
default.  Public callers must pass an exact root or explicitly opt into Git
worktree discovery.  Local installations can add denied roots through
``AGENT_FANOUT_SENSITIVE_ROOTS`` without baking private topology into the
package.
"""

from __future__ import annotations

import hashlib
import json
import errno
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class ProjectBindingError(ValueError):
    """The requested project authority cannot be established safely."""


PORTABLE_SENSITIVE_DIR_NAMES = frozenset({
    ".aws", ".claude", ".codex", ".config", ".git", ".gnupg", ".pai", ".ssh",
})
PORTABLE_SENSITIVE_FILE_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc", "credentials", "id_dsa",
    "id_ecdsa", "id_ed25519", "id_rsa", "private_key", "privatekey",
})
PORTABLE_SENSITIVE_PATH_NAMES = frozenset({
    ".azure", ".docker", ".gcloud", ".kube",
})
PORTABLE_SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
SENSITIVE_CONFIG_STEMS = frozenset({
    "api-key", "api_key", "api_keys", "apikey", "auth", "authentication",
    "oauth", "service-account", "service-accounts", "service_account", "service_accounts", "serviceaccount", "serviceaccounts",
    "token", "tokens", "access-key", "access-keys", "access_key", "access_keys",
    "access-token", "access-tokens", "access_token", "access_tokens",
})
SENSITIVE_CONFIG_SUFFIXES = frozenset({".conf", ".env", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml"})
SENSITIVE_CONTENT_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk[-_]|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\b(?:xai-)[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\b(?:glpat-|npm_)[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/:@\s]+:[^@\s/]+@[^\s]+", re.IGNORECASE),
    re.compile(
        r'''(?ix)["'](?:api[_-]?key|secret|password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|'''
        r'''auth[_-]?token|aws[_-]?secret[_-]?access[_-]?key|database[_-]?(?:url|uri|dsn)|'''
        r'''connection[_-]?(?:string|url|uri))["']\s*:\s*["'][^"']{8,}["']'''
    ),
    re.compile(
        r'''(?im)^\s*(?:export\s+)?(?:api[_-]?key|secret|password|passwd|pwd|token|access[_-]?token|'''
        r'''refresh[_-]?token|auth[_-]?token|aws[_-]?secret[_-]?access[_-]?key|database[_-]?(?:url|uri|dsn)|'''
        r'''connection[_-]?(?:string|url|uri))\s*[:=]\s*["']?[^\s#"']{8,}'''
    ),
    re.compile(
        r'''(?im)^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_-]*(?:api[_-]?key|secret|password|passwd|pwd|token|'''
        r'''access[_-]?key)\s*[:=]\s*["']?[^\s#"']{8,}'''
    ),
    re.compile(
        r'''(?ix)["'][A-Za-z_][A-Za-z0-9_-]*(?:api[_-]?key|secret|password|passwd|pwd|token|'''
        r'''access[_-]?key)["']\s*:\s*["'][^"']{8,}["']'''
    ),
    re.compile(r"(?im)^\s*//[^:\s]+/:_authToken\s*=\s*[^\s#]{8,}"),
    re.compile(r"(?im)^\s*(?:registry\.)?npmjs\.org/:_authToken\s*=\s*[^\s#]{8,}"),
    re.compile(r"(?im)^\s*machine\s+\S+(?:\s+login\s+\S+)?\s+password\s+\S+"),
    re.compile(r'''(?is)["']auths["']\s*:\s*\{.*?["']auth["']\s*:\s*["'][A-Za-z0-9+/=]{8,}["']'''),
    re.compile(r"(?im)(?:^|;)\s*AccountKey\s*=\s*[^;\s]{8,}"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _csv_paths(raw: str | None) -> list[Path]:
    values: list[Path] = []
    for item in (raw or "").split(","):
        if item.strip():
            values.append(Path(item.strip()).expanduser().resolve(strict=False))
    return values


def configured_sensitive_roots(extra: Iterable[str | Path] = ()) -> tuple[Path, ...]:
    """Return exact denied roots; ordinary repo names such as ``auth`` remain valid."""

    home = Path.home().resolve()
    roots = [home / name for name in PORTABLE_SENSITIVE_DIR_NAMES if name != ".git"]
    roots.extend(_csv_paths(os.getenv("AGENT_FANOUT_SENSITIVE_ROOTS")))
    roots.extend(Path(item).expanduser().resolve(strict=False) for item in extra)
    return tuple(dict.fromkeys(path.resolve(strict=False) for path in roots))


def _policy_digest() -> str:
    return _canonical_sha256({
        "sensitive_roots": sorted(str(path) for path in configured_sensitive_roots()),
        "portable_sensitive_dirs": sorted(PORTABLE_SENSITIVE_DIR_NAMES),
    })


def path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise ProjectBindingError("secure_descriptor_traversal_unsupported")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_chain(root: Path, parts: tuple[str, ...]) -> int:
    """Open a symlink-free directory chain and return the final descriptor."""

    try:
        anchor = Path(root.anchor)
        descriptor = os.open(anchor, _directory_flags())
    except OSError as exc:
        raise ProjectBindingError("bounded_directory_unavailable") from exc
    try:
        root_parts = root.parts[1:] if root.anchor else root.parts
        for part in (*root_parts, *parts):
            if part in {"", ".", ".."}:
                raise ProjectBindingError("bounded_file_reference_unsafe")
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except OSError as exc:
                raise ProjectBindingError("bounded_directory_symlink_or_unavailable") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def secure_private_directory(path: str | Path, *, create: bool = False) -> Path:
    """Require an owned mode-0700 directory reached without symlinks."""

    raw = Path(path).expanduser()
    normalized = Path(os.path.abspath(os.path.normpath(str(raw))))
    if not normalized.is_absolute():
        raise ProjectBindingError("private_directory_must_be_absolute")
    flags = _directory_flags()
    descriptor = os.open(Path(normalized.anchor), flags)
    try:
        uid = os.getuid() if hasattr(os, "getuid") else None
        root_info = os.fstat(descriptor)
        if uid is not None and root_info.st_uid not in {0, uid}:
            raise ProjectBindingError("private_directory_ancestor_owner_mismatch")
        if stat.S_IMODE(root_info.st_mode) & 0o022:
            if not (root_info.st_uid == 0 and root_info.st_mode & stat.S_ISVTX):
                raise ProjectBindingError("private_directory_ancestor_permissions_unsafe")
        parts = normalized.parts[1:]
        for index, part in enumerate(parts):
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno != errno.ENOENT or not create:
                    raise ProjectBindingError("private_directory_symlink_or_unavailable") from exc
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError as create_exc:
                    raise ProjectBindingError("private_directory_create_failed") from create_exc
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            is_final = index == len(parts) - 1
            if not is_final:
                if uid is not None and info.st_uid not in {0, uid}:
                    raise ProjectBindingError("private_directory_ancestor_owner_mismatch")
                if stat.S_IMODE(info.st_mode) & 0o022:
                    if not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                        raise ProjectBindingError("private_directory_ancestor_permissions_unsafe")
        info = os.fstat(descriptor)
        if uid is not None and info.st_uid != uid:
            raise ProjectBindingError("private_directory_owner_mismatch")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ProjectBindingError("private_directory_permissions_unsafe")
        return normalized
    finally:
        os.close(descriptor)


def default_private_state_directory(namespace: str = "agentfanout") -> Path:
    """Return a created owner-only state root with a safe default fallback.

    An explicitly configured ``XDG_STATE_HOME`` is authoritative and therefore
    fails closed when relative or reached through a writable ancestor. For the
    implicit XDG default only, fall back under the user's home when a commonly
    group-writable ``~/.local`` cannot satisfy the private-path contract.
    """

    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", namespace):
        raise ProjectBindingError("private_state_namespace_invalid")
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    if not base.is_absolute():
        raise ProjectBindingError("xdg_state_home_must_be_absolute")
    preferred = base / namespace
    if configured:
        return secure_private_directory(preferred, create=True)
    try:
        return secure_private_directory(preferred, create=True)
    except ProjectBindingError as exc:
        if str(exc) != "private_directory_ancestor_permissions_unsafe":
            raise
    return secure_private_directory(Path.home() / f".{namespace}" / "state", create=True)


def secure_private_file(
    path: str | Path, *, create: bool = False, max_bytes: int | None = None,
) -> tuple[Path, int]:
    """Open an owned mode-0600 regular file without following a symlink."""

    raw = Path(path).expanduser()
    normalized = Path(os.path.abspath(os.path.normpath(str(raw))))
    parent = secure_private_directory(normalized.parent, create=create)
    parent_fd = _open_directory_chain(parent, ())
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT
    try:
        try:
            descriptor = os.open(normalized.name, flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise ProjectBindingError("private_file_symlink_or_unavailable") from exc
    finally:
        os.close(parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ProjectBindingError("private_file_not_regular")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ProjectBindingError("private_file_owner_mismatch")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ProjectBindingError("private_file_permissions_unsafe")
        if max_bytes is not None and info.st_size > max_bytes:
            raise ProjectBindingError("private_file_too_large")
        return normalized, descriptor
    except Exception:
        os.close(descriptor)
        raise


def secure_bounded_directory(root: str | Path, candidate: str | Path) -> Path:
    """Validate an absolute project subdirectory without following symlinks."""

    project = Path(root).expanduser().resolve(strict=True)
    raw = Path(candidate).expanduser()
    if not raw.is_absolute():
        raise ProjectBindingError("bounded_file_allowed_path_invalid")
    normalized = Path(os.path.abspath(os.path.normpath(str(raw))))
    try:
        relative = normalized.relative_to(project)
    except ValueError as exc:
        raise ProjectBindingError("bounded_file_allowed_path_outside_project") from exc
    descriptor = _open_directory_chain(project, relative.parts)
    os.close(descriptor)
    return normalized


def secure_bounded_utf8_read(
    root: str | Path, relative: str | Path, *, max_bytes: int = 250_000,
) -> tuple[Path, str]:
    """Read one regular UTF-8 file using openat-style no-follow traversal."""

    project = Path(root).expanduser().resolve(strict=True)
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts or relative_path_is_sensitive(rel):
        raise ProjectBindingError("bounded_file_reference_unsafe")
    parent = _open_directory_chain(project, rel.parts[:-1])
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(rel.parts[-1], flags, dir_fd=parent)
        except OSError as exc:
            raise ProjectBindingError("bounded_file_symlink_or_unavailable") from exc
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ProjectBindingError("bounded_file_unavailable_or_too_large")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes or os.read(descriptor, 1):
            raise ProjectBindingError("bounded_file_unavailable_or_too_large")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectBindingError("bounded_file_not_utf8_text") from exc
        return project / rel, content
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def path_is_sensitive(path: str | Path, *, sensitive_roots: Iterable[str | Path] = ()) -> bool:
    resolved = Path(path).expanduser().resolve(strict=False)
    return any(
        resolved == root or path_within(resolved, root)
        for root in configured_sensitive_roots(sensitive_roots)
    )


def relative_path_is_sensitive(relative: str | Path) -> bool:
    """Detect credential-bearing paths without rejecting normal source like auth.py."""

    path = Path(relative)
    for index, part in enumerate(path.parts):
        lowered = part.lower()
        is_leaf = index == len(path.parts) - 1
        if lowered in PORTABLE_SENSITIVE_DIR_NAMES or lowered in PORTABLE_SENSITIVE_PATH_NAMES:
            return True
        if is_leaf and (
            lowered in PORTABLE_SENSITIVE_FILE_NAMES
            or lowered.startswith(".env.")
            or "credential" in lowered
            or "secret" in lowered
            or "private_key" in lowered
            or "private-key" in lowered
            or Path(lowered).suffix in PORTABLE_SENSITIVE_SUFFIXES
            or (
                Path(lowered).stem in SENSITIVE_CONFIG_STEMS
                and Path(lowered).suffix in SENSITIVE_CONFIG_SUFFIXES
            )
        ):
            return True
    return False


def bounded_content_is_sensitive(content: str) -> bool:
    return any(pattern.search(content) is not None for pattern in SENSITIVE_CONTENT_PATTERNS)


def secure_bounded_input(
    root: str | Path, input_ref: str, *, max_bytes: int = 250_000,
) -> tuple[Path, str]:
    if not isinstance(input_ref, str) or not input_ref.startswith("file:"):
        raise ProjectBindingError("adapter_input_mode_unsupported")
    relative = Path(input_ref.removeprefix("file:"))
    if relative.is_absolute() or ".." in relative.parts or relative_path_is_sensitive(relative):
        raise ProjectBindingError("bounded_file_reference_unsafe")
    path, content = secure_bounded_utf8_read(root, relative, max_bytes=max_bytes)
    if bounded_content_is_sensitive(content):
        raise ProjectBindingError("bounded_file_content_sensitive")
    return path, content


def _approved_allowlist() -> set[Path]:
    approved: set[Path] = set()
    for path in _csv_paths(os.getenv("AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST")):
        try:
            approved.add(path.resolve(strict=True))
        except OSError as exc:
            raise ProjectBindingError("approved_root_allowlist_invalid") from exc
    return approved


def _configured_broad_roots() -> set[Path]:
    return set(_csv_paths(os.getenv("AGENT_FANOUT_BROAD_ROOTS")))


def _has_git_marker(root: Path) -> bool:
    marker = root / ".git"
    try:
        info = marker.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    if stat.S_ISDIR(info.st_mode):
        return True
    if not stat.S_ISREG(info.st_mode) or not 8 <= info.st_size <= 4096:
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(marker, flags)
        try:
            payload = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    if len(payload) > 4096 or b"\x00" in payload:
        return False
    try:
        line = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        return False
    return line.startswith("gitdir: ") and bool(line.removeprefix("gitdir: ").strip())


def _discover_git_root(cwd: Path) -> Path:
    try:
        current = cwd.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProjectBindingError("approved_project_root_invalid") from exc
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if _has_git_marker(candidate):
            return candidate
    raise ProjectBindingError("approved_project_root_not_repo_or_allowlisted")


def validate_project_root(root: str | Path) -> Path:
    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProjectBindingError("approved_project_root_invalid") from exc
    if not resolved.is_dir():
        raise ProjectBindingError("approved_project_root_not_directory")
    home = Path.home().resolve()
    broad = {
        Path(resolved.anchor), home, home.parent,
        Path("/home").resolve(strict=False), Path("/tmp").resolve(strict=False),
    }
    broad.update(_configured_broad_roots())
    if resolved in broad:
        raise ProjectBindingError("approved_project_root_too_broad")
    if path_is_sensitive(resolved):
        raise ProjectBindingError("approved_project_root_sensitive")
    # Git worktrees use either a directory or a text .git marker.
    if not _has_git_marker(resolved) and resolved not in _approved_allowlist():
        raise ProjectBindingError("approved_project_root_not_repo_or_allowlisted")
    return resolved


def resolve_project_binding(
    *,
    explicit_root: str | Path | None,
    env_root: str | Path | None = None,
    cwd: str | Path | None = None,
    discover_current_worktree: bool = False,
) -> dict[str, Any]:
    """Resolve one exact project authority and return a digest-bound packet."""

    if explicit_root is not None and discover_current_worktree:
        raise ProjectBindingError("project_root_modes_mutually_exclusive")
    configured = env_root if env_root is not None else os.getenv("AGENT_FANOUT_PROJECT_ROOT", "")
    configured = str(configured).strip() if configured is not None else ""
    source = "explicit_cli"
    requested: str | Path | None = explicit_root
    if requested is None and discover_current_worktree:
        requested = _discover_git_root(Path(cwd or Path.cwd()))
        source = "explicit_worktree_discovery"
    elif requested is None and configured:
        requested = configured
        source = "compatibility_environment"
    if requested is None or not str(requested).strip():
        raise ProjectBindingError("approved_project_root_missing")
    root = validate_project_root(requested)
    if source in {"explicit_cli", "explicit_worktree_discovery"} and configured:
        configured_root = validate_project_root(configured)
        if configured_root != root:
            raise ProjectBindingError("project_root_not_host_approved")
    cwd_path = Path(cwd or Path.cwd()).expanduser().resolve(strict=False)
    vcs = "git" if _has_git_marker(root) else "none"
    stable = {
        "schema": "ProjectBindingV1",
        "resolved_root": str(root),
        "vcs": vcs,
        "vcs_root": str(root),
        "policy_digest": _policy_digest(),
    }
    return {
        **stable,
        "source": source,
        "requested_root": str(requested),
        "root_sha256": _canonical_sha256(stable),
        "cwd_relation": "inside" if path_within(cwd_path, root) else "outside",
        "created_at": _now(),
    }


def validate_project_binding(binding: dict[str, Any], *, expected_root: str | Path | None = None) -> Path:
    if not isinstance(binding, dict) or binding.get("schema") != "ProjectBindingV1":
        raise ProjectBindingError("project_binding_required")
    root = validate_project_root(str(binding.get("resolved_root", "")))
    stable = {
        "schema": "ProjectBindingV1",
        "resolved_root": str(root),
        "vcs": binding.get("vcs"),
        "vcs_root": binding.get("vcs_root"),
        "policy_digest": binding.get("policy_digest"),
    }
    expected_vcs = "git" if _has_git_marker(root) else "none"
    if binding.get("vcs") != expected_vcs or binding.get("vcs_root") != str(root):
        raise ProjectBindingError("project_binding_vcs_mismatch")
    if binding.get("policy_digest") != _policy_digest():
        raise ProjectBindingError("project_binding_policy_mismatch")
    if binding.get("root_sha256") != _canonical_sha256(stable):
        raise ProjectBindingError("project_binding_digest_mismatch")
    if expected_root is not None and validate_project_root(expected_root) != root:
        raise ProjectBindingError("project_binding_root_mismatch")
    return root


def resolve_bounded_file(
    binding: dict[str, Any], input_ref: str, *, max_bytes: int = 250_000,
) -> tuple[Path, str]:
    root = validate_project_binding(binding)
    return secure_bounded_input(root, input_ref, max_bytes=max_bytes)
