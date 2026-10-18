"""Offline, review-only calibration helpers for cheapest-capable routing.

This module never calls a provider, reads credentials, or changes the provider
registry. Hosts supply already-sanitized measured run records. The output is a
review artifact that may inform a later human-approved registry update.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable


MANIFEST_SCHEMA = "AgentFanoutBenchmarkManifestV1"
RUN_SCHEMA = "CalibrationRunRecordV1"
REPORT_SCHEMA = "CalibrationReportV1"
STRATA = {"low", "medium", "high", "safety"}
OUTCOMES = {"success", "blocked", "failed"}
EFFORTS = {"low", "medium", "high", "xhigh", "ultra"}
ROUTING_MODES = {"direct", "worker", "blocked"}
COST_BASES = {"provider_reported", "approved_free", "reservation_upper_bound", "unknown"}
SHA256 = re.compile(r"^[a-f0-9]{64}$")
SENSITIVE_TEXT = re.compile(
    r"(?i)(authorization|bearer|api[_-]?key|secret|password|private[_-]?key|credential|"
    r"\bsk-[a-z0-9_-]{12,}|\bghp_[a-z0-9_]{12,}|-----begin [a-z ]*private key-----)"
)


class CalibrationError(ValueError):
    """Raised when an offline calibration artifact is malformed or unsafe."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def fixture_hash(fixture: dict[str, Any]) -> str:
    """Return the stable hash of one manifest fixture, excluding no fields."""

    return hashlib.sha256(canonical_json(fixture)).hexdigest()


def _safe_text(value: Any, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CalibrationError(f"invalid_{field}")
    if SENSITIVE_TEXT.search(value):
        raise CalibrationError(f"sensitive_{field}")
    return value


def _nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CalibrationError(f"invalid_{field}")
    return float(value)


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CalibrationError(f"invalid_{field}")
    return value


def validate_fixture_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate deterministic, non-secret benchmark fixture metadata."""

    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise CalibrationError("invalid_fixture_manifest_schema")
    _safe_text(manifest.get("version"), "fixture_manifest_version", 100)
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise CalibrationError("fixture_manifest_required")
    indexed: dict[str, dict[str, Any]] = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise CalibrationError("invalid_fixture")
        fixture_id = _safe_text(fixture.get("fixture_id"), "fixture_id", 200)
        if fixture_id in indexed:
            raise CalibrationError("duplicate_fixture_id")
        if fixture.get("stratum") not in STRATA:
            raise CalibrationError("invalid_fixture_stratum")
        _safe_text(fixture.get("task_class"), "fixture_task_class", 100)
        _safe_text(fixture.get("tool_policy"), "fixture_tool_policy", 200)
        _safe_text(fixture.get("input_size_tier"), "fixture_input_size_tier", 100)
        if fixture.get("routing_mode") not in ROUTING_MODES:
            raise CalibrationError("invalid_fixture_routing_mode")
        _safe_text(fixture.get("intended_profile"), "fixture_intended_profile", 100)
        if fixture.get("expected_outcome") not in {"success", "blocked"}:
            raise CalibrationError("invalid_fixture_expected_outcome")
        acceptance = fixture.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance or not all(isinstance(item, str) for item in acceptance):
            raise CalibrationError("invalid_fixture_acceptance")
        for item in acceptance:
            _safe_text(item, "fixture_acceptance", 500)
        indexed[fixture_id] = deepcopy(fixture)
    return indexed


def validate_run_record(record: dict[str, Any], fixtures: dict[str, dict[str, Any]]) -> None:
    """Validate a host-measured, sanitized logical task attempt aggregate."""

    if not isinstance(record, dict) or record.get("schema") != RUN_SCHEMA:
        raise CalibrationError("invalid_run_record_schema")
    for field, maximum in (
        ("record_id", 200), ("provider", 100), ("exact_model_id", 500),
        ("task_class", 100), ("tool_policy_sha256", 64), ("fixture_id", 200),
        ("fixture_sha256", 64), ("observed_at", 100),
    ):
        _safe_text(record.get(field), field, maximum)
    if not SHA256.fullmatch(record["tool_policy_sha256"]):
        raise CalibrationError("invalid_tool_policy_sha256")
    if not SHA256.fullmatch(record["fixture_sha256"]):
        raise CalibrationError("invalid_fixture_sha256")
    if record.get("reasoning_effort") not in EFFORTS:
        raise CalibrationError("invalid_reasoning_effort")
    fixture = fixtures.get(record["fixture_id"])
    if not fixture:
        raise CalibrationError("fixture_not_found")
    if fixture_hash(fixture) != record["fixture_sha256"]:
        raise CalibrationError("fixture_hash_mismatch")
    if record["task_class"] != fixture["task_class"]:
        raise CalibrationError("fixture_task_class_mismatch")
    if record.get("outcome") not in OUTCOMES:
        raise CalibrationError("invalid_outcome")
    try:
        datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalibrationError("invalid_observed_at") from exc
    for field in ("structural_validation_passed", "independent_validation_passed", "evidence_contract_passed", "safety_gate_passed"):
        if not isinstance(record.get(field), bool):
            raise CalibrationError(f"invalid_{field}")
    for field in ("usage_verified", "cost_verified"):
        if not isinstance(record.get(field), bool):
            raise CalibrationError(f"invalid_{field}")
    if record["usage_verified"]:
        _safe_text(record.get("usage_evidence_ref"), "usage_evidence_ref", 300)
    elif record.get("usage_evidence_ref") not in (None, ""):
        raise CalibrationError("unverified_usage_cannot_claim_evidence")
    if record["cost_verified"]:
        _safe_text(record.get("cost_evidence_ref"), "cost_evidence_ref", 300)
    elif record.get("cost_evidence_ref") not in (None, ""):
        raise CalibrationError("unverified_cost_cannot_claim_evidence")
    cost_basis = record.get("cost_basis", "provider_reported" if record["cost_verified"] else "unknown")
    if cost_basis not in COST_BASES:
        raise CalibrationError("invalid_cost_basis")
    if record["cost_verified"] and cost_basis not in {"provider_reported", "approved_free"}:
        raise CalibrationError("verified_cost_basis_invalid")
    if not record["cost_verified"] and cost_basis in {"provider_reported", "approved_free"}:
        raise CalibrationError("unverified_cost_basis_invalid")
    for field in ("input_tokens", "cached_tokens", "output_tokens", "attempt_count"):
        _positive_integer(record.get(field), field) if field == "attempt_count" else _nonnegative_number(record.get(field), field)
    for field in ("worker_cost_usd", "retry_cost_usd", "validator_cost_usd", "energy_cost_usd", "latency_ms"):
        _nonnegative_number(record.get(field), field)
    if str(record["provider"]).lower() == "local":
        _nonnegative_number(record.get("local_gpu_seconds"), "local_gpu_seconds")
        _nonnegative_number(record.get("energy_kwh"), "energy_kwh")
    elif record.get("local_gpu_seconds") is not None or record.get("energy_kwh") is not None:
        raise CalibrationError("local_compute_metrics_on_nonlocal_record")
    if fixture["stratum"] == "safety":
        if record["outcome"] != fixture["expected_outcome"]:
            raise CalibrationError("safety_fixture_expected_outcome_mismatch")
        if record.get("validated_success") is True:
            raise CalibrationError("safety_fixture_must_not_count_as_quality_success")
    else:
        expected_success = (
            record["outcome"] == "success"
            and record["structural_validation_passed"]
            and record["independent_validation_passed"]
            and record["evidence_contract_passed"]
            and record["safety_gate_passed"]
        )
        if record.get("validated_success") is not expected_success:
            raise CalibrationError("validated_success_mismatch")


def wilson_lower_bound(successes: int, total: int, z: float = 1.959963984540054) -> float:
    """Two-sided 95% Wilson lower confidence bound for a binomial outcome."""

    if isinstance(successes, bool) or isinstance(total, bool) or total < 0 or successes < 0 or successes > total:
        raise CalibrationError("invalid_wilson_inputs")
    if total == 0:
        return 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    center = proportion + z * z / (2 * total)
    spread = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
    return max(0.0, (center - spread) / denominator)


def p95(values: Iterable[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def total_record_cost(record: dict[str, Any]) -> float:
    """Count all worker, repair, validation, and local-energy costs."""

    return round(sum(float(record[field]) for field in (
        "worker_cost_usd", "retry_cost_usd", "validator_cost_usd", "energy_cost_usd"
    )), 8)


def _candidate_key(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        record["provider"], record["exact_model_id"], record["reasoning_effort"],
        record["task_class"], record["tool_policy_sha256"],
    )


def _model_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    """Safety evidence follows the exact provider/model/effort across task classes."""

    return record["provider"], record["exact_model_id"], record["reasoning_effort"]


def _threshold_for(thresholds: dict[str, Any], stratum: str, name: str, default: float | int) -> float | int:
    return thresholds.get("by_stratum", {}).get(stratum, {}).get(name, thresholds.get(name, default))


def candidate_metrics(records: list[dict[str, Any]], stratum: str, thresholds: dict[str, Any]) -> dict[str, Any]:
    """Aggregate one exact model/task/policy cohort into reviewable evidence."""

    non_safety = [record for record in records if record["fixture_stratum"] != "safety"]
    safety = [record for record in records if record["fixture_stratum"] == "safety"]
    workload_total = len(non_safety)
    successes = sum(1 for record in non_safety if record["validated_success"])
    failed = sum(1 for record in non_safety if record["outcome"] == "failed")
    validator_failures = sum(1 for record in non_safety if not record["independent_validation_passed"])
    validator_successes = workload_total - validator_failures
    unverified_usage = sum(1 for record in non_safety if not record["usage_verified"])
    unverified_cost = sum(1 for record in non_safety if not record["cost_verified"])
    cost_basis_counts: dict[str, int] = {}
    for record in non_safety:
        basis = str(record.get("cost_basis", "provider_reported" if record["cost_verified"] else "unknown"))
        cost_basis_counts[basis] = cost_basis_counts.get(basis, 0) + 1
    safety_violations = sum(1 for record in safety if not record["safety_gate_passed"])
    charged_or_reserved_cost = sum(total_record_cost(record) for record in non_safety)
    verified_total_cost = None if unverified_cost else charged_or_reserved_cost
    quality_lcb = wilson_lower_bound(successes, workload_total)
    validator_quality_lcb = wilson_lower_bound(validator_successes, workload_total)
    min_sample_size = int(_threshold_for(thresholds, stratum, "min_sample_size", 30))
    min_quality_lcb = float(_threshold_for(thresholds, stratum, "min_quality_lcb", 0.95))
    min_validator_quality_lcb = float(_threshold_for(thresholds, stratum, "min_validator_quality_lcb", 0.95))
    max_failure_rate = float(_threshold_for(thresholds, stratum, "max_failure_rate", 0.02))
    max_validator_failure_rate = float(_threshold_for(thresholds, stratum, "max_validator_failure_rate", 0.02))
    # Keep the review artifact strict-JSON serializable; an explicit, generous
    # default is safer than emitting a non-standard Infinity value.
    max_p95_latency_ms = float(_threshold_for(thresholds, stratum, "max_p95_latency_ms", 300_000))
    reasons: list[str] = []
    if workload_total < min_sample_size:
        reasons.append("sample_size_below_threshold")
    if quality_lcb < min_quality_lcb:
        reasons.append("quality_confidence_below_threshold")
    if validator_quality_lcb < min_validator_quality_lcb:
        reasons.append("validator_quality_below_threshold")
    if unverified_usage:
        reasons.append("usage_evidence_unverified")
    if unverified_cost:
        reasons.append("cost_evidence_unverified")
    failure_rate = failed / workload_total if workload_total else 1.0
    validator_failure_rate = validator_failures / workload_total if workload_total else 1.0
    latency = p95(record["latency_ms"] for record in non_safety)
    if failure_rate > max_failure_rate:
        reasons.append("failure_rate_above_threshold")
    if validator_failure_rate > max_validator_failure_rate:
        reasons.append("validator_failure_rate_above_threshold")
    if latency > max_p95_latency_ms:
        reasons.append("p95_latency_above_threshold")
    if safety_violations:
        reasons.append("safety_gate_violation")
    return {
        "sample_size": workload_total,
        "validated_successes": successes,
        "quality_lcb_95": round(quality_lcb, 6),
        "validator_quality_lcb_95": round(validator_quality_lcb, 6),
        "failure_rate": round(failure_rate, 6),
        "validator_failure_rate": round(validator_failure_rate, 6),
        "verified_usage_runs": workload_total - unverified_usage,
        "verified_cost_runs": workload_total - unverified_cost,
        "evidence_complete": not unverified_usage and not unverified_cost,
        "p95_latency_ms": latency,
        "cost_basis_counts": cost_basis_counts,
        "charged_or_reserved_cost_upper_bound_usd": round(charged_or_reserved_cost, 8),
        "total_cost_usd": round(verified_total_cost, 8) if verified_total_cost is not None else None,
        "cost_per_validated_success_usd": round(verified_total_cost / successes, 8) if successes and verified_total_cost is not None else None,
        "safety_fixture_runs": len(safety),
        "safety_violations": safety_violations,
        "eligible": not reasons,
        "ineligibility_reasons": reasons,
        "thresholds": {
            "min_sample_size": min_sample_size,
            "min_quality_lcb": min_quality_lcb,
            "min_validator_quality_lcb": min_validator_quality_lcb,
            "max_failure_rate": max_failure_rate,
            "max_validator_failure_rate": max_validator_failure_rate,
            "max_p95_latency_ms": max_p95_latency_ms,
        },
    }


def build_calibration_report(
    manifest: dict[str, Any], records: list[dict[str, Any]], thresholds: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Create a sanitized report; it intentionally contains no mutation command."""

    fixtures = validate_fixture_manifest(manifest)
    if not isinstance(records, list) or not records:
        raise CalibrationError("calibration_records_required")
    policy = deepcopy(thresholds or {})
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    safety_by_model: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    direct_route_records: list[dict[str, Any]] = []
    for raw in records:
        validate_run_record(raw, fixtures)
        record = deepcopy(raw)
        fixture = fixtures[record["fixture_id"]]
        record["fixture_stratum"] = fixture["stratum"]
        if fixture["routing_mode"] == "direct":
            direct_route_records.append(record)
            continue
        if record["fixture_stratum"] == "safety":
            safety_by_model[_model_identity(record)].append(record)
        else:
            grouped[_candidate_key(record)].append(record)
    candidates: list[dict[str, Any]] = []
    for key, workload in sorted(grouped.items()):
        cohort = workload + safety_by_model.get((key[0], key[1], key[2]), [])
        workload_strata = {record["fixture_stratum"] for record in cohort if record["fixture_stratum"] != "safety"}
        stratum = next(iter(workload_strata)) if len(workload_strata) == 1 else "mixed"
        metrics = candidate_metrics(cohort, stratum, policy)
        candidates.append({
            "provider": key[0], "exact_model_id": key[1], "reasoning_effort": key[2],
            "task_class": key[3], "tool_policy_sha256": key[4], "stratum": stratum,
            **metrics,
        })
    return {
        "schema": REPORT_SCHEMA,
        "manifest_version": manifest["version"],
        "manifest_sha256": hashlib.sha256(canonical_json(manifest)).hexdigest(),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "direct_route_record_count": len(direct_route_records),
        "direct_route_fixture_ids": sorted({record["fixture_id"] for record in direct_route_records}),
        "selection_policy": "minimum_cost_per_validated_success_subject_to_eligibility",
        "manual_review_required": True,
        "automatic_registry_mutation": False,
        "proposed_registry_changes": [],
    }


def select_cheapest_capable(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a recommendation only; callers must not treat it as an update action."""

    eligible = [
        candidate for candidate in candidates
        if (
            candidate.get("eligible") is True
            and candidate.get("cost_per_validated_success_usd") is not None
            and candidate.get("evidence_complete") is True
            and candidate.get("sample_size", 0) >= candidate.get("thresholds", {}).get("min_sample_size", 1)
            and candidate.get("validator_quality_lcb_95", 0.0)
            >= candidate.get("thresholds", {}).get("min_validator_quality_lcb", 1.0)
        )
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda candidate: (
        float(candidate["cost_per_validated_success_usd"]), float(candidate["p95_latency_ms"]),
        candidate["provider"], candidate["exact_model_id"],
    ))
