"""Small host-neutral planner for native task-scoped worker waves."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class NativeWaveError(ValueError):
    """Invalid capability, packet, result, or lifecycle transition."""


@dataclass(frozen=True)
class CapabilityHandshake:
    provider: str
    spawn: bool = True
    wait: bool = True
    follow_up: bool = False
    cancel: bool = True
    result_callback: bool = True
    launch_acknowledgement: bool = True
    model_selection: bool = False
    reasoning_selection: bool = False
    enforcement: str = "advisory"

    def as_dict(self) -> dict[str, Any]:
        return {"schema": "NativeCapabilityHandshakeV1", **self.__dict__}

    def require_launch(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise NativeWaveError("native_wave_provider_invalid")
        flags = (self.spawn, self.wait, self.follow_up, self.cancel, self.result_callback,
                 self.launch_acknowledgement,
                 self.model_selection, self.reasoning_selection)
        if not all(isinstance(flag, bool) for flag in flags):
            raise NativeWaveError("native_wave_capability_flag_invalid")
        if not (self.spawn and self.wait and self.result_callback):
            raise NativeWaveError("native_wave_capability_missing_spawn_wait_or_callback")
        if self.enforcement not in {"advisory", "harness_enforced", "runner_enforced"}:
            raise NativeWaveError("native_wave_enforcement_invalid")


def _tokens(value: str) -> int:
    return max(1, (len(value.encode("utf-8")) + 3) // 4)


def _packet_tokens(packet: dict[str, Any], error: str) -> int:
    try:
        compact = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise NativeWaveError(error) from exc
    return _tokens(compact)


def validate_task_packet(packet: dict[str, Any]) -> None:
    required = {"task_id", "task", "scope", "limits", "forbidden_actions", "result_contract"}
    if set(packet) != required:
        raise NativeWaveError("native_task_packet_shape_invalid")
    if not isinstance(packet["task_id"], str) or not packet["task_id"].strip():
        raise NativeWaveError("native_task_packet_id_invalid")
    if not isinstance(packet["task"], str) or not packet["task"].strip():
        raise NativeWaveError("native_task_packet_task_invalid")
    if not isinstance(packet["limits"], dict):
        raise NativeWaveError("native_task_packet_limits_invalid")
    if not isinstance(packet["forbidden_actions"], list) or not all(
        isinstance(item, str) for item in packet["forbidden_actions"]
    ):
        raise NativeWaveError("native_task_packet_forbidden_actions_invalid")
    if not isinstance(packet["result_contract"], str) or not packet["result_contract"].strip():
        raise NativeWaveError("native_task_packet_result_contract_invalid")
    if _packet_tokens(packet, "native_task_packet_serialization_invalid") > 250:
        raise NativeWaveError("native_task_packet_over_budget")


def validate_result_packet(packet: dict[str, Any]) -> None:
    required = {"task_id", "status", "summary", "evidence", "uncertainty", "residual_work"}
    if not required.issubset(packet):
        raise NativeWaveError("native_result_packet_shape_invalid")
    if not isinstance(packet["task_id"], str) or not packet["task_id"].strip():
        raise NativeWaveError("native_result_task_id_invalid")
    if packet["status"] not in {"success", "partial", "failed", "blocked"}:
        raise NativeWaveError("native_result_status_invalid")
    if not isinstance(packet["summary"], str):
        raise NativeWaveError("native_result_summary_invalid")
    for key in ("evidence", "uncertainty", "residual_work"):
        if not isinstance(packet[key], list):
            raise NativeWaveError(f"native_result_{key}_invalid")
    if "contradictions" in packet and not isinstance(packet["contradictions"], list):
        raise NativeWaveError("native_result_contradictions_invalid")
    if "scope_changed" in packet and not isinstance(packet["scope_changed"], bool):
        raise NativeWaveError("native_result_scope_changed_invalid")
    if _packet_tokens(packet, "native_result_packet_serialization_invalid") > 300:
        raise NativeWaveError("native_result_packet_over_budget")


class NativeWavePlanner:
    """Plan repeated waves without persisting provider or conversation state."""

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        capability: CapabilityHandshake,
        max_workers: int = 4,
        max_waves: int = 8,
        max_attempts_per_task: int = 3,
        *,
        v2_mode: bool = False,
        run_id: str | None = None,
        feature_flags: dict[str, Any] | None = None,
        model_snapshot: dict[str, Any] | None = None,
    ):
        if (not tasks and not v2_mode) or max_workers < 1 or max_waves < 1 or max_attempts_per_task < 1:
            raise NativeWaveError("native_wave_tasks_or_worker_limit_invalid")
        capability.require_launch()
        self.capability = capability
        if not all(isinstance(task, dict) and "task_id" in task and "packet" in task for task in tasks):
            raise NativeWaveError("native_wave_task_invalid")
        if not all(isinstance(task["task_id"], str) and task["task_id"].strip() for task in tasks):
            raise NativeWaveError("native_wave_task_id_invalid")
        task_ids = [task["task_id"] for task in tasks]
        if len(set(task_ids)) != len(task_ids):
            raise NativeWaveError("native_wave_duplicate_task_id")
        self.tasks: dict[str, dict[str, Any]] = {}
        for task_id, task in zip(task_ids, tasks):
            packet = task["packet"]
            if not isinstance(packet, dict):
                raise NativeWaveError("native_task_packet_shape_invalid")
            validate_task_packet(packet)
            if packet["task_id"] != task_id:
                raise NativeWaveError("native_task_packet_id_mismatch")
            self.tasks[task_id] = {**task, "packet": dict(packet), "role": task.get("role", "worker")}
        self.max_workers = max_workers
        self.max_waves = max_waves
        self.max_attempts_per_task = max_attempts_per_task
        self.v2_mode = v2_mode
        self.run_id = run_id
        self.feature_flags = dict(feature_flags or {})
        self.model_snapshot = dict(model_snapshot or {})
        self.completed: set[str] = set()
        self.recovery_counts: dict[str, dict[str, int]] = {
            task_id: {"repair": 0, "escalation": 0, "rejection": 0} for task_id in task_ids
        }
        self.wave = 0
        self.active = True
        self.terminal_status = "active"
        self.blocked_reason: str | None = None
        self.pending_stop_reason: str | None = None
        self.assignments: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, int] = {task_id: 0 for task_id in task_ids}

    def start(self) -> list[dict[str, Any]]:
        return self._launch(list(self.tasks))

    def _launch(self, task_ids: list[str]) -> list[dict[str, Any]]:
        if not self.active:
            return []
        available_slots = self.max_workers - len(self.assignments)
        if available_slots <= 0:
            return []
        launchable = [task_id for task_id in task_ids if task_id not in self.assignments]
        if self.v2_mode:
            launchable = [task_id for task_id in launchable if set(self.tasks[task_id].get("depends_on", [])) <= self.completed]
        if not launchable:
            return []
        if self.wave >= self.max_waves:
            self._stop_when_idle("native_wave_limit_exceeded")
            return []
        if any(self.attempts.get(task_id, 0) >= self.max_attempts_per_task for task_id in launchable):
            self._stop_when_idle("native_task_attempt_limit_exceeded")
            return []
        proposed_wave = self.wave + 1
        if not self.v2_mode:
            self.wave = proposed_wave
        assignments: list[dict[str, Any]] = []
        for task_id in launchable:
            if len(assignments) >= available_slots:
                break
            task = self.tasks[task_id]
            if self.v2_mode:
                claimed = set(task.get("write_set", []))
                if any(claimed & set(self.tasks[active_id].get("write_set", [])) for active_id in self.assignments):
                    continue
            packet = task["packet"]
            validate_task_packet(packet)
            prior_worker = task.get("worker_id")
            worker_id = prior_worker if task.get("unchanged_scope") and self.capability.follow_up else f"worker-{proposed_wave}-{task_id}"
            proposed_attempt = self.attempts.get(task_id, 0) + 1
            if not self.v2_mode:
                self.attempts[task_id] = proposed_attempt
            assignment = {
                "wave": proposed_wave,
                "task_id": task_id,
                "worker_id": worker_id,
                "role": task.get("role", "worker"),
                "attempt": proposed_attempt,
                "launch_acknowledged": not self.capability.launch_acknowledgement,
                "receiver_id": None,
                "packet": packet,
            }
            if self.v2_mode:
                assignment.update({
                    "assignment_id": f"{self.run_id}:{proposed_wave}:{task_id}:{proposed_attempt}",
                    "model_selection": dict(task["model_selection"]),
                    "validation_required": bool(task.get("validation_required")),
                    "scope": task["scope"],
                    "actual_model": None,
                    "actual_effort": None,
                    "retry_after_ms": task.get("retry_after_ms", 0),
                })
            self.assignments[task_id] = assignment
            assignments.append(assignment)
        return assignments

    def acknowledge_launch(
        self, task_id: str, receiver_id: str,
        actual_model: str | None = None, actual_effort: str | None = None,
    ) -> None:
        if not self.active or task_id not in self.assignments:
            raise NativeWaveError("native_launch_task_not_active")
        if not isinstance(receiver_id, str) or not receiver_id.strip():
            raise NativeWaveError("native_launch_receiver_id_invalid")
        if any(
            assignment.get("receiver_id") == receiver_id and assigned_task_id != task_id
            for assigned_task_id, assignment in self.assignments.items()
        ):
            raise NativeWaveError("native_launch_receiver_id_duplicate")
        assignment = self.assignments[task_id]
        if assignment["launch_acknowledged"]:
            raise NativeWaveError("native_launch_acknowledgement_replay")
        if self.v2_mode:
            selection = assignment["model_selection"]
            if self.feature_flags.get("model") is True:
                if not actual_model or not actual_effort:
                    raise NativeWaveError("v2_actual_model_or_effort_missing")
                if actual_model != selection["requested_model"] or actual_effort != selection["requested_effort"]:
                    raise NativeWaveError("v2_model_ack_mismatch")
            assignment["actual_model"] = actual_model
            assignment["actual_effort"] = actual_effort
        assignment["launch_acknowledged"] = True
        assignment["receiver_id"] = receiver_id
        assignment["worker_id"] = receiver_id
        if self.v2_mode:
            self.wave = max(self.wave, assignment["wave"])
            self.attempts[task_id] = assignment["attempt"]

    def reject_launch(self, task_id: str, *, replan: bool = True) -> list[dict[str, Any]]:
        """Discard an unacknowledged V2 proposal without charging an attempt."""
        if not self.v2_mode or task_id not in self.assignments:
            raise NativeWaveError("v2_launch_rejection_invalid")
        if self.assignments[task_id]["launch_acknowledged"]:
            raise NativeWaveError("v2_acknowledged_launch_cannot_reject")
        del self.assignments[task_id]
        self.recovery_counts[task_id]["rejection"] += 1
        if sum(self.recovery_counts[task_id].values()) >= 2 or not replan:
            self._stop_when_idle("v2_launch_rejection_limit" if replan else "v2_master_disabled_after_drain")
            return []
        return self._launch(list(self.tasks))

    def ingest(
        self, results: list[dict[str, Any]],
        host_receipts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.active:
            return []
        if self.v2_mode:
            return self._ingest_v2(results, host_receipts)
        result_ids: list[str] = []
        for result in results:
            validate_result_packet(result)
            result_ids.append(result["task_id"])
        if len(set(result_ids)) != len(result_ids):
            raise NativeWaveError("native_result_batch_duplicate_task_id")
        structured_residual_ids: list[str] = []
        derived_packets: dict[str, dict[str, Any]] = {}
        for result, task_id in zip(results, result_ids):
            if task_id not in self.tasks:
                raise NativeWaveError("native_result_task_unknown")
            if task_id not in self.assignments:
                raise NativeWaveError("native_result_task_not_active")
            if not self.assignments[task_id]["launch_acknowledged"]:
                raise NativeWaveError("native_result_launch_unacknowledged")
            residual = result.get("residual_work")
            contradictions = result.get("contradictions") or []
            if contradictions and residual:
                raise NativeWaveError("native_contradiction_and_residual_ambiguous")
            if contradictions:
                packet = dict(self.tasks[task_id]["packet"])
                packet["task"] = f"Independently validate contradictions for: {packet['task']}"
                validate_task_packet(packet)
                derived_packets[task_id] = packet
            if residual and any(isinstance(item, dict) for item in residual):
                if not all(isinstance(item, dict) for item in residual):
                    raise NativeWaveError("native_residual_packet_mixed_types")
                for packet in residual:
                    validate_task_packet(packet)
                    residual_id = packet["task_id"]
                    if residual_id != task_id and residual_id in self.tasks:
                        raise NativeWaveError("native_residual_task_id_collision")
                    structured_residual_ids.append(residual_id)
            elif residual and not all(isinstance(item, str) and item.strip() for item in residual):
                raise NativeWaveError("native_residual_work_invalid")
            elif residual:
                packet = dict(self.tasks[task_id]["packet"])
                packet["task"] = "Residual work: " + "; ".join(residual)
                validate_task_packet(packet)
                derived_packets[task_id] = packet
        if len(set(structured_residual_ids)) != len(structured_residual_ids):
            raise NativeWaveError("native_residual_task_id_duplicate")
        for result, task_id in zip(results, result_ids):
            task = self.tasks[task_id]
            task["worker_id"] = self.assignments.pop(task_id)["worker_id"]
            task["unchanged_scope"] = not bool(result.get("scope_changed"))
            residual = result.get("residual_work")
            contradictions = result.get("contradictions") or []
            if contradictions:
                task.update({"packet": derived_packets[task_id], "role": "validator", "unchanged_scope": False})
            elif residual and all(isinstance(item, dict) for item in residual):
                del self.tasks[task_id]
                prior_worker = task.get("worker_id")
                unchanged_scope = task.get("unchanged_scope", False)
                for packet in residual:
                    residual_id = packet["task_id"]
                    self.tasks[residual_id] = {
                        "task_id": residual_id,
                        "packet": dict(packet),
                        "role": "worker",
                        "worker_id": prior_worker if residual_id == task_id else None,
                        "unchanged_scope": unchanged_scope if residual_id == task_id else False,
                    }
                    self.attempts.setdefault(residual_id, self.attempts.get(task_id, 0) if residual_id == task_id else 0)
            elif residual:
                task.update({"packet": derived_packets[task_id], "role": "worker"})
            elif result.get("status") == "success":
                del self.tasks[task_id]
            else:
                task["role"] = "worker"
                if result.get("status") in {"failed", "blocked"}:
                    task["unchanged_scope"] = False
        if not self.tasks:
            self.active = False
            self.terminal_status = "completed"
            return []
        return self._launch(list(self.tasks))

    def _ingest_v2(self, results: list[dict[str, Any]], host_receipts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Apply correlated results after validating the whole batch."""
        from .v2_policy import FAILURE_ALIASES, FAILURE_CLASSES, recovery_action, flag

        if not isinstance(results, list) or not results:
            raise NativeWaveError("v2_results_required")
        result_ids: list[str] = []
        receipts = {receipt.get("task_id"): receipt for receipt in (host_receipts or [])
                    if isinstance(receipt, dict)}
        if len(receipts) != len(host_receipts or []):
            raise NativeWaveError("v2_host_receipt_duplicate_or_invalid")
        for result in results:
            validate_result_packet(result)
            task_id = result["task_id"]
            result_ids.append(task_id)
            if task_id not in self.tasks or task_id not in self.assignments:
                raise NativeWaveError("v2_result_task_not_active")
            assignment = self.assignments[task_id]
            if not assignment["launch_acknowledged"]:
                raise NativeWaveError("v2_result_launch_unacknowledged")
            if (result.get("assignment_id") != assignment["assignment_id"]
                    or result.get("receiver_id") != assignment["receiver_id"]
                    or result.get("attempt") != assignment["attempt"]):
                raise NativeWaveError("v2_result_correlation_mismatch")
            if (self.feature_flags.get("model") is True
                    and (result.get("actual_model") != assignment["actual_model"]
                         or result.get("actual_effort") != assignment["actual_effort"])):
                raise NativeWaveError("v2_result_model_mismatch")
            if result["residual_work"] and not all(isinstance(item, str) and item.strip() for item in result["residual_work"]):
                raise NativeWaveError("v2_residual_invalid")
            if result["status"] == "success":
                receipt = receipts.get(task_id)
                if not isinstance(receipt, dict):
                    raise NativeWaveError("v2_host_receipt_missing")
                if (receipt.get("assignment_id") != assignment["assignment_id"]
                        or receipt.get("receiver_id") != assignment["receiver_id"]):
                    raise NativeWaveError("v2_host_receipt_correlation_mismatch")
                if receipt.get("acceptance_check_passed") is not True:
                    raise NativeWaveError("v2_acceptance_check_missing")
                if receipt.get("structural_passed") is not True:
                    raise NativeWaveError("v2_structural_validation_missing")
                dynamic_semantic = bool(result["uncertainty"] or result.get("contradictions")
                                        or not result["evidence"] or
                                        sum(self.recovery_counts[task_id].values()) > 0)
                if assignment["validation_required"] or dynamic_semantic:
                    if receipt.get("semantic_passed") is not True:
                        raise NativeWaveError("v2_semantic_validation_missing")
                    validator_id = receipt.get("validator_receiver_id")
                    if not isinstance(validator_id, str) or not validator_id.strip() or validator_id == assignment["receiver_id"]:
                        raise NativeWaveError("v2_independent_validator_required")
            elif FAILURE_ALIASES.get(result.get("failure_class"), result.get("failure_class")) not in FAILURE_CLASSES:
                raise NativeWaveError("v2_failure_class_required")
        if len(set(result_ids)) != len(result_ids):
            raise NativeWaveError("v2_duplicate_result_task")

        for result in results:
            task_id = result["task_id"]
            assignment = self.assignments.pop(task_id)
            task = self.tasks[task_id]
            task["worker_id"] = assignment["receiver_id"]
            task["unchanged_scope"] = result["status"] in {"success", "partial"}
            if result["residual_work"] or result.get("scope_changed"):
                self._stop_when_idle("v2_residual_new_plan_required")
                continue
            if result["status"] == "success":
                self.completed.add(task_id)
                del self.tasks[task_id]
                continue
            selection = task["model_selection"]
            recovery_enabled = flag(self.feature_flags, "recovery")
            if not recovery_enabled:
                task["unchanged_scope"] = False
                continue
            if sum(self.recovery_counts[task_id].values()) >= 2:
                self._stop_when_idle("v2_recovery_budget_exhausted")
                continue
            recovery = recovery_action(result["failure_class"], assignment["attempt"], selection["tier"], enabled_stage=True)
            action = recovery["action"]
            if action == "escalate_sol" and selection.get("selection_reason") == "explicit_user_model_pin":
                action = "stop"
            if action == "repair" and self.recovery_counts[task_id]["repair"] >= 1:
                action = "stop"
            if action == "repair":
                self.recovery_counts[task_id]["repair"] += 1
                task["unchanged_scope"] = self.capability.follow_up
                task["retry_after_ms"] = recovery["backoff_ms"]
            elif action == "escalate_sol" and self.feature_flags.get("model") is True:
                candidates = [model for model in self.model_snapshot.get("models", [])
                              if model.get("tier") == "sol" and model.get("available") is True
                              and "medium" in model.get("efforts", [])]
                if not candidates:
                    self._stop_when_idle("v2_sol_escalation_unavailable")
                    continue
                task["model_selection"] = {**selection,
                    "requested_model": candidates[0]["id"], "requested_effort": "medium",
                    "tier": "sol", "selection_reason": "quality_failure_luna_to_sol"}
                task["unchanged_scope"] = False
                task["retry_after_ms"] = 0
                self.recovery_counts[task_id]["escalation"] += 1
            else:
                self._stop_when_idle(f"v2_{recovery['failure_class']}_failure")
        if not self.active:
            return []
        if not self.tasks:
            self.active = False
            self.terminal_status = "completed"
            return []
        if self.pending_stop_reason:
            if not self.assignments:
                self._block(self.pending_stop_reason)
            return []
        next_assignments = self._launch(list(self.tasks))
        if not next_assignments and not self.assignments and self.tasks:
            self._block("v2_no_ready_tasks")
        return next_assignments

    def cancel(self) -> list[str]:
        receiver_ids = [assignment["receiver_id"] for assignment in self.assignments.values()
                        if assignment.get("receiver_id")]
        self.active = False
        self.terminal_status = "cancelled"
        self.assignments.clear()
        return receiver_ids

    def _block(self, reason: str) -> None:
        self.active = False
        self.terminal_status = "blocked"
        self.blocked_reason = reason
        self.pending_stop_reason = None
        self.assignments.clear()

    def _stop_when_idle(self, reason: str) -> None:
        if self.assignments:
            self.pending_stop_reason = reason
            return
        self._block(reason)

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.terminal_status,
            "blocked_reason": self.blocked_reason,
            "pending_stop_reason": self.pending_stop_reason,
            "wave": self.wave,
            "active_task_ids": sorted(self.assignments),
            "active_receiver_ids": {
                task_id: assignment.get("receiver_id")
                for task_id, assignment in sorted(self.assignments.items())
            },
            "pending_task_ids": sorted(task_id for task_id in self.tasks if task_id not in self.assignments),
            "attempts": dict(sorted(self.attempts.items())),
            "completed_task_ids": sorted(self.completed),
            "recovery_counts": self.recovery_counts if self.v2_mode else {},
        }

    def export_state(self) -> dict[str, Any]:
        """Replayable advisory state; the host remains authority for launches."""
        return {
            "schema": "NativeWaveStateV2" if self.v2_mode else "NativeWaveStateV1",
            "run_id": self.run_id, "capability": dict(self.capability.__dict__),
            "max_workers": self.max_workers, "max_waves": self.max_waves,
            "max_attempts_per_task": self.max_attempts_per_task,
            "feature_flags": self.feature_flags, "model_snapshot": self.model_snapshot,
            "tasks": list(self.tasks.values()), "assignments": self.assignments,
            "attempts": self.attempts, "completed": sorted(self.completed),
            "recovery_counts": self.recovery_counts,
            "wave": self.wave, "active": self.active,
            "terminal_status": self.terminal_status,
            "blocked_reason": self.blocked_reason,
            "pending_stop_reason": self.pending_stop_reason,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> NativeWavePlanner:
        if not isinstance(state, dict) or state.get("schema") != "NativeWaveStateV2":
            raise NativeWaveError("v2_state_schema_invalid")
        try:
            capability = CapabilityHandshake(**state["capability"])
            planner = cls(state["tasks"], capability,
                          max_workers=state["max_workers"],
                          max_waves=state["max_waves"],
                          max_attempts_per_task=state["max_attempts_per_task"],
                          v2_mode=True, run_id=state["run_id"],
                          feature_flags=state["feature_flags"],
                          model_snapshot=state["model_snapshot"])
            planner.assignments = dict(state["assignments"])
            planner.attempts = dict(state["attempts"])
            planner.completed = set(state["completed"])
            planner.recovery_counts = dict(state["recovery_counts"])
            planner.wave = state["wave"]
            planner.active = state["active"]
            planner.terminal_status = state["terminal_status"]
            planner.blocked_reason = state["blocked_reason"]
            planner.pending_stop_reason = state["pending_stop_reason"]
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeWaveError("v2_state_invalid") from exc
        return planner
