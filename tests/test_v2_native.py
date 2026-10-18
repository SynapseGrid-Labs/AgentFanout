"""Native V2 wave state and correlation checks; no provider calls."""
import os
import unittest
from unittest.mock import patch

from agentfanout.native import NativeWaveError
from agentfanout.v2_policy import process_native_wave

from tests.test_v2_policy import request, task


def step(state, action, **fields):
    if action == "result" and "host_receipts" not in fields:
        fields["host_receipts"] = [
            {"task_id": item["task_id"],
             "assignment_id": state["assignments"][item["task_id"]]["assignment_id"],
             "receiver_id": state["assignments"][item["task_id"]]["receiver_id"],
             "acceptance_check_passed": True, "structural_passed": True,
             "semantic_passed": item.get("validation", {}).get("semantic") is True,
             "validator_receiver_id": f"validator-{item['task_id']}"}
            for item in fields.get("results", []) if item.get("status") == "success"
        ]
    return process_native_wave({"action": action, "run_id": "run-1", "state": state, **fields})


def ack(response, assignment, receiver=None):
    return step(response["state"], "ack", task_id=assignment["task_id"],
                receiver_id=receiver or f"receiver-{assignment['task_id']}",
                actual_model=assignment["model_selection"]["requested_model"],
                actual_effort=assignment["model_selection"]["requested_effort"])


def result(assignment, *, status="success", failure_class=None, evidence=None,
           uncertainty=None, residual=None, semantic=True):
    item = {"task_id": assignment["task_id"], "assignment_id": assignment["assignment_id"],
            "receiver_id": f"receiver-{assignment['task_id']}", "attempt": assignment["attempt"],
            "actual_model": assignment["model_selection"]["requested_model"],
            "actual_effort": assignment["model_selection"]["requested_effort"],
            "status": status, "summary": "checked", "evidence": evidence if evidence is not None else ["src proof"],
            "uncertainty": uncertainty or [], "residual_work": residual or []}
    if status == "success":
        item.update({"acceptance_check_passed": True,
                     "validation": {"structural": True, "semantic": semantic}})
    else:
        item["failure_class"] = failure_class or "quality"
    return item


@patch.dict(os.environ, {"AGENT_FANOUT_V2_MASTER": "1"})
class V2NativeTests(unittest.TestCase):
    def test_two_phase_ack_and_correlated_completion(self):
        first = process_native_wave(request())
        assignment = first["assignments"][0]
        self.assertEqual(first["snapshot"]["wave"], 0)
        self.assertEqual(first["snapshot"]["attempts"]["one"], 0)
        acknowledged = ack(first, assignment)
        self.assertEqual(acknowledged["snapshot"]["wave"], 1)
        self.assertEqual(acknowledged["snapshot"]["attempts"]["one"], 1)
        with self.assertRaisesRegex(NativeWaveError, "correlation_mismatch"):
            step(acknowledged["state"], "result", results=[{**result(assignment), "receiver_id": "stale"}])
        completed = step(acknowledged["state"], "result", results=[result(assignment)])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["snapshot"]["completed_task_ids"], ["one"])

    def test_rejected_unacked_launch_charges_no_attempt(self):
        first = process_native_wave(request())
        rejected = step(first["state"], "reject", task_id="one")
        self.assertEqual(rejected["snapshot"]["wave"], 0)
        self.assertEqual(rejected["snapshot"]["attempts"]["one"], 0)
        self.assertEqual(len(rejected["assignments"]), 1)

    def test_batch_validates_before_mutation_and_dependencies_wait(self):
        candidate = request([task("a"), task("b"), task("c", depends_on=["a"])])
        first = process_native_wave(candidate)
        self.assertEqual({item["task_id"] for item in first["assignments"]}, {"a", "b"})
        current = first
        for assignment in first["assignments"]:
            current = ack(current, assignment)
        left, right = first["assignments"]
        with self.assertRaisesRegex(NativeWaveError, "correlation_mismatch"):
            step(current["state"], "result", results=[result(left), {**result(right), "attempt": 99}])
        self.assertEqual(set(current["snapshot"]["active_task_ids"]), {"a", "b"})
        with self.assertRaisesRegex(NativeWaveError, "duplicate"):
            step(current["state"], "result", results=[result(left), result(left)])
        next_wave = step(current["state"], "result", results=[result(left)])
        self.assertEqual([item["task_id"] for item in next_wave["assignments"]], ["c"])

    def test_cancel_returns_only_acknowledged_receivers(self):
        first = process_native_wave(request([task("a"), task("b")]))
        acknowledged = ack(first, first["assignments"][0])
        cancelled = step(acknowledged["state"], "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["cancel_receiver_ids"], ["receiver-a"])

    def test_dynamic_validation_and_residual_stop(self):
        first = process_native_wave(request())
        assignment = first["assignments"][0]
        acknowledged = ack(first, assignment)
        with self.assertRaisesRegex(NativeWaveError, "semantic_validation_missing"):
            step(acknowledged["state"], "result", results=[result(assignment, uncertainty=["open issue"], semantic=False)])
        blocked = step(acknowledged["state"], "result", results=[result(assignment, status="partial", residual=["new scope"])])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["snapshot"]["blocked_reason"], "v2_residual_new_plan_required")

    def test_worker_self_attestation_is_not_host_acceptance(self):
        first = process_native_wave(request())
        assignment = first["assignments"][0]
        acknowledged = ack(first, assignment)
        with self.assertRaisesRegex(NativeWaveError, "host_receipt_missing"):
            step(acknowledged["state"], "result", results=[result(assignment)], host_receipts=[])
        forged = {"task_id": "one", "assignment_id": assignment["assignment_id"],
                  "receiver_id": "receiver-one", "acceptance_check_passed": True,
                  "structural_passed": True, "semantic_passed": True,
                  "validator_receiver_id": "receiver-one"}
        hard = process_native_wave(request([task(task_class="hard", risk="high")]))
        hard_assignment = hard["assignments"][0]
        hard_ack = ack(hard, hard_assignment)
        forged["assignment_id"] = hard_assignment["assignment_id"]
        with self.assertRaisesRegex(NativeWaveError, "independent_validator_required"):
            step(hard_ack["state"], "result", results=[result(hard_assignment)], host_receipts=[forged])

    def test_master_off_drains_without_new_launch_and_rejections_bound(self):
        first = process_native_wave(request([task("a"), task("b", depends_on=["a"])]))
        a = first["assignments"][0]
        acknowledged = ack(first, a)
        with patch.dict(os.environ, {}, clear=True):
            drained = step(acknowledged["state"], "result", results=[result(a)])
        self.assertEqual(drained["status"], "blocked")
        self.assertEqual(drained["assignments"], [])
        rejected = process_native_wave(request())
        again = step(rejected["state"], "reject", task_id="one")
        self.assertEqual(again["snapshot"]["recovery_counts"]["one"]["rejection"], 1)
        bounded = step(again["state"], "reject", task_id="one")
        self.assertEqual(bounded["status"], "blocked")

    def test_repair_then_luna_to_sol_only_for_automatic_route(self):
        first = process_native_wave(request())
        a1 = first["assignments"][0]
        after_ack = ack(first, a1)
        second = step(after_ack["state"], "result", results=[result(a1, status="failed")])
        a2 = second["assignments"][0]
        self.assertEqual(a2["model_selection"]["tier"], "luna")
        after_ack = ack(second, a2)
        third = step(after_ack["state"], "result", results=[result(a2, status="failed")])
        a3 = third["assignments"][0]
        self.assertEqual(a3["model_selection"]["tier"], "sol")
        self.assertEqual(third["snapshot"]["recovery_counts"]["one"], {"repair": 1, "escalation": 1, "rejection": 0})
        pinned = process_native_wave(request([task(requested_model="gpt-6-luna", requested_effort="low")]))
        p1 = pinned["assignments"][0]
        pinned_second = step(ack(pinned, p1)["state"], "result", results=[result(p1, status="failed")])
        p2 = pinned_second["assignments"][0]
        pinned_ack = ack(pinned_second, p2)
        stopped = step(pinned_ack["state"], "result", results=[result(p2, status="failed")])
        self.assertEqual(stopped["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
