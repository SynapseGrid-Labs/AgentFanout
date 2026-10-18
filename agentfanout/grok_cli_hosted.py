"""Hosted runner interfaces for conformed, injected GrokCLI sessions.

No production transport or evidence authority is selected by this module. A host
must provide all callbacks explicitly. Public registry admission remains disabled
until installed CLI conformance. Synthetic tests exercise the real host lifecycle.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone

from .grok_cli import (ACPClient, AdmissionEvidence, GrokCLIUnavailable,
                       normalize_usage, UsageReconciler, input_digest)


def _packet(value):
    # Task data is a bounded JSON packet, never shell/path/config authority.
    return {key: item for key, item in value.items() if not key.startswith("_")}


class GrokCLIWorkerAdapter:
    provider = "GrokCLI"
    supports_context_reuse = False
    no_tool_retry_safe = False

    def __init__(self, *, evidence: AdmissionEvidence, host_verify, transport_factory,
                 capability_factory, authorize, durable_check, journal,
                 usage_semantics, project_root=None, bounded_task_factory=None, usage_state=None, workspace="/empty", wire_mapping=None):
        self.wire_mapping = wire_mapping
        if wire_mapping is not None:
            wire_mapping.verify(evidence)
        self.evidence = evidence
        self.host_verify = host_verify
        self.transport_factory = transport_factory
        self.capability_factory = capability_factory
        self.authorize = authorize
        self.durable_check = durable_check
        self.journal = journal
        self.usage_semantics = usage_semantics
        self.workspace = workspace
        if project_root is None and bounded_task_factory is None:
            raise GrokCLIUnavailable("bounded_input_authority_required")
        self.project_root = project_root
        self.bounded_task_factory = bounded_task_factory
        if wire_mapping is not None:
            from .grok_abi import MappedUsageReconciler
            self._usage = MappedUsageReconciler(usage_state)
        else:
            self._usage = UsageReconciler(usage_state)
        self._lock = threading.Lock()

    def prepare(self, assignment, tool_policy_sha256):
        self.evidence.verify(self.host_verify)
        if self.wire_mapping is not None:
            self.wire_mapping.verify(self.evidence)
        task = assignment["host_task"]
        if task["reasoning_effort"] != self.evidence.effort:
            raise GrokCLIUnavailable("effective_selection_mismatch")
        capability = self.capability_factory(task, tool_policy_sha256, self.evidence)
        if capability.get("provider") != self.provider or capability.get("exact_model_id") != self.evidence.model:
            raise GrokCLIUnavailable("host_capability_mismatch")
        return {"resolved_model": self.evidence.model, "capability": capability}

    def _bounded_task(self, task):
        if self.bounded_task_factory is not None:
            bounded = self.bounded_task_factory(task)
        else:
            from .runner import CodexWorkerAdapter
            bounded = CodexWorkerAdapter(self.project_root)._bounded_task(task)
        if not isinstance(bounded, dict) or not isinstance(bounded.get("bounded_input"), dict):
            raise GrokCLIUnavailable("bounded_input_required")
        content = bounded["bounded_input"].get("content")
        if not isinstance(content, str) or not content:
            raise GrokCLIUnavailable("bounded_input_required")
        input_digest(content)
        # Host paths, budgets and private policy state stay outside provider prompt.
        allowed = {"schema", "task_id", "title", "scope", "input_ref", "input_sha256", "input_bytes",
            "task_class", "role", "reasoning_effort", "parent_task_id", "launch_correlation_id", "bounded_input"}
        return {key: value for key, value in bounded.items() if key in allowed}

    def preflight(self, assignment):
        self.evidence.verify(self.host_verify)
        return {**assignment, "_bounded_task": self._bounded_task(assignment["task"])}

    def _execute(self, task, prompt, role, receipt, confirm_exit, cancelled):
        if cancelled and cancelled():
            raise GrokCLIUnavailable("cancelled_before_session")
        if self.wire_mapping is not None:
            self.wire_mapping.verify(self.evidence)
        if self.journal(task, role, "harmless_bootstrap_start_intent", {"evidence_id": self.evidence.evidence_id}) is not True:
            raise GrokCLIUnavailable("journal_not_durable")
        transport = self.transport_factory(role, task, self.evidence)
        client = None
        try:
            client = ACPClient(transport, self.evidence, self.host_verify,
                lambda kind, data: self.journal(task, role, kind, data), wire_mapping=self.wire_mapping)
            prepared = client.prepare(task["task_id"], prompt, role, self.workspace)
            # Only host callback may construct/verify a durable authorization.
            authorization = self.authorize(task, role, prepared, receipt)
            if cancelled and cancelled():
                client.cancel()
                raise GrokCLIUnavailable("cancellation_uncertain")
            output = client.execute(prompt, authorization, self.durable_check)
            result = json.loads(output["text"])
            contract = "WorkerResultV1" if role == "worker" else "ValidatorResultV1"
            if not isinstance(result, dict) or result.get("schema") != contract:
                raise GrokCLIUnavailable("result_contract_mismatch")
            if role == "worker" and (result.get("task_id") != task["task_id"] or
                    result.get("launch_correlation_id") != task["launch_correlation_id"]):
                raise GrokCLIUnavailable("result_correlation_mismatch")
            if any(result.get(key) not in ([], None) for key in ("changed_files", "commands", "exit_codes")):
                raise GrokCLIUnavailable("no_tool_claim_invalid")
            mapped = output.get("mapped_usage")
            observation = normalize_usage(mapped["raw"] if mapped else output["result"].get("usage", {}),
                process_epoch=prepared.process_epoch, session_id=prepared.session_id,
                request_id=authorization.receipt_id, semantics=mapped["semantics"] if mapped else self.usage_semantics)
            with self._lock:
                usage = self._usage.reconcile(observation, mapped["audit"]) if mapped else self._usage.reconcile(observation)
                if self.journal(task, role, "usage_reconciled", {
                    "observation": asdict(observation), "usage": usage,
                    "recovery_state": self._usage.state,
                    "wire_usage": mapped["audit"] if mapped else None}) is not True:
                    raise GrokCLIUnavailable("journal_not_durable")
            if usage["duplicate"]:
                raise GrokCLIUnavailable("duplicate_inference_result")
            transport.close()
            # Transport contract explicitly reports *local* supervisor confirmation.
            # Remote cancellation remains separate and is never inferred from this.
            if getattr(transport, "exit_confirmed", False) is not True:
                raise GrokCLIUnavailable("process_exit_unconfirmed")
            confirm_exit()
            return {"resolved_model": self.evidence.model, "result": result,
                "process_group_exit_confirmed": True, "provider_session_id": prepared.session_id,
                "wire_usage": mapped["audit"] if mapped else None,
                "usage": {"usage_verified": True, "cost_verified": usage["provider_cost_usd"] is not None and usage["provider_cost_verified"],
                    "cost_usd": usage["provider_cost_usd"], **usage["tokens"],
                    "cached_tokens": usage["tokens"]["cached_input_tokens"],
                    "uncached_input_tokens": usage["tokens"]["input_tokens"] - usage["tokens"]["cached_input_tokens"],
                    "visible_output_tokens": usage["tokens"]["output_tokens"] - usage["tokens"]["reasoning_tokens"]}}
        except Exception:
            if client is not None and client.prompted:
                client.uncertain = True
                self.journal(task, role, "session_reconciliation_required", {"process_epoch": transport.epoch})
            raise
        finally:
            transport.close()

    def run(self, assignment, receipt, heartbeat):
        if not isinstance(assignment.get("_bounded_task"), dict):
            raise GrokCLIUnavailable("bounded_preflight_required")
        task = dict(assignment["_bounded_task"])
        task["launch_correlation_id"] = receipt["launch_correlation_id"]
        task["expected_result_id"] = "result_" + receipt["launch_correlation_id"].removeprefix("launch_")
        prompt = json.dumps({"output_contract": "WorkerResultV1", "task": _packet(task)}, sort_keys=True)
        from .provider_adapters import sanitized_prompt
        prompt = sanitized_prompt(prompt)
        heartbeat(datetime.now(timezone.utc).isoformat())
        return self._execute(task, prompt, "worker", receipt,
            assignment["_confirm_process_group_exit"], assignment.get("_cancellation_requested"))


class GrokCLIValidatorAdapter(GrokCLIWorkerAdapter):
    def prepare(self, task, result, tool_policy_sha256):
        return super().prepare({"host_task": task}, tool_policy_sha256)

    def run(self, task, result):
        prompt = json.dumps({"output_contract": "ValidatorResultV1", "task": self._bounded_task(task),
            "worker_result": result, "peer_results": task.get("_peer_worker_results", [])}, sort_keys=True)
        from .provider_adapters import sanitized_prompt
        prompt = sanitized_prompt(prompt)
        return self._execute(task, prompt, "validator", None,
            task["_confirm_process_group_exit"], task.get("_cancellation_requested"))
