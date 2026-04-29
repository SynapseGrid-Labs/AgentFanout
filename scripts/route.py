#!/usr/bin/env python3
"""Deterministic advisory router for AgentFanout.

The helper is stateless. It accepts prior conversation state with --state-json
and returns next_state for the caller to carry forward.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PAI_DIR = Path(os.getenv("PAI_DIR") or os.getenv("PAI_HOME") or (Path.home() / ".pai"))

PROVIDERS = {"Auto", "MiniMax", "Local", "Codex", "Claude"}
EFFORTS = {"Auto", "High"}
DEFAULT_STATE = {
    "provider_mode": "Auto",
    "effort_mode": "Auto",
    "fanout_policy": "automatic_guarded",
    "state_scope": "conversation_only",
}

VALID_COMMANDS = [
    "AgentFanout Status",
    "AgentFanout Help",
    "AgentFanout MiniMax",
    "AgentFanout Local",
    "AgentFanout Codex",
    "AgentFanout Claude",
    "AgentFanout HIGH",
    "AgentFanout Auto",
    "AgentFanout Clear",
    "AgentFanout Reset",
]

COMMAND_LINE = re.compile(r"^AgentFanout(?:\s+([A-Za-z]+))?\s*$", re.IGNORECASE)
COMMAND_PREFIX = re.compile(r"^AgentFanout\b", re.IGNORECASE)

ORCH_TRIGGERS = [
    "agentfanout", "parallel agents", "orchestrator mode", "orchestratormode", "agentorchestrator",
    "delegate", "spin up workers",
    "worker agents", "run validators", "validators", "cheapest capable",
    "local-first", "minimax workers", "codex subagents", "claude agents",
    "local llm workers", "multi-agent", "fan out", "provider-agnostic",
    "cost-aware", "agents",
]
SPECIALIST_AGENT_CUES = [
    "custom agent", "custom agents", "named agent", "named agents", "personality",
    "personalities", "traits", "specialist agents", "specialized agents", "skeptical",
    "enthusiastic", "persona", "personas",
]
CODEX_AGENT_CUES = ["codexagent", "codex-native", "codex persona", "codex profile"]
CLEAR_APPROVALS = ["approved", "implement it", "do it", "execute the plan", "yes proceed", "proceed with the plan"]
AMBIGUOUS_APPROVALS = ["ok", "okay", "sounds good", "maybe", "continue", "interesting"]

MCP_CUES = [
    "memory", "calendar", "gmail", "email", "send an email", "asana", "slack",
    "discord", "playwright", "gws", "google workspace", "private search",
    "workspace tool", "mcp",
]
SECRET_CUES = ["secret", "api key", "token", "credential", "password", "private key"]
GIT_STATE_CUES = ["commit", "rebase", "cherry-pick", "reset --hard", "force push", "tag release", "create branch"]
GIT_READ_CUES = ["diff", "git log", "blame"]
DESTRUCTIVE_CUES = ["delete", "wipe", "drop table", "truncate", "destroy", "production deploy", "irreversible"]
HIGH_RISK_CUES = ["security", "release", "architecture", "migration", "high stakes", "critical", "threat", "vulnerability"]

FORBIDDEN_PATTERN = re.compile(
    r"(voice server|tts|text-to-speech|localhost:8888|localhost callback|/notify|"
    r"voice_id|voice_enabled|\bspeak\b|\bcurl\b|webhook|callback|notification|"
    r"spoken-summary|spoken summary|readback)",
    re.IGNORECASE,
)

SAFE_DELEGATED_INSTRUCTION = (
    "Return results only to the main session. Do not contact external services, "
    "start audio behavior, run post-run status hooks, or send out-of-band status messages. "
    "Produce the requested result text only."
)


def contains_any(text: str, cues: list[str]) -> bool:
    lowered = text.lower()
    return any(cue in lowered for cue in cues)


def canonical_provider(value: str) -> str:
    lookup = {
        "auto": "Auto",
        "minimax": "MiniMax",
        "local": "Local",
        "codex": "Codex",
        "claude": "Claude",
    }
    return lookup.get(value.lower(), value)


def default_state() -> dict[str, Any]:
    return dict(DEFAULT_STATE)


def normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return default_state()
    state = default_state()
    provider = canonical_provider(str(raw.get("provider_mode", state["provider_mode"])))
    effort = str(raw.get("effort_mode", state["effort_mode"])).capitalize()
    if provider in PROVIDERS:
        state["provider_mode"] = provider
    if effort in EFFORTS:
        state["effort_mode"] = effort
    if raw.get("fanout_policy") == "automatic_guarded":
        state["fanout_policy"] = "automatic_guarded"
    if raw.get("state_scope") == "conversation_only":
        state["state_scope"] = "conversation_only"
    return state


def state_warnings(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["state_invalid_schema_defaulted"]
    expected = set(DEFAULT_STATE)
    warnings: list[str] = []
    unexpected = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unexpected:
        warnings.append(f"unknown_state_keys_dropped:{','.join(unexpected)}")
    if missing:
        warnings.append(f"state_missing_keys_defaulted:{','.join(missing)}")
    if "provider_mode" in raw and canonical_provider(str(raw["provider_mode"])) not in PROVIDERS:
        warnings.append("state_invalid_provider_defaulted")
    if "effort_mode" in raw and str(raw["effort_mode"]).capitalize() not in EFFORTS:
        warnings.append("state_invalid_effort_defaulted")
    if "fanout_policy" in raw and raw["fanout_policy"] != "automatic_guarded":
        warnings.append("state_invalid_fanout_policy_defaulted")
    if "state_scope" in raw and raw["state_scope"] != "conversation_only":
        warnings.append("state_invalid_scope_defaulted")
    return warnings


def parse_state_arg(raw: str | None) -> tuple[dict[str, Any], list[str]]:
    if raw is None:
        return default_state(), []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "malformed_state_json"}), file=sys.stderr)
        sys.exit(2)
    return normalize_state(parsed), state_warnings(parsed)


def command_template(command_type: str = "route", name: str | None = None) -> dict[str, Any]:
    return {
        "type": command_type,
        "name": name,
        "args": {},
        "valid_commands": VALID_COMMANDS,
        "error": None,
    }


def extract_command(prompt: str, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, list[str]]:
    lines = prompt.splitlines() or [prompt]
    command_lines = [(index, line.strip()) for index, line in enumerate(lines) if COMMAND_PREFIX.match(line.strip())]
    warnings: list[str] = []
    next_state = dict(state)

    if not command_lines:
        return command_template(), next_state, prompt, warnings
    if len(command_lines) > 1:
        command = command_template("invalid")
        command["error"] = "multiple_agentfanout_commands"
        warnings.append("invalid_command")
        return command, next_state, prompt, warnings

    index, line = command_lines[0]
    match = COMMAND_LINE.match(line)
    if not match or not match.group(1):
        command = command_template("invalid")
        command["error"] = "invalid_command_syntax"
        warnings.append("invalid_command")
        return command, next_state, prompt, warnings

    word = match.group(1)
    normalized = word.lower()
    remaining = "\n".join(line for i, line in enumerate(lines) if i != index).strip()

    if normalized == "status":
        return command_template("status", "AgentFanout Status"), next_state, remaining, warnings
    if normalized == "help":
        return command_template("help", "AgentFanout Help"), next_state, remaining, warnings
    if normalized in {"minimax", "local", "codex", "claude"}:
        provider = canonical_provider(normalized)
        next_state["provider_mode"] = provider
        return command_template("set_provider", f"AgentFanout {provider}"), next_state, remaining, warnings
    if normalized == "high":
        next_state["effort_mode"] = "High"
        return command_template("set_effort", "AgentFanout HIGH"), next_state, remaining, warnings
    if normalized in {"auto", "clear", "reset"}:
        label = "Auto" if normalized == "auto" else normalized.capitalize()
        return command_template("reset", f"AgentFanout {label}"), default_state(), remaining, warnings

    command = command_template("invalid")
    command["error"] = f"unknown_command:{word}"
    warnings.append("invalid_command")
    return command, state, prompt, warnings


def sanitize_prompt(prompt: str) -> dict[str, Any]:
    kept: list[str] = []
    removed = 0
    for line in prompt.splitlines():
        if FORBIDDEN_PATTERN.search(line):
            removed += 1
            continue
        kept.append(line)
    sanitized = "\n".join(kept).strip()
    if sanitized:
        sanitized += "\n\n"
    sanitized += SAFE_DELEGATED_INSTRUCTION
    still_contains = bool(FORBIDDEN_PATTERN.search(sanitized))
    return {"sanitized_prompt": sanitized, "removed_lines": removed, "safe": not still_contains}


def classify(prompt: str) -> str:
    p = prompt.lower()
    if contains_any(p, MCP_CUES):
        return "mcp-inline"
    if contains_any(p, GIT_STATE_CUES) or contains_any(p, GIT_READ_CUES):
        return "git-ops"
    if "one-line" in p or "one line" in p or "tiny" in p or "small edit" in p or "copy edit" in p:
        return "small-edit"
    if "security" in p or "threat" in p or "vulnerability" in p:
        return "security-review"
    if "refactor" in p or "restructure" in p or "multi-file" in p:
        return "refactor"
    if "test" in p and any(x in p for x in ["write", "generate", "add", "modify", "failing", "debug", "fix"]):
        return "debug" if any(x in p for x in ["debug", "failing", "failure"]) else "test-gen"
    if "debug" in p or "error" in p or "failure" in p or "failing" in p:
        return "debug"
    if "classify" in p or "label" in p or "extract" in p:
        return "bulk-classify"
    if "csv" in p or "json" in p or "etl" in p or "schema" in p or "reshape" in p or "parse" in p:
        return "data-xform"
    if "logs" in p or "summarize" in p or "grep" in p or "review these" in p or "modules" in p:
        return "bulk-read"
    if "architecture" in p or "migration" in p or "design" in p or "strategy" in p or "spec" in p or "plan" in p:
        return "plan"
    if "prove" in p or "correctness" in p or "argument" in p or "reason" in p:
        return "reason"
    if "copy" in p or "marketing" in p or "creative" in p or "draft" in p:
        return "creative"
    return "bulk-read" if contains_any(p, ORCH_TRIGGERS) else "small-edit"


def requested_provider(prompt: str) -> tuple[str | None, list[str]]:
    p = prompt.lower()
    warnings: list[str] = []
    if "orchestratormode" in p or "codexagent" in p or "agentorchestrator" in p:
        warnings.append("legacy_trigger_one_turn_hint_only")
    if "minimax" in p:
        return "MiniMax", warnings
    if "claude" in p:
        return "Claude", warnings
    if "codex" in p:
        return "Codex", warnings
    if "local" in p:
        return "Local", warnings
    return None, warnings


def hard_gates(prompt: str, task_class: str) -> dict[str, bool]:
    return {
        "requires_mcp_private_tools": task_class == "mcp-inline",
        "requires_secrets": contains_any(prompt, SECRET_CUES),
        "requires_git_state_change": contains_any(prompt, GIT_STATE_CUES),
        "requires_destructive_action": contains_any(prompt, DESTRUCTIVE_CUES),
        "requires_final_synthesis": True,
    }


def reasoning_effort(task_class: str, prompt: str, state: dict[str, Any]) -> str:
    p = prompt.lower()
    if state["effort_mode"] == "High":
        return "xhigh" if contains_any(p, HIGH_RISK_CUES) or task_class in {"security-review", "plan", "refactor", "reason"} else "high"
    if "critical" in p or "high stakes" in p or "xhigh" in p:
        return "xhigh"
    if task_class in {"plan", "refactor", "security-review", "reason"}:
        return "high"
    if task_class in {"debug", "test-gen", "data-xform"}:
        return "medium"
    return "low"


def role_for(task_class: str, prompt: str) -> str:
    p = prompt.lower()
    if task_class in {"bulk-read", "plan", "security-review", "reason"}:
        return "explorer"
    if task_class == "debug" and not any(x in p for x in ["fix", "edit", "patch", "modify"]):
        return "explorer"
    if task_class in {"bulk-classify", "data-xform", "test-gen", "refactor", "debug", "creative"}:
        return "worker"
    return "main"


def agent_composition_required(prompt: str) -> bool:
    return contains_any(prompt, SPECIALIST_AGENT_CUES)


def codex_adapter_required(prompt: str, provider: str) -> bool:
    return provider == "Codex" or contains_any(prompt, CODEX_AGENT_CUES)


def phrase_present(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase)
    return bool(re.search(rf"(?<!\w){escaped}(?!\w)", text))


def approval_status(prompt: str, pending_plan: bool, plan_stale: bool, needs_separate_confirmation: bool) -> tuple[bool, list[str], str]:
    if not pending_plan:
        return False, [], "no_pending_plan"
    lowered = prompt.strip().lower()
    warnings: list[str] = []
    if any(lowered == cue or phrase_present(lowered, cue) for cue in AMBIGUOUS_APPROVALS):
        warnings.append("approval_ambiguous")
        return False, warnings, "ambiguous_approval"
    clear = any(lowered == cue or phrase_present(lowered, cue) for cue in CLEAR_APPROVALS)
    if not clear:
        return False, [], "no_clear_approval"
    if plan_stale:
        warnings.append("approved_plan_stale")
        return False, warnings, "plan_stale"
    if needs_separate_confirmation:
        warnings.append("approved_plan_needs_separate_confirmation")
        return False, warnings, "needs_separate_confirmation"
    return True, [], "clear_approval"


def should_delegate(task_class: str, prompt: str, gates: dict[str, bool]) -> bool:
    if gates["requires_mcp_private_tools"] or gates["requires_secrets"] or gates["requires_git_state_change"] or gates["requires_destructive_action"]:
        return False
    if task_class == "small-edit":
        return False
    explicit = contains_any(prompt, ORCH_TRIGGERS)
    complex_class = task_class in {"bulk-read", "bulk-classify", "data-xform", "debug", "plan", "refactor", "test-gen", "reason", "security-review"}
    return explicit or complex_class


def env_flag(*names: str) -> bool:
    return any(os.getenv(name, "").lower() in {"1", "true", "yes"} for name in names)


def display_path(path: Path) -> str:
    try:
        return f"~/{path.expanduser().resolve().relative_to(Path.home().resolve())}"
    except (OSError, ValueError):
        return str(path)


def pai_skill_file(*parts: str) -> Path:
    return PAI_DIR / "skills" / Path(*parts)


def readiness(provider: str, selector: str) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    adapter_files = {
        "MiniMax": ROOT / "adapters" / "MiniMax.md",
        "Local": ROOT / "adapters" / "LocalLLM.md",
        "Codex": ROOT / "adapters" / "Codex.md",
        "Claude": ROOT / "adapters" / "Claude.md",
    }
    adapter_path = adapter_files.get(provider)
    if adapter_path and not adapter_path.exists():
        return False, [f"provider_unavailable:{provider.lower()}:adapter_missing"]
    if provider == "MiniMax":
        tool = pai_skill_file("MiniMax", "Tools", "MiniMaxExec.ts")
        if env_flag("AGENT_FANOUT_MINIMAX_AVAILABLE", "AGENT_ORCH_MINIMAX_AVAILABLE"):
            return True, warnings
        if not tool.exists():
            return False, ["provider_unavailable:minimax:tool_missing"]
    if provider == "Local":
        if not env_flag("AGENT_FANOUT_LOCAL_AVAILABLE", "AGENT_ORCH_LOCAL_AVAILABLE"):
            return False, ["provider_unavailable:local:service_reachability_unknown"]
    if provider == "Codex":
        codex_runtime_detected = bool(os.getenv("CODEX_THREAD_ID") or os.getenv("PAI_CODEX_LAUNCH_ID"))
        if not codex_runtime_detected and not env_flag("AGENT_FANOUT_CODEX_SUBAGENTS", "AGENT_ORCH_CODEX_SUBAGENTS"):
            return False, ["provider_unavailable:codex:native_subagents_unknown"]
    if provider == "Claude":
        if not env_flag("AGENT_FANOUT_CLAUDE_AVAILABLE", "AGENT_ORCH_CLAUDE_AVAILABLE"):
            return False, ["provider_unavailable:claude:adapter_not_configured"]
    if not selector:
        warnings.append(f"provider_unavailable:{provider.lower()}:selector_unmapped")
        return False, warnings
    return True, warnings


def selector_for(provider: str, effort: str, task_class: str) -> str:
    high = effort in {"high", "xhigh"}
    if provider == "MiniMax":
        return "minimax-latest" if high else "minimax-fast"
    if provider == "Local":
        if high:
            return "local-thinker"
        if task_class in {"debug", "test-gen", "refactor"}:
            return "local-coder"
        return "local-worker"
    if provider == "Codex":
        if high:
            return "current-frontier"
        return "current-balanced" if effort == "medium" else "current-fast-mini"
    if provider == "Claude":
        if high:
            return "claude-frontier"
        return "claude-balanced" if effort == "medium" else "claude-fast"
    return "main-session"


def auto_provider_candidates(task_class: str, effort: str) -> list[str]:
    if task_class in {"debug", "test-gen", "refactor"}:
        return ["Codex", "Local", "MiniMax", "Claude"]
    if effort == "low" and task_class in {"bulk-read", "bulk-classify", "data-xform", "creative"}:
        return ["Local", "MiniMax", "Codex", "Claude"]
    if task_class in {"security-review", "plan", "reason"}:
        return ["Codex", "MiniMax", "Claude", "Local"]
    return ["Codex", "MiniMax", "Local", "Claude"]


def choose_provider(prompt: str, state: dict[str, Any], task_class: str, effort: str, delegated: bool) -> tuple[str, str, list[str]]:
    if not delegated:
        return "main-session", "main-session", []
    provider_hint, hint_warnings = requested_provider(prompt)
    if provider_hint:
        candidates = [provider_hint]
    elif state["provider_mode"] != "Auto":
        candidates = [state["provider_mode"]]
    else:
        candidates = auto_provider_candidates(task_class, effort)

    warnings = list(hint_warnings)
    for provider in candidates:
        selector = selector_for(provider, effort, task_class)
        available, readiness_warnings = readiness(provider, selector)
        if available:
            return provider, selector, warnings + readiness_warnings
        warnings.extend(readiness_warnings)
    return "main-session", "main-session", warnings


def estimate_work_units(prompt: str, task_class: str, delegated: bool) -> tuple[int, list[str], list[str]]:
    if not delegated:
        return 0, [], ["delegation_not_useful_or_hard_gated"]
    lowered = prompt.lower()
    word_numbers = {"two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "twelve": 12, "twenty": 20}
    count = 1
    for pattern in [
        r"\b(\d{1,3})\s+(?:independent\s+)?(?:modules|files|items|tasks|services|components|workstreams|things)\b",
        r"\b(\d{1,3})\s+independent\b",
    ]:
        match = re.search(pattern, lowered)
        if match:
            count = max(1, int(match.group(1)))
            break
    else:
        for word, value in word_numbers.items():
            if re.search(rf"\b{word}\s+(?:independent\s+)?(?:modules|files|items|tasks|services|components|workstreams|things)\b", lowered):
                count = value
                break
        else:
            if task_class in {"bulk-classify", "data-xform"}:
                count = 4
            elif task_class in {"plan", "refactor", "security-review", "debug", "test-gen"}:
                count = 3
            elif "parallel" in lowered or "multiple" in lowered or "fan out" in lowered:
                count = 3
    units = [f"work_unit_{i}" for i in range(1, count + 1)]
    guardrails: list[str] = []
    guardrails.append("hard_gates_prechecked")
    guardrails.append("provider_readiness_prechecked")
    if count == 1:
        guardrails.append("single_worker_enough")
    return count, units, guardrails


def capsule_for(task_class: str, prompt: str, role: str, delegated: bool) -> dict[str, Any] | None:
    if not delegated:
        return None
    role_map = {
        "debug": ("Coding Specialist", ["Trace failures systematically", "Preserve existing behavior", "Return evidence and minimal fixes"]),
        "refactor": ("Coding Specialist", ["Understand local patterns", "Keep ownership bounded", "Avoid unrelated cleanup"]),
        "test-gen": ("Test Engineer", ["Target meaningful coverage", "Use existing test style", "Report exact commands"]),
        "security-review": ("Security Reviewer", ["Identify concrete risks", "Avoid credential exposure", "Separate evidence from suspicion"]),
        "plan": ("Architecture Reviewer", ["Evaluate tradeoffs", "Surface assumptions", "Return implementation-ready findings"]),
        "data-xform": ("Data Extraction Specialist", ["Preserve schema meaning", "Validate edge cases", "Return structured output"]),
        "bulk-classify": ("Data Classification Specialist", ["Use consistent labels", "Flag uncertain cases", "Return structured output"]),
        "bulk-read": ("Research Analyst", ["Stay within assigned scope", "Collect evidence", "Summarize only relevant findings"]),
    }
    role_name, expertise = role_map.get(task_class, ("Task Specialist", ["Stay within scope", "Return evidence", "Avoid unrelated work"]))
    if "code" in prompt.lower() or role == "worker":
        role_name = "Coding Specialist" if task_class in {"debug", "refactor", "test-gen"} else role_name
    return {
        "role_name": role_name,
        "expertise": expertise,
        "task_scope": "bounded delegated work unit",
        "allowed_actions": ["read", "edit", "test", "analyze"] if role == "worker" else ["read", "analyze"],
        "forbidden_actions": ["mcp_private_tools", "secrets", "git_state_changes", "destructive_actions", "callbacks_voice_notifications", "final_synthesis", "automatic_memory_loading"],
        "output_contract": {"format": "markdown_or_json", "required_fields": ["summary", "evidence", "risks_or_uncertainties"]},
        "return_only_to_main_session": True,
        "memory_context": "none",
        "full_agents_context": False,
        "full_codexagent_context": False,
    }


def references_to_load(prompt: str, provider: str, delegated: bool, full_agents: bool, codex_ref: bool, pending_plan: bool) -> list[str]:
    refs: list[str] = []
    if delegated:
        refs.append("references/AgentCapsules.md")
    if full_agents:
        refs.append(display_path(pai_skill_file("Agents", "SKILL.md")))
    if codex_ref:
        refs.append("adapters/Codex.md")
    elif provider in {"MiniMax", "Local", "Claude"}:
        adapter = {"MiniMax": "MiniMax.md", "Local": "LocalLLM.md", "Claude": "Claude.md"}[provider]
        refs.append(f"adapters/{adapter}")
    if pending_plan or "approved" in prompt.lower() or "execute the plan" in prompt.lower():
        refs.append("references/ApprovalExecution.md")
    return refs


def validator_for(provider: str, task_class: str, role: str, delegated: bool, effort: str) -> dict[str, Any]:
    required = delegated and (provider != "main-session" or role == "worker" or task_class in {"plan", "refactor", "security-review", "reason", "debug", "test-gen"} or effort in {"high", "xhigh"})
    packet = None
    if required:
        packet = {
            "validator_id": "validator_1",
            "task_scope": "bounded worker result validation",
            "evidence_refs": [],
            "checklist": ["safety gates respected", "evidence supports claims", "output contract satisfied"],
            "pass_threshold": "all_critical_items_pass",
            "forbidden_actions": ["mcp_private_tools", "secrets", "git_state_changes", "destructive_actions", "callbacks_voice_notifications", "final_synthesis"],
            "output_contract": {
                "verdict": "pass|concerns|fail",
                "required_fields": ["checked_items", "evidence_refs", "required_fixes", "escalation_required"],
            },
            "escalation_target": "main_session",
        }
    return {
        "required": required,
        "provider": "codex_or_main_session" if provider == "MiniMax" else "main_session_or_independent_adapter" if required else None,
        "packet": packet,
    }


def circuit_breaker(worker_failures: int = 0) -> dict[str, Any]:
    failures = max(0, worker_failures)
    return {
        "scope": "route_invocation_wave",
        "triggered": failures >= 3,
        "failure_count": failures,
        "wave_id": "wave_1" if failures else None,
        "reason": "worker_failure_threshold" if failures >= 3 else None,
    }


def status_payload(state: dict[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "provider_mode": state["provider_mode"],
        "effort_mode": state["effort_mode"],
        "fanout_policy": "automatic_guarded",
        "state_scope": "conversation_only",
        "hard_gate_policy": {
            "mcp_private_tools": "main_session_only",
            "secrets": "main_session_only",
            "git_state_changes": "main_session_only",
            "destructive_actions": "main_session_only",
            "final_synthesis": "main_session_only",
        },
        "startup_mode": "compact_skill_routing_only",
        "full_skill_loaded_for_this_request": False,
        "warnings": warnings or [],
    }


def route(
    prompt: str,
    state: dict[str, Any] | None = None,
    worker_failures: int = 0,
    pending_plan: bool = False,
    plan_stale: bool = False,
    needs_separate_confirmation: bool = False,
) -> dict[str, Any]:
    state = normalize_state(state or default_state())
    command, next_state, remaining_prompt, command_warnings = extract_command(prompt, state)
    warnings = list(command_warnings)
    should_execute_plan, approval_warnings, approval_reason = approval_status(
        prompt, pending_plan, plan_stale, needs_separate_confirmation
    )
    warnings.extend(approval_warnings)

    if command["type"] == "invalid" or (command["type"] != "route" and not remaining_prompt):
        return {
            "command": command,
            "route": {"task_class": "command", "worth_delegating": False, "executor": {"provider": "main-session", "role": "main", "model_selector": "main-session", "reasoning_effort": "low"}},
            "orchestration_useful": False,
            "agent_composition_required": False,
            "codex_adapter_required": False,
            "selected_provider": "main-session",
            "references_to_load": ["references/ApprovalExecution.md"] if pending_plan else [],
            "approved_plan_should_execute": should_execute_plan,
            "reason": approval_reason if pending_plan else "command_only",
            "worker_capsule": None,
            "next_state": next_state,
            "status": status_payload(next_state, warnings),
            "hard_gates": hard_gates("", "small-edit"),
            "validator": {"required": False, "provider": None, "packet": None},
            "fanout": {"policy": "automatic_guarded", "planned_workers": 0, "work_units": [], "rationale": "command_only", "guardrails_applied": ["no_task_to_delegate"], "fallback_reason": None},
            "circuit_breaker": circuit_breaker(worker_failures),
            "warnings": warnings,
        }

    task_prompt = remaining_prompt if command["type"] != "invalid" else prompt
    task_class = classify(task_prompt)
    gates = hard_gates(task_prompt, task_class)
    delegated = should_delegate(task_class, task_prompt, gates)
    effort = reasoning_effort(task_class, task_prompt, next_state)
    role = role_for(task_class, task_prompt) if delegated else "main"
    provider, selector, provider_warnings = choose_provider(task_prompt, next_state, task_class, effort, delegated)
    warnings.extend(provider_warnings)
    if provider == "main-session":
        delegated = False
        role = "main"
    planned_workers, units, guardrails = estimate_work_units(task_prompt, task_class, delegated)
    parallelism = "none" if not delegated else "parallel" if planned_workers > 1 else "serial-delegated"
    validator = validator_for(provider, task_class, role, delegated, effort)
    full_agents = agent_composition_required(task_prompt)
    codex_ref = codex_adapter_required(task_prompt, provider) or next_state["provider_mode"] == "Codex" or "codex" in task_prompt.lower()
    refs = references_to_load(task_prompt, provider, delegated, full_agents, codex_ref, pending_plan)
    capsule = capsule_for(task_class, task_prompt, role, delegated)
    breaker = circuit_breaker(worker_failures)
    if breaker["triggered"]:
        warnings.append("circuit_breaker_triggered")

    return {
        "command": command,
        "route": {
            "task_class": task_class,
            "worth_delegating": delegated,
            "parallelism": parallelism,
            "executor": {
                "role": role,
                "provider": provider,
                "model_selector": selector,
                "reasoning_effort": effort,
            },
        },
        "orchestration_useful": delegated,
        "agent_composition_required": full_agents,
        "codex_adapter_required": codex_ref,
        "selected_provider": provider,
        "references_to_load": refs,
        "approved_plan_should_execute": should_execute_plan,
        "reason": approval_reason if pending_plan else ("orchestration_useful" if delegated else "main_session_only"),
        "worker_capsule": capsule,
        "next_state": next_state,
        "status": status_payload(next_state, warnings),
        "hard_gates": gates,
        "validator": validator,
        "fanout": {
            "policy": "automatic_guarded",
            "planned_workers": planned_workers,
            "work_units": units,
            "rationale": "automatic decomposition estimate" if delegated else "main_session_only",
            "guardrails_applied": guardrails,
            "fallback_reason": "; ".join(provider_warnings) if provider_warnings and not delegated else None,
        },
        "circuit_breaker": breaker,
        "warnings": warnings,
        "main_session_responsibilities": ["MCP/private tools", "secrets", "git state changes", "destructive actions", "final synthesis"],
        "safe_delegated_instruction": SAFE_DELEGATED_INSTRUCTION if delegated else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="*", help="Prompt to route")
    parser.add_argument("--sanitize", action="store_true", help="Sanitize stdin/prompt for delegated use")
    parser.add_argument("--state-json", help="Prior conversation state JSON")
    parser.add_argument("--worker-failures", type=int, default=0, help="Observed worker failures in the current fanout wave")
    parser.add_argument("--pending-plan", action="store_true", help="Treat the current prompt as a response to a pending AgentFanout plan")
    parser.add_argument("--plan-stale", action="store_true", help="Mark the pending plan stale")
    parser.add_argument("--needs-separate-confirmation", action="store_true", help="Pending plan needs separate destructive/production/secret/git confirmation")
    args = parser.parse_args()
    text = " ".join(args.prompt).strip()
    if args.sanitize:
        print(json.dumps(sanitize_prompt(text), indent=2))
        return
    state, state_warnings = parse_state_arg(args.state_json)
    result = route(
        text,
        state,
        args.worker_failures,
        args.pending_plan,
        args.plan_stale,
        args.needs_separate_confirmation,
    )
    result["warnings"] = state_warnings + result.get("warnings", [])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
