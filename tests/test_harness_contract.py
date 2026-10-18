import unittest
from datetime import datetime, timedelta, timezone

from agentfanout.harness import (
    HarnessContractError,
    authorize_native_action,
    check_conformance,
    classify_native_action,
    manifest_digest,
    validate_manifest,
)


def manifest(level="runner_enforced", control="absent", authority=None):
    now = datetime.now(timezone.utc)
    value = {
        "schema": "HostCapabilityManifestV1",
        "harness": {"name": "fixture", "version": "1"},
        "enforcement": {
            "level": level, "native_worker_tool_control": control,
            "unknown_tool_policy": "deny", "anti_bypass_certified": False,
            "enforcement_contract_declared": level != "advisory",
            "native_worker_tools": [] if control == "absent" else ["spawn_agent"],
        },
        "provider_adapters": [{
            "provider": "Codex", "adapter_id": "fixture.codex", "version": "1",
            "input_modes": ["bounded_file_text"], "usage_proof": "fixture", "cancellation": "confirmed",
        }],
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat(),
    }
    if authority:
        value["attestation"] = {"authority": authority}
    return value


def bound(events):
    return [
        {
            **event, "run_id": "run_fixture", "guard_id": "guard_fixture",
            "trace_nonce": "nonce_fixture", "sequence": index,
        }
        for index, event in enumerate(events, 1)
    ]


class HarnessContractTests(unittest.TestCase):
    def test_aliases_and_active_guard_deny_native_spawn(self):
        self.assertEqual(classify_native_action("multi_agent_v1__spawn_agent"), "worker_spawn")
        decision = authorize_native_action(manifest(), "collaboration.spawn_agent", guard_active=True, delegable_residual=True)
        self.assertFalse(decision["authorized"])
        self.assertFalse(decision["certified"])
        self.assertTrue(decision["contract_enforced"])

    def test_detect_only_cannot_claim_enforcement(self):
        with self.assertRaisesRegex(HarnessContractError, "native_tool_control_insufficient"):
            validate_manifest(manifest(control="detect_only"))
        advisory = manifest(level="advisory", control="detect_only")
        advisory["enforcement"]["anti_bypass_certified"] = False
        self.assertEqual(validate_manifest(advisory)["enforcement"]["level"], "advisory")

    def test_shipped_runner_manifest_is_current_and_valid(self):
        import json
        from pathlib import Path
        shipped = json.loads((Path(__file__).resolve().parents[1] / "integrations/codex/standalone-runner-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_manifest(shipped, require_level="runner_enforced")["enforcement"]["level"], "runner_enforced")

    def test_enforced_unknown_tools_must_fail_closed(self):
        value = manifest()
        value["enforcement"]["unknown_tool_policy"] = "allow"
        with self.assertRaisesRegex(HarnessContractError, "unknown_tool_policy_must_deny"):
            validate_manifest(value)

    def test_duplicate_provider_adapter_entries_are_rejected(self):
        value = manifest()
        value["provider_adapters"].append({**value["provider_adapters"][0], "adapter_id": "fixture.other"})
        with self.assertRaisesRegex(HarnessContractError, "provider_adapter_manifest_duplicate"):
            validate_manifest(value)

    def test_harness_enforcement_requires_trusted_authority(self):
        value = manifest(level="harness_enforced", control="mediated", authority="test-host")
        with self.assertRaisesRegex(HarnessContractError, "attestation_untrusted"):
            validate_manifest(value)
        self.assertEqual(
            validate_manifest(
                value, trusted_authorities=["test-host"],
                trusted_manifest_digests=[manifest_digest(value)],
            )["enforcement"]["level"],
            "harness_enforced",
        )

    def test_trace_rejects_parent_fallback_and_terminal_residual(self):
        events = bound([
            {"type": "session_guard_armed"},
            {"type": "native_tool_inventory", "complete": True, "tools": []},
            {"type": "native_tool_attempt", "tool": "spawn_agent", "guard_active": True, "delegable_residual": True, "authorized": False},
            {"type": "worker_unavailable", "fallback_target": "parent"},
            {"type": "terminal_transition", "delegable_residual": True, "authorized": True},
        ])
        result = check_conformance(manifest(), events, require_level="runner_enforced")
        self.assertFalse(result["passed"])
        self.assertIn("event_3:silent_parent_fallback", result["failures"])
        self.assertIn("event_4:terminal_with_delegable_residual", result["failures"])

    def test_minimal_runner_trace_passes(self):
        result = check_conformance(
            manifest(),
            bound([
                {"type": "session_guard_armed"},
                {"type": "native_tool_inventory", "complete": True, "tools": []},
                {"type": "native_tool_attempt", "tool": "spawn_agent", "guard_active": True, "delegable_residual": True, "authorized": False},
                {"type": "native_tool_attempt", "tool": "unregistered_fixture_tool", "guard_active": True, "delegable_residual": True, "authorized": False},
                {"type": "terminal_transition", "delegable_residual": True, "authorized": False},
                {"type": "worker_unavailable", "fallback_allowed": False, "fallback_target": None},
                {"type": "worker_launch", "wave_id": 1, "task_id": "task_a", "launch_id": "launch_a", "worker_id": "worker_a", "provider": "Codex", "model": "fixture-model"},
                {"type": "worker_result", "wave_id": 1, "task_id": "task_a", "launch_id": "launch_a", "worker_id": "worker_a", "provider": "Codex", "model": "fixture-model"},
                {"type": "validator_result", "wave_id": 1, "task_id": "task_a", "validated_launch_id": "launch_a", "worker_id": "worker_a", "validator_id": "validator_a", "validator_provider": "Codex", "validator_model": "fixture-validator", "verdict": "passed"},
                {"type": "result_batch_replanned", "wave_id": 1, "residual_before": 1, "action": "follow_up"},
                {"type": "worker_launch", "wave_id": 2, "task_id": "task_b", "launch_id": "launch_b", "worker_id": "worker_b", "provider": "Codex", "model": "fixture-model"},
                {"type": "worker_result", "wave_id": 2, "task_id": "task_b", "launch_id": "launch_b", "worker_id": "worker_b", "provider": "Codex", "model": "fixture-model"},
                {"type": "validator_result", "wave_id": 2, "task_id": "task_b", "validated_launch_id": "launch_b", "worker_id": "worker_b", "validator_id": "validator_b", "validator_provider": "Codex", "validator_model": "fixture-validator", "verdict": "passed"},
                {"type": "terminal_transition", "delegable_residual": False, "authorized": True},
            ]),
            require_level="runner_enforced",
        )
        self.assertTrue(result["passed"], result["failures"])

    def test_blocked_replan_and_unapproved_fallback_do_not_pass(self):
        events = bound([
            {"type": "session_guard_armed"},
            {"type": "native_tool_inventory", "complete": True, "tools": []},
            {"type": "native_tool_attempt", "tool": "spawn_agent", "guard_active": True, "delegable_residual": True, "authorized": False},
            {"type": "native_tool_attempt", "tool": "unknown", "guard_active": True, "delegable_residual": True, "authorized": False},
            {"type": "terminal_transition", "delegable_residual": True, "authorized": False},
            {"type": "worker_unavailable", "fallback_allowed": False, "fallback_target": "evil-provider"},
            {"type": "result_batch_replanned", "wave_id": 1, "residual_before": 1, "action": "blocked"},
            {"type": "run_blocked"},
        ])
        result = check_conformance(manifest(), events, require_level="runner_enforced")
        self.assertFalse(result["passed"])
        self.assertIn("event_5:unapproved_provider_fallback", result["failures"])
        self.assertIn("trace:required_scenario_missing:post_batch_replan", result["failures"])


if __name__ == "__main__":
    unittest.main()
