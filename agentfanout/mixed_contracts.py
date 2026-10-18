"""Additive hosted mixed-run contracts, independent of native V2 packets.

These envelopes pin routing and readers. They do not manufacture launch
receipts, coordinator ownership, release authority or provider conformance.
Legacy state/guard/ledger payloads retain their existing meanings and hashes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import time
from typing import Any

from .egress import validate_policy


class MixedContractError(ValueError):
    """Unsupported contracts or changed authority cannot authorize work."""


PROVIDER_MODES = {
    "GrokCLI": "grok_cli_subscription_acp", "Local": "coordinator_local",
    "Grok": "grok_direct_https", "Codex": "codex_cli_no_tool",
}
CONTROLS = {"grok_cli_subscription", "coordinator_local", "mixed_provider_runs"}
_HASH = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


def canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise MixedContractError("contract_not_canonical_json") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: Any, field: str, limit: int = 512) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MixedContractError("invalid_contract_field:" + field)


def _hash(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MixedContractError("invalid_contract_digest:" + field)


def _id(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MixedContractError("invalid_contract_id:" + field)


def validate_runtime(runtime: Any) -> None:
    keys = {"source_commit", "release_id", "manifest_sha256", "entrypoint_sha256", "contracts_sha256"}
    if not isinstance(runtime, dict) or set(runtime) != keys:
        raise MixedContractError("runtime_identity_incomplete")
    if not isinstance(runtime["source_commit"], str) or not re.fullmatch(r"[a-f0-9]{40,64}", runtime["source_commit"]):
        raise MixedContractError("invalid_source_commit")
    _text(runtime["release_id"], "release_id", 128)
    for field in keys - {"source_commit", "release_id"}:
        _hash(runtime[field], field)


def validate_resource(resource: Any, role: str) -> None:
    keys = {"reservation_id", "domain", "lease_id", "serving_epoch", "fencing_token", "readiness", "ownership", "cancellation", "expires_at", "purpose"}
    if not isinstance(resource, dict) or set(resource) != keys:
        raise MixedContractError("resource_binding_incomplete")
    for field in ("reservation_id", "domain", "lease_id", "serving_epoch", "fencing_token"):
        _text(resource[field], field, 256)
    if resource["purpose"] != role:
        raise MixedContractError("resource_role_mismatch")
    if resource["readiness"] not in ("ready", "waiting", "unavailable", "uncertain") or resource["ownership"] not in ("certain", "uncertain") or resource["cancellation"] not in ("none", "confirmed", "uncertain"):
        raise MixedContractError("invalid_resource_state")
    if isinstance(resource["expires_at"], bool) or not isinstance(resource["expires_at"], (int, float)) or not math.isfinite(resource["expires_at"]):
        raise MixedContractError("invalid_resource_expiry")


def validate_binding(binding: Any, role: str) -> None:
    keys = {"provider", "mode", "model_selector", "resolved_model", "reasoning_effort", "identity_sha256", "resource", "reservation_cost_usd", "destination_id"}
    if not isinstance(binding, dict) or set(binding) != keys:
        raise MixedContractError("execution_binding_incomplete")
    provider = binding["provider"]
    if not isinstance(provider, str) or PROVIDER_MODES.get(provider) != binding["mode"]:
        raise MixedContractError("provider_mode_mismatch")
    for field in ("model_selector", "resolved_model", "reasoning_effort"):
        _text(binding[field], field)
    _hash(binding["identity_sha256"], "identity_sha256")
    _id(binding["destination_id"], "destination_id")
    cost = binding["reservation_cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        raise MixedContractError("numeric_reservation_required")
    if provider == "Local":
        validate_resource(binding["resource"], role)
    elif binding["resource"] is not None:
        raise MixedContractError("nonlocal_resource_binding")


def _validate_assignment(assignment: Any) -> None:
    keys = {"assignment_id", "worker", "validator", "validation_required", "egress_policy"}
    if not isinstance(assignment, dict) or set(assignment) != keys:
        raise MixedContractError("assignment_binding_incomplete")
    _id(assignment["assignment_id"], "assignment_id")
    if type(assignment["validation_required"]) is not bool:
        raise MixedContractError("invalid_validation_requirement")
    validate_binding(assignment["worker"], "worker")
    if assignment["validation_required"] and assignment["validator"] is None:
        raise MixedContractError("required_validator_unbound")
    if assignment["validator"] is not None:
        validate_binding(assignment["validator"], "validator")
    policy = validate_policy(assignment["egress_policy"])
    for role in ("worker", "validator"):
        binding = assignment[role]
        if binding is None:
            continue
        destination = policy["destinations"].get(binding["destination_id"])
        if destination is None or destination["provider"] != binding["provider"]:
            raise MixedContractError("binding_destination_mismatch")
        category = "input" if role == "worker" else "validator_input"
        if binding["destination_id"] not in policy["allowed"][category]:
            raise MixedContractError("binding_egress_denied")


def _payload(contract: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in contract.items() if key != "contract_sha256"}


def validate_contract(contract: Any) -> dict[str, Any]:
    keys = {"schema", "run_id", "runtime", "assignments", "allowed_transitions", "feature_controls", "contract_sha256"}
    if not isinstance(contract, dict) or set(contract) != keys or contract.get("schema") not in {"MixedRunContractV1", "MixedRunContractV2"}:
        raise MixedContractError("unsupported_mixed_contract")
    _id(contract["run_id"], "run_id")
    validate_runtime(contract["runtime"])
    controls = contract["feature_controls"]
    if not isinstance(controls, dict) or set(controls) != CONTROLS or any(type(value) is not bool for value in controls.values()):
        raise MixedContractError("invalid_feature_controls")
    assignments = contract["assignments"]
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 1024:
        raise MixedContractError("invalid_assignments")
    ids = set()
    for assignment in assignments:
        _validate_assignment(assignment)
        if assignment["assignment_id"] in ids:
            raise MixedContractError("duplicate_assignment")
        ids.add(assignment["assignment_id"])
    transitions = contract["allowed_transitions"]
    if not isinstance(transitions, list) or len(transitions) > 4096:
        raise MixedContractError("invalid_transitions")
    seen = set()
    for transition in transitions:
        transition_keys = {"assignment_id", "role", "from_binding_sha256", "to_binding"}
        if contract["schema"] == "MixedRunContractV2":
            transition_keys |= {"reason", "max_count"}
        if not isinstance(transition, dict) or set(transition) != transition_keys or transition["assignment_id"] not in ids or transition["role"] not in ("worker", "validator"):
            raise MixedContractError("invalid_transition")
        if contract["schema"] == "MixedRunContractV2":
            _id(transition["reason"], "transition_reason")
            if type(transition["max_count"]) is not int or not 1 <= transition["max_count"] <= 16:
                raise MixedContractError("invalid_transition_limit")
        _hash(transition["from_binding_sha256"], "from_binding_sha256")
        validate_binding(transition["to_binding"], transition["role"])
        assignment = next(item for item in assignments if item["assignment_id"] == transition["assignment_id"])
        # The existing worker planner reserves the original task cap. A routing
        # change cannot silently change that numeric authority or advertise a
        # reservation which the current receipt writer cannot produce.
        if (contract["schema"] == "MixedRunContractV2" and transition["role"] == "worker"
                and transition["to_binding"]["reservation_cost_usd"] != assignment["worker"]["reservation_cost_usd"]):
            raise MixedContractError("transition_worker_reservation_change_unsupported")
        alternative = deepcopy(assignment)
        alternative[transition["role"]] = transition["to_binding"]
        _validate_assignment(alternative)
        digest = canonical_digest(transition)
        if digest in seen:
            raise MixedContractError("duplicate_transition")
        seen.add(digest)
    _hash(contract["contract_sha256"], "contract_sha256")
    if canonical_digest(_payload(contract)) != contract["contract_sha256"]:
        raise MixedContractError("mixed_contract_digest_mismatch")
    return contract


def freeze_contract(run_id: str, runtime: dict[str, Any], assignments: list[dict[str, Any]], *, allowed_transitions: list[dict[str, Any]] | None = None, feature_controls: dict[str, bool] | None = None, schema: str = "MixedRunContractV1") -> dict[str, Any]:
    """Freeze host-selected bindings. Flags alone never establish readiness."""
    contract = deepcopy({"schema": schema, "run_id": run_id, "runtime": runtime, "assignments": assignments, "allowed_transitions": allowed_transitions or [], "feature_controls": feature_controls if feature_controls is not None else dict.fromkeys(sorted(CONTROLS), False)})
    contract["contract_sha256"] = canonical_digest(contract)
    return validate_contract(contract)


def assert_reader_compatible(contract: dict[str, Any], runtime: dict[str, Any], *, supported_schemas: set[str] | frozenset[str]) -> None:
    validate_contract(contract)
    validate_runtime(runtime)
    if contract["schema"] not in supported_schemas:
        raise MixedContractError("reader_schema_unsupported")
    if contract["runtime"] != runtime:
        raise MixedContractError("active_run_runtime_mismatch")


def binding_for(contract: dict[str, Any], assignment_id: str, role: str) -> dict[str, Any] | None:
    validate_contract(contract)
    if role not in ("worker", "validator"):
        raise MixedContractError("unknown_execution_role")
    for assignment in contract["assignments"]:
        if assignment["assignment_id"] == assignment_id:
            return deepcopy(assignment[role])
    raise MixedContractError("unknown_assignment")


def authorize_transition(contract: dict[str, Any], assignment_id: str, role: str, current_binding: dict[str, Any], next_binding: dict[str, Any]) -> None:
    validate_contract(contract)
    if role not in ("worker", "validator"):
        raise MixedContractError("unknown_execution_role")
    validate_binding(current_binding, role)
    validate_binding(next_binding, role)
    initial = binding_for(contract, assignment_id, role)
    # A transition starts from the initial or an explicitly reachable target.
    reachable = {canonical_digest(initial)} if initial is not None else set()
    changed = True
    while changed:
        changed = False
        for entry in contract["allowed_transitions"]:
            if entry["assignment_id"] == assignment_id and entry["role"] == role and entry["from_binding_sha256"] in reachable:
                digest = canonical_digest(entry["to_binding"])
                if digest not in reachable:
                    reachable.add(digest)
                    changed = True
    current_digest = canonical_digest(current_binding)
    if current_digest not in reachable or not any(all(entry.get(k) == v for k, v in {"assignment_id": assignment_id, "role": role, "from_binding_sha256": current_digest, "to_binding": next_binding}.items()) for entry in contract["allowed_transitions"]):
        raise MixedContractError("transition_not_authorized")


def assert_execution_ready(contract: dict[str, Any], assignment_id: str, *, now: float | None = None, admission_controls: dict[str, bool] | None = None, observed_resources: dict[str, dict[str, Any]] | None = None) -> None:
    """Check BOTH roles before worker dispatch. Waiting does not launch a wave.

    Current controls may disable new admission, while the frozen controls and
    runtime remain available for draining/reconciliation of existing work.
    Coordinator evidence must be refreshed independently by the caller.
    """
    validate_contract(contract)
    controls = contract["feature_controls"] if admission_controls is None else admission_controls
    if not isinstance(controls, dict) or set(controls) != CONTROLS or any(type(value) is not bool for value in controls.values()):
        raise MixedContractError("invalid_feature_controls")
    bindings = [binding_for(contract, assignment_id, role) for role in ("worker", "validator")]
    providers = {binding["provider"] for binding in bindings if binding is not None}
    for assignment in contract["assignments"]:
        providers.update(binding["provider"] for binding in (assignment["worker"], assignment["validator"]) if binding is not None)
    if len(providers) > 1 and not (controls["mixed_provider_runs"] and contract["feature_controls"]["mixed_provider_runs"]):
        raise MixedContractError("mixed_admission_disabled")
    observed_now = time.time() if now is None else now
    if isinstance(observed_now, bool) or not isinstance(observed_now, (int, float)) or not math.isfinite(observed_now):
        raise MixedContractError("invalid_observation_time")
    for role, binding in zip(("worker", "validator"), bindings):
        if binding is None:
            continue
        flag = {"GrokCLI": "grok_cli_subscription", "Local": "coordinator_local"}.get(binding["provider"])
        if flag and not (controls[flag] and contract["feature_controls"][flag]):
            raise MixedContractError("provider_admission_disabled")
        resource = binding["resource"]
        if resource is not None:
            if not isinstance(observed_resources, dict) or role not in observed_resources:
                raise MixedContractError("resource_not_independently_observed")
            validate_resource(observed_resources[role], role)
            if resource != observed_resources[role]:
                raise MixedContractError("resource_epoch_or_lease_drift")
            if resource["readiness"] != "ready" or resource["ownership"] != "certain":
                raise MixedContractError("resource_not_ready")
            if resource["cancellation"] != "none":
                raise MixedContractError("resource_cancelled_or_quarantined")
            if resource["expires_at"] <= observed_now:
                raise MixedContractError("resource_evidence_expired")
    if all(binding is not None for binding in bindings) and bindings[0]["provider"] == "Local" and bindings[1]["provider"] == "Local" and bindings[0]["resource"]["reservation_id"] == bindings[1]["resource"]["reservation_id"]:
        raise MixedContractError("validator_resource_not_reserved")


def assert_rollback_safe(contract: dict[str, Any], *, active_effects: list[dict[str, Any]], accounting_reconciled: bool) -> None:
    """Activation changes require positively reconciled effects and accounting.

    A blocked rollback retains/quarantines the pinned runtime; it never rewrites
    old ledgers or converts GrokCLI into direct Grok. Admission is disabled first.
    """
    validate_contract(contract)
    if type(accounting_reconciled) is not bool or not accounting_reconciled or not isinstance(active_effects, list):
        raise MixedContractError("rollback_accounting_unreconciled")
    for effect in active_effects:
        if not isinstance(effect, dict) or set(effect) != {"effect_id", "status", "ownership", "cancellation"}:
            raise MixedContractError("rollback_effect_evidence_incomplete")
        _text(effect["effect_id"], "effect_id", 256)
        if effect["status"] not in ("reconciled", "not_started") or effect["ownership"] != "certain" or effect["cancellation"] not in ("not_requested", "confirmed"):
            raise MixedContractError("rollback_effects_unresolved")
