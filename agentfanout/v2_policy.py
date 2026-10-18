"""Opt-in, host-bound model and task policy for native AgentFanout waves.

The host supplies concrete tasks and a fresh capability snapshot.  This module
does not infer work units from prompt words or discover provider models.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from .native import CapabilityHandshake, NativeWaveError, validate_task_packet


REVISION = "NativePolicyV2.1"
FLAGS = ("assignment", "validation", "recovery", "model", "lifecycle")
FORBIDDEN = ["secrets", "private_account_side_effects", "git_state_mutation",
             "destructive_actions", "deploys", "final_user_synthesis"]
SAFE_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
FAILURE_ALIASES = {"transient": "transient_transport", "quality": "semantic_failure",
                   "capability": "model_unavailable", "unsafe": "policy_failure"}
FAILURE_CLASSES = {"rate_limit", "transient_transport", "model_unavailable",
                   "tool_failure", "malformed_result", "deterministic_verification_failure",
                   "semantic_failure", "worker_timeout", "policy_failure", "cancelled"}
CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|access[_-]?key|secret|token|password|credential|private[_-]?key)\s*[:=]\s*[^\s,'\"]{8,}",
    re.IGNORECASE,
)


def enabled(flags: Any) -> bool:
    """Master defaults off; caller opt-in alone cannot enable the experiment."""
    return (os.getenv("AGENT_FANOUT_V2_MASTER", "").lower() in {"1", "true", "yes"}
            and isinstance(flags, dict) and flags.get("master") is True)


def flag(flags: dict[str, Any], name: str) -> bool:
    if name not in FLAGS:
        raise NativeWaveError("v2_feature_flag_unknown")
    return enabled(flags) and flags.get(name) is True


def _nonempty(value: Any, code: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise NativeWaveError(code)
    return value.strip()


def _strings(value: Any, code: str, *, paths: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise NativeWaveError(code)
    result = [item.strip() for item in value]
    if len(result) != len(set(result)) or len(result) > 64 or any(len(item) > 240 for item in result):
        raise NativeWaveError(code)
    if paths and any(not SAFE_PATH.fullmatch(item) or item in {".", ".."} or ".." in item.split("/") for item in result):
        raise NativeWaveError(code)
    return result


def _snapshot(raw: Any, *, model_stage: bool) -> tuple[CapabilityHandshake, dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("provider") != "Codex":
        raise NativeWaveError("v2_codex_capability_required")
    for key in ("host", "surface", "build", "snapshot_id"):
        _nonempty(raw.get(key), f"v2_capability_{key}_required", 120)
    if not isinstance(raw.get("free_slots"), int) or isinstance(raw["free_slots"], bool) or raw["free_slots"] < 0:
        raise NativeWaveError("v2_capability_free_slots_invalid")
    if model_stage and (raw.get("model_selection") is not True or raw.get("reasoning_selection") is not True):
        raise NativeWaveError("v2_exact_model_selection_unavailable")
    if not model_stage:
        default_model = _nonempty(raw.get("default_model"), "v2_baseline_model_unknown", 100)
        if "astra" in default_model.lower():
            raise NativeWaveError("v2_baseline_astra_forbidden")
    capability = CapabilityHandshake(
        provider="Codex", spawn=raw.get("spawn"), wait=raw.get("wait"),
        follow_up=raw.get("follow_up", False), cancel=raw.get("cancel"),
        result_callback=raw.get("result_callback"),
        launch_acknowledgement=raw.get("launch_acknowledgement"),
        model_selection=raw.get("model_selection", False),
        reasoning_selection=raw.get("reasoning_selection", False),
        enforcement=raw.get("enforcement", "advisory"),
    )
    capability.require_launch()
    if not capability.launch_acknowledgement or not capability.cancel:
        raise NativeWaveError("v2_ack_and_cancel_required")
    models = raw.get("models", [])
    if not isinstance(models, list) or (model_stage and not models):
        raise NativeWaveError("v2_models_required")
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise NativeWaveError("v2_model_invalid")
        model_id = _nonempty(model.get("id"), "v2_model_id_invalid", 100)
        if model_id in seen or model.get("tier") not in {"luna", "sol", "astra", "other"}:
            raise NativeWaveError("v2_model_tier_or_duplicate_invalid")
        lowered_id = model_id.lower()
        if (("astra" in lowered_id) != (model["tier"] == "astra")
                or (model["tier"] == "astra" and model_id != "gpt-6-astra")
                or ("luna" in lowered_id and model["tier"] != "luna")
                or ("sol" in lowered_id and model["tier"] != "sol")):
            raise NativeWaveError("v2_model_identity_tier_mismatch")
        seen.add(model_id)
        efforts = _strings(model.get("efforts"), "v2_model_efforts_invalid")
        if not efforts or any(effort not in {"low", "medium", "high", "xhigh", "max", "ultra"} for effort in efforts):
            raise NativeWaveError("v2_model_efforts_invalid")
        if not isinstance(model.get("available"), bool):
            raise NativeWaveError("v2_model_availability_required")
    return capability, raw


def _ancestor(dependencies: dict[str, list[str]], ancestor: str, child: str) -> bool:
    return any(parent == ancestor or _ancestor(dependencies, ancestor, parent)
               for parent in dependencies[child])


def _validate_tasks(raw_tasks: Any) -> list[dict[str, Any]]:
    from .telemetry import contains_sensitive_material
    from scripts.route import classify, hard_gates

    if not isinstance(raw_tasks, list) or len(raw_tasks) > 32:
        raise NativeWaveError("v2_tasks_invalid")
    tasks: list[dict[str, Any]] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict) or not SAFE_ID.fullmatch(str(raw.get("task_id", ""))):
            raise NativeWaveError("v2_task_id_invalid")
        if raw.get("requested_model") == "gpt-6-astra":
            raise NativeWaveError("v2_astra_use_legacy_explicit_lane")
        if contains_sensitive_material(raw) or CREDENTIAL_ASSIGNMENT.search(str(raw.get("task", ""))):
            raise NativeWaveError("v2_sensitive_task_denied")
        gate_text = " ".join(str(value) for value in (
            raw.get("task", ""), raw.get("scope", {}).get("inputs", ""),
            raw.get("scope", {}).get("expected_output", ""),
            raw.get("scope", {}).get("check", ""),
        )) if isinstance(raw.get("scope"), dict) else str(raw.get("task", ""))
        gates = hard_gates(gate_text, classify(gate_text))
        if any(gates[key] for key in ("requires_mcp_private_tools", "requires_secrets",
                                      "requires_git_state_change", "requires_destructive_action")):
            raise NativeWaveError("v2_parent_only_action_denied")
        task_id = raw["task_id"]
        task = _nonempty(raw.get("task"), "v2_task_text_required", 500)
        scope = raw.get("scope")
        if not isinstance(scope, dict) or set(scope) != {"inputs", "expected_output", "owner", "write_set", "depends_on", "check"}:
            raise NativeWaveError("v2_scope_contract_incomplete")
        inputs = _strings(scope["inputs"], "v2_scope_inputs_invalid")
        if not inputs:
            raise NativeWaveError("v2_scope_inputs_required")
        _nonempty(scope["expected_output"], "v2_expected_output_required", 240)
        _nonempty(scope["owner"], "v2_owner_required", 100)
        _nonempty(scope["check"], "v2_acceptance_check_required", 240)
        write_set = _strings(scope["write_set"], "v2_write_set_invalid", paths=True)
        depends_on = _strings(scope["depends_on"], "v2_dependencies_invalid")
        task_class = raw.get("task_class")
        if task_class not in {"simple", "mechanical", "engineering", "hard", "security"}:
            raise NativeWaveError("v2_task_class_invalid")
        risk = raw.get("risk")
        if risk not in {"low", "medium", "high", "critical"}:
            raise NativeWaveError("v2_task_risk_invalid")
        if task_class in {"hard", "security"} and risk not in {"high", "critical"}:
            raise NativeWaveError("v2_task_risk_understated")
        packet = {"task_id": task_id, "task": task, "scope": scope,
                  "limits": {"max_recovery_actions": 2},
                  "forbidden_actions": FORBIDDEN,
                  "result_contract": "NativeResultPacketV2"}
        validate_task_packet(packet)
        tasks.append({"task_id": task_id, "packet": packet, "scope": scope,
                      "task_class": task_class, "risk": risk,
                      "requested_model": raw.get("requested_model"),
                      "requested_effort": raw.get("requested_effort"),
                      "authorization": raw.get("authorization")})
    ids = [task["task_id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise NativeWaveError("v2_duplicate_task_id")
    dependencies = {task["task_id"]: task["scope"]["depends_on"] for task in tasks}
    if any(parent not in dependencies or parent == task_id for task_id, parents in dependencies.items() for parent in parents):
        raise NativeWaveError("v2_dependency_unknown_or_self")
    def visit(task_id: str, stack: set[str], done: set[str]) -> None:
        if task_id in stack:
            raise NativeWaveError("v2_dependency_cycle")
        if task_id in done:
            return
        stack.add(task_id)
        for parent in dependencies[task_id]:
            visit(parent, stack, done)
        stack.remove(task_id)
        done.add(task_id)
    done: set[str] = set()
    for task_id in ids:
        visit(task_id, set(), done)
    for index, left in enumerate(tasks):
        for right in tasks[index + 1:]:
            if set(left["scope"]["write_set"]) & set(right["scope"]["write_set"]):
                if not (_ancestor(dependencies, left["task_id"], right["task_id"])
                        or _ancestor(dependencies, right["task_id"], left["task_id"])):
                    raise NativeWaveError("v2_overlapping_write_ownership")
    return tasks


def _select_model(task: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    requested = task.get("requested_model")
    requested_effort = task.get("requested_effort")
    if requested is not None:
        _nonempty(requested, "v2_requested_model_invalid", 100)
        if requested_effort is not None and requested_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
            raise NativeWaveError("v2_requested_effort_invalid")
    elif requested_effort is not None:
        raise NativeWaveError("v2_effort_pin_requires_model_pin")
    if requested is None:
        if task["task_class"] == "simple" and task["risk"] == "low":
            tier, effort = "luna", "low"
        elif task["task_class"] == "mechanical" and task["risk"] in {"low", "medium"}:
            tier, effort = "luna", "medium"
        else:
            tier, effort = "sol", "high" if task["risk"] in {"high", "critical"} else "medium"
        candidates = [model for model in snapshot["models"] if model["tier"] == tier and model["available"] and effort in model["efforts"]]
        if not candidates:
            raise NativeWaveError(f"v2_model_unavailable:{tier}:{effort}")
        model = candidates[0]
        reason = f"task_capability:{task['task_class']}:{task['risk']}"
    else:
        candidates = [model for model in snapshot["models"] if model["id"] == requested and model["available"]]
        if not candidates:
            raise NativeWaveError("v2_pinned_model_unavailable")
        model = candidates[0]
        if model["tier"] == "astra":
            raise NativeWaveError("v2_astra_use_legacy_explicit_lane")
        effort = requested_effort or ("max" if model["tier"] == "astra" else "medium")
        if effort not in model["efforts"]:
            raise NativeWaveError("v2_pinned_effort_unsupported")
        reason = "explicit_user_model_pin"
    return {"requested_model": model["id"], "requested_effort": effort,
            "tier": model["tier"], "selection_reason": reason,
            "capability_snapshot_id": snapshot["snapshot_id"]}


def prepare(request: dict[str, Any]) -> dict[str, Any]:
    """Validate a V2 plan request and return planner inputs, without launching."""
    if not isinstance(request, dict) or request.get("action") != "plan":
        raise NativeWaveError("v2_plan_request_invalid")
    flags = request.get("feature_flags")
    if not enabled(flags):
        return {"status": "legacy_route", "reason": "v2_master_disabled"}
    if not all(flag(flags, name) for name in ("assignment", "lifecycle")):
        return {"status": "legacy_route", "reason": "v2_stage_disabled"}
    _nonempty(request.get("run_id"), "v2_run_id_required", 120)
    model_stage = flag(flags, "model")
    capability, snapshot = _snapshot(request.get("capability"), model_stage=model_stage)
    tasks = _validate_tasks(request.get("tasks"))
    if not tasks:
        return {"status": "stay_local", "reason": "no_independent_ready_work"}
    if snapshot["free_slots"] == 0:
        return {"status": "stay_local", "reason": "no_host_free_slots"}
    ready = [task for task in tasks if not task["scope"]["depends_on"]]
    if len(ready) == 1 and len(tasks) == 1 and request.get("explicit_offload") is not True:
        evidence = request.get("benefit_evidence")
        if not isinstance(evidence, dict) or evidence.get("qualified") is not True or not isinstance(evidence.get("evidence_ref"), str) or not evidence["evidence_ref"].strip():
            return {"status": "stay_local", "reason": "single_worker_benefit_unproven"}
    for task in tasks:
        task["model_selection"] = (_select_model(task, snapshot) if model_stage else {
            "requested_model": snapshot["default_model"], "requested_effort": None,
            "tier": "host_default", "selection_reason": "baseline_host_model",
            "capability_snapshot_id": snapshot["snapshot_id"],
        })
        task["depends_on"] = task["scope"]["depends_on"]
        task["write_set"] = task["scope"]["write_set"]
        task["validation_required"] = (flag(flags, "validation") and
                                       (task["risk"] in {"high", "critical"} or
                                        task["task_class"] in {"hard", "security"} or
                                        request.get("independent_validation") is True))
    if any(task["model_selection"]["tier"] == "astra" for task in tasks):
        max_workers = 1
    else:
        max_workers = min(3, snapshot["free_slots"], len(ready))
    return {"status": "launch_required", "tasks": tasks, "capability": capability,
            "max_workers": max_workers, "feature_flags": flags,
            "capability_snapshot": snapshot, "policy_revision": REVISION}


def recovery_action(failure_class: str, attempt: int, tier: str, *, enabled_stage: bool) -> dict[str, Any]:
    """Bounded proposal only. The host implements backoff; no sleep or provider call."""
    failure_class = FAILURE_ALIASES.get(failure_class, failure_class)
    if failure_class not in FAILURE_CLASSES:
        raise NativeWaveError("v2_failure_class_invalid")
    action, backoff_ms = "stop", 0
    if enabled_stage and failure_class not in {"policy_failure", "cancelled"}:
        if failure_class == "model_unavailable" and tier == "luna" and attempt <= 2:
            action = "escalate_sol"
        elif attempt <= 2 and failure_class in {"rate_limit", "transient_transport", "worker_timeout"}:
            action, backoff_ms = "repair", min(8000, 1000 * 2 ** attempt)
        elif attempt == 2 and failure_class in {"semantic_failure", "deterministic_verification_failure"} and tier == "luna":
            action = "escalate_sol"
        elif attempt <= 2 and failure_class in {"tool_failure", "malformed_result", "semantic_failure", "deterministic_verification_failure"}:
            action = "repair"
    return {"action": action, "backoff_ms": backoff_ms, "failure_class": failure_class}


def process_native_wave(request: dict[str, Any], telemetry_file: str | None = None) -> dict[str, Any]:
    """One stateless CLI step; the host persists returned advisory state."""
    from .native import NativeWavePlanner

    if not isinstance(request, dict):
        raise NativeWaveError("v2_request_invalid")
    action = request.get("action")
    run_id = request.get("run_id")
    if action == "plan":
        plan = prepare(request)
        if plan["status"] != "launch_required":
            return {"schema": "NativeWaveResponseV2", "status": plan["status"],
                    "reason": plan["reason"], "assignments": [], "state": None}
        planner = NativeWavePlanner(
            plan["tasks"], plan["capability"],
            max_workers=plan["max_workers"], max_attempts_per_task=3,
            v2_mode=True, run_id=run_id,
            feature_flags=plan["feature_flags"],
            model_snapshot=plan["capability_snapshot"],
        )
        assignments = planner.start()
        event_type = "plan"
        event_role = "parent"
        event_assignments = assignments
        cancel_receivers: list[str] = []
    else:
        planner = NativeWavePlanner.from_state(request.get("state"))
        if run_id is not None and run_id != planner.run_id:
            raise NativeWaveError("v2_run_id_mismatch")
        run_id = planner.run_id
        master_active = enabled(planner.feature_flags)
        if not master_active and action in {"result", "reject"}:
            planner.pending_stop_reason = "v2_master_disabled_after_drain"
        cancel_receivers = []
        event_assignments = []
        if action == "ack":
            planner.acknowledge_launch(
                request.get("task_id"), request.get("receiver_id"),
                request.get("actual_model"), request.get("actual_effort"),
            )
            assignments = []
            event_type, event_role = "ack", "worker"
            event_assignments = [planner.assignments[request["task_id"]]]
        elif action == "reject":
            assignments = planner.reject_launch(request.get("task_id"), replan=master_active)
            event_type, event_role = "retry", "retry"
            event_assignments = assignments
        elif action == "result":
            assignments_before = {task_id: dict(assignment) for task_id, assignment in planner.assignments.items()}
            assignments = planner.ingest(request.get("results"), request.get("host_receipts"))
            event_type, event_role = "result", "worker"
            event_assignments = [assignments_before[result["task_id"]] for result in request["results"]]
        elif action == "cancel":
            cancel_receivers = planner.cancel()
            assignments = []
            event_type, event_role = "cancel", "parent"
        elif action == "snapshot":
            assignments = []
            event_type, event_role = "plan", "parent"
        else:
            raise NativeWaveError("v2_action_invalid")
    status = "launch_required" if assignments else planner.terminal_status
    response = {
        "schema": "NativeWaveResponseV2", "status": status,
        "assignments": assignments, "state": planner.export_state(),
        "snapshot": planner.snapshot(),
        "cancel_receiver_ids": cancel_receivers,
        "advisory_only": True,
    }
    if telemetry_file:
        try:
            from .v2_telemetry import record_event
            items = event_assignments or [None]
            for assignment in items:
                selection = assignment.get("model_selection", {}) if assignment else {}
                record_event(telemetry_file, {
                    "event_id": uuid.uuid4().hex, "run_id": run_id,
                    "event_type": event_type, "role": event_role,
                    "assignment_id": assignment.get("assignment_id") if assignment else None,
                    "worker_id": assignment.get("receiver_id") if assignment else None,
                    "requested_model": selection.get("requested_model"),
                    "requested_effort": selection.get("requested_effort"),
                    "actual_model": assignment.get("actual_model") if assignment else None,
                    "actual_effort": assignment.get("actual_effort") if assignment else None,
                    "policy_revision": REVISION,
                    "surface": planner.model_snapshot.get("surface"),
                    "host": planner.model_snapshot.get("host"),
                    "status": status,
                })
        except (ImportError, OSError, ValueError):
            pass
    return response
