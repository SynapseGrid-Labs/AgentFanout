import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentfanout.host_runtime import HostDispatcher, HostLedger, validator_policy_sha256
from agentfanout.lifecycle import (
    build_task_units,
    initialize_run,
    load_run_state,
    plan_next_wave,
    record_provider_usage,
    save_run_state,
    validate_run_state,
)
from agentfanout.runner import HostedRunnerError, _find_packet, _validated_duration_ms
from agentfanout.telemetry import sanitize_event


def make_state(project: Path, budget=None):
    tasks = build_task_units(
        "review bounded item", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
        ["request_item_1"], allowed_paths=[str(project)],
    )
    return initialize_run("review bounded item", tasks, budget)


def launch_receipt(state, assignment):
    now = datetime.now(timezone.utc)
    planned = state["launch_records"][assignment["launch_correlation_id"]]["provider_model"]
    return {
        "schema": "LaunchReceiptV2", "run_id": state["run_id"], "task_id": assignment["task_id"],
        "launch_correlation_id": assignment["launch_correlation_id"], "provider": planned["provider"],
        "provider_profile": planned["profile"], "model_selector": planned["selector"],
        "resolved_model": "fixture-model", "reasoning_effort": "low",
        "worker_handle": assignment["worker_handle"],
        "termination_token": "termination-token-fixture-0123456789",
        "started_at": now.isoformat(),
    }


class NumericSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nonfinite_budget_and_task_cost_values_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(scope="run_budget", value=repr(value)):
                with self.assertRaisesRegex(ValueError, "invalid_run_budget:max_total_cost_usd"):
                    make_state(self.project, {"max_total_cost_usd": value})
            with self.subTest(scope="task_budget", value=repr(value)):
                tasks = build_task_units(
                    "review bounded item", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
                    ["request_item_1"], allowed_paths=[str(self.project)],
                )
                tasks[0]["budgets"]["max_cost_usd"] = value
                with self.assertRaisesRegex(ValueError, "invalid_task_budget"):
                    initialize_run("review bounded item", tasks)

    def test_nonfinite_decoded_and_internal_run_state_is_rejected(self):
        baseline = make_state(self.project)
        for field_path in (("budget", "max_total_cost_usd"), ("usage", "cost_usd"), ("usage", "reserved_cost_usd")):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field_path=field_path, value=repr(value)):
                    state = deepcopy(baseline)
                    state[field_path[0]][field_path[1]] = value
                    with self.assertRaisesRegex(ValueError, "invalid_run_(?:budget|usage)"):
                        validate_run_state(state)

        planned = deepcopy(baseline)
        assignment = plan_next_wave(planned)["assignments"][0]
        correlation = assignment["launch_correlation_id"]
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(field_path="launch_record_budget", value=repr(value)):
                state = deepcopy(planned)
                state["launch_records"][correlation]["budgets"]["max_cost_usd"] = value
                with self.assertRaisesRegex(ValueError, "invalid_launch_record_budget"):
                    validate_run_state(state)
            with self.subTest(field_path="launch_record_usage", value=repr(value)):
                state = deepcopy(planned)
                state["launch_records"][correlation]["provider_usage"] = {
                    "input_tokens": 1, "output_tokens": 1, "cost_usd": value,
                }
                with self.assertRaisesRegex(ValueError, "invalid_launch_record_provider_usage"):
                    validate_run_state(state)
            with self.subTest(field_path="provider_metric", value=repr(value)):
                state = deepcopy(baseline)
                state["tasks"][0]["provider_model"]["relative_cost"] = value
                with self.assertRaisesRegex(ValueError, "invalid_task_provider_metric"):
                    validate_run_state(state)

        state_path = self.root / "strict-state.json"
        save_run_state(state_path, baseline)
        valid_text = state_path.read_text(encoding="utf-8")
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(persisted_literal=literal):
                state_path.write_text(
                    valid_text.replace('"max_total_cost_usd": 10.0', f'"max_total_cost_usd": {literal}'),
                    encoding="utf-8",
                )
                state_path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "run_state_numeric_non_finite"):
                    load_run_state(state_path)

    def test_nonfinite_provider_usage_and_capability_metrics_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(lane="worker_usage", value=repr(value)):
                state = make_state(self.project)
                assignment = plan_next_wave(state)["assignments"][0]
                with self.assertRaisesRegex(ValueError, "provider_usage_value_invalid"):
                    record_provider_usage(state, assignment["launch_correlation_id"], {
                        "input_tokens": 1, "output_tokens": 1, "cost_usd": value,
                    })

            with self.subTest(lane="capability", value=repr(value)):
                ledger = HostLedger(self.root / f"capability-{repr(value).replace('(', '').replace(')', '')}.sqlite3")
                try:
                    dispatcher = HostDispatcher(ledger, require_capability=False)
                    state = make_state(self.project)
                    now = datetime.now(timezone.utc)
                    capability = {
                        "schema": "CapabilityRecordV1", "provider": "Codex", "exact_model_id": "fixture",
                        "reasoning_effort": "low", "task_class": "bulk-read", "tool_policy_sha256": "c" * 64,
                        "evaluation_version": "test", "quality_confidence": value, "sample_size": 1,
                        "evidence_class": "measured_capability", "p50_latency_ms": 1, "p95_latency_ms": 2,
                        "failure_rate": 0.0, "validator_failure_rate": 0.0, "context_window": 8192,
                        "price_card_version": "test", "observed_at": now.isoformat(),
                        "expires_at": (now + timedelta(minutes=5)).isoformat(), "status": "approved",
                    }
                    with self.assertRaisesRegex(ValueError, "capability_record_metrics_invalid"):
                        dispatcher.record_capability(state, capability)
                    self.assertEqual(ledger.events(state["run_id"], "capability_record"), [])
                finally:
                    ledger.close()

    def test_nonfinite_validator_usage_is_rejected_before_receipt_persistence(self):
        for index, value in enumerate((float("nan"), float("inf"), float("-inf"))):
            with self.subTest(value=repr(value)):
                ledger = HostLedger(self.root / f"validator-{index}.sqlite3")
                try:
                    state = make_state(self.project)
                    dispatcher = HostDispatcher(ledger, require_capability=False)
                    assignment = plan_next_wave(state)["assignments"][0]
                    dispatcher.record_launch(state, launch_receipt(state, assignment))
                    correlation = assignment["launch_correlation_id"]
                    result = {
                        "schema": "WorkerResultV1", "result_id": f"result-{index}",
                        "task_id": assignment["task_id"], "status": "success", "summary": "safe result",
                        "evidence": ["fixture"], "changed_files": [], "commands": [], "exit_codes": [],
                        "remaining_work": [], "launch_correlation_id": correlation,
                    }
                    dispatcher.ingest_worker_batch(state, [result], {
                        correlation: {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.001},
                    })
                    now = datetime.now(timezone.utc)
                    receipt = {
                        "schema": "ValidatorLaunchReceiptV1", "run_id": state["run_id"],
                        "task_id": assignment["task_id"],
                        "validator_launch_correlation_id": f"validator-launch-{index}-0123456789",
                        "validated_result_id": result["result_id"],
                        "validated_result_sha256": ledger.events(state["run_id"], "worker_result_accepted")[-1]["payload"]["result_sha256"],
                        "validator_policy_sha256": validator_policy_sha256(
                            assignment["task_id"],
                            ledger.events(state["run_id"], "worker_result_accepted")[-1]["payload"]["result_sha256"],
                        ), "validator_provider": "Codex",
                        "validator_resolved_model": "fixture-validator", "validator_reasoning_effort": "low",
                        "validator_capability_digest": "e" * 64, "started_at": now.isoformat(),
                        "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
                        "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": value,
                                  "usage_verified": True, "cost_verified": False,
                                  "cost_basis": "reservation_upper_bound", "measured_savings_claimed": False},
                    }
                    # The cost guard executes before policy/capability binding.
                    with self.assertRaisesRegex(ValueError, "validator_usage_invalid"):
                        dispatcher.record_validator_launch(state, receipt)
                    self.assertEqual(ledger.events(state["run_id"], "validator_launch_receipt"), [])
                finally:
                    ledger.close()

    def test_nonfinite_ledger_telemetry_duration_and_provider_json_are_rejected(self):
        ledger = HostLedger(self.root / "strict-ledger.sqlite3")
        try:
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(lane="ledger", value=repr(value)):
                    with self.assertRaises((TypeError, ValueError)):
                        ledger.append("run-numeric", "usage_basis", {"cost_usd": value})
            self.assertEqual(ledger.events("run-numeric"), [])
        finally:
            ledger.close()

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(lane="telemetry", value=repr(value)):
                with self.assertRaisesRegex(ValueError, "telemetry_numeric_non_finite"):
                    sanitize_event("result", {"cost_usd": value})
            with self.subTest(lane="duration", value=repr(value)):
                with self.assertRaisesRegex(HostedRunnerError, "duration_invalid"):
                    _validated_duration_ms(value, "duration_invalid")
            with self.subTest(lane="provider_json", value=repr(value)):
                literal = "NaN" if value != value else ("Infinity" if value > 0 else "-Infinity")
                with self.assertRaisesRegex(HostedRunnerError, "provider_packet_numeric_non_finite"):
                    _find_packet('{"schema":"WorkerResultV1","cost_usd":' + literal + '}', "WorkerResultV1")


if __name__ == "__main__":
    unittest.main()
