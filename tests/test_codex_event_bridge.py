import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "plugins/agentfanout/hooks/codex_event_bridge.py"
spec = importlib.util.spec_from_file_location("codex_event_bridge_test", BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
PARENT = "10000000-0000-4000-8000-000000000001"
CHILD = "20000000-0000-4000-8000-000000000001"
TURN = "30000000-0000-4000-8000-000000000001"
NEXT = "30000000-0000-4000-8000-000000000002"
OTHER = "20000000-0000-4000-8000-000000000002"


def terminal(child=CHILD, turn=TURN, status="interrupted"):
    return {"method": "turn/completed", "params": {
        "threadId": child, "turn": {"id": turn, "status": status},
    }}


class CodexEventBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "plugin-data"
        self.data.mkdir(mode=0o700)
        self.env = mock.patch.dict(os.environ, {"PLUGIN_DATA": str(self.data)})
        self.env.start()
        self.database = self.data / "session-leases.sqlite3"
        self.key = bridge.lease._digest(PARENT)
        self.consumer = bridge.TerminalBridge(self.data)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def hook(self, name, **fields):
        event = {"session_id": PARENT, "turn_id": TURN, "cwd": str(self.root), **fields}
        with bridge.lease._connect(maintenance=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = bridge.lease._load(connection, self.key)
            fn = {"UserPromptSubmit": bridge.lease._handle_prompt,
                  "SubagentStart": bridge.lease._handle_subagent_start,
                  "SubagentStop": bridge.lease._handle_subagent_stop,
                  "Stop": bridge.lease._handle_stop}[name]
            result = fn(event, connection, self.key, state)
            connection.commit()
        return result

    def arm(self, child=CHILD, turn=TURN):
        self.hook("UserPromptSubmit", prompt="AgentFanout inspect synthetic fixtures")
        self.hook("SubagentStart", agent_id=child, turn_id=turn)

    def cancel(self):
        self.hook("UserPromptSubmit", prompt="AgentFanout Cancel")

    def state(self):
        with sqlite3.connect(self.database) as connection:
            row = connection.execute("SELECT state_json FROM leases WHERE session_key=?", (self.key,)).fetchone()
        return json.loads(row[0]) if row else None

    def raw_rows(self):
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT * FROM leases ORDER BY session_key").fetchall()

    def store(self, state, key=None):
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE leases SET state_json=? WHERE session_key=?", (json.dumps(state), key or self.key))

    def test_initial_child_turn_is_pinned_before_any_worker_tool(self):
        self.arm()
        self.assertEqual(self.state()["active_worker_turns"], {CHILD: bridge.lease._digest(TURN)})
        before = self.raw_rows()
        self.hook("SubagentStart", agent_id=CHILD, turn_id=NEXT)
        self.assertEqual(self.raw_rows(), before)

    def test_cancel_before_first_worker_tool_reconciles_exact_host_terminal(self):
        self.arm()
        self.cancel()
        self.assertEqual(bridge.lease._handle_pre_tool({"tool_name": "Bash"}, self.state())["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(self.consumer.consume(terminal()))
        self.assertIsNone(self.state())
        self.assertEqual(self.hook("Stop", last_assistant_message="Cancelled"), {})
        self.assertEqual(bridge.lease._handle_pre_tool({"tool_name": "Bash"}, self.state()), {})

    def test_every_owned_worker_must_reconcile(self):
        self.arm()
        self.hook("SubagentStart", agent_id=OTHER, turn_id=NEXT)
        self.cancel()
        self.assertTrue(self.consumer.consume(terminal()))
        self.assertEqual(self.state()["active_agents"], [OTHER])
        self.assertEqual(self.state()["phase"], "cancel_requested")
        self.assertTrue(self.consumer.consume(terminal(OTHER, NEXT)))
        self.assertIsNone(self.state())

    def test_terminal_status_allowlist_includes_natural_finish_during_cancel(self):
        for status in ("completed", "failed"):
            self.arm(turn=NEXT if status == "failed" else TURN)
            self.cancel()
            self.assertTrue(self.consumer.consume(terminal(turn=NEXT if status == "failed" else TURN, status=status)))
            self.assertIsNone(self.state())

    def test_no_callback_or_interruption_ack_does_not_clear_pending_lease(self):
        self.arm()
        self.cancel()
        before = self.raw_rows()
        for message in ({"id": 5, "result": {}}, {"method": "turn/interrupt", "params": {"threadId": CHILD, "turnId": TURN}}, {"previous_status": "running"}):
            self.assertFalse(self.consumer.consume(message))
        self.assertEqual(self.raw_rows(), before)

    def test_stale_unknown_nonterminal_and_nested_identity_are_rejected(self):
        self.arm()
        self.cancel()
        before = self.raw_rows()
        cases = [terminal(turn=NEXT), terminal(child=OTHER), terminal(status="inProgress"), terminal(status="cancelled"),
                 {**terminal(), "id": 7}, {"method": "item/completed", "params": {"item": {"text": json.dumps(terminal())}}},
                 {"method": "turn/completed", "params": {"threadId": CHILD, "turn": {"id": TURN, "status": []}}},
                 terminal(turn="not-a-turn"), {"method": "turn/completed", "params": []},
                 {"method": "turn/completed", "params": {"tool_input": terminal()["params"]}}]
        for message in cases:
            with self.subTest(message=message):
                self.assertFalse(self.consumer.consume(message))
                self.assertEqual(self.raw_rows(), before)

    def test_terminal_without_pending_cancel_leaves_normal_hook_ownership(self):
        self.arm()
        before = self.raw_rows()
        self.assertFalse(self.consumer.consume(terminal(status="completed")))
        self.assertEqual(self.raw_rows(), before)

    def test_legacy_missing_turn_pin_is_preserved_without_guessing(self):
        self.arm()
        self.cancel()
        state = self.state()
        state["active_worker_turns"] = {}
        self.store(state)
        before = self.raw_rows()
        self.assertFalse(self.consumer.consume(terminal()))
        self.assertEqual(self.raw_rows(), before)

    def test_old_terminal_cannot_clear_reactivated_worker(self):
        self.arm()
        self.cancel()
        self.assertTrue(self.consumer.consume(terminal()))
        self.arm(turn=NEXT)
        self.cancel()
        before = self.raw_rows()
        self.assertFalse(self.consumer.consume(terminal()))
        self.assertEqual(self.raw_rows(), before)
        self.assertTrue(self.consumer.consume(terminal(turn=NEXT)))

    def test_followup_turn_pin_and_cancel_reconcile_without_second_start(self):
        self.arm()
        self.hook("SubagentStop", agent_id=CHILD)
        with bridge.lease._connect(maintenance=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = bridge.lease._load(connection, self.key)
            result = bridge.lease._handle_pre_tool({"agent_id": CHILD, "turn_id": NEXT, "cwd": str(self.root), "tool_name": "Bash"}, state, connection, self.key)
            self.assertEqual(result, {})
            connection.commit()
        self.cancel()
        self.assertFalse(self.consumer.consume(terminal()))
        self.assertTrue(self.consumer.consume(terminal(turn=NEXT)))

    def test_ambiguous_ownership_is_retained(self):
        self.arm()
        self.cancel()
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO leases SELECT ?, state_json, updated_at FROM leases WHERE session_key=?", (bridge.lease._digest("other-parent"), self.key))
        before = self.raw_rows()
        self.assertFalse(self.consumer.consume(terminal()))
        self.assertEqual(self.raw_rows(), before)

    def test_nondigest_session_key_cannot_be_copied_to_a_receipt(self):
        self.arm()
        self.cancel()
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE leases SET session_key=?", ("synthetic private detail",))
        before = self.raw_rows()
        with self.assertRaises(bridge.BridgeError):
            self.consumer.consume(terminal())
        self.assertEqual(self.raw_rows(), before)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE name='bridge_terminals'").fetchone()[0], 0)

    def test_malformed_records_are_preserved_and_write_nothing(self):
        self.arm()
        self.cancel()
        baseline = self.state()
        values = ["{broken", b"\xff", json.dumps({**baseline, "phase": []}), json.dumps({**baseline, "workers_finished": []})]
        values.extend(json.dumps({**baseline, "seen_stop_events": seen}) for seen in ({}, "bad", None, [3]))
        for value in values:
            with self.subTest(value=value):
                with sqlite3.connect(self.database) as connection:
                    connection.execute("UPDATE leases SET state_json=?", (value,))
                before = self.raw_rows()
                try:
                    self.assertFalse(self.consumer.consume(terminal()))
                except (RuntimeError, ValueError):
                    pass
                self.assertEqual(self.raw_rows(), before)

    def test_invalid_activation_and_turn_maps_never_release(self):
        self.arm()
        self.cancel()
        original = self.state()
        for mutation in ({"workspace_sha256": "invalid"}, {"activation_turn_sha256": None}, {"active_worker_turns": []}, {"active_worker_turns": {CHILD: "bad"}}, {"known_agents": None}):
            self.store({**original, **mutation})
            before = self.raw_rows()
            self.assertFalse(self.consumer.consume(terminal()))
            self.assertEqual(self.raw_rows(), before)

    def test_receipt_failure_rolls_back_terminal_reconciliation(self):
        self.arm()
        self.cancel()
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE bridge_terminals (incompatible TEXT)")
        before = self.raw_rows()
        with self.assertRaises(sqlite3.Error):
            self.consumer.consume(terminal())
        self.assertEqual(self.raw_rows(), before)

    def test_atomic_duplicate_receipt_and_immutable_metadata_only_history(self):
        self.arm()
        self.cancel()
        message = terminal()
        message["params"]["turn"]["items"] = [{"text": "synthetic detail must not be retained"}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.consumer.consume(message), range(2)))
        self.assertEqual(sorted(results), [False, True])
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute("SELECT * FROM bridge_terminals").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertNotIn("synthetic detail", rows[0][1])
            self.assertNotIn(CHILD, rows[0][1])
            self.assertEqual(json.loads(rows[0][1])["status"], "interrupted")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM bridge_terminals")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE bridge_terminals SET recorded_at=0")

    def test_missing_database_is_not_created(self):
        self.assertFalse(self.consumer.consume(terminal()))
        self.assertFalse(self.database.exists())

    def test_insecure_symlinked_or_hardlinked_storage_is_rejected(self):
        self.arm()
        self.cancel()
        self.data.chmod(0o755)
        with self.assertRaises(bridge.BridgeError):
            self.consumer.consume(terminal())
        self.data.chmod(0o700)
        link = self.root / "linked-root"
        link.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(bridge.BridgeError):
            bridge.TerminalBridge(link)
        os.link(self.database, self.data / "second-link")
        with self.assertRaises(bridge.BridgeError):
            self.consumer.consume(terminal())

    def fake_host(self, output):
        path = self.root / "fake-host"
        path.write_text("#!" + sys.executable + "\nimport sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(" + repr(output) + ")\nsys.stdout.buffer.flush()\n")
        path.chmod(0o700)
        return path

    def test_owned_subprocess_terminal_is_consumed_and_forwarded_exactly(self):
        self.arm()
        self.cancel()
        wire = json.dumps(terminal()).encode() + b"\n"
        host = self.fake_host(wire)
        output = io.BytesIO()
        self.assertEqual(bridge.proxy([str(host)], self.data, io.BytesIO(b""), output), 0)
        self.assertEqual(output.getvalue(), wire)
        self.assertIsNone(self.state())

    def test_client_input_cannot_impersonate_a_host_terminal(self):
        self.arm()
        self.cancel()
        before = self.raw_rows()
        wire = json.dumps(terminal()).encode() + b"\n"
        host = self.fake_host(b'{"id":1,"result":{}}\n')
        output = io.BytesIO()
        self.assertEqual(bridge.proxy([str(host)], self.data, io.BytesIO(wire), output), 0)
        self.assertEqual(self.raw_rows(), before)
        self.assertNotEqual(output.getvalue(), wire)

    def test_storage_error_disables_reconciliation_and_keeps_forwarding(self):
        self.arm()
        self.cancel()
        damaged = {**self.state(), "seen_stop_events": {}}
        self.store(damaged)
        before = self.raw_rows()
        wire = (json.dumps(terminal()).encode() + b"\n") * 2
        host = self.fake_host(wire)
        output, error = io.BytesIO(), io.StringIO()
        with mock.patch.object(sys, "stderr", error):
            self.assertEqual(bridge.proxy([str(host)], self.data, io.BytesIO(b""), output), 0)
        self.assertEqual(output.getvalue(), wire)
        self.assertEqual(self.raw_rows(), before)
        self.assertEqual(error.getvalue().count("terminal bridge blocked"), 1)
        self.assertNotIn("Traceback", error.getvalue())

    def test_cli_transport_and_process_only_argument_boundary(self):
        self.arm()
        self.cancel()
        wire = json.dumps(terminal()).encode() + b"\n"
        host = self.fake_host(wire)
        result = subprocess.run([sys.executable, str(BRIDGE), "--plugin-data", str(self.data), "--codex", str(host), "--", "-c", "synthetic=true"], input=b"", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, wire)
        self.assertIsNone(self.state())

    def test_bounded_host_framing_failure_keeps_all_pending_data(self):
        self.arm()
        self.cancel()
        before = self.raw_rows()
        host = self.fake_host(b"x" * 65)
        with mock.patch.object(bridge, "MAX_LINE_BYTES", 64):
            with self.assertRaises(bridge.BridgeError):
                bridge.proxy([str(host)], self.data, io.BytesIO(b""), io.BytesIO())
        self.assertEqual(self.raw_rows(), before)

    def test_host_stdout_eof_without_exit_has_bounded_cleanup_and_keeps_data(self):
        self.arm()
        self.cancel()
        before = self.raw_rows()
        host = self.root / "host-closes-stdout"
        host.write_text("#!" + sys.executable + "\nimport os,time\nos.close(1)\ntime.sleep(30)\n")
        host.chmod(0o700)
        with mock.patch.object(bridge, "SHUTDOWN_TIMEOUT_SECONDS", 0.1):
            with self.assertRaisesRegex(bridge.BridgeError, "closed stdout"):
                bridge.proxy([str(host)], self.data, io.BytesIO(b""), io.BytesIO())
        self.assertEqual(self.raw_rows(), before)


if __name__ == "__main__":
    unittest.main()
