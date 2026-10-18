"""Host-enforced execution contracts for AgentFanout.

The portable lifecycle remains provider-neutral.  This module is the mandatory
wrapper for hosts that claim end-to-end enforcement: it records launches and
effects, requires measured usage and validator receipts, verifies signed Local
launch attestations, and authorizes parent/terminal actions.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from functools import wraps

from .lifecycle import ingest_results, record_provider_usage, validate_run_state
from .project_binding import secure_private_file
from .telemetry import contains_sensitive_material

try:  # Fail closed at verification time when the runtime dependency is absent.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # pragma: no cover - exercised through explicit error path
    InvalidSignature = Exception
    Ed25519PublicKey = None


EFFECT_STATES = {"pending", "applied", "failed", "unknown_after_crash"}
VALIDATOR_VERDICTS = {"pass", "concerns", "fail"}
PARENT_WORKER_ACTIONS = {"worker_research", "worker_edit", "worker_debug", "worker_validation"}
MAX_CAPABILITY_LIFETIME_SECONDS = 24 * 60 * 60
MAX_LAUNCH_LEASE_SECONDS = 15 * 60
MAX_CLOCK_SKEW_SECONDS = 30
MAX_VALIDATOR_CHECK_AGE_SECONDS = 15 * 60
LEDGER_CHAIN_GENESIS_SHA256 = "0" * 64
DOCKER_SANDBOX_EXPERIMENTAL_STATUS = "experimental_observation_only"
DOCKER_SANDBOX_ENFORCED_STATUS = "host_enforced"
DOCKER_TMPFS_POLICY = {
    "destination": "/tmp",
    "options": ["nodev", "noexec", "nosuid", "rw", "size=64m"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")


def _reject_nonfinite_json_constant(_value: str) -> None:
    raise ValueError("json_numeric_non_finite")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _event_chain_sha256(
    *,
    event_id: str,
    run_id: str,
    task_id: str | None,
    event_type: str,
    occurred_at: str,
    payload_sha256: str,
    previous_event_sha256: str,
) -> str:
    """Hash the immutable identity and content link for one ledger event."""

    return hashlib.sha256(_canonical({
        "event_id": event_id,
        "run_id": run_id,
        "task_id": task_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "payload_sha256": payload_sha256,
        "previous_event_sha256": previous_event_sha256,
    })).hexdigest()


def _parse_time(value: Any, error: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    if parsed.tzinfo is None:
        raise ValueError(error)
    return parsed.astimezone(timezone.utc)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _docker_safe_text(value: Any, error: str, *, reject_comma: bool = False) -> str:
    """Validate a value before it reaches Docker's argument mini-languages."""

    if not isinstance(value, str) or not value:
        raise ValueError(error)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(error)
    if reject_comma and "," in value:
        raise ValueError(error)
    return value


def _canonical_docker_daemon_identity(value: Any) -> dict[str, Any]:
    required = {"engine", "endpoint", "server_id", "server_version", "rootless"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("docker_daemon_identity_invalid")
    engine = _docker_safe_text(value.get("engine"), "docker_daemon_identity_invalid")
    endpoint = _docker_safe_text(value.get("endpoint"), "docker_daemon_identity_invalid", reject_comma=True)
    server_id = _docker_safe_text(value.get("server_id"), "docker_daemon_identity_invalid", reject_comma=True)
    server_version = _docker_safe_text(value.get("server_version"), "docker_daemon_identity_invalid", reject_comma=True)
    if engine != "docker" or not endpoint.startswith("unix://") or not Path(endpoint[7:]).is_absolute():
        raise ValueError("docker_daemon_identity_invalid")
    if not isinstance(value.get("rootless"), bool):
        raise ValueError("docker_daemon_identity_invalid")
    return {
        "engine": engine,
        "endpoint": endpoint,
        "server_id": server_id,
        "server_version": server_version,
        "rootless": value["rootless"],
    }


def _canonical_docker_limits(memory: Any, cpus: Any, pids_limit: Any) -> dict[str, Any]:
    memory_text = _docker_safe_text(str(memory), "docker_memory_limit_invalid")
    if not re.fullmatch(r"[1-9][0-9]*(?:[bkmg]|[kmgt]b)?", memory_text.lower()):
        raise ValueError("docker_memory_limit_invalid")
    cpu_text = _docker_safe_text(str(cpus), "docker_cpu_limit_invalid")
    try:
        cpu_value = Decimal(cpu_text)
    except InvalidOperation as exc:
        raise ValueError("docker_cpu_limit_invalid") from exc
    if not cpu_value.is_finite() or cpu_value <= 0 or cpu_value > 1024:
        raise ValueError("docker_cpu_limit_invalid")
    normalized_cpu = format(cpu_value.normalize(), "f")
    if isinstance(pids_limit, bool):
        raise ValueError("docker_pids_limit_invalid")
    try:
        normalized_pids = int(pids_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("docker_pids_limit_invalid") from exc
    if normalized_pids < 1 or normalized_pids > 4096 or str(normalized_pids) != str(pids_limit):
        raise ValueError("docker_pids_limit_invalid")
    return {"memory": memory_text.lower(), "cpus": normalized_cpu, "pids_limit": normalized_pids}


def _canonical_docker_tmpfs(value: Any) -> dict[str, Any]:
    if value is None:
        value = DOCKER_TMPFS_POLICY
    if not isinstance(value, dict) or set(value) != {"destination", "options"}:
        raise ValueError("docker_tmpfs_policy_invalid")
    destination = _docker_safe_text(value.get("destination"), "docker_tmpfs_policy_invalid", reject_comma=True)
    options = value.get("options")
    if destination != DOCKER_TMPFS_POLICY["destination"] or not isinstance(options, list):
        raise ValueError("docker_tmpfs_policy_invalid")
    normalized_options = sorted({_docker_safe_text(item, "docker_tmpfs_policy_invalid", reject_comma=True) for item in options})
    if len(options) != len(normalized_options) or normalized_options != DOCKER_TMPFS_POLICY["options"]:
        raise ValueError("docker_tmpfs_policy_invalid")
    return {"destination": destination, "options": normalized_options}


def _canonical_docker_tool_policy(policy: Any, *, require_paths_exist: bool) -> dict[str, Any]:
    """Normalize the exact policy whose hash is carried by a sandbox receipt."""

    if not isinstance(policy, dict):
        raise ValueError("docker_policy_object_required")
    allowed = {
        "image", "command", "project_root", "read_paths", "write_files", "timeout_seconds",
        "max_output_bytes", "memory", "cpus", "pids_limit", "user", "tmpfs", "daemon_identity",
        "network_mode", "read_only", "cap_drop", "no_new_privileges", "privileged", "devices",
    }
    if set(policy) - allowed:
        raise ValueError("docker_policy_unknown_field")
    image = _docker_safe_text(policy.get("image"), "docker_image_digest_required", reject_comma=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", image):
        raise ValueError("docker_image_digest_required")
    command = policy.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("docker_command_array_required")
    normalized_command = [_docker_safe_text(item, "docker_command_array_required") for item in command]

    project_value = _docker_safe_text(policy.get("project_root"), "docker_project_root_invalid", reject_comma=True)
    project_root = Path(project_value).expanduser().resolve()
    if (not project_root.is_absolute() or project_root == Path(project_root.anchor)
            or project_root == Path.home().resolve()
            or (require_paths_exist and not project_root.is_dir())):
        raise ValueError("docker_project_root_invalid")
    _docker_safe_text(str(project_root), "docker_project_root_invalid", reject_comma=True)

    def normalize_paths(field: str) -> list[str]:
        raw_paths = policy.get(field, [])
        if not isinstance(raw_paths, list) or not all(isinstance(item, str) and item for item in raw_paths):
            raise ValueError("docker_bound_paths_invalid")
        if len(raw_paths) > 64:
            raise ValueError("docker_mount_count_exceeded")
        normalized: list[str] = []
        for raw_path in raw_paths:
            _docker_safe_text(raw_path, "docker_mount_path_unsafe", reject_comma=True)
            path = Path(raw_path).expanduser().resolve()
            _docker_safe_text(str(path), "docker_mount_path_unsafe", reject_comma=True)
            try:
                relative = path.relative_to(project_root)
            except ValueError as exc:
                raise ValueError("docker_path_outside_project") from exc
            if relative == Path("."):
                raise ValueError("docker_broad_mount_denied")
            if require_paths_exist and not path.is_file():
                raise ValueError("docker_mount_source_not_regular_file")
            normalized.append(str(path))
        if len(normalized) != len(set(normalized)):
            raise ValueError("docker_duplicate_mount_denied")
        return sorted(normalized)

    reads = normalize_paths("read_paths")
    writes = normalize_paths("write_files")
    if set(reads) & set(writes):
        raise ValueError("docker_mount_mode_conflict")

    if isinstance(policy.get("timeout_seconds", 300), bool) or isinstance(policy.get("max_output_bytes", 65536), bool):
        raise ValueError("docker_execution_limit_invalid")
    try:
        timeout_seconds = int(policy.get("timeout_seconds", 300))
        max_output_bytes = int(policy.get("max_output_bytes", 65536))
    except (TypeError, ValueError) as exc:
        raise ValueError("docker_execution_limit_invalid") from exc
    if not 1 <= timeout_seconds <= 1800 or not 1024 <= max_output_bytes <= 1_000_000:
        raise ValueError("docker_execution_limit_invalid")
    limits = _canonical_docker_limits(
        policy.get("memory", "512m"), policy.get("cpus", "1"), policy.get("pids_limit", 128)
    )
    user = _docker_safe_text(policy.get("user", "65534:65534"), "docker_user_invalid", reject_comma=True)
    match = re.fullmatch(r"([1-9][0-9]{0,9}):([1-9][0-9]{0,9})", user)
    if not match or any(int(item) > 2**31 - 1 for item in match.groups()):
        raise ValueError("docker_user_invalid")
    if policy.get("network_mode", "none") != "none" or policy.get("read_only", True) is not True:
        raise ValueError("docker_policy_not_fail_closed")
    if policy.get("cap_drop", ["ALL"]) != ["ALL"] or policy.get("no_new_privileges", True) is not True:
        raise ValueError("docker_policy_privilege_boundary_invalid")
    if policy.get("privileged", False) is not False or policy.get("devices", []) != []:
        raise ValueError("docker_policy_device_boundary_invalid")
    return {
        "image": image,
        "command": normalized_command,
        "project_root": str(project_root),
        "read_paths": reads,
        "write_files": writes,
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "memory": limits["memory"],
        "cpus": limits["cpus"],
        "pids_limit": limits["pids_limit"],
        "user": user,
        "tmpfs": _canonical_docker_tmpfs(policy.get("tmpfs")),
        "daemon_identity": _canonical_docker_daemon_identity(policy.get("daemon_identity")),
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "privileged": False,
        "devices": [],
    }


def _docker_boundary_from_policy(policy: dict[str, Any]) -> dict[str, Any]:
    mounts = [
        {"source": path, "destination": path, "read_only": True}
        for path in policy["read_paths"]
    ] + [
        {"source": path, "destination": path, "read_only": False}
        for path in policy["write_files"]
    ]
    mounts.sort(key=lambda item: (item["destination"], item["source"], item["read_only"]))
    return {
        "image_digest": policy["image"],
        "read_only": True,
        "network_mode": "none",
        "mounts": mounts,
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "privileged": False,
        "devices": [],
        "limits": {"memory": policy["memory"], "cpus": policy["cpus"], "pids_limit": policy["pids_limit"]},
        "user": policy["user"],
        "tmpfs": deepcopy(policy["tmpfs"]),
        "daemon_identity": deepcopy(policy["daemon_identity"]),
    }


def _canonical_docker_observation(observed: Any) -> tuple[str, str, dict[str, Any]]:
    boundary_fields = {
        "image_digest", "read_only", "network_mode", "mounts", "cap_drop", "no_new_privileges",
        "limits", "user", "tmpfs", "daemon_identity", "privileged", "devices",
    }
    if not isinstance(observed, dict) or set(observed) != boundary_fields | {"container_id", "observation_source"}:
        raise ValueError("docker_policy_observation_incomplete")
    container_id = _docker_safe_text(observed.get("container_id"), "docker_container_identity_invalid", reject_comma=True)
    if len(container_id) < 8:
        raise ValueError("docker_container_identity_invalid")
    observation_source = _docker_safe_text(
        observed.get("observation_source"), "docker_policy_observation_source_invalid"
    )
    if observation_source != "host_docker_inspect":
        raise ValueError("docker_policy_observation_source_invalid")
    mounts = observed.get("mounts")
    if not isinstance(mounts, list):
        raise ValueError("docker_mount_observation_invalid")
    if len(mounts) > 128:
        raise ValueError("docker_mount_count_exceeded")
    canonical_mounts: list[dict[str, Any]] = []
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) != {"source", "destination", "read_only"}:
            raise ValueError("docker_mount_observation_invalid")
        source = _docker_safe_text(mount.get("source"), "docker_mount_observation_invalid", reject_comma=True)
        destination = _docker_safe_text(mount.get("destination"), "docker_mount_observation_invalid", reject_comma=True)
        if not Path(source).is_absolute() or not Path(destination).is_absolute() or not isinstance(mount.get("read_only"), bool):
            raise ValueError("docker_mount_observation_invalid")
        canonical_mounts.append({"source": source, "destination": destination, "read_only": mount["read_only"]})
    canonical_mounts.sort(key=lambda item: (item["destination"], item["source"], item["read_only"]))
    if len({(item["source"], item["destination"], item["read_only"]) for item in canonical_mounts}) != len(canonical_mounts):
        raise ValueError("docker_duplicate_mount_denied")
    if observed.get("read_only") is not True or observed.get("network_mode") != "none":
        raise ValueError("docker_policy_not_fail_closed")
    if observed.get("cap_drop") != ["ALL"] or observed.get("no_new_privileges") is not True:
        raise ValueError("docker_policy_privilege_boundary_invalid")
    if observed.get("privileged") is not False or observed.get("devices") != []:
        raise ValueError("docker_policy_device_boundary_invalid")
    image = _docker_safe_text(observed.get("image_digest"), "docker_image_digest_required", reject_comma=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", image):
        raise ValueError("docker_image_digest_required")
    limits_value = observed.get("limits")
    if not isinstance(limits_value, dict) or set(limits_value) != {"memory", "cpus", "pids_limit"}:
        raise ValueError("docker_limit_observation_invalid")
    limits = _canonical_docker_limits(limits_value["memory"], limits_value["cpus"], limits_value["pids_limit"])
    user = _docker_safe_text(observed.get("user"), "docker_user_invalid", reject_comma=True)
    if not re.fullmatch(r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}", user):
        raise ValueError("docker_user_invalid")
    return container_id, observation_source, {
        "image_digest": image,
        "read_only": True,
        "network_mode": "none",
        "mounts": canonical_mounts,
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "privileged": False,
        "devices": [],
        "limits": limits,
        "user": user,
        "tmpfs": _canonical_docker_tmpfs(observed.get("tmpfs")),
        "daemon_identity": _canonical_docker_daemon_identity(observed.get("daemon_identity")),
    }


def _validator_policy_sha256(task_id: str, result_sha256: str) -> str:
    return hashlib.sha256(_canonical({"task_id": task_id, "accepted_result_sha256": result_sha256})).hexdigest()


def validator_policy_sha256(task_id: str, result_sha256: str) -> str:
    """Return the supported validator-policy binding for host adapters."""

    return _validator_policy_sha256(task_id, result_sha256)


class HostLedger:
    """Append-only SQLite evidence ledger stored outside the immutable skill.

    The chain and triggers provide cooperative process integrity and make
    accidental or unprivileged in-process mutation evident.  They are not a
    security boundary against the database owner, who can replace the file or
    drop SQLite triggers.  Stronger attestation requires a separately trusted
    signer or storage principal.
    """

    def __init__(self, path: str | Path):
        self.path, ledger_fd = secure_private_file(path, create=True)
        os.close(ledger_fd)
        self.operation_lock_path = self.path.with_name(self.path.name + ".operation.lock")
        self._thread_operation_lock = threading.RLock()
        self._operation_lock_depth = 0
        self._operation_lock_fd: int | None = None
        self.db = sqlite3.connect(self.path)
        with self.operation_lock():
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self._initialize_schema()

    def _initialize_schema(self) -> None:
        """Create or safely migrate the ledger before installing immutability triggers."""

        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              run_id TEXT NOT NULL,
              task_id TEXT,
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,
              previous_event_sha256 TEXT NOT NULL,
              event_sha256 TEXT NOT NULL
            )
            """
            )

            # Removing our own triggers inside the migration transaction permits
            # a legacy backfill.  A failed migration rolls this DDL back too.
            self.db.execute("DROP TRIGGER IF EXISTS events_deny_update")
            self.db.execute("DROP TRIGGER IF EXISTS events_deny_delete")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
            if "previous_event_sha256" not in columns:
                self.db.execute("ALTER TABLE events ADD COLUMN previous_event_sha256 TEXT")
            if "event_sha256" not in columns:
                self.db.execute("ALTER TABLE events ADD COLUMN event_sha256 TEXT")

            previous_by_run: dict[str, str] = {}
            rows = self.db.execute(
                "SELECT sequence,event_id,run_id,task_id,event_type,occurred_at,payload_json,"
                "payload_sha256,previous_event_sha256,event_sha256 FROM events ORDER BY sequence"
            ).fetchall()
            for row in rows:
                (sequence, event_id, run_id, task_id, event_type, occurred_at, payload_json,
                 payload_sha256, stored_previous, stored_event_sha256) = row
                try:
                    payload = json.loads(payload_json, parse_constant=_reject_nonfinite_json_constant)
                except (TypeError, ValueError) as exc:
                    raise ValueError("ledger_migration_payload_json_invalid") from exc
                canonical_payload = _canonical(payload)
                expected_payload_sha256 = hashlib.sha256(canonical_payload).hexdigest()
                if (payload_json != canonical_payload.decode("utf-8")
                        or payload_sha256 != expected_payload_sha256):
                    raise ValueError("ledger_migration_payload_integrity_failed")
                expected_previous = previous_by_run.get(run_id, LEDGER_CHAIN_GENESIS_SHA256)
                expected_event_sha256 = _event_chain_sha256(
                    event_id=event_id,
                    run_id=run_id,
                    task_id=task_id,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    payload_sha256=payload_sha256,
                    previous_event_sha256=expected_previous,
                )
                if stored_previous is None and stored_event_sha256 is None:
                    self.db.execute(
                        "UPDATE events SET previous_event_sha256=?,event_sha256=? WHERE sequence=?",
                        (expected_previous, expected_event_sha256, sequence),
                    )
                elif (stored_previous != expected_previous
                      or stored_event_sha256 != expected_event_sha256):
                    raise ValueError("ledger_migration_event_chain_invalid")
                previous_by_run[run_id] = expected_event_sha256

            self.db.execute("CREATE INDEX IF NOT EXISTS events_run_type ON events(run_id, event_type, sequence)")
            self.db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS events_one_requeue_per_launch
                   ON events(run_id, task_id, json_extract(payload_json, '$.prior_launch_correlation_id'))
                   WHERE event_type = 'cancelled_task_requeued'"""
            )
            self.db.execute(
                """CREATE TRIGGER events_deny_update BEFORE UPDATE ON events
                   BEGIN SELECT RAISE(ABORT, 'events_append_only_update_denied'); END"""
            )
            self.db.execute(
                """CREATE TRIGGER events_deny_delete BEFORE DELETE ON events
                   BEGIN SELECT RAISE(ABORT, 'events_append_only_delete_denied'); END"""
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def operation_lock(self):
        """Crash-releasing cross-process lock for compound ledger/state decisions."""

        with self._thread_operation_lock:
            if self._operation_lock_depth == 0:
                _lock_path, fd = secure_private_file(self.operation_lock_path, create=True)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._operation_lock_fd = fd
            self._operation_lock_depth += 1
            try:
                yield
            finally:
                self._operation_lock_depth -= 1
                if self._operation_lock_depth == 0:
                    fd = self._operation_lock_fd
                    self._operation_lock_fd = None
                    if fd is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)

    def append(self, run_id: str, event_type: str, payload: dict[str, Any], task_id: str | None = None) -> str:
        with self.operation_lock():
            body = deepcopy(payload)
            # Raw result text is useful for validation, but a provider can
            # accidentally echo a credential from its bounded input.  Screen at
            # the final persistence boundary even if upstream validation was
            # already performed.  The rejection never includes packet content.
            if event_type in {"accepted_result_packet", "validator_result"} and contains_sensitive_material(body):
                raise ValueError("result_packet_sensitive_material")
            encoded = _canonical(body)
            payload_sha256 = hashlib.sha256(encoded).hexdigest()
            event_id = _id("event")
            occurred_at = _now()
            try:
                self.db.execute("BEGIN IMMEDIATE")
                prior = self._verified_event_rows(run_id)
                previous_event_sha256 = (
                    prior[-1]["event_sha256"] if prior else LEDGER_CHAIN_GENESIS_SHA256
                )
                event_sha256 = _event_chain_sha256(
                    event_id=event_id,
                    run_id=run_id,
                    task_id=task_id,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    payload_sha256=payload_sha256,
                    previous_event_sha256=previous_event_sha256,
                )
                self.db.execute(
                    "INSERT INTO events(event_id,run_id,task_id,event_type,occurred_at,payload_json,"
                    "payload_sha256,previous_event_sha256,event_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, run_id, task_id, event_type, occurred_at, encoded.decode("utf-8"),
                     payload_sha256, previous_event_sha256, event_sha256),
                )
                self.db.commit()
                return event_id
            except Exception:
                self.db.rollback()
                raise

    def _verified_event_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT event_id,run_id,task_id,event_type,occurred_at,payload_json,payload_sha256,"
            "previous_event_sha256,event_sha256 FROM events WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        verified: list[dict[str, Any]] = []
        expected_previous = LEDGER_CHAIN_GENESIS_SHA256
        for row in rows:
            (event_id, stored_run_id, task_id, event_type, occurred_at, payload_json,
             payload_sha256, previous_event_sha256, event_sha256) = row
            try:
                payload = json.loads(payload_json, parse_constant=_reject_nonfinite_json_constant)
            except (TypeError, ValueError) as exc:
                raise ValueError("ledger_payload_json_invalid") from exc
            canonical_payload = _canonical(payload)
            if (payload_json != canonical_payload.decode("utf-8")
                    or hashlib.sha256(canonical_payload).hexdigest() != payload_sha256):
                raise ValueError("ledger_payload_integrity_failed")
            if previous_event_sha256 != expected_previous:
                raise ValueError("ledger_previous_event_chain_invalid")
            expected_event_sha256 = _event_chain_sha256(
                event_id=event_id,
                run_id=stored_run_id,
                task_id=task_id,
                event_type=event_type,
                occurred_at=occurred_at,
                payload_sha256=payload_sha256,
                previous_event_sha256=previous_event_sha256,
            )
            if event_sha256 != expected_event_sha256:
                raise ValueError("ledger_event_chain_invalid")
            verified.append({
                "event_id": event_id,
                "task_id": task_id,
                "event_type": event_type,
                "occurred_at": occurred_at,
                "payload": payload,
                "payload_sha256": payload_sha256,
                "previous_event_sha256": previous_event_sha256,
                "event_sha256": event_sha256,
            })
            expected_previous = event_sha256
        return verified

    def events(self, run_id: str, event_type: str | None = None) -> list[dict[str, Any]]:
        with self.operation_lock():
            rows = self._verified_event_rows(run_id)
            if event_type is not None:
                rows = [row for row in rows if row["event_type"] == event_type]
            return rows

    def latest_task_event(self, run_id: str, task_id: str, event_type: str) -> dict[str, Any] | None:
        for event in reversed(self.events(run_id, event_type)):
            if event["task_id"] == task_id:
                return event["payload"]
        return None

    def latest_effect(self, run_id: str, task_id: str, effect_id: str) -> dict[str, Any] | None:
        for event in reversed(self.events(run_id, "effect_record")):
            if event["task_id"] == task_id and event["payload"].get("effect_id") == effect_id:
                return event["payload"]
        return None


def verify_local_launch_attestation(receipt: dict[str, Any], public_key_b64: str, expected: dict[str, Any]) -> None:
    if Ed25519PublicKey is None:
        raise ValueError("ed25519_runtime_dependency_unavailable")
    if receipt.get("schema") != "LocalLaunchAttestationV2" or receipt.get("algorithm") != "Ed25519":
        raise ValueError("local_launch_attestation_schema_invalid")
    signature = receipt.get("signature")
    signed = receipt.get("signed")
    if not isinstance(signature, str) or not isinstance(signed, dict):
        raise ValueError("local_launch_attestation_shape_invalid")
    for key, value in expected.items():
        if signed.get(key) != value:
            raise ValueError(f"local_launch_attestation_mismatch:{key}")
    now = datetime.now(timezone.utc)
    try:
        issued = datetime.fromisoformat(str(signed["issued_at"]).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(signed["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ValueError("local_launch_attestation_time_invalid") from exc
    if issued > now or expires <= now or (expires - issued).total_seconds() > 900:
        raise ValueError("local_launch_attestation_stale")
    if not isinstance(signed.get("nonce"), str) or len(signed["nonce"]) < 16:
        raise ValueError("local_launch_attestation_nonce_invalid")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature, validate=True), _canonical(signed))
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("local_launch_attestation_signature_invalid") from exc


def _ledger_operation(method):
    """Serialize a compound dispatcher decision with every ledger append."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.ledger.operation_lock():
            return method(self, *args, **kwargs)
    return locked


class HostDispatcher:
    """Mandatory host wrapper around the existing lifecycle controller."""

    def __init__(
        self,
        ledger: HostLedger,
        local_public_key_b64: str | None = None,
        require_capability: bool = True,
        telemetry_sink: Any = None,
        local_sandbox_public_key_b64: str | None = None,
        termination_callback: Callable[[dict[str, Any]], Any] | None = None,
        require_adapter_identity: bool = False,
        host_manifest_sha256: str | None = None,
        adapter_manifests: dict[str, dict[str, Any]] | None = None,
    ):
        self.ledger = ledger
        self.local_public_key_b64 = local_public_key_b64
        self.local_sandbox_public_key_b64 = local_sandbox_public_key_b64 or local_public_key_b64
        self.coordinator_boundary_verifier = None
        self.require_capability = require_capability
        self.telemetry_sink = telemetry_sink
        self.termination_callback = termination_callback
        self.require_adapter_identity = bool(require_adapter_identity)
        self.host_manifest_sha256 = host_manifest_sha256
        self.adapter_manifests = deepcopy(adapter_manifests or {})
        if self.require_adapter_identity:
            if not _is_sha256(self.host_manifest_sha256) or not self.adapter_manifests:
                raise ValueError("trusted_adapter_manifest_required")

    def _emit(self, event_type: str, fields: dict[str, Any]) -> None:
        if self.telemetry_sink is None:
            return
        from .telemetry import sanitize_event
        try:
            self.telemetry_sink.emit(sanitize_event(event_type, fields))
        except Exception as exc:
            run_id = fields.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                return
            try:
                field_digest = hashlib.sha256(_canonical(fields)).hexdigest()
            except Exception:
                field_digest = hashlib.sha256(type(fields).__name__.encode()).hexdigest()
            try:
                self.ledger.append(run_id, "telemetry_delivery_failed", {
                    "attempted_event_type": event_type,
                    "attempted_fields_sha256": field_digest,
                    "failure_class": type(exc).__name__,
                    "failure_sha256": hashlib.sha256(str(exc).encode("utf-8", errors="surrogatepass")).hexdigest(),
                    "failed_at": _now(),
                    "control_plane_effect": "none",
                }, fields.get("task_id") if isinstance(fields.get("task_id"), str) else None)
            except Exception:
                # Telemetry is diagnostic. A sink or diagnostic-ledger failure
                # must not convert an already accepted control-plane mutation
                # into a false caller-visible operation failure.
                return

    @staticmethod
    def _tasks(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {task["task_id"]: task for task in state["tasks"]}

    def freeze_providers(self, state: dict[str, Any], providers: list[str]) -> None:
        validate_run_state(state)
        normalized = sorted({provider.strip() for provider in providers if isinstance(provider, str) and provider.strip()})
        if not normalized:
            raise ValueError("approved_provider_set_required")
        existing = self.ledger.events(state["run_id"], "approved_providers")
        if existing and existing[-1]["payload"].get("providers") != normalized:
            raise ValueError("approved_provider_set_immutable")
        if not existing:
            self.ledger.append(state["run_id"], "approved_providers", {"providers": normalized})

    def record_capability(self, state: dict[str, Any], capability: dict[str, Any]) -> None:
        validate_run_state(state)
        if capability.get("schema") != "CapabilityRecordV1" or capability.get("status") != "approved":
            raise ValueError("capability_record_not_approved")
        required = {"provider", "exact_model_id", "reasoning_effort", "task_class", "tool_policy_sha256",
                    "evaluation_version", "quality_confidence", "sample_size", "context_window", "price_card_version",
                    "observed_at", "expires_at", "evidence_class", "p50_latency_ms", "p95_latency_ms",
                    "failure_rate", "validator_failure_rate"}
        if any(capability.get(field) in (None, "") for field in required):
            raise ValueError("capability_record_required_fields_invalid")
        if capability.get("reasoning_effort") not in {"low", "medium", "high", "xhigh", "ultra"}:
            raise ValueError("capability_record_reasoning_effort_invalid")
        if not _is_sha256(capability.get("tool_policy_sha256")):
            raise ValueError("capability_record_policy_sha256_invalid")
        evidence_class = capability.get("evidence_class")
        if evidence_class not in {"measured_capability", "launch_readiness"}:
            raise ValueError("capability_record_evidence_class_invalid")
        quality = capability.get("quality_confidence")
        sample_size = capability.get("sample_size")
        p50, p95 = capability.get("p50_latency_ms"), capability.get("p95_latency_ms")
        failure_rate, validator_failure_rate = capability.get("failure_rate"), capability.get("validator_failure_rate")
        context_window = capability.get("context_window")
        if (
            isinstance(quality, bool) or not isinstance(quality, (int, float))
            or not math.isfinite(float(quality)) or not 0 <= quality <= 1
            or isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 0
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value < 0
                for value in (p50, p95)
            )
            or p95 < p50
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not 0 <= value <= 1
                for value in (failure_rate, validator_failure_rate)
            )
            or isinstance(context_window, bool) or not isinstance(context_window, int) or context_window < 1
        ):
            raise ValueError("capability_record_metrics_invalid")
        if evidence_class == "launch_readiness" and (sample_size != 0 or float(quality) != 0.0):
            raise ValueError("launch_readiness_must_not_claim_quality")
        if evidence_class == "measured_capability" and sample_size < 1:
            raise ValueError("measured_capability_sample_required")
        if self.require_adapter_identity:
            identity = capability.get("adapter_identity")
            declared = self.adapter_manifests.get(capability.get("provider"))
            if (
                capability.get("host_manifest_sha256") != self.host_manifest_sha256
                or capability.get("project_binding_sha256") != state.get("project_binding", {}).get("root_sha256")
                or not isinstance(identity, dict) or not isinstance(declared, dict)
                or identity.get("adapter_id") != declared.get("adapter_id")
                or identity.get("adapter_version") != declared.get("version")
                or set(identity.get("input_modes", [])) != set(declared.get("input_modes", []))
                or not _is_sha256(identity.get("executable_sha256"))
                or capability.get("adapter_id") != identity.get("adapter_id")
                or capability.get("adapter_version") != identity.get("adapter_version")
                or capability.get("executable_sha256") != identity.get("executable_sha256")
            ):
                raise ValueError("capability_adapter_identity_binding_invalid")
        observed = _parse_time(capability["observed_at"], "capability_record_observed_at_invalid")
        expires = _parse_time(capability["expires_at"], "capability_record_expiry_invalid")
        now = datetime.now(timezone.utc)
        if observed > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            raise ValueError("capability_record_observed_at_future")
        if expires <= now:
            raise ValueError("capability_record_expired")
        if expires <= observed or (expires - observed).total_seconds() > MAX_CAPABILITY_LIFETIME_SECONDS:
            raise ValueError("capability_record_lifetime_invalid")
        self.ledger.append(state["run_id"], "capability_record", capability)

    def _authorize_launch_capability(self, state: dict[str, Any], task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any] | None:
        if not self.require_capability:
            return None
        approved = self.ledger.events(state["run_id"], "approved_providers")
        if not approved or receipt["provider"] not in approved[-1]["payload"].get("providers", []):
            raise ValueError("launch_provider_not_approved")
        now = datetime.now(timezone.utc)
        receipt_digest = receipt.get("capability_digest")
        if not _is_sha256(receipt_digest):
            raise ValueError("launch_capability_digest_required")
        for event in reversed(self.ledger.events(state["run_id"], "capability_record")):
            capability = event["payload"]
            try:
                observed = _parse_time(capability["observed_at"], "capability_record_observed_at_invalid")
                expires = _parse_time(capability["expires_at"], "capability_record_expiry_invalid")
                fresh = (
                    observed <= now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
                    and expires > now
                    and expires > observed
                    and (expires - observed).total_seconds() <= MAX_CAPABILITY_LIFETIME_SECONDS
                )
            except ValueError:
                fresh = False
            identity_matches = all(
                capability.get(capability_field) == receipt.get(receipt_field)
                for receipt_field, capability_field in (
                    ("host_manifest_sha256", "host_manifest_sha256"),
                    ("project_binding_sha256", "project_binding_sha256"),
                    ("adapter_identity", "adapter_identity"),
                )
            )
            admission_matches = receipt.get("admission_basis") == (
                "explicit_provider_launch_readiness"
                if capability.get("evidence_class") == "launch_readiness"
                else "measured_capability"
            )
            if (fresh and capability.get("status") == "approved" and capability.get("provider") == receipt["provider"]
                    and capability.get("exact_model_id") == receipt["resolved_model"]
                    and capability.get("reasoning_effort") == receipt["reasoning_effort"]
                    and capability.get("task_class") in {task["task_class"], "*"}
                    and capability.get("tool_policy_sha256") == receipt.get("tool_policy_sha256")
                    and (identity_matches if self.require_adapter_identity else all(
                        receipt.get(receipt_field) is None or capability.get(capability_field) == receipt.get(receipt_field)
                        for receipt_field, capability_field in (
                            ("host_manifest_sha256", "host_manifest_sha256"),
                            ("project_binding_sha256", "project_binding_sha256"),
                            ("adapter_identity", "adapter_identity"),
                        )
                    ))
                    and admission_matches
                    and event["payload_sha256"] == receipt_digest):
                return event
        raise ValueError("fresh_matching_capability_required")

    def _verified_launch_receipt(self, state: dict[str, Any], task_id: str, correlation: str) -> dict[str, Any]:
        """Return the unique host-recorded receipt for this attempted launch."""

        matches = [
            event["payload"]
            for event in self.ledger.events(state["run_id"], "launch_receipt")
            if event["task_id"] == task_id
            and event["payload"].get("launch_correlation_id") == correlation
        ]
        if len(matches) != 1:
            raise ValueError("verified_launch_receipt_required")
        return matches[0]

    def _verify_local_no_tool_boundary(
        self,
        state: dict[str, Any],
        task: dict[str, Any],
        receipt: dict[str, Any],
        capability_event: dict[str, Any] | None,
    ) -> None:
        """Verify the trusted bounded-file Local data-client protocol.

        This is deliberately separate from ``SandboxReceiptV1``.  It grants no
        model-side tools, filesystem, shell, credentials, or project mount, so
        there is no delegated tool execution to sandbox.  The adapter itself
        must still be pinned by a runner-enforced host manifest.
        """

        from .provider_adapters import validate_local_host_evidence

        if not self.require_adapter_identity or not isinstance(capability_event, dict):
            raise ValueError("local_no_tool_trusted_adapter_required")
        boundary = receipt.get("local_no_tool_boundary")
        capability_attestation = receipt.get("local_capability_attestation")
        bridge_observation = receipt.get("local_bridge_observation")
        required = {
            "schema", "endpoint", "endpoint_sha256", "endpoint_policy",
            "allow_loopback_http", "selector_alias", "exact_model_id",
            "reasoning_effort", "required_mode", "auth_mode", "input_mode",
            "tools_enabled", "project_root_mounted", "network_scope",
            "boundary_status", "capability_attestation_sha256",
            "bridge_observation_sha256",
        }
        if not isinstance(boundary, dict) or set(boundary) != required:
            raise ValueError("local_no_tool_boundary_shape_invalid")
        if (
            boundary.get("schema") != "LocalNoToolBoundaryV1"
            or boundary.get("auth_mode") != "none"
            or boundary.get("input_mode") != "bounded_file_text"
            or boundary.get("tools_enabled") is not False
            or boundary.get("project_root_mounted") is not False
            or boundary.get("network_scope") != "single_exact_endpoint"
            or boundary.get("boundary_status") != "host_adapter_no_tool_protocol"
            or not isinstance(boundary.get("allow_loopback_http"), bool)
        ):
            raise ValueError("local_no_tool_boundary_not_enforced")
        if (
            receipt.get("tool_mode") != "no_tools"
            or receipt.get("input_mode") != "bounded_file_text"
            or boundary.get("selector_alias") != receipt.get("model_selector")
            or boundary.get("exact_model_id") != receipt.get("resolved_model")
            or boundary.get("reasoning_effort") != receipt.get("reasoning_effort")
        ):
            raise ValueError("local_no_tool_launch_binding_invalid")
        validated = validate_local_host_evidence(
            capability_attestation,
            bridge_observation,
            selector=receipt["model_selector"],
            exact_model_id=receipt["resolved_model"],
            endpoint=boundary["endpoint"],
            task_class=task["task_class"],
            reasoning_effort=receipt["reasoning_effort"],
            required_mode=boundary["required_mode"],
            allow_loopback_http=boundary["allow_loopback_http"],
        )
        capability = capability_event.get("payload")
        if not isinstance(capability, dict):
            raise ValueError("local_no_tool_capability_required")
        endpoint_sha256 = hashlib.sha256(validated["endpoint"].encode()).hexdigest()
        expected = {
            "endpoint_sha256": endpoint_sha256,
            "endpoint_policy": validated["endpoint_policy"],
            "capability_attestation_sha256": validated["capability_attestation_sha256"],
            "bridge_observation_sha256": validated["bridge_observation_sha256"],
        }
        if any(boundary.get(key) != value for key, value in expected.items()):
            raise ValueError("local_no_tool_evidence_hash_mismatch")
        capability_expected = {
            "endpoint_sha256": endpoint_sha256,
            "endpoint_policy": validated["endpoint_policy"],
            "auth_mode": "none",
            "local_capability_attestation_sha256": validated["capability_attestation_sha256"],
            "local_bridge_observation_sha256": validated["bridge_observation_sha256"],
        }
        if any(capability.get(key) != value for key, value in capability_expected.items()):
            raise ValueError("local_no_tool_capability_binding_invalid")
        self.ledger.append(state["run_id"], "local_no_tool_boundary", {
            "launch_correlation_id": receipt["launch_correlation_id"],
            "endpoint_sha256": endpoint_sha256,
            "capability_attestation_sha256": validated["capability_attestation_sha256"],
            "bridge_observation_sha256": validated["bridge_observation_sha256"],
            "endpoint_policy": validated["endpoint_policy"],
            "auth_mode": "none", "tools_enabled": False,
            "project_root_mounted": False,
            "phase": receipt.get("boundary_phase", "worker_launch"),
        }, task["task_id"])

    @_ledger_operation
    def record_local_no_tool_validator_preflight(
        self,
        state: dict[str, Any],
        task_id: str,
        receipt: dict[str, Any],
        capability_digest: str,
    ) -> None:
        """Freeze Local validator endpoint evidence before its network call."""

        validate_run_state(state)
        task = self._tasks(state).get(task_id)
        if not task or task.get("status") != "completed" or not _is_sha256(capability_digest):
            raise ValueError("local_validator_preflight_binding_invalid")
        capability = next(
            (
                event for event in reversed(self.ledger.events(state["run_id"], "capability_record"))
                if event["payload_sha256"] == capability_digest
            ),
            None,
        )
        if not capability or capability["payload"].get("provider") != "Local":
            raise ValueError("local_validator_preflight_capability_required")
        self._verify_local_no_tool_boundary(
            state, task, {**receipt, "boundary_phase": "validator_preflight"}, capability,
        )

    def _verify_local_sandbox_receipt(self, state: dict[str, Any], receipt: dict[str, Any]) -> None:
        sandbox = receipt.get("sandbox_receipt")
        if not self.local_sandbox_public_key_b64 or not isinstance(sandbox, dict) or sandbox.get("schema") != "SandboxReceiptV1" or sandbox.get("algorithm") != "Ed25519":
            raise ValueError("local_sandbox_receipt_required")
        signed, signature = sandbox.get("signed"), sandbox.get("signature")
        if not isinstance(signed, dict) or not isinstance(signature, str):
            raise ValueError("local_sandbox_receipt_shape_invalid")
        if Ed25519PublicKey is None:
            raise ValueError("ed25519_runtime_dependency_unavailable")
        try:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(self.local_sandbox_public_key_b64, validate=True))
            key.verify(base64.b64decode(signature, validate=True), _canonical(signed))
        except (ValueError, InvalidSignature) as exc:
            raise ValueError("local_sandbox_receipt_signature_invalid") from exc
        expected = {
            "run_id": state["run_id"], "task_id": receipt["task_id"],
            "launch_correlation_id": receipt["launch_correlation_id"],
            "tool_policy_sha256": receipt.get("tool_policy_sha256"),
        }
        if any(signed.get(key) != value for key, value in expected.items()):
            raise ValueError("local_sandbox_receipt_binding_invalid")
        if (not _is_sha256(signed.get("tool_policy_sha256"))
                or not _is_sha256(signed.get("declared_policy_sha256"))
                or not _is_sha256(signed.get("observation_sha256"))):
            raise ValueError("local_sandbox_receipt_hash_invalid")
        if signed.get("tool_policy_sha256") != receipt.get("tool_policy_sha256"):
            raise ValueError("local_sandbox_policy_mismatch")

        tool_policy = _canonical_docker_tool_policy(signed.get("tool_policy"), require_paths_exist=True)
        if signed.get("tool_policy") != tool_policy:
            raise ValueError("local_sandbox_tool_policy_not_canonical")
        if hashlib.sha256(_canonical(tool_policy)).hexdigest() != signed["tool_policy_sha256"]:
            raise ValueError("local_sandbox_policy_hash_invalid")
        expected_boundary = _docker_boundary_from_policy(tool_policy)
        declared = signed.get("declared_policy")
        if not isinstance(declared, dict):
            raise ValueError("local_sandbox_declared_policy_invalid")
        try:
            _container, _source, canonical_declared = _canonical_docker_observation({
                **declared,
                "container_id": signed.get("container_id"),
                "observation_source": signed.get("observation_source"),
            })
        except ValueError as exc:
            raise ValueError("local_sandbox_declared_policy_invalid") from exc
        if declared != canonical_declared or declared != expected_boundary:
            raise ValueError("local_sandbox_declared_policy_mismatch")
        if hashlib.sha256(_canonical(declared)).hexdigest() != signed["declared_policy_sha256"]:
            raise ValueError("local_sandbox_declared_policy_hash_invalid")
        observed = signed.get("observed_policy")
        try:
            container_id, observation_source, canonical_observed = _canonical_docker_observation({
                **(observed if isinstance(observed, dict) else {}),
                "container_id": signed.get("container_id"),
                "observation_source": signed.get("observation_source"),
            })
        except ValueError as exc:
            raise ValueError("local_sandbox_observed_policy_invalid") from exc
        if observed != canonical_observed or observed != declared:
            raise ValueError("local_sandbox_observed_policy_mismatch")
        observation_identity = {
            "container_id": container_id,
            "observation_source": observation_source,
            "observed_policy": canonical_observed,
        }
        if hashlib.sha256(_canonical(observation_identity)).hexdigest() != signed["observation_sha256"]:
            raise ValueError("local_sandbox_observation_hash_invalid")
        if signed.get("boundary_status") != DOCKER_SANDBOX_ENFORCED_STATUS:
            raise ValueError("local_sandbox_receipt_not_enforced")

        verified_at = _parse_time(signed.get("verified_at"), "local_sandbox_receipt_time_invalid")
        expires_at = _parse_time(signed.get("expires_at"), "local_sandbox_receipt_time_invalid")
        now = datetime.now(timezone.utc)
        if (verified_at > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS) or expires_at <= now
                or expires_at <= verified_at or (expires_at - verified_at).total_seconds() > MAX_LAUNCH_LEASE_SECONDS):
            raise ValueError("local_sandbox_receipt_stale")
        image = signed.get("image_digest")
        if image != expected_boundary["image_digest"]:
            raise ValueError("local_sandbox_image_digest_invalid")
        nonce = signed.get("nonce")
        if not isinstance(container_id, str) or len(container_id) < 8 or not isinstance(nonce, str) or len(nonce) < 16:
            raise ValueError("local_sandbox_identity_invalid")
        nonce_sha = hashlib.sha256(nonce.encode()).hexdigest()
        if any(event["payload"].get("nonce_sha256") == nonce_sha for event in self.ledger.events(state["run_id"], "local_sandbox_receipt")):
            raise ValueError("local_sandbox_receipt_replay")
        self.ledger.append(state["run_id"], "local_sandbox_receipt", {
            "nonce_sha256": nonce_sha, "container_id": container_id,
            "signed_sha256": hashlib.sha256(_canonical(signed)).hexdigest(),
        }, receipt["task_id"])

    def _enforce_launch_liveness(self, state: dict[str, Any], now: datetime | None = None) -> None:
        """Cancel expired or heartbeat-stale launches before accepting new work."""

        if not self.require_capability:
            return
        current = now or datetime.now(timezone.utc)
        for task in list(self._tasks(state).values()):
            if task.get("status") not in {"dispatched", "active"}:
                continue
            correlation = task.get("launch_correlation_id")
            if not isinstance(correlation, str):
                continue
            try:
                receipt = self._verified_launch_receipt(state, task["task_id"], correlation)
                started = _parse_time(receipt.get("started_at"), "launch_started_at_invalid")
                lease = _parse_time(receipt.get("lease_expires_at"), "launch_lease_required")
                interval = int(receipt.get("heartbeat_interval_seconds", 0))
            except (ValueError, TypeError):
                self.cancel_launch(state, task["task_id"], "launch_receipt_invalid")
                continue
            if lease <= current:
                self.cancel_launch(state, task["task_id"], "launch_lease_expired")
                continue
            beats = self.ledger.events(state["run_id"], "launch_heartbeat")
            latest = next((event["payload"] for event in reversed(beats) if event["task_id"] == task["task_id"]
                           and event["payload"].get("launch_correlation_id") == correlation), None)
            reference = _parse_time(latest["observed_at"], "heartbeat_timestamp_invalid") if latest else started
            if interval <= 0 or (current - reference).total_seconds() > interval:
                self.cancel_launch(state, task["task_id"], "launch_heartbeat_stale")

    def verify_coordinator_boundary(self, state, task, boundary, capability, role):
        # Only the profiled host installs this verifier. Neither an adapter nor a
        # legacy Local packet may opt itself into the coordinator contract.
        if not callable(self.coordinator_boundary_verifier):
            raise ValueError("coordinator_profiled_host_verifier_required")
        self.coordinator_boundary_verifier(state, task, boundary, capability, role)
        if not self.ledger.events(state["run_id"], "profiled_session_guard"):
            raise ValueError("coordinator_profiled_session_guard_required")

    def record_launch(self, state: dict[str, Any], receipt: dict[str, Any]) -> None:
        validate_run_state(state)
        if receipt.get("schema") != "LaunchReceiptV2":
            raise ValueError("launch_receipt_schema_invalid")
        required = {"run_id", "task_id", "launch_correlation_id", "provider", "provider_profile", "model_selector",
                    "resolved_model", "reasoning_effort", "worker_handle", "started_at", "termination_token"}
        if any(not receipt.get(field) for field in required) or receipt["run_id"] != state["run_id"]:
            raise ValueError("launch_receipt_required_fields_invalid")
        if not isinstance(receipt["termination_token"], str) or len(receipt["termination_token"]) < 16:
            raise ValueError("termination_token_invalid")
        adapter_identity = receipt.get("adapter_identity")
        if self.require_adapter_identity and adapter_identity is None:
            raise ValueError("launch_adapter_identity_required")
        if adapter_identity is not None:
            declared = self.adapter_manifests.get(receipt.get("provider")) if self.adapter_manifests else None
            if (
                not _is_sha256(receipt.get("host_manifest_sha256"))
                or not _is_sha256(receipt.get("project_binding_sha256"))
                or receipt.get("project_binding_sha256") != state.get("project_binding", {}).get("root_sha256")
                or not isinstance(adapter_identity, dict)
                or not all(isinstance(adapter_identity.get(field), str) and adapter_identity[field] for field in (
                    "adapter_id", "adapter_version", "executable_realpath", "executable_sha256",
                ))
                or not _is_sha256(adapter_identity.get("executable_sha256"))
                or receipt.get("input_mode") not in adapter_identity.get("input_modes", [])
                or receipt.get("tool_mode") != "no_tools"
                or (self.require_adapter_identity and receipt.get("host_manifest_sha256") != self.host_manifest_sha256)
                or (self.require_adapter_identity and not isinstance(declared, dict))
                or (self.require_adapter_identity and adapter_identity.get("adapter_id") != declared.get("adapter_id"))
                or (self.require_adapter_identity and adapter_identity.get("adapter_version") != declared.get("version"))
                or (self.require_adapter_identity and set(adapter_identity.get("input_modes", [])) != set(declared.get("input_modes", [])))
            ):
                raise ValueError("launch_adapter_identity_binding_invalid")
        now = datetime.now(timezone.utc)
        started = _parse_time(receipt.get("started_at"), "launch_started_at_invalid")
        if started > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            raise ValueError("launch_started_at_future")
        if self.require_capability:
            lease = _parse_time(receipt.get("lease_expires_at"), "launch_lease_required")
            if lease <= now:
                raise ValueError("launch_lease_expired")
            if lease <= started or (lease - started).total_seconds() > MAX_LAUNCH_LEASE_SECONDS:
                raise ValueError("launch_lease_lifetime_invalid")
            try:
                interval = int(receipt["heartbeat_interval_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("launch_heartbeat_interval_required") from exc
            if interval < 1 or interval > MAX_LAUNCH_LEASE_SECONDS:
                raise ValueError("launch_heartbeat_interval_invalid")
        task = self._tasks(state).get(receipt["task_id"])
        record = state["launch_records"].get(receipt["launch_correlation_id"])
        if not task or not record or record.get("task_id") != task["task_id"]:
            raise ValueError("launch_receipt_correlation_invalid")
        if receipt["worker_handle"] != record.get("worker_handle") or receipt["reasoning_effort"] != record.get("reasoning_effort"):
            raise ValueError("launch_receipt_host_binding_invalid")
        planned = record.get("provider_model", {})
        if (receipt.get("provider") != planned.get("provider")
                or receipt.get("provider_profile") != planned.get("profile")
                or receipt.get("model_selector") != planned.get("selector")):
            raise ValueError("launch_receipt_task_provider_binding_invalid")
        if any(event["task_id"] == task["task_id"] and event["payload"].get("launch_correlation_id") == receipt["launch_correlation_id"]
               for event in self.ledger.events(state["run_id"], "launch_receipt")):
            raise ValueError("launch_receipt_duplicate")
        capability_event = self._authorize_launch_capability(state, task, receipt)
        if str(receipt["provider"]).lower() == "local":
            if receipt.get("coordinator_hosted_boundary") is not None:
                if any(receipt.get(k) is not None for k in ("local_no_tool_boundary", "local_attestation", "sandbox_receipt")):
                    raise ValueError("coordinator_legacy_boundary_conflict")
                self.verify_coordinator_boundary(state, task, receipt["coordinator_hosted_boundary"], capability_event["payload"] if capability_event else {}, "worker")
            elif receipt.get("local_no_tool_boundary") is not None:
                self._verify_local_no_tool_boundary(
                    state, task, receipt, capability_event,
                )
            else:
                attestation = receipt.get("local_attestation")
                policy_sha = receipt.get("tool_policy_sha256")
                if not self.local_public_key_b64 or not isinstance(attestation, dict) or not policy_sha:
                    raise ValueError("local_launch_signed_attestation_required")
                self._verify_local_sandbox_receipt(state, receipt)
                expected = {"run_id": state["run_id"], "task_id": task["task_id"],
                            "launch_correlation_id": receipt["launch_correlation_id"],
                            "exact_model_id": receipt["resolved_model"], "tool_policy_sha256": policy_sha}
                verify_local_launch_attestation(attestation, self.local_public_key_b64, expected)
                nonce_sha = hashlib.sha256(attestation["signed"]["nonce"].encode()).hexdigest()
                if any(event["payload"].get("nonce_sha256") == nonce_sha for event in self.ledger.events(state["run_id"], "local_attestation")):
                    raise ValueError("local_launch_attestation_replay")
                self.ledger.append(state["run_id"], "local_attestation", {"nonce_sha256": nonce_sha,
                                   "attestation_sha256": hashlib.sha256(_canonical(attestation["signed"])).hexdigest()}, task["task_id"])
        # Once a host receipt exists, controller completion waits for the
        # ledger-backed validator outcome rather than racing to a terminal state.
        state["host_validation_required"] = True
        self.ledger.append(state["run_id"], "launch_receipt", receipt, task["task_id"])
        self._emit("launch", {"run_id": state["run_id"], "task_id": task["task_id"], "provider": receipt["provider"],
                              "model": receipt["resolved_model"], "reasoning_effort": receipt["reasoning_effort"], "status": "started"})

    def record_heartbeat(self, state: dict[str, Any], task_id: str, launch_correlation_id: str, observed_at: str) -> None:
        validate_run_state(state)
        task = self._tasks(state).get(task_id)
        record = state["launch_records"].get(launch_correlation_id)
        if not task or not record or record.get("task_id") != task_id or record.get("status") != "active":
            raise ValueError("heartbeat_launch_not_active")
        observed = _parse_time(observed_at, "heartbeat_timestamp_invalid")
        receipt = self._verified_launch_receipt(state, task_id, launch_correlation_id)
        started = _parse_time(receipt.get("started_at"), "launch_started_at_invalid")
        lease_value = receipt.get("lease_expires_at")
        now = datetime.now(timezone.utc)
        if observed > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            raise ValueError("heartbeat_timestamp_future")
        if observed < started:
            raise ValueError("heartbeat_before_launch")
        if lease_value is not None and observed > _parse_time(lease_value, "launch_lease_required"):
            raise ValueError("heartbeat_after_lease")
        for event in reversed(self.ledger.events(state["run_id"], "launch_heartbeat")):
            if event["task_id"] == task_id and event["payload"].get("launch_correlation_id") == launch_correlation_id:
                previous = _parse_time(event["payload"].get("observed_at"), "heartbeat_timestamp_invalid")
                if observed <= previous:
                    raise ValueError("heartbeat_not_monotonic")
                break
        self.ledger.append(state["run_id"], "launch_heartbeat", {"launch_correlation_id": launch_correlation_id,
                           "observed_at": observed_at}, task_id)

    def cancel_launch(self, state: dict[str, Any], task_id: str, reason: str) -> None:
        validate_run_state(state)
        task = self._tasks(state).get(task_id)
        if not task or task.get("status") not in {"dispatched", "active"} or not reason.strip():
            raise ValueError("cancellation_task_not_active")
        correlation = task.get("launch_correlation_id")
        record = state["launch_records"].get(correlation)
        if not record or record.get("status") != "active":
            raise ValueError("cancellation_launch_not_active")
        try:
            receipt = self._verified_launch_receipt(state, task_id, correlation)
        except ValueError:
            # Planning can fail before a process exists (for example no adapter
            # is installed). That is a pre-launch block, not a termination.
            receipt = None
        termination_token = receipt.get("termination_token") if receipt else None
        if termination_token is not None and (not isinstance(termination_token, str) or len(termination_token) < 16):
            raise ValueError("termination_token_invalid")
        if self.termination_callback is not None and receipt is not None:
            request = {
                "run_id": state["run_id"], "task_id": task_id, "launch_correlation_id": correlation,
                "termination_token": termination_token, "reason": reason,
            }
            try:
                response = self.termination_callback(deepcopy(request))
            except Exception as exc:
                raise ValueError("host_termination_callback_failed") from exc
            if response is not True and not (isinstance(response, dict) and response.get("terminated") is True):
                raise ValueError("host_termination_not_confirmed")
            termination_scope = (
                response.get("termination_scope") if isinstance(response, dict) else "legacy_single_process"
            )
            if self.require_capability and termination_scope not in {"process_group_only", "sandbox_boundary"}:
                raise ValueError("host_termination_scope_unverified")
            self.ledger.append(state["run_id"], "launch_termination_confirmed", {
                "launch_correlation_id": correlation,
                "termination_token_sha256": hashlib.sha256((termination_token or "").encode()).hexdigest(),
                "terminated_at": _now(),
                "termination_scope": termination_scope,
            }, task_id)
        elif termination_token:
            self.ledger.append(state["run_id"], "launch_termination_pending", {
                "launch_correlation_id": correlation,
                "termination_token_sha256": hashlib.sha256(termination_token.encode()).hexdigest(),
            }, task_id)
            raise ValueError("host_termination_callback_required")
        reserved = float(task.get("reserved_cost_usd", 0.0))
        state["usage"]["reserved_cost_usd"] = round(max(0.0, float(state["usage"].get("reserved_cost_usd", 0.0)) - reserved), 6)
        task["reserved_cost_usd"] = 0.0
        task["status"] = "cancelled"
        task["last_error"] = reason[:2000]
        task["updated_at"] = _now()
        record["status"] = "cancelled"
        self.ledger.append(state["run_id"], "launch_cancelled", {"launch_correlation_id": correlation,
                           "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(), "cancelled_at": _now()}, task_id)
        self._emit("policy_denied", {"run_id": state["run_id"], "task_id": task_id,
                                    "status": "cancelled", "reason_code": "host_cancelled"})

    @_ledger_operation
    def requeue_cancelled_task(self, state: dict[str, Any], task_id: str, reason: str) -> str:
        """Return one confirmed-cancelled task to pending without minting a launch.

        Cancellation and requeue are distinct host decisions.  This transition
        requires the prior receipt, confirmed process termination, and durable
        cancellation evidence to agree.  It deliberately leaves the cancelled
        launch record and correlation untouched; only ``plan_next_wave`` may
        mint the later launch identity.
        """

        validate_run_state(state)
        if state.get("status") in {"blocked", "complete"}:
            raise ValueError("requeue_run_terminal")
        if state.get("status") != "collecting":
            raise ValueError("requeue_run_state_invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("requeue_reason_required")
        if len(reason.encode("utf-8")) > 512:
            raise ValueError("requeue_reason_too_large")
        task = self._tasks(state).get(task_id)
        if not task:
            raise ValueError("requeue_task_not_found")
        correlation = task.get("launch_correlation_id")
        record = state.get("launch_records", {}).get(correlation)
        if not isinstance(correlation, str) or not record or record.get("task_id") != task_id or record.get("status") != "cancelled":
            raise ValueError("requeue_cancelled_record_required")
        if any(
            item.get("task_id") == task_id and item.get("status") == "active"
            for item in state.get("launch_records", {}).values()
        ):
            raise ValueError("requeue_active_launch_exists")

        events = self.ledger.events(state["run_id"])
        prior_requeues = [
            event for event in events
            if event["event_type"] == "cancelled_task_requeued" and event["task_id"] == task_id
            and event["payload"].get("prior_launch_correlation_id") == correlation
        ]
        if len(prior_requeues) > 1:
            raise ValueError("requeue_evidence_duplicated")
        receipt_indexes = [
            index for index, event in enumerate(events)
            if event["event_type"] == "launch_receipt" and event["task_id"] == task_id
            and event["payload"].get("launch_correlation_id") == correlation
        ]
        termination_indexes = [
            index for index, event in enumerate(events)
            if event["event_type"] == "launch_termination_confirmed" and event["task_id"] == task_id
            and event["payload"].get("launch_correlation_id") == correlation
        ]
        cancelled_indexes = [
            index for index, event in enumerate(events)
            if event["event_type"] == "launch_cancelled" and event["task_id"] == task_id
            and event["payload"].get("launch_correlation_id") == correlation
        ]
        if len(receipt_indexes) != 1 or len(cancelled_indexes) != 1:
            raise ValueError("requeue_cancelled_evidence_required")
        if len(termination_indexes) != 1:
            raise ValueError("requeue_termination_confirmation_required")
        if not (receipt_indexes[0] < termination_indexes[0] < cancelled_indexes[0]):
            raise ValueError("requeue_cancellation_evidence_order_invalid")
        receipt = events[receipt_indexes[0]]["payload"]
        termination_token = receipt.get("termination_token")
        if not isinstance(termination_token, str) or len(termination_token) < 16:
            raise ValueError("requeue_termination_confirmation_invalid")
        expected_token_sha = hashlib.sha256(termination_token.encode()).hexdigest()
        if events[termination_indexes[0]]["payload"].get("termination_token_sha256") != expected_token_sha:
            raise ValueError("requeue_termination_confirmation_invalid")
        if any(
            event["event_type"] == "worker_result_accepted" and event["task_id"] == task_id
            and event["payload"].get("launch_correlation_id") == correlation
            for event in events
        ):
            raise ValueError("requeue_worker_result_already_accepted")

        latest_effects: dict[str, dict[str, Any]] = {}
        for event in events:
            if event["event_type"] == "effect_record" and event["task_id"] == task_id:
                effect_id = event["payload"].get("effect_id")
                if isinstance(effect_id, str):
                    latest_effects[effect_id] = event["payload"]
        effect_states = {effect.get("state") for effect in latest_effects.values()}
        if effect_states & {"pending", "unknown_after_crash"}:
            raise ValueError("requeue_effect_unresolved")
        if "applied" in effect_states:
            raise ValueError("requeue_effect_already_applied")
        if effect_states - {"failed"}:
            raise ValueError("requeue_effect_state_invalid")

        budget = state["budget"]
        usage = state["usage"]
        if task["attempts"] >= budget["max_attempts_per_task"]:
            raise ValueError("requeue_attempt_budget_exhausted")
        if state["wave_index"] >= budget["max_waves"]:
            raise ValueError("requeue_wave_budget_exhausted")
        if usage["workers_launched"] >= budget["max_total_workers"]:
            raise ValueError("requeue_worker_budget_exhausted")
        available_cost = float(budget["max_total_cost_usd"]) - float(usage["cost_usd"]) - float(usage.get("reserved_cost_usd", 0.0))
        if available_cost <= 0 or float(task["budgets"].get("max_cost_usd", 0.0)) <= 0:
            raise ValueError("requeue_cost_budget_exhausted")
        if state["no_progress_waves"] >= budget["max_no_progress_waves"]:
            raise ValueError("requeue_no_progress_budget_exhausted")

        if prior_requeues:
            prior = prior_requeues[0]
            if prior["payload"].get("reason_sha256") != hashlib.sha256(reason.encode()).hexdigest():
                raise ValueError("requeue_reason_mismatch")
            if task.get("status") == "cancelled":
                task["status"] = "pending"
                task["updated_at"] = prior["payload"].get("requeued_at") or _now()
            elif task.get("status") != "pending":
                raise ValueError("requeue_state_reconciliation_invalid")
            validate_run_state(state)
            self._emit("replan", {"run_id": state["run_id"], "task_id": task_id,
                                  "status": "pending", "reason_code": "confirmed_cancellation_reconciled"})
            return prior["event_id"]
        if task.get("status") != "cancelled":
            raise ValueError("requeue_task_not_cancelled")

        prior_error = task.get("last_error")
        task["status"] = "pending"
        task["updated_at"] = _now()
        validate_run_state(state)
        event_id = self.ledger.append(
            state["run_id"],
            "cancelled_task_requeued",
            {
                "prior_launch_correlation_id": correlation,
                "prior_attempt": task["attempts"],
                "prior_error_sha256": hashlib.sha256(str(prior_error or "").encode()).hexdigest(),
                "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
                "new_launch_correlation_id": None,
                "remaining_attempts": int(budget["max_attempts_per_task"]) - int(task["attempts"]),
                "remaining_workers": int(budget["max_total_workers"]) - int(usage["workers_launched"]),
                "remaining_cost_usd": round(available_cost, 6),
                "requeued_at": task["updated_at"],
            },
            task_id,
        )
        self._emit("replan", {"run_id": state["run_id"], "task_id": task_id,
                              "status": "pending", "reason_code": "confirmed_cancellation_requeued"})
        return event_id

    @_ledger_operation
    def record_effect(self, run_id: str, task_id: str, effect: dict[str, Any]) -> str:
        if effect.get("schema") != "EffectRecordV1" or effect.get("state") not in EFFECT_STATES:
            raise ValueError("effect_record_invalid")
        required = {"effect_id", "idempotency_key", "intent_sha256", "policy_sha256", "state", "observed_at"}
        if any(not effect.get(field) for field in required):
            raise ValueError("effect_record_required_fields_invalid")
        task_events = [event for event in self.ledger.events(run_id) if event["task_id"] == task_id]
        latest_requeue = max(
            (index for index, event in enumerate(task_events) if event["event_type"] == "cancelled_task_requeued"),
            default=-1,
        )
        latest_launch = max(
            (index for index, event in enumerate(task_events) if event["event_type"] == "launch_receipt"),
            default=-1,
        )
        if latest_requeue > latest_launch:
            raise ValueError("effect_launch_required_after_requeue")
        previous = self.ledger.latest_effect(run_id, task_id, effect["effect_id"])
        if previous:
            legal = {
                "pending": {"applied", "failed", "unknown_after_crash"},
                "applied": set(), "failed": set(), "unknown_after_crash": set(),
            }
            if effect["state"] not in legal.get(previous["state"], set()):
                raise ValueError("effect_transition_invalid")
        return self.ledger.append(run_id, "effect_record", effect, task_id)

    def ingest_worker_batch(
        self,
        state: dict[str, Any],
        results: list[dict[str, Any]],
        usage_by_launch: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        validate_run_state(state)
        self._enforce_launch_liveness(state)
        if any(contains_sensitive_material(result) for result in results):
            raise ValueError("worker_result_sensitive_material")
        for result in results:
            correlation = result.get("launch_correlation_id")
            task = self._tasks(state).get(result.get("task_id"))
            if not task or not isinstance(correlation, str):
                raise ValueError("verified_launch_receipt_required")
            self._verified_launch_receipt(state, task["task_id"], correlation)
            usage = usage_by_launch.get(correlation)
            if not usage:
                raise ValueError("measured_provider_usage_required")
            record_provider_usage(state, correlation, usage)
        outcome = ingest_results(state, results)
        tasks = self._tasks(state)
        for result in results:
            task = tasks.get(result.get("task_id"))
            if task and result.get("status") in {"success", "partial"}:
                self.ledger.append(
                    state["run_id"], "worker_result_accepted",
                    {"result_id": result["result_id"], "launch_correlation_id": result["launch_correlation_id"],
                     "result_sha256": hashlib.sha256(_canonical(result)).hexdigest(),
                     "validator_required": bool(task.get("validator_required"))},
                    task["task_id"],
                )
                self._emit("result", {"run_id": state["run_id"], "task_id": task["task_id"], "status": result["status"],
                                      "residual_count": len(result.get("remaining_work") or [])})
        return outcome

    def _accepted_worker_result(self, state: dict[str, Any], task_id: str, result_id: str) -> dict[str, Any]:
        matches = [
            event["payload"] for event in self.ledger.events(state["run_id"], "worker_result_accepted")
            if event["task_id"] == task_id and event["payload"].get("result_id") == result_id
        ]
        if len(matches) != 1 or not _is_sha256(matches[0].get("result_sha256")):
            raise ValueError("validator_result_not_accepted")
        return matches[0]

    def record_validator_launch(self, state: dict[str, Any], receipt: dict[str, Any]) -> None:
        """Record a host-issued validator launch before accepting its verdict."""

        validate_run_state(state)
        if receipt.get("schema") != "ValidatorLaunchReceiptV1" or receipt.get("run_id") != state["run_id"]:
            raise ValueError("validator_launch_receipt_invalid")
        required = {
            "task_id", "validator_launch_correlation_id", "validated_result_id", "validated_result_sha256",
            "validator_policy_sha256", "validator_provider", "validator_resolved_model",
            "validator_reasoning_effort", "validator_capability_digest", "started_at", "lease_expires_at", "usage",
        }
        if any(not receipt.get(field) for field in required):
            raise ValueError("validator_launch_receipt_required_fields_invalid")
        task = self._tasks(state).get(receipt["task_id"])
        if not task or task.get("status") != "completed":
            raise ValueError("validator_launch_task_not_completed")
        accepted = self._accepted_worker_result(state, task["task_id"], receipt["validated_result_id"])
        if receipt["validated_result_sha256"] != accepted["result_sha256"]:
            raise ValueError("validator_launch_result_binding_invalid")
        expected_policy = _validator_policy_sha256(task["task_id"], accepted["result_sha256"])
        if receipt["validator_policy_sha256"] != expected_policy:
            raise ValueError("validator_launch_policy_binding_invalid")
        correlation = receipt["validator_launch_correlation_id"]
        if not isinstance(correlation, str) or len(correlation) < 16:
            raise ValueError("validator_launch_correlation_invalid")
        if any(event["payload"].get("validator_launch_correlation_id") == correlation
               for event in self.ledger.events(state["run_id"], "validator_launch_receipt")):
            raise ValueError("validator_launch_receipt_replay")
        now = datetime.now(timezone.utc)
        started = _parse_time(receipt["started_at"], "validator_launch_started_at_invalid")
        lease = _parse_time(receipt["lease_expires_at"], "validator_launch_lease_invalid")
        if started > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS) or lease <= now:
            raise ValueError("validator_launch_lease_expired")
        if lease <= started or (lease - started).total_seconds() > MAX_LAUNCH_LEASE_SECONDS:
            raise ValueError("validator_launch_lease_invalid")
        usage = receipt["usage"]
        if not isinstance(usage, dict) or usage.get("usage_verified") is not True:
            raise ValueError("validator_usage_unverified")
        for field in ("input_tokens", "output_tokens"):
            if isinstance(usage.get(field), bool) or not isinstance(usage.get(field), int) or usage[field] < 0:
                raise ValueError("validator_usage_invalid")
        if usage["input_tokens"] > int(task["budgets"]["max_prompt_tokens"]):
            raise ValueError("validator_prompt_token_budget_exceeded")
        if usage["output_tokens"] > int(task["budgets"]["max_output_tokens"]):
            raise ValueError("validator_output_token_budget_exceeded")
        if (
            isinstance(usage.get("cost_usd"), bool)
            or not isinstance(usage.get("cost_usd"), (int, float))
            or not math.isfinite(float(usage["cost_usd"]))
            or usage["cost_usd"] < 0
        ):
            raise ValueError("validator_usage_invalid")
        cost_basis = usage.get("cost_basis")
        measured_savings = usage.get("measured_savings_claimed")
        if cost_basis == "provider_reported":
            if usage.get("cost_verified") is not True or not isinstance(usage.get("price_provenance"), str) or not usage["price_provenance"].strip():
                raise ValueError("validator_usage_price_provenance_required")
        elif cost_basis == "reservation_upper_bound":
            if usage.get("cost_verified") is not False or measured_savings is not False or float(usage["cost_usd"]) <= 0:
                raise ValueError("validator_usage_reservation_required")
        elif cost_basis == "covered_by_task_reservation":
            worker_record = state["launch_records"].get(task.get("launch_correlation_id"), {})
            worker_usage = worker_record.get("provider_usage") if isinstance(worker_record, dict) else None
            cap = float(task["budgets"]["max_cost_usd"])
            charged_task_cap = usage.get("charged_task_cap_usd")
            worker_cost = worker_usage.get("cost_usd") if isinstance(worker_usage, dict) else None
            if (usage.get("cost_verified") is not False or measured_savings is not False or float(usage["cost_usd"]) != 0.0
                    or isinstance(charged_task_cap, bool)
                    or not isinstance(charged_task_cap, (int, float))
                    or not math.isfinite(float(charged_task_cap))
                    or abs(float(usage["charged_task_cap_usd"]) - cap) > 1e-6
                    or isinstance(worker_cost, bool)
                    or not isinstance(worker_cost, (int, float))
                    or not math.isfinite(float(worker_cost))
                    or abs(float(worker_cost) - cap) > 1e-6):
                raise ValueError("validator_usage_task_reservation_not_charged")
        else:
            raise ValueError("validator_usage_cost_basis_invalid")
        digest = receipt["validator_capability_digest"]
        if not _is_sha256(digest):
            raise ValueError("validator_capability_digest_invalid")
        capability = next((event for event in reversed(self.ledger.events(state["run_id"], "capability_record"))
                           if event["payload_sha256"] == digest), None)
        approved = self.ledger.events(state["run_id"], "approved_providers")
        if self.require_capability and (not capability or not approved or receipt["validator_provider"] not in approved[-1]["payload"].get("providers", [])):
            raise ValueError("validator_provider_not_approved")
        if capability and (
            capability["payload"].get("provider") != receipt["validator_provider"]
            or capability["payload"].get("exact_model_id") != receipt["validator_resolved_model"]
            or capability["payload"].get("reasoning_effort") != receipt["validator_reasoning_effort"]
            or capability["payload"].get("task_class") not in {task["task_class"], "*"}
            or capability["payload"].get("tool_policy_sha256") != expected_policy
        ):
            raise ValueError("validator_capability_binding_invalid")
        if receipt["validator_provider"] == "Local" and receipt.get("coordinator_hosted_boundary") is not None:
            if receipt.get("local_no_tool_boundary") is not None:
                raise ValueError("coordinator_legacy_boundary_conflict")
            if not callable(self.coordinator_boundary_verifier):
                raise ValueError("coordinator_profiled_host_verifier_required")
            if capability and capability["payload"].get("coordinator_boundary_sha256") != hashlib.sha256(_canonical(receipt["coordinator_hosted_boundary"])).hexdigest():
                raise ValueError("coordinator_validator_capability_boundary_mismatch")
            starts = self.ledger.events(state["run_id"], "validator_attempt_started")
            if not any(e["payload"].get("validator_launch_correlation_id") == receipt["validator_launch_correlation_id"] and e["payload"].get("coordinator_hosted_boundary") == receipt["coordinator_hosted_boundary"] for e in starts):
                raise ValueError("coordinator_validator_start_binding_required")
        elif receipt["validator_provider"] == "Local":
            if not isinstance(receipt.get("local_no_tool_boundary"), dict):
                raise ValueError("local_validator_no_tool_boundary_required")
            self._verify_local_no_tool_boundary(
                state,
                task,
                {
                    "model_selector": receipt.get("validator_model_selector"),
                    "resolved_model": receipt["validator_resolved_model"],
                    "reasoning_effort": receipt["validator_reasoning_effort"],
                    "launch_correlation_id": receipt["validator_launch_correlation_id"],
                    "input_mode": receipt.get("input_mode"),
                    "tool_mode": receipt.get("tool_mode"),
                    "local_capability_attestation": receipt.get("local_capability_attestation"),
                    "local_bridge_observation": receipt.get("local_bridge_observation"),
                    "local_no_tool_boundary": receipt.get("local_no_tool_boundary"),
                    "boundary_phase": "validator_receipt",
                },
                capability,
            )
        self.ledger.append(state["run_id"], "validator_launch_receipt", receipt, task["task_id"])

    def record_validator_result(self, state: dict[str, Any], result: dict[str, Any]) -> None:
        validate_run_state(state)
        if contains_sensitive_material(result):
            raise ValueError("validator_result_sensitive_material")
        if result.get("schema") != "ValidatorResultV1" or result.get("verdict") not in VALIDATOR_VERDICTS:
            raise ValueError("validator_result_invalid")
        task = self._tasks(state).get(result.get("task_id"))
        if not task or task.get("status") != "completed" or result.get("run_id") != state["run_id"]:
            raise ValueError("validator_task_not_completed")
        required = {"validator_id", "evidence_refs", "validated_result_id", "validator_provider",
                    "validator_resolved_model", "validator_reasoning_effort", "validator_capability_digest"}
        if any(not result.get(field) for field in required):
            raise ValueError("validator_evidence_required")
        contradictions = result.get("contradictions")
        if not isinstance(contradictions, list) or (result["verdict"] == "pass" and contradictions):
            raise ValueError("validator_contradiction_contract_invalid")
        for contradiction in contradictions:
            if (
                not isinstance(contradiction, dict)
                or not isinstance(contradiction.get("claim"), str)
                or not contradiction["claim"]
                or not isinstance(contradiction.get("task_ids"), list)
                or len(contradiction["task_ids"]) < 2
                or not all(isinstance(item, str) and item for item in contradiction["task_ids"])
                or not isinstance(contradiction.get("evidence_refs"), list)
                or not contradiction["evidence_refs"]
                or not all(isinstance(item, str) and item for item in contradiction["evidence_refs"])
                or not isinstance(contradiction.get("resolution_scope"), str)
                or not contradiction["resolution_scope"]
            ):
                raise ValueError("validator_contradiction_contract_invalid")
        accepted = self._accepted_worker_result(state, task["task_id"], result["validated_result_id"])
        if not _is_sha256(result.get("validator_capability_digest")):
            raise ValueError("validator_capability_digest_invalid")
        checked_at = _parse_time(result.get("checked_at"), "validator_checked_at_invalid")
        now = datetime.now(timezone.utc)
        if checked_at > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS) or (now - checked_at).total_seconds() > MAX_VALIDATOR_CHECK_AGE_SECONDS:
            raise ValueError("validator_checked_at_stale")
        if self.require_capability:
            launch_correlation = result.get("validator_launch_correlation_id")
            result_sha = result.get("validated_result_sha256")
            policy_sha = result.get("validator_policy_sha256")
            if not isinstance(launch_correlation, str) or result_sha != accepted["result_sha256"]:
                raise ValueError("validator_launch_result_binding_invalid")
            expected_policy = _validator_policy_sha256(task["task_id"], accepted["result_sha256"])
            if policy_sha != expected_policy:
                raise ValueError("validator_launch_policy_binding_invalid")
            launches = [
                event["payload"] for event in self.ledger.events(state["run_id"], "validator_launch_receipt")
                if event["task_id"] == task["task_id"]
                and event["payload"].get("validator_launch_correlation_id") == launch_correlation
            ]
            if len(launches) != 1:
                raise ValueError("validator_launch_receipt_required")
            launch = launches[0]
            if (launch.get("validated_result_id") != result["validated_result_id"]
                    or launch.get("validated_result_sha256") != result_sha
                    or launch.get("validator_policy_sha256") != policy_sha
                    or launch.get("validator_provider") != result["validator_provider"]
                    or launch.get("validator_resolved_model") != result["validator_resolved_model"]
                    or launch.get("validator_reasoning_effort") != result["validator_reasoning_effort"]
                    or launch.get("validator_capability_digest") != result["validator_capability_digest"]):
                raise ValueError("validator_launch_receipt_binding_invalid")
            started = _parse_time(launch.get("started_at"), "validator_launch_started_at_invalid")
            lease = _parse_time(launch.get("lease_expires_at"), "validator_launch_lease_invalid")
            if checked_at < started or checked_at > lease or lease <= now:
                raise ValueError("validator_launch_lease_expired")
            if any(event["payload"].get("validator_launch_correlation_id") == launch_correlation
                   or (event["task_id"] == task["task_id"] and event["payload"].get("validated_result_sha256") == result_sha)
                   for event in self.ledger.events(state["run_id"], "validator_result")):
                raise ValueError("validator_result_replay")
        capability = next((event for event in reversed(self.ledger.events(state["run_id"], "capability_record"))
                           if event["payload_sha256"] == result["validator_capability_digest"]), None)
        if self.require_capability and not capability:
            raise ValueError("validator_capability_not_recorded")
        if self.require_capability:
            approved = self.ledger.events(state["run_id"], "approved_providers")
            if not approved or result["validator_provider"] not in approved[-1]["payload"].get("providers", []):
                raise ValueError("validator_provider_not_approved")
        if capability and (capability["payload"].get("provider") != result["validator_provider"]
                           or capability["payload"].get("exact_model_id") != result["validator_resolved_model"]
                           or capability["payload"].get("reasoning_effort") != result["validator_reasoning_effort"]
                           or capability["payload"].get("task_class") not in {task["task_class"], "*"}
                           or (self.require_capability and capability["payload"].get("tool_policy_sha256") != result.get("validator_policy_sha256"))):
            raise ValueError("validator_capability_binding_invalid")
        self.ledger.append(state["run_id"], "validator_result", result, task["task_id"])
        if result["verdict"] == "pass":
            state["validation_status"][task["task_id"]] = "passed"
        else:
            state["validation_status"][task["task_id"]] = "repair_required"
            task["last_error"] = f"validator_{result['verdict']}"
            task["status"] = "pending" if task["attempts"] < state["budget"]["max_attempts_per_task"] else "blocked"
            task["updated_at"] = _now()
        self._emit("validation", {"run_id": state["run_id"], "task_id": task["task_id"],
                                  "validator_verdict": result["verdict"], "status": result["verdict"]})

    def authorize_parent_action(self, state: dict[str, Any], action_class: str) -> None:
        validate_run_state(state)
        if action_class not in PARENT_WORKER_ACTIONS:
            return
        residual = [task for task in state["tasks"] if task.get("delegable") and task.get("status") != "completed"]
        if residual:
            raise PermissionError("parent_worker_action_denied:delegable_work_remains")
        for task in state["tasks"]:
            if task.get("validator_required"):
                verdict = self.ledger.latest_task_event(state["run_id"], task["task_id"], "validator_result")
                if not verdict or verdict.get("verdict") != "pass":
                    raise PermissionError("parent_worker_action_denied:validation_pending")

    def authorize_terminal(self, state: dict[str, Any]) -> None:
        validate_run_state(state)
        self._enforce_launch_liveness(state)
        if state.get("status") != "complete":
            raise PermissionError("terminal_denied:controller_not_complete")
        for task in state["tasks"]:
            if task.get("validator_required"):
                verdict = self.ledger.latest_task_event(state["run_id"], task["task_id"], "validator_result")
                if not verdict or verdict.get("verdict") != "pass":
                    raise PermissionError("terminal_denied:validation_not_passed")
            launch = task.get("launch_correlation_id")
            if task.get("delegable") and launch:
                record = state["launch_records"].get(launch, {})
                if not record.get("provider_usage"):
                    raise PermissionError("terminal_denied:usage_unverified")
        latest_effects: dict[str, dict[str, Any]] = {}
        for event in self.ledger.events(state["run_id"], "effect_record"):
            latest_effects[event["payload"].get("effect_id", event["event_id"])] = event["payload"]
        for effect in latest_effects.values():
            if effect.get("state") in {"pending", "unknown_after_crash"}:
                raise PermissionError("terminal_denied:effect_unresolved")
        self.ledger.append(
            state["run_id"], "terminal_authorized",
            {"status": "authorized", "residual_count": 0, "validated_task_count": len(state["tasks"])},
        )
        self._emit("terminal", {"run_id": state["run_id"], "status": "authorized", "residual_count": 0})


def docker_policy_receipt(
    policy: dict[str, Any],
    observed: dict[str, Any],
    *,
    run_id: str,
    task_id: str,
    launch_correlation_id: str,
    signing_key: Any,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Sign a canonical but experimental Docker-policy observation.

    This helper validates exact policy/observation equality and signs the
    result, but it does not itself collect Docker inspect output or prove that
    the signer is isolated from the worker.  It therefore cannot mint the
    ``host_enforced`` status required by :class:`HostDispatcher`.
    """

    canonical_policy = _canonical_docker_tool_policy(policy, require_paths_exist=True)
    expected_boundary = _docker_boundary_from_policy(canonical_policy)
    container_id, observation_source, canonical_observed = _canonical_docker_observation(observed)
    if canonical_observed != expected_boundary:
        raise ValueError("docker_policy_observation_mismatch")
    if not hasattr(signing_key, "sign"):
        raise ValueError("docker_host_signing_key_required")
    now = datetime.now(timezone.utc)
    observation_identity = {
        "container_id": container_id,
        "observation_source": observation_source,
        "observed_policy": canonical_observed,
    }
    signed = {
        "run_id": run_id, "task_id": task_id, "launch_correlation_id": launch_correlation_id,
        "container_id": container_id, "nonce": uuid.uuid4().hex,
        "boundary_status": DOCKER_SANDBOX_EXPERIMENTAL_STATUS,
        "observation_source": observation_source,
        "tool_policy": canonical_policy,
        "tool_policy_sha256": hashlib.sha256(_canonical(canonical_policy)).hexdigest(),
        "declared_policy": expected_boundary,
        "declared_policy_sha256": hashlib.sha256(_canonical(expected_boundary)).hexdigest(),
        "observed_policy": canonical_observed,
        "observation_sha256": hashlib.sha256(_canonical(observation_identity)).hexdigest(),
        "image_digest": canonical_observed["image_digest"], "verified_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=max(1, min(int(ttl_seconds), MAX_LAUNCH_LEASE_SECONDS)))).isoformat().replace("+00:00", "Z"),
    }
    try:
        signature = signing_key.sign(_canonical(signed))
    except Exception as exc:
        raise ValueError("docker_host_observation_sign_failed") from exc
    return {
        "schema": "SandboxReceiptV1", "algorithm": "Ed25519", "signed": signed,
        "signature": base64.b64encode(signature).decode(),
    }


def build_docker_tool_command(policy: dict[str, Any]) -> list[str]:
    """Build an experimental no-shell Docker command for exact file mounts."""

    normalized = _canonical_docker_tool_policy(policy, require_paths_exist=True)
    daemon = normalized["daemon_identity"]
    args = [
        "docker", "--host", daemon["endpoint"], "run", "--rm", "--pull", "never", "--privileged=false", "--read-only",
        "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", str(normalized["pids_limit"]), "--memory", normalized["memory"],
        "--cpus", normalized["cpus"], "--user", normalized["user"],
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
    ]
    for path in normalized["read_paths"]:
        args.extend(["--mount", f"type=bind,src={path},dst={path},readonly"])
    for path in normalized["write_files"]:
        args.extend(["--mount", f"type=bind,src={path},dst={path}"])
    args.extend([normalized["image"], *normalized["command"]])
    return args


def execute_docker_tool(policy: dict[str, Any]) -> dict[str, Any]:
    """Run the experimental Docker helper without claiming container cancellation."""

    normalized = _canonical_docker_tool_policy(policy, require_paths_exist=True)
    args = build_docker_tool_command(policy)
    timed_out = False
    termination_signal = None
    termination_escalation_failed = False
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        try:
            process.wait(timeout=normalized["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            termination_signal = "SIGTERM"
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                termination_signal = "SIGKILL"
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    termination_escalation_failed = True
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout_bytes = stdout_file.read(normalized["max_output_bytes"] + 1)
        stderr_bytes = stderr_file.read(normalized["max_output_bytes"] + 1)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        cli_group_exit_confirmed = True
    except PermissionError:
        cli_group_exit_confirmed = False
    else:
        cli_group_exit_confirmed = False
    max_output = normalized["max_output_bytes"]
    stdout = stdout_bytes[:max_output].decode(errors="replace")
    stderr = stderr_bytes[:max_output].decode(errors="replace")
    status = "timeout_container_state_unknown" if timed_out else (
        "docker_cli_exited" if process.returncode == 0 else "docker_cli_failed"
    )
    return {
        "schema": "DockerToolResultV1",
        "enforcement_level": "experimental_non_enforced",
        "status": status,
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "output_truncated": len(stdout_bytes) > max_output or len(stderr_bytes) > max_output,
        "timed_out": timed_out,
        "termination_requested": timed_out,
        "termination_signal": termination_signal,
        "termination_escalation_failed": termination_escalation_failed,
        "termination_scope": "docker_cli_process_group_only" if timed_out else "none",
        "docker_cli_process_group_exit_confirmed": cli_group_exit_confirmed,
        "container_exit_confirmed": False,
        "cancellation_confirmed": False,
        "external_effect_status": "unknown_after_timeout" if timed_out else "not_attested",
        "enforced_local_receipt_eligible": False,
    }
