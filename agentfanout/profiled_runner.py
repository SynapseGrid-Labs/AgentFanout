"""Opt-in hosted routing over frozen per-assignment contracts.

This engineering path requires host-injected conformance and resource observers.
The shipped command-line profiles refuse production admission until conformance.
It neither changes native V2 nor grants adapters persistence authority.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import os

from typing import Any, Callable

from .egress import authorize_packet, destination_allowed, filter_peer_evidence
from .mixed_contracts import (
    MixedContractError, assert_execution_ready, assert_reader_compatible,
    binding_for, canonical_digest, validate_contract,
)
from .runner import HostedFanoutRunner, HostedRunnerError
from .project_binding import ProjectBindingError, secure_private_file


class ProfiledHostedRunner(HostedFanoutRunner):
    def __init__(self, *args, contract: dict[str, Any], runtime_identity: dict[str, Any],
                 worker_bindings: dict[str, Any], validator_bindings: dict[str, Any],
                 host_conformance_check: Callable[[dict[str, Any]], bool],
                 observe_resources: Callable[[str], dict[str, dict[str, Any]]],
                 admission_controls: dict[str, bool], host_destination: str,
                 parent_destination: str, **kwargs):
        self.contract = deepcopy(validate_contract(contract))
        self.transition_history = []
        if self.contract["allowed_transitions"] and self.contract["schema"] != "MixedRunContractV2":
            raise MixedContractError("persisted_transition_execution_unsupported")
        assert_reader_compatible(self.contract, runtime_identity, supported_schemas={"MixedRunContractV1", "MixedRunContractV2"})
        self.runtime_identity = deepcopy(runtime_identity)
        self.worker_bindings = dict(worker_bindings)
        self.validator_bindings = dict(validator_bindings)
        self.host_conformance_check = host_conformance_check
        self.observe_resources = observe_resources
        self.admission_controls = dict(admission_controls)
        self.host_destination = host_destination
        self.parent_destination = parent_destination
        exact_providers = sorted({binding["provider"]
                                  for assignment in self.contract["assignments"]
                                  for binding in (assignment["worker"], assignment["validator"])
                                  if binding is not None})
        exact_providers = sorted(set(exact_providers) | {e["to_binding"]["provider"] for e in self.contract["allowed_transitions"]})
        kwargs["approved_providers"] = exact_providers
        super().__init__(*args, **kwargs)
        # Legacy adapter maps remain constructor plumbing, never extra authority.
        self.approved_providers = exact_providers

    def _effective_contract(self):
        if self.contract["schema"] == "MixedRunContractV2":
            from .mixed_transitions import effective_contract
            return effective_contract(self.contract, self.transition_history)
        return self.contract

    def _root_assignment(self, state, task):
        by_id = {item["task_id"]: item for item in state["tasks"]}
        seen = set()
        while True:
            if task["task_id"] in seen:
                raise MixedContractError("residual_assignment_cycle")
            seen.add(task["task_id"])
            if any(item["assignment_id"] == task["task_id"] for item in self.contract["assignments"]):
                return next(item for item in self._effective_contract()["assignments"] if item["assignment_id"] == task["task_id"])
            parent = by_id.get(task.get("parent_task_id"))
            if parent is None:
                raise MixedContractError("unfrozen_assignment")
            # Residual work inherits only the same bounded scope/host path grants.
            for field in ("allowed_paths", "write_set", "task_class", "reasoning_effort"):
                if task.get(field) != parent.get(field):
                    raise MixedContractError("residual_binding_scope_changed")
            task = parent

    def _binding(self, state, task, role):
        root = self._root_assignment(state, task)
        binding = deepcopy(root[role])
        if role == "worker":
            model = task["provider_model"]
            if (binding["provider"] != model["provider"] or binding["model_selector"] != model["selector"]
                    or binding["resolved_model"] != model.get("resolved_model")
                    or binding["reasoning_effort"] != task["reasoning_effort"]):
                raise MixedContractError("task_frozen_binding_mismatch")
            if bool(task.get("validator_required")) != root["validation_required"]:
                raise MixedContractError("task_validation_requirement_mismatch")
        return binding

    def _adapter(self, state, task, role):
        binding = self._binding(state, task, role)
        if binding is None:
            return None
        adapters = self.worker_bindings if role == "worker" else self.validator_bindings
        adapter = adapters.get(canonical_digest(binding))
        if adapter is None or adapter.provider != binding["provider"]:
            raise HostedRunnerError(role + "_binding_adapter_unavailable")
        if not callable(getattr(adapter, "identity", None)):
            raise HostedRunnerError(role + "_binding_identity_required")
        if canonical_digest(adapter.identity()) != binding["identity_sha256"]:
            raise HostedRunnerError(role + "_binding_identity_drift")
        return adapter

    def _worker_for(self, state, task):
        return self._adapter(state, task, "worker")

    def _validator_for(self, state, task):
        return self._adapter(state, task, "validator")

    def _load(self):
        from .mixed_state import load_hosted_state
        envelope = load_hosted_state(self.state_file, self.runtime_identity)
        if envelope["contract"] != self.contract:
            raise MixedContractError("persisted_contract_changed")
        self.transition_history = deepcopy(envelope.get("transition_history", []))
        return envelope["legacy_state"]

    def _save(self, state):
        from .mixed_state import save_hosted_state
        envelope = {"schema": "HostedRunStateV4" if self.contract["schema"] == "MixedRunContractV2" else "HostedRunStateV3", "contract": self.contract, "legacy_state": state}
        if self.contract["schema"] == "MixedRunContractV2":
            envelope["transition_history"] = deepcopy(self.transition_history)
        save_hosted_state(self.state_file, envelope, self.runtime_identity)

    def _guard_binding(self, state):
        state_path = os.path.abspath(os.path.normpath(str(self.state_file.expanduser())))
        return {"schema": "ProfiledSessionGuardV1", "run_id": state["run_id"],
                "contract_sha256": self.contract["contract_sha256"],
                "runtime_sha256": canonical_digest(self.runtime_identity),
                "state_path_sha256": hashlib.sha256(os.fsencode(state_path)).hexdigest(),
                "host_destination": self.host_destination,
                "parent_destination": self.parent_destination}

    def _before_run(self, dispatcher, state):
        dispatcher.coordinator_boundary_verifier = self._verify_coordinator_boundary
        self._register_contract(dispatcher, state)
        self._recover_transitions(dispatcher.ledger, state)
        self._before_plan(dispatcher, state)

    def _register_contract(self, dispatcher, state):
        self._execution_state = state
        if self.contract["run_id"] != state["run_id"]:
            raise MixedContractError("contract_run_mismatch")
        expected_guard = self._guard_binding(state)
        guard_events = dispatcher.ledger.events(state["run_id"], "profiled_session_guard")
        if guard_events:
            if len(guard_events) != 1 or guard_events[0]["payload"] != expected_guard:
                raise MixedContractError("profiled_session_guard_mismatch")
        elif (state["launch_records"] or dispatcher.ledger.events(state["run_id"], "launch_receipt")
              or dispatcher.ledger.events(state["run_id"], "mixed_contract_frozen")):
            raise MixedContractError("active_run_profiled_guard_missing")
        else:
            dispatcher.ledger.append(state["run_id"], "profiled_session_guard", expected_guard)
        events = dispatcher.ledger.events(state["run_id"], "mixed_contract_frozen")
        if events:
            if len(events) != 1 or events[0]["payload"] != self.contract:
                raise MixedContractError("ledger_contract_binding_mismatch")
        else:
            # New contract registration cannot reinterpret already-started work.
            if state["launch_records"] or dispatcher.ledger.events(state["run_id"], "launch_receipt"):
                raise MixedContractError("active_run_contract_missing")
            dispatcher.ledger.append(state["run_id"], "mixed_contract_frozen", self.contract)

    def _before_plan(self, dispatcher, state):
        remaining_required = 0.0
        for task in state["tasks"]:
            if task["status"] in {"completed", "cancelled", "failed", "blocked"} and (
                    not task.get("validator_required") or state["validation_status"].get(task["task_id"]) == "passed"):
                continue
            root = self._root_assignment(state, task)
            worker_binding = self._binding(state, task, "worker")
            if task["status"] not in {"completed", "cancelled", "blocked"}:
                remaining_required += worker_binding["reservation_cost_usd"]
            validator_binding = self._binding(state, task, "validator")
            if validator_binding is not None and state["validation_status"].get(task["task_id"]) != "passed":
                remaining_required += validator_binding["reservation_cost_usd"]
            assert_execution_ready(self._effective_contract(), root["assignment_id"],
                                   admission_controls=self.admission_controls,
                                   observed_resources=self.observe_resources(root["assignment_id"]))
            for role in ("worker", "validator"):
                binding = self._binding(state, task, role)
                if binding is not None:
                    self._adapter(state, task, role)
                    if self.host_conformance_check(deepcopy(binding)) is not True:
                        raise MixedContractError("host_provider_conformance_unproved")
            # Receipts, errors, usage, artifacts and residual text stay host-local
            # only when all of those destinations were approved at admission.
            for category in ("worker_result", "artifact", "error", "residual", "telemetry"):
                if not destination_allowed(root["egress_policy"], category, self.host_destination):
                    raise MixedContractError("host_evidence_destination_denied")

        remaining_budget = float(state["budget"]["max_total_cost_usd"]) - float(state["usage"]["cost_usd"])
        if remaining_required > remaining_budget + 1e-9:
            raise HostedRunnerError("worker_validator_reservations_insufficient")

    def _refresh(self, state, task):
        root = self._root_assignment(state, task)
        assert_execution_ready(self._effective_contract(), root["assignment_id"],
                               admission_controls=self.admission_controls,
                               observed_resources=self.observe_resources(root["assignment_id"]))
        for role in ("worker", "validator"):
            binding = self._binding(state, task, role)
            if binding is not None:
                self._adapter(state, task, role)
                if self.host_conformance_check(deepcopy(binding)) is not True:
                    raise MixedContractError("host_provider_conformance_unproved")

    def _before_worker_execute(self, assignment, receipt):
        self._refresh(self._execution_state, assignment["host_task"])

    def _before_validator_execute(self, task):
        original = next(item for item in self._execution_state["tasks"] if item["task_id"] == task["task_id"])
        self._refresh(self._execution_state, original)

    def _prepare_launch(self, dispatcher, state, assignment):
        task = next(item for item in state["tasks"] if item["task_id"] == assignment["task_id"])
        self._refresh(state, task)
        root = self._root_assignment(state, task)
        binding = self._binding(state, task, "worker")
        payload = assignment.get("_bounded_task", assignment["task"])
        authorize_packet(root["egress_policy"], "input", binding["destination_id"], payload)
        adapter, receipt = super()._prepare_launch(dispatcher, state, assignment)
        dispatcher.ledger.append(state["run_id"], "mixed_launch_binding", {
            "contract_sha256": self.contract["contract_sha256"], "role": "worker",
            "binding_sha256": canonical_digest(binding),
            "launch_correlation_id": receipt["launch_correlation_id"]}, task["task_id"])
        return adapter, receipt

    def _verify_coordinator_boundary(self, state, task, boundary, capability, role):
        from .coordinator_hosted import verify_boundary
        binding = self._binding(state, task, role)
        if self.host_conformance_check(deepcopy(binding)) is not True:
            raise MixedContractError("host_provider_conformance_unproved")
        verify_boundary(boundary, run_id=state["run_id"], task=task, role=role,
                        binding=binding, capability=capability,
                        assignment_id=self._root_assignment(state, task)["assignment_id"])

    def _validate_prepared_worker(self, state, task, prepared):
        binding = self._binding(state, task, "worker")
        if binding["provider"] == "Local":
            self._verify_coordinator_boundary(state, task, prepared.get("coordinator_hosted_boundary"), prepared.get("capability", {}), "worker")
        if prepared.get("resolved_model") != binding["resolved_model"] or prepared.get("capability", {}).get("reasoning_effort") != binding["reasoning_effort"]:
            raise MixedContractError("prepared_exact_model_drift")
        if abs(float(task["reserved_cost_usd"]) - binding["reservation_cost_usd"]) > 1e-9:
            raise MixedContractError("worker_reservation_binding_mismatch")

    def _validator_input_task(self, state, task):
        binding = self._binding(state, task, "validator")
        if binding is None:
            raise MixedContractError("required_validator_unbound")
        packet = dict(task)
        packet["reasoning_effort"] = binding["reasoning_effort"]
        packet["provider_model"] = {**task["provider_model"], "provider": binding["provider"],
                                    "selector": binding["model_selector"], "resolved_model": binding["resolved_model"]}
        policy = self._root_assignment(state, task)["egress_policy"]
        # Host paths remain in host-only adapter bookkeeping, never egress authority.
        authorize_packet(policy, "validator_input", binding["destination_id"],
                         {key: value for key, value in packet.items() if not key.startswith("_")})
        return packet

    def _validate_prepared_validator(self, state, task, prepared):
        self._refresh(state, task)
        binding = self._binding(state, task, "validator")
        capability = prepared.get("capability", {})
        if binding["provider"] == "Local":
            self._verify_coordinator_boundary(state, task, prepared.get("coordinator_hosted_boundary"), capability, "validator")
        if prepared.get("resolved_model") != binding["resolved_model"] or capability.get("reasoning_effort") != binding["reasoning_effort"]:
            raise MixedContractError("validator_prepared_binding_drift")

    def _usage(self, state, assignment, value):
        task = next(item for item in state["tasks"] if item["task_id"] == assignment["task_id"])
        policy = self._root_assignment(state, task)["egress_policy"]
        authorize_packet(policy, "worker_result", self.host_destination, value.get("result"))
        authorize_packet(policy, "telemetry", self.host_destination, value.get("usage"))
        result = value.get("result", {})
        authorize_packet(policy, "artifact", self.host_destination, result.get("evidence", []))
        authorize_packet(policy, "residual", self.host_destination, result.get("remaining_work", []))
        return super()._usage(state, assignment, value)

    def _validator_accounting_reservation(self, dispatcher, state, task, validator_correlation, validator_wave_reserved):
        binding = self._binding(state, task, "validator")
        cost = binding["reservation_cost_usd"]
        available = (float(state["budget"]["max_total_cost_usd"]) - float(state["usage"]["cost_usd"])
                     - float(state["usage"].get("reserved_cost_usd", 0.0)) - validator_wave_reserved)
        if available + 1e-9 < cost:
            raise HostedRunnerError("validator_accounting_reservation_insufficient")
        # Separate numeric validator capacity; do not erase the worker charge.
        payload = {"schema": "ValidatorAccountingReservationV1", "validator_launch_correlation_id": validator_correlation,
                   "reserved_cost_usd": cost, "reservation_basis": "profile_validator_bound",
                   "covered_task_cap_usd": None, "provider_side_spend_cap": False,
                   "accepted_accounting_only": True, "reserved_at": datetime.now(timezone.utc).isoformat(),
                   "released": False, "contract_sha256": self.contract["contract_sha256"],
                   "binding_sha256": canonical_digest(binding)}
        event_id = dispatcher.ledger.append(state["run_id"], "validator_accounting_reserved", payload, task["task_id"])
        return {**payload, "event_id": event_id}

    def _validate_validator_output(self, state, task, output):
        policy = self._root_assignment(state, task)["egress_policy"]
        authorize_packet(policy, "worker_result", self.host_destination, output.get("result"))
        authorize_packet(policy, "telemetry", self.host_destination, output.get("usage"))

    def _peer_results(self, dispatcher, state, task):
        root = self._root_assignment(state, task)
        binding = self._binding(state, task, "validator")
        if binding is None:
            raise MixedContractError("required_validator_unbound")
        own = dispatcher.ledger.latest_task_event(state["run_id"], task["task_id"], "accepted_result_packet")
        authorize_packet(root["egress_policy"], "validator_input", binding["destination_id"], own["result"])
        peers = []
        for peer in state["tasks"]:
            if peer["task_id"] == task["task_id"]:
                continue
            accepted = dispatcher.ledger.latest_task_event(state["run_id"], peer["task_id"], "accepted_result_packet")
            if isinstance(accepted, dict) and isinstance(accepted.get("result"), dict):
                result = accepted["result"]
                compact = {key: result[key] for key in ("task_id", "result_id", "status", "summary", "evidence") if key in result}
                peers.append((self._root_assignment(state, peer)["egress_policy"], compact))
        return filter_peer_evidence(peers, binding["destination_id"])

    def present(self, outcome):
        """Bounded parent presentation; never return raw state or local-only peers."""
        state = outcome["state"]
        # Even status/IDs are evidence destinations. Denial exposes no metadata.
        for root in self.contract["assignments"]:
            if not destination_allowed(root["egress_policy"], "parent_synthesis", self.parent_destination):
                raise MixedContractError("parent_synthesis_destination_denied")
        from .host_runtime import HostLedger
        summaries = []
        ledger = HostLedger(self.ledger_file)
        try:
            for task in state["tasks"][:32]:
                policy = self._root_assignment(state, task)["egress_policy"]
                if destination_allowed(policy, "parent_synthesis", self.parent_destination):
                    accepted = ledger.latest_task_event(state["run_id"], task["task_id"], "accepted_result_packet")
                    result = accepted.get("result", {}) if isinstance(accepted, dict) else {}
                    packet = {"task_id": task["task_id"], "status": task["status"],
                              "summary": str(result.get("summary", ""))[:900],
                              "evidence": [str(item)[:200] for item in result.get("evidence", [])[:3]]}
                    summaries.append(authorize_packet(policy, "parent_synthesis", self.parent_destination, packet))
        finally:
            ledger.close()
        return {"schema": "ProfiledHostedOutcomeV1", "status": outcome["status"],
                "run_id": state["run_id"], "contract_sha256": self.contract["contract_sha256"],
                "accepted_summaries": summaries, "results_omitted": max(0, len(state["tasks"]) - 32), "full_state_omitted": True, "fallback_allowed": False}

    def run(self, max_cycles=100):
        return self._with_driver_lock(lambda: self._run_locked(max_cycles))

    def _with_driver_lock(self, action):
        # Lifetime ownership is separate from state-save CAS. The retained lock
        # inode prevents another controller from journaling or bootstrapping
        # between saves; never unlink a held or released lifetime lock.
        lock_path = self.state_file.with_name(self.state_file.name + ".driver.lock")
        descriptor = None
        try:
            try:
                _path, descriptor = secure_private_file(lock_path, create=True, max_bytes=4096)
            except ProjectBindingError as exc:
                raise MixedContractError("profiled_driver_lock_unsafe") from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MixedContractError("profiled_driver_already_active") from exc
            return action()
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _recover_transitions(self, ledger, state):
        from .mixed_transitions import apply_record, validate_reconciliation
        intents = ledger.events(state['run_id'], 'mixed_transition_authorized')
        commits = ledger.events(state['run_id'], 'mixed_transition_committed')
        if self.contract['schema'] != 'MixedRunContractV2':
            if intents or commits:
                raise MixedContractError('transition_reader_unsupported')
            return
        if len(self.transition_history) > len(intents):
            raise MixedContractError('transition_host_authorization_missing')
        for i, event in enumerate(intents):
            payload = event['payload']
            if not isinstance(payload,dict) or set(payload) != {'record','proof'}:
                raise MixedContractError('transition_journal_invalid')
            record, proof = payload['record'], payload['proof']
            if canonical_digest(proof) != record['proof_sha256'] or record['sequence'] != i + 1:
                raise MixedContractError('transition_journal_proof_mismatch')
            existing_commit = [e for e in commits if e['payload'].get('transition_id') == record['transition_id']]
            if not existing_commit:
                all_events = ledger.events(state['run_id'])
                position = next(n for n,e in enumerate(all_events) if e['event_id'] == event['event_id'])
                if any(e['event_type'] not in {'mixed_transition_authorized','mixed_transition_committed'} for e in all_events[position+1:]):
                    raise MixedContractError('transition_recovery_later_effects_exist')
            if i < len(self.transition_history):
                if self.transition_history[i] != record:
                    raise MixedContractError('transition_state_journal_conflict')
            else:
                # Reconciliation proof pins the exact pre-intent state and ledger
                # tip. Crashed authorization cannot be rebased onto later work.
                prior = ledger.events(state['run_id'])
                index = next(n for n,e in enumerate(prior) if e['event_id'] == event['event_id'])
                tip = prior[index-1]['event_id'] if index else None
                validate_reconciliation(proof,state,self.contract,tip)
                if any(e['event_type'] not in {'mixed_transition_authorized','mixed_transition_committed'} for e in prior[index+1:]):
                    raise MixedContractError('transition_recovery_later_effects_exist')
                apply_record(state,self.contract,self.transition_history,record)
                self._save(state)
            matching = [e for e in commits if e['payload'].get('transition_id') == record['transition_id']]
            expected = {'transition_id':record['transition_id'],'record_sha256':canonical_digest(record)}
            if len(matching) > 1 or (matching and matching[0]['payload'] != expected):
                raise MixedContractError('transition_commit_conflict')
            if not matching:
                ledger.append(state['run_id'],'mixed_transition_committed',expected,record['assignment_id'])
        if any(e['payload'].get('transition_id') not in {r['transition_id'] for r in self.transition_history} for e in commits):
            raise MixedContractError('transition_commit_without_intent')

    def transition(self, assignment_id, role, next_binding, reason, *, reconcile, authorize, transition_id=None):
        """Host-only effect-free routing change, serialized with dispatch.

        Coordinator ownership/effects must be positively reconciled first. This
        method cannot switch models, cancel work, or synthesize that proof.
        """
        def transact():
            import uuid
            from .host_runtime import HostLedger
            from .mixed_transitions import apply_record, validate_reconciliation, family
            from types import SimpleNamespace
            state = self._load()
            ledger = HostLedger(self.ledger_file)
            try:
                self._register_contract(SimpleNamespace(ledger=ledger),state)
                self._recover_transitions(ledger,state)
                if self.contract['schema'] != 'MixedRunContractV2':
                    raise MixedContractError('transition_reader_unsupported')
                identifier = transition_id or str(uuid.uuid4())
                prior = next((r for r in self.transition_history if r['transition_id'] == identifier),None)
                if prior is not None:
                    if (prior['assignment_id'],prior['role'],prior['to_binding'],prior['reason']) != (assignment_id,role,next_binding,reason):
                        raise MixedContractError('transition_id_reused')
                    return deepcopy(prior)
                events = ledger.events(state['run_id'])
                tip = events[-1]['event_id'] if events else None
                if not callable(reconcile) or not callable(authorize):
                    raise MixedContractError('transition_host_authority_required')
                proof = reconcile(deepcopy(state),self.contract['contract_sha256'],tip)
                validate_reconciliation(proof,state,self.contract,tip)
                current = binding_for(self._effective_contract(),assignment_id,role)
                record = {'schema':'MixedTransitionV1','transition_id':identifier,'sequence':len(self.transition_history)+1,
                          'assignment_id':assignment_id,'role':role,'from_binding_sha256':canonical_digest(current),
                          'to_binding':deepcopy(next_binding),'reason':reason,'proof_sha256':canonical_digest(proof)}
                trial_state, trial_history = deepcopy(state),deepcopy(self.transition_history)
                apply_record(trial_state,self.contract,trial_history,record)
                if any(e['task_id'] in family(state,assignment_id)
                       for e in ledger.events(state['run_id'],'accepted_result_packet')) and role == 'worker':
                    raise MixedContractError('transition_worker_result_or_effect_exists')
                # Pre-admission checks include both roles, exact resources,
                # conformance, egress and budget, before durable authorization.
                old_history = self.transition_history
                try:
                    self.transition_history = trial_history
                    self._before_plan(SimpleNamespace(ledger=ledger),trial_state)
                finally:
                    self.transition_history = old_history
                payload = {'record':record,'proof':deepcopy(proof)}
                if authorize(deepcopy(payload)) is not True:
                    raise MixedContractError('transition_durable_host_authorization_required')
                latest = ledger.events(state['run_id'])
                if (latest[-1]['event_id'] if latest else None) != tip:
                    raise MixedContractError('transition_authorization_mutated_run')
                ledger.append(state['run_id'],'mixed_transition_authorized',payload,assignment_id)
                self._recover_transitions(ledger,state)
                return deepcopy(record)
            finally:
                ledger.close()
        return self._with_driver_lock(transact)

    def _run_locked(self, max_cycles=100):
        try:
            return self.present(super().run(max_cycles))
        except (MixedContractError, HostedRunnerError) as exc:
            # Host-facing typed block; presentation policy is checked separately.
            state = self._load()
            for root in self.contract["assignments"]:
                if not destination_allowed(root["egress_policy"], "error", self.parent_destination):
                    raise MixedContractError("parent_error_destination_denied") from exc
            return {"schema": "ProfiledHostedOutcomeV1", "status": "blocked", "run_id": state["run_id"],
                    "reason": str(exc), "full_state_omitted": True, "fallback_allowed": False}
