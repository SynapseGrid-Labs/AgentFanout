"""Deterministic engineering gate for skill-first token economics.

This fixture is a gate, not a market claim. Replace the fixture values with
captured provider measurements before publishing savings language.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Measurement:
    mode: str
    tasks: int
    parent_input_tokens: int
    worker_input_tokens: int
    validator_input_tokens: int
    parent_ingested_result_tokens: int
    provider_input_tokens: int
    provider_output_tokens: int
    wall_time_ms: int
    quality_score: float
    no_benefit_tasks: int


def run_benchmark() -> dict[str, object]:
    measurements = [
        Measurement("parent-only", 4, 6000, 0, 0, 0, 6000, 1200, 8000, 0.98, 0),
        Measurement("skill-native", 4, 1200, 7000, 0, 180, 7000, 900, 5000, 0.98, 0),
        Measurement("optional-runtime", 4, 1200, 7000, 6000, 220, 13000, 1400, 9000, 0.99, 0),
    ]
    by_mode = {item.mode: asdict(item) for item in measurements}
    native = by_mode["skill-native"]
    runtime = by_mode["optional-runtime"]
    gates = {
        "no_default_validators_low_risk": native["validator_input_tokens"] == 0,
        "native_two_worker_input_under_8000": native["provider_input_tokens"] <= 8000,
        "runtime_two_worker_one_validator_under_15000": runtime["provider_input_tokens"] <= 15000,
        "raw_transcripts_not_ingested": native["parent_ingested_result_tokens"] <= 300,
        "quality_recorded": all(0 <= item["quality_score"] <= 1 for item in by_mode.values()),
    }
    return {
        "schema": "AgentFanoutSkillFirstEconomicsV1",
        "fixture": True,
        "claim_status": "not_a_savings_claim",
        "tasks_are_identical": True,
        "measurements": by_mode,
        "gates": gates,
        "all_gates_pass": all(gates.values()),
        "no_benefit_fanout_rate": native["no_benefit_tasks"] / native["tasks"],
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))
