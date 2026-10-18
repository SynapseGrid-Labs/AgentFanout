#!/usr/bin/env python3
"""Codex lifecycle lease for persistent AgentFanout worker waves.

The hook is intentionally standard-library-only and stores no prompt or
transcript content. Codex invokes one short-lived process per lifecycle event;
SQLite serializes concurrent subagent callbacks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA = "CodexSessionLeaseV1"
MAX_INPUT_BYTES = 1_048_576
MAX_CONTINUATIONS = 16
LEASE_TTL_SECONDS = 6 * 60 * 60
CONTINUATION_PREFIX = "AGENTFANOUT_CONTINUE"
CONTROL_TOOLS = frozenset({
    "spawn_agent", "followup_task", "send_message", "wait_agent", "list_agents", "interrupt_agent",
    "collaboration.spawn_agent", "collaboration.followup_task", "collaboration.send_message",
    "collaboration.wait_agent", "collaboration.list_agents", "collaboration.interrupt_agent",
    "functions.collaboration.spawn_agent", "functions.collaboration.followup_task",
    "functions.collaboration.send_message", "functions.collaboration.wait_agent",
    "functions.collaboration.list_agents", "functions.collaboration.interrupt_agent",
    # Codex 0.144.4 normalizes native collaboration tool names by removing the
    # namespace separator before invoking PreToolUse hooks.
    "collaborationspawn_agent", "collaborationfollowup_task", "collaborationsend_message",
    "collaborationwait_agent", "collaborationlist_agents", "collaborationinterrupt_agent",
    "spawn", "follow_up", "send_input", "wait", "interrupt", "cancel",
})
CANCELLATION_CONTROL_TOOLS = frozenset({
    "wait_agent", "list_agents", "interrupt_agent",
    "collaboration.wait_agent", "collaboration.list_agents", "collaboration.interrupt_agent",
    "functions.collaboration.wait_agent", "functions.collaboration.list_agents", "functions.collaboration.interrupt_agent",
    "collaborationwait_agent", "collaborationlist_agents", "collaborationinterrupt_agent",
    "wait", "interrupt", "cancel",
})
FANOUT_NAME = r"(?:\$agent-fanout|agent[\s-]?fanout|agentfanout)"
START_COMMAND = re.compile(
    rf"^\s*(?:(?:use|run|start|invoke|activate)\s+(?:the\s+)?)?{FANOUT_NAME}"
    rf"(?:\s+(?:codex|grok|local))?(?:\s*[:\-]\s*|\s+)(?P<task>\S.*)\s*$",
    re.IGNORECASE | re.DOTALL,
)
CANCEL_COMMAND = re.compile(
    rf"^\s*(?:{FANOUT_NAME}\s+(?:cancel|stop|off)|"
    rf"(?:cancel|stop|disable)\s+(?:the\s+)?{FANOUT_NAME})\s*[.!]?\s*$",
    re.IGNORECASE,
)
INFO_COMMAND = re.compile(
    rf"^\s*{FANOUT_NAME}\s+(?:help|status)\s*[?!.]?\s*$",
    re.IGNORECASE,
)
MARKER = re.compile(
    r"<!--\s*agentfanout:(complete|blocked|cancelled)\s+"
    r"lease=([0-9a-f]{32})(?:\s+residual=(\d+))?(?:\s+reason=([a-z0-9_.-]{1,64}))?\s*-->",
    re.IGNORECASE,
)


def _now() -> int:
    return int(time.time())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _token() -> str:
    return secrets.token_hex(16)


def _json_output(value: dict[str, Any] | None = None) -> None:
    sys.stdout.write(json.dumps(value or {}, sort_keys=True, separators=(",", ":")))


def _data_root(*, create: bool = True) -> Path | None:
    configured = os.environ.get("PLUGIN_DATA")
    root = Path(configured).expanduser() if configured else Path.home() / ".codex" / "agentfanout-data"
    if root.is_symlink():
        raise RuntimeError("AgentFanout plugin data root must not be a symlink")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not root.exists():
        return None
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("AgentFanout plugin data root must be a directory")
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise RuntimeError("AgentFanout plugin data root must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("AgentFanout plugin data root must not be accessible by group or other users")
    return root


def _connect(*, maintenance: bool = True) -> sqlite3.Connection:
    root = _data_root()
    assert root is not None
    path = root / "session-leases.sqlite3"
    if path.is_symlink():
        raise RuntimeError("AgentFanout lease database must not be a symlink")
    if path.exists():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("AgentFanout lease database must be a regular file")
        if os.name == "posix":
            if metadata.st_uid != os.geteuid():
                raise RuntimeError("AgentFanout lease database must be owned by the current user")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RuntimeError("AgentFanout lease database must not be accessible by group or other users")
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS leases ("
        "session_key TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    if maintenance:
        # Unknown/corrupt records are not evidence that workers are inactive.
        # Prune only classified inactive rows, comparing the complete snapshot.
        for session_key, state_json, updated_at in connection.execute("SELECT session_key, state_json, updated_at FROM leases").fetchall():
            try:
                stored_state = json.loads(state_json)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(updated_at, int):
                continue
            if _active_ownership(stored_state) is False and (
                stored_state.get("status") != "armed" or _now() - int(updated_at) > LEASE_TTL_SECONDS
            ):
                connection.execute(
                    "DELETE FROM leases WHERE session_key = ? AND state_json = ? AND updated_at = ?",
                    (session_key, state_json, updated_at),
                )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def _connect_readonly() -> sqlite3.Connection | None:
    root = _data_root(create=False)
    if root is None:
        return None
    path = root / "session-leases.sqlite3"
    if path.is_symlink():
        raise RuntimeError("AgentFanout lease database must not be a symlink")
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("AgentFanout lease database must be a regular file")
    if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise RuntimeError("AgentFanout lease database ownership or mode is unsafe")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0, isolation_level=None)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _load_readonly_state(session_key: str) -> dict[str, Any] | None:
    connection = _connect_readonly()
    if connection is None:
        return None
    try:
        return _load(connection, session_key, cleanup=False)
    finally:
        connection.close()


def _load(connection: sqlite3.Connection, session_key: str, *, cleanup: bool = True) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT state_json, updated_at FROM leases WHERE session_key = ?", (session_key,)
    ).fetchone()
    if row is None:
        return None
    try:
        state = json.loads(row[0])
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("AgentFanout lease state cannot be classified safely") from exc
    active = _active_ownership(state)
    if active is None or not isinstance(row[1], int):
        raise RuntimeError("AgentFanout lease ownership cannot be classified safely")
    expired = _now() - int(row[1]) > LEASE_TTL_SECONDS
    if active and (expired or state.get("status") != "armed"):
        state["status"] = "armed"
        state["phase"] = "cancel_requested"
        reason = "lease_expired" if expired else "unresolved_workers"
        state["blocked_reason"] = state.get("blocked_reason") or reason
        state["cancel_reason"] = state.get("cancel_reason") or reason
        # Read-only policy checks derive the restrictive view without writing.
        if cleanup:
            _save(connection, session_key, state)
    elif active and state.get("cancel_reason") and state.get("phase") != "cancel_requested":
        state["phase"] = "cancel_requested"
        if cleanup:
            _save(connection, session_key, state)
    elif expired or state.get("status") != "armed":
        if cleanup:
            connection.execute(
                "DELETE FROM leases WHERE session_key = ? AND state_json = ? AND updated_at = ?",
                (session_key, row[0], row[1]),
            )
        return None
    return state


def _active_ownership(state: Any) -> bool | None:
    if not isinstance(state, dict) or state.get("schema") != SCHEMA or not isinstance(state.get("status"), str) or state["status"] not in {"armed", "released", "blocked"}:
        return None
    phase = state.get("phase")
    armed_phases = {"awaiting_dispatch", "workers_active", "replan_required", "decision_required", "cancel_requested"}
    if not isinstance(phase, str) or phase not in armed_phases | {"complete", "blocked", "cancelled"}:
        return None
    if state["status"] == "armed" and phase not in armed_phases:
        return None
    active = state.get("active_agents")
    if not isinstance(active, list) or any(not isinstance(value, str) or not value or len(value) > 256 for value in active):
        return None
    return bool(active)


def _save(connection: sqlite3.Connection, session_key: str, state: dict[str, Any]) -> None:
    active = _active_ownership(state)
    if active is None:
        raise RuntimeError("AgentFanout lease ownership cannot be classified safely")
    if active and state.get("status") != "armed":
        # A local ceiling/status transition cannot establish worker terminality.
        state["status"] = "armed"
        state["phase"] = "cancel_requested"
        state["blocked_reason"] = state.get("blocked_reason") or "unresolved_workers"
        state["cancel_reason"] = state.get("cancel_reason") or state["blocked_reason"]
    if active and state.get("cancel_reason"):
        state["phase"] = "cancel_requested"
    if state.get("status") != "armed":
        connection.execute("DELETE FROM leases WHERE session_key = ?", (session_key,))
        return
    state["updated_at"] = _now()
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    connection.execute(
        "INSERT INTO leases(session_key, state_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(session_key) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
        (session_key, encoded, state["updated_at"]),
    )


def _new_state(event: dict[str, Any], prompt: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "armed",
        "phase": "awaiting_dispatch",
        "task_sha256": _digest(prompt),
        "activation_turn_sha256": _digest(str(event.get("turn_id", ""))),
        "workspace_sha256": _digest(os.path.realpath(str(event.get("cwd", "")))),
        "wave": 0,
        "batch": 0,
        "workers_started": 0,
        "workers_finished": 0,
        "active_agents": [],
        "known_agents": [],
        "seen_stop_events": [],
        "terminal_worker_turns": [],
        "active_worker_turns": {},
        "decision_token": _token(),
        "continuations": 0,
        "max_continuations": MAX_CONTINUATIONS,
        "armed_at": _now(),
        "updated_at": _now(),
    }


def _context(event_name: str, message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": message,
        }
    }


def _stop_continue(state: dict[str, Any], reason: str) -> dict[str, Any]:
    state["continuations"] += 1
    if state["continuations"] > state["max_continuations"]:
        if state.get("active_agents"):
            state["phase"] = "cancel_requested"
            state["cancel_reason"] = state.get("cancel_reason") or "continuation_ceiling"
            state["blocked_reason"] = "continuation_ceiling"
            return {
                "continue": True,
                "systemMessage": "AgentFanout is blocked at its continuation ceiling. Unresolved worker ownership is retained; do not claim workers stopped or launch more work.",
            }
        state["status"] = "blocked"
        state["phase"] = "blocked"
        state["blocked_reason"] = "continuation_ceiling"
        return {
            "continue": True,
            "systemMessage": "AgentFanout stopped at its continuation ceiling; the run is explicitly blocked.",
        }
    return {"decision": "block", "reason": f"{CONTINUATION_PREFIX} {reason}"}


def _marker(message: Any) -> tuple[str, str, int | None, str | None] | None:
    if not isinstance(message, str):
        return None
    matches = list(MARKER.finditer(message))
    if not matches:
        return None
    match = matches[-1]
    residual = int(match.group(3)) if match.group(3) is not None else None
    return match.group(1).lower(), match.group(2).lower(), residual, match.group(4)


def _workspace_matches(event: dict[str, Any], state: dict[str, Any]) -> bool:
    cwd = event.get("cwd")
    expected = state.get("workspace_sha256")
    return not isinstance(cwd, str) or not expected or _digest(os.path.realpath(cwd)) == expected


def _stop_event_digest(event: dict[str, Any], agent_id: str) -> str:
    return _digest("\0".join((agent_id, str(event.get("turn_id", "")), str(event.get("last_assistant_message", "")))))


def _storage_failure_output(event_name: str, event: dict[str, Any]) -> dict[str, Any]:
    if event_name == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "AgentFanout lease storage is unavailable; parent work is denied until the plugin state boundary is repaired or disabled.",
            }
        }
    if event_name == "Stop":
        if event.get("stop_hook_active") is True:
            return {}
        return {
            "decision": "block",
            "reason": f"{CONTINUATION_PREFIX} AgentFanout lease storage is unavailable. Report an explicit blocked outcome; do not continue worker work in the parent.",
        }
    prompt = event.get("prompt")
    if event_name == "UserPromptSubmit" and isinstance(prompt, str) and (
        START_COMMAND.match(prompt) or CANCEL_COMMAND.match(prompt) or INFO_COMMAND.match(prompt)
    ):
        return _context("UserPromptSubmit", "AgentFanout is unavailable because its private lease storage failed validation. Do not launch or claim fanout.")
    return {}


def _handle_prompt(event: dict[str, Any], connection: sqlite3.Connection, key: str, state: dict[str, Any] | None) -> dict[str, Any]:
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or len(prompt.encode("utf-8", "replace")) > MAX_INPUT_BYTES:
        return {}
    if prompt.startswith(CONTINUATION_PREFIX):
        return {}
    if CANCEL_COMMAND.search(prompt):
        if state and state.get("status") == "armed":
            if state.get("active_agents"):
                state["phase"] = "cancel_requested"
                state["cancel_reason"] = "user_cancelled"
                state["decision_token"] = _token()
            else:
                state["status"] = "released"
                state["phase"] = "cancelled"
                state["released_reason"] = "user_cancelled"
            _save(connection, key, state)
            if state.get("status") == "armed":
                return _context("UserPromptSubmit", "AgentFanout cancellation requested. Interrupt active workers, wait for their terminal callbacks, and do not launch another wave.")
        return _context("UserPromptSubmit", "AgentFanout lease cancelled; do not launch another wave.")
    if INFO_COMMAND.search(prompt):
        if state and state.get("status") == "armed":
            summary = f"AgentFanout lease armed: wave {state['wave']}, phase {state['phase']}, active workers {len(state['active_agents'])}."
        else:
            summary = "AgentFanout has no active task-scoped lease."
        return _context("UserPromptSubmit", summary)
    starts = START_COMMAND.match(prompt)
    if not starts:
        if state and state.get("status") == "armed":
            if state.get("active_agents"):
                state["phase"] = "cancel_requested"
                state["cancel_reason"] = "user_replaced_task"
                state["decision_token"] = _token()
                _save(connection, key, state)
                return _context(
                    "UserPromptSubmit",
                    "The user replaced the active AgentFanout task. Interrupt active workers and reconcile their terminal callbacks before starting the new request.",
                )
            state["status"] = "released"
            state["phase"] = "cancelled"
            state["released_reason"] = "user_replaced_task"
            _save(connection, key, state)
            return _context("UserPromptSubmit", "The prior AgentFanout lease was released for this unrelated user request; continue normally without fanout.")
        return {}
    if state and state.get("status") == "armed":
        return _context(
            "UserPromptSubmit",
            "An AgentFanout lease is already active. Continue that task or issue the exact `AgentFanout Cancel` command before starting another fanout task.",
        )
    state = _new_state(event, prompt)
    _save(connection, key, state)
    return _context(
        "UserPromptSubmit",
        "AgentFanout lease armed for this task. Act only as orchestrator, validator, and final synthesizer. "
        "Spawn the first bounded worker wave before using parent work tools. After every completed worker batch, "
        "the lease will force a fresh residual-work decision. Parent validation is structural; assign substantive "
        "correctness checks to an independent worker when risk or contradiction requires them. Do not expose lease markers in prose.",
    )


def _handle_subagent_start(event: dict[str, Any], connection: sqlite3.Connection, key: str, state: dict[str, Any] | None) -> dict[str, Any]:
    if not state or state.get("status") != "armed":
        return {}
    agent_id = event.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 256:
        return {}
    active = state["active_agents"]
    cancelling = state.get("phase") == "cancel_requested"
    if agent_id in active:
        if cancelling:
            return _context("SubagentStart", "AgentFanout cancellation is pending. Do not perform work; retain this worker for terminal reconciliation.")
        return _context(
            "SubagentStart",
            "You are an AgentFanout worker. Stay within the assigned scope and return a compact result with evidence, uncertainty, contradictions, and explicit residual work.",
        )
    if not active:
        state["wave"] += 1
    active.append(agent_id)
    # SubagentStart carries the child's host-issued turn, including when it is
    # interrupted before its first tool. Never derive this pair from tool input
    # or overwrite it on a duplicate start while ownership is unresolved.
    context = _worker_turn_context(event)
    if context is not None:
        if not _valid_active_turns(state):
            raise RuntimeError("AgentFanout active worker turn history is invalid")
        state.setdefault("active_worker_turns", {})[agent_id] = context[1]
    if agent_id not in state["known_agents"]:
        state["known_agents"].append(agent_id)
        state["workers_started"] += 1
    if cancelling:
        _save(connection, key, state)
        return _context("SubagentStart", "AgentFanout cancellation is pending. This late worker remains registered for reconciliation; do not perform work or launch another wave.")
    state["phase"] = "workers_active"
    state["decision_token"] = None
    _save(connection, key, state)
    return _context(
        "SubagentStart",
        "You are an AgentFanout worker. Stay within the assigned scope and return a compact result with evidence, uncertainty, contradictions, and explicit residual work.",
    )


def _handle_subagent_stop(event: dict[str, Any], connection: sqlite3.Connection, key: str, state: dict[str, Any] | None) -> dict[str, Any]:
    if not state or state.get("status") != "armed":
        return {}
    agent_id = event.get("agent_id")
    if not isinstance(agent_id, str):
        return {}
    context = _worker_turn_context(event)
    active_turns = state.get("active_worker_turns", {})
    if not isinstance(active_turns, dict):
        raise RuntimeError("AgentFanout active worker turn history is invalid")
    expected_turn = active_turns.get(agent_id)
    if expected_turn is not None and (context is None or context[1] != expected_turn):
        return {}
    if context is not None and context[1] in _terminal_turn_hashes(state, agent_id):
        return {}
    stop_digest = _stop_event_digest(event, agent_id)
    seen = state.setdefault("seen_stop_events", [])
    if not isinstance(seen, list) or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in seen
    ):
        raise RuntimeError("AgentFanout terminal event history is invalid")
    if stop_digest in seen:
        return {}
    seen.append(stop_digest)
    del seen[:-128]
    if agent_id in state.get("known_agents", []):
        context = _worker_turn_context(event)
        if context is not None:
            terminal_turns = state.setdefault("terminal_worker_turns", [])
            if not isinstance(terminal_turns, list):
                raise RuntimeError("AgentFanout worker turn history is invalid")
            terminal = {"agent_id": context[0], "turn_sha256": context[1]}
            if terminal not in terminal_turns:
                terminal_turns.append(terminal)
                del terminal_turns[:-128]
    if agent_id not in state["active_agents"]:
        if agent_id not in state["known_agents"] or state.get("active_agents") or state.get("phase") not in {"replan_required", "decision_required"}:
            _save(connection, key, state)
            return {}
        # Codex follow-up work may resume an existing worker without emitting a
        # second SubagentStart. A new trusted terminal callback from a known,
        # inactive worker is therefore a completed follow-up batch.
        state["wave"] += 1
        state["batch"] += 1
        state["workers_finished"] += 1
        state["phase"] = "replan_required"
        state["decision_token"] = _token()
        _save(connection, key, state)
        return {}
    state["active_agents"].remove(agent_id)
    active_turns.pop(agent_id, None)
    state["workers_finished"] += 1
    if not state["active_agents"]:
        if state.get("phase") == "cancel_requested":
            state["status"] = "released"
            state["phase"] = "cancelled"
            state["released_reason"] = state.get("cancel_reason", "cancelled_after_worker_stop")
            _save(connection, key, state)
            return _context("SubagentStop", "AgentFanout cancellation is reconciled; the task lease is released.")
        state["batch"] += 1
        state["phase"] = "replan_required"
        state["decision_token"] = _token()
    _save(connection, key, state)
    return {}


def _worker_turn_context(event: dict[str, Any]) -> tuple[str, str] | None:
    # These are host hook fields, never identities supplied inside tool_input.
    agent_id, turn_id = event.get("agent_id"), event.get("turn_id")
    if (
        not isinstance(agent_id, str) or not agent_id or len(agent_id) > 256
        or not isinstance(turn_id, str) or not turn_id or len(turn_id) > 512
    ):
        return None
    return agent_id, _digest(turn_id)


def _terminal_turn_hashes(state: dict[str, Any], agent_id: str) -> set[str]:
    history = state.get("terminal_worker_turns")
    if not isinstance(history, list):
        return set()
    return {
        item["turn_sha256"] for item in history[-128:]
        if isinstance(item, dict) and item.get("agent_id") == agent_id
        and isinstance(item.get("turn_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", item["turn_sha256"])
    }


def _strict_workspace_matches(event: dict[str, Any], state: dict[str, Any]) -> bool:
    cwd, expected = event.get("cwd"), state.get("workspace_sha256")
    return (
        isinstance(cwd, str) and bool(cwd.strip())
        and isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected) is not None
        and _digest(os.path.realpath(cwd)) == expected
    )


def _activation_identity(state: dict[str, Any] | None) -> tuple[str, str, str] | None:
    if not state:
        return None
    fields = tuple(state.get(key) for key in ("task_sha256", "activation_turn_sha256", "workspace_sha256"))
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in fields):
        return None
    return fields


def _valid_active_turns(state: dict[str, Any]) -> bool:
    active_turns = state.get("active_worker_turns", {})
    return isinstance(active_turns, dict) and all(
        isinstance(agent_id, str) and 0 < len(agent_id) <= 256
        and isinstance(turn_hash, str) and re.fullmatch(r"[0-9a-f]{64}", turn_hash)
        for agent_id, turn_hash in active_turns.items()
    )


def _worker_resume_needed(event: dict[str, Any], state: dict[str, Any] | None) -> bool:
    context = _worker_turn_context(event)
    if not state or state.get("status") != "armed" or context is None:
        return False
    agent_id, turn_hash = context
    if not isinstance(state.get("known_agents"), list) or not isinstance(state.get("active_agents"), list):
        return False
    history = _terminal_turn_hashes(state, agent_id)
    return (
        agent_id in state.get("known_agents", [])
        and agent_id not in state.get("active_agents", [])
        and state.get("phase") in {"workers_active", "replan_required", "decision_required"}
        and _strict_workspace_matches(event, state)
        and _activation_identity(state) is not None and _valid_active_turns(state)
        and bool(history) and turn_hash not in history
    )


def _worker_tool_denied() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "AgentFanout worker turn is unregistered, stale, cancelled, or cannot be durably resumed.",
        }
    }


def _handle_pre_tool(
    event: dict[str, Any], state: dict[str, Any] | None,
    connection: sqlite3.Connection | None = None, key: str | None = None,
    *, expected_activation: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    if expected_activation is not None and _activation_identity(state) != expected_activation:
        return _worker_tool_denied()
    if not state or state.get("status") != "armed":
        return _worker_tool_denied() if expected_activation is not None else {}
    if event.get("agent_id") is not None:
        context = _worker_turn_context(event)
        if (
            context is None or not isinstance(state.get("known_agents"), list)
            or not isinstance(state.get("active_agents"), list)
            or context[0] not in state.get("known_agents", [])
            or not _workspace_matches(event, state) or state.get("phase") == "cancel_requested"
            or context[1] in _terminal_turn_hashes(state, context[0])
            or not _valid_active_turns(state)
            or state.get("active_worker_turns", {}).get(context[0], context[1]) != context[1]
        ):
            return _worker_tool_denied()
        if context[0] not in state.get("active_agents", []):
            if not _worker_resume_needed(event, state) or connection is None or key is None:
                return _worker_tool_denied()
            # Persist ownership and invalidate the old token before allowing this
            # intercepted tool. The caller commits before emitting hook output.
            _handle_subagent_start(event, connection, key, state)
            state.setdefault("active_worker_turns", {})[context[0]] = context[1]
            _save(connection, key, state)
    elif expected_activation is not None:
        return _worker_tool_denied()
    tool_name = event.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip().lower() in CONTROL_TOOLS:
        if (state.get("phase") == "cancel_requested" or not _workspace_matches(event, state)) and tool_name.strip().lower() not in CANCELLATION_CONTROL_TOOLS:
            return _worker_tool_denied()
        return {}
    if not _workspace_matches(event, state):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "AgentFanout workspace changed during an armed task; block or cancel the lease before parent work.",
            }
        }
    if state.get("active_agents") and state.get("phase") != "cancel_requested":
        return {}
    if state.get("phase") not in {"awaiting_dispatch", "replan_required", "decision_required", "cancel_requested"}:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "AgentFanout lease requires worker dispatch or a post-batch residual-work decision before parent tool use.",
        }
    }


def _handle_stop(event: dict[str, Any], connection: sqlite3.Connection, key: str, state: dict[str, Any] | None) -> dict[str, Any]:
    if not state or state.get("status") != "armed":
        return {}
    observed = _marker(event.get("last_assistant_message"))
    token = state.get("decision_token")

    if not _workspace_matches(event, state):
        if observed and observed[0] == "blocked" and observed[1] == token and observed[3] == "workspace_changed":
            state["status"] = "blocked"
            state["phase"] = "blocked"
            state["blocked_reason"] = "workspace_changed"
            _save(connection, key, state)
            return {}
        if state.get("active_agents"):
            state["phase"] = "cancel_requested"
            state["cancel_reason"] = state.get("cancel_reason") or "workspace_changed"
        else:
            state["phase"] = "decision_required"
        state["decision_token"] = state.get("decision_token") or _token()
        token = state["decision_token"]
        result = _stop_continue(
            state,
            f"lease={token} workspace changed. Do not continue in the parent; end with "
            f"<!-- agentfanout:blocked lease={token} reason=workspace_changed -->.",
        )
        _save(connection, key, state)
        return result

    if state["active_agents"]:
        action = "interrupt active workers and wait for terminal callbacks" if state.get("phase") == "cancel_requested" else "wait"
        result = _stop_continue(
            state,
            f"lease={token or 'pending'} action={action} active_workers={len(state['active_agents'])}. "
            "Do not finish or take over their work.",
        )
        _save(connection, key, state)
        return result

    if state["phase"] == "decision_required" and observed and observed[1] == token:
        action, _, residual, reason = observed
        if action == "complete" and residual == 0 and state["workers_started"] > 0:
            state["status"] = "released"
            state["phase"] = "complete"
            state["released_reason"] = "empty_residual_after_replan"
            _save(connection, key, state)
            return {}
        if action == "blocked":
            state["status"] = "blocked"
            state["phase"] = "blocked"
            state["blocked_reason"] = reason or "worker_or_capability_blocked"
            _save(connection, key, state)
            return {}
        if action == "cancelled":
            state["status"] = "released"
            state["phase"] = "cancelled"
            state["released_reason"] = "orchestrator_cancelled"
            _save(connection, key, state)
            return {}

    if state["phase"] in {"awaiting_dispatch", "replan_required"}:
        state["phase"] = "decision_required"
    token = state.get("decision_token")
    if state["workers_started"] == 0:
        instruction = (
            f"lease={token} wave=0. Spawn a bounded worker wave now. If no approved worker can launch, "
            "if dispatch returns no non-empty receiver ID, or if no SubagentStart is observed, do not retry or wait: "
            f"end immediately with <!-- agentfanout:blocked lease={token} reason=provider_unavailable -->."
        )
    else:
        instruction = (
            f"lease={token} wave={state['wave']} batch={state['batch']}. Reconcile the completed result batch into "
            "a residual-work ledger. Delegate every eligible residual through follow-up or a fresh worker. "
            f"Only if residual work is empty, finish with <!-- agentfanout:complete lease={token} residual=0 -->. "
            f"If progress is impossible, use <!-- agentfanout:blocked lease={token} reason=capability_unavailable -->."
        )
    result = _stop_continue(state, instruction)
    _save(connection, key, state)
    return result


def _handle_session_start(state: dict[str, Any] | None) -> dict[str, Any]:
    if not state or state.get("status") != "armed":
        return {}
    return _context(
        "SessionStart",
        f"AgentFanout lease remains armed after session transition: wave {state['wave']}, phase {state['phase']}, active workers {len(state['active_agents'])}. Continue orchestration; do not take over residual worker work.",
    )


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        _json_output()
        return 0
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _json_output()
        return 0
    if not isinstance(event, dict):
        _json_output()
        return 0
    session_id = event.get("session_id")
    event_name = event.get("hook_event_name")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 512 or not isinstance(event_name, str):
        _json_output()
        return 0
    key = _digest(session_id)
    try:
        v2 = (
            os.environ.get("AGENT_FANOUT_V2_MASTER") == "1"
            and os.environ.get("AGENT_FANOUT_V2_HOOK") == "1"
        )
        readonly_handled = False
        resume_requested = False
        expected_activation = None
        if v2:
            # The common no-op and policy-check paths never open a write
            # transaction or create a lease database. A possible transition
            # below is always reloaded after BEGIN IMMEDIATE.
            if event_name not in {"UserPromptSubmit", "SubagentStart", "SubagentStop", "PreToolUse", "Stop", "SessionStart"}:
                output, readonly_handled = {}, True
            elif event_name in {"PreToolUse", "SessionStart"}:
                state = _load_readonly_state(key)
                resume_requested = event_name == "PreToolUse" and _worker_resume_needed(event, state)
                if resume_requested:
                    expected_activation = _activation_identity(state)
                if not resume_requested:
                    output = _handle_pre_tool(event, state) if event_name == "PreToolUse" else _handle_session_start(state)
                    readonly_handled = True
            elif event_name == "UserPromptSubmit":
                prompt = event.get("prompt")
                if not isinstance(prompt, str) or len(prompt.encode("utf-8", "replace")) > MAX_INPUT_BYTES:
                    output, readonly_handled = {}, True
                elif INFO_COMMAND.search(prompt) or not START_COMMAND.match(prompt):
                    state = _load_readonly_state(key)
                    if INFO_COMMAND.search(prompt) or not state or state.get("status") != "armed":
                        output = _handle_prompt(event, None, key, state)
                        readonly_handled = True
            elif event_name == "SubagentStart":
                agent_id = event.get("agent_id")
                if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 256:
                    output, readonly_handled = {}, True
            elif event_name == "SubagentStop":
                agent_id = event.get("agent_id")
                if not isinstance(agent_id, str):
                    output, readonly_handled = {}, True
                else:
                    state = _load_readonly_state(key)
                    if not state or state.get("status") != "armed" or _stop_event_digest(event, agent_id) in state.get("seen_stop_events", []):
                        output, readonly_handled = {}, True
            elif event_name == "Stop":
                state = _load_readonly_state(key)
                if not state or state.get("status") != "armed":
                    output, readonly_handled = {}, True
        if not readonly_handled:
            with _connect(maintenance=not v2) as connection:
                connection.execute("BEGIN IMMEDIATE")
                state = _load(connection, key)
                if event_name == "UserPromptSubmit":
                    output = _handle_prompt(event, connection, key, state)
                elif event_name == "SubagentStart":
                    output = _handle_subagent_start(event, connection, key, state)
                elif event_name == "SubagentStop":
                    output = _handle_subagent_stop(event, connection, key, state)
                elif event_name == "PreToolUse":
                    output = _handle_pre_tool(event, state, connection, key, expected_activation=expected_activation)
                elif event_name == "Stop":
                    output = _handle_stop(event, connection, key, state)
                elif event_name == "SessionStart":
                    output = _handle_session_start(state)
                else:
                    output = {}
                connection.commit()
    except (OSError, RuntimeError, sqlite3.Error, TypeError, KeyError, ValueError):
        output = _storage_failure_output(event_name, event)
    _json_output(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
