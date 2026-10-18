"""Offline, deterministic LangGraph evaluation tests for AgentFanout."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))

from agentfanout.lifecycle import build_task_units, initialize_run, plan_next_wave
from fixture_runtime import FixtureRuntime, SimulatedCrash, resumed_state


def make_state(project: Path, count: int, *, max_attempts: int = 3, state_path: Path | None = None):
    tasks = build_task_units(
        "fixture objective", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
        [f"request_item_{index}" for index in range(1, count + 1)],
        allowed_paths=[str(project)],
    )
    return {
        "af_state": initialize_run(
            "fixture objective", tasks,
            {"max_wave_size": count, "max_attempts_per_task": max_attempts},
        ),
        "state_path": str(state_path) if state_path else None,
        "thread_id": "fixture-thread",
        "scripted_batches": [],
        "validator_receipts": [],
        "validator_launch_receipts": [],
        "wave_history": [],
        "parent_tool_calls": 0,
        "terminal_attempts": 0,
        "effect_mode": "none",
        "effect_calls": 0,
    }


class LangGraphFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.runtime = FixtureRuntime(self.root / "host.sqlite3")

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_residual_only_second_wave(self):
        state = make_state(self.project, 2)
        state["scripted_batches"] = [
            [
                {"status": "partial", "remaining_work": [{"scope": "Validate residual evidence"}]},
                {"status": "success"},
            ],
            [{"status": "success"}],
        ]
        output = self.runtime.invoke(state)
        dispatches = [plan for plan in output["wave_history"] if plan["action"] == "dispatch_wave"]
        self.assertEqual(len(dispatches), 2)
        first, second = dispatches
        self.assertEqual(len(second["assignments"]), 1)
        root_assignment = first["assignments"][0]
        residual_assignment = second["assignments"][0]
        residual_task = next(task for task in output["af_state"]["tasks"] if task["task_id"] == residual_assignment["task_id"])
        self.assertEqual(residual_task["parent_task_id"], root_assignment["task_id"])
        self.assertEqual(residual_assignment["worker_action"], "follow_up")
        self.assertEqual(residual_assignment["recommended_worker_handle"], root_assignment["worker_handle"])
        self.assertEqual(output["parent_tool_calls"], 0)
        self.assertEqual(output["graph_status"], "complete")
        self.assertEqual(len(output["validator_launch_receipts"]), 3)
        self.assertTrue(all(item["usage"]["cost_basis"] == "reservation_upper_bound" for item in output["validator_launch_receipts"]))

    def test_contradiction_requires_independent_validation(self):
        state = make_state(self.project, 2)
        state["scripted_batches"] = [[
            {"status": "success", "evidence": [{"claim_id": "claim-x", "value": "left"}]},
            {"status": "success", "evidence": [{"claim_id": "claim-x", "value": "right"}]},
        ]]
        output = self.runtime.invoke(state)
        self.assertEqual(output["graph_status"], "blocked_validation")
        self.assertEqual(output["blocked_reason"], "independent_validator_contradiction")
        self.assertEqual(len(output["validator_receipts"]), 2)
        self.assertEqual(len(output["validator_launch_receipts"]), 2)
        self.assertTrue(all(item["verdict"] == "fail" for item in output["validator_receipts"]))
        for result, launch in zip(output["validator_receipts"], output["validator_launch_receipts"]):
            self.assertEqual(result["validator_launch_correlation_id"], launch["validator_launch_correlation_id"])
            self.assertEqual(result["validated_result_sha256"], launch["validated_result_sha256"])
            self.assertEqual(result["validator_policy_sha256"], launch["validator_policy_sha256"])
        self.assertTrue(all(not item["validator_id"].startswith("worker_") for item in output["validator_receipts"]))
        self.assertEqual(output["parent_tool_calls"], 0)
        self.assertEqual(output["terminal_attempts"], 0)

    def test_parent_tool_denial(self):
        state = make_state(self.project, 1)
        with self.assertRaisesRegex(PermissionError, "parent_worker_action_denied:delegable_work_remains"):
            self.runtime.attempt_parent_worker_action(state)
        self.assertEqual(state["parent_tool_calls"], 0)

    def test_resume_unknown_effect_never_reexecutes(self):
        state_path = self.root / "run.json"
        state = make_state(self.project, 1, state_path=state_path)
        state["scripted_batches"] = [[{"status": "success"}]]
        state["effect_mode"] = "crash_after_pending"
        with self.assertRaises(SimulatedCrash):
            self.runtime.invoke(state)
        self.assertEqual(self.runtime.effect_executions, 1)
        self.runtime.close()
        self.runtime = FixtureRuntime(self.root / "host.sqlite3")
        resumed = resumed_state(
            state_path,
            scripted_batches=[[{"status": "success"}]],
            validator_receipts=[],
            validator_launch_receipts=[],
            wave_history=[],
            parent_tool_calls=0,
            terminal_attempts=0,
            effect_mode="resume_unknown",
            effect_calls=1,
        )
        output = self.runtime.invoke(resumed)
        self.assertEqual(output["effect_calls"], 1)
        self.assertEqual(self.runtime.effect_executions, 0)
        effects = self.runtime.ledger.events(output["af_state"]["run_id"], "effect_record")
        self.assertEqual([event["payload"]["state"] for event in effects], ["pending", "unknown_after_crash"])
        self.assertEqual(output["graph_status"], "blocked_terminal")
        self.assertIn("effect_unresolved", output["blocked_reason"])

    def test_blocked_worker_never_falls_back_to_parent(self):
        state = make_state(self.project, 1, max_attempts=1)
        state["scripted_batches"] = [[{"status": "blocked", "summary": "worker unavailable"}]]
        output = self.runtime.invoke(state)
        self.assertEqual(output["graph_status"], "blocked")
        self.assertEqual(output["current_plan"]["reason"], "delegable_residual_not_dispatchable")
        self.assertGreater(output["current_plan"]["remaining_delegable_tasks"], 0)
        self.assertEqual(output["parent_tool_calls"], 0)
        with self.assertRaisesRegex(PermissionError, "parent_worker_action_denied:delegable_work_remains"):
            self.runtime.attempt_parent_worker_action(output)

    def test_adapter_does_not_change_core_semantics(self):
        direct_state = make_state(self.project, 1)["af_state"]
        direct_plan = plan_next_wave(direct_state)
        state = make_state(self.project, 1)
        state["scripted_batches"] = [[{"status": "partial", "remaining_work": ["bounded residual"]}], [{"status": "success"}]]
        output = self.runtime.invoke(state)
        first = next(plan for plan in output["wave_history"] if plan["action"] == "dispatch_wave")
        self.assertEqual(first["action"], direct_plan["action"])
        self.assertEqual(len(first["assignments"]), len(direct_plan["assignments"]))
        self.assertEqual(first["parent_policy"], direct_plan["parent_policy"])
        self.assertEqual(output["parent_tool_calls"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
