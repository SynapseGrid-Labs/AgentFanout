"""Validate matched measurement records without creating live evidence.

Concurrency and routing are different experiments. Per-domain memory is never
summed into a shared capacity. Subscription authentication is not cost evidence.
"""
from __future__ import annotations

import math
from typing import Any

from .mixed_contracts import canonical_digest


class EvaluationError(ValueError):
    pass


IDENTITY_FIELDS = ("assignment_sha256", "bindings_sha256", "inference_profile_sha256", "workload_condition")
METRIC_FIELDS = (
    "elapsed_seconds", "queue_seconds", "load_seconds", "accepted_tasks",
    "quality_score", "evidence_pass_rate", "repair_rate", "input_tokens",
    "cached_input_tokens", "reasoning_tokens", "output_tokens",
    "parent_context_tokens", "cancellation_uncertain", "memory_high_water_by_domain",
    "provider_cost_usd", "subscription_quota_remaining",
)


def _number(value: Any, field: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise EvaluationError("invalid_evaluation_metric:" + field)


def validate_trial(trial: dict[str, Any]) -> None:
    keys = {"schema", "trial_id", "evidence_kind", "evidence_sha256", "concurrency", "usage_complete", "metrics", *IDENTITY_FIELDS}
    if not isinstance(trial, dict) or set(trial) != keys or trial["schema"] != "EvaluationTrialV1":
        raise EvaluationError("unsupported_evaluation_trial")
    if trial["evidence_kind"] not in ("synthetic", "live"):
        raise EvaluationError("invalid_evidence_kind")
    for field in ("evidence_sha256", "assignment_sha256", "bindings_sha256", "inference_profile_sha256"):
        value = trial[field]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "abcdef0123456789" for char in value):
            raise EvaluationError("invalid_evaluation_digest:" + field)
    if not isinstance(trial["trial_id"], str) or not 1 <= len(trial["trial_id"]) <= 128:
        raise EvaluationError("invalid_trial_id")
    if type(trial["concurrency"]) is not int or trial["concurrency"] < 1 or type(trial["usage_complete"]) is not bool or trial["workload_condition"] not in ("warm", "cold"):
        raise EvaluationError("invalid_evaluation_conditions")
    metrics = trial["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != set(METRIC_FIELDS):
        raise EvaluationError("evaluation_metrics_incomplete")
    for field in set(METRIC_FIELDS) - {"cancellation_uncertain", "memory_high_water_by_domain"}:
        _number(metrics[field], field, optional=field in {"provider_cost_usd", "subscription_quota_remaining"})
    if metrics["elapsed_seconds"] <= 0:
        raise EvaluationError("elapsed_time_required")
    for field in ("accepted_tasks", "input_tokens", "cached_input_tokens", "reasoning_tokens", "output_tokens", "parent_context_tokens"):
        if type(metrics[field]) is not int:
            raise EvaluationError("integer_metric_required:" + field)
    for field in ("quality_score", "evidence_pass_rate", "repair_rate"):
        if metrics[field] > 1:
            raise EvaluationError("rate_out_of_range:" + field)
    if type(metrics["cancellation_uncertain"]) is not bool:
        raise EvaluationError("cancellation_state_required")
    memory = metrics["memory_high_water_by_domain"]
    if not isinstance(memory, dict) or not memory or len(memory) > 64:
        raise EvaluationError("memory_domains_required")
    for domain, value in memory.items():
        if not isinstance(domain, str) or not 1 <= len(domain) <= 128:
            raise EvaluationError("invalid_memory_domain")
        _number(value, "memory_high_water")


def compare_trials(serial: dict[str, Any], candidate: dict[str, Any], *, experiment: str) -> dict[str, Any]:
    validate_trial(serial)
    validate_trial(candidate)
    if experiment not in ("concurrency", "routing"):
        raise EvaluationError("unknown_experiment")
    if serial["trial_id"] == candidate["trial_id"] or serial["evidence_sha256"] == candidate["evidence_sha256"]:
        raise EvaluationError("comparison_requires_distinct_trials")
    if serial["evidence_kind"] != candidate["evidence_kind"]:
        raise EvaluationError("mixed_synthetic_live_evidence")
    matched = IDENTITY_FIELDS if experiment == "concurrency" else ("assignment_sha256", "workload_condition")
    if any(serial[field] != candidate[field] for field in matched):
        raise EvaluationError("unmatched_experiment_conditions")
    if experiment == "concurrency" and (serial["concurrency"] != 1 or candidate["concurrency"] <= 1):
        raise EvaluationError("serial_concurrent_pair_required")
    if experiment == "routing" and serial["concurrency"] != candidate["concurrency"]:
        raise EvaluationError("routing_concurrency_must_match")
    if not serial["usage_complete"] or not candidate["usage_complete"]:
        raise EvaluationError("comparison_usage_incomplete")
    if serial["metrics"]["cancellation_uncertain"] or candidate["metrics"]["cancellation_uncertain"]:
        raise EvaluationError("comparison_cancellation_uncertain")
    a, b = serial["metrics"], candidate["metrics"]
    # This is an observed accepted-task metric, not raw output speed or savings.
    throughput_a = a["accepted_tasks"] / a["elapsed_seconds"]
    throughput_b = b["accepted_tasks"] / b["elapsed_seconds"]
    cost_known = a["provider_cost_usd"] is not None and b["provider_cost_usd"] is not None
    return {
        "schema": "EvaluationComparisonV1", "experiment": experiment,
        "evidence_kind": serial["evidence_kind"],
        "live_claim_permitted": False,  # evidence references need independent review
        "serial_trial_sha256": canonical_digest(serial),
        "candidate_trial_sha256": canonical_digest(candidate),
        "accepted_tasks_per_second": {"serial": throughput_a, "candidate": throughput_b},
        "quality_score": {"serial": a["quality_score"], "candidate": b["quality_score"]},
        "parent_context_token_change": b["parent_context_tokens"] - a["parent_context_tokens"],
        "provider_cost_difference_usd": b["provider_cost_usd"] - a["provider_cost_usd"] if cost_known else None,
        "memory_high_water_by_domain": {"serial": a["memory_high_water_by_domain"].copy(), "candidate": b["memory_high_water_by_domain"].copy()},
        "performance_savings_claim": None,
    }
