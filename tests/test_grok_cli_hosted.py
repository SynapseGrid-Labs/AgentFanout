"""Real hosted lifecycle with synthetic ACP only; never live conformance."""
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentfanout.grok_cli import HostAuthorization, GrokCLIUnavailable
from agentfanout.grok_cli_hosted import GrokCLIWorkerAdapter, GrokCLIValidatorAdapter
from agentfanout.host_runtime import HostLedger
from agentfanout.lifecycle import build_task_units, initialize_run, save_run_state
from agentfanout.runner import HostedFanoutRunner
from tests.test_grok_cli import FakeTransport, evidence, SEMANTICS


class PacketTransport(FakeTransport):
    def __init__(self, index, role, partial=False, fail=None):
        super().__init__(epoch="fixture-epoch-" + str(index), session="fixture-session-" + str(index), fail=fail)
        self.role, self.partial = role, partial

    def send(self, frame):
        if frame.get("method") == "session/prompt":
            packet = json.loads(frame["params"]["prompt"][0]["text"])
            task = packet["task"]
            if self.role == "worker":
                result = {"schema": "WorkerResultV1", "task_id": task["task_id"],
                    "result_id": task["expected_result_id"], "status": "partial" if self.partial else "success",
                    "summary": "bounded ACP fixture", "evidence": ["fixture source"],
                    "changed_files": [], "commands": [], "exit_codes": [],
                    "remaining_work": [{"scope": "finish bounded residual", "input_ref": "result:remaining_work"}] if self.partial else [],
                    "launch_correlation_id": task["launch_correlation_id"]}
            else:
                result = {"schema": "ValidatorResultV1", "validator_id": self.session,
                    "verdict": "pass", "evidence_refs": ["fixture source"], "material_findings": [],
                    "contradictions": [], "checked_at": datetime.now(timezone.utc).isoformat()}
            self.output = json.dumps(result)
        super().send(frame)
        if frame.get("method") == "session/prompt" and self.fail in {"usage_partial", "price_partial"}:
            self.queue[-1]["result"]["usage"].update(
                {"usage_is_partial": True} if self.fail == "usage_partial" else
                {"provider_cost_usd": 0.001, "provider_cost_verified": True, "cost_is_partial": True})


class HostedACPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.state_file = Path(self.tmp.name) / "state.json"
        self.ledger_file = Path(self.tmp.name) / "host.sqlite3"
        self.sessions = []
        tasks = build_task_units("review bounded", "bulk-read", "explorer", "GrokCLI", "grok-fast", "low", ["request_item_1"], allowed_paths=[str(self.project)])
        self.state = initialize_run("review bounded", tasks, {"max_wave_size": 1, "max_waves": 4})
        save_run_state(self.state_file, self.state)
        self.authorizations = {}

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, kind, data, task_id):
        ledger = HostLedger(self.ledger_file)
        try:
            ledger.append(self.state["run_id"], kind, data, task_id)
        finally:
            ledger.close()

    def events(self, kind):
        ledger = HostLedger(self.ledger_file)
        try:
            return ledger.events(self.state["run_id"], kind)
        finally:
            ledger.close()

    def authorize(self, task, role, prepared, receipt):
        # Explicit host fixture authority consumes existing durable runner receipt.
        # Adapter cannot construct this authorization or read the host ledger.
        existing = self.events("launch_receipt" if role == "worker" else "validator_attempt_started")
        candidates = [event for event in existing if event["task_id"] == task["task_id"]]
        self.assertTrue(candidates)
        source = candidates[-1]["payload"]
        if role == "worker":
            self.assertEqual(source, receipt)
            correlation = source["launch_correlation_id"]
        else:
            correlation = source["validator_launch_correlation_id"]
        auth = HostAuthorization(correlation, prepared, 0.03)
        payload = {"receipt_id": correlation, "prepared": asdict(prepared),
                   "reservation_usd": auth.reservation_usd, "source_receipt": source}
        self.append("grok_cli_session_authorized", payload, task["task_id"])
        self.authorizations[correlation] = auth
        return auth

    def durable_check(self, auth):
        return any(event["payload"]["receipt_id"] == auth.receipt_id and
                   event["payload"]["prepared"] == asdict(auth.prepared)
                   for event in self.events("grok_cli_session_authorized"))

    def adapters(self, partial=True, failure=None, validator_failure=None):
        def transport_factory(role, task, ev):
            index = len(self.sessions) + 1
            worker_count = sum(session.role == "worker" for session in self.sessions)
            session = PacketTransport(index, role, partial=partial and role == "worker" and worker_count == 0,
                fail=failure if role == "worker" else validator_failure)
            self.sessions.append(session)
            return session
        def capability(task, policy, ev):
            now = datetime.now(timezone.utc)
            return {"schema": "CapabilityRecordV1", "provider": "GrokCLI", "exact_model_id": ev.model,
                "reasoning_effort": "low", "task_class": task["task_class"], "tool_policy_sha256": policy,
                "evaluation_version": "synthetic-acp-only", "evidence_class": "measured_capability",
                "quality_confidence": 1.0, "sample_size": 1, "p50_latency_ms": 1, "p95_latency_ms": 1,
                "failure_rate": 0.0, "validator_failure_rate": 0.0, "context_window": 8192,
                "price_card_version": "synthetic-reservation", "observed_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(), "status": "approved"}
        args = dict(evidence=evidence(), host_verify=lambda ev: True, transport_factory=transport_factory,
            capability_factory=capability, authorize=self.authorize, durable_check=self.durable_check,
            journal=lambda task, role, kind, data: (self.append("grok_cli_" + kind, {"role": role, **data}, task["task_id"]), True)[1],
            usage_semantics=SEMANTICS,
            bounded_task_factory=lambda task: {**task, "bounded_input": {"content": "synthetic bounded fixture", "input_ref": task["input_ref"]}})
        return GrokCLIWorkerAdapter(**args), GrokCLIValidatorAdapter(**args)

    def run_fixture(self, **kwargs):
        worker, validator = self.adapters(**kwargs)
        return HostedFanoutRunner(self.state_file, self.ledger_file, {"GrokCLI": worker}, validator,
            max_workers=1, lease_seconds=120, heartbeat_seconds=30).run()

    def test_fresh_worker_validator_and_residual_second_wave(self):
        outcome = self.run_fixture()
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(outcome["state"]["wave_index"], 2)
        self.assertEqual(sorted(session.role for session in self.sessions), ["validator", "validator", "worker", "worker"])
        self.assertEqual(len({session.epoch for session in self.sessions}), 4)
        self.assertEqual(len(self.events("launch_receipt")), 2)
        self.assertEqual(len(self.events("validator_attempt_started")), 2)
        self.assertEqual(len(self.events("grok_cli_session_authorized")), 4)
        self.assertEqual(len(self.events("grok_cli_inference_start_intent")), 4)
        bases = self.events("usage_basis")
        self.assertEqual(len(bases), 2)
        self.assertTrue(all(event["payload"]["cost_basis"] == "reservation_upper_bound" for event in bases))
        self.assertGreater(outcome["state"]["usage"]["cost_usd"], 0)

    def test_worker_crash_never_replayed_or_completed(self):
        outcome = self.run_fixture(partial=False, failure="crash")
        self.assertNotEqual(outcome["status"], "complete")
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(len(self.events("grok_cli_inference_effect_uncertain")), 1)
        self.assertFalse(self.events("accepted_result_packet"))

    def test_required_validator_crash_blocks_worker_success(self):
        outcome = self.run_fixture(partial=False, validator_failure="crash")
        self.assertNotEqual(outcome["status"], "complete")
        self.assertEqual([session.role for session in self.sessions], ["worker", "validator"])
        self.assertTrue(self.events("validator_attempt_started"))
        self.assertFalse(self.events("validator_launch_receipt"))

    def test_required_validator_auth_unavailable_cannot_complete_worker_success(self):
        outcome = self.run_fixture(partial=False, validator_failure="auth")
        self.assertNotEqual(outcome["status"], "complete")
        self.assertEqual([session.role for session in self.sessions], ["worker", "validator"])
        self.assertFalse(any(frame.get("method") == "session/prompt" for frame in self.sessions[-1].sent))
        self.assertEqual(len(self.events("grok_cli_usage_reconciled")), 1)

    def test_sensitive_host_supplied_text_is_refused_before_bootstrap(self):
        worker, validator = self.adapters(partial=False)
        worker.bounded_task_factory = lambda task: {**task, "bounded_input": {
            "content": json.dumps({"api_key": "fixture-sensitive-credential-value"}),
            "input_ref": task["input_ref"]}}
        outcome = HostedFanoutRunner(self.state_file, self.ledger_file, {"GrokCLI": worker}, validator,
            max_workers=1, lease_seconds=120, heartbeat_seconds=30).run()
        self.assertNotEqual(outcome["status"], "complete")
        self.assertFalse(self.sessions)
        self.assertFalse(self.events("grok_cli_harmless_bootstrap_start_intent"))

    def test_declared_partial_usage_cannot_authorize_terminal_success_or_baseline_commit(self):
        outcome = self.run_fixture(partial=False, failure="usage_partial")
        self.assertNotEqual(outcome["status"], "complete")
        self.assertFalse(self.events("grok_cli_usage_reconciled"))
        self.assertFalse(self.events("accepted_result_packet"))

    def test_cost_only_partial_retains_numeric_reservation_charge(self):
        worker, validator = self.adapters(partial=False, failure="price_partial")
        worker.usage_semantics = {**SEMANTICS, "price_evidence_verified": True}
        outcome = HostedFanoutRunner(self.state_file, self.ledger_file, {"GrokCLI": worker}, validator,
            max_workers=1, lease_seconds=120, heartbeat_seconds=30).run()
        self.assertEqual(outcome["status"], "complete")
        self.assertGreater(outcome["state"]["usage"]["cost_usd"], 0.001)
        bases = self.events("usage_basis")
        self.assertTrue(all(event["payload"]["cost_basis"] == "reservation_upper_bound" for event in bases))
