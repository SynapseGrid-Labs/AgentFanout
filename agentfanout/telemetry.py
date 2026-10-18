"""Client-side redacted operational telemetry for AgentFanout."""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .project_binding import SENSITIVE_CONTENT_PATTERNS


EVENT_TYPES = {"route", "launch", "policy_denied", "result", "validation", "replan", "terminal", "retention"}
ALLOWED_FIELDS = {
    "run_id", "wave_id", "task_id", "worker_id", "provider", "model", "reasoning_effort",
    "status", "action", "reason_code", "input_tokens", "output_tokens", "cost_usd",
    "duration_ms", "residual_count", "validator_verdict", "policy_sha256", "evidence_sha256",
}
SENSITIVE = re.compile(r"(?i)(authorization|bearer|api[_-]?key|secret|password|token|private[_-]?key|credential)")
SENSITIVE_VALUE_PATTERNS = (
    *SENSITIVE_CONTENT_PATTERNS,
    re.compile(r"(?i)\bgh[oprsu]_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"(?i)\bxapp-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"(?i)(?:proxy-)?authorization\s*[:=]\s*(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:\s*[^\r\n]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9_])(?:session|sessionid|sid|auth|token)\s*=\s*[^;\s]{8,}"),
)
_ENCODED_FRAGMENT = re.compile(r"(?<![A-Za-z0-9+/_=-])([A-Za-z0-9+/_-]{16,}={0,2})(?![A-Za-z0-9+/_=-])")
_UNICODE_ESCAPE = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|x([0-9A-Fa-f]{2}))")
_MAX_DECODED_CANDIDATES = 32
_MAX_DECODE_DEPTH = 3
_MAX_SECRET_SCAN_DEPTH = 32
_MAX_SECRET_SCAN_NODES = 4096
_CREDENTIAL_KEY_TERM = re.compile(
    r"(?:^|_)(?:api_key|api_token|access_token|refresh_token|auth_token|client_secret|"
    r"private_key|secret_access_key|access_key|password|passwd|pwd|credential|credentials|"
    r"authorization|proxy_authorization|authentication|auth|bearer|secret|token|cookie|"
    r"set_cookie|session_cookie|"
    r"session|session_id|sessionid|sid|pin|passcode|pass_code|security_pin|access_pin|"
    r"otp|totp|mfa_code|verification_code|security_code|access_code|recovery_code|"
    r"backup_code|cvv|2fa_code|two_factor_code)(?:_|$)"
)
_CONNECTION_KEY_TERM = re.compile(
    r"(?:^|_)(?:(?:database|db|connection|postgres|postgresql|mysql|mariadb|redis|mongodb|"
    r"mongo|jdbc)(?:_connection)?_(?:url|uri|dsn|string)|dsn)(?:_|$)"
)
_SAFE_CREDENTIAL_PLACEHOLDERS = {
    "", "none", "null", "missing", "not present", "not_present", "unavailable",
    "redacted", "<redacted>", "[redacted]", "masked", "placeholder", "example",
    "disabled", "false", "unknown", "n/a", "not set", "not_set", "not configured",
    "not_configured", "omitted", "removed", "present", "configured", "required",
}
_SAFE_CREDENTIAL_PLACEHOLDER_WRAPPER = re.compile(
    r"(?i)^[\s*_<\[(]*(?:redacted|masked|missing|omitted|removed|not[ _-]?(?:set|present|configured))[\s*_>\])]*$"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _nested_strings(value: Any, *, depth: int = 0):
    if depth > _MAX_DECODE_DEPTH:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value[:16]:
            yield from _nested_strings(item, depth=depth + 1)
    elif isinstance(value, dict):
        for index, (key, item) in enumerate(value.items()):
            if index >= 16:
                break
            if isinstance(key, str):
                yield key
            yield from _nested_strings(item, depth=depth + 1)


def _decoded_sensitive_candidates(value: str):
    queue: list[tuple[str, int]] = [(value, 0)]
    seen: set[str] = set()
    while queue and len(seen) < _MAX_DECODED_CANDIDATES:
        candidate, depth = queue.pop(0)
        if candidate in seen:
            continue
        seen.add(candidate)
        yield candidate
        if depth >= _MAX_DECODE_DEPTH:
            continue

        decoded: list[str] = []
        if "%" in candidate:
            unquoted = urllib.parse.unquote(candidate)
            if unquoted != candidate:
                decoded.append(unquoted)
        if "\\" in candidate:
            unescaped = _UNICODE_ESCAPE.sub(
                lambda match: chr(int(match.group(1) or match.group(2), 16)),
                candidate,
            )
            if unescaped != candidate:
                decoded.append(unescaped)
        stripped = candidate.strip()
        if stripped.startswith(("{", "[", '"')):
            try:
                nested = json.loads(stripped)
            except (TypeError, ValueError, json.JSONDecodeError):
                nested = None
            if nested is not None:
                decoded.extend(_nested_strings(nested))
        for fragment in _ENCODED_FRAGMENT.findall(candidate)[:8]:
            padded = fragment + "=" * ((4 - len(fragment) % 4) % 4)
            try:
                raw = base64.b64decode(padded, altchars=b"-_", validate=True)
                text = raw.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if text and all(character.isprintable() or character in "\r\n\t" for character in text):
                decoded.append(text)
        queue.extend((item, depth + 1) for item in decoded if isinstance(item, str))


def _sensitive_string(value: str) -> bool:
    for candidate in _decoded_sensitive_candidates(value):
        if SENSITIVE.search(candidate):
            return True
        if any(pattern.search(candidate) is not None for pattern in SENSITIVE_VALUE_PATTERNS):
            return True
    return False


def _normalized_mapping_key(key: str) -> str:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip())
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def _credential_key(key: str) -> bool:
    normalized = _normalized_mapping_key(key)
    return bool(
        normalized
        and (
            _CREDENTIAL_KEY_TERM.search(normalized)
            or _CONNECTION_KEY_TERM.search(normalized)
        )
    )


def _credential_scalar_present(value: Any) -> bool:
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > 256 or depth > 16:
            return True
        if isinstance(item, str):
            candidate = item.strip()
            if candidate.casefold() in _SAFE_CREDENTIAL_PLACEHOLDERS:
                continue
            if _SAFE_CREDENTIAL_PLACEHOLDER_WRAPPER.fullmatch(candidate):
                continue
            if len(candidate) >= 3 and set(candidate) <= {"*", "x", "X", "-", "_"}:
                continue
            if candidate:
                return True
        elif isinstance(item, bool):
            # False is an explicit absence/disabled marker. True is ambiguous
            # credential-presence data and therefore fails closed.
            if item:
                return True
        elif item is None:
            # JSON null is the only non-string absence placeholder.
            continue
        elif isinstance(item, (int, float)):
            # Numeric API keys, passwords, tokens, and PINs are still secrets;
            # type coercion must not bypass the credential-key boundary.
            return True
        elif isinstance(item, dict):
            stack.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in item)
    return False


def _credential_value_present(key: str, value: Any) -> bool:
    """Detect a credential stored under a credential-bearing object key.

    This deliberately distinguishes a credential value from prose that merely
    discusses an API key or token.  Packet screening is a fail-closed final
    boundary, but security-review results must still be able to say that a
    credential is missing or redacted without being rejected.
    """

    return _credential_key(key) and _credential_scalar_present(value)


def contains_sensitive_material(value: Any) -> bool:
    """Return whether a bounded result contains credential material.

    Provider packets are untrusted even after their structural schema passes.
    Scan every string, decoded representation, and credential-bearing mapping
    value immediately before acceptance/persistence.  Excessive nesting also
    fails closed instead of creating a screening bypass.
    """

    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > _MAX_SECRET_SCAN_NODES or depth > _MAX_SECRET_SCAN_DEPTH:
            return True
        if isinstance(item, str):
            for candidate in _decoded_sensitive_candidates(item):
                if any(pattern.search(candidate) is not None for pattern in SENSITIVE_VALUE_PATTERNS):
                    return True
            continue
        if isinstance(item, dict):
            for key, nested in item.items():
                if isinstance(key, str):
                    if _credential_value_present(key, nested):
                        return True
                    stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
            continue
        if isinstance(item, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in item)
    return False


def _hash_string(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def sanitize_event(event_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError("telemetry_event_type_invalid")
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in ALLOWED_FIELDS or SENSITIVE.search(key):
            continue
        if isinstance(value, str):
            if value.startswith("/") or len(value) > 256 or _sensitive_string(value):
                sanitized[key] = _hash_string(value)
            else:
                sanitized[key] = value
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("telemetry_numeric_non_finite")
        elif isinstance(value, (int, float, bool)) or value is None:
            sanitized[key] = value
    return {"event_type": event_type, "occurred_at": _now(), "fields": sanitized}


def _sanitize_for_sink(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict) or not isinstance(event.get("fields"), dict):
        raise ValueError("telemetry_event_shape_invalid")
    sanitized = sanitize_event(event.get("event_type"), event["fields"])
    occurred_at = event.get("occurred_at")
    if isinstance(occurred_at, str) and len(occurred_at) <= 64:
        try:
            parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            sanitized["occurred_at"] = occurred_at
    return sanitized


class JsonlSink:
    def __init__(self, path: str | Path):
        self.path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
        self._prepare_private_directory()
        parent = os.lstat(self.path.parent)
        self._parent_identity = (parent.st_dev, parent.st_ino)
        descriptor = self._open_file(allow_create=True)
        try:
            current = os.fstat(descriptor)
            self._file_identity = (current.st_dev, current.st_ino)
        finally:
            os.close(descriptor)

    def emit(self, event: dict[str, Any]) -> None:
        safe_event = _sanitize_for_sink(event)
        encoded = (json.dumps(safe_event, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        descriptor = self._open_file(allow_create=False)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != self._file_identity:
                raise ValueError("telemetry_jsonl_file_identity_changed")
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise OSError("telemetry_jsonl_short_write")
        finally:
            os.close(descriptor)

    def _prepare_private_directory(self) -> None:
        directory = self.path.parent
        missing: list[Path] = []
        cursor = directory
        while True:
            try:
                current = os.lstat(cursor)
            except FileNotFoundError:
                missing.append(cursor)
                parent = cursor.parent
                if parent == cursor:
                    raise ValueError("telemetry_jsonl_directory_missing_root")
                cursor = parent
                continue
            if stat.S_ISLNK(current.st_mode):
                raise ValueError("telemetry_jsonl_directory_symlink")
            if not stat.S_ISDIR(current.st_mode):
                raise ValueError("telemetry_jsonl_directory_not_directory")
            break

        for candidate in reversed(missing):
            created = False
            try:
                os.mkdir(candidate, 0o700)
                created = True
            except FileExistsError:
                pass
            current = os.lstat(candidate)
            if stat.S_ISLNK(current.st_mode):
                raise ValueError("telemetry_jsonl_directory_symlink")
            if not stat.S_ISDIR(current.st_mode):
                raise ValueError("telemetry_jsonl_directory_not_directory")
            if created:
                os.chmod(candidate, 0o700, follow_symlinks=False)
                current = os.lstat(candidate)
            self._validate_private_directory(current)

        self._reject_symlinked_directory_components(directory)
        self._validate_private_directory(os.lstat(directory))

    @staticmethod
    def _reject_symlinked_directory_components(directory: Path) -> None:
        current = Path(directory.anchor)
        for component in directory.parts[1:]:
            current = current / component
            status = os.lstat(current)
            if stat.S_ISLNK(status.st_mode):
                raise ValueError("telemetry_jsonl_directory_symlink")

    @staticmethod
    def _validate_private_directory(status: os.stat_result) -> None:
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError("telemetry_jsonl_directory_not_directory")
        if status.st_uid != os.geteuid():
            raise ValueError("telemetry_jsonl_directory_owner_unsafe")
        if stat.S_IMODE(status.st_mode) != 0o700:
            raise ValueError("telemetry_jsonl_directory_permissions_unsafe")

    @staticmethod
    def _validate_private_file(status: os.stat_result) -> None:
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("telemetry_jsonl_file_not_regular")
        if status.st_uid != os.geteuid():
            raise ValueError("telemetry_jsonl_file_owner_unsafe")
        if stat.S_IMODE(status.st_mode) != 0o600:
            raise ValueError("telemetry_jsonl_file_permissions_unsafe")
        if status.st_nlink != 1:
            raise ValueError("telemetry_jsonl_file_link_count_unsafe")

    def _open_parent(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path.parent, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("telemetry_jsonl_directory_symlink") from exc
            raise
        try:
            current = os.fstat(descriptor)
            self._validate_private_directory(current)
            if (current.st_dev, current.st_ino) != self._parent_identity:
                raise ValueError("telemetry_jsonl_directory_identity_changed")
            path_status = os.lstat(self.path.parent)
            if (path_status.st_dev, path_status.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("telemetry_jsonl_directory_identity_changed")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_file(self, *, allow_create: bool) -> int:
        parent_descriptor = self._open_parent()
        try:
            try:
                entry = os.stat(self.path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                entry = None
            if entry is not None and stat.S_ISLNK(entry.st_mode):
                raise ValueError("telemetry_jsonl_file_symlink")

            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            created = False
            if allow_create and entry is None:
                try:
                    descriptor = os.open(
                        self.path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    created = True
                except FileExistsError:
                    descriptor = os.open(self.path.name, flags, dir_fd=parent_descriptor)
            else:
                try:
                    descriptor = os.open(self.path.name, flags, dir_fd=parent_descriptor)
                except FileNotFoundError as exc:
                    raise ValueError("telemetry_jsonl_file_missing") from exc
            try:
                if created:
                    os.fchmod(descriptor, 0o600)
                current = os.fstat(descriptor)
                self._validate_private_file(current)
                path_status = os.stat(self.path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                if stat.S_ISLNK(path_status.st_mode):
                    raise ValueError("telemetry_jsonl_file_symlink")
                if (path_status.st_dev, path_status.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("telemetry_jsonl_file_identity_changed")
                return descriptor
            except Exception:
                os.close(descriptor)
                raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("telemetry_jsonl_file_symlink") from exc
            raise
        finally:
            os.close(parent_descriptor)


class LangfuseHttpSink:
    """Small fail-closed sink for the version-pinned Langfuse ingestion API."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        timeout: float = 5.0,
        allowed_hosts: set[str] | None = None,
        *,
        dangerously_allow_insecure_loopback_http_for_testing: bool = False,
    ):
        if not public_key or not secret_key:
            raise ValueError("langfuse_sink_configuration_invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("langfuse_sink_timeout_invalid")
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("langfuse_sink_configuration_invalid")
        if parsed.scheme != "https":
            try:
                is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                is_loopback = False
            if not dangerously_allow_insecure_loopback_http_for_testing or not is_loopback:
                raise ValueError("langfuse_sink_https_required")
        configured = (
            {item.strip() for item in os.environ.get("LANGFUSE_ALLOWED_HOSTS", "").split(",") if item.strip()}
            if allowed_hosts is None
            else allowed_hosts
        )
        configured = {item.casefold().rstrip(".") for item in configured}
        hostname = parsed.hostname.casefold().rstrip(".")
        if parsed.username or parsed.password or hostname not in configured:
            raise ValueError("langfuse_sink_host_not_allowed")
        self.url = host.rstrip("/") + "/api/public/ingestion"
        self.authorization = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.timeout = float(timeout)

    def emit(self, event: dict[str, Any]) -> None:
        safe_event = _sanitize_for_sink(event)
        event_id = hashlib.sha256(json.dumps(safe_event, sort_keys=True, allow_nan=False).encode()).hexdigest()
        body = {"batch": [{"id": event_id, "type": "event-create", "timestamp": safe_event["occurred_at"],
                           "body": {"name": "agentfanout." + safe_event["event_type"], "metadata": safe_event["fields"]}}]}
        request = urllib.request.Request(
            self.url, data=json.dumps(body, allow_nan=False).encode(), method="POST",
            headers={"Authorization": self.authorization, "Content-Type": "application/json"},
        )
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=self.timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"langfuse_ingestion_failed:{response.status}")
