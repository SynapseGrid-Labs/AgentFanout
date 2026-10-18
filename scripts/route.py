#!/usr/bin/env python3
"""Deterministic router and persistent multi-wave CLI for AgentFanout."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentfanout.lifecycle import (  # noqa: E402
    apply_task_bindings,
    build_task_units,
    ingest_results,
    initialize_run,
    load_run_state,
    plan_next_wave,
    save_run_state,
)
from agentfanout.project_binding import (  # noqa: E402
    ProjectBindingError,
    path_is_sensitive,
    relative_path_is_sensitive,
    resolve_bounded_file,
    resolve_project_binding,
    validate_project_binding,
)
from agentfanout.provider_registry import (  # noqa: E402
    decision_telemetry,
    load_registry,
    normalize_reasoning,
    profile_for,
    profile_capable,
    provider_config,
    provider_enabled,
    ranked_provider_options,
    requested_model_alias,
    resolve_model,
    risk_for,
)

PAI_DIR = Path(os.getenv("PAI_DIR") or os.getenv("PAI_HOME") or (Path.home() / ".pai"))

PROVIDERS = {"Auto", "MiniMax", "Local", "Codex", "Grok", "Claude"}
EFFORTS = {"Auto", "High"}
DEFAULT_STATE = {
    "provider_mode": "Auto",
    "effort_mode": "Auto",
    "fanout_policy": "automatic_guarded",
    "state_scope": "task_only",
    "force_dispatch": False,
}

VALID_COMMANDS = [
    "AgentFanout <task>",
    "AgentFanout Codex <task>",
    "AgentFanout Grok <task>",
    "AgentFanout Local <task>",
    "AgentFanout Help",
    "AgentFanout Status",
    "AgentFanout Cancel",
]

COMMAND_LINE = re.compile(
    r"^AgentFanout(?:\s+(.*?))?\s*$",
    re.IGNORECASE,
)
COMMAND_PREFIX = re.compile(r"^AgentFanout\b", re.IGNORECASE)
INLINE_COMMAND = re.compile(
    r"\bAgentFanout\s+(Status|Help|Cancel|Codex|Grok|Local)\b",
    re.IGNORECASE,
)

ORCH_TRIGGERS = [
    "agentfanout", "parallel agents", "delegate workers", "spin up workers",
    "worker agents", "run validators", "fan out", "independent work units",
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
    "check memory", "remembered context", "private knowledge base",
]
INFRA_DIAGNOSTIC_CUES = [
    "kokoro", "voice", "tts", "health", "healthcheck", "service", "systemctl",
    "ssh", "remote host", "curl", "dashboard", "live", "readiness", "queue",
    "account", "worker", "daemon", "timer", "port", "endpoint", "api",
]
PUBLIC_WEB_CUES = [
    "public web", "web search", "website", "vendor", "pricing", "docs", "documentation",
    "http://", "https://", "url", "domain", "review site", "compare", "competitor",
]
DASHBOARD_CUES = ["dashboard", "queue", "live readiness", "account readiness", "control panel", "status page"]
PROVIDER_HINT_PATTERNS = {
    "MiniMax": [
        r"\buse\s+minimax\b",
        r"\bwith\s+minimax\b",
        r"\bask\s+minimax\b",
        r"\bdelegate\s+(?:to|with)\s+minimax\b",
        r"\bminimax\s+(?:workers?|review|validator|second-opinion|second opinion)\b",
    ],
    "Local": [
        r"\buse\s+(?:the\s+)?local(?:\s+llm|\s+worker)?\b",
        r"\bwith\s+(?:the\s+)?local(?:\s+llm|\s+worker)?\b",
        r"\blocal\s+(?:llm\s+)?workers?\b",
        r"\bvllm\s+workers?\b",
    ],
    "Codex": [
        r"\buse\s+codex\b",
        r"\bwith\s+codex\b",
        r"\bcodex\s+subagents?\b",
        r"\bcodex\s+(?:workers?|review|validator)\b",
    ],
    "Grok": [
        r"\buse\s+grok\b",
        r"\bwith\s+grok\b",
        r"\bask\s+grok\b",
        r"\bgrok\s+(?:agents?|workers?|review|validator|research)\b",
    ],
    "Claude": [
        r"\buse\s+claude\b",
        r"\bwith\s+claude\b",
        r"\bclaude\s+(?:agents?|workers?|review|validator)\b",
    ],
}
SECRET_CUES = [
    "use the secret", "read the secret", "show the secret", "secret value",
    "api key", "credential", "password", "private key", "access token",
    "auth token", "bearer token", "refresh token",
]
SECRET_CONTEXT_PATTERNS = [
    re.compile(r"\b(?:inspect|read|use|show|find|rotate|update|access|expose)\s+(?:the\s+)?secrets?\b", re.IGNORECASE),
    re.compile(r"\bsecrets?\s+(?:file|store|manager|value|contents?)\b", re.IGNORECASE),
]
NEGATED_SECRET_CONTEXT = re.compile(
    r"\b(?:do\s+not|don't|must\s+not|never|without|no)\s+"
    r"(?:(?:use|read|access|inspect|handle|receive|include|contain|expose)\s+)?"
    r"(?:the\s+)?(?:secrets?|credentials?|passwords?|private\s+keys?|access\s+tokens?|api\s+keys?)"
    r"(?:\s*(?:,|or|and)\s*(?:secrets?|credentials?|passwords?|private\s+keys?|access\s+tokens?|api\s+keys?))*\b",
    re.IGNORECASE,
)
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
    r"(voice server|tts|text-to-speech|localhost:[0-9]{1,5}|localhost callback|/notify|"
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
    # Credential-shaped material is always gated.  Policy statements such as
    # "do not use secrets" are constraints, not evidence that the task needs
    # secret access, so remove only those explicit negated clauses before the
    # semantic cue checks.
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return True
    scoped = NEGATED_SECRET_CONTEXT.sub("", text)
    return (
        contains_any(scoped, SECRET_CUES)
        or any(pattern.search(scoped) for pattern in SECRET_CONTEXT_PATTERNS)
    )


def contains_credential_material(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


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
        "grok": "Grok",
        "claude": "Claude",
    }
    return lookup.get(value.lower(), value)


def default_state() -> dict[str, Any]:
    return dict(DEFAULT_STATE)


def normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return default_state()
    state = default_state()
    # Provider preference is deliberately task-scoped in public v1. Accept old
    # state shapes for compatibility, but never carry a provider forward.
    provider = "Auto"
    effort = str(raw.get("effort_mode", state["effort_mode"])).capitalize()
    state["provider_mode"] = provider
    if effort in EFFORTS:
        state["effort_mode"] = effort
    state["force_dispatch"] = False
    if raw.get("fanout_policy") == "automatic_guarded":
        state["fanout_policy"] = "automatic_guarded"
    state["state_scope"] = "task_only"
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
    if raw.get("provider_mode", "Auto") != "Auto":
        warnings.append("provider_preference_ignored_task_scoped_only")
    if raw.get("force_dispatch", False):
        warnings.append("force_dispatch_ignored_task_scoped_only")
    if "effort_mode" in raw and str(raw["effort_mode"]).capitalize() not in EFFORTS:
        warnings.append("state_invalid_effort_defaulted")
    if "fanout_policy" in raw and raw["fanout_policy"] != "automatic_guarded":
        warnings.append("state_invalid_fanout_policy_defaulted")
    if raw.get("state_scope", "task_only") != "task_only":
        warnings.append("state_scope_task_only")
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


def parse_state_file(path: str | None) -> tuple[dict[str, Any], list[str]]:
    if not path:
        return default_state(), []
    state_path = Path(path).expanduser()
    if not state_path.exists():
        return default_state(), []
    try:
        parsed = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state(), ["state_file_invalid_defaulted"]
    return normalize_state(parsed), state_warnings(parsed)


def write_state_file(path: str | None, state: dict[str, Any]) -> list[str]:
    if not path:
        return []
    state_path = Path(path).expanduser()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=state_path.parent, delete=False) as handle:
            json.dump(normalize_state(state), handle, indent=2)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(state_path)
    except OSError:
        return ["state_file_write_failed"]
    return []


def command_template(command_type: str = "route", name: str | None = None) -> dict[str, Any]:
    return {
        "type": command_type,
        "name": name,
        "args": {},
        "valid_commands": VALID_COMMANDS,
        "error": None,
    }


def command_from_parts(
    word: str,
    modifier: str | None,
    state: dict[str, Any],
    remaining: str,
    warnings: list[str],
    trigger_location: str,
) -> tuple[dict[str, Any], dict[str, Any], str, list[str]]:
    next_state = dict(state)
    normalized = word.lower()
    normalized_modifier = modifier.lower() if modifier else None

    if normalized == "status":
        command = command_template("status", "AgentFanout Status")
        command["args"]["trigger_location"] = trigger_location
        return command, next_state, remaining, warnings
    if normalized == "help":
        command = command_template("help", "AgentFanout Help")
        command["args"]["trigger_location"] = trigger_location
        return command, next_state, remaining, warnings
    if normalized == "cancel":
        command = command_template("cancel", "AgentFanout Cancel")
        command["args"]["trigger_location"] = trigger_location
        return command, default_state(), remaining, warnings
    if normalized in {"minimax", "local", "codex", "grok", "claude"}:
        provider = canonical_provider(normalized)
        next_state = default_state()
        command = command_template("provider_task" if remaining else "provider_command", f"AgentFanout {provider}")
        command["args"]["provider"] = provider
        command["args"]["lifecycle"] = "task_scoped"
        command["args"]["trigger_location"] = trigger_location
        return command, next_state, remaining, warnings
    command = command_template("route", "AgentFanout <task>")
    command["args"]["trigger_location"] = trigger_location
    return command, default_state(), f"{word} {remaining}".strip(), warnings


def extract_command(prompt: str, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, list[str]]:
    lines = prompt.splitlines() or [prompt]
    command_lines = [(index, line.strip()) for index, line in enumerate(lines) if COMMAND_PREFIX.match(line.strip())]
    warnings: list[str] = []
    next_state = dict(state)

    if not command_lines:
        matches = list(INLINE_COMMAND.finditer(prompt))
        if not matches:
            return command_template(), default_state(), prompt, warnings
        match = matches[-1]
        head = match.group(1)
        remaining = (prompt[: match.start()] + prompt[match.end() :]).strip()
        return command_from_parts(head, None, state, remaining, warnings, "inline")
    if len(command_lines) > 1:
        command = command_template("invalid")
        command["error"] = "multiple_agentfanout_commands"
        warnings.append("invalid_command")
        return command, next_state, prompt, warnings

    index, line = command_lines[0]
    body = line[len("AgentFanout"):].strip()
    if not body:
        command = command_template("invalid")
        command["error"] = "invalid_command_syntax"
        warnings.append("invalid_command")
        return command, next_state, prompt, warnings
    head, _, inline_task = body.partition(":")
    if not inline_task:
        head, _, inline_task = body.partition(" ")
    head = head.strip()
    inline_task = inline_task.strip()
    other_lines = "\n".join(line for i, line in enumerate(lines) if i != index).strip()
    remaining = "\n".join(part for part in (inline_task, other_lines) if part).strip()
    if head.lower() in {"minimax", "codex", "grok", "local", "claude", "status", "help", "cancel"}:
        return command_from_parts(head, None, state, remaining, warnings, "full_line")
    return command_template("route"), default_state(), body + (f"\n{other_lines}" if other_lines else ""), warnings


def sanitize_task_text(prompt: str) -> dict[str, Any]:
    kept: list[str] = []
    removed = 0
    for line in prompt.splitlines():
        if FORBIDDEN_PATTERN.search(line):
            removed += 1
            continue
        kept.append(line)
    credential_bearing = contains_credential_material(prompt)
    sanitized = "" if credential_bearing else "\n".join(kept).strip()
    voice_safe = not bool(FORBIDDEN_PATTERN.search(sanitized))
    delegation_safe = voice_safe and not credential_bearing
    return {
        "sanitized_task_text": sanitized,
        "removed_lines": removed,
        "voice_safe": voice_safe,
        "delegation_safe": delegation_safe,
        "credential_bearing": credential_bearing,
        "omitted_reason": "credential_material_detected" if credential_bearing else None,
    }


def sanitize_prompt(prompt: str) -> dict[str, Any]:
    task_result = sanitize_task_text(prompt)
    sanitized = task_result["sanitized_task_text"]
    if sanitized:
        sanitized += "\n\n"
    sanitized += SAFE_DELEGATED_INSTRUCTION
    still_contains = bool(FORBIDDEN_PATTERN.search(sanitized))
    voice_safe = task_result["voice_safe"] and not still_contains
    delegation_safe = task_result["delegation_safe"] and voice_safe
    return {
        "sanitized_prompt": sanitized,
        "removed_lines": task_result["removed_lines"],
        "voice_safe": voice_safe,
        "delegation_safe": delegation_safe,
        "credential_bearing": task_result["credential_bearing"],
        "omitted_reason": task_result["omitted_reason"],
        "safe": delegation_safe,
    }


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
    # A path such as ``docs/architecture.md`` names an input; it does not by
    # itself turn a bounded inspection into an architecture-design task.
    readonly_inspection = bool(re.search(r"\b(?:inspect|read|review|summarize|compare|cross-check)\b", p))
    bounded_file_reference = bool(re.search(
        r"(?:^|\s|[,`])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:md|txt|rst|py|js|mjs|cjs|ts|tsx|jsx|json|toml|ya?ml)(?:$|\s|[,.`:])",
        p,
    ))
    design_action = bool(re.search(
        r"\b(?:create|write|draft|develop|propose|implement|revise|update|change|design|architect|migrate)\b"
        r"[^.\n]{0,48}\b(?:architecture|migration|design|strategy|spec|plan)\b",
        p,
    ))
    if readonly_inspection and bounded_file_reference and not design_action:
        return "bulk-read"
    if re.search(r"\b(?:architecture|migration|design|strategy|spec|plan)\b", p):
        return "plan"
    if "prove" in p or "correctness" in p or "argument" in p or "reason" in p:
        return "reason"
    if contains_any(p, INVESTIGATION_CUES):
        return "bulk-read"
    if "logs" in p or "summarize" in p or "grep" in p or "review" in p or "modules" in p:
        return "bulk-read"
    if "copy" in p or "marketing" in p or "creative" in p or "draft" in p:
        return "creative"
    return "bulk-read" if contains_any(p, ORCH_TRIGGERS) else "small-edit"


def requested_provider(prompt: str) -> tuple[str | None, list[str]]:
    p = prompt.lower()
    warnings: list[str] = []
    if "orchestratormode" in p or "codexagent" in p or "agentorchestrator" in p:
        warnings.append("legacy_trigger_one_turn_hint_only")
    for provider, patterns in PROVIDER_HINT_PATTERNS.items():
        if any(re.search(pattern, p) for pattern in patterns):
            return provider, warnings
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
    explicit_patterns = (
        ("max", r"\bmax(?:imum)?(?:[- ](?:reasoning|effort))?\b|\b(?:reasoning|effort)(?: level)?\s*[:=]?\s*max(?:imum)?\b"),
        ("ultra", r"\bultra(?:[- ](?:reasoning|effort))?\b|\b(?:reasoning|effort)(?: level)?\s*[:=]?\s*ultra\b"),
        ("xhigh", r"\b(?:extra[- ]?high|xhigh)(?:[- ](?:reasoning|effort))?\b|\b(?:reasoning|effort)(?: level)?\s*[:=]?\s*(?:extra[- ]?high|xhigh)\b"),
        ("high", r"\bhigh[- ](?:reasoning|effort)\b|\b(?:reasoning|effort)(?: level)?\s*[:=]?\s*high\b|\bwith high reasoning\b"),
        ("medium", r"\bmedium[- ](?:reasoning|effort)\b|\b(?:reasoning|effort)(?: level)?\s*[:=]?\s*medium\b|\bwith medium reasoning\b"),
        ("low", r"\b(?:light|low)[- ](?:reasoning|effort)\b|\b(?:reasoning|effort)(?: level)?\s*[:=]?\s*(?:light|low)\b|\bwith (?:light|low) reasoning\b"),
    )
    for normalized, pattern in explicit_patterns:
        if re.search(pattern, p):
            return normalized
    if state["effort_mode"] == "High":
        return "xhigh" if contains_any(p, HIGH_RISK_CUES) or task_class in {"security-review", "plan", "refactor", "reason"} else "high"
    if "critical" in p or "high stakes" in p or "xhigh" in p:
        return "xhigh"
    if task_class in {"plan", "refactor", "security-review", "reason"}:
        return "high"
    if task_class in {"debug", "test-gen", "data-xform"}:
        return "medium"
    return "low"


def astra_routing_policy(prompt: str, worker_failures: int) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Return the model alias and auditable authorization for Astra."""

    alias = requested_model_alias(prompt)
    if alias and alias.get("provider") == "Codex" and alias.get("profile") == "astra":
        selected = dict(alias)
        selected["selection_reason"] = "explicit_user_request:astra"
        return selected, {
            "schema": "AstraCostGateV1",
            "selected": True,
            "authorized": True,
            "authorization_source": "explicit_user_request",
            "reason": "explicit_user_request:astra",
            "required_model_id": "gpt-6-astra",
            "max_parallel_workers": 1,
            "resolved_model_ack_required": True,
        }

    escalation_enabled = env_flag("AGENT_FANOUT_ALLOW_ASTRA_ESCALATION")
    escalation_reason = os.getenv("AGENT_FANOUT_ASTRA_ESCALATION_REASON", "").strip()
    escalation_attempted = escalation_enabled or bool(escalation_reason)
    if (
        escalation_enabled
        and worker_failures == 2
        and escalation_reason == "sol_validation_failed_twice"
    ):
        selected = {
            "alias": "astra",
            "provider": "Codex",
            "profile": "astra",
            "selection_reason": "controlled_escalation:sol_validation_failed_twice",
        }
        return selected, {
            "schema": "AstraCostGateV1",
            "selected": True,
            "authorized": True,
            "authorization_source": "host_controlled_escalation",
            "reason": "controlled_escalation:sol_validation_failed_twice",
            "required_model_id": "gpt-6-astra",
            "max_parallel_workers": 1,
            "resolved_model_ack_required": True,
        }

    denied_reason = None
    if escalation_attempted:
        if not escalation_enabled:
            denied_reason = "astra_escalation_enable_flag_required"
        elif worker_failures != 2:
            denied_reason = "astra_escalation_requires_exactly_two_sol_validation_failures"
        else:
            denied_reason = "astra_escalation_reason_invalid"
    return alias, {
        "schema": "AstraCostGateV1",
        "selected": False,
        "authorized": False,
        "authorization_source": None,
        "reason": denied_reason or "not_requested",
        "required_model_id": "gpt-6-astra",
        "max_parallel_workers": 1,
        "resolved_model_ack_required": True,
        "warning": denied_reason,
    }


def role_for(task_class: str, prompt: str) -> str:
    p = prompt.lower()
    if task_class in {"bulk-read", "bulk-classify", "data-xform", "creative", "plan", "security-review", "reason"}:
        if task_class in {"data-xform", "creative"} and any(cue in p for cue in ["edit file", "write file", "save to", "modify file", "patch"]):
            return "worker"
        return "explorer"
    if task_class == "debug" and not any(x in p for x in ["fix", "edit", "patch", "modify"]):
        return "explorer"
    if task_class in {"test-gen", "refactor", "debug"}:
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


def should_delegate(task_class: str, prompt: str, gates: dict[str, bool], state: dict[str, Any], command: dict[str, Any]) -> bool:
    if gates["requires_mcp_private_tools"] or gates["requires_secrets"] or gates["requires_git_state_change"] or gates["requires_destructive_action"]:
        return False
    if task_class == "small-edit":
        return False
    provider_hint, _ = requested_provider(prompt)
    explicit = command.get("type") == "provider_task" or provider_hint is not None or any(cue in prompt.lower() for cue in [
        "agentfanout", "parallel agents", "delegate workers", "spin up workers",
        "worker agents", "run validators", "fan out", "independent work units",
    ])
    separable = any(cue in prompt.lower() for cue in [
        "independent", "separate", "each of", "multiple modules", "four modules",
        "api, ui", "api, database", "ui, database", "database, tests",
    ])
    complex_class = task_class in {"bulk-read", "bulk-classify", "data-xform", "debug", "plan", "refactor", "test-gen", "reason", "security-review"}
    if env_flag("AGENT_FANOUT_RUNTIME_MODE") and complex_class:
        return True
    return explicit or (separable and complex_class)


def anti_waste_override(prompt: str, state: dict[str, Any], command: dict[str, Any]) -> tuple[bool, str | None]:
    """Return whether the user explicitly accepts worker-launch overhead."""

    lowered = prompt.lower()
    if state.get("force_dispatch"):
        return True, "force_dispatch"
    # Selecting a provider or asking for parallelism chooses *how* eligible
    # work should run; it does not authorize a predictably more expensive plan.
    # Keep economic consent independent so an atomic host can bind an explicit
    # provider without accidentally disabling the anti-waste gate.
    if any(cue in lowered for cue in (
        "i accept orchestration overhead",
        "accept worker launch overhead",
        "force orchestration despite overhead",
        "run fanout even if direct execution is cheaper",
    )):
        return True, "explicit_overhead_acceptance"
    return False, None


def bounded_unit_input_token_estimates(units: list[str]) -> list[int]:
    """Estimate exact bounded file payloads without granting new authority.

    The later task-binding pass re-reads and digest-binds every accepted file;
    these values affect only cost planning. Non-file/symbolic work keeps a zero
    payload estimate and remains subject to its adapter binding checks.
    """

    if not any(isinstance(unit, str) and unit.startswith("file:") for unit in units):
        return [0 for _unit in units]
    try:
        binding = resolve_project_binding(explicit_root=None)
    except ProjectBindingError:
        return [0 for _unit in units]
    estimates: list[int] = []
    for unit in units:
        if not isinstance(unit, str) or not unit.startswith("file:"):
            estimates.append(0)
            continue
        try:
            _path, content = resolve_bounded_file(binding, unit)
        except ProjectBindingError:
            estimates.append(0)
            continue
        estimates.append(max(1, (len(content.encode("utf-8")) + 3) // 4))
    return estimates


def hard_independent_validation_requested(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(cue in lowered for cue in (
        "independent validation required",
        "independent validator required",
        "must validate independently",
        "must be independently validated",
        "cross-provider validation required",
    ))


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


def _future_timestamp(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def local_capability_attestation(selector: str, task_class: str | None, effort: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate host-issued local-model capability evidence for this exact launch."""

    raw = os.getenv("AGENT_FANOUT_LOCAL_ATTESTATION_JSON", "").strip()
    if not raw:
        return None, ["capability_attestation_missing"]
    try:
        attestation = json.loads(raw)
    except json.JSONDecodeError:
        return None, ["capability_attestation_invalid_json"]
    if not isinstance(attestation, dict) or attestation.get("schema") != "LocalCapabilityAttestationV1":
        return None, ["capability_attestation_invalid_schema"]
    attestation_id = attestation.get("attestation_id")
    if not isinstance(attestation_id, str) or not re.fullmatch(r"localcap_[a-f0-9]{32}", attestation_id):
        return None, ["capability_attestation_id_invalid"]
    if attestation.get("source") != "host" or attestation.get("issuer") != "host-runtime":
        return None, ["capability_attestation_not_host_issued"]
    if attestation.get("selector_alias") != selector:
        return None, [f"capability_selector_mismatch:{attestation.get('selector_alias')}!={selector}"]
    configured_selector = os.getenv("AGENT_FANOUT_LOCAL_MODEL_ALIAS", "local-worker").strip()
    if configured_selector != selector:
        return None, [f"configured_selector_mismatch:{configured_selector}!={selector}"]
    configured_model = os.getenv("AGENT_FANOUT_LOCAL_MODEL_ID", "").strip()
    exact_model = attestation.get("exact_model_id")
    if not isinstance(exact_model, str) or not exact_model or exact_model.lower() == "unknown":
        return None, ["capability_exact_model_missing"]
    if not configured_model or configured_model != exact_model:
        return None, ["capability_exact_model_mismatch"]

    now = datetime.now(timezone.utc)
    observed = _timestamp(attestation.get("observed_at"))
    expires = _timestamp(attestation.get("expires_at"))
    try:
        max_age = min(86_400, max(1, int(os.getenv("AGENT_FANOUT_LOCAL_ATTESTATION_MAX_AGE_SECONDS", "3600"))))
    except ValueError:
        max_age = 3600
    if observed is None or observed > now or (now - observed).total_seconds() > max_age:
        return None, ["capability_attestation_observation_stale"]
    if expires is None or expires <= now:
        return None, ["capability_attestation_expired"]

    required_mode = os.getenv("AGENT_FANOUT_LOCAL_REQUIRED_MODE", "").strip()
    if not required_mode or attestation.get("active_mode") != required_mode:
        return None, ["capability_mode_mismatch"]
    context_window = attestation.get("context_window")
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        return None, ["capability_context_window_invalid"]
    configured_context = os.getenv("AGENT_FANOUT_LOCAL_CONTEXT_WINDOW", "").strip()
    if configured_context:
        try:
            if int(configured_context) != context_window:
                return None, ["capability_context_window_mismatch"]
        except ValueError:
            return None, ["capability_context_window_mismatch"]

    evaluated_classes = attestation.get("evaluated_task_classes")
    if not isinstance(evaluated_classes, list) or not all(isinstance(item, str) for item in evaluated_classes):
        return None, ["capability_task_classes_invalid"]
    if task_class and task_class not in evaluated_classes and "*" not in evaluated_classes:
        return None, [f"task_class_not_evaluated:{task_class}"]
    supported_efforts = attestation.get("supported_reasoning_efforts")
    normalized_effort = normalize_reasoning(effort)
    if not isinstance(supported_efforts, list) or normalized_effort not in supported_efforts:
        return None, [f"reasoning_effort_not_evaluated:{normalized_effort}"]
    max_risk = attestation.get("max_risk")
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    required_risk = risk_for(task_class or "bulk-read", normalized_effort)
    if max_risk not in risk_order or risk_order[max_risk] < risk_order[required_risk]:
        return None, [f"evaluated_risk_insufficient:{required_risk}>{max_risk}"]
    tools = attestation.get("tools")
    if not isinstance(tools, list) or not all(isinstance(tool, str) and tool for tool in tools):
        return None, ["capability_tools_invalid"]
    supports_editing = attestation.get("supports_file_editing")
    if not isinstance(supports_editing, bool):
        return None, ["capability_file_editing_invalid"]
    if local_tool_agent_enabled() and (not supports_editing or "scoped_patch" not in tools):
        return None, ["capability_tool_agent_mismatch"]
    if local_hosted_no_tool_enabled() and (tools or supports_editing):
        return None, ["capability_no_tool_lane_mismatch"]
    return attestation, []


def readiness(
    provider: str, selector: str, task_class: str | None = None,
    effort: str = "low", profile: str | None = None,
    *, allow_explicit_diagnostic: bool = False,
) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    adapter_files = {
        "MiniMax": ROOT / "adapters" / "MiniMax.md",
        "Local": ROOT / "adapters" / "LocalLLM.md",
        "Codex": ROOT / "adapters" / "Codex.md",
        "Grok": ROOT / "adapters" / "Grok.md",
        "Claude": ROOT / "adapters" / "Claude.md",
    }
    enabled, reason = provider_enabled(provider)
    if not enabled:
        config = provider_config(provider) or {}
        if not (
            allow_explicit_diagnostic
            and provider == "Codex"
            and config.get("explicit_diagnostic_routing") is True
        ):
            return False, [f"provider_unavailable:{provider.lower()}:{reason}"]
        warnings.append("provider_runtime_diagnostic_only:codex")
    if task_class:
        selected_profile = profile or profile_for(provider, task_class, effort)
        capable, capability_reason = profile_capable(provider, selected_profile, task_class, effort)
        if not capable:
            return False, [f"provider_unavailable:{provider.lower()}:{capability_reason}"]
        resolution = resolve_model(provider, selected_profile)
        resolved_model = resolution.get("resolved_model")
        if resolved_model and resolved_model in resolution.get("forbidden_model_ids", []):
            return False, [
                f"provider_unavailable:{provider.lower()}:resolved_model_forbidden:{resolved_model}"
            ]
        required_model = resolution.get("required_model_id")
        if resolved_model and required_model and resolved_model != required_model:
            return False, [
                f"provider_unavailable:{provider.lower()}:exact_model_required:{required_model}"
            ]
    adapter_path = adapter_files.get(provider)
    if adapter_path and not adapter_path.exists():
        return False, [f"provider_unavailable:{provider.lower()}:adapter_missing"]
    try:
        approved_project_root()
    except ValueError as exc:
        return False, [f"provider_unavailable:{provider.lower()}:{exc}"]
    if provider in {"MiniMax", "Local"}:
        try:
            existing_read_roots(os.getcwd(), provider)
        except ValueError as exc:
            return False, [f"provider_unavailable:{provider.lower()}:{exc}"]
    if provider == "MiniMax":
        if not minimax_tool_agent_enabled():
            return False, ["provider_unavailable:minimax:tool_agent_disabled"]
        tool = pai_skill_file("MiniMax", "Tools", "MiniMaxAgent.ts")
        if env_flag("AGENT_FANOUT_MINIMAX_AVAILABLE", "AGENT_ORCH_MINIMAX_AVAILABLE"):
            return True, warnings
        if not tool.exists():
            return False, ["provider_unavailable:minimax:tool_missing"]
    if provider == "Local":
        if local_tool_agent_enabled() and local_hosted_no_tool_enabled():
            return False, ["provider_unavailable:local:execution_modes_mutually_exclusive"]
        if not env_flag("AGENT_FANOUT_LOCAL_AVAILABLE", "AGENT_ORCH_LOCAL_AVAILABLE"):
            return False, ["provider_unavailable:local:service_reachability_unknown"]
        runtime = local_runtime_config()
        if not runtime["endpoint"]:
            return False, ["provider_unavailable:local:endpoint_missing"]
        _, attestation_errors = local_capability_attestation(selector, task_class, effort)
        if attestation_errors:
            return False, [f"provider_unavailable:local:{error}" for error in attestation_errors]
        _, bridge_errors = local_bridge_observation(runtime)
        if bridge_errors:
            return False, [f"provider_unavailable:local:{error}" for error in bridge_errors]
        if local_hosted_no_tool_enabled():
            from agentfanout.provider_adapters import (
                ProviderAdapterError, local_host_evidence_from_environment,
            )
            try:
                local_host_evidence_from_environment(
                    selector=selector, task_class=task_class or "bulk-read",
                    reasoning_effort=effort,
                )
            except ProviderAdapterError as exc:
                return False, [f"provider_unavailable:local:{exc}"]
        if not local_tool_agent_enabled() and not local_hosted_no_tool_enabled():
            return False, ["provider_unavailable:local:vllm_agent_bridge_required"]
    if provider == "Codex":
        compatibility_rejection = os.getenv(
            "AGENT_FANOUT_CODEX_COMPATIBILITY_REJECTED", "",
        ).strip()
        if compatibility_rejection:
            return False, [
                f"provider_unavailable:codex:runtime_compatibility_rejected:{compatibility_rejection}"
            ]
        codex_runtime_detected = bool(os.getenv("CODEX_THREAD_ID") or os.getenv("PAI_CODEX_LAUNCH_ID"))
        if not codex_runtime_detected and not env_flag("AGENT_FANOUT_CODEX_SUBAGENTS", "AGENT_ORCH_CODEX_SUBAGENTS"):
            return False, ["provider_unavailable:codex:native_subagents_unknown"]
    if provider == "Grok":
        if not env_flag("AGENT_FANOUT_GROK_AVAILABLE"):
            return False, ["provider_unavailable:grok:adapter_not_configured"]
    if provider == "Claude":
        if not env_flag("AGENT_FANOUT_CLAUDE_AVAILABLE", "AGENT_ORCH_CLAUDE_AVAILABLE"):
            return False, ["provider_unavailable:claude:adapter_not_configured"]
    if not selector:
        warnings.append(f"provider_unavailable:{provider.lower()}:selector_unmapped")
        return False, warnings
    return True, warnings


def local_runtime_config() -> dict[str, Any]:
    endpoint = os.getenv("AGENT_FANOUT_LOCAL_ENDPOINT", "").strip()
    host = os.getenv("AGENT_FANOUT_LOCAL_HOST", "").strip()
    try:
        port = int(os.getenv("AGENT_FANOUT_LOCAL_PORT", "0")) or None
    except ValueError:
        port = None
    model_alias = os.getenv("AGENT_FANOUT_LOCAL_MODEL_ALIAS", "local-worker")
    required_mode = os.getenv("AGENT_FANOUT_LOCAL_REQUIRED_MODE", "").strip()
    runtime_path = os.getenv("AGENT_FANOUT_LOCAL_RUNTIME_PATH", "").strip()
    return {
        "endpoint": endpoint,
        "host": host,
        "port": port,
        "model_alias": model_alias,
        "required_mode": required_mode,
        "runtime_path": runtime_path,
        "health_check": f"{endpoint.rstrip('/')}/models" if endpoint else None,
        "worker_command": os.getenv("AGENT_FANOUT_LOCAL_WORKER_COMMAND", "bun $HOME/.pai/skills/VLLMWorker/Tools/Worker.ts --json --stdin"),
        "tool_agent_command": os.getenv("AGENT_FANOUT_LOCAL_TOOL_AGENT_COMMAND", "bun $HOME/.pai/skills/VLLMWorker/Tools/VLLMAgent.ts --request-json - --json"),
        "mode_check": os.getenv("AGENT_FANOUT_LOCAL_MODE_CHECK_COMMAND") or None,
    }


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
            "available_tools": ["ToolEnvelopeV1", "bounded_repo_read", "public_web_fetch", "configured_readonly_shell"] if tool_agent else [],
            "supports_file_editing": tool_agent,
            "validation_eligible": True,
            "snapshot_source": "agentfanout_env_and_adapter",
            "tool_runtime": "MiniMaxAgent" if tool_agent else "blocked",
        }
    if provider == "Local":
        local_runtime = local_runtime_config()
        tool_agent = local_tool_agent_enabled()
        no_tool_hosted = local_hosted_no_tool_enabled()
        attestation, attestation_errors = local_capability_attestation(selector, task_class, effort)
        bridge_observation, bridge_errors = local_bridge_observation(local_runtime)
        return {
            "provider": provider,
            "model_selector": selector,
            "available_tools": list(attestation.get("tools", [])) if attestation else [],
            "supports_file_editing": bool(attestation and attestation.get("supports_file_editing") and tool_agent),
            "validation_eligible": not attestation_errors and not bridge_errors and (tool_agent or no_tool_hosted),
            "snapshot_source": "host_local_capability_attestation_and_bridge_observation",
            "tool_runtime": "VLLMAgent" if tool_agent else "HostedLocalNoTool" if no_tool_hosted else "VLLMWorker",
            "required_live_checks": ["selector_alias", "exact_model_id", "fresh_observation", "active_mode", "context_window", "task_class", "risk", "tools"],
            "runtime": local_runtime,
            "capability_attestation": attestation,
            "attestation_errors": attestation_errors,
            "bridge_observation": bridge_observation,
            "bridge_errors": bridge_errors,
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
    if provider == "Grok":
        return {
            "provider": provider,
            "model_selector": selector,
            "available_tools": ["adapter_configured_tools"],
            "supports_file_editing": env_flag("AGENT_FANOUT_GROK_FILE_EDITING"),
            "validation_eligible": True,
            "snapshot_source": "grok_adapter_env",
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


def selector_for(provider: str, effort: str, task_class: str, alias: dict[str, str] | None = None) -> str:
    if provider == "main-session":
        return "main-session"
    profile = profile_for(provider, task_class, effort, alias)
    return str(resolve_model(provider, profile, alias).get("selector") or "")


def runtime_launch(
    provider: str,
    selector: str,
    effort: str,
    selection_reason: str | None = None,
) -> dict[str, Any]:
    if provider == "Codex":
        model_preference = {
            "current-fast-mini": "luna",
            "current-balanced": "terra",
            "current-frontier": "sol",
            "astra-explicit": "astra",
        }.get(selector, "provider-default")
        cost_tier = {
            "current-fast-mini": "cheap",
            "current-balanced": "balanced",
            "current-frontier": "frontier",
            "astra-explicit": "astra_explicit",
        }.get(selector, "unknown")
        codex_native: dict[str, Any] = {
            "agent_type": "explorer" if selector == "current-fast-mini" else "default",
            "cost_tier": cost_tier,
            "model_preference": model_preference,
            "reasoning_effort": effort,
            "must_pass_model_override": True,
            "do_not_use_default_frontier_for_fast_mini": selector == "current-fast-mini",
            "resolved_model_ack_required": True,
            "required_launch_audit_fields": ["resolved_model", "selection_reason"],
            "selection_reason": selection_reason or "registry_profile_selection",
        }
        if selector == "current-fast-mini":
            codex_native["required_model_capability"] = "mini_or_fast"
            codex_native["fallback_model_preference"] = "lowest_available_codex_mini_or_fast_model"
        elif selector == "current-frontier":
            codex_native["required_model_capability"] = "sol_or_equivalent_non_astra"
            codex_native["forbidden_model_ids"] = ["gpt-6-astra"]
        elif selector == "current-balanced":
            codex_native["required_model_capability"] = "terra_or_equivalent_balanced"
        elif selector == "astra-explicit":
            codex_native["required_model_capability"] = "exact_gpt_6_astra"
            codex_native["required_model_id"] = "gpt-6-astra"
            codex_native["max_parallel_workers"] = 1
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
            "forbidden_fallbacks": ["main_session_implementation", "local_worker_substitution"],
            "failure_policy": "denied_timeout_or_partial_advisory_result_requires_retry_or_fanout_blocked",
        }
    if provider == "Local":
        if local_tool_agent_enabled():
            launch["local_vllm_worker"] = {
                **local_runtime_config(),
                "required_flags": [
                    "AGENT_FANOUT_LOCAL_AVAILABLE=1",
                    "AGENT_FANOUT_LOCAL_ATTESTATION_JSON=<host-issued LocalCapabilityAttestationV1>",
                    "AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON=<host-measured bridge observation>",
                    "AGENT_FANOUT_LOCAL_TOOL_AGENT=1",
                ],
                "host_bridge": {
                    "issuer": "scripts/local-bridge-issue.mjs",
                    "bridge_module": "$HOME/.pai/skills/VLLMWorker/Tools/broker/host_bridge.ts",
                    "attestation_owner": "host_only",
                    "usage_correlation": "host_only_never_serialized_to_worker_request",
                },
                "forbidden_fallbacks": ["minimax_substitution", "main_session_implementation"],
                "failure_policy": "health_mode_or_capability_failure_requires_fanout_blocked",
            }
            launch["local_tool_agent"] = {
                "command": "bun $HOME/.pai/skills/VLLMWorker/Tools/VLLMAgent.ts --request-json - --json",
                "stdin_request": True,
                "required_flag": "AGENT_FANOUT_LOCAL_TOOL_AGENT=1",
                "text_fallback_command": "bun $HOME/.pai/skills/VLLMWorker/Tools/Worker.ts --json --stdin",
                "forbidden_fallbacks": ["minimax_substitution", "main_session_implementation"],
                "failure_policy": "denied_timeout_or_partial_advisory_result_requires_retry_or_fanout_blocked",
            }
        elif local_hosted_no_tool_enabled():
            launch["local_hosted_no_tool"] = {
                "endpoint": local_runtime_config()["endpoint"],
                "model_alias": local_runtime_config()["model_alias"],
                "adapter": "agentfanout.local.http.no-tools.bounded-file",
                "input_mode": "bounded_file_text",
                "required_flag": "AGENT_FANOUT_LOCAL_HOSTED_NO_TOOL=1",
                "required_evidence": [
                    "AGENT_FANOUT_LOCAL_ATTESTATION_JSON=<host-issued LocalCapabilityAttestationV1>",
                    "AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON=<host-measured bridge observation>",
                ],
                "endpoint_policy": "https_or_explicit_numeric_loopback_http",
                "auth_mode": "none",
                "project_root_mounted": False,
                "tools_enabled": False,
                "forbidden_fallbacks": ["main_session_implementation", "tool_capable_local_substitution"],
                "failure_policy": "endpoint_model_usage_or_evidence_failure_requires_fanout_blocked",
            }
    return launch


def minimax_tool_agent_enabled() -> bool:
    return env_flag("AGENT_FANOUT_MINIMAX_TOOL_AGENT")


def local_tool_agent_enabled() -> bool:
    return env_flag("AGENT_FANOUT_LOCAL_TOOL_AGENT")


def local_hosted_no_tool_enabled() -> bool:
    return env_flag("AGENT_FANOUT_LOCAL_HOSTED_NO_TOOL")


def minimax_agent_request_for(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if not envelope:
        return None
    return {
        "version": 1,
        "request_id": envelope.get("request_id", "agentfanout-minimax"),
        "envelope": envelope,
    }


def local_agent_request_for(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if not envelope:
        return None
    runtime = local_runtime_config()
    observation, errors = local_bridge_observation(runtime)
    if errors or observation is None:
        raise ValueError("local_bridge_observation_invalid:" + ";".join(errors))
    bridge_envelope = vllm_bridge_envelope(envelope)
    evaluation = vllm_bridge_evaluation(bridge_envelope)
    request = {
        "version": 1,
        "request_id": bridge_envelope["request_id"],
        "model_alias": runtime["model_alias"],
        "endpoint": runtime["endpoint"],
        "evaluation": evaluation,
        "capability": {},
        "envelope": bridge_envelope,
    }
    payload = {
        "schema": "AgentFanoutLocalBridgeLaunchInputV1",
        "request": request,
        "observation": observation,
        "correlation_id": f"afcorr:{uuid.uuid4().hex}",
    }
    issuer = Path(os.getenv("AGENT_FANOUT_LOCAL_BRIDGE_ISSUER", ROOT / "scripts" / "local-bridge-issue.mjs")).expanduser()
    if not issuer.is_file():
        raise ValueError("local_bridge_issuer_missing")
    try:
        proc = subprocess.run(
            ["bun", str(issuer)], input=json.dumps(payload), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("local_bridge_issuer_unavailable") from exc
    if proc.returncode != 0:
        raise ValueError("local_bridge_issuance_failed")
    try:
        issued = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("local_bridge_issuer_invalid_output") from exc
    if not isinstance(issued, dict) or issued.get("schema") != "AgentFanoutLocalBridgeLaunchV1" or not isinstance(issued.get("request"), dict) or not isinstance(issued.get("usage_correlation"), dict):
        raise ValueError("local_bridge_issuer_invalid_output")
    correlation = issued["usage_correlation"]
    issued_capability = issued["request"].get("capability", {})
    if (
        correlation.get("request_id") != request["request_id"]
        or not isinstance(correlation.get("correlation_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", correlation["correlation_id"])
        or not isinstance(correlation.get("launch_contract_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", correlation["launch_contract_sha256"])
        or correlation["launch_contract_sha256"] != issued_capability.get("launch_contract_sha256")
    ):
        raise ValueError("local_bridge_correlation_invalid")
    return issued


def local_bridge_observation(runtime: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    raw = os.getenv("AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON", "").strip()
    if not raw:
        return None, ["bridge_observation_missing"]
    try:
        observation = json.loads(raw)
    except json.JSONDecodeError:
        return None, ["bridge_observation_invalid_json"]
    if not isinstance(observation, dict):
        return None, ["bridge_observation_invalid"]
    required = {"model_alias", "model_id", "endpoint", "context_window", "profiles", "checked_at", "expires_at", "source"}
    if set(observation) != required:
        return None, ["bridge_observation_shape_invalid"]
    if observation["model_alias"] != runtime["model_alias"] or observation["model_id"] != os.getenv("AGENT_FANOUT_LOCAL_MODEL_ID", "").strip() or observation["endpoint"].rstrip("/") != runtime["endpoint"].rstrip("/"):
        return None, ["bridge_observation_identity_mismatch"]
    if isinstance(observation["context_window"], bool) or not isinstance(observation["context_window"], int) or observation["context_window"] <= 0 or not isinstance(observation["profiles"], list) or not all(isinstance(item, str) and item for item in observation["profiles"]):
        return None, ["bridge_observation_capability_invalid"]
    checked_at = _timestamp(observation["checked_at"])
    expires_at = _timestamp(observation["expires_at"])
    now = datetime.now(timezone.utc)
    if not isinstance(observation["source"], str) or not observation["source"].strip() or checked_at is None or expires_at is None:
        return None, ["bridge_observation_freshness_invalid"]
    max_age = timedelta(minutes=15)
    max_future_skew = timedelta(minutes=1)
    if checked_at > now + max_future_skew or now - checked_at > max_age or expires_at <= now or expires_at - checked_at > max_age + max_future_skew:
        return None, ["bridge_observation_stale"]
    return observation, []


def vllm_bridge_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    limits = envelope.get("limits", {})
    return {
        "version": 1,
        "request_id": envelope["request_id"],
        "mode": envelope["mode"],
        "workdir": envelope["workdir"],
        "prompt": envelope["prompt"],
        "tools": list(envelope["tools"]),
        "capability_profiles": list(envelope["capability_profiles"]),
        "read_roots": list(envelope["read_roots"]),
        "write_files": list(envelope["write_files"]),
        "assigned_files": list(envelope["assigned_files"]),
        "shell_allowlist": list(envelope["shell_allowlist"]),
        "private_knowledge": {"enabled": False, "tools": [], "allowed_sections": [], "max_results": 10},
        "limits": {
            "max_steps": min(8, int(limits.get("max_steps", 8))),
            "max_total_tokens": min(64_000, int(limits.get("max_total_tokens", 64_000))),
            "max_workers": min(16, int(limits.get("max_workers", 1))),
            "max_parallel_tools": min(8, int(limits.get("max_parallel_tools", 1))),
            "max_file_bytes": min(4_194_304, int(limits.get("max_file_bytes", 250_000))),
            "max_tool_output_bytes": min(262_144, int(limits.get("max_tool_output_bytes", 65_536))),
            "max_transcript_bytes": min(4_194_304, int(limits.get("max_transcript_bytes", 1_048_576))),
            "timeout_ms": min(300_000, int(limits.get("timeout_ms", 300_000))),
        },
        "deny": ["secrets", "private_account_side_effects", "git_state_mutation", "destructive_actions", "deploys", "final_user_synthesis"],
        "validator_required": bool(envelope["validator_required"]),
        "output_contract": envelope["output_contract"],
    }


def vllm_bridge_evaluation(envelope: dict[str, Any]) -> dict[str, str]:
    tools = set(envelope["tools"])
    has_write = "apply_scoped_patch" in tools
    has_shell = bool({"run_readonly_command", "run_verification_command"} & tools)
    has_network = any(item.startswith(("github_", "gitea_", "private_kb_")) for item in tools) or "public_web_fetch" in tools
    orchestrator = envelope["mode"] == "orchestrator"
    return {
        "task_class": envelope["mode"],
        "risk_class": "high" if orchestrator or has_write else "medium" if has_shell or has_network else "low",
        "tool_class": "orchestration" if orchestrator else "write" if has_write else "restricted_shell" if has_shell else "read_only",
    }


def _approved_root_allowlist() -> set[Path]:
    approved: set[Path] = set()
    for item in env_csv("AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST"):
        try:
            approved.add(Path(item).expanduser().resolve(strict=True))
        except OSError as exc:
            raise ValueError("approved_root_allowlist_invalid") from exc
    return approved


def approved_project_root() -> Path:
    try:
        return validate_project_binding(resolve_project_binding(explicit_root=None))
    except ProjectBindingError as exc:
        raise ValueError(str(exc)) from exc


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def existing_read_roots(workdir: str, provider: str) -> list[str]:
    project_root = approved_project_root()
    roots = [project_root]
    configured_name = "AGENT_FANOUT_LOCAL_READ_ROOTS" if provider == "Local" else "AGENT_FANOUT_EXTERNAL_READ_ROOTS"
    roots.extend(Path(item) for item in env_csv(configured_name))
    seen: set[str] = set()
    resolved: list[str] = []
    for root in roots:
        try:
            path = root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("configured_read_root_invalid") from exc
        if not path.is_dir() or not _within(path, project_root):
            raise ValueError("configured_read_root_outside_project")
        relative = path.relative_to(project_root)
        if path_is_sensitive(path) or relative_path_is_sensitive(relative):
            raise ValueError("configured_read_root_sensitive")
        rendered = str(path)
        if rendered not in seen:
            seen.add(rendered)
            resolved.append(rendered)
    return resolved


def capability_profiles_for(task_prompt: str, task_class: str, provider: str) -> list[str]:
    lowered = task_prompt.lower()
    profiles = ["repo_read"]
    if contains_any(lowered, PUBLIC_WEB_CUES):
        profiles.append("public_web_read")
    if task_class in {"bulk-read", "debug", "plan"} and contains_any(lowered, INFRA_DIAGNOSTIC_CUES):
        profiles.append("infra_readonly")
    if contains_any(lowered, DASHBOARD_CUES):
        profiles.append("dashboard_readonly")
    if task_class in {"bulk-read", "plan", "security-review", "reason"}:
        profiles.append("review_only")
    return list(dict.fromkeys(profiles))


def needs_readonly_infra_diagnostics(task_prompt: str, task_class: str, profiles: list[str] | None = None) -> bool:
    lowered = task_prompt.lower()
    return "infra_readonly" in (profiles or []) or (task_class in {"bulk-read", "debug", "plan"} and contains_any(lowered, INFRA_DIAGNOSTIC_CUES))


def env_csv(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def readonly_infra_shell_entries(read_roots: list[str], workdir: str) -> list[dict[str, Any]]:
    ssh_host = os.getenv("AGENT_FANOUT_INFRA_SSH_HOST", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", ssh_host):
        ssh_host = ""
    services = [
        service for service in env_csv("AGENT_FANOUT_INFRA_SERVICES")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}", service)
    ]
    health_urls = [
        url for url in env_csv("AGENT_FANOUT_HEALTH_URLS") + env_csv("AGENT_FANOUT_DASHBOARD_HEALTH_URLS")
        if safe_health_url(url)
    ]
    entries: list[dict[str, Any]] = []
    if ssh_host:
        entries.append(
            {
                "id": "ssh-configured-host-readonly",
                "cmd": "ssh",
                "policy": "readonly",
                "cwd_roots": read_roots or [workdir],
                "allowed_args_regex": rf"^{re.escape(ssh_host)} hostname$",
                "timeout_ms": 30000,
            }
        )
    if ssh_host and services:
        service_pattern = "|".join(re.escape(service) for service in services)
        entries.append(
            {
                "id": "ssh-configured-service-readonly",
                "cmd": "ssh",
                "policy": "readonly",
                "cwd_roots": read_roots or [workdir],
                "allowed_args_regex": rf"^{re.escape(ssh_host)} systemctl (?:is-active|status) (?:{service_pattern})$",
                "timeout_ms": 30000,
            }
        )
    for index, url in enumerate(dict.fromkeys(health_urls), start=1):
        entries.append(
            {
                "id": "curl-health" if index == 1 else f"curl-health-{index}",
                "cmd": "curl",
                "policy": "readonly",
                "cwd_roots": read_roots or [workdir],
                "allowed_args_regex": rf"^(?:(?:-fsS|-sS|-I) --max-time 20 |--max-time 20 )?{re.escape(url)}$",
                "timeout_ms": 30000,
            }
        )
    return entries


def safe_health_url(value: str) -> bool:
    if not value or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or port not in {None, 443}:
        return False
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_unspecified or address.is_multicast
    )


def tool_envelope_for(
    task_prompt: str,
    task_class: str,
    role: str,
    worker_type_value: str,
    effort: str,
    planned_workers: int,
    provider: str,
    task_units: list[dict[str, Any]],
    validator_required: bool,
) -> dict[str, Any] | None:
    if worker_type_value == "main_session":
        return None
    mode = worker_type_value if worker_type_value in {"research_worker", "execution_worker", "validator"} else "research_worker"
    read_roots = existing_read_roots(os.getcwd(), provider)
    workdir = read_roots[0]
    tools_by_mode = {
        "research_worker": [
            "repo_read_file",
            "repo_list_files",
            "repo_search",
            "run_readonly_command",
        ],
        "execution_worker": [
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
    write_files = sorted({path for task in task_units for path in task.get("write_set", [])})
    capability_profiles = capability_profiles_for(task_prompt, task_class, provider)
    if "public_web_read" in capability_profiles:
        for mode_tools in tools_by_mode.values():
            if "public_web_fetch" not in mode_tools:
                mode_tools.append("public_web_fetch")
    shell_allowlist = [
        {
            "id": "rg-search",
            "cmd": "rg",
            "policy": "readonly",
            "cwd_roots": read_roots,
            "allowed_args_regex": r"^(?!.*(?:^| )--)(?!.*(?:^| )/)(?!.*\.\.)(?!.*[;&|`$><])(?:-n )?(?:-F )?(?:-i )?(?!-)[A-Za-z0-9_./:@%+*?{},= -]+$",
            "timeout_ms": 30000,
        },
        {
            "id": "rg-list-files",
            "cmd": "rg",
            "policy": "readonly",
            "cwd_roots": read_roots,
            "allowed_args_regex": r"^(?!.*\.\.)(?!.*[;&|`$><])--files(?: --glob [A-Za-z0-9_*?./!{}-]+)*$",
            "timeout_ms": 30000,
        },
        {
            "id": "git-read",
            "cmd": "git",
            "policy": "readonly",
            "cwd_roots": read_roots,
            "allowed_args_regex": r"^(?!.*\.\.)(?!.*[;&|`$><])(?:status --short|diff(?: --(?:stat|name-only|check))?(?: -- [A-Za-z0-9_][A-Za-z0-9_./-]*)?|log --oneline(?: -n [0-9]+)?|show --stat [A-Za-z0-9._/-]+)$",
            "timeout_ms": 30000,
        },
        {
            "id": "ls-read",
            "cmd": "ls",
            "policy": "readonly",
            "cwd_roots": read_roots,
            "allowed_args_regex": r"^(?!.*\.\.)(?!.*(?:^| )/)(?!.*[;&|`$><])(?:(?:-la|-l|-a)(?: [A-Za-z0-9_./-]+)?|[A-Za-z0-9_./-]+)?$",
            "timeout_ms": 30000,
        },
    ]
    if needs_readonly_infra_diagnostics(task_prompt, task_class, capability_profiles):
        shell_allowlist.extend(readonly_infra_shell_entries(read_roots, workdir))
    sanitized = sanitize_task_text(task_prompt)
    delegated_prompt = sanitized["sanitized_task_text"] or "Delegated task text was omitted by safety policy; return a blocked result."
    request_id = f"agentfanout-{provider.lower()}-{uuid.uuid4().hex}"
    return {
        "version": 1,
        "request_id": request_id,
        "mode": mode,
        "workdir": workdir,
        "prompt": delegated_prompt,
        "safe_instruction": SAFE_DELEGATED_INSTRUCTION,
        "prompt_sanitization": {
            "removed_lines": sanitized["removed_lines"],
            "voice_safe": sanitized["voice_safe"] and not bool(FORBIDDEN_PATTERN.search(delegated_prompt)),
            "delegation_safe": sanitized["delegation_safe"],
            "credential_bearing": sanitized["credential_bearing"],
            "omitted_reason": sanitized["omitted_reason"],
            "safe": sanitized["delegation_safe"],
        },
        "task_units": task_units[:planned_workers],
        "tools": tools_by_mode[mode],
        "capability_profiles": capability_profiles,
        "read_roots": read_roots,
        "write_files": write_files,
        "assigned_files": write_files,
        "shell_allowlist": shell_allowlist,
        "limits": {
            "max_steps": 20,
            "max_workers": max(1, min(4, planned_workers or 1)),
            "max_parallel_tools": 4,
            "max_file_bytes": 250000,
            "max_tool_output_bytes": 65536,
            "max_transcript_bytes": 1048576,
            "timeout_ms": 300000,
        },
        "success_contract": real_offload_contract(provider, True),
        "deny": [
            "secrets",
            "private_account_side_effects",
            "git_state_mutation",
            "destructive_actions",
            "deploys",
            "final_user_synthesis",
            "broad_or_sensitive_read_roots",
            "shell_indirection",
            "mutation_flags_in_readonly_commands",
            "arbitrary_interpreter_execution",
        ],
        "denied_path_components": [
            ".git", ".env", ".aws", ".claude", ".codex", ".config", ".gnupg",
            ".pai", ".ssh", "auth", "credentials", "private-data", "private-knowledge", "knowledge-base", "runtime", "secrets",
        ],
        "denied_commands": ["sh", "bash", "zsh", "python", "python3", "node", "bun", "sed", "perl", "ruby"],
        "validator_required": validator_required,
        "output_contract": "worker_result_v1" if mode != "validator" else "validator_result_v1",
    }


def auto_provider_candidates(task_class: str, effort: str) -> list[str]:
    native_codex = not env_flag("AGENT_FANOUT_RUNTIME_MODE") and bool(os.getenv("CODEX_THREAD_ID") or os.getenv("PAI_CODEX_LAUNCH_ID") or env_flag("AGENT_FANOUT_CODEX_SUBAGENTS", "AGENT_ORCH_CODEX_SUBAGENTS"))
    candidates = [option["provider"] for option in ranked_provider_options(task_class, effort)]
    if native_codex and "Codex" not in candidates:
        candidates.insert(0, "Codex")
    elif native_codex:
        candidates.remove("Codex")
        candidates.insert(0, "Codex")
    return candidates


def choose_provider(
    prompt: str,
    state: dict[str, Any],
    task_class: str,
    effort: str,
    delegated: bool,
    provider_override: str | None = None,
    requested_alias_override: dict[str, str] | None = None,
) -> tuple[str, str, list[str]]:
    if not delegated:
        return "main-session", "main-session", []
    provider_hint, hint_warnings = requested_provider(prompt)
    alias = requested_alias_override or requested_model_alias(prompt)
    if provider_override:
        candidates = [provider_override]
    elif provider_hint:
        candidates = [provider_hint]
    elif alias:
        candidates = [alias["provider"]]
    else:
        candidates = auto_provider_candidates(task_class, effort)
    native_codex = not env_flag("AGENT_FANOUT_RUNTIME_MODE") and bool(os.getenv("CODEX_THREAD_ID") or os.getenv("PAI_CODEX_LAUNCH_ID") or env_flag("AGENT_FANOUT_CODEX_SUBAGENTS", "AGENT_ORCH_CODEX_SUBAGENTS"))
    explicitly_constrained = bool(provider_override or provider_hint or alias or native_codex)

    warnings = list(hint_warnings)
    for provider in candidates:
        provider_alias = alias if alias and alias.get("provider") == provider else None
        profile = profile_for(provider, task_class, effort, provider_alias)
        selector = selector_for(provider, effort, task_class, provider_alias)
        available, readiness_warnings = readiness(
            provider, selector, task_class, effort, profile,
            allow_explicit_diagnostic=explicitly_constrained,
        )
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


def estimate_work_units(prompt: str, task_class: str, delegated: bool, role: str = "explorer") -> tuple[int, list[str], list[str]]:
    if not delegated:
        return 0, [], ["delegation_not_useful_or_hard_gated"]
    lowered = prompt.lower()
    word_numbers = {"two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "twelve": 12, "twenty": 20}
    count = 1
    dimensions = investigation_dimensions(prompt) if task_class == "bulk-read" else []
    named_domains = {item for item in ("api", "ui", "database", "tests", "docs") if re.search(rf"\b{item}\b", lowered)}
    if len(named_domains) >= 2:
        count = len(named_domains)
    file_units = list(dict.fromkeys(
        "file:" + match
        for match in re.findall(
            r"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:md|txt|rst|py|js|mjs|cjs|ts|tsx|jsx|json|toml|ya?ml))(?![A-Za-z0-9_-])",
            prompt,
            flags=re.IGNORECASE,
        )
    ))
    # Concrete filenames are the strongest safe decomposition for the bundled
    # file-only lane across every read-only/explorer task class. Do not discard
    # them merely because classification selected security-review, plan, or
    # debug instead of bulk-read.
    if file_units and role == "explorer":
        return len(file_units), file_units, [
            "hard_gates_prechecked",
            "provider_readiness_prechecked",
            "read_only_worker_scope",
            "bounded_named_files_authoritative",
            *( ["single_worker_enough"] if len(file_units) == 1 else [] ),
        ]
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
            elif task_class == "bulk-read" and len(file_units) > 1:
                count = len(file_units)
            elif count == 1 and task_class == "bulk-read" and (contains_any(lowered, BROAD_INVESTIGATION_CUES) or len(dimensions) >= 2):
                count = max(3, min(6, len(dimensions) + 1))
            elif count == 1 and ("parallel" in lowered or "multiple" in lowered or "fan out" in lowered):
                count = 3
    if file_units and count == len(file_units):
        units = file_units
    elif dimensions and count > 1:
        units = dimensions[:count]
        while len(units) < count:
            units.append(f"request_item_{len(units) + 1}")
    else:
        units = [f"request_item_{i}" for i in range(1, count + 1)]
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
    elif provider in {"MiniMax", "Local", "Grok", "Claude"}:
        adapter = {"MiniMax": "MiniMax.md", "Local": "LocalLLM.md", "Grok": "Grok.md", "Claude": "Claude.md"}[provider]
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
        "lifecycle": "current_task_only",
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
        "success_statuses": ["success"],
        "advisory_only_statuses": ["partial", "checklist_only", "review_only"],
        "advisory_only_counts_as_offload": False,
    }


def real_offload_contract(provider: str, delegated: bool) -> dict[str, Any] | None:
    if not delegated or provider == "main-session":
        return None
    return {
        "schema": "RealOffloadContractV1",
        "success_requires": [
            "worker_completed_bounded_work_unit",
            "worker_result_status_success",
            "compact_result_packet_present",
            "evidence_or_changed_files_present",
            "structural_validation_passed",
        ],
        "does_not_count_as_success": [
            "provider_invoked_only",
            "checklist_only",
            "partial_read_only_context",
            "status_denied",
            "timeout",
            "host_runtime_fallback",
        ],
        "on_failure": "retry_within_provider_repair_budget_or_report_fanout_blocked",
        "main_session_fallback_allowed": False,
        "provider_substitution_allowed": False,
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
        "success_contract": real_offload_contract(provider, delegated),
        "validation_checklist": ["scope_compliance", "evidence_supported_claims", "tests_or_commands_reported", "risks_reported"],
        "escalation_conditions": provider_primary_config(provider, True, delegated)["escalate_on"],
    }


def semantic_validator_required(task_class: str, effort: str, *, force_required: bool = False) -> bool:
    """Require semantic validation where error cost justifies another model call.

    Low/medium extraction, classification, transformation, and creative work
    still receive deterministic packet/evidence validation.  Higher-risk task
    classes, high reasoning, and an explicit independent-validation request get
    a separate provider call.
    """

    return bool(
        force_required
        or task_class not in {"bulk-classify", "data-xform", "creative"}
        or effort in {"high", "xhigh", "ultra"}
    )


def validator_for(
    provider: str,
    task_class: str,
    role: str,
    delegated: bool,
    effort: str,
    provider_primary: bool = False,
    force_required: bool = False,
) -> dict[str, Any]:
    required = bool(
        delegated
        and provider != "main-session"
        and semantic_validator_required(task_class, effort, force_required=force_required)
    )
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
    selection_reason: str | None = None,
) -> dict[str, Any]:
    if not delegated:
        blocked = bool(provider_warnings and provider == "main-session" and task_class != "small-edit")
        return {
            "required": blocked,
            "status": "fanout_blocked" if blocked else ("not_required" if env_flag("AGENT_FANOUT_RUNTIME_MODE") else "main_session"),
            "main_session_policy": "report_block" if blocked else "handle_directly",
            "worker_type": "main_session",
            "launch_before_main_research": False,
            "main_session_may_duplicate_worker_research": False,
            "provider": provider,
            "model_selector": selector,
            "runtime_launch": runtime_launch(provider, selector, effort, selection_reason),
            "planned_workers": 0,
            "work_units": [],
            "fallback_allowed": not blocked,
            "block_reason": "; ".join(provider_warnings) if blocked else None,
            "forbidden_main_session_actions_until_workers_return": [],
            "success_contract": None,
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
        "runtime_launch": runtime_launch(provider, selector, effort, selection_reason),
        "planned_workers": planned_workers,
        "work_units": units,
        "fallback_allowed": False,
        "block_reason": None,
        "success_contract": real_offload_contract(provider, True),
        "forbidden_main_session_actions_until_workers_return": [
            "substantive_research",
            "ssh_diagnostics",
            "filesystem_sweeps",
            "repo_wide_search",
            "parallel_duplicate_analysis",
            "native_worker_spawn_outside_host_adapter",
            "provider_substitution",
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
        "force_dispatch": state.get("force_dispatch", False),
        "fanout_policy": "automatic_guarded",
        "state_scope": "task_only",
        "hard_gate_policy": {
            "mcp_private_tools": "main_session_only",
            "secrets": "main_session_only",
            "git_state_changes": "main_session_only",
            "destructive_actions": "main_session_only",
            "final_synthesis": "main_session_only",
        },
        "startup_mode": "skill_selected_only",
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
    if re.search(r"\b(?:AgentFanout|use|with|ask)\s+GrokCLI\b", prompt, re.IGNORECASE):
        # This explicitly selected transport cannot become Auto/main-session or
        # the existing direct-HTTPS Grok lane when unavailable.
        return {"schema": "AgentFanoutUpgradeRouteUnavailableV1", "status": "fanout_blocked",
                "selected_provider": "GrokCLI", "mode": "grok_cli_subscription_acp",
                "block_reason": "grok_cli_installed_boundary_unproved",
                "fallback_allowed": False, "orchestration_useful": False,
                "agent_composition_required": False, "execution_allowed": False,
                "next_state": default_state(), "references_to_load": [],
                "fanout": {"planned_workers": 0, "work_units": []}}
    state = normalize_state(state or default_state())
    original_state = dict(state)
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
    routing_alias, astra_cost_gate = astra_routing_policy(task_prompt, worker_failures)
    if astra_cost_gate.get("warning"):
        warnings.append(str(astra_cost_gate["warning"]))
    task_class = classify(task_prompt)
    gates = hard_gates(task_prompt, task_class)
    delegation_wanted = should_delegate(task_class, task_prompt, gates, next_state, command)
    if (
        routing_alias
        and routing_alias.get("profile") == "astra"
        and task_class != "small-edit"
        and not any((
            gates["requires_mcp_private_tools"],
            gates["requires_secrets"],
            gates["requires_git_state_change"],
            gates["requires_destructive_action"],
        ))
    ):
        delegation_wanted = True
    effort = reasoning_effort(task_class, task_prompt, next_state)
    project_binding_error: str | None = None
    if delegation_wanted:
        try:
            resolve_project_binding(explicit_root=None)
        except ProjectBindingError as exc:
            project_binding_error = str(exc)
            warnings.append(f"project_binding_required:{project_binding_error}")
            # An execution-bearing provider directive cannot become sticky
            # when its authorization boundary was never established.
            next_state = original_state
    breaker = circuit_breaker(worker_failures)
    if breaker["triggered"]:
        warnings.append("circuit_breaker_triggered")
        delegation_wanted = False
    role = role_for(task_class, task_prompt) if delegation_wanted else "main"
    provider_warnings: list[str]
    if breaker["triggered"]:
        provider, selector, provider_warnings = "main-session", "main-session", ["circuit_breaker_triggered:worker_failure_threshold"]
    elif project_binding_error is not None:
        provider, selector, provider_warnings = (
            "main-session", "main-session", [project_binding_error],
        )
    else:
        provider, selector, provider_warnings = choose_provider(
            task_prompt,
            next_state,
            task_class,
            effort,
            delegation_wanted,
            command_provider_override,
            routing_alias,
        )
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
    requested_workers, units, guardrails = estimate_work_units(task_prompt, task_class, delegated, role)
    try:
        max_wave_size = max(1, int(os.getenv("AGENT_FANOUT_MAX_PARALLEL_WORKERS", "4")))
    except ValueError:
        max_wave_size = 4
        warnings.append("invalid_max_parallel_workers_defaulted")
    if routing_alias and routing_alias.get("profile") == "astra":
        max_wave_size = 1
        guardrails.append("astra_single_worker_wave_cap")
    planned_workers = min(requested_workers, max_wave_size)
    deferred_workers = max(0, requested_workers - planned_workers)
    parallelism = "none" if not delegated else "parallel" if planned_workers > 1 else "serial-delegated"
    alias = routing_alias
    candidate_alias = alias if alias and alias.get("provider") == provider else None
    candidate_profile = profile_for(provider, task_class, effort, candidate_alias) if delegated else "main"
    unit_token_estimates = bounded_unit_input_token_estimates(units) if delegated else []
    validation_override = hard_independent_validation_requested(task_prompt)
    candidate_validator_required = bool(
        delegated
        and provider != "main-session"
        and semantic_validator_required(
            task_class, effort, force_required=validation_override,
        )
    )
    candidate_telemetry = decision_telemetry(
        task_prompt,
        provider,
        task_class,
        effort,
        requested_workers,
        planned_workers,
        candidate_alias,
        candidate_validator_required,
        unit_token_estimates,
    )
    explicit_override, override_reason = anti_waste_override(task_prompt, next_state, command)
    native_codex_path = provider == "Codex" and not env_flag("AGENT_FANOUT_RUNTIME_MODE") and bool(
        os.getenv("CODEX_THREAD_ID") or os.getenv("PAI_CODEX_LAUNCH_ID")
        or env_flag("AGENT_FANOUT_CODEX_SUBAGENTS", "AGENT_ORCH_CODEX_SUBAGENTS")
    )
    anti_waste_guard = {
        "schema": "AntiWasteGuardV1",
        "evaluated": bool(delegated and provider != "main-session" and not native_codex_path),
        "decision": "allow",
        "reason": "native_host_usage_unavailable_claims_disabled" if native_codex_path else "candidate_cost_not_higher_than_main_baseline",
        "candidate": {
            "provider": provider,
            "profile": candidate_profile,
            "model_selector": candidate_telemetry.get("model_resolution", {}).get("selector"),
            "reasoning_effort": effort,
            "planned_workers": planned_workers,
            "estimated_relative_cost_units_this_wave": candidate_telemetry.get("estimated_relative_cost_units_this_wave"),
            "estimated_relative_cost_units_request": candidate_telemetry.get("estimated_relative_cost_units_request"),
            "estimated_main_session_baseline_units": candidate_telemetry.get("estimated_main_session_baseline_units"),
            "estimated_orchestration_overhead_dominates_baseline": candidate_telemetry.get("estimated_orchestration_overhead_dominates_baseline"),
        },
        "override": {
            "explicit_requested": explicit_override,
            "reason": override_reason,
            "hard_independent_validation_required": validation_override,
        },
    }
    # Every hosted provider pays launch/context overhead. Missing calibration
    # uses the registry's conservative non-zero default; selecting a provider
    # never converts unknown overhead to zero.
    if (
        anti_waste_guard["evaluated"]
        and candidate_telemetry.get("estimated_orchestration_overhead_dominates_baseline")
    ):
        if explicit_override:
            anti_waste_guard["reason"] = f"override:{override_reason}"
        elif validation_override:
            anti_waste_guard["reason"] = "override:hard_independent_validation_required"
        else:
            anti_waste_guard["decision"] = "suppress_to_main_session"
            anti_waste_guard["reason"] = "predicted_orchestration_cost_exceeds_direct_main_session_baseline"
            warnings.append("anti_waste_guard_suppressed_auto_fanout")
            delegated = False
            delegation_wanted = False
            provider, selector, role = "main-session", "main-session", "main"
            # Candidate discovery warnings remain in top-level telemetry, but
            # they cannot turn an intentional anti-waste direct decision into
            # `fanout_blocked` after no worker launch was attempted.
            provider_warnings = []
            planned_workers = 0
            deferred_workers = 0
            parallelism = "none"
    prompt_provider_hint, _ = requested_provider(task_prompt)
    explicit_provider_primary = bool(
        command_provider_override
        or command["type"] == "set_provider_mode"
        or prompt_provider_hint
        or command["type"] == "provider_task"
    )
    provider_primary = provider_primary_config(provider, explicit_provider_primary, delegated)
    capability_snapshot = provider_capability_snapshot(provider, selector, task_class, delegated, effort)
    selected_alias = alias if alias and alias.get("provider") == provider else None
    model_profile = profile_for(provider, task_class, effort, selected_alias) if delegated else "main"
    model_resolution = resolve_model(provider, model_profile, selected_alias) if delegated else {
        "provider": "main-session", "profile": "main", "selector": "main-session", "ack_required": False
    }
    capability_snapshot["model_resolution"] = model_resolution
    validator = validator_for(
        provider, task_class, role, delegated, effort,
        provider_primary["active"], validation_override,
    )
    full_agents = agent_composition_required(task_prompt)
    codex_ref = codex_adapter_required(task_prompt, provider) or "codex" in task_prompt.lower()
    refs = references_to_load(task_prompt, provider, delegated, full_agents, codex_ref, pending_plan)
    capsule = capsule_for(task_class, task_prompt, role, delegated)
    sanitized_task = sanitize_task_text(task_prompt)
    worker_task_prompt = sanitized_task["sanitized_task_text"] or "Delegated task text was omitted by safety policy; return a blocked result."
    try:
        approved_paths = [str(approved_project_root())] if delegated else []
    except ValueError as exc:
        approved_paths = []
        warnings.append(f"task_paths_unbound:{exc}")
    task_units = build_task_units(
        worker_task_prompt,
        task_class,
        role,
        provider,
        selector,
        effort,
        units,
        allowed_paths=approved_paths,
        budget={"max_wave_size": max_wave_size},
        requested_alias=selected_alias,
        validator_required=validator["required"],
    ) if delegated else []
    envelope = tool_envelope_for(
        task_prompt,
        task_class,
        role,
        worker_kind(task_class, role) if delegated else "main_session",
        effort,
        planned_workers,
        provider,
        task_units,
        validator["required"],
    ) if provider in {"MiniMax", "Local"} and delegated and (provider == "MiniMax" or local_tool_agent_enabled()) else None
    minimax_agent_request = minimax_agent_request_for(envelope)
    local_agent_request = None
    local_host_usage_correlation = None
    if provider == "Local" and delegated and local_tool_agent_enabled():
        try:
            local_bridge_launch = local_agent_request_for(envelope)
        except ValueError as exc:
            warnings.append(f"fanout_blocked:local:{exc}")
            fanout_blocked = True
            local_bridge_launch = None
        if local_bridge_launch:
            local_agent_request = local_bridge_launch["request"]
            local_host_usage_correlation = local_bridge_launch["usage_correlation"]
    if breaker["triggered"]:
        fanout_blocked = True
        if "circuit_breaker_stopped_new_launches" not in guardrails:
            guardrails.append("circuit_breaker_stopped_new_launches")
    telemetry = candidate_telemetry if anti_waste_guard["decision"] == "suppress_to_main_session" else decision_telemetry(
        task_prompt,
        provider,
        task_class,
        effort,
        requested_workers,
        planned_workers,
        selected_alias,
        validator["required"],
        unit_token_estimates,
    )
    unresolved_binding_ids = [
        task["task_id"] for task in task_units
        if task.get("requires_path_binding") or task.get("requires_write_binding")
    ]
    adapter_input_errors: list[dict[str, str]] = []
    adapter_tool_errors: list[dict[str, str]] = []
    # The bundled hosted Codex, Grok, and credential-free Local adapters are
    # deliberately no-tool and file-only.  Bind the exact input bytes before
    # any of those adapters can create a launch receipt.
    # Reject symbolic decomposition before a RunState or launch receipt can
    # suggest that a nonexistent request.items[] payload is executable.
    native_codex = provider == "Codex" and not env_flag("AGENT_FANOUT_RUNTIME_MODE") and bool(os.getenv("CODEX_THREAD_ID") or os.getenv("PAI_CODEX_LAUNCH_ID") or env_flag("AGENT_FANOUT_CODEX_SUBAGENTS", "AGENT_ORCH_CODEX_SUBAGENTS"))
    if provider in {"Codex", "Grok", "Local"} and delegated and (not native_codex or role == "worker") and (
        provider in {"Codex", "Grok"} or local_hosted_no_tool_enabled()
    ):
        if role == "worker":
            adapter_tool_errors.append({
                "task_id": "*",
                "reason": "adapter_tool_mode_unsupported:no_tools_reference_lane",
            })
        try:
            project_binding = resolve_project_binding(explicit_root=None)
        except ProjectBindingError as exc:
            project_binding = None
            adapter_input_errors.append({"task_id": "*", "reason": str(exc)})
        if project_binding is not None:
            for task in task_units:
                input_ref = task.get("input_ref")
                if not isinstance(input_ref, str) or not input_ref.startswith("file:"):
                    adapter_input_errors.append({
                        "task_id": str(task.get("task_id")),
                        "reason": "adapter_input_mode_unsupported:bounded_file_text_required",
                    })
                    continue
                try:
                    _path, content = resolve_bounded_file(project_binding, input_ref)
                    task["input_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    task["input_bytes"] = len(content.encode("utf-8"))
                except ProjectBindingError as exc:
                    adapter_input_errors.append({"task_id": str(task.get("task_id")), "reason": str(exc)})
    dispatch_plan = dispatch_contract(
        delegated, provider, selector, effort, role, task_class,
        planned_workers, units[:planned_workers], provider_warnings,
        model_resolution.get("selection_reason"),
    )
    if project_binding_error is not None:
        fanout_blocked = True
        dispatch_plan.update({
            "status": "binding_required",
            "main_session_policy": "supply_exact_project_root_then_replan",
            "planned_workers": 0,
            "launch_before_main_research": False,
            "fallback_allowed": False,
            "block_reason": project_binding_error,
        })
    if (
        provider == "Local" and delegated and not local_agent_request
        and not local_hosted_no_tool_enabled()
    ):
        fanout_blocked = True
        dispatch_plan.update({
            "status": "fanout_blocked",
            "main_session_policy": "report_block",
            "planned_workers": 0,
            "launch_before_main_research": False,
            "fallback_allowed": False,
            "block_reason": "local_bridge_issuance_required",
        })
    if unresolved_binding_ids:
        fanout_blocked = True
        dispatch_plan.update({
            "status": "binding_required",
            "main_session_policy": "apply_host_path_and_write_bindings_then_replan",
            "planned_workers": 0,
            "launch_before_main_research": False,
            "block_reason": "task_bindings_required",
            "binding_task_ids": unresolved_binding_ids,
        })
    if adapter_input_errors:
        fanout_blocked = True
        dispatch_plan.update({
            "status": "binding_required",
            "main_session_policy": "supply_supported_bounded_inputs_then_replan",
            "planned_workers": 0,
            "launch_before_main_research": False,
            "fallback_allowed": False,
            "block_reason": "adapter_input_binding_required",
            "adapter_input_errors": adapter_input_errors,
        })
    if adapter_tool_errors:
        fanout_blocked = True
        dispatch_plan.update({
            "status": "fanout_blocked",
            "main_session_policy": "report_block",
            "planned_workers": 0,
            "launch_before_main_research": False,
            "fallback_allowed": False,
            "block_reason": "hosted_adapter_tool_boundary_required",
            "adapter_tool_errors": adapter_tool_errors,
        })

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
                "runtime_launch": runtime_launch(
                    provider, selector, effort,
                    model_resolution.get("selection_reason"),
                ),
                "tool_envelope": envelope,
                "minimax_agent_request": minimax_agent_request,
                "local_agent_request": local_agent_request,
                "local_host_usage_correlation": local_host_usage_correlation,
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
        "task_units": task_units,
        "decision_telemetry": telemetry,
        "anti_waste_guard": anti_waste_guard,
        "astra_cost_gate": astra_cost_gate,
        "tool_envelope": envelope,
        "minimax_agent_request": minimax_agent_request,
        "local_agent_request": local_agent_request,
        "local_host_usage_correlation": local_host_usage_correlation,
        "fanout": {
            "policy": provider_primary["orchestration_style"],
            "planned_workers": planned_workers,
            "requested_units": requested_workers,
            "total_task_units": len(task_units),
            "deferred_units": deferred_workers,
            "work_units": units[:planned_workers],
            "task_units": task_units,
            "rationale": "bounded first wave; lifecycle replans remaining units" if delegated else "main_session_only",
            "guardrails_applied": guardrails,
            "fallback_reason": "; ".join(provider_warnings) if provider_warnings and not delegated else None,
            "blocked": fanout_blocked,
        },
        "dispatch": dispatch_plan,
        "enforcement": {
            "level": "advisory",
            "native_worker_tool_control": "unknown",
            "native_spawn_policy": "host_adapter_only",
            "anti_bypass_certified": False,
            "upgrade_path": "run_through_atomic_hosted_cli_or_independently_controlled_harness_adapter",
        },
        "circuit_breaker": breaker,
        "warnings": warnings,
        "main_session_responsibilities": ["MCP/private tools", "secrets", "git state changes", "destructive actions", "final synthesis"],
        "safe_delegated_instruction": SAFE_DELEGATED_INSTRUCTION if delegated else None,
    }


def lifecycle_summary(state: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for task in state.get("tasks", []):
        status = str(task.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema": state.get("schema"),
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "wave_index": state.get("wave_index"),
        "task_counts": counts,
        "usage": state.get("usage"),
        "budget": state.get("budget"),
        "block_reason": state.get("block_reason"),
    }


def _compact_text(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    compacted = " ".join(value.split())
    if len(compacted) > limit:
        return compacted[:limit - 1] + "…"
    return compacted


def _compact_task_summaries(tasks: Any) -> list[dict[str, Any]]:
    if not isinstance(tasks, list):
        return []
    summaries: list[dict[str, Any]] = []
    for task in tasks[:32]:
        if not isinstance(task, dict):
            continue
        model = task.get("provider_model") if isinstance(task.get("provider_model"), dict) else {}
        summaries.append({
            "task_id": task.get("task_id"),
            "parent_task_id": task.get("parent_task_id"),
            "title": _compact_text(task.get("title"), 160),
            "scope": _compact_text(task.get("scope"), 280),
            "task_class": task.get("task_class"),
            "role": task.get("role"),
            "status": task.get("status"),
            "delegable": task.get("delegable"),
            "provider": model.get("provider"),
            "model_selector": model.get("selector"),
            "reasoning_effort": task.get("reasoning_effort"),
            "validator_required": task.get("validator_required"),
            "attempts": task.get("attempts"),
        })
    return summaries


def _compact_next_wave(next_wave: Any) -> dict[str, Any] | None:
    if not isinstance(next_wave, dict):
        return None
    assignments: list[dict[str, Any]] = []
    for assignment in next_wave.get("assignments", [])[:32]:
        if not isinstance(assignment, dict):
            continue
        task = assignment.get("task") if isinstance(assignment.get("task"), dict) else {}
        assignments.append({
            "task_id": assignment.get("task_id"),
            "worker_action": assignment.get("worker_action"),
            "worker_handle": assignment.get("worker_handle"),
            "recommended_worker_handle": assignment.get("recommended_worker_handle"),
            "launch_correlation_id": assignment.get("launch_correlation_id"),
            "reasoning_effort": task.get("reasoning_effort"),
        })
    return {
        "action": next_wave.get("action"),
        "reason": next_wave.get("reason"),
        "wave_id": next_wave.get("wave_id"),
        "wave_index": next_wave.get("wave_index"),
        "remaining_delegable_tasks": next_wave.get("remaining_delegable_tasks"),
        "budget_remaining": next_wave.get("budget_remaining"),
        "binding_task_ids": next_wave.get("binding_task_ids", []),
        "validation_task_ids": next_wave.get("validation_task_ids", []),
        "assignments": assignments,
    }


def _compact_lifecycle(lifecycle: Any) -> dict[str, Any] | None:
    if not isinstance(lifecycle, dict):
        return None
    run = lifecycle.get("run") if isinstance(lifecycle.get("run"), dict) else None
    compact_run = None if run is None else {
        "schema": run.get("schema"),
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "wave_index": run.get("wave_index"),
        "task_counts": run.get("task_counts"),
        "usage": run.get("usage"),
        "block_reason": run.get("block_reason"),
    }
    output: dict[str, Any] = {
        "state_file": lifecycle.get("state_file"),
        "error": lifecycle.get("error"),
        "run": compact_run,
        "next_action": _compact_next_wave(lifecycle.get("next_wave")),
    }
    if isinstance(lifecycle.get("ingestion"), dict):
        ingestion = lifecycle["ingestion"]
        output["ingestion"] = {
            "completed_this_batch": ingestion.get("completed_this_batch"),
            "residual_delegable_tasks": ingestion.get("residual_delegable_tasks"),
            "next_required_action": ingestion.get("next_required_action"),
            "invalid_result_count": sum(1 for item in ingestion.get("validation", []) if isinstance(item, dict) and not item.get("valid")),
        }
    if isinstance(lifecycle.get("binding_update"), dict):
        output["binding_update"] = {
            "updated_task_ids": lifecycle["binding_update"].get("updated_task_ids", []),
        }
    return output


def _host_runner_guidance(dispatch: dict[str, Any], lifecycle: dict[str, Any] | None) -> dict[str, Any]:
    next_action = lifecycle.get("next_action") if isinstance(lifecycle, dict) else None
    state_file = lifecycle.get("state_file") if isinstance(lifecycle, dict) else None
    dispatch_status = dispatch.get("status")
    if dispatch_status == "launch_required":
        return {
            "next_step": "restart_with_atomic_cli",
            "command_template": [
                "python3", "scripts/agentfanout.py", "run",
                "--project-root", "<exact-git-worktree>",
                "--provider", dispatch.get("provider"),
                "--accept-overhead", "--", "<repeat-original-bounded-task>",
            ],
            "advisory_state_file": str(state_file) if state_file else None,
            "reason": (
                "direct route planning is advisory; the atomic CLI must own "
                "project binding, guard creation, manifest binding, and runner launch"
            ),
            "parent_policy": "do_not_perform_worker_research_or_edits_while_assignments_remain",
            "packet_location": "full host task packets are persisted in the RunStateV2 state file",
        }
    if dispatch_status in {"binding_required", "fanout_blocked"}:
        return {
            "next_step": "report_or_resolve_host_gate",
            "block_reason": dispatch.get("block_reason"),
            "fallback_allowed": dispatch.get("fallback_allowed"),
            "parent_policy": dispatch.get("main_session_policy"),
        }
    if isinstance(next_action, dict) and next_action.get("action") == "wait":
        return {
            "next_step": "await_or_submit_required_host_evidence",
            "reason": next_action.get("reason"),
            "packet_location": "use the persisted RunStateV2 state file; do not reconstruct packets in parent context",
        }
    return {
        "next_step": "no_host_worker_launch_required",
        "parent_policy": dispatch.get("main_session_policy"),
        "fallback_allowed": dispatch.get("fallback_allowed"),
    }


def compact_route_output(result: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded parent-facing route summary while retaining full JSON by default."""

    route_info = result.get("route") if isinstance(result.get("route"), dict) else {}
    executor = route_info.get("executor") if isinstance(route_info.get("executor"), dict) else {}
    dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
    lifecycle = _compact_lifecycle(result.get("lifecycle"))
    fanout = result.get("fanout") if isinstance(result.get("fanout"), dict) else {}
    command = result.get("command") if isinstance(result.get("command"), dict) else {}
    command_args = command.get("args") if isinstance(command.get("args"), dict) else {}
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    return {
        "schema": "AgentFanoutCompactRouteV1",
        "command": {
            "type": command.get("type"), "name": command.get("name"),
            "provider": command_args.get("provider"), "error": command.get("error"),
        },
        "status": {
            "provider_mode": status.get("provider_mode"),
            "effort_mode": status.get("effort_mode"),
            "fanout_policy": status.get("fanout_policy"),
            "force_dispatch": status.get("force_dispatch"),
            "warnings": status.get("warnings", []),
        },
        "task_class": route_info.get("task_class"),
        "provider": executor.get("provider", result.get("selected_provider")),
        "model_selector": executor.get("model_selector"),
        "reasoning_effort": executor.get("reasoning_effort"),
        "role": executor.get("role"),
        "orchestration_useful": result.get("orchestration_useful"),
        "dispatch": {
            "status": dispatch.get("status"),
            "required": dispatch.get("required"),
            "planned_workers": dispatch.get("planned_workers"),
            "launch_before_main_research": dispatch.get("launch_before_main_research"),
            "main_session_policy": dispatch.get("main_session_policy"),
            "fallback_allowed": dispatch.get("fallback_allowed"),
            "block_reason": dispatch.get("block_reason"),
            "adapter_input_errors": dispatch.get("adapter_input_errors", []),
        },
        "fanout": {
            "policy": fanout.get("policy"),
            "planned_workers": fanout.get("planned_workers"),
            "total_task_units": fanout.get("total_task_units"),
            "deferred_units": fanout.get("deferred_units"),
            "blocked": fanout.get("blocked"),
            "rationale": _compact_text(fanout.get("rationale"), 240),
        },
        "anti_waste_guard": result.get("anti_waste_guard"),
        "task_summaries": _compact_task_summaries(result.get("task_units")),
        "warnings": result.get("warnings", []),
        "next_state": result.get("next_state"),
        "lifecycle": lifecycle,
        "host_runner_guidance": _host_runner_guidance(dispatch, lifecycle),
        "enforcement": result.get("enforcement", {
            "level": "advisory", "native_worker_tool_control": "unknown",
            "native_spawn_policy": "host_adapter_only", "anti_bypass_certified": False,
        }),
        "project_binding": result.get("project_binding"),
    }


def parse_results_json(raw: str) -> list[dict[str, Any]]:
    payload = raw
    if raw.startswith("@"):
        source = Path(raw[1:]).expanduser()
        if source.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("results_json_file_too_large")
        payload = source.read_text(encoding="utf-8")
    if len(payload.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("results_json_too_large")
    parsed = json.loads(payload)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("results_json_must_be_array")
    return parsed


def parse_bindings_json(raw: str) -> list[dict[str, Any]]:
    payload = raw
    if raw.startswith("@"):
        source = Path(raw[1:]).expanduser()
        if source.stat().st_size > 1024 * 1024:
            raise ValueError("bindings_json_file_too_large")
        payload = source.read_text(encoding="utf-8")
    if len(payload.encode("utf-8")) > 1024 * 1024:
        raise ValueError("bindings_json_too_large")
    parsed = json.loads(payload)
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("bindings_json_must_be_nonempty_array")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="*", help="Prompt to route")
    parser.add_argument("--sanitize", action="store_true", help="Sanitize stdin/prompt for delegated use")
    parser.add_argument("--compact", action="store_true", help="Emit bounded parent-facing routing/lifecycle guidance instead of the full compatibility JSON")
    parser.add_argument("--state-json", help="Prior conversation state JSON")
    parser.add_argument("--state-file", default=os.getenv("AGENT_FANOUT_STATE_FILE"), help="Optional persistent AgentFanout state JSON path")
    root_group = parser.add_mutually_exclusive_group()
    root_group.add_argument("--project-root", type=Path, help="Exact authorized Git worktree root")
    root_group.add_argument("--discover-current-worktree", action="store_true", help="Explicitly discover the Git worktree containing the current directory")
    parser.add_argument("--run-state-file", help="Persistent RunStateV2 lifecycle path")
    parser.add_argument("--new-run", action="store_true", help="Start or replace the lifecycle run at --run-state-file")
    parser.add_argument("--results-json", help="WorkerResultV1 JSON array, or @path, to ingest into an existing run")
    parser.add_argument("--bindings-json", help="Host task path/write binding JSON array, or @path, for an existing run")
    parser.add_argument("--max-wave-size", type=int, help="Maximum workers dispatched in one lifecycle wave")
    parser.add_argument("--max-waves", type=int, help="Maximum lifecycle waves")
    parser.add_argument("--max-total-cost-usd", type=float, help="Maximum measured lifecycle cost")
    parser.add_argument("--worker-failures", type=int, default=0, help="Observed worker failures in the current fanout wave")
    parser.add_argument("--pending-plan", action="store_true", help="Treat the current prompt as a response to a pending AgentFanout plan")
    parser.add_argument("--plan-stale", action="store_true", help="Mark the pending plan stale")
    parser.add_argument("--needs-separate-confirmation", action="store_true", help="Pending plan needs separate destructive/production/secret/git confirmation")
    parser.add_argument("--v2-request-json", help="Opt-in NativeWaveRequestV2 JSON object, or - for stdin")
    parser.add_argument("--telemetry-file", help="Optional V2 metadata ledger path")
    args = parser.parse_args()
    if args.v2_request_json is not None:
        from agentfanout.v2_policy import enabled as v2_enabled, process_native_wave
        from agentfanout.native import NativeWaveError
        try:
            raw = sys.stdin.read(1_048_577) if args.v2_request_json == "-" else args.v2_request_json
            if len(raw.encode("utf-8")) > 1_048_576:
                raise NativeWaveError("v2_request_too_large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise NativeWaveError("v2_request_invalid")
            if not v2_enabled(request.get("feature_flags")):
                legacy_prompt = request.get("prompt") or " ".join(args.prompt).strip()
                if not isinstance(legacy_prompt, str) or not legacy_prompt.strip():
                    write_json_output({"schema": "NativeWaveResponseV2", "status": "legacy_route", "reason": "v2_master_disabled"})
                else:
                    baseline = route(legacy_prompt, default_state())
                    baseline["v2_status"] = "legacy_baseline"
                    write_json_output(baseline)
                return
            write_json_output(process_native_wave(request, args.telemetry_file))
            return
        except (NativeWaveError, TypeError, ValueError, json.JSONDecodeError) as exc:
            write_json_output({"schema": "NativeWaveResponseV2", "status": "blocked", "error": str(exc)})
            raise SystemExit(2) from exc
    project_binding: dict[str, Any] | None = None
    if args.project_root is not None or args.discover_current_worktree:
        try:
            project_binding = resolve_project_binding(
                explicit_root=args.project_root,
                discover_current_worktree=args.discover_current_worktree,
            )
        except ProjectBindingError as exc:
            write_json_output({"error": str(exc), "status": "project_binding_required"})
            raise SystemExit(3) from exc
        os.environ["AGENT_FANOUT_PROJECT_ROOT"] = project_binding["resolved_root"]
    elif os.getenv("AGENT_FANOUT_PROJECT_ROOT", "").strip():
        # Compatibility callers historically receive a structured route block
        # for an invalid ambient root. The explicit public CLI is stricter and
        # exits above before routing.
        try:
            project_binding = resolve_project_binding(explicit_root=None)
        except ProjectBindingError:
            project_binding = None
    text = " ".join(args.prompt).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if args.sanitize:
        write_json_output(sanitize_prompt(text))
        return
    if args.bindings_json:
        if args.results_json:
            write_json_output({"error": "bindings_and_results_must_be_separate_calls"})
            raise SystemExit(2)
        if not args.run_state_file:
            write_json_output({"error": "run_state_file_required_for_bindings"})
            raise SystemExit(2)
        try:
            run_state = load_run_state(args.run_state_file)
            binding_update = apply_task_bindings(run_state, parse_bindings_json(args.bindings_json))
            next_wave = plan_next_wave(run_state)
            save_run_state(args.run_state_file, run_state)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            write_json_output({"error": str(exc)})
            raise SystemExit(2) from exc
        response = {
            "lifecycle": {
                "state_file": str(Path(args.run_state_file).expanduser()),
                "run": lifecycle_summary(run_state),
                "binding_update": binding_update,
                "next_wave": next_wave,
            }
        }
        write_json_output({"schema": "AgentFanoutCompactLifecycleV1", "lifecycle": _compact_lifecycle(response["lifecycle"])} if args.compact else response)
        return
    if args.results_json:
        if not args.run_state_file:
            write_json_output({"error": "run_state_file_required_for_results"})
            raise SystemExit(2)
        try:
            run_state = load_run_state(args.run_state_file)
            ingestion = ingest_results(run_state, parse_results_json(args.results_json))
            next_wave = plan_next_wave(run_state)
            save_run_state(args.run_state_file, run_state)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            write_json_output({"error": str(exc)})
            raise SystemExit(2) from exc
        response = {
            "lifecycle": {
                "state_file": str(Path(args.run_state_file).expanduser()),
                "run": lifecycle_summary(run_state),
                "ingestion": ingestion,
                "next_wave": next_wave,
            }
        }
        write_json_output({"schema": "AgentFanoutCompactLifecycleV1", "lifecycle": _compact_lifecycle(response["lifecycle"])} if args.compact else response)
        return
    if args.state_json is not None:
        state, state_warnings = parse_state_arg(args.state_json)
    else:
        state, state_warnings = parse_state_file(args.state_file)
    result = route(
        text,
        state,
        args.worker_failures,
        args.pending_plan,
        args.plan_stale,
        args.needs_separate_confirmation,
    )
    result["project_binding"] = project_binding
    write_warnings = write_state_file(args.state_file, result.get("next_state", default_state()))
    result["warnings"] = state_warnings + write_warnings + result.get("warnings", [])
    if "status" in result and isinstance(result["status"], dict):
        result["status"]["warnings"] = result["warnings"]
    if args.run_state_file:
        run_path = Path(args.run_state_file).expanduser()
        budget_overrides = {
            key: value
            for key, value in {
                "max_wave_size": args.max_wave_size,
                "max_waves": args.max_waves,
                "max_total_cost_usd": args.max_total_cost_usd,
            }.items()
            if value is not None
        }
        try:
            if args.new_run or not run_path.exists():
                if not result.get("task_units"):
                    raise ValueError("delegable_task_units_required_to_start_run")
                if result.get("dispatch", {}).get("status") != "launch_required":
                    raise ValueError(result.get("dispatch", {}).get("block_reason") or "dispatch_not_launchable")
                run_state = initialize_run(text, result["task_units"], budget_overrides)
                if project_binding is None:
                    raise ValueError("project_binding_required")
                run_state["project_binding"] = project_binding
            else:
                run_state = load_run_state(run_path)
                persisted_binding = run_state.get("project_binding")
                if persisted_binding is not None:
                    validate_project_binding(persisted_binding, expected_root=project_binding["resolved_root"] if project_binding else None)
            next_wave = plan_next_wave(run_state)
            save_run_state(run_path, run_state, replace=args.new_run)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result["lifecycle"] = {"error": str(exc), "state_file": str(run_path)}
        else:
            result["lifecycle"] = {
                "state_file": str(run_path),
                "run": lifecycle_summary(run_state),
                "next_wave": next_wave,
            }
    write_json_output(compact_route_output(result) if args.compact else result)


if __name__ == "__main__":
    main()
