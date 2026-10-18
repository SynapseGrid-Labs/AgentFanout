"""Persistent, deterministic multi-wave lifecycle for AgentFanout.

This module does not launch providers. It gives host runtimes a state machine that
makes repeated delegation explicit and prevents premature parent execution or
completion while eligible delegated work remains.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .project_binding import (
    ProjectBindingError, secure_private_directory, secure_private_file, validate_project_binding,
)

from .provider_registry import profile_for, resolve_model
from .telemetry import contains_sensitive_material

RUN_SCHEMA = "RunStateV2"
TASK_SCHEMA = "TaskUnitV1"
RESULT_SCHEMA = "WorkerResultV1"
NEXT_WAVE_SCHEMA = "NextWavePlanV1"

TASK_TERMINAL = {"completed", "blocked", "cancelled"}
RUN_TERMINAL = {"complete", "blocked"}
TASK_STATUSES = {"pending", "dispatched", "active", "completed", "blocked", "cancelled"}
RUN_STATUSES = {"planning", "dispatching", "collecting", "validating", "replanning", "blocked", "complete"}
ID_PATTERNS = {
    "run": re.compile(r"^run_[a-f0-9]{32}$"),
    "task": re.compile(r"^task_[a-f0-9]{32}$"),
    "launch": re.compile(r"^launch_[a-f0-9]{32}$"),
    "usage": re.compile(r"^usage_[a-f0-9]{32}$"),
    "worker": re.compile(r"^worker_[a-f0-9]{32}$"),
}
LEGAL_TRANSITIONS = {
    "planning": {"dispatching", "blocked", "complete"},
    "dispatching": {"collecting", "blocked"},
    "collecting": {"validating", "blocked"},
    "validating": {"replanning", "blocked"},
    "replanning": {"dispatching", "blocked", "complete"},
    "blocked": set(),
    "complete": set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _transition(state: dict[str, Any], target: str, reason: str) -> None:
    current = state["status"]
    if current == target:
        return
    if target not in LEGAL_TRANSITIONS.get(current, set()):
        raise ValueError(f"illegal_run_transition:{current}->{target}")
    state["status"] = target
    state["updated_at"] = _now()
    state["history"].append({"at": state["updated_at"], "from": current, "to": target, "reason": reason})


def _task_index(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["task_id"]: task for task in state["tasks"]}


def _default_budget(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    budget = {
        "max_wave_size": 4,
        "max_waves": 12,
        "max_total_workers": 48,
        "max_total_cost_usd": 10.0,
        "max_no_progress_waves": 2,
        "max_attempts_per_task": 3,
        # Codex CLI usage includes its runtime/system context, not only the
        # bounded task text.  The default is a hard total-input ceiling sized
        # above the observed ~16k startup floor; cached and uncached usage are
        # still recorded separately by the provider adapter.
        # The hosted packet includes bounded content plus a fixed validation
        # contract. 40k admits a typical ~30k documentation file without
        # weakening the adapter's conservative prelaunch byte/token ceiling.
        "max_worker_prompt_tokens": 40960,
        "max_worker_output_tokens": 1400,
        "max_parent_result_tokens_per_wave": 5000,
        "max_result_bytes": 65536,
    }
    if overrides:
        for key in budget:
            if key in overrides:
                budget[key] = overrides[key]
    integer_fields = (
        "max_wave_size", "max_waves", "max_total_workers", "max_no_progress_waves",
        "max_attempts_per_task", "max_worker_prompt_tokens", "max_worker_output_tokens",
        "max_parent_result_tokens_per_wave", "max_result_bytes",
    )
    for field in integer_fields:
        value = budget[field]
        if isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError(f"invalid_run_budget:{field}")
        try:
            budget[field] = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid_run_budget:{field}") from exc
    budget["max_wave_size"] = max(1, budget["max_wave_size"])
    budget["max_waves"] = max(1, budget["max_waves"])
    budget["max_total_workers"] = max(1, budget["max_total_workers"])
    budget["max_no_progress_waves"] = max(1, budget["max_no_progress_waves"])
    budget["max_attempts_per_task"] = max(1, budget["max_attempts_per_task"])
    budget["max_worker_prompt_tokens"] = max(1, budget["max_worker_prompt_tokens"])
    budget["max_worker_output_tokens"] = max(1, budget["max_worker_output_tokens"])
    budget["max_parent_result_tokens_per_wave"] = max(1, budget["max_parent_result_tokens_per_wave"])
    budget["max_result_bytes"] = max(1024, budget["max_result_bytes"])
    try:
        total_cost = float(budget["max_total_cost_usd"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_run_budget:max_total_cost_usd") from exc
    if not math.isfinite(total_cost):
        raise ValueError("invalid_run_budget:max_total_cost_usd")
    budget["max_total_cost_usd"] = max(0.0, total_cost)
    return budget


def _scope_for(unit: str, position: int, total: int) -> tuple[str, str]:
    known = {
        "knowledge_base_context": (
            "Review supplied project-history evidence",
            "Inspect only the project-history sources explicitly attached to this task and return relevant evidence references.",
        ),
        "live_host_state": (
            "Inspect bounded live-host evidence",
            "Run only the read-only host checks explicitly allowed by the task packet and report exact evidence.",
        ),
        "process_service_state": (
            "Inspect bounded process/service state",
            "Check the assigned process or service slice without mutation and return observed state and uncertainty.",
        ),
        "repo_code_path_trace": (
            "Trace an assigned repository path",
            "Inspect the assigned repository scope, trace relevant call/configuration paths, and cite exact files or symbols.",
        ),
        "independent_validation": (
            "Validate an independent claim set",
            "Check only the assigned claims against supplied evidence and report pass, concerns, or fail.",
        ),
        "security_release_review": (
            "Review an assigned security/release dimension",
            "Inspect the bounded security or public-release dimension and return evidence-backed material findings.",
        ),
    }
    if unit in known:
        return known[unit]
    if unit.startswith("file:"):
        path = unit.removeprefix("file:")
        return (
            f"Inspect bounded file {position} of {total}",
            f"Inspect only the assigned repository-relative file {path!r}; return compact evidence and do not inspect sibling inputs.",
        )
    return (
        f"Process user-supplied work item {position} of {total}",
        f"Process only item {position} from the host-bound input collection; do not infer or inspect other items.",
    )


def build_task_units(
    prompt: str,
    task_class: str,
    role: str,
    provider: str,
    selector: str,
    reasoning_effort: str,
    units: list[str],
    *,
    allowed_paths: list[str] | None = None,
    budget: dict[str, Any] | None = None,
    requested_alias: dict[str, str] | None = None,
    validator_required: bool | None = None,
) -> list[dict[str, Any]]:
    """Create real bounded packets for every requested work unit."""

    task_budget = _default_budget(budget)
    profile = "main"
    model_resolution: dict[str, Any]
    if provider == "main-session":
        model_resolution = {
            "provider": provider,
            "profile": profile,
            "selector": "main-session",
            "resolved_model": None,
            "ack_required": False,
        }
    else:
        profile = profile_for(provider, task_class, reasoning_effort, requested_alias)
        model_resolution = resolve_model(provider, profile, requested_alias)
        if selector:
            model_resolution["selector"] = selector

    packets: list[dict[str, Any]] = []
    total = len(units)
    for position, unit in enumerate(units, start=1):
        title, scope = _scope_for(unit, position, total)
        packet_model = deepcopy(model_resolution)
        packets.append(
            {
                "schema": TASK_SCHEMA,
                "task_id": _id("task"),
                "title": title,
                "scope": scope,
                "input_ref": unit if unit.startswith("file:") or unit in {
                    "knowledge_base_context",
                    "live_host_state",
                    "process_service_state",
                    "repo_code_path_trace",
                    "independent_validation",
                    "security_release_review",
                } else f"request.items[{position - 1}]",
                "objective_excerpt": prompt[:500],
                "task_class": task_class,
                "role": role,
                "status": "pending",
                "delegable": provider != "main-session",
                "dependencies": [],
                "parent_task_id": None,
                "allowed_paths": list(allowed_paths or []),
                "requires_path_binding": not bool(allowed_paths),
                "write_set": [],
                "requires_write_binding": role == "worker",
                "provider_model": packet_model,
                "launch_correlation_id": None,
                "reasoning_effort": reasoning_effort,
                "budgets": {
                    "max_prompt_tokens": task_budget["max_worker_prompt_tokens"],
                    "max_output_tokens": task_budget["max_worker_output_tokens"],
                    "max_cost_usd": round(task_budget["max_total_cost_usd"] / max(1, total), 4),
                },
                "validator_required": (
                    provider != "main-session"
                    if validator_required is None
                    else bool(validator_required and provider != "main-session")
                ),
                "recommended_worker_handle": None,
                "attempts": 0,
                "last_error": None,
                "created_at": _now(),
                "updated_at": _now(),
            }
        )
    return packets


def initialize_run(
    objective: str,
    tasks: list[dict[str, Any]],
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not objective.strip():
        raise ValueError("run_objective_required")
    if not tasks:
        raise ValueError("run_tasks_required")
    seen: set[str] = set()
    normalized_tasks = deepcopy(tasks)
    run_budget = _default_budget(budget)
    for task in normalized_tasks:
        if task.get("schema") != TASK_SCHEMA:
            raise ValueError("invalid_task_schema")
        task_id = task.get("task_id")
        if not task_id or task_id in seen:
            raise ValueError("duplicate_or_missing_task_id")
        seen.add(task_id)
        task["status"] = "pending"
        task["attempts"] = 0
        task["reserved_cost_usd"] = 0.0
        task["launch_correlation_id"] = None
    delegable_count = max(1, sum(1 for task in normalized_tasks if task.get("delegable")))
    initial_task_cap = round(run_budget["max_total_cost_usd"] / delegable_count, 6)
    for task in normalized_tasks:
        raw_task_cap = task.setdefault("budgets", {}).get("max_cost_usd", initial_task_cap)
        try:
            task_cap = float(raw_task_cap)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid_task_budget") from exc
        if not math.isfinite(task_cap) or task_cap < 0:
            raise ValueError("invalid_task_budget")
        task["budgets"]["max_cost_usd"] = min(task_cap, initial_task_cap)
    now = _now()
    return {
        "schema": RUN_SCHEMA,
        "run_id": _id("run"),
        "objective": objective,
        "objective_hash": hashlib.sha256(objective.encode("utf-8")).hexdigest(),
        "status": "planning",
        "wave_index": 0,
        "current_wave_id": None,
        "last_wave_task_ids": [],
        "tasks": normalized_tasks,
        "budget": run_budget,
        "usage": {
            "workers_launched": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "parent_result_tokens": 0,
            "cost_usd": 0.0,
            "reserved_cost_usd": 0.0,
        },
        "no_progress_waves": 0,
        "current_wave_parent_result_tokens": 0,
        "current_wave_completed_tasks": 0,
        "last_wave_completed_tasks": 0,
        "worker_handles": {},
        "launch_records": {},
        "result_ids": [],
        "host_validation_required": False,
        "validation_status": {
            task["task_id"]: "pending" if task.get("validator_required") else "not_required"
            for task in normalized_tasks
        },
        "block_reason": None,
        "created_at": now,
        "updated_at": now,
        "history": [],
        "revision": 0,
    }


def _nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and value >= 0
    )


def _positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def validate_run_state(state: dict[str, Any]) -> None:
    """Validate persisted control-plane state before any lifecycle mutation."""

    if not isinstance(state, dict) or state.get("schema") != RUN_SCHEMA:
        raise ValueError("invalid_run_schema")
    required_fields = {
        "schema", "run_id", "objective", "objective_hash", "status", "wave_index",
        "current_wave_id", "last_wave_task_ids", "tasks", "budget", "usage",
        "no_progress_waves", "current_wave_parent_result_tokens",
        "current_wave_completed_tasks", "last_wave_completed_tasks", "worker_handles",
        "launch_records", "result_ids", "host_validation_required", "validation_status", "block_reason", "created_at", "updated_at", "history", "revision",
    }
    if any(field not in state for field in required_fields):
        raise ValueError("run_state_required_fields_missing")
    if state.get("project_binding") is not None:
        try:
            validate_project_binding(state["project_binding"])
        except ProjectBindingError as exc:
            raise ValueError(str(exc)) from exc
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not ID_PATTERNS["run"].fullmatch(run_id):
        raise ValueError("invalid_run_id")
    objective = state.get("objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 100_000:
        raise ValueError("invalid_run_objective")
    expected_hash = hashlib.sha256(objective.encode("utf-8")).hexdigest()
    if state.get("objective_hash") != expected_hash:
        raise ValueError("run_objective_hash_mismatch")
    if state.get("status") not in RUN_STATUSES:
        raise ValueError("invalid_run_status")
    if not isinstance(state.get("host_validation_required"), bool):
        raise ValueError("invalid_host_validation_required")
    if state.get("block_reason") is not None and (not isinstance(state["block_reason"], str) or len(state["block_reason"]) > 2000):
        raise ValueError("invalid_run_block_reason")
    for field in ("created_at", "updated_at"):
        value = state.get(field)
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else (_ for _ in ()).throw(ValueError())
        except ValueError as exc:
            raise ValueError(f"invalid_run_timestamp:{field}") from exc
    for field in ("wave_index", "no_progress_waves", "current_wave_parent_result_tokens", "current_wave_completed_tasks", "last_wave_completed_tasks", "revision"):
        value = state.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid_run_integer:{field}")
    current_wave_id = state.get("current_wave_id")
    if current_wave_id is not None and (not isinstance(current_wave_id, str) or not re.fullmatch(r"^wave_[a-f0-9]{32}$", current_wave_id)):
        raise ValueError("invalid_current_wave_id")

    budget = state.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("invalid_run_budget")
    for field in (
        "max_wave_size", "max_waves", "max_total_workers", "max_no_progress_waves",
        "max_attempts_per_task", "max_worker_prompt_tokens", "max_worker_output_tokens",
        "max_parent_result_tokens_per_wave", "max_result_bytes",
    ):
        if not _positive_integer(budget.get(field)):
            raise ValueError(f"invalid_run_budget:{field}")
    if not _nonnegative_number(budget.get("max_total_cost_usd")):
        raise ValueError("invalid_run_budget:max_total_cost_usd")

    usage = state.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("invalid_run_usage")
    for field in ("workers_launched", "input_tokens", "output_tokens", "parent_result_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid_run_usage:{field}")
    for field in ("cost_usd", "reserved_cost_usd"):
        if not _nonnegative_number(usage.get(field)):
            raise ValueError(f"invalid_run_usage:{field}")
    if float(usage["cost_usd"]) + float(usage["reserved_cost_usd"]) > float(budget["max_total_cost_usd"]) + 1e-6:
        raise ValueError("run_cost_budget_invariant_exceeded")
    if int(usage["workers_launched"]) > int(budget["max_total_workers"]):
        raise ValueError("run_worker_budget_invariant_exceeded")
    if int(state["wave_index"]) > int(budget["max_waves"]):
        raise ValueError("run_wave_budget_invariant_exceeded")

    tasks = state.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > 10_000:
        raise ValueError("invalid_run_tasks")
    task_ids: set[str] = set()
    reserved_sum = 0.0
    for task in tasks:
        if not isinstance(task, dict) or task.get("schema") != TASK_SCHEMA:
            raise ValueError("invalid_task_schema")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not ID_PATTERNS["task"].fullmatch(task_id) or task_id in task_ids:
            raise ValueError("duplicate_or_invalid_task_id")
        task_ids.add(task_id)
        for field, maximum in (("title", 200), ("scope", 2000), ("input_ref", 1000), ("task_class", 100)):
            value = task.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"invalid_task_field:{field}")
        if task.get("input_sha256") is not None and (
            not isinstance(task["input_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", task["input_sha256"])
        ):
            raise ValueError("invalid_task_input_sha256")
        if task.get("input_bytes") is not None and (
            isinstance(task["input_bytes"], bool) or not isinstance(task["input_bytes"], int) or task["input_bytes"] < 0
        ):
            raise ValueError("invalid_task_input_bytes")
        if task.get("status") not in TASK_STATUSES:
            raise ValueError("invalid_task_status")
        if task.get("role") not in {"main", "explorer", "worker", "validator", "external-worker", "local-worker"}:
            raise ValueError("invalid_task_role")
        if not isinstance(task.get("delegable"), bool):
            raise ValueError("invalid_task_delegable")
        if not isinstance(task.get("requires_path_binding"), bool) or not isinstance(task.get("requires_write_binding"), bool):
            raise ValueError("invalid_task_binding_flags")
        for field in ("dependencies", "allowed_paths", "write_set"):
            values = task.get(field)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values) or len(values) != len(set(values)):
                raise ValueError(f"invalid_task_array:{field}")
        if any(not Path(path).is_absolute() for path in task["allowed_paths"] + task["write_set"]):
            raise ValueError("task_paths_must_be_absolute")
        for path in task["allowed_paths"]:
            _safe_binding_root(Path(path))
        if not task["allowed_paths"] and not task["requires_path_binding"]:
            raise ValueError("task_path_binding_invariant")
        model = task.get("provider_model")
        if not isinstance(model, dict):
            raise ValueError("invalid_task_provider_model")
        if not isinstance(model.get("provider"), str) or not model["provider"] or model.get("profile") not in {"main", "fast", "balanced", "frontier"}:
            raise ValueError("invalid_task_provider_model_identity")
        if model.get("selector") is not None and not isinstance(model["selector"], str):
            raise ValueError("invalid_task_provider_selector")
        if not isinstance(model.get("ack_required"), bool):
            raise ValueError("invalid_task_provider_ack_flag")
        for field in ("relative_cost", "relative_latency"):
            if model.get(field) is not None and not _nonnegative_number(model[field]):
                raise ValueError(f"invalid_task_provider_metric:{field}")
        correlation = task.get("launch_correlation_id")
        if correlation is not None and (not isinstance(correlation, str) or not ID_PATTERNS["launch"].fullmatch(correlation)):
            raise ValueError("invalid_task_launch_correlation")
        if task.get("reasoning_effort") not in {"low", "medium", "high", "xhigh", "ultra"}:
            raise ValueError("invalid_task_reasoning_effort")
        task_budget = task.get("budgets")
        if not isinstance(task_budget, dict) or not _positive_integer(task_budget.get("max_prompt_tokens")) or not _positive_integer(task_budget.get("max_output_tokens")) or not _nonnegative_number(task_budget.get("max_cost_usd")):
            raise ValueError("invalid_task_budget")
        attempts = task.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValueError("invalid_task_attempts")
        reserved = task.get("reserved_cost_usd", 0.0)
        if not _nonnegative_number(reserved) or float(reserved) > float(task_budget["max_cost_usd"]) + 1e-6:
            raise ValueError("invalid_task_reserved_cost")
        reserved_sum += float(reserved)
        recommended = task.get("recommended_worker_handle")
        for handle in (recommended,):
            if handle is not None and (not isinstance(handle, str) or not ID_PATTERNS["worker"].fullmatch(handle)):
                raise ValueError("invalid_worker_handle")

    index = _task_index(state)
    validation_status = state.get("validation_status")
    if not isinstance(validation_status, dict) or set(validation_status) != set(index):
        raise ValueError("invalid_validation_status")
    valid_validation_states = {"pending", "passed", "repair_required", "not_required"}
    for task_id, status in validation_status.items():
        task = index[task_id]
        if status not in valid_validation_states:
            raise ValueError("invalid_validation_status")
        if not task.get("validator_required") and status != "not_required":
            raise ValueError("invalid_validation_status")
    resolved_writes: list[tuple[Path, str]] = []
    for task in tasks:
        if task["task_id"] in task["dependencies"] or any(dep not in index for dep in task["dependencies"]):
            raise ValueError("invalid_task_dependency")
        parent_task_id = task.get("parent_task_id")
        if parent_task_id is not None and (parent_task_id not in index or parent_task_id == task["task_id"]):
            raise ValueError("invalid_parent_task_id")
        for path in task["write_set"]:
            resolved = Path(path).resolve(strict=False)
            if not any(_path_within(resolved, Path(root).resolve(strict=False)) for root in task["allowed_paths"]):
                raise ValueError("task_write_outside_allowed_paths")
            relative_parts = next(resolved.relative_to(Path(root).resolve(strict=False)).parts for root in task["allowed_paths"] if _path_within(resolved, Path(root).resolve(strict=False)))
            if any(part.lower() in {".env", ".git", ".ssh", ".aws", ".config", ".codex", ".claude", ".pai", "credentials", "secrets"} for part in relative_parts):
                raise ValueError("task_write_path_sensitive")
            if any(
                _paths_overlap(resolved, existing)
                and task["status"] != "completed"
                and existing_status != "completed"
                for existing, existing_status in resolved_writes
            ):
                raise ValueError("task_write_set_overlap")
            resolved_writes.append((resolved, task["status"]))
    if abs(reserved_sum - float(usage["reserved_cost_usd"])) > 1e-6:
        raise ValueError("reserved_cost_accounting_mismatch")

    result_ids = state.get("result_ids")
    if not isinstance(result_ids, list) or not all(isinstance(item, str) and item for item in result_ids) or len(result_ids) != len(set(result_ids)):
        raise ValueError("invalid_result_ids")
    last_wave_task_ids = state.get("last_wave_task_ids")
    if not isinstance(last_wave_task_ids, list) or len(last_wave_task_ids) != len(set(last_wave_task_ids)) or any(item not in index for item in last_wave_task_ids):
        raise ValueError("invalid_last_wave_task_ids")
    handles = state.get("worker_handles")
    if not isinstance(handles, dict):
        raise ValueError("invalid_worker_handles")
    for task_id, handle in handles.items():
        if task_id not in index or not isinstance(handle, str) or not ID_PATTERNS["worker"].fullmatch(handle):
            raise ValueError("invalid_worker_handle_mapping")
    records = state.get("launch_records")
    if not isinstance(records, dict):
        raise ValueError("invalid_launch_records")
    for correlation, record in records.items():
        if not isinstance(correlation, str) or not ID_PATTERNS["launch"].fullmatch(correlation) or not isinstance(record, dict):
            raise ValueError("invalid_launch_record")
        if record.get("correlation_id") != correlation or record.get("task_id") not in index:
            raise ValueError("launch_record_task_mismatch")
        # A host cancellation is durable control-plane evidence.  It must survive
        # save/load so an expired or explicitly cancelled worker cannot be
        # resurrected by a later planner invocation.
        if record.get("status") not in {"active", "consumed", "superseded", "cancelled"}:
            raise ValueError("invalid_launch_record_status")
        if not isinstance(record.get("worker_handle"), str) or not ID_PATTERNS["worker"].fullmatch(record["worker_handle"]):
            raise ValueError("invalid_launch_record_handle")
        if not isinstance(record.get("provider_model"), dict) or not isinstance(record.get("budgets"), dict):
            raise ValueError("invalid_launch_record_payload")
        record_budgets = record["budgets"]
        if (
            not _positive_integer(record_budgets.get("max_prompt_tokens"))
            or not _positive_integer(record_budgets.get("max_output_tokens"))
            or not _nonnegative_number(record_budgets.get("max_cost_usd"))
            or not _nonnegative_number(record.get("reserved_cost_usd"))
            or float(record["reserved_cost_usd"]) > float(record_budgets["max_cost_usd"]) + 1e-6
        ):
            raise ValueError("invalid_launch_record_budget")
        provider_usage = record.get("provider_usage")
        if provider_usage is not None:
            if not isinstance(provider_usage, dict) or set(provider_usage) != {"input_tokens", "output_tokens", "cost_usd"}:
                raise ValueError("invalid_launch_record_provider_usage")
            if any(
                isinstance(provider_usage.get(field), bool)
                or not isinstance(provider_usage.get(field), int)
                or provider_usage[field] < 0
                for field in ("input_tokens", "output_tokens")
            ) or not _nonnegative_number(provider_usage.get("cost_usd")):
                raise ValueError("invalid_launch_record_provider_usage")
            if (
                provider_usage["input_tokens"] > record_budgets["max_prompt_tokens"]
                or provider_usage["output_tokens"] > record_budgets["max_output_tokens"]
                or float(provider_usage["cost_usd"]) > float(record["reserved_cost_usd"]) + 1e-6
            ):
                raise ValueError("invalid_launch_record_provider_usage")
        if record.get("status") == "active" and record.get("task_id") in handles and handles[record["task_id"]] != record["worker_handle"]:
            raise ValueError("launch_record_handle_mismatch")
    for task in tasks:
        correlation = task.get("launch_correlation_id")
        if task["status"] in {"dispatched", "active"}:
            if not correlation or records.get(correlation, {}).get("status") != "active" or records[correlation].get("task_id") != task["task_id"]:
                raise ValueError("active_task_launch_record_required")
    history = state.get("history")
    if not isinstance(history, list) or not all(
        isinstance(item, dict)
        and item.get("from") in RUN_STATUSES
        and item.get("to") in RUN_STATUSES
        and isinstance(item.get("reason"), str)
        and isinstance(item.get("at"), str)
        for item in history
    ):
        raise ValueError("invalid_run_history")


def _dependencies_complete(task: dict[str, Any], index: dict[str, dict[str, Any]]) -> bool:
    return all(index.get(dep, {}).get("status") == "completed" for dep in task.get("dependencies", []))


def _binding_reason(task: dict[str, Any]) -> str | None:
    if task.get("requires_path_binding") or not task.get("allowed_paths"):
        return "path"
    if task.get("requires_write_binding"):
        return "write"
    if task.get("role") == "worker" and not task.get("write_set"):
        return "write"
    return None


def _binding_block(state: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    task = tasks[0]
    binding = _binding_reason(task) or "unknown"
    reason = f"task_binding_required:{binding}:{task['task_id']}"
    state["block_reason"] = reason
    state["updated_at"] = _now()
    return {
        "schema": NEXT_WAVE_SCHEMA,
        "run_id": state["run_id"],
        "wave_id": None,
        "wave_index": state["wave_index"],
        "action": "blocked",
        "reason": reason,
        "assignments": [],
        "remaining_delegable_tasks": len(_remaining_delegable(state)),
        "budget_remaining": budget_remaining(state),
        "binding_task_ids": [item["task_id"] for item in tasks],
    }


def apply_task_bindings(state: dict[str, Any], bindings: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply host-authored path/write bindings without dispatching work."""

    validate_run_state(state)
    index = _task_index(state)
    seen_writes = {
        Path(path).resolve(strict=False)
        for task in state["tasks"]
        for path in task.get("write_set", [])
    }
    updated: list[str] = []
    for binding in bindings:
        if not isinstance(binding, dict) or binding.get("task_id") not in index:
            raise ValueError("binding_unknown_task")
        task = index[binding["task_id"]]
        allowed = binding.get("allowed_paths", task.get("allowed_paths", []))
        writes = binding.get("write_set", task.get("write_set", []))
        if not isinstance(allowed, list) or not allowed or not all(isinstance(path, str) and Path(path).is_absolute() for path in allowed):
            raise ValueError("binding_allowed_paths_invalid")
        allowed_roots = [_safe_binding_root(Path(path)) for path in allowed]
        if not isinstance(writes, list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in writes):
            raise ValueError("binding_write_set_invalid")
        normalized_writes: list[str] = []
        for path in writes:
            resolved = Path(path).resolve(strict=False)
            if not any(_path_within(resolved, root) for root in allowed_roots):
                raise ValueError("binding_write_outside_allowed_paths")
            if any(_paths_overlap(resolved, existing) for existing in seen_writes) and str(resolved) not in task.get("write_set", []):
                raise ValueError("binding_write_set_overlap")
            seen_writes.add(resolved)
            normalized_writes.append(str(resolved))
        if task.get("role") == "worker" and not normalized_writes:
            raise ValueError("binding_write_set_required")
        task["allowed_paths"] = [str(path) for path in allowed_roots]
        task["write_set"] = normalized_writes
        task["requires_path_binding"] = False
        task["requires_write_binding"] = False
        task["updated_at"] = _now()
        updated.append(task["task_id"])
    state["block_reason"] = None
    state["updated_at"] = _now()
    validate_run_state(state)
    return {"schema": "TaskBindingUpdateV1", "run_id": state["run_id"], "updated_task_ids": updated}


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _worker_task_packet(task: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded worker-visible packet without host control-plane data."""

    public_fields = {
        "schema", "task_id", "title", "scope", "input_ref", "input_sha256", "input_bytes", "objective_excerpt",
        "task_class", "role", "dependencies", "parent_task_id", "allowed_paths",
        "write_set", "reasoning_effort", "validator_required", "launch_correlation_id",
    }
    packet = {field: deepcopy(task[field]) for field in public_fields if field in task}
    packet["schema"] = "WorkerTaskPacketV1"
    return packet


def worker_task_packet(task: dict[str, Any]) -> dict[str, Any]:
    """Return the supported bounded worker view for an already-planned task."""

    return _worker_task_packet(task)


def _paths_overlap(left: Path, right: Path) -> bool:
    return _path_within(left, right) or _path_within(right, left)


def _safe_binding_root(path: Path) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("binding_allowed_path_missing") from exc
    from .project_binding import path_is_sensitive

    if not root.is_dir() or root == Path(root.anchor) or root == Path.home().resolve() or path_is_sensitive(root):
        raise ValueError("binding_allowed_path_unsafe")
    return root


def _remaining_delegable(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [task for task in state["tasks"] if task.get("delegable") and task.get("status") != "completed"]


def _parent_may_self_execute(state: dict[str, Any]) -> bool:
    """Return the narrow direct-lifecycle compatibility signal.

    Supported hosted runs must finish through host terminal authorization, so a
    run that has enabled host validation never grants this advisory permission.
    Direct compatibility callers receive ``True`` only after every lifecycle
    gate visible in run state is already resolved.
    """

    if state.get("host_validation_required"):
        return False
    if state.get("status") not in {"replanning", "complete"} or state.get("block_reason") is not None:
        return False
    if state.get("current_wave_id") is not None or _remaining_delegable(state):
        return False
    if any(task.get("status") != "completed" for task in state["tasks"]):
        return False
    if any(
        task.get("validator_required") and state["validation_status"].get(task["task_id"]) != "passed"
        for task in state["tasks"]
    ):
        return False
    if any(record.get("status") != "consumed" for record in state["launch_records"].values()):
        return False
    return True


def _block(state: dict[str, Any], reason: str) -> dict[str, Any]:
    if state["status"] not in RUN_TERMINAL:
        _transition(state, "blocked", reason)
    state["block_reason"] = reason
    return {
        "schema": NEXT_WAVE_SCHEMA,
        "run_id": state["run_id"],
        "wave_id": None,
        "wave_index": state["wave_index"],
        "action": "blocked",
        "reason": reason,
        "assignments": [],
        "remaining_delegable_tasks": len(_remaining_delegable(state)),
        "budget_remaining": budget_remaining(state),
    }


def budget_remaining(state: dict[str, Any]) -> dict[str, Any]:
    budget = state["budget"]
    usage = state["usage"]
    return {
        "waves": max(0, int(budget["max_waves"]) - int(state["wave_index"])),
        "workers": max(0, int(budget["max_total_workers"]) - int(usage["workers_launched"])),
        "cost_usd": round(max(0.0, float(budget["max_total_cost_usd"]) - float(usage["cost_usd"]) - float(usage.get("reserved_cost_usd", 0.0))), 6),
    }


def plan_next_wave(state: dict[str, Any]) -> dict[str, Any]:
    validate_run_state(state)
    if state["status"] == "complete":
        return {
            "schema": NEXT_WAVE_SCHEMA,
            "run_id": state["run_id"],
            "wave_id": None,
            "wave_index": state["wave_index"],
            "action": "complete",
            "reason": "already_complete",
            "assignments": [],
            "remaining_delegable_tasks": 0,
            "budget_remaining": budget_remaining(state),
        }
    if state["status"] == "blocked":
        return _block(state, state.get("block_reason") or "already_blocked")

    active = [task for task in state["tasks"] if task["status"] in {"dispatched", "active"}]
    if active:
        return {
            "schema": NEXT_WAVE_SCHEMA,
            "run_id": state["run_id"],
            "wave_id": state.get("current_wave_id"),
            "wave_index": state["wave_index"],
            "action": "wait",
            "reason": "active_worker_results_required",
            "assignments": [],
            "active_task_ids": [task["task_id"] for task in active],
            "remaining_delegable_tasks": len(_remaining_delegable(state)),
            "budget_remaining": budget_remaining(state),
        }

    index = _task_index(state)
    ready = [
        task
        for task in state["tasks"]
        if task["status"] == "pending"
        and task.get("delegable")
        and _dependencies_complete(task, index)
        and _binding_reason(task) is None
    ]
    if not ready:
        binding_tasks = [
            task for task in state["tasks"]
            if task["status"] == "pending" and task.get("delegable") and _binding_reason(task) is not None
        ]
        if binding_tasks:
            return _binding_block(state, binding_tasks)
        residual = _remaining_delegable(state)
        if residual:
            return _block(state, "delegable_residual_not_dispatchable")
        incomplete_main = [task for task in state["tasks"] if task["status"] != "completed"]
        if incomplete_main:
            return _block(state, "main_session_gated_action_required")
        if state.get("host_validation_required"):
            awaiting_validation = [
                task_id for task_id, status in state["validation_status"].items()
                if status != "passed" and index[task_id].get("validator_required")
            ]
            if awaiting_validation:
                return {
                    "schema": NEXT_WAVE_SCHEMA,
                    "run_id": state["run_id"],
                    "wave_id": None,
                    "wave_index": state["wave_index"],
                    "action": "wait",
                    "reason": "validator_results_required",
                    "assignments": [],
                    "validation_task_ids": awaiting_validation,
                    "remaining_delegable_tasks": 0,
                    "budget_remaining": budget_remaining(state),
                }
        mark_complete(state)
        return {
            "schema": NEXT_WAVE_SCHEMA,
            "run_id": state["run_id"],
            "wave_id": None,
            "wave_index": state["wave_index"],
            "action": "complete",
            "reason": "all_tasks_complete",
            "assignments": [],
            "remaining_delegable_tasks": 0,
            "budget_remaining": budget_remaining(state),
        }

    remaining_budget = budget_remaining(state)
    if remaining_budget["waves"] <= 0:
        return _block(state, "wave_budget_exhausted")
    if remaining_budget["workers"] <= 0:
        return _block(state, "worker_budget_exhausted")
    if remaining_budget["cost_usd"] <= 0:
        return _block(state, "cost_budget_exhausted")
    if state["no_progress_waves"] >= state["budget"]["max_no_progress_waves"]:
        return _block(state, "no_progress_budget_exhausted")

    capacity = min(
        int(state["budget"]["max_wave_size"]),
        int(remaining_budget["workers"]),
        len(ready),
    )
    selected = ready[:capacity]
    if state["status"] in {"planning", "replanning"}:
        _transition(state, "dispatching", "eligible_tasks_selected")
    wave_id = _id("wave")
    state["wave_index"] += 1
    state["current_wave_id"] = wave_id
    state["current_wave_parent_result_tokens"] = 0
    state["current_wave_completed_tasks"] = 0
    state["last_wave_task_ids"] = [task["task_id"] for task in selected]
    pending_delegable = [task for task in state["tasks"] if task["status"] == "pending" and task.get("delegable")]
    # Keep one equal-sized reserve tranche for validator/repair/residual waves.
    # Without this, providers that report tokens but no USD cost consume the
    # entire run reservation in wave one, making the required replan loop
    # impossible even though the host correctly refuses to claim measured cost.
    fair_cost_cap = round(remaining_budget["cost_usd"] / max(1, len(pending_delegable) * 2), 6)
    if fair_cost_cap <= 0:
        return _block(state, "cost_budget_too_small_for_dispatch")
    assignments: list[dict[str, Any]] = []
    for task in selected:
        task_cost_cap = min(float(task["budgets"].get("max_cost_usd", fair_cost_cap)), fair_cost_cap)
        if task_cost_cap <= 0:
            return _block(state, "task_cost_budget_exhausted")
        task["budgets"]["max_cost_usd"] = task_cost_cap
        task["reserved_cost_usd"] = task_cost_cap
        state["usage"]["reserved_cost_usd"] = round(state["usage"].get("reserved_cost_usd", 0.0) + task_cost_cap, 6)
        recommended_handle = task.get("recommended_worker_handle")
        known_handles = set(state.get("worker_handles", {}).values())
        reuse_handle = recommended_handle in known_handles
        worker_handle = recommended_handle if reuse_handle else _id("worker")
        prior_correlation = task.get("launch_correlation_id")
        if prior_correlation in state["launch_records"] and state["launch_records"][prior_correlation].get("status") == "active":
            state["launch_records"][prior_correlation]["status"] = "superseded"
        correlation = _id("launch")
        task["launch_correlation_id"] = correlation
        state["launch_records"][correlation] = {
            "correlation_id": correlation,
            "task_id": task["task_id"],
            "status": "active",
            "worker_handle": worker_handle,
            "provider_model": deepcopy(task["provider_model"]),
            "reasoning_effort": task["reasoning_effort"],
            "budgets": deepcopy(task["budgets"]),
            "reserved_cost_usd": task_cost_cap,
            "provider_usage": None,
            "created_at": _now(),
        }
        state["worker_handles"][task["task_id"]] = worker_handle
        task["status"] = "dispatched"
        task["attempts"] += 1
        task["updated_at"] = _now()
        assignments.append(
            {
                "task_id": task["task_id"],
                "task": _worker_task_packet(task),
                "worker_action": "follow_up" if reuse_handle else "spawn",
                "recommended_worker_handle": task.get("recommended_worker_handle"),
                "worker_handle": worker_handle,
                "launch_correlation_id": correlation,
                "host_launch_record_required": True,
            }
        )
    state["usage"]["workers_launched"] += len(selected)
    _transition(state, "collecting", "wave_dispatched")
    return {
        "schema": NEXT_WAVE_SCHEMA,
        "run_id": state["run_id"],
        "wave_id": wave_id,
        "wave_index": state["wave_index"],
        "action": "dispatch_wave",
        "reason": "eligible_delegable_work_remains",
        "assignments": assignments,
        "remaining_delegable_tasks": len(_remaining_delegable(state)),
        "budget_remaining": budget_remaining(state),
        "wave_reserved_cost_usd": round(sum(float(task["reserved_cost_usd"]) for task in selected), 6),
        "parent_policy": "dispatch_collect_validate_replan; do_not_self_execute_eligible_work",
    }


SENSITIVE_RESIDUAL_PATTERN = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|Authorization\s*:\s*Bearer|\bsk-[A-Za-z0-9_-]{20,}\b|\bghp_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@|(?:SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE_KEY|API_KEY|ACCESS_KEY)[A-Z0-9_]*\s*=\s*\S{8,})",
    re.IGNORECASE,
)


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("result_not_json_serializable") from exc


def _clean_residual_text(value: Any, maximum: int, field: str) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, f"residual_{field}_must_be_string"
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None, f"residual_{field}_required"
    if len(cleaned) > maximum:
        return None, f"residual_{field}_too_long"
    if SENSITIVE_RESIDUAL_PATTERN.search(cleaned):
        return None, f"residual_{field}_sensitive"
    return cleaned, None


def _normalized_residual(item: Any) -> tuple[dict[str, str] | None, list[str]]:
    source = item if isinstance(item, dict) else {"scope": item}
    errors: list[str] = []
    scope, error = _clean_residual_text(source.get("scope"), 2000, "scope")
    if error:
        errors.append(error)
    title_value = source.get("title", "Continue bounded residual work")
    title, error = _clean_residual_text(title_value, 200, "title")
    if error:
        errors.append(error)
    reference_value = source.get("input_ref", "result:remaining_work")
    reference, error = _clean_residual_text(reference_value, 1000, "input_ref")
    if error:
        errors.append(error)
    if errors:
        return None, errors
    return {"title": title or "", "scope": scope or "", "input_ref": reference or ""}, []


def _result_shape_errors(result: dict[str, Any], max_bytes: int) -> tuple[int, list[str]]:
    errors: list[str] = []
    try:
        raw_bytes = _serialized_size(result)
    except ValueError as exc:
        return max_bytes + 1, [str(exc)]
    if raw_bytes > max_bytes:
        errors.append("result_payload_bytes_exceeded")
    allowed_fields = {
        "schema", "result_id", "task_id", "status", "summary", "evidence",
        "changed_files", "commands", "exit_codes", "remaining_work", "launch_correlation_id",
    }
    if any(field not in allowed_fields for field in result):
        errors.append("result_unknown_fields")
    for field in allowed_fields:
        if field not in result:
            errors.append(f"result_field_required:{field}")
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary_required")
    elif len(summary) > 8000:
        errors.append("summary_too_long")
    array_rules = {
        "evidence": (128, 4096),
        "changed_files": (128, 1024),
        "commands": (64, 2048),
    }
    for field, (max_items, max_item_bytes) in array_rules.items():
        value = result.get(field)
        if not isinstance(value, list):
            errors.append(f"{field}_must_be_array")
            continue
        if len(value) > max_items:
            errors.append(f"{field}_too_many_items")
        for item in value:
            if field != "evidence" and not isinstance(item, str):
                errors.append(f"{field}_item_invalid")
                break
            if field == "evidence" and not isinstance(item, (str, dict)):
                errors.append("evidence_item_invalid")
                break
            if _serialized_size(item) > max_item_bytes:
                errors.append(f"{field}_item_too_large")
                break
    exit_codes = result.get("exit_codes")
    if not isinstance(exit_codes, list) or len(exit_codes) > 64 or any(isinstance(code, bool) or not isinstance(code, int) for code in exit_codes or []):
        errors.append("exit_codes_invalid")
    remaining = result.get("remaining_work")
    if not isinstance(remaining, list):
        errors.append("remaining_work_must_be_array")
    else:
        if len(remaining) > 32:
            errors.append("remaining_work_too_many_items")
        for item in remaining:
            _, residual_errors = _normalized_residual(item)
            errors.extend(residual_errors)
    result_id = result.get("result_id")
    if not isinstance(result_id, str) or not result_id or len(result_id) > 200:
        errors.append("result_id_invalid")
    task_id = result.get("task_id")
    if not isinstance(task_id, str) or not ID_PATTERNS["task"].fullmatch(task_id):
        errors.append("result_task_id_invalid")
    correlation = result.get("launch_correlation_id")
    if not isinstance(correlation, str) or not ID_PATTERNS["launch"].fullmatch(correlation):
        errors.append("launch_correlation_invalid")
    return raw_bytes, list(dict.fromkeys(errors))


def _host_usage(record: dict[str, Any], host_parent_tokens: int) -> dict[str, Any]:
    """Use only host-private provider data or a non-zero conservative estimate."""

    budgets = record["budgets"]
    raw = record.get("provider_usage")
    if isinstance(raw, dict):
        return {
            "input_tokens": int(raw["input_tokens"]),
            "output_tokens": int(raw["output_tokens"]),
            "parent_result_tokens": host_parent_tokens,
            "cost_usd": float(raw["cost_usd"]),
        }
    reserved = float(record.get("reserved_cost_usd", budgets["max_cost_usd"]))
    conservative_cost = min(reserved, max(0.01, (int(budgets["max_prompt_tokens"]) + int(budgets["max_output_tokens"])) / 1_000_000))
    return {
        "input_tokens": int(budgets["max_prompt_tokens"]),
        "output_tokens": int(budgets["max_output_tokens"]),
        "parent_result_tokens": host_parent_tokens,
        "cost_usd": conservative_cost,
    }


def record_provider_usage(state: dict[str, Any], correlation_id: str, usage: dict[str, Any]) -> None:
    """Host-only adapter hook for measured provider accounting; never call from worker results."""

    validate_run_state(state)
    record = state["launch_records"].get(correlation_id)
    if not record or record.get("status") != "active":
        raise ValueError("provider_usage_unknown_or_inactive_launch")
    if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "cost_usd"}:
        raise ValueError("provider_usage_shape_invalid")
    for field in ("input_tokens", "output_tokens"):
        if isinstance(usage[field], bool) or not isinstance(usage[field], int) or usage[field] < 0:
            raise ValueError("provider_usage_value_invalid")
    if not _nonnegative_number(usage["cost_usd"]):
        raise ValueError("provider_usage_value_invalid")
    budgets = record["budgets"]
    if usage["input_tokens"] > budgets["max_prompt_tokens"] or usage["output_tokens"] > budgets["max_output_tokens"] or float(usage["cost_usd"]) > float(record["reserved_cost_usd"]):
        raise ValueError("provider_usage_budget_exceeded")
    record["provider_usage"] = deepcopy(usage)


def _validate_result(
    result: dict[str, Any],
    task: dict[str, Any],
    record: dict[str, Any],
    normalized_usage: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not result.get("result_id"):
        errors.append("result_id_required")
    if result.get("schema") != RESULT_SCHEMA:
        errors.append("invalid_result_schema")
    if result.get("task_id") != task["task_id"]:
        errors.append("task_id_mismatch")
    if result.get("status") not in {"success", "partial", "failed", "blocked"}:
        errors.append("invalid_result_status")
    if not isinstance(result.get("summary"), str) or not result.get("summary", "").strip():
        errors.append("summary_required")
    if result.get("status") == "success" and not (result.get("evidence") or result.get("changed_files")):
        errors.append("success_requires_evidence_or_changed_files")
    if result.get("status") == "partial" and not result.get("remaining_work"):
        errors.append("partial_requires_remaining_work")
    if result.get("launch_correlation_id") != task.get("launch_correlation_id") or record.get("task_id") != task["task_id"] or record.get("status") != "active":
        errors.append("launch_correlation_mismatch")
    usage = normalized_usage or _host_usage(record, max(1, (_serialized_size(result) + 3) // 4))
    budgets = record["budgets"]
    if usage["input_tokens"] > int(budgets.get("max_prompt_tokens", 0) or 0):
        errors.append("worker_prompt_token_budget_exceeded")
    if usage["output_tokens"] > int(budgets.get("max_output_tokens", 0) or 0):
        errors.append("worker_output_token_budget_exceeded")
    if usage["cost_usd"] > float(budgets.get("max_cost_usd", 0.0) or 0.0):
        errors.append("worker_cost_budget_exceeded")
    return errors


def _residual_task(parent: dict[str, Any], item: Any, worker_handle: str | None) -> dict[str, Any]:
    normalized, errors = _normalized_residual(item)
    if errors or normalized is None:
        raise ValueError("invalid_residual_work:" + ";".join(errors))
    if normalized["input_ref"] == "result:remaining_work":
        normalized["input_ref"] = parent["input_ref"]
    residual = deepcopy(parent)
    residual["task_id"] = _id("task")
    residual["parent_task_id"] = parent["task_id"]
    residual.update(normalized)
    if normalized["input_ref"] != parent.get("input_ref"):
        residual.pop("input_sha256", None)
        residual.pop("input_bytes", None)
    residual["status"] = "pending"
    # A provider session is bound to the exact bounded input that created it.
    # Reusing that context for a different file would combine unrelated
    # authority and evidence while also making the worker's prior prompt an
    # ambient dependency.  Keep the handle only for a genuine same-input
    # follow-up; cross-input residuals must spawn a fresh worker.
    residual["recommended_worker_handle"] = (
        worker_handle
        if normalized["input_ref"] == parent.get("input_ref")
        else None
    )
    residual["launch_correlation_id"] = None
    residual["reserved_cost_usd"] = 0.0
    residual["attempts"] = 0
    residual["last_error"] = None
    residual["created_at"] = _now()
    residual["updated_at"] = residual["created_at"]
    return residual


def ingest_results(state: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    validate_run_state(state)
    if state["status"] != "collecting":
        raise ValueError(f"result_ingestion_requires_collecting:{state['status']}")
    if not isinstance(results, list) or not results or not all(isinstance(result, dict) for result in results):
        raise ValueError("worker_results_required")
    if len(results) > int(state["budget"]["max_wave_size"]):
        raise ValueError("result_batch_size_exceeded")
    # This is the last content boundary before a worker packet can influence
    # lifecycle state or be persisted by a host runner.  Never include the
    # rejected material in an exception, validation record, or ledger event.
    if any(contains_sensitive_material(result) for result in results):
        raise ValueError("worker_result_sensitive_material")

    index = _task_index(state)
    validation: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []
    seen_result_ids: set[str] = set()
    seen_task_ids: set[str] = set()
    for result in results:
        try:
            payload_bytes = _serialized_size(result)
        except ValueError as exc:
            validation.append({"result_id": None, "task_id": result.get("task_id"), "valid": False, "errors": [str(exc)]})
            continue
        reported_id = result.get("result_id")
        tracking_id = reported_id if isinstance(reported_id, str) and reported_id else f"invalid_{hashlib.sha256(json.dumps(result, sort_keys=True, default=str, allow_nan=False).encode('utf-8')).hexdigest()}"
        if tracking_id in state["result_ids"] or tracking_id in seen_result_ids:
            validation.append({"result_id": reported_id, "task_id": result.get("task_id"), "valid": False, "errors": ["duplicate_result_id"]})
            continue
        task_id = result.get("task_id")
        task = index.get(task_id) if isinstance(task_id, str) else None
        if not task or task["status"] not in {"dispatched", "active"}:
            validation.append({"result_id": reported_id, "task_id": task_id, "valid": False, "errors": ["unknown_or_inactive_task"]})
            continue
        if task_id in seen_task_ids:
            validation.append({"result_id": reported_id, "task_id": task_id, "valid": False, "errors": ["duplicate_task_result_in_batch"]})
            continue
        seen_result_ids.add(tracking_id)
        seen_task_ids.add(task_id)
        correlation = result.get("launch_correlation_id")
        record = state["launch_records"].get(correlation) if isinstance(correlation, str) else None
        if not record or record.get("status") != "active" or record.get("task_id") != task_id or task.get("launch_correlation_id") != correlation:
            validation.append({"result_id": reported_id, "task_id": task_id, "valid": False, "errors": ["launch_correlation_mismatch"]})
            continue
        _, shape_errors = _result_shape_errors(result, int(state["budget"]["max_result_bytes"]))
        host_parent_tokens = max(1, (payload_bytes + 3) // 4)
        usage = _host_usage(record, host_parent_tokens)
        errors = list(dict.fromkeys(shape_errors + _validate_result(result, task, record, usage)))
        prepared.append({
            "result": result,
            "result_id": tracking_id,
            "reported_result_id": reported_id,
            "task": task,
            "record": record,
            "usage": usage,
            "errors": errors,
        })
        validation.append({"result_id": reported_id, "task_id": task_id, "valid": not errors, "errors": errors})

    if not prepared:
        return {
            "schema": "ResultIngestionV1",
            "run_id": state["run_id"],
            "wave_index": state["wave_index"],
            "completed_this_batch": 0,
            "validation": validation,
            "residual_delegable_tasks": len(_remaining_delegable(state)),
            "parent_may_self_execute": False,
            "next_required_action": "collect_remaining_results",
        }

    batch_parent_tokens = sum(item["usage"]["parent_result_tokens"] for item in prepared)
    if state.get("current_wave_parent_result_tokens", 0) + batch_parent_tokens > int(state["budget"]["max_parent_result_tokens_per_wave"]):
        raise ValueError("parent_result_token_budget_exceeded")

    released_reservations = sum(float(item["task"].get("reserved_cost_usd", 0.0)) for item in prepared)
    ingested_cost = sum(float(item["usage"]["cost_usd"]) for item in prepared)
    projected_cost = (
        float(state["usage"]["cost_usd"])
        + float(state["usage"].get("reserved_cost_usd", 0.0))
        - released_reservations
        + ingested_cost
    )
    if projected_cost > float(state["budget"]["max_total_cost_usd"]) + 1e-6:
        raise ValueError("run_cost_budget_exceeded")

    _transition(state, "validating", "worker_results_received")
    completed = 0
    for item in prepared:
        result = item["result"]
        task = item["task"]
        record = item["record"]
        usage = item["usage"]
        errors = item["errors"]
        state["result_ids"].append(item["result_id"])
        reserved = float(task.get("reserved_cost_usd", 0.0))
        state["usage"]["reserved_cost_usd"] = round(max(0.0, float(state["usage"].get("reserved_cost_usd", 0.0)) - reserved), 6)
        task["reserved_cost_usd"] = 0.0
        state["usage"]["input_tokens"] += usage["input_tokens"]
        state["usage"]["output_tokens"] += usage["output_tokens"]
        state["usage"]["parent_result_tokens"] += usage["parent_result_tokens"]
        state["usage"]["cost_usd"] = round(
            state["usage"]["cost_usd"] + usage["cost_usd"],
            6,
        )
        worker_handle = record["worker_handle"]
        record["status"] = "consumed"
        if errors:
            task["last_error"] = ";".join(errors)
            task["status"] = "pending" if task["attempts"] < state["budget"]["max_attempts_per_task"] else "blocked"
            task["updated_at"] = _now()
            continue
        if result["status"] == "success":
            task["status"] = "completed"
            task["last_error"] = None
            completed += 1
        elif result["status"] == "partial":
            task["status"] = "completed"
            completed += 1
        elif task["attempts"] < state["budget"]["max_attempts_per_task"]:
            task["status"] = "pending"
            task["last_error"] = result["status"]
        else:
            task["status"] = "blocked"
            task["last_error"] = result["status"]
        task["updated_at"] = _now()
        if not errors and result["status"] in {"success", "partial"}:
            for item in result.get("remaining_work") or []:
                residual = _residual_task(task, item, worker_handle)
                state["tasks"].append(residual)
                state["validation_status"][residual["task_id"]] = "pending" if residual.get("validator_required") else "not_required"
        if not errors and result["status"] in {"success", "partial"}:
            state["validation_status"][task["task_id"]] = "pending" if task.get("validator_required") else "not_required"

    state["current_wave_parent_result_tokens"] = state.get("current_wave_parent_result_tokens", 0) + batch_parent_tokens
    state["current_wave_completed_tasks"] = state.get("current_wave_completed_tasks", 0) + completed
    still_active = [task for task in state["tasks"] if task["status"] in {"dispatched", "active"}]
    if still_active:
        _transition(state, "replanning", "partial_wave_results_validated")
        _transition(state, "dispatching", "waiting_tasks_preserved")
        _transition(state, "collecting", "awaiting_remaining_wave_results")
    else:
        wave_completed = state.get("current_wave_completed_tasks", 0)
        state["last_wave_completed_tasks"] = wave_completed
        state["no_progress_waves"] = 0 if wave_completed else state["no_progress_waves"] + 1
        _transition(state, "replanning", "wave_results_validated")
    state["current_wave_id"] = state["current_wave_id"] if still_active else None
    validate_run_state(state)
    return {
        "schema": "ResultIngestionV1",
        "run_id": state["run_id"],
        "wave_index": state["wave_index"],
        "completed_this_batch": completed,
        "validation": validation,
        "residual_delegable_tasks": len(_remaining_delegable(state)),
        "parent_may_self_execute": _parent_may_self_execute(state),
        "next_required_action": "collect_remaining_results" if still_active else "replan_and_dispatch",
    }


def mark_complete(state: dict[str, Any]) -> None:
    validate_run_state(state)
    residual = _remaining_delegable(state)
    if residual:
        raise ValueError("completion_illegal_delegable_residual_work")
    incomplete = [task for task in state["tasks"] if task["status"] != "completed"]
    if incomplete:
        raise ValueError("completion_illegal_incomplete_tasks")
    if state.get("host_validation_required") and any(
        task.get("validator_required") and state["validation_status"].get(task["task_id"]) != "passed"
        for task in state["tasks"]
    ):
        raise ValueError("completion_illegal_validation_pending")
    _transition(state, "complete", "all_tasks_terminal_and_no_delegable_residual")


def _read_run_state_json(path: str | Path) -> tuple[Path, dict[str, Any]]:
    try:
        state_path, descriptor = secure_private_file(path, max_bytes=16 * 1024 * 1024)
    except ProjectBindingError as exc:
        if str(exc) == "private_file_too_large":
            raise ValueError("run_state_file_too_large") from exc
        raise ValueError(str(exc)) from exc
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        try:
            state = json.loads(
                b"".join(chunks).decode("utf-8"),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("run_state_numeric_non_finite")),
            )
        except UnicodeDecodeError as exc:
            raise ValueError("run_state_not_utf8") from exc
    finally:
        os.close(descriptor)
    return state_path, state


def load_run_state(path: str | Path) -> dict[str, Any]:
    _state_path, state = _read_run_state_json(path)
    validate_run_state(state)
    state["_loaded_revision"] = state["revision"]
    return state


def save_run_state(path: str | Path, state: dict[str, Any], *, replace: bool = False) -> None:
    validate_run_state(state)
    raw_path = Path(path).expanduser()
    state_parent = secure_private_directory(raw_path.parent, create=True)
    state_path = state_parent / raw_path.name
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_fd: int | None = None
    try:
        _lock_path, lock_fd = secure_private_file(lock_path, create=True)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, ProjectBindingError) as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        raise ValueError("run_state_locked") from exc
    temp_name: str | None = None
    try:
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(lock_fd)
        existing: dict[str, Any] | None = None
        if state_path.exists():
            _existing_path, existing = _read_run_state_json(state_path)
            validate_run_state(existing)
        expected_revision = state.get("_loaded_revision")
        if existing is not None and not replace:
            if expected_revision is None:
                raise ValueError("run_state_cas_context_required")
            if existing["run_id"] != state["run_id"]:
                raise ValueError("run_state_run_id_conflict")
            if int(existing["revision"]) != int(expected_revision):
                raise ValueError("run_state_revision_conflict")
        if existing is None and expected_revision is not None and not replace:
            raise ValueError("run_state_missing_during_save")

        next_revision = (int(existing["revision"]) + 1) if existing is not None else (int(state["revision"]) + 1)
        serialized = deepcopy(state)
        serialized.pop("_loaded_revision", None)
        serialized["revision"] = next_revision
        validate_run_state(serialized)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=state_path.parent, delete=False) as handle:
            json.dump(serialized, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, state_path)
        temp_name = None
        directory_fd = os.open(state_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        state["revision"] = next_revision
        state["_loaded_revision"] = next_revision
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
