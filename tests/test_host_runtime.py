import base64
import hashlib
import json
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentfanout.host_runtime import (
    HostDispatcher,
    HostLedger,
    _validator_policy_sha256,
    docker_policy_receipt,
    build_docker_tool_command,
    execute_docker_tool,
    verify_local_launch_attestation,
)
from agentfanout.lifecycle import build_task_units, initialize_run, load_run_state, plan_next_wave, save_run_state
from agentfanout.project_binding import resolve_project_binding


ROOT = Path(__file__).resolve().parents[1]


class RecordingTelemetrySink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class FailingTelemetrySink:
    def emit(self, _event):
        raise RuntimeError("fixture_telemetry_delivery_failure")


def launch_receipt(state, assignment, *, provider="Codex", model="test-model", now=None, capability_digest=None,
                   tool_policy_sha256="c" * 64, heartbeat_interval_seconds=60):
    observed = now or datetime.now(timezone.utc)
    planned = state["launch_records"][assignment["launch_correlation_id"]]["provider_model"]
    receipt = {
        "schema": "LaunchReceiptV2", "run_id": state["run_id"], "task_id": assignment["task_id"],
        "launch_correlation_id": assignment["launch_correlation_id"], "provider": provider,
        "provider_profile": planned["profile"], "model_selector": planned["selector"],
        "resolved_model": model, "reasoning_effort": "low", "worker_handle": assignment["worker_handle"],
        "termination_token": "termination-token-fixture-0123456789",
        "started_at": observed.isoformat().replace("+00:00", "Z"),
    }
    if capability_digest is not None:
        receipt.update({
            "capability_digest": capability_digest,
            "tool_policy_sha256": tool_policy_sha256,
            "lease_expires_at": (observed + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
            "admission_basis": "measured_capability",
        })
    return receipt


def validator_result(state, task_id, result_id, capability_digest="d" * 64, verdict="pass"):
    return {
        "schema": "ValidatorResultV1", "run_id": state["run_id"], "validator_id": "validator_1",
        "task_id": task_id, "validated_result_id": result_id, "validator_provider": "Codex",
        "validator_resolved_model": "test-model", "validator_reasoning_effort": "low",
        "validator_capability_digest": capability_digest, "verdict": verdict,
        "evidence_refs": ["fixture-validation"], "material_findings": [], "contradictions": [],
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def complete_run(dispatcher, state, result_id="result_1"):
    wave = plan_next_wave(state)
    assignment = wave["assignments"][0]
    task_id = assignment["task_id"]
    correlation = assignment["launch_correlation_id"]
    dispatcher.record_launch(state, launch_receipt(state, assignment))
    dispatcher.ingest_worker_batch(state, [{
        "schema": "WorkerResultV1", "result_id": result_id, "task_id": task_id,
        "status": "success", "summary": "bounded result", "evidence": ["fixture"],
        "changed_files": [], "commands": [], "exit_codes": [], "remaining_work": [],
        "launch_correlation_id": correlation,
    }], {correlation: {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}})
    plan_next_wave(state)
    return task_id


class HostRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.ledger = HostLedger(Path(self.tmp.name) / "run.sqlite3")
        tasks = build_task_units(
            "review one file", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
            ["request_item_1"], allowed_paths=[str(self.project)],
        )
        self.state = initialize_run("review one file", tasks)
        self.state["project_binding"] = resolve_project_binding(explicit_root=self.project, env_root="")
        # Diagnostic compatibility path: most unit tests isolate host contracts
        # unrelated to capability admission. Production/default behavior stays on.
        self.dispatcher = HostDispatcher(self.ledger, require_capability=False)

    def capability(self, provider="Codex", model="test-model"):
        now = datetime.now(timezone.utc)
        return {"schema": "CapabilityRecordV1", "provider": provider, "exact_model_id": model,
                "reasoning_effort": "low", "task_class": "bulk-read", "tool_policy_sha256": "c" * 64,
                "evaluation_version": "fixture-v1", "quality_confidence": 0.9, "sample_size": 10,
                "evidence_class": "measured_capability",
                "p50_latency_ms": 10, "p95_latency_ms": 20, "failure_rate": 0.0, "validator_failure_rate": 0.0,
                "context_window": 8192, "price_card_version": "fixture", "observed_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(), "status": "approved"}

    def docker_policy_fixture(self):
        source = self.project / "input.txt"
        source.write_text("bounded input")
        daemon_identity = {
            "engine": "docker", "endpoint": "unix:///var/run/docker.sock",
            "server_id": "daemon-fixture-0123456789", "server_version": "99.0.0", "rootless": False,
        }
        policy = {
            "image": "example.invalid/tool@sha256:" + "d" * 64,
            "command": ["/bin/true"],
            "project_root": str(self.project),
            "read_paths": [str(source)],
            "write_files": [],
            "memory": "512m", "cpus": "1", "pids_limit": 128,
            "user": "65534:65534",
            "daemon_identity": daemon_identity,
        }
        observed = {
            "container_id": "container-0123456789",
            "observation_source": "host_docker_inspect",
            "image_digest": policy["image"],
            "read_only": True,
            "network_mode": "none",
            "mounts": [{"source": str(source), "destination": str(source), "read_only": True}],
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "privileged": False,
            "devices": [],
            "limits": {"memory": "512m", "cpus": "1", "pids_limit": 128},
            "user": "65534:65534",
            "tmpfs": {"destination": "/tmp", "options": ["nodev", "noexec", "nosuid", "rw", "size=64m"]},
            "daemon_identity": daemon_identity,
        }
        return policy, observed

    @staticmethod
    def mark_sandbox_receipt_host_enforced(sandbox, private_key):
        result = json.loads(json.dumps(sandbox))
        result["signed"]["boundary_status"] = "host_enforced"
        result["signature"] = base64.b64encode(private_key.sign(
            json.dumps(result["signed"], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        )).decode()
        return result

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_ledger_rejects_symlink_target(self):
        target = Path(self.tmp.name) / "target.sqlite3"
        target.write_bytes(b"")
        target.chmod(0o600)
        link = Path(self.tmp.name) / "linked.sqlite3"
        link.symlink_to(target)
        with self.assertRaisesRegex(Exception, "private_file_symlink_or_unavailable"):
            HostLedger(link)
        self.assertEqual(target.stat().st_size, 0)

    def test_measured_usage_and_validator_are_terminal_requirements(self):
        wave = plan_next_wave(self.state)
        assignment = wave["assignments"][0]
        correlation = assignment["launch_correlation_id"]
        result = {
            "schema": "WorkerResultV1", "result_id": "result_a", "task_id": assignment["task_id"],
            "status": "success", "summary": "done", "evidence": ["fixture"], "changed_files": [],
            "commands": [], "exit_codes": [], "remaining_work": [], "launch_correlation_id": correlation,
        }
        with self.assertRaisesRegex(ValueError, "verified_launch_receipt_required"):
            self.dispatcher.ingest_worker_batch(self.state, [result], {})
        self.dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        self.dispatcher.ingest_worker_batch(self.state, [result], {
            correlation: {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}
        })
        waiting = plan_next_wave(self.state)
        self.assertEqual(waiting["reason"], "validator_results_required")
        with self.assertRaisesRegex(PermissionError, "controller_not_complete"):
            self.dispatcher.authorize_terminal(self.state)
        self.dispatcher.record_validator_result(self.state, validator_result(self.state, assignment["task_id"], "result_a"))
        self.assertEqual(plan_next_wave(self.state)["action"], "complete")
        self.dispatcher.authorize_terminal(self.state)

    def test_parent_action_denied_with_residual_work(self):
        with self.assertRaisesRegex(PermissionError, "delegable_work_remains"):
            self.dispatcher.authorize_parent_action(self.state, "worker_research")

    def test_capability_enforcement_defaults_on(self):
        dispatcher = HostDispatcher(self.ledger)
        self.assertTrue(dispatcher.require_capability)

    def test_capability_records_reject_untyped_metrics_and_quality_overclaim(self):
        dispatcher = HostDispatcher(self.ledger, require_capability=True)
        base = self.capability()
        for mutation, reason in (
            ({"evidence_class": None}, "required_fields_invalid"),
            ({"quality_confidence": "high"}, "metrics_invalid"),
            ({"p50_latency_ms": 20, "p95_latency_ms": 10}, "metrics_invalid"),
            ({"sample_size": -1}, "metrics_invalid"),
            ({"failure_rate": 2.0}, "metrics_invalid"),
            ({"evidence_class": "launch_readiness", "sample_size": 1}, "launch_readiness_must_not_claim_quality"),
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, reason):
                    dispatcher.record_capability(self.state, {**base, **mutation})
        assignment = plan_next_wave(self.state)["assignments"][0]
        with self.assertRaisesRegex(ValueError, "launch_lease_required"):
            dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))

    def test_enforced_launch_requires_frozen_provider_and_fresh_capability(self):
        dispatcher = HostDispatcher(
            self.ledger, require_capability=True,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        receipt = launch_receipt(self.state, assignment, capability_digest="0" * 64)
        with self.assertRaisesRegex(ValueError, "not_approved"):
            dispatcher.record_launch(self.state, receipt)
        dispatcher.freeze_providers(self.state, ["Codex"])
        with self.assertRaisesRegex(ValueError, "capability"):
            dispatcher.record_launch(self.state, receipt)
        dispatcher.record_capability(self.state, self.capability())
        receipt["capability_digest"] = self.ledger.events(self.state["run_id"], "capability_record")[-1]["payload_sha256"]
        dispatcher.record_launch(self.state, receipt)

    def test_receipt_is_bound_to_planned_provider_profile_and_selector(self):
        assignment = plan_next_wave(self.state)["assignments"][0]
        receipt = launch_receipt(self.state, assignment, provider="Grok")
        with self.assertRaisesRegex(ValueError, "task_provider_binding"):
            self.dispatcher.record_launch(self.state, receipt)
        receipt = launch_receipt(self.state, assignment)
        receipt["model_selector"] = "different-selector"
        with self.assertRaisesRegex(ValueError, "task_provider_binding"):
            self.dispatcher.record_launch(self.state, receipt)

    def test_capability_observation_and_lease_heartbeat_are_fail_closed(self):
        dispatcher = HostDispatcher(
            self.ledger, require_capability=True,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.freeze_providers(self.state, ["Codex"])
        stale = self.capability()
        stale["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        stale["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with self.assertRaisesRegex(ValueError, "lifetime_invalid"):
            dispatcher.record_capability(self.state, stale)
        dispatcher.record_capability(self.state, self.capability())
        digest = self.ledger.events(self.state["run_id"], "capability_record")[-1]["payload_sha256"]
        now = datetime.now(timezone.utc)
        receipt = launch_receipt(self.state, assignment, now=now, capability_digest=digest, heartbeat_interval_seconds=5)
        dispatcher.record_launch(self.state, receipt)
        first_beat = (now + timedelta(seconds=1)).isoformat()
        dispatcher.record_heartbeat(self.state, assignment["task_id"], assignment["launch_correlation_id"], first_beat)
        with self.assertRaisesRegex(ValueError, "not_monotonic"):
            dispatcher.record_heartbeat(self.state, assignment["task_id"], assignment["launch_correlation_id"], first_beat)
        with self.assertRaisesRegex(ValueError, "timestamp_future"):
            dispatcher.record_heartbeat(
                self.state, assignment["task_id"], assignment["launch_correlation_id"],
                (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            )
        dispatcher._enforce_launch_liveness(self.state, now=now + timedelta(seconds=7))
        self.assertEqual(self.state["tasks"][0]["status"], "cancelled")

    def test_validator_provenance_and_concerns_reopen_a_repair_wave(self):
        task_id = complete_run(self.dispatcher, self.state, result_id="repair-result")
        invalid = validator_result(self.state, task_id, "not-accepted")
        with self.assertRaisesRegex(ValueError, "not_accepted"):
            self.dispatcher.record_validator_result(self.state, invalid)
        concerns = validator_result(self.state, task_id, "repair-result", verdict="concerns")
        self.dispatcher.record_validator_result(self.state, concerns)
        self.assertEqual(self.state["tasks"][0]["status"], "pending")
        self.assertEqual(self.state["validation_status"][task_id], "repair_required")
        self.assertEqual(plan_next_wave(self.state)["action"], "dispatch_wave")

    def test_enforced_validator_launch_binds_result_policy_usage_and_replay(self):
        dispatcher = HostDispatcher(self.ledger, require_capability=True)
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.freeze_providers(self.state, ["Codex"])
        dispatcher.record_capability(self.state, self.capability())
        worker_digest = self.ledger.events(self.state["run_id"], "capability_record")[-1]["payload_sha256"]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment, capability_digest=worker_digest))
        correlation = assignment["launch_correlation_id"]
        worker_result = {
            "schema": "WorkerResultV1", "result_id": "validator-bound-result", "task_id": assignment["task_id"],
            "status": "success", "summary": "done", "evidence": ["fixture"], "changed_files": [],
            "commands": [], "exit_codes": [], "remaining_work": [], "launch_correlation_id": correlation,
        }
        dispatcher.ingest_worker_batch(self.state, [worker_result], {
            correlation: {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}
        })
        accepted = self.ledger.events(self.state["run_id"], "worker_result_accepted")[-1]["payload"]
        policy_sha = _validator_policy_sha256(assignment["task_id"], accepted["result_sha256"])
        validator_capability = self.capability("Codex", "validator-model")
        validator_capability["tool_policy_sha256"] = policy_sha
        dispatcher.record_capability(self.state, validator_capability)
        validator_digest = self.ledger.events(self.state["run_id"], "capability_record")[-1]["payload_sha256"]
        now = datetime.now(timezone.utc)
        launch = {
            "schema": "ValidatorLaunchReceiptV1", "run_id": self.state["run_id"], "task_id": assignment["task_id"],
            "validator_launch_correlation_id": "validator-launch-0123456789", "validated_result_id": worker_result["result_id"],
            "validated_result_sha256": accepted["result_sha256"], "validator_policy_sha256": policy_sha,
            "validator_provider": "Codex", "validator_resolved_model": "validator-model",
            "validator_reasoning_effort": "low", "validator_capability_digest": validator_digest,
            "started_at": now.isoformat(), "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            "usage": {"input_tokens": 4, "output_tokens": 2, "cost_usd": 0.01, "usage_verified": True,
                      "cost_verified": False, "cost_basis": "reservation_upper_bound", "measured_savings_claimed": False},
        }
        bad_usage = json.loads(json.dumps(launch))
        bad_usage["usage"]["usage_verified"] = False
        with self.assertRaisesRegex(ValueError, "validator_usage_unverified"):
            dispatcher.record_validator_launch(self.state, bad_usage)
        prompt_over_cap = json.loads(json.dumps(launch))
        prompt_over_cap["usage"]["input_tokens"] = self.state["tasks"][0]["budgets"]["max_prompt_tokens"] + 1
        with self.assertRaisesRegex(ValueError, "validator_prompt_token_budget_exceeded"):
            dispatcher.record_validator_launch(self.state, prompt_over_cap)
        output_over_cap = json.loads(json.dumps(launch))
        output_over_cap["usage"]["output_tokens"] = self.state["tasks"][0]["budgets"]["max_output_tokens"] + 1
        with self.assertRaisesRegex(ValueError, "validator_output_token_budget_exceeded"):
            dispatcher.record_validator_launch(self.state, output_over_cap)
        zero_upper = json.loads(json.dumps(launch))
        zero_upper["usage"]["cost_usd"] = 0
        with self.assertRaisesRegex(ValueError, "validator_usage_reservation_required"):
            dispatcher.record_validator_launch(self.state, zero_upper)
        dispatcher.record_validator_launch(self.state, launch)
        result = validator_result(self.state, assignment["task_id"], worker_result["result_id"], validator_digest)
        result.update({"validator_resolved_model": "validator-model", "validator_launch_correlation_id": launch["validator_launch_correlation_id"],
                       "validated_result_sha256": accepted["result_sha256"], "validator_policy_sha256": policy_sha})
        dispatcher.record_validator_result(self.state, result)
        with self.assertRaisesRegex(ValueError, "replay"):
            dispatcher.record_validator_result(self.state, result)

    def test_validator_launch_and_checked_at_fail_closed(self):
        dispatcher = HostDispatcher(self.ledger, require_capability=True)
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.freeze_providers(self.state, ["Codex"])
        dispatcher.record_capability(self.state, self.capability())
        digest = self.ledger.events(self.state["run_id"], "capability_record")[-1]["payload_sha256"]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment, capability_digest=digest))
        correlation = assignment["launch_correlation_id"]
        worker_result = {"schema": "WorkerResultV1", "result_id": "validator-stale", "task_id": assignment["task_id"],
                         "status": "success", "summary": "done", "evidence": ["fixture"], "changed_files": [],
                         "commands": [], "exit_codes": [], "remaining_work": [], "launch_correlation_id": correlation}
        dispatcher.ingest_worker_batch(self.state, [worker_result], {correlation: {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.001}})
        accepted = self.ledger.events(self.state["run_id"], "worker_result_accepted")[-1]["payload"]
        policy_sha = _validator_policy_sha256(assignment["task_id"], accepted["result_sha256"])
        validator_capability = self.capability("Codex", "validator-stale-model")
        validator_capability["tool_policy_sha256"] = policy_sha
        dispatcher.record_capability(self.state, validator_capability)
        validator_digest = self.ledger.events(self.state["run_id"], "capability_record")[-1]["payload_sha256"]
        result = validator_result(self.state, assignment["task_id"], worker_result["result_id"], validator_digest)
        result.update({"validator_resolved_model": "validator-stale-model", "validator_launch_correlation_id": "validator-launch-stale-01",
                       "validated_result_sha256": accepted["result_sha256"], "validator_policy_sha256": policy_sha,
                       "checked_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
        with self.assertRaisesRegex(ValueError, "checked_at_stale"):
            dispatcher.record_validator_result(self.state, result)

    def test_termination_callback_receives_host_token_and_must_confirm(self):
        calls = []
        # Diagnostic compatibility path for termination semantics without a
        # separate capability fixture.
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda request: calls.append(request) or {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        receipt = launch_receipt(self.state, assignment)
        tokenless = dict(receipt)
        tokenless.pop("termination_token")
        with self.assertRaisesRegex(ValueError, "required_fields"):
            dispatcher.record_launch(self.state, tokenless)
        receipt["termination_token"] = "termination-token-0123456789"
        dispatcher.record_launch(self.state, receipt)
        dispatcher.cancel_launch(self.state, assignment["task_id"], "test cancellation")
        self.assertEqual(calls[0]["termination_token"], receipt["termination_token"])
        self.assertEqual(self.state["tasks"][0]["status"], "cancelled")
        retry_tasks = build_task_units(
            "review retry", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
            ["request_item_1"], allowed_paths=[str(self.project)],
        )
        retry_state = initialize_run("review retry", retry_tasks)
        retry_assignment = plan_next_wave(retry_state)["assignments"][0]
        rejecting = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": False},
        )
        retry_receipt = launch_receipt(retry_state, retry_assignment)
        retry_receipt["termination_token"] = "termination-token-reject-0123456789"
        rejecting.record_launch(retry_state, retry_receipt)
        with self.assertRaisesRegex(ValueError, "termination_not_confirmed"):
            rejecting.cancel_launch(retry_state, retry_assignment["task_id"], "must not mark cancelled")
        self.assertEqual(retry_state["tasks"][0]["status"], "dispatched")

    def test_local_launch_requires_signed_sandbox_receipt_bound_to_policy(self):
        local_tasks = build_task_units(
            "review local", "bulk-read", "explorer", "Local", "local-worker", "low",
            ["request_item_1"], allowed_paths=[str(self.project)],
        )
        local_state = initialize_run("review local", local_tasks)
        assignment = plan_next_wave(local_state)["assignments"][0]
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        dispatcher = HostDispatcher(
            self.ledger, local_public_key_b64=base64.b64encode(public).decode(),
            local_sandbox_public_key_b64=base64.b64encode(public).decode(), require_capability=True,
        )
        policy, observed = self.docker_policy_fixture()
        sandbox = docker_policy_receipt(policy, observed, run_id=local_state["run_id"], task_id=assignment["task_id"],
           launch_correlation_id=assignment["launch_correlation_id"], signing_key=private)
        capability = self.capability("Local", "local-test")
        capability["tool_policy_sha256"] = sandbox["signed"]["tool_policy_sha256"]
        dispatcher.freeze_providers(local_state, ["Local"])
        dispatcher.record_capability(local_state, capability)
        digest = self.ledger.events(local_state["run_id"], "capability_record")[-1]["payload_sha256"]
        receipt = launch_receipt(local_state, assignment, provider="Local", model="local-test", capability_digest=digest,
                                 tool_policy_sha256=sandbox["signed"]["tool_policy_sha256"])
        now = datetime.now(timezone.utc)
        signed = {
            "run_id": local_state["run_id"], "wave_id": local_state["current_wave_id"], "task_id": assignment["task_id"],
            "launch_correlation_id": assignment["launch_correlation_id"], "nonce": "nonce-0123456789abcdef",
            "selector_alias": "local-worker", "exact_model_id": "local-test", "endpoint": "http://127.0.0.1:8000",
            "tool_policy_sha256": sandbox["signed"]["tool_policy_sha256"], "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        }
        receipt["local_attestation"] = {
            "schema": "LocalLaunchAttestationV2", "algorithm": "Ed25519", "signed": signed,
            "signature": base64.b64encode(private.sign(json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode())).decode(),
        }
        with self.assertRaisesRegex(ValueError, "sandbox_receipt_required"):
            dispatcher.record_launch(local_state, receipt)
        bad_sandbox = json.loads(json.dumps(sandbox))
        bad_sandbox["signed"]["container_id"] = "tampered-container-0123456789"
        receipt["sandbox_receipt"] = bad_sandbox
        with self.assertRaisesRegex(ValueError, "sandbox_receipt_signature_invalid"):
            dispatcher.record_launch(local_state, receipt)
        receipt["sandbox_receipt"] = sandbox
        with self.assertRaisesRegex(ValueError, "sandbox_receipt_not_enforced"):
            dispatcher.record_launch(local_state, receipt)
        tampered_observation = self.mark_sandbox_receipt_host_enforced(sandbox, private)
        tampered_observation["signed"]["observed_policy"]["mounts"][0]["destination"] = "/"
        observation_identity = {
            "container_id": tampered_observation["signed"]["container_id"],
            "observation_source": tampered_observation["signed"]["observation_source"],
            "observed_policy": tampered_observation["signed"]["observed_policy"],
        }
        tampered_observation["signed"]["observation_sha256"] = hashlib.sha256(json.dumps(
            observation_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()).hexdigest()
        tampered_observation["signature"] = base64.b64encode(private.sign(json.dumps(
            tampered_observation["signed"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode())).decode()
        receipt["sandbox_receipt"] = tampered_observation
        with self.assertRaisesRegex(ValueError, "observed_policy_mismatch"):
            dispatcher.record_launch(local_state, receipt)
        receipt["sandbox_receipt"] = self.mark_sandbox_receipt_host_enforced(sandbox, private)
        dispatcher.record_launch(local_state, receipt)

    def test_heartbeat_and_cancellation_release_reservation(self):
        # Diagnostic compatibility path for cancellation accounting only.
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        before = self.state["usage"]["reserved_cost_usd"]
        dispatcher.record_heartbeat(self.state, assignment["task_id"], assignment["launch_correlation_id"], datetime.now(timezone.utc).isoformat())
        dispatcher.cancel_launch(self.state, assignment["task_id"], "fixture timeout")
        self.assertGreater(before, 0)
        self.assertEqual(self.state["usage"]["reserved_cost_usd"], 0)
        self.assertEqual(self.state["tasks"][0]["status"], "cancelled")
        state_file = Path(self.tmp.name) / "cancelled-run.json"
        save_run_state(state_file, self.state)
        self.assertEqual(load_run_state(state_file)["launch_records"][assignment["launch_correlation_id"]]["status"], "cancelled")

    def test_confirmed_cancellation_requeues_without_minting_launch_identity(self):
        # Diagnostic compatibility path: isolate cancellation/requeue evidence
        # from the independently covered capability-admission contract.
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        prior_correlation = assignment["launch_correlation_id"]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        dispatcher.cancel_launch(self.state, assignment["task_id"], "retry confirmed cancellation")

        prior_records = set(self.state["launch_records"])
        dispatcher.requeue_cancelled_task(self.state, assignment["task_id"], "bounded host retry")
        task = self.state["tasks"][0]
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["launch_correlation_id"], prior_correlation)
        self.assertEqual(set(self.state["launch_records"]), prior_records)
        self.assertEqual(self.state["launch_records"][prior_correlation]["status"], "cancelled")
        requeues = self.ledger.events(self.state["run_id"], "cancelled_task_requeued")
        self.assertEqual(len(requeues), 1)
        self.assertIsNone(requeues[0]["payload"]["new_launch_correlation_id"])
        requeued_state_file = Path(self.tmp.name) / "requeued-run.json"
        save_run_state(requeued_state_file, self.state)
        restored = load_run_state(requeued_state_file)
        self.assertEqual(restored["tasks"][0]["status"], "pending")
        self.assertEqual(restored["launch_records"][prior_correlation]["status"], "cancelled")
        crash_replay = json.loads(json.dumps(self.state))
        crash_replay["tasks"][0]["status"] = "cancelled"
        reconciled_event = dispatcher.requeue_cancelled_task(
            crash_replay, assignment["task_id"], "bounded host retry"
        )
        self.assertEqual(reconciled_event, requeues[0]["event_id"])
        self.assertEqual(crash_replay["tasks"][0]["status"], "pending")
        with self.assertRaisesRegex(ValueError, "requeue_reason_mismatch"):
            dispatcher.requeue_cancelled_task(crash_replay, assignment["task_id"], "different retry reason")

        exhausted_replay = json.loads(json.dumps(self.state))
        exhausted_replay["tasks"][0]["status"] = "cancelled"
        exhausted_replay["budget"]["max_total_workers"] = exhausted_replay["usage"]["workers_launched"]
        with self.assertRaisesRegex(ValueError, "requeue_worker_budget_exhausted"):
            dispatcher.requeue_cancelled_task(
                exhausted_replay, assignment["task_id"], "bounded host retry"
            )

        self.ledger.append(self.state["run_id"], "effect_record", {
            "schema": "EffectRecordV1", "effect_id": "effect_after_requeue",
            "idempotency_key": "post-requeue-effect-1", "intent_sha256": "7" * 64,
            "policy_sha256": "8" * 64, "state": "pending",
            "observed_at": "2026-07-10T00:02:00Z",
        }, assignment["task_id"])
        self.ledger.append(self.state["run_id"], "effect_record", {
            "schema": "EffectRecordV1", "effect_id": "effect_after_requeue",
            "idempotency_key": "post-requeue-effect-1", "intent_sha256": "7" * 64,
            "policy_sha256": "8" * 64, "state": "applied",
            "observed_at": "2026-07-10T00:03:00Z",
        }, assignment["task_id"])
        late_effect_replay = json.loads(json.dumps(self.state))
        late_effect_replay["tasks"][0]["status"] = "cancelled"
        with self.assertRaisesRegex(ValueError, "requeue_effect_already_applied"):
            dispatcher.requeue_cancelled_task(
                late_effect_replay, assignment["task_id"], "bounded host retry"
            )

        retry = plan_next_wave(self.state)
        self.assertEqual(retry["action"], "dispatch_wave")
        new_correlation = retry["assignments"][0]["launch_correlation_id"]
        self.assertNotEqual(new_correlation, prior_correlation)
        self.assertEqual(self.state["launch_records"][prior_correlation]["status"], "cancelled")
        self.assertEqual(self.state["launch_records"][new_correlation]["status"], "active")
        self.assertEqual(task["attempts"], 2)

    def test_telemetry_enabled_requeue_emits_standard_replan_event(self):
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        dispatcher.cancel_launch(self.state, assignment["task_id"], "confirmed retry")
        sink = RecordingTelemetrySink()
        dispatcher.telemetry_sink = sink

        event_id = dispatcher.requeue_cancelled_task(
            self.state, assignment["task_id"], "bounded retry"
        )

        self.assertTrue(event_id)
        self.assertEqual(self.state["tasks"][0]["status"], "pending")
        self.assertEqual([event["event_type"] for event in sink.events], ["replan"])
        self.assertEqual(sink.events[0]["fields"]["reason_code"], "confirmed_cancellation_requeued")

    def test_requeue_telemetry_failure_is_diagnostic_after_durable_mutation(self):
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        dispatcher.cancel_launch(self.state, assignment["task_id"], "confirmed retry")
        dispatcher.telemetry_sink = FailingTelemetrySink()

        event_id = dispatcher.requeue_cancelled_task(
            self.state, assignment["task_id"], "bounded retry"
        )

        self.assertTrue(event_id)
        self.assertEqual(self.state["tasks"][0]["status"], "pending")
        events = self.ledger.events(self.state["run_id"])
        requeue_index = next(index for index, event in enumerate(events) if event["event_type"] == "cancelled_task_requeued")
        failure_index = next(index for index, event in enumerate(events) if event["event_type"] == "telemetry_delivery_failed")
        self.assertLess(requeue_index, failure_index)
        failure = events[failure_index]["payload"]
        self.assertEqual(failure["attempted_event_type"], "replan")
        self.assertEqual(failure["control_plane_effect"], "none")
        self.assertEqual(len(failure["failure_sha256"]), 64)

    def test_host_dispatch_cli_exposes_confirmed_cancellation_requeue(self):
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        dispatcher.cancel_launch(self.state, assignment["task_id"], "confirmed CLI cancellation")
        state_file = Path(self.tmp.name) / "cli-requeue-run.json"
        save_run_state(state_file, self.state)

        completed = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "host-dispatch.py"),
                "--state-file", str(state_file),
                "--ledger", str(Path(self.tmp.name) / "run.sqlite3"),
                "--telemetry-env", str(Path(self.tmp.name) / "missing-telemetry.env"),
                "--requeue-json", json.dumps({
                    "task_id": assignment["task_id"],
                    "reason": "bounded operator retry",
                }),
            ],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertIsInstance(output["requeue_event_id"], str)
        self.assertTrue(output["requeue_event_id"])
        restored = load_run_state(state_file)
        self.assertEqual(restored["tasks"][0]["status"], "pending")
        self.assertEqual(
            restored["launch_records"][assignment["launch_correlation_id"]]["status"],
            "cancelled",
        )
        rejected = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "host-dispatch.py"),
                "--state-file", str(state_file),
                "--ledger", str(Path(self.tmp.name) / "run.sqlite3"),
                "--telemetry-env", str(Path(self.tmp.name) / "missing-telemetry.env"),
                "--requeue-json", "[]",
            ],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("requeue_json_object_required", rejected.stderr)
        combined = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "host-dispatch.py"),
                "--state-file", str(state_file),
                "--ledger", str(Path(self.tmp.name) / "run.sqlite3"),
                "--telemetry-env", str(Path(self.tmp.name) / "missing-telemetry.env"),
                "--requeue-json", json.dumps({
                    "task_id": assignment["task_id"], "reason": "bounded operator retry",
                }),
                "--authorize-terminal",
            ],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(combined.returncode, 0)
        self.assertIn("requeue_operation_must_be_exclusive", combined.stderr)

    def test_host_dispatch_rejects_symlink_and_writable_trust_roots(self):
        state_file = Path(self.tmp.name) / "trust-root-run.json"
        save_run_state(state_file, self.state)
        missing_telemetry = Path(self.tmp.name) / "missing-telemetry.env"
        base = [
            sys.executable,
            str(ROOT / "scripts" / "host-dispatch.py"),
            "--state-file", str(state_file),
            "--ledger", str(Path(self.tmp.name) / "trust.sqlite3"),
        ]

        manifest_link = Path(self.tmp.name) / "manifest-link.json"
        manifest_link.symlink_to(ROOT / "integrations" / "codex" / "standalone-runner-manifest.json")
        manifest_result = subprocess.run(
            [*base, "--host-manifest", str(manifest_link), "--telemetry-env", str(missing_telemetry)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(manifest_result.returncode, 0)
        self.assertIn("trusted_file_open_denied", manifest_result.stderr)

        telemetry_target = Path(self.tmp.name) / "telemetry-target.env"
        telemetry_target.write_text(
            "LANGFUSE_HOST=https://example.invalid\n"
            "LANGFUSE_PUBLIC_KEY=public\n"
            "LANGFUSE_SECRET_KEY=secret\n"
            "LANGFUSE_ALLOWED_HOSTS=example.invalid\n",
            encoding="utf-8",
        )
        telemetry_target.chmod(0o600)
        telemetry_link = Path(self.tmp.name) / "telemetry-link.env"
        telemetry_link.symlink_to(telemetry_target)
        telemetry_result = subprocess.run(
            [*base, "--telemetry-env", str(telemetry_link)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(telemetry_result.returncode, 0)
        self.assertIn("trusted_file_open_denied", telemetry_result.stderr)

        public_key = Path(self.tmp.name) / "local-public.key"
        public_key.write_text("fixture-public-key\n", encoding="utf-8")
        public_key.chmod(0o666)
        key_result = subprocess.run(
            [
                *base,
                "--telemetry-env", str(missing_telemetry),
                "--local-public-key-file", str(public_key),
            ],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(key_result.returncode, 0)
        self.assertIn("trusted_file_writable_by_group_or_other", key_result.stderr)

    def test_concurrent_requeue_cli_keeps_one_event_and_consistent_state(self):
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        dispatcher.cancel_launch(self.state, assignment["task_id"], "confirmed concurrent cancellation")
        state_file = Path(self.tmp.name) / "concurrent-requeue-run.json"
        save_run_state(state_file, self.state)
        command = [
            sys.executable, str(ROOT / "scripts" / "host-dispatch.py"),
            "--state-file", str(state_file),
            "--ledger", str(Path(self.tmp.name) / "run.sqlite3"),
            "--telemetry-env", str(Path(self.tmp.name) / "missing-telemetry.env"),
            "--requeue-json", json.dumps({
                "task_id": assignment["task_id"], "reason": "one serialized retry",
            }),
        ]
        processes = [
            subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(2)
        ]
        outcomes = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertTrue(all(returncode == 0 for _stdout, _stderr, returncode in outcomes), outcomes)
        restored = load_run_state(state_file)
        self.assertEqual(restored["tasks"][0]["status"], "pending")
        events = self.ledger.events(self.state["run_id"], "cancelled_task_requeued")
        self.assertEqual(len(events), 1)

    def test_effect_writer_serializes_with_requeue_and_requires_fresh_launch(self):
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        dispatcher.cancel_launch(self.state, assignment["task_id"], "confirmed effect race cancellation")
        attempted = threading.Event()
        finished = threading.Event()
        errors = []

        def append_effect():
            other_ledger = None
            attempted.set()
            try:
                other_ledger = HostLedger(Path(self.tmp.name) / "run.sqlite3")
                other = HostDispatcher(other_ledger, require_capability=False)
                other.record_effect(self.state["run_id"], assignment["task_id"], {
                    "schema": "EffectRecordV1", "effect_id": "effect_race",
                    "idempotency_key": "effect-race-1", "intent_sha256": "9" * 64,
                    "policy_sha256": "a" * 64, "state": "pending",
                    "observed_at": "2026-07-10T00:04:00Z",
                })
            except Exception as exc:  # Assertion inspects the exact fail-closed reason.
                errors.append(str(exc))
            finally:
                if other_ledger is not None:
                    other_ledger.close()
                finished.set()

        with self.ledger.operation_lock():
            thread = threading.Thread(target=append_effect)
            thread.start()
            self.assertTrue(attempted.wait(timeout=2))
            self.assertFalse(finished.wait(timeout=0.1))
            dispatcher.requeue_cancelled_task(self.state, assignment["task_id"], "serialized retry")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["effect_launch_required_after_requeue"])
        self.assertEqual(self.ledger.events(self.state["run_id"], "effect_record"), [])

    def test_ledger_advisory_lock_releases_after_process_kill(self):
        script = (
            "import sys,time; sys.path.insert(0, sys.argv[1]); "
            "from agentfanout.host_runtime import HostLedger; "
            "ledger=HostLedger(sys.argv[2]); "
            "lock=ledger.operation_lock(); lock.__enter__(); "
            "print('locked', flush=True); time.sleep(30)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT), str(Path(self.tmp.name) / "run.sqlite3")],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(process.stdout.readline().strip(), "locked")
        process.kill()
        process.communicate(timeout=5)
        recovered = HostLedger(Path(self.tmp.name) / "run.sqlite3")
        try:
            with recovered.operation_lock():
                event_id = recovered.append("run_lock_recovery", "lock_recovered", {"status": "ok"})
        finally:
            recovered.close()
        self.assertTrue(event_id)

    def test_cancelled_requeue_refuses_missing_confirmation_effect_ambiguity_and_budgets(self):
        # Diagnostic compatibility path: exercise refusal ordering without
        # duplicating the capability fixture in every cancellation case.
        dispatcher = HostDispatcher(
            self.ledger, require_capability=False,
            termination_callback=lambda _request: {"terminated": True, "termination_scope": "process_group_only"},
        )
        assignment = plan_next_wave(self.state)["assignments"][0]
        dispatcher.record_launch(self.state, launch_receipt(self.state, assignment))
        dispatcher.cancel_launch(self.state, assignment["task_id"], "confirmed cancellation")
        effect = {
            "schema": "EffectRecordV1", "effect_id": "effect_requeue", "idempotency_key": "requeue-effect-1",
            "intent_sha256": "a" * 64, "policy_sha256": "b" * 64,
            "state": "unknown_after_crash", "observed_at": "2026-07-10T00:00:00Z",
        }
        dispatcher.record_effect(self.state["run_id"], assignment["task_id"], effect)
        with self.assertRaisesRegex(ValueError, "requeue_effect_unresolved"):
            dispatcher.requeue_cancelled_task(self.state, assignment["task_id"], "must refuse ambiguity")

        applied_tasks = build_task_units(
            "review applied effect", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
            ["request_item_1"], allowed_paths=[str(self.project)],
        )
        applied = initialize_run("review applied effect", applied_tasks)
        applied_assignment = plan_next_wave(applied)["assignments"][0]
        dispatcher.record_launch(applied, launch_receipt(applied, applied_assignment))
        dispatcher.cancel_launch(applied, applied_assignment["task_id"], "confirmed cancellation")
        dispatcher.record_effect(applied["run_id"], applied_assignment["task_id"], {
            "schema": "EffectRecordV1", "effect_id": "effect_applied", "idempotency_key": "applied-effect-1",
            "intent_sha256": "d" * 64, "policy_sha256": "e" * 64,
            "state": "pending", "observed_at": "2026-07-10T00:00:00Z",
        })
        dispatcher.record_effect(applied["run_id"], applied_assignment["task_id"], {
            "schema": "EffectRecordV1", "effect_id": "effect_applied", "idempotency_key": "applied-effect-1",
            "intent_sha256": "d" * 64, "policy_sha256": "e" * 64,
            "state": "applied", "observed_at": "2026-07-10T00:01:00Z",
        })
        with self.assertRaisesRegex(ValueError, "requeue_effect_already_applied"):
            dispatcher.requeue_cancelled_task(applied, applied_assignment["task_id"], "must not duplicate effect")

        clean_tasks = build_task_units(
            "review cancellation evidence", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
            ["request_item_1"], allowed_paths=[str(self.project)],
        )
        unconfirmed = initialize_run("review cancellation evidence", clean_tasks)
        unconfirmed_assignment = plan_next_wave(unconfirmed)["assignments"][0]
        dispatcher.record_launch(unconfirmed, launch_receipt(unconfirmed, unconfirmed_assignment))
        unconfirmed_task = unconfirmed["tasks"][0]
        unconfirmed_record = unconfirmed["launch_records"][unconfirmed_assignment["launch_correlation_id"]]
        reserved = float(unconfirmed_task["reserved_cost_usd"])
        unconfirmed["usage"]["reserved_cost_usd"] = round(unconfirmed["usage"]["reserved_cost_usd"] - reserved, 6)
        unconfirmed_task["reserved_cost_usd"] = 0.0
        unconfirmed_task["status"] = "cancelled"
        unconfirmed_record["status"] = "cancelled"
        self.ledger.append(
            unconfirmed["run_id"], "launch_cancelled",
            {"launch_correlation_id": unconfirmed_assignment["launch_correlation_id"],
             "reason_sha256": "c" * 64, "cancelled_at": datetime.now(timezone.utc).isoformat()},
            unconfirmed_assignment["task_id"],
        )
        with self.assertRaisesRegex(ValueError, "requeue_termination_confirmation_required"):
            dispatcher.requeue_cancelled_task(unconfirmed, unconfirmed_assignment["task_id"], "missing confirmation")

        budget_tasks = build_task_units(
            "review exhausted retry", "bulk-read", "explorer", "Codex", "current-fast-mini", "low",
            ["request_item_1"], allowed_paths=[str(self.project)],
        )
        exhausted = initialize_run("review exhausted retry", budget_tasks)
        exhausted_assignment = plan_next_wave(exhausted)["assignments"][0]
        dispatcher.record_launch(exhausted, launch_receipt(exhausted, exhausted_assignment))
        dispatcher.cancel_launch(exhausted, exhausted_assignment["task_id"], "confirmed budget cancellation")
        exhausted["budget"]["max_total_workers"] = exhausted["usage"]["workers_launched"]
        with self.assertRaisesRegex(ValueError, "requeue_worker_budget_exhausted"):
            dispatcher.requeue_cancelled_task(exhausted, exhausted_assignment["task_id"], "worker budget exhausted")
        exhausted["budget"]["max_total_workers"] += 1
        exhausted["budget"]["max_attempts_per_task"] = exhausted["tasks"][0]["attempts"]
        with self.assertRaisesRegex(ValueError, "requeue_attempt_budget_exhausted"):
            dispatcher.requeue_cancelled_task(exhausted, exhausted_assignment["task_id"], "attempt budget exhausted")

    def test_unresolved_effect_blocks_terminal(self):
        task_id = complete_run(self.dispatcher, self.state)
        self.dispatcher.record_validator_result(self.state, validator_result(self.state, task_id, "result_1"))
        self.assertEqual(plan_next_wave(self.state)["action"], "complete")
        effect = {
            "schema": "EffectRecordV1", "effect_id": "effect_1", "idempotency_key": "idempotency-key-1",
            "intent_sha256": "a" * 64, "policy_sha256": "b" * 64,
            "state": "pending", "observed_at": "2026-07-10T00:00:02Z",
        }
        self.dispatcher.record_effect(self.state["run_id"], task_id, effect)
        with self.assertRaisesRegex(PermissionError, "effect_unresolved"):
            self.dispatcher.authorize_terminal(self.state)
        effect["state"] = "applied"
        effect["observed_at"] = "2026-07-10T00:00:03Z"
        self.dispatcher.record_effect(self.state["run_id"], task_id, effect)
        self.dispatcher.authorize_terminal(self.state)

    def test_signed_local_attestation_rejects_tampering(self):
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        now = datetime.now(timezone.utc)
        signed = {
            "run_id": self.state["run_id"], "wave_id": "wave_" + "a" * 32,
            "task_id": self.state["tasks"][0]["task_id"], "launch_correlation_id": "launch_" + "b" * 32,
            "nonce": "nonce-0123456789abcdef", "selector_alias": "local-worker", "exact_model_id": "local-test",
            "endpoint": "http://127.0.0.1:8000", "tool_policy_sha256": "c" * 64,
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        }
        encoded = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        receipt = {"schema": "LocalLaunchAttestationV2", "algorithm": "Ed25519", "signed": signed,
                   "signature": base64.b64encode(private.sign(encoded)).decode()}
        expected = {key: signed[key] for key in ("run_id", "task_id", "launch_correlation_id", "exact_model_id", "tool_policy_sha256")}
        verify_local_launch_attestation(receipt, base64.b64encode(public).decode(), expected)
        receipt["signed"]["exact_model_id"] = "tampered"
        with self.assertRaisesRegex(ValueError, "mismatch|signature"):
            verify_local_launch_attestation(receipt, base64.b64encode(public).decode(), expected)

    def test_docker_policy_requires_fail_closed_observation(self):
        private = Ed25519PrivateKey.generate()
        policy, observed = self.docker_policy_fixture()
        receipt = docker_policy_receipt(policy, observed, run_id=self.state["run_id"], task_id=self.state["tasks"][0]["task_id"],
                                        launch_correlation_id="launch_" + "a" * 32, signing_key=private)
        self.assertEqual(receipt["signed"]["container_id"], observed["container_id"])
        self.assertEqual(receipt["signed"]["boundary_status"], "experimental_observation_only")
        observed["network_mode"] = "bridge"
        with self.assertRaisesRegex(ValueError, "not_fail_closed"):
            docker_policy_receipt(policy, observed, run_id=self.state["run_id"], task_id=self.state["tasks"][0]["task_id"],
                                  launch_correlation_id="launch_" + "a" * 32, signing_key=private)

    def test_docker_policy_rejects_broad_or_extra_observed_mounts(self):
        private = Ed25519PrivateKey.generate()
        policy, observed = self.docker_policy_fixture()
        broad_policy = {**policy, "read_paths": [str(self.project)]}
        with self.assertRaisesRegex(ValueError, "broad_mount|not_regular_file"):
            docker_policy_receipt(
                broad_policy, observed, run_id=self.state["run_id"], task_id=self.state["tasks"][0]["task_id"],
                launch_correlation_id="launch_" + "a" * 32, signing_key=private,
            )
        observed["mounts"].append({
            "source": str(self.project), "destination": str(self.project), "read_only": True,
        })
        with self.assertRaisesRegex(ValueError, "observation_mismatch"):
            docker_policy_receipt(
                policy, observed, run_id=self.state["run_id"], task_id=self.state["tasks"][0]["task_id"],
                launch_correlation_id="launch_" + "a" * 32, signing_key=private,
            )

    def test_docker_command_is_digest_pinned_and_fail_closed(self):
        target = self.project / "target.txt"
        target.write_text("fixture")
        policy, _observed = self.docker_policy_fixture()
        policy["read_paths"] = []
        policy["write_files"] = [str(target)]
        command = build_docker_tool_command(policy)
        joined = " ".join(command)
        for expected in ("--read-only", "--network none", "--cap-drop ALL", "no-new-privileges", "--user 65534:65534", "--host unix:///var/run/docker.sock"):
            self.assertIn(expected, joined)
        policy["image"] = "example.invalid/tool:latest"
        with self.assertRaisesRegex(ValueError, "digest"):
            build_docker_tool_command(policy)

    def test_docker_command_rejects_comma_mount_path(self):
        bad_path = self.project / "input,name.txt"
        bad_path.write_text("fixture")
        policy, _observed = self.docker_policy_fixture()
        policy["read_paths"] = [str(bad_path)]
        with self.assertRaisesRegex(ValueError, "mount_path_unsafe"):
            build_docker_tool_command(policy)

    def test_docker_timeout_reports_container_state_unknown(self):
        policy, _observed = self.docker_policy_fixture()

        class FakeProcess:
            pid = 424242
            returncode = None

            def __init__(self):
                self.wait_calls = 0

            def wait(self, timeout):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("docker", timeout)
                self.returncode = -signal.SIGTERM
                return self.returncode

        fake = FakeProcess()

        def fake_killpg(_pid, requested_signal):
            if requested_signal == 0:
                raise ProcessLookupError

        with patch("agentfanout.host_runtime.subprocess.Popen", return_value=fake), patch(
            "agentfanout.host_runtime.os.killpg", side_effect=fake_killpg
        ):
            result = execute_docker_tool(policy)
        self.assertEqual(result["status"], "timeout_container_state_unknown")
        self.assertTrue(result["docker_cli_process_group_exit_confirmed"])
        self.assertFalse(result["container_exit_confirmed"])
        self.assertFalse(result["cancellation_confirmed"])
        self.assertEqual(result["external_effect_status"], "unknown_after_timeout")
        self.assertFalse(result["enforced_local_receipt_eligible"])


if __name__ == "__main__":
    unittest.main()
