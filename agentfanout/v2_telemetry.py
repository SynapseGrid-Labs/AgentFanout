"""Host-local, metadata-only V2 usage ledger. Never a task execution dependency."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .telemetry import _sensitive_string

ROLES = {"parent", "worker", "validator", "retry", "synthesis", "audit", "system"}
EVENTS = {"plan", "queue", "launch", "ack", "first_output", "usage", "result",
          "validation", "retry", "reuse", "cancel", "terminal", "hook"}
LABELS = {"event_id", "run_id", "assignment_id", "worker_id", "parent_event_id", "host", "provider", "request_id",
          "requested_model", "actual_model", "requested_effort", "actual_effort",
          "policy_revision", "surface", "configuration", "status", "reason_code",
          "failure_class", "usage_basis", "cost_basis", "evidence_sha256"}
COUNTERS = {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
            "model_requests", "tool_calls", "context_bytes", "retry_count"}
MEASURES = COUNTERS | {"duration_ms", "cost_usd", "lock_wait_ms"}
TOTALS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
          "model_requests", "tool_calls", "cost_usd")
LABEL = re.compile(r"^[A-Za-z0-9_.:@+\-]{1,160}$")


def clean_event(event: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown fields; reject malformed known fields rather than coerce them."""
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    if event.get("event_type") not in EVENTS or event.get("role") not in ROLES:
        raise ValueError("event type or role invalid")
    result = {"schema": "AgentFanoutUsageV2", "event_type": event["event_type"],
              "role": event["role"], "recorded_at": datetime.now(timezone.utc).isoformat()}
    for key in LABELS:
        value = event.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not LABEL.fullmatch(value) or _sensitive_string(value):
            raise ValueError("invalid metadata label")
        result[key] = value
    if not result.get("event_id") or not result.get("run_id"):
        raise ValueError("event and run identifiers required")
    if result["event_type"] == "usage" and (
        result.get("usage_basis") != "request_delta"
        or not all(result.get(key) for key in ("host", "provider", "request_id"))
    ):
        raise ValueError("usage requires host provider request identity and request_delta basis")
    for key in MEASURES:
        value = event.get(key)
        if value is None:
            result[key] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid metric")
        elif key in COUNTERS and not isinstance(value, int):
            raise ValueError("counter must be integer")
        else:
            result[key] = value
    for subset, total in (("cached_input_tokens", "input_tokens"), ("reasoning_tokens", "output_tokens")):
        if result[subset] is not None and result[total] is not None and result[subset] > result[total]:
            raise ValueError("token subset exceeds total")
    if result["cost_usd"] is not None and result.get("cost_basis") not in {"actual_charge", "api_equivalent_estimate"}:
        raise ValueError("cost basis required")
    result.setdefault("actual_model", "unknown")
    result.setdefault("actual_effort", "unknown")
    return result


def _safe_path(path: str | Path) -> Path:
    candidate = Path(path).absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise ValueError("ledger must not traverse symlinks")
    if not candidate.parent.is_dir() or (candidate.exists() and not candidate.is_file()):
        raise ValueError("ledger parent must already exist")
    return candidate


def _require_schema(db: sqlite3.Connection) -> None:
    columns = [row[1] for row in db.execute("PRAGMA table_info(v2_events)")]
    if columns != ["event_id", "run_id", "payload", "host", "provider", "request_id"]:
        raise ValueError("incompatible usage ledger; retain it and use a fresh V2 ledger")


def record_event(path: str | Path, event: dict[str, Any]) -> bool:
    """Best-effort append; duplicate IDs cannot inflate usage or rewrite history."""
    try:
        payload = clean_event(event)
        target = _safe_path(path)
        if not target.exists():
            try:
                descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            except FileExistsError:
                _safe_path(target)
        with closing(sqlite3.connect(str(target), timeout=0.1)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS v2_events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL, host TEXT, provider TEXT, request_id TEXT, UNIQUE(host,provider,request_id))")
            _require_schema(db)
            db.execute("CREATE INDEX IF NOT EXISTS v2_events_run ON v2_events(run_id)")
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload FROM v2_events WHERE event_id=?", (payload["event_id"],)).fetchone()
            request_replay = False
            if not prior and payload["event_type"] == "usage":
                prior = db.execute("SELECT payload FROM v2_events WHERE host=? AND provider=? AND request_id=?",
                                   (payload["host"], payload["provider"], payload["request_id"])).fetchone()
                request_replay = bool(prior)
            if prior:
                previous = json.loads(prior[0])
                previous.pop("recorded_at", None)
                current = dict(payload)
                current.pop("recorded_at", None)
                if request_replay:
                    previous.pop("event_id", None)
                    current.pop("event_id", None)
                return previous == current
            identity = tuple(payload[key] for key in ("host", "provider", "request_id")) if payload["event_type"] == "usage" else (None, None, None)
            db.execute("INSERT INTO v2_events VALUES (?, ?, ?, ?, ?, ?)",
                       (payload["event_id"], payload["run_id"], json.dumps(payload, sort_keys=True, allow_nan=False), *identity))
        return True
    except Exception:
        # Observability loss must never become an execution failure.
        return False


def summarize(path: str | Path, run_id: str) -> dict[str, Any]:
    """Sum per-request delta usage only; unknown coverage stays unknown.

    Cached input is a subset of input and reasoning a subset of output. Neither
    is added again to total_tokens. Duration is not summed across parallel work.
    Parent and synthesis usage must be disjoint request records, not two copies
    of the parent session's cumulative counters.
    """
    try:
        target = _safe_path(path)
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
            _require_schema(db)
            events = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM v2_events WHERE run_id=? ORDER BY rowid", (run_id,))]
            for event in events:
                clean_event(event)
    except Exception:
        return {"schema": "AgentFanoutUsageSummaryV2", "status": "unavailable", "run_id": run_id}
    usage = [event for event in events if event["event_type"] == "usage"]
    totals: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    for key in TOTALS:
        values = [event[key] for event in usage if event.get(key) is not None]
        coverage[key] = {"known": len(values), "records": len(usage)}
        totals[key] = sum(values) if usage and len(values) == len(usage) else None
    bases = {event.get("cost_basis") for event in usage if event.get("cost_usd") is not None}
    if len(bases) != 1:
        totals["cost_usd"] = None
    totals["total_tokens"] = (totals["input_tokens"] + totals["output_tokens"]
                               if totals["input_tokens"] is not None and totals["output_tokens"] is not None else None)
    role_counts = {role: sum(e["role"] == role for e in usage) for role in sorted(ROLES)}
    return {"schema": "AgentFanoutUsageSummaryV2", "status": "available", "run_id": run_id,
            "usage_records": len(usage), "totals": totals, "coverage": coverage,
            "cost_basis": next(iter(bases)) if len(bases) == 1 else "unknown",
            "usage_records_by_role": role_counts,
            "event_counts": {kind: sum(e["event_type"] == kind for e in events) for kind in sorted(EVENTS)},
            "unknown_model_records": sum(e.get("actual_model") == "unknown" for e in usage),
            "complete_task_accounting_verified": False,
            "limitations": ["Importer must reconcile all host request IDs and parent/synthesis boundaries; event presence alone does not prove complete accounting."]}
