import copy
import json
import unittest
from pathlib import Path

from agentfanout.calibration import (
    CalibrationError,
    build_calibration_report,
    fixture_hash,
    select_cheapest_capable,
    total_record_cost,
    validate_fixture_manifest,
    validate_run_record,
    wilson_lower_bound,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "benchmarks" / "fixtures" / "manifest-v1.json").read_text(encoding="utf-8"))


def record(fixture, record_id, *, provider="Codex", model="codex-luna-fixture", effort="low", cost=0.10,
           retry=0.02, validator=0.03, energy=0.0, latency=100, success=True, safety=True,
           validator_pass=None, usage_verified=True, cost_verified=True, outcome=None):
    if validator_pass is None:
        validator_pass = success
    return {
        "schema": "CalibrationRunRecordV1", "record_id": record_id,
        "provider": provider, "exact_model_id": model, "reasoning_effort": effort,
        "task_class": fixture["task_class"], "tool_policy_sha256": "a" * 64,
        "fixture_id": fixture["fixture_id"], "fixture_sha256": fixture_hash(fixture),
        "observed_at": "2026-07-10T00:00:00Z", "outcome": outcome or ("success" if success else "failed"),
        "structural_validation_passed": success, "independent_validation_passed": validator_pass,
        "evidence_contract_passed": success, "safety_gate_passed": safety,
        "validated_success": success and validator_pass and safety, "input_tokens": 50, "cached_tokens": 0,
        "output_tokens": 20, "attempt_count": 2, "worker_cost_usd": cost,
        "retry_cost_usd": retry, "validator_cost_usd": validator, "energy_cost_usd": energy,
        "latency_ms": latency,
        "usage_verified": usage_verified,
        "cost_verified": cost_verified,
        "usage_evidence_ref": "fixture:usage-meter-v1" if usage_verified else None,
        "cost_evidence_ref": "fixture:price-card-v1" if cost_verified else None,
    }


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = validate_fixture_manifest(MANIFEST)
        self.low = self.fixtures["low-classification-v1"]
        self.safety = self.fixtures["safety-context-overflow-v1"]

    def test_manifest_covers_private_routing_decisions_without_registry_mutation(self):
        expected = {
            "direct-trivial-anti-waste-v1", "luna-light-read-v1", "luna-light-classify-v1",
            "terra-medium-engineering-v1", "sol-high-security-review-v1",
            "sol-high-architecture-v1", "local-unavailable-fail-closed-v1",
            "grok-unmetered-ineligible-v1", "residual-validator-overhead-v1",
            "contradictory-output-validation-v1",
        }
        self.assertTrue(expected.issubset(self.fixtures))
        self.assertEqual(self.fixtures["direct-trivial-anti-waste-v1"]["routing_mode"], "direct")
        self.assertEqual(self.fixtures["local-unavailable-fail-closed-v1"]["routing_mode"], "blocked")
        self.assertEqual(self.fixtures["sol-high-security-review-v1"]["intended_profile"], "sol-high")

    def test_direct_anti_waste_records_never_become_worker_candidates(self):
        direct = self.fixtures["direct-trivial-anti-waste-v1"]
        report = build_calibration_report(MANIFEST, [record(direct, "direct_001")])
        self.assertEqual(report["candidate_count"], 0)
        self.assertEqual(report["direct_route_record_count"], 1)
        self.assertEqual(report["direct_route_fixture_ids"], [direct["fixture_id"]])
        self.assertIsNone(select_cheapest_capable(report["candidates"]))

    def test_manifest_and_record_are_sanitized_and_deterministic(self):
        item = record(self.low, "run_001")
        validate_run_record(item, self.fixtures)
        self.assertEqual(len(fixture_hash(self.low)), 64)
        unsafe = copy.deepcopy(item)
        unsafe["exact_model_id"] = "secret-model"
        with self.assertRaisesRegex(CalibrationError, "sensitive"):
            validate_run_record(unsafe, self.fixtures)

    def test_local_requires_measured_compute_and_energy(self):
        item = record(self.low, "local_001", provider="Local", model="local-fixture")
        with self.assertRaisesRegex(CalibrationError, "local_gpu_seconds"):
            validate_run_record(item, self.fixtures)
        item["local_gpu_seconds"] = 12.0
        item["energy_kwh"] = 0.01
        validate_run_record(item, self.fixtures)

    def test_unavailable_local_fixture_requires_explicit_fail_closed_record(self):
        local = self.fixtures["local-unavailable-fail-closed-v1"]
        item = record(local, "local_unavailable", provider="Local", model="local-fixture", success=False, outcome="blocked")
        item["local_gpu_seconds"] = 0.0
        item["energy_kwh"] = 0.0
        validate_run_record(item, self.fixtures)

    def test_wilson_bound_is_conservative(self):
        self.assertEqual(wilson_lower_bound(0, 0), 0.0)
        self.assertGreater(wilson_lower_bound(9, 10), 0.59)
        self.assertLess(wilson_lower_bound(9, 10), 0.61)

    def test_report_counts_retries_and_validators_in_cost(self):
        records = [record(self.low, f"run_{index:03d}", cost=0.10, retry=0.02, validator=0.03) for index in range(30)]
        records.append({
            "schema": "CalibrationRunRecordV1", "record_id": "safety_001", "provider": "Codex",
            "exact_model_id": "codex-luna-fixture", "reasoning_effort": "low", "task_class": self.safety["task_class"],
            "tool_policy_sha256": "a" * 64, "fixture_id": self.safety["fixture_id"],
            "fixture_sha256": fixture_hash(self.safety), "observed_at": "2026-07-10T00:00:00Z", "outcome": "blocked",
            "structural_validation_passed": True, "independent_validation_passed": True, "evidence_contract_passed": True,
            "safety_gate_passed": True, "validated_success": False, "input_tokens": 50, "cached_tokens": 0,
            "output_tokens": 20, "attempt_count": 1, "worker_cost_usd": 0.01, "retry_cost_usd": 0.0,
            "validator_cost_usd": 0.0, "energy_cost_usd": 0.0, "latency_ms": 10,
            "usage_verified": True, "cost_verified": True,
            "usage_evidence_ref": "fixture:usage-meter-v1", "cost_evidence_ref": "fixture:price-card-v1",
        })
        report = build_calibration_report(MANIFEST, records, {
            "min_sample_size": 30, "min_quality_lcb": 0.85,
            "min_validator_quality_lcb": 0.85, "max_failure_rate": 0.1, "max_validator_failure_rate": 0.1,
        })
        candidate = report["candidates"][0]
        self.assertEqual(total_record_cost(records[0]), 0.15)
        self.assertEqual(candidate["cost_per_validated_success_usd"], 0.15)
        self.assertTrue(candidate["eligible"])
        self.assertTrue(report["manual_review_required"])
        self.assertFalse(report["automatic_registry_mutation"])
        self.assertEqual(select_cheapest_capable(report["candidates"])["provider"], "Codex")

    def test_safety_violation_prevents_eligibility(self):
        records = [record(self.low, f"run_{index:03d}") for index in range(30)]
        safety = {
            "schema": "CalibrationRunRecordV1", "record_id": "safety_bad", "provider": "Codex",
            "exact_model_id": "codex-luna-fixture", "reasoning_effort": "low", "task_class": self.safety["task_class"],
            "tool_policy_sha256": "a" * 64, "fixture_id": self.safety["fixture_id"],
            "fixture_sha256": fixture_hash(self.safety), "observed_at": "2026-07-10T00:00:00Z", "outcome": "blocked",
            "structural_validation_passed": True, "independent_validation_passed": True, "evidence_contract_passed": True,
            "safety_gate_passed": False, "validated_success": False, "input_tokens": 10, "cached_tokens": 0,
            "output_tokens": 1, "attempt_count": 1, "worker_cost_usd": 0.0, "retry_cost_usd": 0.0,
            "validator_cost_usd": 0.0, "energy_cost_usd": 0.0, "latency_ms": 1,
            "usage_verified": True, "cost_verified": True,
            "usage_evidence_ref": "fixture:usage-meter-v1", "cost_evidence_ref": "fixture:price-card-v1",
        }
        report = build_calibration_report(MANIFEST, records + [safety], {
            "min_sample_size": 30, "min_quality_lcb": 0.85,
            "min_validator_quality_lcb": 0.85, "max_failure_rate": 0.1, "max_validator_failure_rate": 0.1,
        })
        self.assertFalse(report["candidates"][0]["eligible"])
        self.assertIn("safety_gate_violation", report["candidates"][0]["ineligibility_reasons"])

    def test_unverified_usage_or_cost_is_ineligible_even_with_quality_and_sample_size(self):
        grok = self.fixtures["grok-unmetered-ineligible-v1"]
        records = [
            record(grok, f"grok_{index:03d}", provider="Grok", model="grok-fixture", usage_verified=False, cost_verified=False)
            for index in range(30)
        ]
        report = build_calibration_report(MANIFEST, records, {
            "min_sample_size": 30, "min_quality_lcb": 0.85, "min_validator_quality_lcb": 0.85,
        })
        candidate = report["candidates"][0]
        self.assertFalse(candidate["eligible"])
        self.assertEqual(candidate["verified_usage_runs"], 0)
        self.assertEqual(candidate["verified_cost_runs"], 0)
        self.assertIn("usage_evidence_unverified", candidate["ineligibility_reasons"])
        self.assertIn("cost_evidence_unverified", candidate["ineligibility_reasons"])
        self.assertIsNone(candidate["total_cost_usd"])
        self.assertIsNone(candidate["cost_per_validated_success_usd"])
        self.assertGreater(candidate["charged_or_reserved_cost_upper_bound_usd"], 0)
        self.assertIsNone(select_cheapest_capable(report["candidates"]))

    def test_validator_quality_and_minimum_sample_are_hard_eligibility_gates(self):
        contradictory = self.fixtures["contradictory-output-validation-v1"]
        undersampled = build_calibration_report(MANIFEST, [record(contradictory, "sol_one", model="sol-fixture", effort="high")], {
            "min_sample_size": 2, "min_quality_lcb": 0.0, "min_validator_quality_lcb": 0.0,
        })["candidates"][0]
        self.assertFalse(undersampled["eligible"])
        self.assertIn("sample_size_below_threshold", undersampled["ineligibility_reasons"])
        records = [record(contradictory, f"sol_{index:03d}", model="sol-fixture", effort="high") for index in range(30)]
        records[0] = record(contradictory, "sol_bad_validator", model="sol-fixture", effort="high", validator_pass=False)
        report = build_calibration_report(MANIFEST, records, {
            "min_sample_size": 30, "min_quality_lcb": 0.0, "min_validator_quality_lcb": 0.99,
            "max_failure_rate": 1.0, "max_validator_failure_rate": 1.0,
        })
        candidate = report["candidates"][0]
        self.assertFalse(candidate["eligible"])
        self.assertIn("validator_quality_below_threshold", candidate["ineligibility_reasons"])

    def test_residual_and_validator_costs_are_selection_cost_not_optional_overhead(self):
        fixture = self.fixtures["residual-validator-overhead-v1"]
        records = [record(fixture, f"overhead_{index:03d}", model="terra-fixture", effort="medium", cost=0.20, retry=0.05, validator=0.07) for index in range(30)]
        report = build_calibration_report(MANIFEST, records, {
            "min_sample_size": 30, "min_quality_lcb": 0.85, "min_validator_quality_lcb": 0.85,
        })
        candidate = report["candidates"][0]
        self.assertEqual(candidate["cost_per_validated_success_usd"], 0.32)
        self.assertTrue(candidate["eligible"])


if __name__ == "__main__":
    unittest.main()
