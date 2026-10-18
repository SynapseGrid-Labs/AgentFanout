import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "agentfanout" / "hooks" / "agentfanout_hook.py"
HOOKS_JSON = ROOT / "plugins" / "agentfanout" / "hooks" / "hooks.json"
PLUGIN_JSON = ROOT / "plugins" / "agentfanout" / ".codex-plugin" / "plugin.json"
TOKEN = re.compile(r"lease=([0-9a-f]{32})")


class CodexPluginHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        self.env["PLUGIN_DATA"] = self.temp.name
        self.session = "session-fixture"

    def tearDown(self):
        self.temp.cleanup()

    def call(self, event_name, **fields):
        result = self.call_raw(event_name, **fields)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout or "{}")

    def call_raw(self, event_name, **fields):
        payload = {
            "session_id": self.session,
            "turn_id": fields.pop("turn_id", "turn-fixture"),
            "hook_event_name": event_name,
            "cwd": str(ROOT),
            "model": "fixture-model",
            **fields,
        }
        result = subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
            timeout=10,
        )
        return result

    def arm(self, prompt="AgentFanout Codex inspect these independent files"):
        result = self.call("UserPromptSubmit", prompt=prompt)
        self.assertIn("lease armed", json.dumps(result).lower())
        return result

    def stop(self, message=""):
        return self.call("Stop", last_assistant_message=message, stop_hook_active=False)

    def token_from(self, result):
        match = TOKEN.search(result.get("reason", ""))
        self.assertIsNotNone(match, result)
        return match.group(1)

    def test_unrelated_prompt_is_invisible_and_does_not_arm(self):
        self.assertEqual(self.call("UserPromptSubmit", prompt="Fix this small typo"), {})
        self.assertEqual(self.call("PreToolUse", tool_name="Bash", tool_input={"command": "pwd"}), {})
        self.assertEqual(self.stop("Done"), {})

    def test_help_and_status_do_not_arm(self):
        help_result = self.call("UserPromptSubmit", prompt="AgentFanout Help")
        self.assertIn("no active", json.dumps(help_result).lower())
        self.assertEqual(self.call("PreToolUse", tool_name="apply_patch", tool_input={"command": "x"}), {})

    def test_mentions_negation_and_questions_do_not_arm(self):
        prompts = (
            "Do not use AgentFanout for this task",
            "How do I use AgentFanout?",
            "The phrase use AgentFanout is forbidden",
            "Please do not disable AgentFanout later",
        )
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                self.session = f"negative-{index}"
                self.assertEqual(self.call("UserPromptSubmit", prompt=prompt), {})
                self.assertEqual(self.call("PreToolUse", tool_name="Bash", tool_input={}), {})

    def test_task_text_starting_with_stop_or_status_is_not_a_control_command(self):
        for index, prompt in enumerate(("AgentFanout stop the infinite loop in these modules", "AgentFanout status page API and UI review")):
            with self.subTest(prompt=prompt):
                self.session = f"task-collision-{index}"
                result = self.call("UserPromptSubmit", prompt=prompt)
                self.assertIn("lease armed", json.dumps(result).lower())

    def test_activation_denies_parent_tool_before_first_worker(self):
        self.arm()
        denied = self.call("PreToolUse", tool_name="Bash", tool_input={"command": "pwd"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.call("PreToolUse", tool_name="collaboration.spawn_agent", tool_input={}), {})

    def test_multiline_invocations_arm_and_keep_parent_work_gated(self):
        prompts = (
            "AgentFanout Codex compare these plans.\n\nCheck capacity and accessibility.",
            "AgentFanout Codex: Compare these plans.\r\n\r\nCheck capacity and accessibility.",
            "Use AgentFanout: Compare these plans.\n- Check capacity.\n- Check accessibility.",
        )
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                self.session = f"multiline-invocation-{index}"
                self.arm(prompt)
                status = self.call("UserPromptSubmit", prompt="AgentFanout Status")
                self.assertIn("lease armed", json.dumps(status).lower())
                before_dispatch = self.call("PreToolUse", tool_name="Bash", tool_input={"command": "pwd"})
                self.assertEqual(before_dispatch["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(self.call("PreToolUse", tool_name="collaboration.spawn_agent", tool_input={}), {})
                self.call("SubagentStart", agent_id="fixture-worker", agent_type="worker")
                self.call("SubagentStop", agent_id="fixture-worker", last_assistant_message="bounded result")
                after_batch = self.call("PreToolUse", tool_name="apply_patch", tool_input={"command": "edit"})
                self.assertEqual(after_batch["hookSpecificOutput"]["permissionDecision"], "deny")
                decision = self.stop("Worker completed; reconcile residual work")
                self.assertEqual(decision["decision"], "block")
                token = self.token_from(decision)
                self.assertEqual(self.stop(f"Done. <!-- agentfanout:complete lease={token} residual=0 -->"), {})

    def test_multiline_mentions_and_negations_do_not_arm(self):
        prompts = (
            "Do not use AgentFanout.\nCompare these plans directly.",
            "Explain this example:\nAgentFanout Codex compare these plans.",
            "How do I use AgentFanout?\nDo not launch workers.",
        )
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=prompt):
                self.session = f"multiline-non-invocation-{index}"
                self.assertEqual(self.call("UserPromptSubmit", prompt=prompt), {})
                self.assertEqual(self.call("PreToolUse", tool_name="Bash", tool_input={}), {})

    def test_native_control_tool_aliases_remain_available(self):
        self.arm()
        for tool_name in (
            "spawn", "wait", "follow_up", "send_input", "interrupt", "cancel",
            "collaborationspawn_agent", "collaborationfollowup_task",
            "collaborationsend_message", "collaborationwait_agent",
            "collaborationlist_agents", "collaborationinterrupt_agent",
        ):
            with self.subTest(tool_name=tool_name):
                self.assertEqual(self.call("PreToolUse", tool_name=tool_name, tool_input={}), {})

    def test_unrelated_mcp_tools_cannot_impersonate_orchestration_controls(self):
        self.arm()
        for tool_name in (
            "mcp__billing__cancel", "mcp__danger__agent.delete", "mcp__foo__wait",
            "mcp__foo__collaborationspawn_agent", "collaborationspawn_agent.delete",
        ):
            with self.subTest(tool_name=tool_name):
                denied = self.call("PreToolUse", tool_name=tool_name, tool_input={})
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_worker_tools_are_not_blocked_while_any_worker_is_active(self):
        self.arm()
        worker = self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.assertIn("agentfanout worker", json.dumps(worker).lower())
        self.assertEqual(self.call("PreToolUse", tool_name="Bash", tool_input={"command": "pwd"}), {})

    def test_first_batch_forces_distinct_replan_then_allows_completion(self):
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.call("SubagentStop", agent_id="agent-a", agent_type="worker", last_assistant_message="result")
        first_stop = self.stop("Worker finished; I am done")
        self.assertEqual(first_stop["decision"], "block")
        self.assertIn("residual-work ledger", first_stop["reason"])
        token = self.token_from(first_stop)
        denied = self.call("PreToolUse", tool_name="apply_patch", tool_input={"command": "edit"})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        marker = f"Final synthesis. <!-- agentfanout:complete lease={token} residual=0 -->"
        self.assertEqual(self.stop(marker), {})
        self.assertEqual(self.call("PreToolUse", tool_name="apply_patch", tool_input={"command": "edit"}), {})

    def test_completion_before_post_batch_replan_is_rejected(self):
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.call("SubagentStop", agent_id="agent-a", agent_type="worker", last_assistant_message="result")
        premature = self.stop("<!-- agentfanout:complete lease=00000000000000000000000000000000 residual=0 -->")
        self.assertEqual(premature["decision"], "block")

    def test_new_wave_invalidates_prior_decision_token(self):
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.call("SubagentStop", agent_id="agent-a", agent_type="worker", last_assistant_message="residual")
        first_token = self.token_from(self.stop())
        self.call("SubagentStart", agent_id="agent-b", agent_type="worker")
        self.call("SubagentStop", agent_id="agent-b", agent_type="worker", last_assistant_message="complete")
        stale = self.stop(f"<!-- agentfanout:complete lease={first_token} residual=0 -->")
        second_token = self.token_from(stale)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(self.stop(f"<!-- agentfanout:complete lease={second_token} residual=0 -->"), {})

    def test_same_worker_followup_without_second_start_rotates_batch_token(self):
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker", turn_id="worker-turn-1")
        self.call("SubagentStop", agent_id="agent-a", agent_type="worker", turn_id="worker-turn-1", last_assistant_message="partial")
        first_token = self.token_from(self.stop())
        self.call("SubagentStop", agent_id="agent-a", agent_type="worker", turn_id="worker-turn-2", last_assistant_message="follow-up complete")
        stale = self.stop(f"<!-- agentfanout:complete lease={first_token} residual=0 -->")
        second_token = self.token_from(stale)
        self.assertNotEqual(first_token, second_token)
        status = self.call("UserPromptSubmit", prompt="AgentFanout Status")
        self.assertIn("wave 2", json.dumps(status).lower())
        self.assertEqual(self.stop(f"<!-- agentfanout:complete lease={second_token} residual=0 -->"), {})

    def lease_state(self):
        with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
            row = connection.execute(
                "SELECT state_json FROM leases WHERE session_key = ?",
                (hashlib.sha256(self.session.encode()).hexdigest(),),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def test_known_worker_followup_is_persisted_before_its_tool(self):
        for enabled_v2 in (False, True):
            with self.subTest(v2=enabled_v2):
                self.session = f"worker-followup-{enabled_v2}"
                self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                self.arm()
                self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="partial")
                prior = self.lease_state()
                parent = self.call("PreToolUse", tool_name="Bash", tool_input={"command": "pwd"})
                self.assertEqual(parent["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-2", tool_name="Bash", tool_input={"command": "pwd"}), {})
                resumed = self.lease_state()
                self.assertEqual(resumed["active_agents"], ["worker-a"])
                self.assertEqual(resumed["phase"], "workers_active")
                self.assertIsNone(resumed["decision_token"])
                self.assertEqual(resumed["workers_started"], 1)
                self.assertEqual(resumed["wave"], 2)
                self.assertEqual(self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-2", tool_name="Bash", tool_input={}), {})
                self.assertEqual(self.lease_state(), resumed)
                waiting = self.stop(f"<!-- agentfanout:complete lease={prior['decision_token']} residual=0 -->")
                self.assertEqual(waiting["decision"], "block")
                self.assertIn("active_workers=1", waiting["reason"])
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-2", last_assistant_message="clarified")
                finished = self.lease_state()
                self.assertEqual(finished["active_agents"], [])
                self.assertEqual(finished["workers_finished"], 2)
                self.assertNotEqual(finished["decision_token"], prior["decision_token"])
                after = self.call("PreToolUse", tool_name="Bash", tool_input={})
                self.assertEqual(after["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_followup_rejects_stale_unknown_and_nested_worker_identity(self):
        self.arm()
        self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
        self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="partial")
        before = self.lease_state()
        attempts = (
            {"agent_id": "worker-a", "turn_id": "worker-turn-1"},
            {"agent_id": "unknown-worker", "turn_id": "worker-turn-2"},
            {"agent_id": "worker-a", "turn_id": ""},
            {"agent_id": "worker-a", "turn_id": "x" * 513},
            {"tool_input": {"agent_id": "worker-a", "turn_id": "worker-turn-2"}},
            {"agent_id": "worker-a", "turn_id": "worker-turn-2", "cwd": "/different/project"},
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                denied = self.call("PreToolUse", tool_name="Bash", **attempt)
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(self.lease_state(), before)

    def test_cancel_requested_worker_cannot_reactivate(self):
        self.arm()
        self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
        self.call("SubagentStart", agent_id="worker-b")
        self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="partial")
        self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
        before = self.lease_state()
        denied = self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-2", tool_name="Bash", tool_input={})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.lease_state(), before)
        self.assertEqual(before["active_agents"], ["worker-b"])
        self.assertEqual(before["phase"], "cancel_requested")

    def test_inactive_legacy_worker_without_turn_fence_is_not_readmitted(self):
        self.arm()
        self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
        self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="partial")
        baseline = self.lease_state()
        variants = (
            {key: value for key, value in baseline.items() if key != "terminal_worker_turns"},
            {**baseline, "terminal_worker_turns": [{"agent_id": "worker-a", "turn_sha256": "invalid"}]},
            {**baseline, "known_agents": None},
            {**baseline, "active_agents": None},
            {**baseline, "active_worker_turns": []},
            {**baseline, "active_worker_turns": {"worker-a": "invalid"}},
        )
        for state in variants:
            with self.subTest(state=state):
                with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                    connection.execute("UPDATE leases SET state_json = ?", (json.dumps(state),))
                denied = self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-2", tool_name="Bash", tool_input={})
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(self.lease_state(), state)

    def test_resumed_worker_rejects_stale_and_mismatched_terminal_turns(self):
        for enabled_v2 in (False, True):
            with self.subTest(v2=enabled_v2):
                self.session = f"terminal-fence-{enabled_v2}"
                self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                self.arm()
                self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="partial")
                self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-2", tool_name="Bash")
                active = self.lease_state()
                for turn in ("worker-turn-1", "unobserved-turn", ""):
                    self.call("SubagentStop", agent_id="worker-a", turn_id=turn, last_assistant_message="changed terminal text")
                    self.assertEqual(self.lease_state(), active)
                denied = self.call("PreToolUse", agent_id="worker-a", turn_id="unobserved-turn", tool_name="Bash")
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(self.lease_state(), active)
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-2", last_assistant_message="done")
                terminal = self.lease_state()
                self.assertEqual(terminal["active_agents"], [])
                self.assertEqual(terminal["workers_finished"], 2)
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-2", last_assistant_message="different done")
                self.assertEqual(self.lease_state(), terminal)

    def test_resume_requires_explicit_valid_workspace_identity(self):
        for enabled_v2 in (False, True):
            self.session = f"workspace-fence-{enabled_v2}"
            self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
            self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
            self.arm()
            self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
            self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="partial")
            baseline = self.lease_state()
            for cwd in (None, "", 17):
                with self.subTest(v2=enabled_v2, cwd=cwd):
                    denied = self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-2", cwd=cwd, tool_name="Bash")
                    self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                    self.assertEqual(self.lease_state(), baseline)
            for digest in (None, "", "invalid", 17):
                with self.subTest(v2=enabled_v2, digest=digest):
                    malformed = {**baseline, "workspace_sha256": digest}
                    with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                        connection.execute("UPDATE leases SET state_json = ? WHERE session_key = ?", (json.dumps(malformed), hashlib.sha256(self.session.encode()).hexdigest()))
                    denied = self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-2", tool_name="Bash")
                    self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                    self.assertEqual(self.lease_state(), malformed)

    def test_v2_resume_rechecks_activation_identity_after_reload(self):
        spec = importlib.util.spec_from_file_location("fixture_agentfanout_hook", HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        self.env["AGENT_FANOUT_V2_MASTER"] = "1"
        self.env["AGENT_FANOUT_V2_HOOK"] = "1"
        for field in ("task_sha256", "activation_turn_sha256"):
            with self.subTest(field=field):
                self.session = f"reload-fence-{field}"
                self.arm()
                self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="partial")
                snapshot = self.lease_state()
                replacement = {**snapshot, field: hashlib.sha256(b"replacement").hexdigest()}
                with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                    connection.execute("UPDATE leases SET state_json = ? WHERE session_key = ?", (json.dumps(replacement), hashlib.sha256(self.session.encode()).hexdigest()))
                event = {"session_id": self.session, "hook_event_name": "PreToolUse", "agent_id": "worker-a", "turn_id": "worker-turn-2", "cwd": str(ROOT), "tool_name": "Bash"}
                incoming, outgoing = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode())), io.StringIO()
                try:
                    with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(hook, "_load_readonly_state", return_value=snapshot), mock.patch.object(hook.sys, "stdin", incoming), mock.patch.object(hook.sys, "stdout", outgoing):
                        self.assertEqual(hook.main(), 0)
                finally:
                    incoming.close()
                denied = json.loads(outgoing.getvalue())
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(self.lease_state(), replacement)

    def test_continuation_ceiling_retains_unresolved_worker_ownership(self):
        for enabled_v2 in (False, True):
            for cancelled in (False, True):
                with self.subTest(v2=enabled_v2, cancelled=cancelled):
                    self.session = f"retained-ceiling-{enabled_v2}-{cancelled}"
                    self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                    self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                    self.arm()
                    self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                    if cancelled:
                        self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
                    baseline = self.lease_state()
                    baseline["continuations"] = baseline["max_continuations"]
                    with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                        connection.execute("UPDATE leases SET state_json = ? WHERE session_key = ?", (json.dumps(baseline), hashlib.sha256(self.session.encode()).hexdigest()))
                    ceiling = self.stop("Unresolved worker")
                    self.assertIs(ceiling.get("continue"), True)
                    self.assertIn("blocked", ceiling["systemMessage"].lower())
                    state = self.lease_state()
                    self.assertIsNotNone(state)
                    self.assertEqual(state["status"], "armed")
                    self.assertEqual(state["phase"], "cancel_requested")
                    self.assertEqual(state["active_agents"], ["worker-a"])
                    self.assertEqual(state["known_agents"], ["worker-a"])
                    self.assertEqual(state["workers_finished"], 0)
                    self.assertEqual(state["blocked_reason"], "continuation_ceiling")
                    self.stop("Still unresolved")
                    self.assertEqual(self.lease_state()["active_agents"], ["worker-a"])
                    for fields in ({}, {"agent_id": "worker-a", "turn_id": "worker-turn-1"}):
                        denied = self.call("PreToolUse", tool_name="Bash", **fields)
                        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                    self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="Host terminal callback")
                    self.assertIsNone(self.lease_state())

    def test_cancel_fences_new_assignments_and_retains_late_worker(self):
        for enabled_v2 in (False, True):
            with self.subTest(v2=enabled_v2):
                self.session = f"cancel-admission-{enabled_v2}"
                self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                self.arm()
                self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
                baseline = self.lease_state()
                for name in ("spawn_agent", "collaborationspawn_agent", "followup_task", "collaborationfollowup_task", "send_input", "collaborationsend_message"):
                    denied = self.call("PreToolUse", tool_name=name)
                    self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                    self.assertEqual(self.lease_state(), baseline)
                for name in ("wait_agent", "list_agents", "interrupt_agent", "collaborationinterrupt_agent"):
                    self.assertEqual(self.call("PreToolUse", tool_name=name), {})
                self.call("SubagentStart", agent_id="late-worker", turn_id="late-turn")
                retained = self.lease_state()
                self.assertEqual(retained["phase"], "cancel_requested")
                self.assertEqual(retained["active_agents"], ["worker-a", "late-worker"])
                self.assertEqual(retained["workers_started"], 2)
                duplicate = self.call("SubagentStart", agent_id="late-worker", turn_id="late-turn")
                self.assertIn("cancellation", json.dumps(duplicate).lower())
                self.assertEqual(self.lease_state(), retained)
                denied = self.call("PreToolUse", agent_id="late-worker", turn_id="late-turn", tool_name="Bash")
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="terminal")
                self.assertEqual(self.lease_state()["active_agents"], ["late-worker"])
                self.call("SubagentStop", agent_id="late-worker", turn_id="late-turn", last_assistant_message="terminal")
                self.assertIsNone(self.lease_state())

    def test_expired_pending_workers_remain_fenced_until_terminal_callback(self):
        for enabled_v2 in (False, True):
            with self.subTest(v2=enabled_v2):
                self.session = f"expired-active-{enabled_v2}"
                self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                self.arm()
                self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
                old = self.lease_state()
                old["updated_at"] = 1
                with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                    connection.execute("UPDATE leases SET state_json = ?, updated_at = 1 WHERE session_key = ?", (json.dumps(old), hashlib.sha256(self.session.encode()).hexdigest()))
                denied = self.call("PreToolUse", tool_name="Bash")
                self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                retained = self.lease_state()
                self.assertIsNotNone(retained)
                self.assertEqual(retained["active_agents"], ["worker-a"])
                if enabled_v2:
                    self.assertEqual(retained, old)
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="Host terminal callback")
                self.assertIsNone(self.lease_state())

    def test_expired_inactive_lease_does_not_block_a_new_task(self):
        for enabled_v2 in (False, True):
            with self.subTest(v2=enabled_v2):
                self.session = f"expired-inactive-{enabled_v2}"
                self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                self.arm()
                old = self.lease_state()
                with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                    connection.execute("UPDATE leases SET updated_at = 1 WHERE session_key = ?", (hashlib.sha256(self.session.encode()).hexdigest(),))
                self.assertEqual(self.call("PreToolUse", tool_name="Bash"), {})
                self.arm("AgentFanout Codex a different synthetic task")
                self.assertNotEqual(self.lease_state()["task_sha256"], old["task_sha256"])

    def test_workspace_block_cannot_discard_active_worker_ownership(self):
        for enabled_v2 in (False, True):
            with self.subTest(v2=enabled_v2):
                self.session = f"workspace-active-retention-{enabled_v2}"
                self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                self.arm()
                self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                challenge = self.call("Stop", cwd="/different/project", last_assistant_message="", stop_hook_active=False)
                token = self.token_from(challenge)
                self.call("Stop", cwd="/different/project", last_assistant_message=f"<!-- agentfanout:blocked lease={token} reason=workspace_changed -->", stop_hook_active=False)
                retained = self.lease_state()
                self.assertIsNotNone(retained)
                self.assertEqual(retained["status"], "armed")
                self.assertEqual(retained["phase"], "cancel_requested")
                self.assertEqual(retained["active_agents"], ["worker-a"])
                self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="Host terminal callback")
                self.assertIsNone(self.lease_state())

    def test_workspace_stop_does_not_reopen_pending_cancellation(self):
        for enabled_v2 in (False, True):
            for cancelled in (False, True):
                with self.subTest(v2=enabled_v2, cancelled=cancelled):
                    self.session = f"cancel-workspace-fence-{enabled_v2}-{cancelled}"
                    self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
                    self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
                    self.arm()
                    self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
                    if cancelled:
                        self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
                    denied = self.call("PreToolUse", cwd="/different/project", tool_name="spawn_agent")
                    self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                    self.call("Stop", cwd="/different/project", last_assistant_message="not complete", stop_hook_active=False)
                    retained = self.lease_state()
                    self.assertEqual(retained["status"], "armed")
                    self.assertEqual(retained["phase"], "cancel_requested")
                    self.assertEqual(retained["active_agents"], ["worker-a"])
                    self.assertEqual(retained["cancel_reason"], "user_cancelled" if cancelled else "workspace_changed")
                    for name in ("spawn_agent", "followup_task", "send_input"):
                        output = self.call("PreToolUse", tool_name=name)
                        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
                    worker = self.call("PreToolUse", agent_id="worker-a", turn_id="worker-turn-1", tool_name="Bash")
                    self.assertEqual(worker["hookSpecificOutput"]["permissionDecision"], "deny")
                    self.assertEqual(self.lease_state(), retained)
                    self.call("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1", last_assistant_message="terminal")
                    self.assertIsNone(self.lease_state())

    def test_active_workers_force_wait_instead_of_parent_finish(self):
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        result = self.stop("I will finish this myself")
        self.assertEqual(result["decision"], "block")
        self.assertIn("active_workers=1", result["reason"])

    def test_provider_unavailable_can_end_as_typed_block(self):
        self.arm()
        decision = self.stop("No worker available")
        token = self.token_from(decision)
        marker = f"Unable to launch. <!-- agentfanout:blocked lease={token} reason=provider_unavailable -->"
        self.assertEqual(self.stop(marker), {})
        self.assertEqual(self.stop("stopped"), {})

    def test_failed_dispatch_instruction_is_fail_fast(self):
        self.arm()
        decision = self.stop("launch returned no receiver")
        self.assertIn("do not retry or wait", decision["reason"])
        self.assertIn("provider_unavailable", decision["reason"])

    def test_cancel_releases_active_lease(self):
        self.arm()
        result = self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
        self.assertIn("cancelled", json.dumps(result).lower())
        self.assertEqual(self.stop("cancelled"), {})

    def test_active_cancel_requires_interrupt_and_reconciles_all_workers(self):
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.call("SubagentStart", agent_id="agent-b", agent_type="worker")
        result = self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
        self.assertIn("interrupt active workers", json.dumps(result).lower())
        denied = self.call("PreToolUse", tool_name="Bash", tool_input={})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.call("SubagentStop", agent_id="agent-a", last_assistant_message="cancelled-a")
        reconciled = self.call("SubagentStop", agent_id="agent-b", last_assistant_message="cancelled-b")
        self.assertIn("released", json.dumps(reconciled).lower())
        self.assertEqual(self.stop("cancelled"), {})

    def test_second_activation_cannot_replace_live_lease(self):
        self.arm("AgentFanout Codex first task")
        self.call("SubagentStart", agent_id="agent-old", agent_type="worker")
        result = self.call("UserPromptSubmit", prompt="AgentFanout Codex second task")
        self.assertIn("already active", json.dumps(result).lower())
        status = self.call("UserPromptSubmit", prompt="AgentFanout Status")
        self.assertIn("active workers 1", json.dumps(status).lower())

    def test_unrelated_user_prompt_releases_idle_lease_but_cancels_active_lease(self):
        self.arm()
        released = self.call("UserPromptSubmit", prompt="Now answer this unrelated question")
        self.assertIn("released", json.dumps(released).lower())
        self.assertEqual(self.call("PreToolUse", tool_name="Bash", tool_input={}), {})
        self.session = "active-replacement"
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        cancellation = self.call("UserPromptSubmit", prompt="Now answer this unrelated question")
        self.assertIn("interrupt active workers", json.dumps(cancellation).lower())
        denied = self.call("PreToolUse", tool_name="Bash", tool_input={})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_workspace_change_fails_closed(self):
        self.arm()
        denied = self.call("PreToolUse", cwd="/different/project", tool_name="Bash", tool_input={})
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        decision = self.call("Stop", cwd="/different/project", last_assistant_message="", stop_hook_active=False)
        self.assertIn("workspace changed", decision["reason"])
        token = self.token_from(decision)
        marker = f"<!-- agentfanout:blocked lease={token} reason=workspace_changed -->"
        self.assertEqual(
            self.call("Stop", cwd="/different/project", last_assistant_message=marker, stop_hook_active=True),
            {},
        )

    def test_state_contains_digest_not_raw_prompt(self):
        prompt = "AgentFanout Codex inspect secret-fixture-value-94721"
        self.arm(prompt)
        database = Path(self.temp.name) / "session-leases.sqlite3"
        self.assertTrue(database.is_file())
        self.assertNotIn(b"secret-fixture-value-94721", database.read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode checks")
    def test_insecure_or_symlinked_state_storage_fails_closed(self):
        os.chmod(self.temp.name, 0o755)
        insecure = self.call_raw("UserPromptSubmit", prompt="AgentFanout Codex inspect files")
        self.assertEqual(insecure.returncode, 0)
        self.assertIn("storage failed validation", insecure.stdout.lower())
        os.chmod(self.temp.name, 0o700)
        target = Path(self.temp.name) / "lease-target"
        target.write_text("not a database", encoding="utf-8")
        database = Path(self.temp.name) / "session-leases.sqlite3"
        database.symlink_to(target)
        denied = self.call_raw("PreToolUse", tool_name="Bash", tool_input={})
        self.assertEqual(denied.returncode, 0)
        payload = json.loads(denied.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
        first_stop = self.call_raw("Stop", last_assistant_message="blocked", stop_hook_active=False)
        self.assertEqual(json.loads(first_stop.stdout)["decision"], "block")
        second_stop = self.call_raw("Stop", last_assistant_message="blocked", stop_hook_active=True)
        self.assertEqual(json.loads(second_stop.stdout), {})

    def test_terminal_lease_rows_are_deleted(self):
        self.arm()
        decision = self.stop("no provider")
        token = self.token_from(decision)
        self.assertEqual(self.stop(f"<!-- agentfanout:blocked lease={token} reason=provider_unavailable -->"), {})
        database = Path(self.temp.name) / "session-leases.sqlite3"
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

    def test_legacy_inactive_terminal_rows_pruned_and_unknown_rows_preserved(self):
        self.arm()
        database = Path(self.temp.name) / "session-leases.sqlite3"
        with sqlite3.connect(database) as connection:
            raw = connection.execute("SELECT state_json FROM leases").fetchone()[0]
            state = json.loads(raw)
            state["status"] = "blocked"
            connection.execute("UPDATE leases SET state_json = ?", (json.dumps(state),))
            connection.execute(
                "INSERT INTO leases(session_key, state_json, updated_at) VALUES (?, ?, ?)",
                ("corrupt-fixture", "not-json", state["updated_at"]),
            )
            connection.execute(
                "INSERT INTO leases(session_key, state_json, updated_at) VALUES (?, ?, ?)",
                ("blob-fixture", b"\xff", state["updated_at"]),
            )
        self.session = "unrelated-session"
        self.assertEqual(self.call("UserPromptSubmit", prompt="Ordinary prompt"), {})
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT session_key, state_json FROM leases ORDER BY session_key").fetchall(), [("blob-fixture", b"\xff"), ("corrupt-fixture", "not-json")])

    def test_unclassifiable_lease_is_preserved_and_denied(self):
        for enabled_v2 in (False, True):
            self.session = f"unknown-ownership-{enabled_v2}"
            self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
            self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
            self.arm()
            baseline = self.lease_state()
            variants = ["not-json", b"\xff", json.dumps({**baseline, "schema": "UnknownFutureSchema"})]
            variants += [json.dumps({**baseline, "active_agents": value}) for value in (None, {}, 17, [None])]
            variants += [json.dumps({**baseline, "status": value}) for value in (None, {}, [], 17)]
            variants = [(raw, 1) for raw in variants] + [(json.dumps(baseline), "invalid-timestamp")]
            for raw, updated_at in variants:
                with self.subTest(v2=enabled_v2, raw=raw, updated_at=updated_at):
                    with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                        connection.execute("UPDATE leases SET state_json = ?, updated_at = ? WHERE session_key = ?", (raw, updated_at, hashlib.sha256(self.session.encode()).hexdigest()))
                    denied = self.call("PreToolUse", tool_name="Bash")
                    self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
                    with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                        actual = connection.execute("SELECT state_json FROM leases WHERE session_key = ?", (hashlib.sha256(self.session.encode()).hexdigest(),)).fetchone()
                    self.assertEqual(actual, (raw,))

    def test_malformed_phase_or_counters_fail_closed_and_keep_raw_lease(self):
        for enabled_v2 in (False, True):
            self.session = f"malformed-policy-{enabled_v2}"
            self.env["AGENT_FANOUT_V2_MASTER"] = "1" if enabled_v2 else "0"
            self.env["AGENT_FANOUT_V2_HOOK"] = "1" if enabled_v2 else "0"
            self.arm()
            baseline = self.lease_state()
            variants = [("PreToolUse", {**baseline, "phase": phase}) for phase in ([], {}, "UnknownFuturePhase", "complete")]
            variants += [("Stop", {**baseline, "continuations": "invalid"}), ("Stop", {**baseline, "max_continuations": "invalid"}), ("Stop", {key: value for key, value in baseline.items() if key != "max_continuations"})]
            for event_name, state in variants:
                with self.subTest(v2=enabled_v2, event=event_name, state=state):
                    raw = json.dumps(state)
                    with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                        connection.execute("UPDATE leases SET state_json = ? WHERE session_key = ?", (raw, hashlib.sha256(self.session.encode()).hexdigest()))
                    output = self.call(event_name, tool_name="Bash", last_assistant_message="not complete")
                    if event_name == "PreToolUse":
                        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
                    else:
                        self.assertEqual(output["decision"], "block")
                    with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                        actual = connection.execute("SELECT state_json FROM leases WHERE session_key = ?", (hashlib.sha256(self.session.encode()).hexdigest(),)).fetchone()
                    self.assertEqual(actual, (raw,))

    def test_malformed_terminal_history_is_preserved_by_shared_hook_handler(self):
        for v2 in (False, True):
            self.env["AGENT_FANOUT_V2_MASTER"] = "1" if v2 else "0"
            self.env["AGENT_FANOUT_V2_HOOK"] = "1" if v2 else "0"
            self.session = f"malformed-terminal-history-{v2}"
            self.arm()
            self.call("SubagentStart", agent_id="worker-a", turn_id="worker-turn-1")
            self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
            original = self.lease_state()
            for seen in ({}, "bad", None, [3]):
                with self.subTest(v2=v2, seen=seen):
                    damaged = {**original, "seen_stop_events": seen}
                    with sqlite3.connect(Path(self.temp.name) / "session-leases.sqlite3") as connection:
                        connection.execute("UPDATE leases SET state_json=? WHERE session_key=?", (json.dumps(damaged), hashlib.sha256(self.session.encode()).hexdigest()))
                    result = self.call_raw("SubagentStop", agent_id="worker-a", turn_id="worker-turn-1")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {})
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(self.lease_state(), damaged)

    def test_concurrent_subagent_callbacks_are_serialized(self):
        self.arm()
        ids = [f"agent-{index}" for index in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda agent: self.call("SubagentStart", agent_id=agent, agent_type="worker"), ids))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda agent: self.call("SubagentStop", agent_id=agent, agent_type="worker", last_assistant_message="result"), ids))
        status = self.call("UserPromptSubmit", prompt="AgentFanout Status")
        self.assertIn("active workers 0", json.dumps(status).lower())
        self.assertIn("wave 1", json.dumps(status).lower())

    def test_v2_readonly_events_do_not_wait_for_a_writer(self):
        self.env["AGENT_FANOUT_V2_MASTER"] = "1"
        self.env["AGENT_FANOUT_V2_HOOK"] = "1"
        self.arm()
        database = Path(self.temp.name) / "session-leases.sqlite3"
        with sqlite3.connect(database, timeout=1) as writer:
            writer.execute("BEGIN IMMEDIATE")
            denied = self.call("PreToolUse", tool_name="Bash", tool_input={})
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("wave 0", json.dumps(self.call("UserPromptSubmit", prompt="AgentFanout Status")))
            self.assertIn("lease remains armed", json.dumps(self.call("SessionStart")))

    def test_v2_unrelated_noop_events_do_not_create_storage(self):
        self.env["AGENT_FANOUT_V2_MASTER"] = "1"
        self.env["AGENT_FANOUT_V2_HOOK"] = "1"
        self.assertEqual(self.call("UserPromptSubmit", prompt="Fix this small typo"), {})
        self.assertEqual(self.call("PreToolUse", tool_name="Bash", tool_input={}), {})
        self.assertEqual(self.call("Stop", last_assistant_message="done"), {})
        self.assertEqual(self.call("SubagentStop", agent_id="late-agent", last_assistant_message="done"), {})
        self.assertEqual(self.call("UnknownEvent"), {})
        self.assertFalse((Path(self.temp.name) / "session-leases.sqlite3").exists())

    def test_v2_duplicate_callbacks_do_not_rewrite_state_or_cancel_phase(self):
        self.env["AGENT_FANOUT_V2_MASTER"] = "1"
        self.env["AGENT_FANOUT_V2_HOOK"] = "1"
        self.arm()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        database = Path(self.temp.name) / "session-leases.sqlite3"

        def stored_state():
            with sqlite3.connect(database) as connection:
                return connection.execute("SELECT state_json FROM leases").fetchone()[0]

        before_duplicate = stored_state()
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.assertEqual(stored_state(), before_duplicate)
        self.call("SubagentStop", agent_id="agent-a", agent_type="worker", last_assistant_message="first")
        after_stop = stored_state()
        self.assertEqual(self.call("SubagentStop", agent_id="agent-a", agent_type="worker", last_assistant_message="first"), {})
        self.assertEqual(stored_state(), after_stop)
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.call("UserPromptSubmit", prompt="AgentFanout Cancel")
        cancel_state = json.loads(stored_state())
        self.assertEqual(cancel_state["phase"], "cancel_requested")
        self.call("SubagentStart", agent_id="agent-a", agent_type="worker")
        self.assertEqual(stored_state(), json.dumps(cancel_state, sort_keys=True, separators=(",", ":")))
        self.call("SubagentStop", agent_id="agent-a", agent_type="worker", last_assistant_message="done")
        self.assertEqual(self.call("SubagentStop", agent_id="agent-a", agent_type="worker", last_assistant_message="done"), {})

    def test_v2_concurrent_callbacks_keep_one_wave(self):
        self.env["AGENT_FANOUT_V2_MASTER"] = "1"
        self.env["AGENT_FANOUT_V2_HOOK"] = "1"
        self.arm()
        ids = [f"v2-agent-{index}" for index in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda agent: self.call("SubagentStart", agent_id=agent, agent_type="worker"), ids))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda agent: self.call("SubagentStop", agent_id=agent, agent_type="worker", last_assistant_message="result"), ids))
        database = Path(self.temp.name) / "session-leases.sqlite3"
        with sqlite3.connect(database) as connection:
            state = json.loads(connection.execute("SELECT state_json FROM leases").fetchone()[0])
        self.assertEqual(state["wave"], 1)
        self.assertEqual(state["batch"], 1)
        self.assertEqual(state["workers_started"], 8)
        self.assertEqual(state["workers_finished"], 8)
        self.assertEqual(state["active_agents"], [])

    def test_plugin_manifest_hooks_and_skill_are_well_formed(self):
        manifest = json.loads(PLUGIN_JSON.read_text())
        hooks = json.loads(HOOKS_JSON.read_text())
        self.assertEqual(manifest["name"], "agentfanout")
        self.assertNotIn("hooks", manifest)
        self.assertEqual(set(hooks["hooks"]), {"SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "PreToolUse", "Stop"})
        for groups in hooks["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertIn("$PLUGIN_ROOT", hook["command"])
                    self.assertIn("%PLUGIN_ROOT%", hook["commandWindows"])
        installed_skill = ROOT / "plugins" / "agentfanout" / "skills" / "agent-fanout" / "SKILL.md"
        self.assertEqual((ROOT / "SKILL.md").read_text(), installed_skill.read_text())


if __name__ == "__main__":
    unittest.main()
