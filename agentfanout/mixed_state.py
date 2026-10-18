"""Versioned hosted state envelope with private atomic CAS persistence.

The embedded lifecycle state remains V2. Its old reader rejects this envelope;
no dual-writing or extracting V2 on disk may downgrade the authority contract.
"""
from __future__ import annotations

from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .lifecycle import _read_run_state_json, validate_run_state
from .mixed_contracts import MixedContractError, assert_reader_compatible, validate_contract
from .project_binding import ProjectBindingError, secure_private_directory, secure_private_file


SCHEMA = "HostedRunStateV3"


def assignment_for_task(contract: dict[str, Any], state: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Resolve only a present, acyclic residual ancestry to a frozen root."""
    validate_contract(contract)
    tasks = {task["task_id"]: task for task in state["tasks"]}
    assignments = {item["assignment_id"]: item for item in contract["assignments"]}
    seen = set()
    current = task_id
    while current not in seen:
        seen.add(current)
        task = tasks.get(current)
        if task is None:
            raise MixedContractError("residual_ancestor_missing")
        if current in assignments:
            if task.get("parent_task_id") is not None:
                raise MixedContractError("frozen_assignment_not_root")
            return deepcopy(assignments[current])
        parent = task.get("parent_task_id")
        if parent is None:
            raise MixedContractError("task_not_frozen_or_authorized_residual")
        current = parent
    raise MixedContractError("residual_ancestry_cycle")


def validate_hosted_state(envelope: Any, runtime: dict[str, Any]) -> dict[str, Any]:
    keys = {"schema", "contract", "legacy_state"}
    v4 = isinstance(envelope, dict) and envelope.get("schema") == "HostedRunStateV4"
    if v4:
        keys.add("transition_history")
    if not isinstance(envelope, dict) or set(envelope) != keys or envelope.get("schema") not in {SCHEMA, "HostedRunStateV4"}:
        raise MixedContractError("unsupported_hosted_run_state")
    contract, state = envelope["contract"], envelope["legacy_state"]
    assert_reader_compatible(contract, runtime, supported_schemas={"MixedRunContractV2"} if v4 else {"MixedRunContractV1"})
    frozen = contract
    if v4:
        from .mixed_transitions import effective_contract
        contract = effective_contract(contract, envelope["transition_history"])
    validate_run_state(state)
    if state["run_id"] != contract["run_id"]:
        raise MixedContractError("hosted_run_id_mismatch")
    tasks = {task["task_id"]: task for task in state["tasks"]}
    if not {item["assignment_id"] for item in contract["assignments"]}.issubset(tasks):
        raise MixedContractError("frozen_assignment_missing")
    for task in state["tasks"]:
        assignment = assignment_for_task(contract, state, task["task_id"])
        expected = assignment["worker"]
        actual = task["provider_model"]
        if any(actual.get(field) != expected[bound] for field, bound in (("provider", "provider"), ("selector", "model_selector"), ("resolved_model", "resolved_model"))) or task["reasoning_effort"] != expected["reasoning_effort"]:
            raise MixedContractError("task_worker_binding_drift")
        if task["validator_required"] != assignment["validation_required"]:
            raise MixedContractError("task_validator_policy_drift")
        if task["task_id"] != assignment["assignment_id"]:
            root = tasks[assignment["assignment_id"]]
            for field in ("allowed_paths", "write_set"):
                if not set(task.get(field, [])).issubset(root.get(field, [])):
                    raise MixedContractError("residual_authority_expansion")
    for record in state["launch_records"].values():
        assignment = assignment_for_task(contract, state, record["task_id"])
        expected = assignment["worker"]
        actual = record["provider_model"]
        candidates = [expected]
        if v4:
            candidates += [a["worker"] for a in frozen["assignments"] if a["assignment_id"] == assignment["assignment_id"]]
            candidates += [r["to_binding"] for r in envelope["transition_history"] if r["assignment_id"] == assignment["assignment_id"] and r["role"] == "worker"]
        if any(all(actual.get(field) == b[bound] for field, bound in (("provider", "provider"), ("selector", "model_selector"), ("resolved_model", "resolved_model"))) for b in candidates):
            continue
        if any(actual.get(field) != expected[bound] for field, bound in (("provider", "provider"), ("selector", "model_selector"), ("resolved_model", "resolved_model"))):
            raise MixedContractError("launch_record_worker_binding_drift")
    return envelope


def wrap_hosted_state(state: dict[str, Any], contract: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    envelope = deepcopy({"schema": "HostedRunStateV4" if contract["schema"] == "MixedRunContractV2" else SCHEMA, "contract": contract, "legacy_state": state})
    if contract["schema"] == "MixedRunContractV2":
        envelope["transition_history"] = []
    return validate_hosted_state(envelope, runtime)


def load_hosted_state(path: str | Path, runtime: dict[str, Any]) -> dict[str, Any]:
    _path, envelope = _read_run_state_json(path)
    validate_hosted_state(envelope, runtime)
    envelope["legacy_state"]["_loaded_revision"] = envelope["legacy_state"]["revision"]
    return envelope


def save_hosted_state(path: str | Path, envelope: dict[str, Any], runtime: dict[str, Any]) -> None:
    """Atomic private envelope save; no replace/downgrade bypass of CAS."""
    validate_hosted_state(envelope, runtime)
    state = envelope["legacy_state"]
    raw_path = Path(path).expanduser()
    parent = secure_private_directory(raw_path.parent, create=True)
    target = parent / raw_path.name
    lock_fd = None
    temp_name = None
    try:
        try:
            _lock, lock_fd = secure_private_file(target.with_name(target.name + ".lock"), create=True)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, ProjectBindingError) as exc:
            raise MixedContractError("hosted_state_locked") from exc
        existing = None
        if target.exists() or target.is_symlink():
            _path, existing = _read_run_state_json(target)
            validate_hosted_state(existing, runtime)
        expected = state.get("_loaded_revision")
        if existing is not None:
            prior = existing["legacy_state"]
            if existing["contract"] != envelope["contract"] or prior["run_id"] != state["run_id"]:
                raise MixedContractError("hosted_state_contract_conflict")
            old_history = existing.get("transition_history", [])
            new_history = envelope.get("transition_history", [])
            if new_history[:len(old_history)] != old_history:
                raise MixedContractError("transition_history_rewrite_denied")
            if expected is None or prior["revision"] != expected:
                raise MixedContractError("hosted_state_revision_conflict")
        elif expected is not None:
            raise MixedContractError("hosted_state_missing_during_save")
        revision = (existing["legacy_state"]["revision"] if existing else state["revision"]) + 1
        serialized = deepcopy(envelope)
        serialized["legacy_state"].pop("_loaded_revision", None)
        serialized["legacy_state"]["revision"] = revision
        validate_hosted_state(serialized, runtime)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=parent, delete=False) as handle:
            temp_name = handle.name
            json.dump(serialized, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = None
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        state["revision"] = revision
        state["_loaded_revision"] = revision
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
