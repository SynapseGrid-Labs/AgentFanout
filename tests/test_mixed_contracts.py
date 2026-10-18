"""Synthetic admission/egress/recovery tests; no live providers/coordinator."""
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.egress import CATEGORIES, EgressError, authorize_packet, filter_peer_evidence, validate_policy
from agentfanout.mixed_contracts import (
    MixedContractError, assert_execution_ready, assert_reader_compatible,
    assert_rollback_safe, authorize_transition, binding_for, canonical_digest,
    freeze_contract, validate_contract,
)


def policy(local_only=False):
    destinations = {"worker.local": {"kind": "local", "provider": "Local"}, "validator.local": {"kind": "local", "provider": "Local"}, "grok.cloud": {"kind": "cloud", "provider": "GrokCLI"}, "parent.host": {"kind": "host", "provider": "Host"}}
    allowed = [name for name in destinations if not local_only or name != "grok.cloud"]
    return {"schema": "EvidenceEgressPolicyV1", "destinations": destinations, "allowed": {category: list(allowed) for category in CATEGORIES}}


def runtime():
    return {"source_commit": "a" * 40, "release_id": "candidate-reviewed", "manifest_sha256": "b" * 64, "entrypoint_sha256": "c" * 64, "contracts_sha256": "d" * 64}


def resource(role):
    return {"reservation_id": "reservation-" + role, "domain": "worker-domain", "lease_id": "lease-" + role, "serving_epoch": "epoch-1", "fencing_token": "fence-1", "readiness": "ready", "ownership": "certain", "cancellation": "none", "expires_at": 200, "purpose": role}


def binding(provider="Local", role="worker"):
    return {"provider": provider, "mode": "coordinator_local" if provider == "Local" else "grok_cli_subscription_acp", "model_selector": "aeon" if provider == "Local" else "frontier", "resolved_model": "AEON-7/Qwen3.8-27B-AEON-ULTIMATE-UNCENSORED-BF16" if provider == "Local" else "grok-model", "reasoning_effort": "high", "identity_sha256": "e" * 64, "resource": resource(role) if provider == "Local" else None, "reservation_cost_usd": 0.1, "destination_id": role + ".local" if provider == "Local" else "grok.cloud"}


def assignment(mixed=True):
    return {"assignment_id": "task_1", "worker": binding(), "validator": binding("GrokCLI" if mixed else "Local", "validator"), "validation_required": True, "egress_policy": policy()}


def contract(assignments=None, transitions=None, controls=None):
    return freeze_contract("run_test", runtime(), assignments or [assignment()], allowed_transitions=transitions, feature_controls=controls if controls is not None else {"coordinator_local": True, "grok_cli_subscription": True, "mixed_provider_runs": True})


class EgressTests(unittest.TestCase):
    def test_local_only_denies_every_cloud_surface(self):
        for category in CATEGORIES:
            with self.subTest(category=category), self.assertRaisesRegex(EgressError, "destination_denied"):
                authorize_packet(policy(True), category, "grok.cloud", {"confidential": "private evidence"})

    def test_missing_policy_and_category_fail_closed(self):
        for category in CATEGORIES:
            broken = policy()
            del broken["allowed"][category]
            with self.subTest(category=category), self.assertRaisesRegex(EgressError, "categories_incomplete"):
                validate_policy(broken)
        with self.assertRaisesRegex(EgressError, "unsupported_egress_policy"):
            authorize_packet(None, "input", "worker.local", "text")

    def test_cloud_parent_cannot_be_labeled_local(self):
        broken = policy()
        broken["destinations"]["parent.host"] = {"kind": "host", "provider": "GrokCLI"}
        with self.assertRaisesRegex(EgressError, "provider_mismatch"):
            validate_policy(broken)

    def test_peers_use_source_policies_and_no_identifiers_leak(self):
        peers = [(policy(True), {"task_id": "secret_task", "summary": "private"}), (policy(), {"task_id": "public_task", "summary": "approved"})]
        self.assertEqual(filter_peer_evidence(peers, "grok.cloud"), [{"task_id": "public_task", "summary": "approved"}])
        self.assertNotIn("secret_task", json.dumps(filter_peer_evidence(peers, "grok.cloud")))

    def test_secret_screen_is_preserved_for_local_and_cloud(self):
        packets = ["xai-" + "a" * 32, {"nested": ["-----BEGIN RSA " + "PRIVATE KEY-----\ncontent"]}, {"api_key": "secretcredential123"}]
        for packet in packets:
            for destination in ("worker.local", "grok.cloud"):
                with self.subTest(packet=packet, destination=destination), self.assertRaisesRegex(EgressError, "sensitive_content"):
                    authorize_packet(policy(), "input", destination, packet)

    def test_payload_limits_and_numeric_truth(self):
        for packet in (float("nan"), {"usage": float("inf")}, object()):
            with self.assertRaisesRegex(EgressError, "not_bounded_json"):
                authorize_packet(policy(), "telemetry", "parent.host", packet)
        with self.assertRaisesRegex(EgressError, "too_large"):
            authorize_packet(policy(), "artifact", "parent.host", "x" * (1024 * 1024))

    def test_authorized_packet_is_copy_and_unknown_destination_denied(self):
        packet = {"answer": ["ok"]}
        output = authorize_packet(policy(), "worker_result", "worker.local", packet)
        output["answer"].append("mutated")
        self.assertEqual(packet, {"answer": ["ok"]})
        with self.assertRaises(EgressError):
            authorize_packet(policy(), "parent_synthesis", "ambient.parent", packet)


class MixedContractTests(unittest.TestCase):
    def test_frozen_contract_is_isolated_and_tampering_detected(self):
        assignments = [assignment()]
        value = contract(assignments)
        assignments[0]["worker"]["resolved_model"] = "replacement"
        self.assertNotEqual(value["assignments"][0]["worker"]["resolved_model"], "replacement")
        value["assignments"][0]["worker"]["resolved_model"] = "replacement"
        with self.assertRaisesRegex(MixedContractError, "digest_mismatch"):
            validate_contract(value)

    def test_old_reader_and_different_runtime_refuse_active_run(self):
        value = contract()
        with self.assertRaisesRegex(MixedContractError, "reader_schema_unsupported"):
            assert_reader_compatible(value, runtime(), supported_schemas={"RunStateV2"})
        assert_reader_compatible(value, runtime(), supported_schemas={"MixedRunContractV1"})
        for field in runtime():
            modified = runtime()
            modified[field] = "f" * (40 if field == "source_commit" else 64)
            with self.subTest(field=field), self.assertRaisesRegex(MixedContractError, "runtime_mismatch"):
                assert_reader_compatible(value, modified, supported_schemas={"MixedRunContractV1"})

    def test_grok_cli_never_means_old_grok(self):
        a = assignment()
        a["validator"]["provider"] = "Grok"
        with self.assertRaisesRegex(MixedContractError, "provider_mode_mismatch"):
            contract([a])

    def test_namespaced_artifact_data_not_path_authority(self):
        value = contract()
        self.assertIn("/", binding_for(value, "task_1", "worker")["resolved_model"])
        for invalid_id in ("../escape", "task\nexec", "$(command)"):
            a = assignment()
            a["assignment_id"] = invalid_id
            with self.assertRaises(MixedContractError):
                contract([a])

    def test_validator_absence_is_not_worker_success(self):
        a = assignment()
        a["validator"] = None
        with self.assertRaisesRegex(MixedContractError, "required_validator_unbound"):
            contract([a])
        a["validation_required"] = False
        value = contract([a])
        self.assertIsNone(binding_for(value, "task_1", "validator"))

    def test_validator_wait_and_uncertain_owner_block_before_worker(self):
        for field, bad in (("readiness", "waiting"), ("ownership", "uncertain"), ("cancellation", "uncertain")):
            a = assignment(False)
            a["validator"]["resource"][field] = bad
            value = contract([a])
            with self.subTest(field=field), self.assertRaises(MixedContractError):
                assert_execution_ready(value, "task_1", now=100, observed_resources={"worker": a["worker"]["resource"], "validator": a["validator"]["resource"]})
            self.assertNotIn("wave_index", value)
            self.assertNotIn("attempts", value)

    def test_independent_observation_and_epoch_lease_fence_match_required(self):
        value = contract()
        with self.assertRaisesRegex(MixedContractError, "not_independently_observed"):
            assert_execution_ready(value, "task_1", now=100)
        assert_execution_ready(value, "task_1", now=100, observed_resources={"worker": resource("worker")})
        for field in ("serving_epoch", "fencing_token", "lease_id", "reservation_id", "domain"):
            observed = resource("worker")
            observed[field] = "changed"
            with self.subTest(field=field), self.assertRaisesRegex(MixedContractError, "epoch_or_lease_drift"):
                assert_execution_ready(value, "task_1", now=100, observed_resources={"worker": observed})
        with self.assertRaisesRegex(MixedContractError, "evidence_expired"):
            assert_execution_ready(value, "task_1", now=200, observed_resources={"worker": resource("worker")})

    def test_validator_resources_are_independently_reserved(self):
        a = assignment(False)
        a["validator"]["resource"]["reservation_id"] = a["worker"]["resource"]["reservation_id"]
        with self.assertRaisesRegex(MixedContractError, "validator_resource_not_reserved"):
            assert_execution_ready(contract([a]), "task_1", now=100, observed_resources={role: a[role]["resource"] for role in ("worker", "validator")})

    def test_current_admission_disable_preserves_frozen_reader_contract(self):
        value = contract()
        for flag in ("mixed_provider_runs", "grok_cli_subscription", "coordinator_local"):
            controls = deepcopy(value["feature_controls"])
            controls[flag] = False
            with self.subTest(flag=flag), self.assertRaisesRegex(MixedContractError, "admission_disabled"):
                assert_execution_ready(value, "task_1", now=100, admission_controls=controls, observed_resources={"worker": resource("worker")})
        assert_reader_compatible(value, runtime(), supported_schemas={"MixedRunContractV1"})

    def test_transition_set_frozen_no_silent_fallback(self):
        value = contract()
        current = binding_for(value, "task_1", "worker")
        next_binding = binding("GrokCLI")
        with self.assertRaisesRegex(MixedContractError, "transition_not_authorized"):
            authorize_transition(value, "task_1", "worker", current, next_binding)
        transition = {"assignment_id": "task_1", "role": "worker", "from_binding_sha256": canonical_digest(current), "to_binding": next_binding}
        value = contract(transitions=[transition])
        authorize_transition(value, "task_1", "worker", current, next_binding)
        next_binding["resolved_model"] = "unknown-model"
        with self.assertRaises(MixedContractError):
            authorize_transition(value, "task_1", "worker", current, next_binding)

    def test_transition_egress_uses_frozen_source_policy(self):
        a = assignment(False)
        a["egress_policy"] = policy(True)
        transition = {"assignment_id": "task_1", "role": "worker", "from_binding_sha256": canonical_digest(a["worker"]), "to_binding": binding("GrokCLI")}
        with self.assertRaisesRegex(MixedContractError, "binding_egress_denied"):
            contract([a], [transition])

    def test_numeric_reservation_never_null_nan_or_bool(self):
        for bad in (None, True, -1, math.nan, math.inf):
            a = assignment()
            a["validator"]["reservation_cost_usd"] = bad
            with self.subTest(bad=bad), self.assertRaises(MixedContractError):
                contract([a])
        self.assertEqual(binding_for(contract(), "task_1", "validator")["reservation_cost_usd"], 0.1)

    def test_rollback_unknown_effects_or_accounting_refuse(self):
        value = contract()
        assert_rollback_safe(value, active_effects=[], accounting_reconciled=True)
        effect = {"effect_id": "effect-1", "status": "reconciled", "ownership": "certain", "cancellation": "confirmed"}
        assert_rollback_safe(value, active_effects=[effect], accounting_reconciled=True)
        for field, bad in (("status", "running"), ("ownership", "uncertain"), ("cancellation", "uncertain")):
            broken = deepcopy(effect)
            broken[field] = bad
            with self.subTest(field=field), self.assertRaisesRegex(MixedContractError, "effects_unresolved"):
                assert_rollback_safe(value, active_effects=[broken], accounting_reconciled=True)
        with self.assertRaisesRegex(MixedContractError, "accounting_unreconciled"):
            assert_rollback_safe(value, active_effects=[], accounting_reconciled=False)


if __name__ == "__main__":
    unittest.main()
