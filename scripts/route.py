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
    "AgentFanout MiniMax Mode",
    "AgentFanout Local",
    "AgentFanout Local Mode",
    "AgentFanout Codex",
    "AgentFanout Codex Mode",
    "AgentFanout Claude",
    "AgentFanout Claude Mode",
    "AgentFanout HIGH",
    "AgentFanout Auto",
    "AgentFanout Clear",
    "AgentFanout Reset",
]

COMMAND_LINE = re.compile(r"^AgentFanout(?:\s+([A-Za-z]+)(?:\s+([A-Za-z]+))?)?\s*$", re.IGNORECASE)
COMMAND_PREFIX = re.compile(r"^AgentFanout\b", re.IGNORECASE)

ORCH_TRIGGERS = [
    "agentfanout", "parallel agents", "orchestrator mode", "orchestratormode", "agentorchestrator",
    "delegate", "spin up workers",
    "worker agents", "run validators", "validators", "cheapest capable",
    "local-first", "minimax workers", "codex subagents", "claude agents",
    "local llm workers", "multi-agent", "fan out", "provider-agnostic",
    "cost-aware", "agents",
]
INVESTIGATION_CUES = [
    "investigate", "full investigation", "research", "find", "search", "look up",
    "trace", "where", "whether", "exists", "current state",
    "stale process", "stale processes", "running process",
    "service", "services", "host", "controller", "controllers", "path", "repo",
]
BROAD_INVESTIGATION_CUES = [
    "full investigation", "fully investigate", "deep investigation", "complete investigation",
    "audit", "security check", "read-only diagnostics", "multiple systems", "cross-check",
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
    "calendar", "gmail", "email", "send an email", "asana", "slack",
    "discord", "playwright", "gws", "google workspace", "private search",
    "workspace tool", "mcp",
]
MCP_MEMORY_CUES = [
    "use memory", "search memory", "search memories", "saved memory",
    "private memory", "memory mcp", "recall from memory", "look in memory",
    "check memory", "remembered context",
]
SECRET_CUES = ["secret", "api key", "token", "credential", "password", "private key"]
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@[^ \t\r\n]+", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE_KEY|API_KEY|ACCESS_KEY)[A-Z0-9_]*\s*=\s*[^\s'\"]{8,}\b"),
]
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


def looks_like_secret(text: str) -> bool:
    return contains_any(text, SECRET_CUES) or any(pattern.search(text) for pattern in SECRET_PATTERNS)


def redact_sensitive_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def redact_sensitive_output(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [redact_sensitive_output(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_sensitive_output(item) for key, item in value.items()}
    return value


def write_json_output(value: Any) -> None:
    payload = json.dumps(redact_sensitive_output(value), indent=2) + "\n"
    os.write(sys.stdout.fileno(), payload.encode("utf-8"))


def requires_private_mcp_tools(text: str) -> bool:
    lowered = text.lower()
    return contains_any(lowered, MCP_CUES) or contains_any(lowered, MCP_MEMORY_CUES)


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
    modifier = match.group(2)
    normalized = word.lower()
    normalized_modifier = modifier.lower() if modifier else None
    remaining = "\n".join(line for i, line in enumerate(lines) if i != index).strip()

    if normalized == "status":
        return command_template("status", "AgentFanout Status"), next_state, remaining, warnings
    if normalized == "help":
        return command_template("help", "AgentFanout Help"), next_state, remaining, warnings
    if normalized in {"minimax", "local", "codex", "claude"}:
        provider = canonical_provider(normalized)
        if normalized_modifier == "mode":
            next_state["provider_mode"] = provider
            command = command_template("set_provider_mode", f"AgentFanout {provider} Mode")
            command["args"]["provider"] = provider
            command["args"]["lifecycle"] = "persistent_conversation_mode"
            return command, next_state, remaining, warnings
        if normalized_modifier:
            command = command_template("invalid")
            command["error"] = f"unknown_command_modifier:{modifier}"
            warnings.append("invalid_command")
            return command, next_state, prompt, warnings
        command = command_template("provider_task", f"AgentFanout {provider}")
        command["args"]["provider"] = provider
        command["args"]["lifecycle"] = "current_task"
        return command, next_state, remaining, warnings
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
    if requires_private_mcp_tools(p):
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
    if "architecture" in p or "migration" in p or "design" in p or "strategy" in p or "spec" in p or "plan" in p:
        return "plan"
    if "prove" in p or "correctness" in p or "argument" in p or "reason" in p:
        return "reason"
    if contains_any(p, INVESTIGATION_CUES):
        return "bulk-read"
    if "logs" in p or "summarize" in p or "grep" in p or "review these" in p or "modules" in p:
        return "bulk-read"
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
        "requires_secrets": looks_like_secret(prompt),
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


def worker_kind(task_class: str, role: str) -> str:
    if role == "main":
        return "main_session"
    if role == "worker":
        return "execution_worker"
    if task_class in {"bulk-read", "plan", "security-review", "reason"} or role == "explorer":
        return "research_worker"
    return "task_worker"


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
        if not env_flag("AGENT_FANOUT_LOCAL_LIVE_CHECKS", "AGENT_ORCH_LOCAL_LIVE_CHECKS"):
            return False, ["provider_unavailable:local:live_capability_check_missing"]
        if not env_flag("AGENT_FANOUT_LOCAL_CAPABILITY_VERIFIED", "AGENT_ORCH_LOCAL_CAPABILITY_VERIFIED"):
            return False, ["provider_unavailable:local:capability_not_verified"]
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


def provider_capability_snapshot(provider: str, selector: str, task_class: str, delegated: bool, effort: str) -> dict[str, Any]:
    if not delegated or provider == "main-session":
        return {
            "provider": "main-session",
            "model_selector": "main-session",
            "available_tools": ["main_session_tools"],
            "supports_file_editing": True,
            "validation_eligible": True,
            "snapshot_source": "main_session",
        }
    if provider == "MiniMax":
        tool_agent = minimax_tool_agent_enabled()
        return {
            "provider": provider,
            "model_selector": selector,
            "available_tools": ["ToolEnvelopeV1"] if tool_agent else [],
            "supports_file_editing": tool_agent,
            "validation_eligible": True,
            "snapshot_source": "agentfanout_env_and_adapter",
            "tool_runtime": "MiniMaxAgent" if tool_agent else "blocked",
        }
    if provider == "Local":
        return {
            "provider": provider,
            "model_selector": selector,
            "available_tools": [],
            "supports_file_editing": False,
            "validation_eligible": env_flag("AGENT_FANOUT_LOCAL_CAPABILITY_VERIFIED", "AGENT_ORCH_LOCAL_CAPABILITY_VERIFIED"),
            "snapshot_source": "live_check_required",
            "required_live_checks": ["/v1/models", "/health", "active_lila_mode", "exact_model_id", "context_window", "task_class_eval"],
            "active_mode": os.getenv("AGENT_FANOUT_LOCAL_ACTIVE_MODE", "unknown"),
            "loaded_model_id": os.getenv("AGENT_FANOUT_LOCAL_MODEL_ID", "unknown"),
            "context_window": os.getenv("AGENT_FANOUT_LOCAL_CONTEXT_WINDOW", "unknown"),
            "capability_expires": os.getenv("AGENT_FANOUT_LOCAL_CAPABILITY_EXPIRES", "short_window_required"),
        }
    if provider == "Codex":
        return {
            "provider": provider,
            "model_selector": selector,
            "available_tools": ["codex_subagent_tools"],
            "supports_file_editing": True,
            "validation_eligible": True,
            "snapshot_source": "codex_runtime_or_env",
        }
    if provider == "Claude":
        return {
            "provider": provider,
            "model_selector": selector,
            "available_tools": ["adapter_configured_tools"],
            "supports_file_editing": True,
            "validation_eligible": True,
            "snapshot_source": "adapter_env",
        }
    return {
        "provider": provider,
        "model_selector": selector,
        "available_tools": [],
        "supports_file_editing": False,
        "validation_eligible": False,
        "snapshot_source": "unknown",
    }


def selector_for(provider: str, effort: str, task_class: str) -> str:
    high = effort in {"high", "xhigh"}
    if provider == "MiniMax":
        return "minimax-latest"
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


def runtime_launch(provider: str, selector: str, effort: str) -> dict[str, Any]:
    if provider == "Codex":
        model_preference = {
            "current-fast-mini": "fast-mini",
            "current-balanced": "balanced",
            "current-frontier": "frontier",
        }.get(selector, "provider-default")
        cost_tier = {
            "current-fast-mini": "cheap",
            "current-balanced": "balanced",
            "current-frontier": "frontier",
        }.get(selector, "unknown")
        codex_native: dict[str, Any] = {
            "agent_type": "explorer" if selector == "current-fast-mini" else "default",
            "cost_tier": cost_tier,
            "model_preference": model_preference,
            "reasoning_effort": effort,
            "must_pass_model_override": selector == "current-fast-mini",
            "do_not_use_default_frontier_for_fast_mini": selector == "current-fast-mini",
        }
        if selector == "current-fast-mini":
            codex_native["required_model_capability"] = "mini_or_fast"
            codex_native["fallback_model_preference"] = "lowest_available_codex_mini_or_fast_model"
        elif selector == "current-frontier":
            codex_native["required_model_capability"] = "frontier_or_highest_capability"
        return {
            "provider": "Codex",
            "selector": selector,
            "codex_native": codex_native,
        }
    launch: dict[str, Any] = {
        "provider": provider,
        "selector": selector,
    }
    if provider == "MiniMax":
        launch["minimax_tool_agent"] = {
            "command": "bun $HOME/.pai/skills/MiniMax/Tools/MiniMaxAgent.ts --request-json - --json",
            "stdin_request": True,
            "required_flag": "AGENT_FANOUT_MINIMAX_TOOL_AGENT=1",
            "text_fallback_command": "bun $HOME/.pai/skills/MiniMax/Tools/MiniMaxExec.ts",
        }
    return launch


def minimax_tool_agent_enabled() -> bool:
    return env_flag("AGENT_FANOUT_MINIMAX_TOOL_AGENT")


def tool_envelope_for(task_prompt: str, task_class: str, role: str, worker_type_value: str, effort: str, planned_workers: int) -> dict[str, Any] | None:
    if worker_type_value == "main_session":
        return None
    mode = worker_type_value if worker_type_value in {"research_worker", "execution_worker", "validator"} else "research_worker"
    workdir = os.getcwd()
    tools_by_mode = {
        "research_worker": [
            "opsvault_search",
            "opsvault_file",
            "opsvault_stats",
            "repo_read_file",
            "repo_list_files",
            "repo_search",
            "run_readonly_command",
        ],
        "execution_worker": [
            "opsvault_search",
            "opsvault_file",
            "opsvault_stats",
            "repo_read_file",
            "repo_list_files",
            "repo_search",
            "run_readonly_command",
            "apply_scoped_patch",
            "run_verification_command",
        ],
        "validator": ["repo_read_file", "repo_list_files", "repo_search", "run_readonly_command", "run_verification_command"],
    }
    if role == "explorer":
        mode = "research_worker"
    write_files: list[str] = []
    shell_allowlist = [
        {
            "id": "rg-search",
            "cmd": "rg",
            "policy": "readonly",
            "cwd_roots": [workdir],
            "allowed_args_regex": r"^[A-Za-z0-9_./:=,@%+ -]+$",
            "timeout_ms": 30000,
        },
        {
            "id": "python-test",
            "cmd": "python3",
            "policy": "verification",
            "cwd_roots": [workdir],
            "allowed_args_regex": r"^[A-Za-z0-9_./:=,@%+ -]+$",
            "timeout_ms": 120000,
        },
        {
            "id": "bun-check",
            "cmd": "bun",
            "policy": "verification",
            "cwd_roots": [workdir],
            "allowed_args_regex": r"^[A-Za-z0-9_./:=,@%+ -]+$",
            "timeout_ms": 120000,
        },
    ]
    return {
        "version": 1,
        "request_id": "agentfanout-minimax",
        "mode": mode,
        "workdir": workdir,
        "prompt": task_prompt,
        "tools": tools_by_mode[mode],
        "read_roots": [workdir],
        "write_files": write_files,
        "assigned_files": write_files,
        "shell_allowlist": shell_allowlist,
        "opsvault": {
            "enabled": task_class in {"bulk-read", "debug", "plan", "security-review"},
            "tools": ["search", "file", "stats"],
            "allowed_sections": ["10_projects", "20_devices", "30_network", "40_runbooks"],
            "max_results": 10,
        },
        "limits": {
            "max_steps": 20,
            "max_workers": max(1, min(4, planned_workers or 1)),
            "max_parallel_tools": 4,
            "max_file_bytes": 250000,
            "max_tool_output_bytes": 65536,
            "max_transcript_bytes": 1048576,
            "timeout_ms": 300000,
        },
        "deny": [
            "secrets",
            "private_account_side_effects",
            "git_state_mutation",
            "destructive_actions",
            "deploys",
            "final_user_synthesis",
        ],
        "validator_required": task_class not in {"creative", "bulk-classify"} or effort in {"high", "xhigh"},
        "output_contract": "worker_result_v1" if mode != "validator" else "validator_result_v1",
    }


def auto_provider_candidates(task_class: str, effort: str) -> list[str]:
    if task_class in {"debug", "test-gen", "refactor"}:
        return ["Codex", "Local", "MiniMax", "Claude"]
    if effort == "low" and task_class in {"bulk-read", "bulk-classify", "data-xform", "creative"}:
        return ["Local", "MiniMax", "Codex", "Claude"]
    if task_class in {"security-review", "plan", "reason"}:
        return ["Codex", "MiniMax", "Claude", "Local"]
    return ["Codex", "MiniMax", "Local", "Claude"]


def choose_provider(prompt: str, state: dict[str, Any], task_class: str, effort: str, delegated: bool, provider_override: str | None = None) -> tuple[str, str, list[str]]:
    if not delegated:
        return "main-session", "main-session", []
    provider_hint, hint_warnings = requested_provider(prompt)
    if provider_override:
        candidates = [provider_override]
    elif provider_hint:
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


def investigation_dimensions(prompt: str) -> list[str]:
    lowered = prompt.lower()
    dimensions: list[str] = []
    if contains_any(lowered, ["knowledge base", "prior", "history", "incident", "document", "docs"]):
        dimensions.append("knowledge_base_context")
    if contains_any(lowered, ["host", "server", "machine", "ssh", "node", "instance"]):
        dimensions.append("live_host_state")
    if contains_any(lowered, ["process", "processes", "service", "services", "application", "gpu", "port", "docker", "ram", "swap", "memory usage"]):
        dimensions.append("process_service_state")
    if contains_any(lowered, ["repo", "code", "controller", "controllers", "path", "module", "component", "integration"]):
        dimensions.append("repo_code_path_trace")
    if contains_any(lowered, ["whether", "exists", "prove", "proof", "verify", "cross-check", "validate"]):
        dimensions.append("independent_validation")
    if contains_any(lowered, ["security", "public", "github", "release", "preflight"]):
        dimensions.append("security_release_review")
    return dimensions


def estimate_work_units(prompt: str, task_class: str, delegated: bool) -> tuple[int, list[str], list[str]]:
    if not delegated:
        return 0, [], ["delegation_not_useful_or_hard_gated"]
    lowered = prompt.lower()
    word_numbers = {"two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "twelve": 12, "twenty": 20}
    count = 1
    dimensions = investigation_dimensions(prompt) if task_class == "bulk-read" else []
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
            elif task_class == "bulk-read" and (contains_any(lowered, BROAD_INVESTIGATION_CUES) or len(dimensions) >= 2):
                count = max(3, min(6, len(dimensions) + 1))
            elif "parallel" in lowered or "multiple" in lowered or "fan out" in lowered:
                count = 3
    if dimensions and count > 1:
        units = dimensions[:count]
        while len(units) < count:
            units.append(f"research_work_unit_{len(units) + 1}")
    else:
        units = [f"work_unit_{i}" for i in range(1, count + 1)]
    guardrails: list[str] = []
    guardrails.append("hard_gates_prechecked")
    guardrails.append("provider_readiness_prechecked")
    if task_class == "bulk-read":
        guardrails.append("read_only_worker_scope")
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
    kind = worker_kind(task_class, role)
    read_only_scope = [
        "repo_file_reads",
        "filesystem_search",
        "knowledge_base_search_and_file_reads",
        "web_or_docs_research_when_available",
        "read_only_ssh_diagnostics_when_explicitly_scoped",
        "read_only_database_queries_when_configured",
    ]
    execution_scope = ["assigned_file_edits", "targeted_tests", "build_or_lint_checks", "bounded_shell_commands"]
    return {
        "role_name": role_name,
        "worker_type": kind,
        "expertise": expertise,
        "task_scope": "bounded delegated work unit",
        "allowed_actions": ["read", "edit", "test", "analyze"] if role == "worker" else ["read", "analyze"],
        "read_only_tool_scope": read_only_scope if kind == "research_worker" else [],
        "execution_tool_scope": execution_scope if kind == "execution_worker" else [],
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


def structural_validation_packet(provider: str, task_class: str, delegated: bool) -> dict[str, Any]:
    return {
        "schema": "StructuralValidationResultV1",
        "required": delegated,
        "checks": [
            "scope_compliance",
            "file_allowlist_compliance",
            "forbidden_tool_requests_absent",
            "test_or_build_evidence_present_when_applicable",
            "transcript_or_artifact_refs_present",
            "claims_have_evidence_refs",
        ],
        "block_on_fail": True,
        "provider": provider,
        "task_class": task_class,
    }


def provider_primary_config(provider: str, explicit: bool, delegated: bool) -> dict[str, Any]:
    active = delegated and provider != "main-session" and explicit
    return {
        "active": active,
        "orchestration_style": "provider_primary" if active else "automatic_guarded",
        "codex_role": "orchestrator_project_manager_final_compact_reviewer",
        "provider_role": "execute_structurally_validate_adversarially_validate" if active else "selected_worker",
        "lifecycle": "current_task_or_provider_mode",
        "max_provider_repair_loops": 2,
        "max_same_provider_validator_passes_per_loop": 1,
        "timeout_ms_per_task_packet": 300000,
        "codex_review_scope": "CompactResultPacketV1_only_unless_escalated",
        "escalate_on": [
            "structural_validation_failed_twice",
            "missing_evidence",
            "validator_disagreement",
            "forbidden_scope_or_tool_request",
            "provider_timeout",
            "risk_exceeds_provider_capability",
        ],
    }


def compact_result_contract(provider: str, delegated: bool) -> dict[str, Any] | None:
    if not delegated or provider == "main-session":
        return None
    return {
        "schema": "CompactResultPacketV1",
        "required_fields": [
            "summary",
            "changed_files_or_evidence_refs",
            "commands",
            "exit_codes",
            "structural_validation_result",
            "provider_validator_result",
            "unresolved_risks",
            "escalation_recommendation",
        ],
        "codex_consumption": "compact_final_review",
    }


def provider_primary_packet(provider: str, selector: str, task_class: str, delegated: bool, capability_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    if not delegated or provider == "main-session":
        return None
    return {
        "schema": "ProviderPrimaryPacketV1",
        "provider": provider,
        "model_selector": selector,
        "task_class": task_class,
        "scope": "bounded delegated work unit",
        "forbidden_scope": ["secrets", "private_mcp_side_effects", "git_state_mutation", "destructive_actions", "final_user_synthesis"],
        "allowed_tools": capability_snapshot.get("available_tools", []),
        "write_permissions": "scoped_tool_envelope_only" if capability_snapshot.get("supports_file_editing") else "none",
        "expected_artifacts": ["worker_result", "structural_validation", "same_provider_validator_result", "compact_result_packet"],
        "validation_checklist": ["scope_compliance", "evidence_supported_claims", "tests_or_commands_reported", "risks_reported"],
        "escalation_conditions": provider_primary_config(provider, True, delegated)["escalate_on"],
    }


def validator_for(provider: str, task_class: str, role: str, delegated: bool, effort: str, provider_primary: bool = False) -> dict[str, Any]:
    required = delegated and (provider != "main-session" or role == "worker" or task_class in {"plan", "refactor", "security-review", "reason", "debug", "test-gen"} or effort in {"high", "xhigh"})
    packet = None
    if required:
        packet = {
            "validator_id": "validator_1",
            "task_scope": "bounded worker result validation",
            "evidence_refs": [],
            "checklist": ["structural validation passed", "safety gates respected", "evidence supports claims", "output contract satisfied"],
            "pass_threshold": "all_critical_items_pass",
            "forbidden_actions": ["mcp_private_tools", "secrets", "git_state_changes", "destructive_actions", "callbacks_voice_notifications", "final_synthesis"],
            "output_contract": {
                "verdict": "pass|concerns|fail",
                "required_fields": ["checked_items", "evidence_refs", "required_fixes", "escalation_required"],
            },
            "escalation_target": "main_session",
        }
    validator_provider = None
    if required:
        validator_provider = f"{provider}:same_provider_adversarial" if provider_primary and provider != "main-session" else "main_session_or_independent_adapter"
    return {
        "required": required,
        "provider": validator_provider,
        "order": [
            "deterministic_structural_validation",
            "same_provider_adversarial_validation",
            "same_provider_repair_loop",
            "codex_compact_final_review",
            "cross_provider_escalation_only_on_failure",
        ] if provider_primary and required else ["independent_or_main_session_validation"] if required else [],
        "packet": packet,
    }


def dispatch_contract(
    delegated: bool,
    provider: str,
    selector: str,
    effort: str,
    role: str,
    task_class: str,
    planned_workers: int,
    units: list[str],
    provider_warnings: list[str],
) -> dict[str, Any]:
    if not delegated:
        blocked = bool(provider_warnings and provider == "main-session" and task_class != "small-edit")
        return {
            "required": blocked,
            "status": "fanout_blocked" if blocked else "not_required",
            "main_session_policy": "report_block" if blocked else "handle_directly",
            "worker_type": "main_session",
            "launch_before_main_research": False,
            "main_session_may_duplicate_worker_research": False,
            "provider": provider,
            "model_selector": selector,
            "runtime_launch": runtime_launch(provider, selector, effort),
            "planned_workers": 0,
            "work_units": [],
            "fallback_allowed": not blocked,
            "block_reason": "; ".join(provider_warnings) if blocked else None,
            "forbidden_main_session_actions_until_workers_return": [],
        }
    return {
        "required": True,
        "status": "launch_required",
        "main_session_policy": "coordinate_wait_integrate",
        "worker_type": worker_kind(task_class, role),
        "launch_before_main_research": True,
        "main_session_may_duplicate_worker_research": False,
        "provider": provider,
        "model_selector": selector,
        "runtime_launch": runtime_launch(provider, selector, effort),
        "planned_workers": planned_workers,
        "work_units": units,
        "fallback_allowed": False,
        "block_reason": None,
        "forbidden_main_session_actions_until_workers_return": [
            "substantive_research",
            "ssh_diagnostics",
            "filesystem_sweeps",
            "repo_wide_search",
            "parallel_duplicate_analysis",
        ],
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
            "provider_primary": provider_primary_config("main-session", False, False),
            "provider_capability_snapshot": provider_capability_snapshot("main-session", "main-session", "small-edit", False, "low"),
            "structural_validation": structural_validation_packet("main-session", "small-edit", False),
            "provider_primary_packet": None,
            "compact_result_contract": None,
            "fanout": {"policy": "automatic_guarded", "planned_workers": 0, "work_units": [], "rationale": "command_only", "guardrails_applied": ["no_task_to_delegate"], "fallback_reason": None},
            "circuit_breaker": circuit_breaker(worker_failures),
            "warnings": warnings,
        }

    task_prompt = remaining_prompt if command["type"] != "invalid" else prompt
    command_provider_override = command.get("args", {}).get("provider") if command["type"] == "provider_task" else None
    task_class = classify(task_prompt)
    gates = hard_gates(task_prompt, task_class)
    delegation_wanted = should_delegate(task_class, task_prompt, gates)
    effort = reasoning_effort(task_class, task_prompt, next_state)
    breaker = circuit_breaker(worker_failures)
    if breaker["triggered"]:
        warnings.append("circuit_breaker_triggered")
        delegation_wanted = False
    role = role_for(task_class, task_prompt) if delegation_wanted else "main"
    provider_warnings: list[str]
    if breaker["triggered"]:
        provider, selector, provider_warnings = "main-session", "main-session", ["circuit_breaker_triggered:worker_failure_threshold"]
    else:
        provider, selector, provider_warnings = choose_provider(task_prompt, next_state, task_class, effort, delegation_wanted, command_provider_override)
    if provider == "MiniMax" and delegation_wanted and not minimax_tool_agent_enabled():
        provider_warnings.append("fanout_blocked:minimax_tool_agent_disabled")
        provider, selector = "main-session", "main-session"
    warnings.extend(provider_warnings)
    fanout_blocked = False
    delegated = delegation_wanted
    if provider == "main-session":
        fanout_blocked = delegation_wanted and bool(provider_warnings)
        delegated = False
        role = "main"
    planned_workers, units, guardrails = estimate_work_units(task_prompt, task_class, delegated)
    parallelism = "none" if not delegated else "parallel" if planned_workers > 1 else "serial-delegated"
    prompt_provider_hint, _ = requested_provider(task_prompt)
    explicit_provider_primary = bool(command_provider_override or command["type"] == "set_provider_mode" or prompt_provider_hint or next_state["provider_mode"] != "Auto")
    provider_primary = provider_primary_config(provider, explicit_provider_primary, delegated)
    capability_snapshot = provider_capability_snapshot(provider, selector, task_class, delegated, effort)
    validator = validator_for(provider, task_class, role, delegated, effort, provider_primary["active"])
    full_agents = agent_composition_required(task_prompt)
    codex_ref = codex_adapter_required(task_prompt, provider) or next_state["provider_mode"] == "Codex" or "codex" in task_prompt.lower()
    refs = references_to_load(task_prompt, provider, delegated, full_agents, codex_ref, pending_plan)
    capsule = capsule_for(task_class, task_prompt, role, delegated)
    envelope = tool_envelope_for(task_prompt, task_class, role, worker_kind(task_class, role) if delegated else "main_session", effort, planned_workers) if provider == "MiniMax" and delegated else None
    if envelope and provider_primary["active"]:
        envelope["output_contract"] = "CompactResultPacketV1"
        envelope["structural_validation_required"] = True
        envelope["same_provider_validator_required"] = True
    if breaker["triggered"]:
        fanout_blocked = True
        if "circuit_breaker_stopped_new_launches" not in guardrails:
            guardrails.append("circuit_breaker_stopped_new_launches")

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
                "runtime_launch": runtime_launch(provider, selector, effort),
                "tool_envelope": envelope,
                "provider_capability_snapshot": capability_snapshot,
            },
        },
        "orchestration_useful": delegated,
        "orchestration_style": provider_primary["orchestration_style"],
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
        "provider_primary": provider_primary,
        "provider_capability_snapshot": capability_snapshot,
        "structural_validation": structural_validation_packet(provider, task_class, delegated),
        "provider_primary_packet": provider_primary_packet(provider, selector, task_class, delegated, capability_snapshot),
        "compact_result_contract": compact_result_contract(provider, delegated),
        "tool_envelope": envelope,
        "fanout": {
            "policy": provider_primary["orchestration_style"],
            "planned_workers": planned_workers,
            "work_units": units,
            "rationale": "automatic decomposition estimate" if delegated else "main_session_only",
            "guardrails_applied": guardrails,
            "fallback_reason": "; ".join(provider_warnings) if provider_warnings and not delegated else None,
            "blocked": fanout_blocked,
        },
        "dispatch": dispatch_contract(delegated, provider, selector, effort, role, task_class, planned_workers, units, provider_warnings),
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
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if args.sanitize:
        write_json_output(sanitize_prompt(text))
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
    write_json_output(result)


if __name__ == "__main__":
    main()
