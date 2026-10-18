"""Deterministic LangGraph evaluation wrapper for AgentFanout.

This is intentionally an experiment, not a provider runtime.  It uses the
existing AgentFanout lifecycle and HostDispatcher as the only authority for
task state, launch receipts, usage, effects, parent authorization, and terminal
authorization.  Worker results are local fixture data; no model, network,
secret, shell, or real tool is invoked.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, TypedDict

try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
except ModuleNotFoundError as exc:  # Deliberately fail the experiment, never the core suite.
    raise RuntimeError(
        "LangGraph evaluation dependency is missing. Create the isolated virtual "
        "environment described in experiments/langgraph-eval/README.md."
    ) from exc

from agentfanout.host_runtime import HostDispatcher, HostLedger, validator_policy_sha256
from agentfanout.lifecycle import (
    load_run_state,
    plan_next_wave,
    save_run_state,
    validate_run_state,
)


class SimulatedCrash(RuntimeError):
    """Expected fixture-only interruption after an unacknowledged effect."""


class FixtureState(TypedDict, total=False):
    af_state: dict[str, Any]
    state_path: str | None
    thread_id: str
    scripted_batches: list[list[dict[str, Any]]]
    current_plan: dict[str, Any] | None
    accepted_results: list[dict[str, Any]]
    validator_receipts: list[dict[str, Any]]
    validator_launch_receipts: list[dict[str, Any]]
    wave_history: list[dict[str, Any]]
    graph_status: str
    blocked_reason: str | None
    parent_tool_calls: int
    terminal_attempts: int
    effect_mode: str
    effect_calls: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


class FixtureRuntime:
    """A LangGraph shell whose state transitions are delegated to AgentFanout."""

    def __init__(self, ledger_path: str | Path):
        self.ledger = HostLedger(ledger_path)
        # The experiment deliberately exercises the hardened path.  It never
        # disables capability, approved-provider, lease, heartbeat, or
        # validator provenance checks merely to make a graph fixture pass.
        self.dispatcher = HostDispatcher(self.ledger, require_capability=True)
        self.checkpointer = InMemorySaver()
        self.effect_executions = 0
        self.graph = self._compile()

    def close(self) -> None:
        self.ledger.close()

    @staticmethod
    def _persist(state: FixtureState) -> None:
        path = state.get("state_path")
        if path:
            save_run_state(path, state["af_state"])

    @staticmethod
    def _result_for(task: dict[str, Any], descriptor: dict[str, Any], wave_index: int, position: int) -> dict[str, Any]:
        status = descriptor.get("status", "success")
        result = {
            "schema": "WorkerResultV1",
            "result_id": descriptor.get("result_id", f"fixture-wave-{wave_index}-task-{position}"),
            "task_id": task["task_id"],
            "status": status,
            "summary": descriptor.get("summary", f"fixture {status} result"),
            "evidence": deepcopy(descriptor.get("evidence", [f"fixture-wave-{wave_index}"])),
            "changed_files": deepcopy(descriptor.get("changed_files", [])),
            "commands": [],
            "exit_codes": [],
            "remaining_work": deepcopy(descriptor.get("remaining_work", [])),
            "launch_correlation_id": task["launch_correlation_id"],
        }
        return result

    @staticmethod
    def _usage(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            assignment["launch_correlation_id"]: {
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_usd": 0.001,
            }
            for assignment in plan["assignments"]
        }

    @staticmethod
    def _capability_payload(
        provider: str,
        task_class: str,
        reasoning_effort: str,
        tool_policy_sha256: str = "c" * 64,
    ) -> dict[str, Any]:
        observed = datetime.now(timezone.utc)
        return {
            "schema": "CapabilityRecordV1",
            "provider": provider,
            "exact_model_id": "fixture-model",
            "reasoning_effort": reasoning_effort,
            "task_class": task_class,
            "tool_policy_sha256": tool_policy_sha256,
            "evaluation_version": "langgraph-eval-fixture-v1",
            "evidence_class": "launch_readiness",
            "quality_confidence": 0.0,
            "sample_size": 0,
            "p50_latency_ms": 1,
            "p95_latency_ms": 1,
            "failure_rate": 0.0,
            "validator_failure_rate": 0.0,
            "context_window": 8192,
            "price_card_version": "fixture-v1",
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
            "expires_at": (observed + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "status": "approved",
        }

    def _capability_digest(
        self,
        state: FixtureState,
        provider: str,
        task_class: str,
        reasoning_effort: str,
        tool_policy_sha256: str = "c" * 64,
    ) -> str:
        for event in reversed(self.ledger.events(state["af_state"]["run_id"], "capability_record")):
            record = event["payload"]
            if (
                record.get("provider") == provider
                and record.get("exact_model_id") == "fixture-model"
                and record.get("task_class") == task_class
                and record.get("reasoning_effort") == reasoning_effort
                and record.get("tool_policy_sha256") == tool_policy_sha256
            ):
                return event["payload_sha256"]
        self.dispatcher.record_capability(
            state["af_state"],
            self._capability_payload(provider, task_class, reasoning_effort, tool_policy_sha256),
        )
        return self.ledger.events(state["af_state"]["run_id"], "capability_record")[-1]["payload_sha256"]

    def _prepare_hardened_launches(self, state: FixtureState) -> None:
        providers = sorted({task["provider_model"]["provider"] for task in state["af_state"]["tasks"] if task.get("delegable")})
        self.dispatcher.freeze_providers(state["af_state"], providers)
        for task in state["af_state"]["tasks"]:
            if task.get("delegable"):
                self._capability_digest(
                    state,
                    task["provider_model"]["provider"],
                    task["task_class"],
                    task["reasoning_effort"],
                )

    def _plan(self, state: FixtureState) -> dict[str, Any]:
        validate_run_state(state["af_state"])
        plan = plan_next_wave(state["af_state"])
        self._persist(state)
        history = [*state.get("wave_history", []), deepcopy(plan)]
        if plan["action"] == "blocked":
            return {
                "current_plan": plan,
                "wave_history": history,
                "graph_status": "blocked",
                "blocked_reason": plan["reason"],
            }
        if plan["action"] == "complete":
            return {"current_plan": plan, "wave_history": history, "graph_status": "completion_pending"}
        if plan["action"] != "dispatch_wave":
            raise AssertionError(f"fixture_requires_dispatch_or_terminal_plan:{plan['action']}")
        return {"current_plan": plan, "wave_history": history, "graph_status": "dispatched"}

    @staticmethod
    def _after_plan(state: FixtureState) -> str:
        action = (state.get("current_plan") or {}).get("action")
        if action == "dispatch_wave":
            return "dispatch"
        if action == "complete":
            return "done"
        return "blocked"

    def _dispatch(self, state: FixtureState) -> dict[str, Any]:
        plan = state["current_plan"]
        assert plan and plan["action"] == "dispatch_wave"
        self._prepare_hardened_launches(state)
        for assignment in plan["assignments"]:
            task = assignment["task"]
            planned = state["af_state"]["launch_records"][assignment["launch_correlation_id"]]["provider_model"]
            task_state = next(item for item in state["af_state"]["tasks"] if item["task_id"] == assignment["task_id"])
            started = datetime.now(timezone.utc)
            self.dispatcher.record_launch(state["af_state"], {
                "schema": "LaunchReceiptV2",
                "run_id": state["af_state"]["run_id"],
                "task_id": assignment["task_id"],
                "launch_correlation_id": assignment["launch_correlation_id"],
                "provider": planned["provider"],
                "provider_profile": planned["profile"],
                "model_selector": planned["selector"],
                "resolved_model": "fixture-model",
                "reasoning_effort": task["reasoning_effort"],
                "worker_handle": assignment["worker_handle"],
                "termination_token": "termination-token-" + assignment["launch_correlation_id"],
                "admission_basis": "explicit_provider_launch_readiness",
                "started_at": started.isoformat().replace("+00:00", "Z"),
                "lease_expires_at": (started + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "heartbeat_interval_seconds": 60,
                "tool_policy_sha256": "c" * 64,
                "capability_digest": self._capability_digest(
                    state, planned["provider"], task_state["task_class"], task["reasoning_effort"]
                ),
            })
            self.dispatcher.record_heartbeat(
                state["af_state"], assignment["task_id"], assignment["launch_correlation_id"], _now()
            )
        self._persist(state)
        return {"graph_status": "collecting"}

    def _collect(self, state: FixtureState) -> dict[str, Any]:
        plan = state["current_plan"]
        assert plan and plan["action"] == "dispatch_wave"
        wave_index = int(plan["wave_index"])
        batches = state.get("scripted_batches", [])
        if wave_index < 1 or wave_index > len(batches):
            raise AssertionError(f"missing_scripted_batch_for_wave:{wave_index}")
        descriptors = batches[wave_index - 1]
        if len(descriptors) != len(plan["assignments"]):
            raise AssertionError("scripted_batch_assignment_count_mismatch")
        results = [
            self._result_for(assignment["task"], descriptor, wave_index, position)
            for position, (assignment, descriptor) in enumerate(zip(plan["assignments"], descriptors), start=1)
        ]
        outcome = self.dispatcher.ingest_worker_batch(state["af_state"], results, self._usage(plan))
        if any(not item["valid"] for item in outcome["validation"]):
            raise AssertionError(f"fixture_result_rejected:{outcome['validation']}")
        self._persist(state)
        return {"accepted_results": results, "graph_status": "validating"}

    @staticmethod
    def _contradictory_claims(results: list[dict[str, Any]]) -> list[str]:
        claims: dict[str, Any] = {}
        conflicts: set[str] = set()
        for result in results:
            for item in result.get("evidence", []):
                if not isinstance(item, dict) or "claim_id" not in item or "value" not in item:
                    continue
                claim_id = str(item["claim_id"])
                value = item["value"]
                if claim_id in claims and claims[claim_id] != value:
                    conflicts.add(claim_id)
                claims[claim_id] = value
        return sorted(conflicts)

    def _validate(self, state: FixtureState) -> dict[str, Any]:
        results = state.get("accepted_results", [])
        conflicts = self._contradictory_claims(results)
        receipts = list(state.get("validator_receipts", []))
        launch_receipts = list(state.get("validator_launch_receipts", []))
        for result in results:
            if result["status"] not in {"success", "partial"}:
                continue
            task = next(item for item in state["af_state"]["tasks"] if item["task_id"] == result["task_id"])
            planned = task["provider_model"]
            result_sha = _canonical_sha256(result)
            policy_sha = validator_policy_sha256(task["task_id"], result_sha)
            correlation = "validator_launch_" + hashlib.sha256(
                f"{state['af_state']['run_id']}:{task['task_id']}:{result['result_id']}".encode("utf-8")
            ).hexdigest()
            started = datetime.now(timezone.utc)
            conservative_usage = {
                "input_tokens": 7,
                "output_tokens": 3,
                "cost_usd": 0.001,
                "usage_verified": True,
                "cost_verified": False,
                "cost_basis": "reservation_upper_bound",
                "measured_savings_claimed": False,
            }
            projected_cost = float(state["af_state"]["usage"]["cost_usd"]) + conservative_usage["cost_usd"]
            if projected_cost > float(state["af_state"]["budget"]["max_total_cost_usd"]):
                raise AssertionError("fixture_validator_conservative_cost_budget_exceeded")
            digest = self._capability_digest(
                state, planned["provider"], task["task_class"], task["reasoning_effort"], policy_sha
            )
            launch_receipt = {
                "schema": "ValidatorLaunchReceiptV1",
                "run_id": state["af_state"]["run_id"],
                "task_id": task["task_id"],
                "validator_launch_correlation_id": correlation,
                "validated_result_id": result["result_id"],
                "validated_result_sha256": result_sha,
                "validator_policy_sha256": policy_sha,
                "validator_provider": planned["provider"],
                "validator_resolved_model": "fixture-model",
                "validator_reasoning_effort": task["reasoning_effort"],
                "validator_capability_digest": digest,
                "started_at": started.isoformat().replace("+00:00", "Z"),
                "lease_expires_at": (started + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                "usage": conservative_usage,
            }
            self.dispatcher.record_validator_launch(state["af_state"], launch_receipt)
            state["af_state"]["usage"]["input_tokens"] += conservative_usage["input_tokens"]
            state["af_state"]["usage"]["output_tokens"] += conservative_usage["output_tokens"]
            state["af_state"]["usage"]["cost_usd"] = round(projected_cost, 6)
            receipt = {
                "schema": "ValidatorResultV1",
                "run_id": state["af_state"]["run_id"],
                "validator_id": "validator_fixture_independent",
                "task_id": result["task_id"],
                "validated_result_id": result["result_id"],
                "validated_result_sha256": result_sha,
                "validator_launch_correlation_id": correlation,
                "validator_policy_sha256": policy_sha,
                "validator_provider": planned["provider"],
                "validator_resolved_model": "fixture-model",
                "validator_reasoning_effort": task["reasoning_effort"],
                "validator_capability_digest": digest,
                "verdict": "fail" if conflicts else "pass",
                "evidence_refs": [f"fixture-validation:{result['result_id']}", *[f"conflict:{claim}" for claim in conflicts]],
                "material_findings": [f"contradictory_claim:{claim}" for claim in conflicts],
                "contradictions": [
                    {
                        "claim": claim,
                        "task_ids": [
                            peer["task_id"] for peer in results
                            if any(isinstance(item, dict) and item.get("claim_id") == claim
                                   for item in peer.get("evidence", []))
                        ],
                        "evidence_refs": [f"conflict:{claim}"],
                        "resolution_scope": "Resolve the conflicting synthetic fixture evidence",
                    }
                    for claim in conflicts
                ],
                "checked_at": _now(),
            }
            self.dispatcher.record_validator_result(state["af_state"], receipt)
            receipts.append(receipt)
            launch_receipts.append(launch_receipt)
        self._persist(state)
        if conflicts:
            return {
                "validator_receipts": receipts,
                "validator_launch_receipts": launch_receipts,
                "graph_status": "blocked_validation",
                "blocked_reason": "independent_validator_contradiction",
            }
        return {
            "validator_receipts": receipts,
            "validator_launch_receipts": launch_receipts,
            "graph_status": "validated",
        }

    @staticmethod
    def _after_validate(state: FixtureState) -> str:
        return "blocked" if state.get("graph_status") == "blocked_validation" else "effect_guard"

    def _effect_guard(self, state: FixtureState) -> dict[str, Any]:
        mode = state.get("effect_mode", "none")
        effect_task_id = (
            state.get("accepted_results", [{}])[0].get("task_id")
            if state.get("accepted_results")
            else next(task["task_id"] for task in state["af_state"]["tasks"] if task["status"] == "completed")
        )
        if mode == "crash_after_pending":
            # The fixture executor is the only place an effect could run. It records
            # intent before the simulated interruption, then never acknowledges it.
            self.effect_executions += 1
            self.dispatcher.record_effect(state["af_state"]["run_id"], effect_task_id, {
                "schema": "EffectRecordV1",
                "effect_id": "fixture_effect_1",
                "idempotency_key": "fixture-idempotency-key-0001",
                "intent_sha256": "a" * 64,
                "policy_sha256": "b" * 64,
                "state": "pending",
                "observed_at": _now(),
            })
            self._persist(state)
            raise SimulatedCrash("fixture_crash_after_effect_before_acknowledgement")
        if mode == "resume_unknown":
            previous = self.ledger.latest_effect(state["af_state"]["run_id"], effect_task_id, "fixture_effect_1")
            if not previous or previous.get("state") != "pending":
                raise AssertionError("pending_effect_required_for_resume")
            unknown = deepcopy(previous)
            unknown["state"] = "unknown_after_crash"
            unknown["observed_at"] = _now()
            self.dispatcher.record_effect(state["af_state"]["run_id"], effect_task_id, unknown)
            self._persist(state)
            return {"effect_calls": state.get("effect_calls", 0), "effect_mode": "none"}
        return {}

    def _done(self, state: FixtureState) -> dict[str, Any]:
        # Only this node attempts terminal authorization. A failed authorization is
        # an explicit graph block, never a silent completion.
        try:
            self.dispatcher.authorize_terminal(state["af_state"])
        except PermissionError as exc:
            return {
                "graph_status": "blocked_terminal",
                "blocked_reason": str(exc),
                "terminal_attempts": state.get("terminal_attempts", 0) + 1,
            }
        return {
            "graph_status": "complete",
            "blocked_reason": None,
            "terminal_attempts": state.get("terminal_attempts", 0) + 1,
        }

    @staticmethod
    def _blocked(state: FixtureState) -> dict[str, Any]:
        return {"graph_status": state.get("graph_status", "blocked")}

    def _compile(self):
        graph = StateGraph(FixtureState)
        graph.add_node("entry", lambda state: {})
        graph.add_node("plan", self._plan)
        graph.add_node("dispatch", self._dispatch)
        graph.add_node("collect", self._collect)
        graph.add_node("validate", self._validate)
        graph.add_node("effect_guard", self._effect_guard)
        graph.add_node("done", self._done)
        graph.add_node("blocked", self._blocked)
        graph.add_edge(START, "entry")
        graph.add_conditional_edges(
            "entry",
            lambda state: "effect_guard" if state.get("effect_mode") == "resume_unknown" else "plan",
            {"effect_guard": "effect_guard", "plan": "plan"},
        )
        graph.add_conditional_edges("plan", self._after_plan, {
            "dispatch": "dispatch", "done": "done", "blocked": "blocked",
        })
        graph.add_edge("dispatch", "collect")
        graph.add_edge("collect", "validate")
        graph.add_conditional_edges("validate", self._after_validate, {
            "effect_guard": "effect_guard", "blocked": "blocked",
        })
        graph.add_edge("effect_guard", "plan")
        graph.add_edge("done", END)
        graph.add_edge("blocked", END)
        return graph.compile(checkpointer=self.checkpointer)

    def invoke(self, state: FixtureState) -> FixtureState:
        validate_run_state(state["af_state"])
        state.setdefault("thread_id", state["af_state"]["run_id"])
        state.setdefault("scripted_batches", [])
        state.setdefault("validator_receipts", [])
        state.setdefault("validator_launch_receipts", [])
        state.setdefault("wave_history", [])
        state.setdefault("parent_tool_calls", 0)
        state.setdefault("terminal_attempts", 0)
        state.setdefault("effect_mode", "none")
        state.setdefault("effect_calls", 0)
        return self.graph.invoke(state, {"configurable": {"thread_id": state["thread_id"]}})

    def attempt_parent_worker_action(self, state: FixtureState, action_class: str = "worker_research") -> None:
        """Fixture parent-tool spy: increments only after host authorization."""
        self.dispatcher.authorize_parent_action(state["af_state"], action_class)
        state["parent_tool_calls"] = state.get("parent_tool_calls", 0) + 1


def resumed_state(path: str | Path, **overrides: Any) -> FixtureState:
    """Load only AgentFanout's durable state for a fresh graph/checkpointer process."""
    state: FixtureState = {
        "af_state": load_run_state(path),
        "state_path": str(path),
        "thread_id": overrides.pop("thread_id", "fixture-resume"),
        **overrides,
    }
    return state
