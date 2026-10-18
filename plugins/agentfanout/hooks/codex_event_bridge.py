#!/usr/bin/env python3
"""Proxy an owned Codex app-server pipe and reconcile exact cancelled turns.

Only this process's child stdout is an event authority. Client input, tool
responses and model text are forwarded without becoming reconciliation input.
The bridge has no network listener, transcript reader or event-import command.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_LEASES = 512
SHUTDOWN_TIMEOUT_SECONDS = 5
TERMINAL_STATUSES = frozenset({"interrupted", "completed", "failed"})
spec = importlib.util.spec_from_file_location(
    "agentfanout_bridge_lease", Path(__file__).with_name("agentfanout_hook.py")
)
assert spec is not None and spec.loader is not None
lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease)


class BridgeError(RuntimeError):
    """The bridge cannot establish its host or storage boundary."""


def _uuid(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _private_path(path: Path, *, directory: bool) -> None:
    if not path.is_absolute() or path.resolve() != path:
        raise BridgeError("Private storage must use an absolute, non-symlink path")
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
        raise BridgeError("Private storage has an unsafe file type")
    if os.name == "posix" and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise BridgeError("Private storage ownership or permissions are unsafe")


class TerminalBridge:
    """Host-side consumer; callers must supply only owned-pipe notifications.

    This is not an untrusted event validation API. The CLI never accepts events
    from its client stdin, a socket, a file, or a model tool.
    """

    def __init__(self, data_root: Path):
        self.data_root = data_root
        _private_path(data_root, directory=True)

    def consume(self, message: Any) -> bool:
        if (
            not isinstance(message, dict) or "id" in message
            or message.get("method") != "turn/completed"
        ):
            return False
        params = message.get("params")
        if not isinstance(params, dict):
            return False
        child, turn = params.get("threadId"), params.get("turn")
        if not _uuid(child) or not isinstance(turn, dict):
            return False
        turn_id, status = turn.get("id"), turn.get("status")
        if not _uuid(turn_id) or not isinstance(status, str) or status not in TERMINAL_STATUSES:
            return False
        _private_path(self.data_root, directory=True)
        database = self.data_root / "session-leases.sqlite3"
        if not database.exists() and not database.is_symlink():
            return False
        _private_path(database, directory=False)
        # mode=rw never creates a replacement database when storage is missing.
        connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5.0, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT session_key, state_json FROM leases LIMIT ?", (MAX_LEASES + 1,)
            ).fetchall()
            if len(rows) > MAX_LEASES:
                return False
            matches = []
            turn_hash = lease._digest(turn_id)
            for key, encoded in rows:
                if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
                    raise BridgeError("Lease session identity cannot be classified")
                state = json.loads(encoded)
                if lease._active_ownership(state) is None:
                    raise BridgeError("Lease ownership cannot be classified")
                if child in state["active_agents"]:
                    matches.append((key, state))
            # Ambiguous ownership, a missing pin, or a stale turn is not proof.
            if len(matches) != 1:
                return False
            key, state = matches[0]
            activation = lease._activation_identity(state)
            if (
                state.get("status") != "armed" or state.get("phase") != "cancel_requested"
                or activation is None or not lease._valid_active_turns(state)
                or state.get("active_worker_turns", {}).get(child) != turn_hash
                or not isinstance(state.get("known_agents"), list)
                or child not in state["known_agents"]
                or any(type(state.get(name)) is not int or state[name] < 0 for name in ("workers_started", "workers_finished"))
            ):
                return False
            event = {"agent_id": child, "turn_id": turn_id}
            lease._handle_subagent_stop(event, connection, key, state)
            if child in state["active_agents"]:
                return False
            # Preserve only identity digests and the terminal status, never the
            # event's items, errors, messages, paths or provider/account data.
            receipt = {
                "schema": "CodexBridgeTerminalV1", "session_key": key,
                "task_sha256": activation[0], "activation_turn_sha256": activation[1],
                "workspace_sha256": activation[2], "agent_sha256": lease._digest(child),
                "turn_sha256": turn_hash, "status": status,
            }
            encoded_receipt = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
            receipt_key = lease._digest(encoded_receipt)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bridge_terminals ("
                "receipt_key TEXT PRIMARY KEY NOT NULL, receipt_json TEXT NOT NULL, recorded_at INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS bridge_terminals_no_update BEFORE UPDATE ON bridge_terminals "
                "BEGIN SELECT RAISE(ABORT, 'terminal receipts are immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS bridge_terminals_no_delete BEFORE DELETE ON bridge_terminals "
                "BEGIN SELECT RAISE(ABORT, 'terminal receipts are immutable'); END"
            )
            connection.execute(
                "INSERT INTO bridge_terminals VALUES (?, ?, ?)",
                (receipt_key, encoded_receipt, int(time.time())),
            )
            connection.commit()
            return True
        finally:
            # Includes rejected identities, partial writes and storage failures.
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def proxy(command: list[str], data_root: Path, input_stream: Any, output_stream: Any) -> int:
    """Keep client and host channels separate; do not retain their contents."""
    bridge = TerminalBridge(data_root)
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        assert process.stdin is not None and process.stdout is not None

        def client_to_host() -> None:
            try:
                while True:
                    chunk = input_stream.read1(65536) if hasattr(input_stream, "read1") else input_stream.read(65536)
                    if not chunk:
                        break
                    process.stdin.write(chunk)
                    process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

        sender = threading.Thread(target=client_to_host, daemon=True)
        sender.start()
        disabled = False
        try:
            while True:
                line = process.stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    raise BridgeError("Owned host stream exceeded its framing bound")
                if not disabled:
                    try:
                        bridge.consume(json.loads(line))
                    except (OSError, RuntimeError, sqlite3.Error, TypeError, KeyError, ValueError):
                        # Fail closed: forward host output, preserve leases, and
                        # stop reconciliation for this process after an error.
                        disabled = True
                        print("AgentFanout terminal bridge blocked; unresolved ownership is retained.", file=sys.stderr)
                output_stream.write(line)
                output_stream.flush()
            try:
                return process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise BridgeError("Owned host closed stdout without exiting") from exc
        finally:
            if process.poll() is None:
                # This owns only the server process, not remote jobs or detached
                # descendants. Absence of terminal events leaves leases intact.
                process.terminate()
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            sender.join(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-data", type=Path, required=True, help="Existing private data root of this exact installed plugin")
    parser.add_argument("--codex", type=Path, required=True, help="Reviewed absolute Codex executable path")
    parser.add_argument("codex_options", nargs=argparse.REMAINDER, help="Process-only Codex options after --")
    args = parser.parse_args()
    if not args.codex.is_absolute() or not args.codex.is_file() or not os.access(args.codex, os.X_OK):
        parser.error("--codex must identify a reviewed absolute executable")
    options = args.codex_options
    if options[:1] == ["--"]:
        options = options[1:]
    try:
        return proxy([str(args.codex), *options, "app-server", "--listen", "stdio://"], args.plugin_data, sys.stdin.buffer, sys.stdout.buffer)
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        print("AgentFanout terminal bridge unavailable; no terminal success is inferred.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
