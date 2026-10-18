"""Focused opt-in policy checks; no provider calls."""
import os
import unittest
from unittest.mock import patch

from agentfanout.native import NativeWaveError
from agentfanout.v2_policy import prepare, recovery_action


def task(task_id="one", *, depends_on=None, write_set=None, task_class="simple", risk="low", **extra):
    return {"task_id": task_id, "task": f"Inspect {task_id}",
            "scope": {"inputs": [f"src/{task_id}.py"], "expected_output": "Evidence packet",
                      "owner": task_id, "write_set": write_set or [],
                      "depends_on": depends_on or [], "check": "Evidence cites input"},
            "task_class": task_class, "risk": risk, **extra}


def request(tasks=None, *, model=True, free_slots=3, flags=None):
    feature_flags = {"master": True, "assignment": True, "lifecycle": True,
                     "model": model, "validation": True, "recovery": True}
    feature_flags.update(flags or {})
    capability = {"provider": "Codex", "host": "fixture-host", "surface": "desktop",
                  "build": "test-build", "snapshot_id": "snapshot-1",
                  "spawn": True, "wait": True, "follow_up": True,
                  "cancel": True, "result_callback": True,
                  "launch_acknowledgement": True,
                  "model_selection": model, "reasoning_selection": model,
                  "default_model": "gpt-5.5",
                  "free_slots": free_slots,
                  "models": ([{"id": "gpt-6-luna", "tier": "luna", "efforts": ["low", "medium"], "available": True},
                              {"id": "gpt-6-sol", "tier": "sol", "efforts": ["medium", "high"], "available": True},
                              {"id": "gpt-6-astra", "tier": "astra", "efforts": ["max"], "available": True}]
                             if model else [])}
    return {"action": "plan", "run_id": "run-1", "prompt": "legacy fallback",
            "feature_flags": feature_flags, "capability": capability,
            "tasks": tasks if tasks is not None else [task()], "explicit_offload": True}


class V2PolicyTests(unittest.TestCase):
    def test_deterministic_failure_classes_and_backoff(self):
        self.assertEqual(recovery_action("rate_limit", 1, "luna", enabled_stage=True)["action"], "repair")
        self.assertEqual(recovery_action("rate_limit", 1, "luna", enabled_stage=True)["backoff_ms"], 2000)
        self.assertEqual(recovery_action("model_unavailable", 1, "luna", enabled_stage=True)["action"], "escalate_sol")
        self.assertEqual(recovery_action("model_unavailable", 1, "sol", enabled_stage=True)["action"], "stop")
        self.assertEqual(recovery_action("policy_failure", 1, "luna", enabled_stage=True)["action"], "stop")
    def test_master_off_returns_legacy_route(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(prepare(request())["status"], "legacy_route")

    @patch.dict(os.environ, {"AGENT_FANOUT_V2_MASTER": "1"})
    def test_zero_work_and_single_benefit_gate(self):
        self.assertEqual(prepare(request([]))["status"], "stay_local")
        candidate = request()
        candidate["explicit_offload"] = False
        self.assertEqual(prepare(candidate)["reason"], "single_worker_benefit_unproven")
        candidate["benefit_evidence"] = {"qualified": True, "evidence_ref": "pilot-12"}
        self.assertEqual(prepare(candidate)["status"], "launch_required")

    @patch.dict(os.environ, {"AGENT_FANOUT_V2_MASTER": "1"})
    def test_assignment_stage_does_not_require_model_selection(self):
        candidate = request(model=False)
        result = prepare(candidate)
        self.assertEqual(result["tasks"][0]["model_selection"]["tier"], "host_default")
        self.assertEqual(result["max_workers"], 1)
        candidate["capability"].pop("default_model")
        with self.assertRaisesRegex(NativeWaveError, "baseline_model_unknown"):
            prepare(candidate)

    @patch.dict(os.environ, {"AGENT_FANOUT_V2_MASTER": "1"})
    def test_model_tiers_and_explicit_pin(self):
        candidate = request([task("a"), task("b", task_class="mechanical", risk="medium"),
                             task("c", task_class="engineering", risk="medium"),
                             task("d", task_class="hard", risk="high")])
        result = prepare(candidate)
        self.assertEqual(result["max_workers"], 3)
        selected = [(item["model_selection"]["tier"], item["model_selection"]["requested_effort"])
                    for item in result["tasks"]]
        self.assertEqual(selected, [("luna", "low"), ("luna", "medium"),
                                    ("sol", "medium"), ("sol", "high")])
        pinned = request([task(requested_model="gpt-6-sol", requested_effort="high")])
        self.assertEqual(prepare(pinned)["tasks"][0]["model_selection"]["selection_reason"], "explicit_user_model_pin")

    @patch.dict(os.environ, {"AGENT_FANOUT_V2_MASTER": "1"})
    def test_astra_only_exact_authorized_single_wave(self):
        candidate = request([task("one", requested_model="gpt-6-astra", authorization="explicit_user_request"), task("two")])
        with self.assertRaisesRegex(NativeWaveError, "astra_use_legacy_explicit_lane"):
            prepare(candidate)
        denied = request([task(requested_model="gpt-6-astra")])
        with self.assertRaisesRegex(NativeWaveError, "astra_use_legacy_explicit_lane"):
            prepare(denied)
        mislabeled = request()
        mislabeled["capability"]["models"][2]["tier"] = "sol"
        with self.assertRaisesRegex(NativeWaveError, "identity_tier_mismatch"):
            prepare(mislabeled)

    @patch.dict(os.environ, {"AGENT_FANOUT_V2_MASTER": "1"})
    def test_dependencies_write_ownership_and_acceptance(self):
        candidate = request([task("a", write_set=["src/shared.py"]),
                             task("b", depends_on=["a"], write_set=["src/shared.py"])])
        self.assertEqual(prepare(candidate)["max_workers"], 1)
        conflict = request([task("a", write_set=["src/shared.py"]),
                            task("b", write_set=["src/shared.py"])])
        with self.assertRaisesRegex(NativeWaveError, "overlapping_write"):
            prepare(conflict)
        cycle = request([task("a", depends_on=["b"]), task("b", depends_on=["a"])])
        with self.assertRaisesRegex(NativeWaveError, "dependency_cycle"):
            prepare(cycle)
        missing = request()
        del missing["tasks"][0]["scope"]["check"]
        with self.assertRaisesRegex(NativeWaveError, "scope_contract_incomplete"):
            prepare(missing)

    @patch.dict(os.environ, {"AGENT_FANOUT_V2_MASTER": "1"})
    def test_sensitive_and_parent_only_work_never_emits_assignment(self):
        for text in ("Use api_key=supersecret123 to inspect", "Commit this change",
                     "Delete old records", "Use Gmail MCP to send email"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(NativeWaveError, "v2_sensitive_task_denied|v2_parent_only_action_denied"):
                    prepare(request([{"task_id": "one", "task": text,
                                      "scope": task()["scope"], "task_class": "simple", "risk": "low"}]))


if __name__ == "__main__":
    unittest.main()
