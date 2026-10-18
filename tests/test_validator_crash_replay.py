import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentfanout.host_runtime import HostLedger
from agentfanout.lifecycle import build_task_units, initialize_run, save_run_state
from agentfanout.runner import HostedFanoutRunner


def _capability(provider, model, task, policy):
    now = datetime.now(timezone.utc)
    return {
        "schema": "CapabilityRecordV1",
        "provider": provider,
        "exact_model_id": model,
        "reasoning_effort": task["reasoning_effort"],
        "task_class": task["task_class"],
        "tool_policy_sha256": policy,
        "evaluation_version": "validator-crash-fixture-v1",
        "quality_confidence": 1.0,
        "evidence_class": "measured_capability",
        "sample_size": 1,
        "p50_latency_ms": 1,
        "p95_latency_ms": 1,
        "failure_rate": 0.0,
        "validator_failure_rate": 0.0,
        "context_window": 8192,
        "price_card_version": "fixture",
        "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "status": "approved",
    }


class OneShotWorker:
    provider = "Codex"
    supports_context_reuse = False

    def __init__(self):
        self.calls = 0

    def preflight(self, assignment):
        return dict(assignment)

    def prepare(self, assignment, policy):
        task = assignment["host_task"]
        return {
            "resolved_model": "codex-worker-fixture",
            "capability": _capability(
                self.provider, "codex-worker-fixture", task, policy
            ),
        }

    def run(self, assignment, receipt, heartbeat):
        self.calls += 1
        heartbeat(datetime.now(timezone.utc).isoformat())
        assignment["_confirm_process_group_exit"]()
        return {
            "resolved_model": receipt["resolved_model"],
            "process_group_exit_confirmed": True,
            "usage": {
                "usage_verified": True,
                "cost_verified": True,
                "input_tokens": 8,
                "output_tokens": 4,
                "cost_usd": 0.001,
            },
            "result": {
                "schema": "WorkerResultV1",
                "result_id": "fixture-worker-result",
                "task_id": assignment["task_id"],
                "status": "success",
                "summary": "bounded result",
                "evidence": ["fixture-evidence"],
                "changed_files": [],
                "commands": [],
                "exit_codes": [],
                "remaining_work": [],
                "launch_correlation_id": receipt["launch_correlation_id"],
            },
        }


class ProcessCrashValidator:
    provider = "Codex"

    def __init__(self):
        self.calls = 0

    def prepare(self, task, _result, policy):
        return {
            "resolved_model": "codex-validator-fixture",
            "capability": _capability(
                self.provider, "codex-validator-fixture", task, policy
            ),
        }

    def run(self, _task, _result):
        self.calls += 1
        # BaseException deliberately escapes the runner's ordinary adapter
        # failure path, modeling abrupt host/process loss after call start.
        raise SystemExit("fixture-validator-process-crash")


class FullBudgetWorker(OneShotWorker):
    def run(self, assignment, receipt, heartbeat):
        output = super().run(assignment, receipt, heartbeat)
        output["usage"]["cost_usd"] = assignment["host_task"]["budgets"][
            "max_cost_usd"
        ]
        return output


class MustNotRunValidator(ProcessCrashValidator):
    def run(self, _task, _result):
        self.calls += 1
        raise AssertionError("validator must not run without accounting capacity")


class PassingValidator(ProcessCrashValidator):
    def run(self, task, _result):
        self.calls += 1
        task["_confirm_process_group_exit"]()
        return {
            "resolved_model": "codex-validator-fixture",
            "process_group_exit_confirmed": True,
            "usage": {
                "usage_verified": True,
                "cost_verified": False,
                "input_tokens": 6,
                "output_tokens": 2,
                "cost_usd": None,
            },
            "result": {
                "schema": "ValidatorResultV1",
                "validator_id": "fixture-validator",
                "verdict": "pass",
                "evidence_refs": ["fixture-validation"],
                "material_findings": [],
                "contradictions": [],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        }


class ValidatorCrashReplayTests(unittest.TestCase):
    def test_unresolved_validator_start_blocks_restart_without_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            state_file = root / "run.json"
            ledger_file = root / "host.sqlite3"
            tasks = build_task_units(
                "review one bounded item",
                "bulk-read",
                "explorer",
                "Codex",
                "current-fast-mini",
                "low",
                ["request_item_1"],
                allowed_paths=[str(project)],
            )
            state = initialize_run("review one bounded item", tasks)
            save_run_state(state_file, state)
            worker = OneShotWorker()
            validator = ProcessCrashValidator()

            first = HostedFanoutRunner(
                state_file,
                ledger_file,
                {"Codex": worker},
                validator,
                max_workers=1,
            )
            with self.assertRaisesRegex(SystemExit, "fixture-validator-process-crash"):
                first.run()

            ledger = HostLedger(ledger_file)
            try:
                events_after_crash = ledger.events(state["run_id"])
                starts = [
                    event for event in events_after_crash
                    if event["event_type"] == "validator_attempt_started"
                ]
                reservations = [
                    event for event in events_after_crash
                    if event["event_type"] == "validator_accounting_reserved"
                ]
            finally:
                ledger.close()
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(reservations), 1)
            self.assertNotIn("usage", starts[0]["payload"])
            self.assertFalse(reservations[0]["payload"]["provider_side_spend_cap"])
            self.assertLess(
                events_after_crash.index(reservations[0]),
                events_after_crash.index(starts[0]),
            )
            self.assertFalse(
                any(
                    event["event_type"] in {
                        "validator_attempt_completed",
                        "validator_launch_receipt",
                        "validator_usage_basis",
                    }
                    for event in events_after_crash
                )
            )

            restarted = HostedFanoutRunner(
                state_file,
                ledger_file,
                {"Codex": worker},
                validator,
                max_workers=1,
            ).run()
            self.assertEqual(restarted["status"], "blocked")
            self.assertEqual(worker.calls, 1)
            self.assertEqual(validator.calls, 1)
            task = restarted["state"]["tasks"][0]
            self.assertEqual(task["status"], "blocked")
            self.assertEqual(task["last_error"], "validator_unknown_after_crash")

            ledger = HostLedger(ledger_file)
            try:
                unknown = ledger.events(
                    state["run_id"], "validator_attempt_unknown_after_crash"
                )
                starts_after_restart = ledger.events(
                    state["run_id"], "validator_attempt_started"
                )
            finally:
                ledger.close()
            self.assertEqual(len(unknown), 1)
            self.assertFalse(unknown[0]["payload"]["provider_replayed"])
            self.assertFalse(unknown[0]["payload"]["replay_authorized"])
            self.assertEqual(len(starts_after_restart), 1)

    def test_insufficient_validator_accounting_capacity_blocks_before_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            state_file = root / "run.json"
            ledger_file = root / "host.sqlite3"
            tasks = build_task_units(
                "review one bounded item",
                "bulk-read",
                "explorer",
                "Codex",
                "current-fast-mini",
                "low",
                ["request_item_1"],
                allowed_paths=[str(project)],
            )
            state = initialize_run("review one bounded item", tasks)
            save_run_state(state_file, state)
            worker = FullBudgetWorker()
            validator = MustNotRunValidator()

            outcome = HostedFanoutRunner(
                state_file,
                ledger_file,
                {"Codex": worker},
                validator,
                max_workers=1,
            ).run()

            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(worker.calls, 1)
            self.assertEqual(validator.calls, 0)
            task = outcome["state"]["tasks"][0]
            self.assertIn("validator_accounting_reservation_insufficient", task["last_error"])
            ledger = HostLedger(ledger_file)
            try:
                starts = ledger.events(
                    state["run_id"], "validator_attempt_started"
                )
                reservations = ledger.events(
                    state["run_id"], "validator_accounting_reserved"
                )
            finally:
                ledger.close()
            self.assertEqual(starts, [])
            self.assertEqual(reservations, [])

    def test_success_persists_start_usage_and_completion_as_distinct_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            state_file = root / "run.json"
            ledger_file = root / "host.sqlite3"
            tasks = build_task_units(
                "review one bounded item",
                "bulk-read",
                "explorer",
                "Codex",
                "current-fast-mini",
                "low",
                ["request_item_1"],
                allowed_paths=[str(project)],
            )
            state = initialize_run("review one bounded item", tasks)
            save_run_state(state_file, state)
            worker = OneShotWorker()
            validator = PassingValidator()

            outcome = HostedFanoutRunner(
                state_file,
                ledger_file,
                {"Codex": worker},
                validator,
                max_workers=1,
            ).run()
            self.assertEqual(outcome["status"], "complete")
            self.assertEqual(validator.calls, 1)

            ledger = HostLedger(ledger_file)
            try:
                events = ledger.events(state["run_id"])
            finally:
                ledger.close()
            positions = {
                event_type: next(
                    index
                    for index, event in enumerate(events)
                    if event["event_type"] == event_type
                )
                for event_type in (
                    "validator_accounting_reserved",
                    "validator_attempt_started",
                    "validator_attempt_returned",
                    "validator_launch_receipt",
                    "validator_usage_basis",
                    "validator_accounting_reconciled",
                    "validator_result",
                    "validator_attempt_completed",
                )
            }
            self.assertEqual(list(positions.values()), sorted(positions.values()))
            start = next(
                event["payload"]
                for event in events
                if event["event_type"] == "validator_attempt_started"
            )
            self.assertNotIn("usage", start)


if __name__ == "__main__":
    unittest.main()
