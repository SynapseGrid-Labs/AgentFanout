from __future__ import annotations

import json
import unittest
from pathlib import Path

from agentfanout.native import CapabilityHandshake, NativeWaveError, NativeWavePlanner
from benchmarks.skill_first_economics import run_benchmark
from scripts.route import route


ROOT = Path(__file__).resolve().parents[1]


def task_packet(task_id: str, task: str | None = None, scope: str | None = None) -> dict[str, object]:
    return {
        "task_id": task_id,
        "task": task or f"inspect {task_id}",
        "scope": scope or task_id,
        "limits": {},
        "forbidden_actions": [],
        "result_contract": "compact",
    }


def result_packet(task_id: str, **overrides: object) -> dict[str, object]:
    packet: dict[str, object] = {
        "task_id": task_id,
        "status": "success",
        "summary": "complete",
        "evidence": [task_id],
        "uncertainty": [],
        "residual_work": [],
    }
    packet.update(overrides)
    return packet


def acknowledge(planner: NativeWavePlanner, assignments: list[dict[str, object]]) -> list[dict[str, object]]:
    for assignment in assignments:
        planner.acknowledge_launch(str(assignment["task_id"]), str(assignment["worker_id"]))
    return assignments


def start(planner: NativeWavePlanner) -> list[dict[str, object]]:
    return acknowledge(planner, planner.start())


def ingest(planner: NativeWavePlanner, results: list[dict[str, object]]) -> list[dict[str, object]]:
    return acknowledge(planner, planner.ingest(results))


class SkillFirstMachineUAT(unittest.TestCase):
    def test_noop_and_sticky_state_matrix_is_silent_and_direct(self):
        prompts = (
            "fix the typo in README",
            "implement a login form",
            "what is multi-agent architecture?",
            "thanks",
            "review these architecture notes after the prior task completed",
            "summarize this short paragraph after the prior one-shot request",
        )
        for prompt in prompts:
            routed = route(prompt, {"provider_mode": "Grok", "effort_mode": "High"})
            self.assertFalse(routed["orchestration_useful"], prompt)
            self.assertEqual(routed["dispatch"]["status"], "main_session", prompt)
            self.assertEqual(routed["next_state"]["provider_mode"], "Auto", prompt)

    def test_positive_commands_are_one_shot_and_provider_scoped(self):
        for provider in ("Codex", "Grok", "Local"):
            routed = route(f"AgentFanout {provider} review API, UI, database, and tests independently")
            self.assertEqual(routed["command"]["type"], "provider_task")
            self.assertEqual(routed["command"]["args"]["provider"], provider)
            self.assertEqual(routed["command"]["args"]["lifecycle"], "task_scoped")
            self.assertEqual(routed["next_state"]["provider_mode"], "Auto")

    def test_smallest_bounded_wave_respects_concurrency(self):
        tasks = [{"task_id": item, "packet": task_packet(item)} for item in ("api", "ui", "db", "tests")]
        planner = NativeWavePlanner(tasks, CapabilityHandshake("Codex"), max_workers=2)
        first = start(planner)
        self.assertEqual([item["task_id"] for item in first], ["api", "ui"])
        second = ingest(planner, [result_packet("api")])
        self.assertEqual([item["task_id"] for item in second], ["db"])
        self.assertEqual(set(planner.assignments), {"ui", "db"})

    def test_multiple_structured_residuals_fan_out_without_loss(self):
        planner = NativeWavePlanner(
            [{"task_id": "root", "packet": task_packet("root")}],
            CapabilityHandshake("Codex"),
            max_workers=2,
        )
        start(planner)
        follow_up = ingest(planner, [result_packet(
            "root",
            status="partial",
            residual_work=[task_packet("left"), task_packet("right")],
        )])
        self.assertEqual({item["task_id"] for item in follow_up}, {"left", "right"})
        self.assertNotIn("root", planner.tasks)

    def test_contradiction_requires_fresh_validator_wave(self):
        planner = NativeWavePlanner(
            [{"task_id": "claim", "packet": task_packet("claim")}],
            CapabilityHandshake("Codex", follow_up=True),
        )
        first = start(planner)[0]
        validator = ingest(planner, [result_packet("claim", contradictions=["evidence disagrees"])])[0]
        self.assertEqual(validator["role"], "validator")
        self.assertNotEqual(validator["worker_id"], first["worker_id"])
        self.assertIn("Independently validate contradictions", validator["packet"]["task"])
        self.assertEqual(ingest(planner, [result_packet("claim")]), [])
        self.assertEqual(planner.snapshot()["status"], "completed")

    def test_unchanged_scope_reuses_worker_and_changed_scope_replaces_it(self):
        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one")}],
            CapabilityHandshake("Codex", follow_up=True),
        )
        first = start(planner)[0]
        same = ingest(planner, [result_packet("one", status="partial", residual_work=["same scope"])])[0]
        self.assertEqual(same["worker_id"], first["worker_id"])
        changed = ingest(planner, [result_packet(
            "one", status="partial", residual_work=["new scope"], scope_changed=True,
        )])[0]
        self.assertNotEqual(changed["worker_id"], same["worker_id"])

    def test_cancellation_stops_all_future_waves(self):
        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one")}],
            CapabilityHandshake("Codex"),
        )
        start(planner)
        planner.cancel()
        self.assertEqual(planner.ingest([result_packet("one")]), [])
        self.assertEqual(planner.snapshot()["status"], "cancelled")

    def test_attempt_ceiling_returns_explicit_blocked_state(self):
        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one")}],
            CapabilityHandshake("Codex"),
            max_attempts_per_task=2,
        )
        start(planner)
        ingest(planner, [result_packet("one", status="failed")])
        self.assertEqual(ingest(planner, [result_packet("one", status="failed")]), [])
        self.assertEqual(planner.snapshot()["status"], "blocked")
        self.assertEqual(planner.snapshot()["blocked_reason"], "native_task_attempt_limit_exceeded")

    def test_wave_ceiling_returns_explicit_blocked_state(self):
        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one")}],
            CapabilityHandshake("Codex", follow_up=True),
            max_waves=1,
        )
        start(planner)
        self.assertEqual(ingest(planner, [result_packet("one", status="partial", residual_work=["more"])]), [])
        self.assertEqual(planner.snapshot()["blocked_reason"], "native_wave_limit_exceeded")

    def test_wave_ceiling_waits_for_already_active_workers_before_blocking(self):
        tasks = [{"task_id": item, "packet": task_packet(item)} for item in ("api", "ui", "db")]
        planner = NativeWavePlanner(tasks, CapabilityHandshake("Codex"), max_workers=2, max_waves=1)
        start(planner)
        self.assertEqual(ingest(planner, [result_packet("api")]), [])
        self.assertEqual(set(planner.assignments), {"ui"})
        self.assertEqual(planner.snapshot()["status"], "active")
        self.assertEqual(planner.snapshot()["pending_stop_reason"], "native_wave_limit_exceeded")
        self.assertEqual(ingest(planner, [result_packet("ui")]), [])
        self.assertEqual(planner.snapshot()["status"], "blocked")

    def test_derived_packet_budget_failure_is_atomic(self):
        near_limit = "x" * 860
        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one", task=near_limit)}],
            CapabilityHandshake("Codex"),
        )
        start(planner)
        before = planner.snapshot()
        with self.assertRaisesRegex(NativeWaveError, "native_task_packet_over_budget"):
            planner.ingest([result_packet("one", contradictions=["conflict"])])
        self.assertEqual(planner.snapshot(), before)

    def test_packet_budgets_cover_nested_and_optional_fields(self):
        oversized_task = task_packet("one")
        oversized_task["limits"] = {"context": "x" * 1_100}
        with self.assertRaisesRegex(NativeWaveError, "native_task_packet_over_budget"):
            NativeWavePlanner([{"task_id": "one", "packet": oversized_task}], CapabilityHandshake("Codex"))

        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one")}],
            CapabilityHandshake("Codex"),
        )
        start(planner)
        before = planner.snapshot()
        with self.assertRaisesRegex(NativeWaveError, "native_result_packet_over_budget"):
            planner.ingest([result_packet("one", contradictions=["x" * 1_300])])
        self.assertEqual(planner.snapshot(), before)

    def test_packet_identity_types_status_and_atomicity_fail_closed(self):
        with self.assertRaisesRegex(NativeWaveError, "native_task_packet_id_mismatch"):
            NativeWavePlanner(
                [{"task_id": "outer", "packet": task_packet("inner")}],
                CapabilityHandshake("Codex"),
            )
        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one")}],
            CapabilityHandshake("Codex"),
        )
        start(planner)
        before = planner.snapshot()
        with self.assertRaisesRegex(NativeWaveError, "native_result_status_invalid"):
            planner.ingest([result_packet("one", status="unknown")])
        self.assertEqual(planner.snapshot(), before)

    def test_results_require_nonempty_nonreplayed_launch_identity(self):
        planner = NativeWavePlanner(
            [{"task_id": "one", "packet": task_packet("one")}],
            CapabilityHandshake("Codex"),
        )
        assignment = planner.start()[0]
        with self.assertRaisesRegex(NativeWaveError, "native_result_launch_unacknowledged"):
            planner.ingest([result_packet("one")])
        with self.assertRaisesRegex(NativeWaveError, "native_launch_receiver_id_invalid"):
            planner.acknowledge_launch("one", "")
        planner.acknowledge_launch("one", "receiver-one")
        with self.assertRaisesRegex(NativeWaveError, "native_launch_acknowledgement_replay"):
            planner.acknowledge_launch("one", "receiver-one")
        self.assertTrue(assignment["launch_acknowledged"])
        self.assertEqual(assignment["receiver_id"], "receiver-one")
        self.assertEqual(planner.ingest([result_packet("one")]), [])

    def test_capability_handshake_is_honest_and_fail_closed(self):
        with self.assertRaisesRegex(NativeWaveError, "provider_invalid"):
            NativeWavePlanner([{"task_id": "one", "packet": task_packet("one")}], CapabilityHandshake(""))
        with self.assertRaisesRegex(NativeWaveError, "capability_missing"):
            NativeWavePlanner(
                [{"task_id": "one", "packet": task_packet("one")}],
                CapabilityHandshake("Codex", spawn=False),
            )
        handshake = CapabilityHandshake("Codex").as_dict()
        self.assertFalse(handshake["model_selection"])
        self.assertFalse(handshake["reasoning_selection"])
        self.assertEqual(handshake["enforcement"], "advisory")

    def test_public_profile_is_portable_lazy_and_dependency_free(self):
        profile = json.loads((ROOT / "profiles/public-skill.json").read_text(encoding="utf-8"))
        self.assertTrue(profile["portable"])
        self.assertEqual(profile["dependencies"], [])
        self.assertEqual(len(profile["default_lazy_documents"]), 4)
        self.assertIn("agentfanout/", profile["excluded_by_default"])

    def test_economics_gate_records_quality_without_savings_claim(self):
        report = run_benchmark()
        self.assertTrue(report["all_gates_pass"])
        self.assertEqual(report["claim_status"], "not_a_savings_claim")
        self.assertTrue(all(item["quality_score"] > 0 for item in report["measurements"].values()))


if __name__ == "__main__":
    unittest.main()
