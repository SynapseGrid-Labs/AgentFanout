from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from agentfanout.native import CapabilityHandshake, NativeWavePlanner, NativeWaveError, validate_result_packet
from benchmarks.skill_first_economics import run_benchmark
from scripts.route import route


ROOT = Path(__file__).resolve().parents[1]


class SkillFirstProductTests(unittest.TestCase):
    def test_public_skill_budgets_and_positive_metadata(self):
        content = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        description = re.search(r"^description:\s*\"(.*)\"$", content, re.MULTILINE).group(1)
        body = content.split("---", 2)[-1].lstrip("\n")
        self.assertLessEqual(len(description), 280)
        self.assertLessEqual(len(body.splitlines()), 20)
        self.assertLessEqual(len(body.encode()), 2_500)
        self.assertIn("explicitly requested fanout", description)
        for broad in ("coding agents", "specialist agents", "custom agents", "local-first", "cost-aware"):
            self.assertNotIn(broad, description.lower())

    def test_default_lazy_documents_are_exactly_four_and_bounded(self):
        profile = json.loads((ROOT / "profiles/public-skill.json").read_text(encoding="utf-8"))
        docs = profile["default_lazy_documents"]
        self.assertEqual(len(docs), 4)
        for relative in docs:
            path = ROOT / relative
            self.assertLessEqual(len(path.read_bytes()), 4_000)
            self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), 50)

    def test_global_guidance_has_no_agentfanout_policy_and_keeps_compact_entry(self):
        guidance = Path.home() / ".codex" / "AGENTS.md"
        routing = Path.home() / ".pai" / "skills" / "skill-routing-index.md"
        if not guidance.is_file() or not routing.is_file():
            self.skipTest("host skill-routing integration is not installed")
        text = guidance.read_text(encoding="utf-8")
        forbidden = (
            "AgentFanout " + "Pre-Action",
            "AGENT_" + "FANOUT_STATE_FILE",
            "route" + ".py",
            "route every " + "user task",
        )
        for item in forbidden:
            self.assertNotIn(item, text)
        self.assertRegex(routing.read_text(encoding="utf-8"), r"(?m)^agent-fanout:")

    def test_false_positive_turns_are_direct_and_sticky_state_is_ignored(self):
        for prompt in (
            "fix the typo in README",
            "implement a login form",
            "what is multi-agent architecture?",
            "thanks",
            "unrelated complex work after a completed task",
        ):
            result = route(prompt, {"provider_mode": "Grok"})
            self.assertFalse(result["orchestration_useful"], prompt)
            self.assertEqual(result["dispatch"]["status"], "main_session", prompt)
            self.assertEqual(result["next_state"]["provider_mode"], "Auto", prompt)

    def test_positive_commands_are_task_scoped(self):
        result = route("AgentFanout Codex review API and UI independently")
        self.assertEqual(result["command"]["type"], "provider_task")
        self.assertEqual(result["command"]["args"]["lifecycle"], "task_scoped")
        self.assertEqual(result["next_state"]["provider_mode"], "Auto")
        self.assertNotIn("set_provider_mode", json.dumps(result))

    def test_native_codex_waves_do_not_require_strict_runtime(self):
        capability = CapabilityHandshake("Codex", follow_up=True, launch_acknowledgement=False)
        tasks = [
            {"task_id": "api", "packet": {"task_id": "api", "task": "inspect API", "scope": "api", "limits": {}, "forbidden_actions": [], "result_contract": "compact"}},
            {"task_id": "ui", "packet": {"task_id": "ui", "task": "inspect UI", "scope": "ui", "limits": {}, "forbidden_actions": [], "result_contract": "compact"}},
        ]
        planner = NativeWavePlanner(tasks, capability, max_workers=2)
        first = planner.start()
        self.assertEqual({item["task_id"] for item in first}, {"api", "ui"})
        second = planner.ingest([{"task_id": "api", "status": "success", "summary": "done", "evidence": ["api"], "uncertainty": [], "residual_work": [{"task_id": "api", "task": "follow up", "scope": "api", "limits": {}, "forbidden_actions": [], "result_contract": "compact"}]}])
        self.assertEqual([item["task_id"] for item in second], ["api"])
        self.assertEqual(second[0]["worker_id"], first[0]["worker_id"])
        self.assertNotIn("ui", {item["task_id"] for item in second})
        self.assertEqual(
            planner.ingest([{"task_id": "ui", "status": "success", "summary": "done", "evidence": ["ui"], "uncertainty": [], "residual_work": []}]),
            [],
        )

    def test_unassigned_result_is_rejected_without_relaunch(self):
        tasks = [
            {"task_id": task_id, "packet": {"task_id": task_id, "task": f"inspect {task_id}", "scope": task_id, "limits": {}, "forbidden_actions": [], "result_contract": "compact"}}
            for task_id in ("one", "two")
        ]
        planner = NativeWavePlanner(tasks, CapabilityHandshake("Codex", launch_acknowledgement=False), max_workers=1)
        self.assertEqual([item["task_id"] for item in planner.start()], ["one"])
        with self.assertRaisesRegex(NativeWaveError, "native_result_task_not_active"):
            planner.ingest([{"task_id": "two", "status": "success", "summary": "unexpected", "evidence": ["two"], "uncertainty": [], "residual_work": []}])

    def test_duplicate_task_ids_are_rejected(self):
        packet = {"task_id": "same", "task": "inspect", "scope": "same", "limits": {}, "forbidden_actions": [], "result_contract": "compact"}
        with self.assertRaisesRegex(NativeWaveError, "native_wave_duplicate_task_id"):
            NativeWavePlanner(
                [{"task_id": "same", "packet": packet}, {"task_id": "same", "packet": packet}],
                CapabilityHandshake("Codex", launch_acknowledgement=False),
            )

    def test_invalid_result_batch_does_not_partially_mutate_assignments(self):
        tasks = [
            {"task_id": task_id, "packet": {"task_id": task_id, "task": f"inspect {task_id}", "scope": task_id, "limits": {}, "forbidden_actions": [], "result_contract": "compact"}}
            for task_id in ("one", "two")
        ]
        planner = NativeWavePlanner(tasks, CapabilityHandshake("Codex", launch_acknowledgement=False), max_workers=2)
        planner.start()
        before = dict(planner.assignments)
        valid = {"task_id": "one", "status": "success", "summary": "done", "evidence": ["one"], "uncertainty": [], "residual_work": []}
        invalid = {"task_id": "missing", "status": "success", "summary": "bad", "evidence": [], "uncertainty": [], "residual_work": []}
        with self.assertRaisesRegex(NativeWaveError, "native_result_task_unknown"):
            planner.ingest([valid, invalid])
        self.assertEqual(planner.assignments, before)
        self.assertEqual(set(planner.tasks), {"one", "two"})

    def test_structured_residual_can_create_a_new_task_identity(self):
        packet = {"task_id": "one", "task": "inspect", "scope": "one", "limits": {}, "forbidden_actions": [], "result_contract": "compact"}
        planner = NativeWavePlanner([{"task_id": "one", "packet": packet}], CapabilityHandshake("Codex", launch_acknowledgement=False))
        planner.start()
        result = {
            "task_id": "one", "status": "partial", "summary": "follow up", "evidence": ["one"], "uncertainty": [],
            "residual_work": [{**packet, "task_id": "different"}],
        }
        follow_up = planner.ingest([result])
        self.assertEqual([item["task_id"] for item in follow_up], ["different"])
        self.assertNotIn("one", planner.tasks)

    def test_changed_scope_uses_new_worker_and_cancel_stops_future_waves(self):
        task = {"task_id": "one", "packet": {"task_id": "one", "task": "inspect one", "scope": "one", "limits": {}, "forbidden_actions": [], "result_contract": "compact"}}
        planner = NativeWavePlanner([task], CapabilityHandshake("Codex", follow_up=True, launch_acknowledgement=False))
        first = planner.start()[0]
        changed = planner.ingest([{"task_id": "one", "status": "partial", "summary": "changed", "evidence": ["x"], "uncertainty": ["scope changed"], "residual_work": ["remaining"], "scope_changed": True}])[0]
        self.assertNotEqual(changed["worker_id"], first["worker_id"])
        planner.cancel()
        self.assertEqual(planner.ingest([]), [])

    def test_compact_result_budget_is_enforced(self):
        with self.assertRaisesRegex(NativeWaveError, "over_budget"):
            validate_result_packet({
                "task_id": "x", "status": "success", "summary": "x" * 1_300,
                "evidence": [], "uncertainty": [], "residual_work": [],
            })

    def test_public_skill_has_no_runtime_dependencies(self):
        profile = json.loads((ROOT / "profiles/public-skill.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["dependencies"], [])
        self.assertFalse(profile["portable"] is False)
        text = "\n".join((ROOT / relative).read_text(encoding="utf-8") for relative in ["SKILL.md", *profile["default_lazy_documents"]])
        for forbidden in ("python3", "bun ", "fcntl", "sqlite", "subprocess", "XDG_"):
            self.assertNotIn(forbidden.lower(), text.lower())

    def test_economics_fixture_enforces_initial_gates_without_claiming_savings(self):
        report = run_benchmark()
        self.assertTrue(report["all_gates_pass"])
        self.assertEqual(report["claim_status"], "not_a_savings_claim")
        self.assertEqual(report["measurements"]["skill-native"]["validator_input_tokens"], 0)
        self.assertLessEqual(report["measurements"]["skill-native"]["provider_input_tokens"], 8_000)
        self.assertLessEqual(report["measurements"]["optional-runtime"]["provider_input_tokens"], 15_000)


if __name__ == "__main__":
    unittest.main()
