#!/usr/bin/env python3
from __future__ import annotations

import atexit
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _runtime_safe_test_project_root() -> Path:
    """Keep routing fixtures outside runtime/auth trees when tests run staged."""

    configured = os.getenv("AGENT_FANOUT_TEST_PROJECT_ROOT", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    sensitive_components = {".pai", ".codex", ".claude", ".ssh", ".aws", ".gnupg"}
    if not any(part.lower() in sensitive_components for part in ROOT.parts):
        return ROOT
    path = Path(tempfile.mkdtemp(prefix="agentfanout-test-project-"))
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    (path / "README.md").write_text("# AgentFanout test project\n", encoding="utf-8")
    (path / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
    return path


PROJECT_ROOT = _runtime_safe_test_project_root()

from agentfanout.lifecycle import (
    apply_task_bindings,
    build_task_units,
    ingest_results,
    initialize_run,
    load_run_state,
    mark_complete,
    plan_next_wave,
    record_provider_usage,
    save_run_state,
    validate_run_state,
)
from agentfanout.provider_registry import (
    decision_telemetry, profile_capable, profile_for, ranked_provider_options,
)

ROUTE = ROOT / "scripts" / "route.py"
FIXTURES = Path(__file__).with_name("fixtures.json")


def get_path(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def expected_value(value):
    if isinstance(value, str):
        return value.replace("__HOME__", str(Path.home()))
    if isinstance(value, list):
        return [expected_value(item) for item in value]
    if isinstance(value, dict):
        return {key: expected_value(item) for key, item in value.items()}
    return value


def run_route(case):
    cmd = [sys.executable, "-S", str(ROUTE), case["prompt"]]
    env = os.environ.copy()
    env.pop("AGENT_FANOUT_STATE_FILE", None)
    env["AGENT_FANOUT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST"] = str(PROJECT_ROOT)
    for local_name in [
        "AGENT_FANOUT_LOCAL_AVAILABLE",
        "AGENT_FANOUT_LOCAL_LIVE_CHECKS",
        "AGENT_FANOUT_LOCAL_CAPABILITY_VERIFIED",
        "AGENT_FANOUT_LOCAL_TOOL_AGENT",
        "AGENT_FANOUT_LOCAL_ENDPOINT",
        "AGENT_FANOUT_LOCAL_HOST",
        "AGENT_FANOUT_LOCAL_PORT",
        "AGENT_FANOUT_LOCAL_REQUIRED_MODE",
        "AGENT_FANOUT_LOCAL_ACTIVE_MODE",
        "AGENT_FANOUT_LOCAL_MODEL_ID",
        "AGENT_FANOUT_LOCAL_MODEL_ALIAS",
        "AGENT_FANOUT_LOCAL_CONTEXT_WINDOW",
        "AGENT_FANOUT_LOCAL_TASK_CLASSES",
        "AGENT_FANOUT_LOCAL_CAPABILITY_EXPIRES",
        "AGENT_FANOUT_LOCAL_ATTESTATION_JSON",
        "AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON",
        "AGENT_FANOUT_LOCAL_BRIDGE_ISSUER",
        "AGENT_FANOUT_LOCAL_BRIDGE_MODULE",
        "AGENT_FANOUT_LOCAL_ATTESTATION_MAX_AGE_SECONDS",
        "AGENT_FANOUT_LOCAL_RUNTIME_PATH",
        "AGENT_FANOUT_LOCAL_READ_ROOTS",
        "AGENT_FANOUT_EXTERNAL_READ_ROOTS",
        "AGENT_FANOUT_LOCAL_PRIVATE_KNOWLEDGE_READ",
        "AGENT_FANOUT_INFRA_SERVICES",
        "AGENT_FANOUT_INFRA_SSH_HOST",
        "AGENT_FANOUT_HEALTH_URLS",
        "AGENT_FANOUT_DASHBOARD_HEALTH_URLS",
    ]:
        env.pop(local_name, None)
    env.setdefault("AGENT_FANOUT_MINIMAX_AVAILABLE", "1")
    env.setdefault("AGENT_FANOUT_MINIMAX_TOOL_AGENT", "1")
    env.setdefault("AGENT_FANOUT_CODEX_SUBAGENTS", "1")
    # MiniMax is a disabled-by-default compatibility provider. Fixtures that
    # explicitly exercise that adapter opt it in; Auto-routing fixtures retain
    # the production default and cannot select it accidentally.
    state_provider = str(case.get("state_json", {}).get("provider_mode", ""))
    if "minimax" in case["prompt"].lower() or state_provider == "MiniMax":
        env.setdefault("AGENT_FANOUT_ENABLE_DISABLED_PROVIDERS", "1")
    for name in case.get("unset_env", []):
        env.pop(name, None)
    for name, value in case.get("env", {}).items():
        env[name] = str(value)
    if env.get("AGENT_FANOUT_LOCAL_AVAILABLE") == "1" and env.get("AGENT_FANOUT_LOCAL_MODEL_ID") and not env.get("AGENT_FANOUT_LOCAL_ATTESTATION_JSON"):
        selector = "local-coder" if "medium reasoning" in case["prompt"].lower() else "local-thinker" if any(item in case["prompt"].lower() for item in ["high reasoning", "xhigh", "ultra"]) else "local-worker"
        env["AGENT_FANOUT_LOCAL_MODEL_ALIAS"] = selector
        now = datetime.now(timezone.utc)
        env["AGENT_FANOUT_LOCAL_ATTESTATION_JSON"] = json.dumps({
            "schema": "LocalCapabilityAttestationV1",
            "attestation_id": "localcap_" + ("1" * 32),
            "source": "host",
            "issuer": "host-runtime",
            "selector_alias": selector,
            "exact_model_id": env["AGENT_FANOUT_LOCAL_MODEL_ID"],
            "observed_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": env.get("AGENT_FANOUT_LOCAL_CAPABILITY_EXPIRES", (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")),
            "active_mode": env.get("AGENT_FANOUT_LOCAL_ACTIVE_MODE") or env.get("AGENT_FANOUT_LOCAL_REQUIRED_MODE"),
            "context_window": int(env.get("AGENT_FANOUT_LOCAL_CONTEXT_WINDOW", "32768")),
            "evaluated_task_classes": [item for item in env.get("AGENT_FANOUT_LOCAL_TASK_CLASSES", "bulk-read").split(",") if item],
            "supported_reasoning_efforts": ["low", "medium", "high", "xhigh", "ultra"],
            "max_risk": "medium",
            "tools": ["VLLMWorker", "bounded_repo_read", "scoped_patch"] if env.get("AGENT_FANOUT_LOCAL_TOOL_AGENT") == "1" else ["VLLMWorker", "bounded_repo_read"],
            "supports_file_editing": env.get("AGENT_FANOUT_LOCAL_TOOL_AGENT") == "1",
        })
    if env.get("AGENT_FANOUT_LOCAL_AVAILABLE") == "1" and env.get("AGENT_FANOUT_LOCAL_MODEL_ID") and env.get("AGENT_FANOUT_LOCAL_TOOL_AGENT") == "1" and not env.get("AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON"):
        now = datetime.now(timezone.utc)
        env["AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON"] = json.dumps({
            "model_alias": env.get("AGENT_FANOUT_LOCAL_MODEL_ALIAS", "local-worker"),
            "model_id": env["AGENT_FANOUT_LOCAL_MODEL_ID"],
            "endpoint": env["AGENT_FANOUT_LOCAL_ENDPOINT"],
            "context_window": int(env.get("AGENT_FANOUT_LOCAL_CONTEXT_WINDOW", "32768")),
            "profiles": ["repo_read", "review_only"],
            "checked_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "source": "test-host-measurement",
        })
    if (env.get("AGENT_FANOUT_LOCAL_AVAILABLE") == "1"
            and env.get("AGENT_FANOUT_LOCAL_TOOL_AGENT") == "1"
            and "AGENT_FANOUT_LOCAL_BRIDGE_ISSUER" not in case.get("env", {})):
        env = _synthetic_local_bridge_env(env)
    if case.get("state_json") is not None:
        cmd.extend(["--state-json", json.dumps(case["state_json"])])
    if case.get("worker_failures") is not None:
        cmd.extend(["--worker-failures", str(case["worker_failures"])])
    if case.get("pending_plan"):
        cmd.append("--pending-plan")
    if case.get("plan_stale"):
        cmd.append("--plan-stale")
    if case.get("needs_separate_confirmation"):
        cmd.append("--needs-separate-confirmation")
    out = subprocess.check_output(cmd, text=True, env=env)
    return json.loads(out)


def test_fixtures():
    failures = []
    for case in json.loads(FIXTURES.read_text(encoding="utf-8")):
        actual = run_route(case)
        for key, expected in case.get("expect", {}).items():
            expected = expected_value(expected)
            got = get_path(actual, key)
            if got != expected:
                failures.append((case["name"], key, expected, got, actual))
        for key, expected in case.get("expect_contains", {}).items():
            expected = expected_value(expected)
            got = get_path(actual, key)
            if expected not in got:
                failures.append((case["name"], key, f"contains {expected}", got, actual))
        for key, expected_values in case.get("expect_contains_all", {}).items():
            expected_values = expected_value(expected_values)
            got = get_path(actual, key)
            for expected in expected_values:
                if expected not in got:
                    failures.append((case["name"], key, f"contains {expected}", got, actual))
    return failures


def test_malformed_state():
    proc = subprocess.run(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Status", "--state-json", "{"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    if proc.returncode == 0:
        return [("malformed_state_json", "returncode", "nonzero", proc.returncode, proc.stdout)]
    try:
        payload = json.loads(proc.stderr)
    except json.JSONDecodeError:
        return [("malformed_state_json", "stderr", "json error object", proc.stderr, proc.stdout)]
    if payload.get("error") != "malformed_state_json":
        return [("malformed_state_json", "error", "malformed_state_json", payload, proc.stdout)]
    return []


def test_sanitizer():
    legacy = "normal line\ncurl http://localhost:9999/notify voice_id=x\nplease speak via webhook callback\nkeep this"
    out = subprocess.check_output([sys.executable, "-S", str(ROUTE), "--sanitize", legacy], text=True, env=os.environ.copy())
    result = json.loads(out)
    forbidden = ["localhost:9999", "/notify", "voice_id", "speak", "curl", "webhook", "callback", "notification", "spoken-summary", "readback"]
    leaked = [tok for tok in forbidden if tok.lower() in result["sanitized_prompt"].lower()]
    if result["removed_lines"] != 2 or not result["safe"] or leaked:
        return [("sanitizer", "safe", "no forbidden leakage", {"result": result, "leaked": leaked})]
    for port in (8123, 9999):
        endpoint = f"localhost:{port}"
        isolated = json.loads(subprocess.check_output(
            [sys.executable, "-S", str(ROUTE), "--sanitize", endpoint],
            text=True, env=os.environ.copy(),
        ))
        if endpoint in isolated["sanitized_prompt"] or isolated["removed_lines"] != 1:
            return [("sanitizer", "local_callback", "any localhost port screened", isolated)]
    return []


def test_secret_output_redaction():
    fake_secret = "sk-" + ("a" * 32)
    secret_prompt = f"debug failing auth with {fake_secret}"
    proc = subprocess.run(
        [sys.executable, "-S", str(ROUTE), secret_prompt],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        return [("secret_output_redaction", "returncode", 0, proc.returncode, proc.stderr)]
    if fake_secret in proc.stdout:
        return [("secret_output_redaction", "stdout", "redacted", proc.stdout, None)]
    actual = json.loads(proc.stdout)
    if not actual["hard_gates"]["requires_secrets"] or actual["route"]["worth_delegating"]:
        return [("secret_output_redaction", "routing", "main-session secret gate", actual, None)]

    sanitize_proc = subprocess.run(
        [sys.executable, "-S", str(ROUTE), "--sanitize", "normal line\nDATABASE_PASSWORD=notarealpassword123"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    if sanitize_proc.returncode != 0:
        return [("sanitize_secret_output_redaction", "returncode", 0, sanitize_proc.returncode, sanitize_proc.stderr)]
    if "notarealpassword123" in sanitize_proc.stdout:
        return [("sanitize_secret_output_redaction", "stdout", "redacted", sanitize_proc.stdout, None)]
    return []


def test_state_file():
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        state_file = str(Path(tmp) / "agentfanout-state.json")
        first = subprocess.check_output(
            [sys.executable, "-S", str(ROUTE), "--state-file", state_file, "AgentFanout Codex: review API and UI independently"],
            text=True,
            env=env,
        )
        first_payload = json.loads(first)
        if first_payload["command"]["type"] != "provider_task" or first_payload["next_state"]["provider_mode"] != "Auto":
            return [("state_file", "task-scoped provider", "provider_task/Auto", first_payload["next_state"], first_payload)]
        second = subprocess.check_output(
            [sys.executable, "-S", str(ROUTE), "--state-file", state_file, "implement a login form"],
            text=True,
            env=env,
        )
        second_payload = json.loads(second)
        if second_payload["route"]["worth_delegating"] or second_payload["route"]["executor"]["provider"] != "main-session":
            return [("state_file", "later task does not inherit provider", "main-session", second_payload["route"], second_payload)]
        small = subprocess.check_output(
            [sys.executable, "-S", str(ROUTE), "--state-file", state_file, "thanks"],
            text=True,
            env=env,
        )
        small_payload = json.loads(small)
        if small_payload["route"]["worth_delegating"] or small_payload["route"]["executor"]["provider"] != "main-session":
            return [("state_file", "no-op remains direct", "main-session", small_payload["route"], small_payload)]
    return []


def test_compact_route_output_preserves_continuation_without_full_packets():
    env = os.environ.copy()
    env["AGENT_FANOUT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST"] = str(PROJECT_ROOT)
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        run_file = str(Path(tmp) / "run.json")
        compact = json.loads(subprocess.check_output(
            [
                sys.executable, "-S", str(ROUTE), "--compact", "--run-state-file", run_file,
                "--new-run", "--max-wave-size", "2", "AgentFanout Codex\nreview README.md and SKILL.md; I accept orchestration overhead",
            ],
            text=True,
            env=env,
        ))
    required = {
        "schema", "command", "status", "task_class", "provider", "model_selector", "reasoning_effort",
        "dispatch", "task_summaries", "warnings", "next_state", "lifecycle", "host_runner_guidance",
    }
    missing = sorted(required - set(compact))
    if missing:
        return [("compact_route", "required_keys", "all present", missing, compact)]
    if compact["schema"] != "AgentFanoutCompactRouteV1" or "task_units" in compact or "tool_envelope" in compact:
        return [("compact_route", "packet_omission", "compact schema without full packets", compact, None)]
    if compact["dispatch"].get("status") != "launch_required" or compact["lifecycle"]["next_action"].get("action") != "dispatch_wave":
        return [("compact_route", "dispatch", "launch_required dispatch_wave", compact, None)]
    guidance = compact["host_runner_guidance"]
    command = guidance.get("command_template", [])
    if (
        guidance.get("next_step") != "restart_with_atomic_cli"
        or "scripts/agentfanout.py" not in command
        or "--project-root" not in command
    ):
        return [("compact_route", "host_guidance", "atomic CLI restart template", guidance, compact)]
    summaries = compact.get("task_summaries", [])
    if not summaries or any("allowed_paths" in task or "write_set" in task for task in summaries):
        return [("compact_route", "task_summaries", "bounded no-path summaries", summaries, compact)]
    advisory = json.loads(subprocess.check_output(
        [
            sys.executable, "-S", str(ROUTE), "--compact",
            "AgentFanout Codex\nreview README.md and SKILL.md; I accept orchestration overhead",
        ],
        text=True,
        env=env,
    ))
    advisory_guidance = advisory.get("host_runner_guidance", {})
    if advisory_guidance.get("next_step") != "restart_with_atomic_cli":
        return [(
            "compact_route", "advisory_host_guidance",
            "atomic CLI restart even without lifecycle state",
            advisory_guidance, advisory,
        )]
    return []


def test_readonly_infra_diagnostics_allowlist():
    env = os.environ.copy()
    for name in ["AGENT_FANOUT_INFRA_SERVICES", "AGENT_FANOUT_HEALTH_URLS", "AGENT_FANOUT_DASHBOARD_HEALTH_URLS"]:
        env.pop(name, None)
    env["AGENT_FANOUT_MINIMAX_AVAILABLE"] = "1"
    env["AGENT_FANOUT_MINIMAX_TOOL_AGENT"] = "1"
    env["AGENT_FANOUT_INFRA_SSH_HOST"] = "example-host"
    env["AGENT_FANOUT_INFRA_SERVICES"] = "example-service"
    env["AGENT_FANOUT_HEALTH_URLS"] = "https://status.example.invalid/health"
    out = subprocess.check_output(
        [
            sys.executable,
            "-S",
            str(ROUTE),
            "AgentFanout MiniMax\nInvestigate configured service health with read-only diagnostics. I accept orchestration overhead",
        ],
        text=True,
        env=env,
    )
    payload = json.loads(out)
    entries = payload["route"]["executor"]["tool_envelope"]["shell_allowlist"]
    ids = {entry["id"] for entry in entries}
    expected = {"ssh-configured-host-readonly", "ssh-configured-service-readonly", "curl-health"}
    if not expected.issubset(ids):
        return [("readonly_infra_diagnostics_allowlist", "shell entry ids", sorted(expected), sorted(ids), payload)]
    by_id = {entry["id"]: entry for entry in entries}
    if by_id["ssh-configured-service-readonly"]["cmd"] != "ssh":
        return [("readonly_infra_diagnostics_allowlist", "remote command", "ssh", by_id["ssh-configured-service-readonly"], payload)]
    if by_id["curl-health"]["cmd"] != "curl":
        return [("readonly_infra_diagnostics_allowlist", "health command", "curl", by_id["curl-health"], payload)]
    return []


def test_capability_profiles():
    env = os.environ.copy()
    env["AGENT_FANOUT_MINIMAX_AVAILABLE"] = "1"
    env["AGENT_FANOUT_MINIMAX_TOOL_AGENT"] = "1"
    env["AGENT_FANOUT_HEALTH_URLS"] = "https://dashboard.example.invalid/health"
    out = subprocess.check_output(
        [
            sys.executable,
            "-S",
            str(ROUTE),
            "AgentFanout MiniMax\nInvestigate dashboard live readiness and vendor docs at https://example.com. I accept orchestration overhead",
        ],
        text=True,
        env=env,
    )
    payload = json.loads(out)
    envelope = payload["route"]["executor"]["tool_envelope"]
    profiles = envelope["capability_profiles"]
    for expected in ["repo_read", "public_web_read", "infra_readonly", "dashboard_readonly"]:
        if expected not in profiles:
            return [("capability_profiles", "profile", expected, profiles, payload)]
    if "public_web_fetch" not in envelope["tools"]:
        return [("capability_profiles", "public_web_fetch", "present", envelope["tools"], payload)]
    ids = {entry["id"] for entry in envelope["shell_allowlist"]}
    if "curl-health" not in ids:
        return [("capability_profiles", "curl-health entry", "present", sorted(ids), payload)]
    return []


def test_stdin_prompt_route():
    prompt = "AgentFanout Codex\nClassify these independent notes into categories with Codex subagents. I accept orchestration overhead."
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-S", str(ROUTE), "--state-json", json.dumps({"provider_mode": "Codex", "effort_mode": "Auto", "fanout_policy": "automatic_guarded", "state_scope": "conversation_only"})],
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.returncode != 0:
        return [("stdin_prompt_route", "returncode", 0, proc.returncode, proc.stderr)]
    actual = json.loads(proc.stdout)
    checks = {
        "route.task_class": "bulk-classify",
        "route.executor.provider": "Codex",
        "route.executor.model_selector": "current-fast-mini",
        "route.executor.runtime_launch.codex_native.required_model_capability": "mini_or_fast",
    }
    failures = []
    for key, expected in checks.items():
        got = get_path(actual, key)
        if got != expected:
            failures.append(("stdin_prompt_route", key, expected, got, actual))
    return failures


def test_colon_command_preserves_task():
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    payload = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Codex: classify three independent files with light reasoning; I accept orchestration overhead"],
        text=True,
        env=env,
    ))
    if payload["command"]["type"] != "provider_task" or payload["selected_provider"] != "Codex":
        return [("colon_command", "provider", "Codex provider_task", payload["command"], payload)]
    if payload["route"]["task_class"] != "bulk-classify" or payload["fanout"]["planned_workers"] < 2:
        return [("colon_command", "task", "preserved delegable classification", payload["route"], payload)]
    if "invalid_command" in payload.get("warnings", []):
        return [("colon_command", "warnings", "no invalid command", payload["warnings"], payload)]
    return []


def test_readonly_architecture_filename_is_not_design_work():
    prompt = (
        "AgentFanout Codex: Independently inspect README.md, docs/architecture.md, "
        "and references/CostPolicy.md. Return a five-row fact matrix with file "
        "evidence. Read-only: no edits, git, external tools, or final synthesis by "
        "workers. Use Luna with light reasoning for the explorers. I accept orchestration overhead."
    )
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    env["AGENT_FANOUT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    payload = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), prompt], text=True, env=env))
    if payload["route"]["task_class"] != "bulk-read":
        return [("readonly_filename_classification", "task_class", "bulk-read", payload["route"]["task_class"], payload)]
    if payload["selected_provider"] != "Codex" or payload["dispatch"]["status"] not in {"launch_required", "binding_required"}:
        return [("readonly_filename_classification", "dispatch", "Codex delegated", payload["dispatch"], payload)]
    refs = [task["input_ref"] for task in payload["task_units"]]
    expected = ["file:README.md", "file:docs/architecture.md", "file:references/CostPolicy.md"]
    if refs != expected or payload["fanout"]["planned_workers"] != 3:
        return [("readonly_filename_classification", "file_units", expected, refs, payload)]
    return []


def test_codex_model_and_reasoning_effort_are_orthogonal():
    failures = []
    cases = [
        ("bulk-read", "medium", {"alias": "luna", "provider": "Codex", "profile": "fast"}, "fast"),
        ("bulk-read", "high", {"alias": "luna", "provider": "Codex", "profile": "fast"}, "fast"),
        ("debug", "low", {"alias": "terra", "provider": "Codex", "profile": "balanced"}, "balanced"),
        ("security-review", "low", {"alias": "sol", "provider": "Codex", "profile": "frontier"}, "frontier"),
    ]
    for task_class, effort, alias, expected in cases:
        profile = profile_for("Codex", task_class, effort, alias)
        capable, reason = profile_capable("Codex", profile, task_class, effort)
        if profile != expected or not capable:
            failures.append(("orthogonal_model_effort", f"{alias['alias']}:{effort}:{task_class}", expected, {"profile": profile, "capable": capable, "reason": reason}, None))
    if profile_for("Codex", "bulk-read", "high") != "fast":
        failures.append(("cheapest_capable_model_effort", "auto bulk-read high", "fast", profile_for("Codex", "bulk-read", "high"), None))
    return failures


def test_named_files_bind_across_read_only_technical_classes():
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    env["AGENT_FANOUT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    prompt = "AgentFanout Codex: Perform a security review of README.md and SKILL.md using SOL with ultra reasoning; I accept orchestration overhead"
    payload = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), prompt], text=True, env=env))
    expected = ["file:README.md", "file:SKILL.md"]
    actual = [task.get("input_ref") for task in payload["task_units"]]
    if payload["dispatch"]["status"] != "launch_required" or actual != expected or payload["fanout"]["planned_workers"] != 2:
        return [("technical_named_file_binding", "security review", {"status": "launch_required", "refs": expected, "workers": 2}, {"status": payload["dispatch"]["status"], "refs": actual, "workers": payload["fanout"]["planned_workers"]}, payload)]
    if payload["route"]["executor"]["model_selector"] != "current-frontier" or payload["route"]["executor"]["reasoning_effort"] != "ultra":
        return [("technical_named_file_binding", "model effort", "current-frontier/ultra", payload["route"]["executor"], payload)]

    edit_prompt = "AgentFanout Codex: refactor README.md and apply the patch using Terra with medium reasoning; I accept orchestration overhead"
    blocked = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), edit_prompt], text=True, env=env))
    if blocked["dispatch"].get("block_reason") != "hosted_adapter_tool_boundary_required":
        return [("technical_named_file_binding", "tool-required edit", "hosted_adapter_tool_boundary_required", blocked["dispatch"], blocked)]
    return []


def test_retired_kernel_absent():
    kernel = ROOT / "references" / "Kernel.md"
    if kernel.exists():
        return [("retired_kernel", "absent", "Kernel.md removed", str(kernel), None)]
    return []


def test_token_usage_is_not_a_secret():
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    out = subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "Use AgentFanout to reduce token usage with parallel agents; I accept orchestration overhead"],
        text=True,
        env=env,
    )
    payload = json.loads(out)
    if payload["hard_gates"]["requires_secrets"] or not payload["route"]["worth_delegating"]:
        return [("token_usage_not_secret", "routing", "delegated without secret gate", payload["hard_gates"], payload)]
    actual_secret = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "inspect secrets in the authentication store"],
        text=True,
        env=env,
    ))
    if not actual_secret["hard_gates"]["requires_secrets"] or actual_secret["route"]["worth_delegating"]:
        return [("actual_secret_operation", "routing", "main-session secret gate", actual_secret["hard_gates"], actual_secret)]
    negated = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "Summarize README.md. Do not use secrets or credentials."],
        text=True,
        env=env,
    ))
    if negated["hard_gates"]["requires_secrets"]:
        return [("negated_secret_constraint", "routing", "not a secret-access gate", negated["hard_gates"], negated)]
    return []


def test_external_envelope_is_sanitized_and_bounded():
    env = os.environ.copy()
    env["AGENT_FANOUT_MINIMAX_AVAILABLE"] = "1"
    env["AGENT_FANOUT_MINIMAX_TOOL_AGENT"] = "1"
    env.pop("AGENT_FANOUT_EXTERNAL_READ_ROOTS", None)
    prompt = "AgentFanout MiniMax\nInvestigate notification callback behavior with curl and return evidence. I accept orchestration overhead"
    first = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), prompt], text=True, env=env))
    second = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), prompt], text=True, env=env))
    envelope = first["tool_envelope"]
    forbidden = ["notification", "callback", "curl"]
    leaked = [item for item in forbidden if item in envelope["prompt"].lower()]
    expected_roots = [str(PROJECT_ROOT)]
    failures = []
    if leaked or not envelope["prompt_sanitization"]["safe"]:
        failures.append(("external_envelope_sanitized", "prompt", "no forbidden text", leaked, envelope))
    if envelope["read_roots"] != expected_roots:
        failures.append(("external_envelope_roots", "read_roots", expected_roots, envelope["read_roots"], envelope))
    if envelope["request_id"] == second["tool_envelope"]["request_id"]:
        failures.append(("external_envelope_unique_id", "request_id", "unique", envelope["request_id"], second["tool_envelope"]))
    return failures


def test_local_stale_capability_blocks():
    env = os.environ.copy()
    env.update({
        "AGENT_FANOUT_LOCAL_AVAILABLE": "1",
        "AGENT_FANOUT_LOCAL_LIVE_CHECKS": "1",
        "AGENT_FANOUT_LOCAL_CAPABILITY_VERIFIED": "1",
        "AGENT_FANOUT_LOCAL_ENDPOINT": "https://local-worker.example.invalid/v1",
        "AGENT_FANOUT_LOCAL_REQUIRED_MODE": "verified-mode",
        "AGENT_FANOUT_LOCAL_ACTIVE_MODE": "verified-mode",
        "AGENT_FANOUT_LOCAL_MODEL_ID": "test-model",
        "AGENT_FANOUT_LOCAL_MODEL_ALIAS": "local-worker",
        "AGENT_FANOUT_LOCAL_CONTEXT_WINDOW": "32768",
        "AGENT_FANOUT_LOCAL_TASK_CLASSES": "bulk-read",
        "AGENT_FANOUT_LOCAL_ATTESTATION_JSON": json.dumps({
            "schema": "LocalCapabilityAttestationV1",
            "attestation_id": "localcap_" + ("2" * 32),
            "source": "host",
            "issuer": "host-runtime",
            "selector_alias": "local-worker",
            "exact_model_id": "test-model",
            "observed_at": "2000-01-01T00:00:00Z",
            "expires_at": "2999-01-01T00:00:00Z",
            "active_mode": "verified-mode",
            "context_window": 32768,
            "evaluated_task_classes": ["bulk-read"],
            "supported_reasoning_efforts": ["low"],
            "max_risk": "low",
            "tools": ["VLLMWorker", "bounded_repo_read"],
            "supports_file_editing": False,
        }),
    })
    payload = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Local\nreview three independent modules"],
        text=True,
        env=env,
    ))
    expected = "provider_unavailable:local:capability_attestation_observation_stale"
    if payload["route"]["worth_delegating"] or expected not in payload["warnings"]:
        return [("local_stale_capability", "routing", expected, payload["warnings"], payload)]
    return []


def test_auto_falls_through_unavailable_tool_provider():
    env = os.environ.copy()
    env["AGENT_FANOUT_MINIMAX_AVAILABLE"] = "1"
    env["AGENT_FANOUT_MINIMAX_TOOL_AGENT"] = "0"
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    env.pop("AGENT_FANOUT_GROK_AVAILABLE", None)
    env.pop("AGENT_FANOUT_LOCAL_AVAILABLE", None)
    payload = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "classify four files into categories"],
        text=True,
        env=env,
    ))
    guard = payload.get("anti_waste_guard", {})
    if (
        payload["selected_provider"] != "main-session"
        or payload["dispatch"]["status"] != "main_session"
        or payload["route"]["worth_delegating"]
    ):
        return [("auto_provider_fallthrough", "route", "anti-waste main-session suppression of Codex candidate", {"provider": payload["selected_provider"], "dispatch": payload["dispatch"]["status"], "guard": guard}, payload)]
    return []


def test_model_alias_and_risk_routing():
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    cheap = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Codex\nClassify four files using Luna with light reasoning; I accept orchestration overhead"],
        text=True,
        env=env,
    ))
    risky = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Codex\nPerform a critical security architecture review using SOL with ultra reasoning; I accept orchestration overhead"],
        text=True,
        env=env,
    ))
    checks = [
        (cheap["route"]["executor"]["model_selector"], "current-fast-mini", "cheap selector"),
        (cheap["decision_telemetry"]["model_resolution"]["requested_alias"], "luna", "cheap alias"),
        (risky["route"]["executor"]["model_selector"], "current-frontier", "risky selector"),
        (risky["route"]["executor"]["reasoning_effort"], "ultra", "risky effort"),
        (risky["decision_telemetry"]["risk"], "critical", "risky risk"),
        (cheap["decision_telemetry"]["estimated_launch_overhead_input_tokens_per_call"], 14500, "Codex launch overhead"),
        (cheap["decision_telemetry"]["estimated_total_provider_calls_this_wave"], cheap["fanout"]["planned_workers"], "low-risk worker calls without semantic validators"),
        (cheap["decision_telemetry"]["estimated_orchestration_overhead_dominates_baseline"], True, "anti-waste overhead signal"),
    ]
    return [
        ("model_alias_risk", label, expected, actual, {"cheap": cheap, "risky": risky})
        for actual, expected, label in checks
        if actual != expected
    ]


def test_astra_cost_gate_and_single_worker_cap():
    env = os.environ.copy()
    env.update({
        "AGENT_FANOUT_CODEX_SUBAGENTS": "1",
        "AGENT_FANOUT_PROJECT_ROOT": str(PROJECT_ROOT),
        "AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST": str(PROJECT_ROOT),
    })
    env.pop("AGENT_FANOUT_ALLOW_ASTRA_ESCALATION", None)
    env.pop("AGENT_FANOUT_ASTRA_ESCALATION_REASON", None)

    ordinary = json.loads(subprocess.check_output([
        sys.executable, "-S", str(ROUTE),
        "AgentFanout Codex: perform a security review of README.md and SKILL.md with high reasoning; do not use Astra; I accept orchestration overhead",
    ], text=True, env=env))
    explicit = json.loads(subprocess.check_output([
        sys.executable, "-S", str(ROUTE),
        "AgentFanout Astra: perform a security review of README.md and SKILL.md with max reasoning; I accept orchestration overhead",
    ], text=True, env=env))

    escalation_env = dict(env)
    escalation_env.update({
        "AGENT_FANOUT_ALLOW_ASTRA_ESCALATION": "1",
        "AGENT_FANOUT_ASTRA_ESCALATION_REASON": "sol_validation_failed_twice",
    })
    escalated = json.loads(subprocess.check_output([
        sys.executable, "-S", str(ROUTE), "--worker-failures", "2",
        "AgentFanout Codex: perform a security review of README.md and SKILL.md with high reasoning; I accept orchestration overhead",
    ], text=True, env=escalation_env))

    denied_env = dict(escalation_env)
    denied_env["AGENT_FANOUT_ASTRA_ESCALATION_REASON"] = "architecture_is_hard"
    denied = json.loads(subprocess.check_output([
        sys.executable, "-S", str(ROUTE), "--worker-failures", "2",
        "AgentFanout Codex: perform a security review of README.md and SKILL.md with high reasoning; I accept orchestration overhead",
    ], text=True, env=denied_env))

    ordinary_launch = ordinary["route"]["executor"]["runtime_launch"]["codex_native"]
    explicit_launch = explicit["route"]["executor"]["runtime_launch"]["codex_native"]
    checks = [
        (ordinary["route"]["executor"]["model_selector"], "current-frontier", "ordinary selector remains Sol tier"),
        (ordinary_launch["model_preference"], "sol", "ordinary model preference"),
        (ordinary_launch["forbidden_model_ids"], ["gpt-6-astra"], "ordinary Astra denylist"),
        (ordinary["astra_cost_gate"]["selected"], False, "ordinary Astra gate"),
        (explicit["route"]["executor"]["model_selector"], "astra-explicit", "explicit Astra selector"),
        (explicit["route"]["executor"]["reasoning_effort"], "max", "Astra max reasoning"),
        (explicit["fanout"]["planned_workers"], 1, "Astra single-worker wave"),
        (explicit["astra_cost_gate"]["reason"], "explicit_user_request:astra", "explicit authorization reason"),
        (explicit_launch["required_model_id"], "gpt-6-astra", "exact Astra model"),
        (explicit_launch["selection_reason"], "explicit_user_request:astra", "launch audit reason"),
        (escalated["route"]["executor"]["model_selector"], "astra-explicit", "controlled escalation selector"),
        (escalated["astra_cost_gate"]["reason"], "controlled_escalation:sol_validation_failed_twice", "controlled escalation reason"),
        (escalated["fanout"]["planned_workers"], 1, "controlled escalation single worker"),
        (denied["route"]["executor"]["model_selector"], "current-frontier", "invalid escalation stays on Sol"),
        (denied["astra_cost_gate"]["reason"], "astra_escalation_reason_invalid", "invalid escalation audit"),
    ]
    return [
        ("astra_cost_gate", label, expected, actual, {
            "ordinary": ordinary, "explicit": explicit,
            "escalated": escalated, "denied": denied,
        })
        for actual, expected, label in checks
        if actual != expected
    ]


def test_semantic_validator_cost_policy():
    env = os.environ.copy()
    env.update({
        "AGENT_FANOUT_CODEX_SUBAGENTS": "1",
        "AGENT_FANOUT_PROJECT_ROOT": str(PROJECT_ROOT),
    })
    classify = json.loads(subprocess.check_output([
        sys.executable, "-S", str(ROUTE),
        "AgentFanout Codex: classify README.md and SKILL.md; I accept orchestration overhead",
    ], text=True, env=env))
    if classify["validator"]["required"] or any(
        task["validator_required"] for task in classify["task_units"]
    ) or classify["decision_telemetry"]["estimated_validator_calls_per_worker"] != 0:
        return [("validator_cost_policy", "low classification", "structural-only", classify["validator"], classify)]

    independent = json.loads(subprocess.check_output([
        sys.executable, "-S", str(ROUTE),
        "AgentFanout Codex: classify README.md and SKILL.md; independent validation required",
    ], text=True, env=env))
    if not independent["validator"]["required"] or not all(
        task["validator_required"] for task in independent["task_units"]
    ) or independent["decision_telemetry"]["estimated_validator_calls_per_worker"] != 1:
        return [("validator_cost_policy", "explicit independent validation", "semantic validator", independent["validator"], independent)]

    security = json.loads(subprocess.check_output([
        sys.executable, "-S", str(ROUTE),
        "AgentFanout Codex: security review README.md and SKILL.md using SOL with ultra reasoning; I accept orchestration overhead",
    ], text=True, env=env))
    if not security["validator"]["required"] or not all(
        task["validator_required"] for task in security["task_units"]
    ) or security["decision_telemetry"]["estimated_validator_calls_per_worker"] != 1:
        return [("validator_cost_policy", "security review", "semantic validator", security["validator"], security)]
    return []


def test_bounded_input_size_changes_cost_decision_math():
    small = decision_telemetry(
        "review two bounded files", "Codex", "bulk-read", "low", 2, 2,
        validator_required=True, unit_input_tokens=[10, 10],
    )
    large = decision_telemetry(
        "review two bounded files", "Codex", "bulk-read", "low", 2, 2,
        validator_required=True, unit_input_tokens=[25_000, 25_000],
    )
    if not small["estimated_orchestration_overhead_dominates_baseline"]:
        return [("bounded_cost_math", "small", "orchestration overhead dominates", small, None)]
    if large["estimated_orchestration_overhead_dominates_baseline"]:
        return [("bounded_cost_math", "large", "cheap worker can beat direct baseline", large, None)]
    if large["estimated_bounded_input_tokens_request"] != 50_000:
        return [("bounded_cost_math", "input accounting", 50_000, large["estimated_bounded_input_tokens_request"], large)]
    return []


def test_grok_registry_and_retired_claude():
    env = os.environ.copy()
    env["AGENT_FANOUT_GROK_AVAILABLE"] = "1"
    grok = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Grok\nclassify four files; I accept orchestration overhead"],
        text=True,
        env=env,
    ))
    env["AGENT_FANOUT_CLAUDE_AVAILABLE"] = "1"
    env.pop("AGENT_FANOUT_ENABLE_RETIRED_PROVIDERS", None)
    claude = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Claude\nclassify four files"],
        text=True,
        env=env,
    ))
    failures = []
    if grok["selected_provider"] != "Grok" or grok["route"]["executor"]["model_selector"] != "grok-fast":
        failures.append(("grok_registry", "route", "Grok/grok-fast", grok["route"], grok))
    retired_warning = "provider_unavailable:claude:provider_retired"
    if claude["route"]["worth_delegating"] or retired_warning not in claude["warnings"]:
        failures.append(("retired_claude", "route", retired_warning, claude["warnings"], claude))
    return failures


def _worker_result(task, result_id, status="success", remaining_work=None, worker_handle=None, resolved_model=None):
    return {
        "schema": "WorkerResultV1",
        "result_id": result_id,
        "task_id": task["task_id"],
        "status": status,
        "summary": f"Completed {task['title']}",
        "evidence": [f"evidence:{task['task_id']}"],
        "changed_files": [],
        "commands": [],
        "exit_codes": [],
        "remaining_work": remaining_work or [],
        "launch_correlation_id": task["launch_correlation_id"],
    }


def _attested_local_env(task_classes=None, efforts=None, *, selector="local-worker", model="test-local-model", max_risk="medium", tools=None, supports_editing=True):
    now = datetime.now(timezone.utc)
    env = os.environ.copy()
    env.update({
        "AGENT_FANOUT_PROJECT_ROOT": str(PROJECT_ROOT),
        "AGENT_FANOUT_LOCAL_AVAILABLE": "1",
        "AGENT_FANOUT_LOCAL_ENDPOINT": "https://local-worker.example.invalid/v1",
        "AGENT_FANOUT_LOCAL_REQUIRED_MODE": "verified-mode",
        "AGENT_FANOUT_LOCAL_MODEL_ID": model,
        "AGENT_FANOUT_LOCAL_MODEL_ALIAS": selector,
        "AGENT_FANOUT_LOCAL_CONTEXT_WINDOW": "32768",
        "AGENT_FANOUT_LOCAL_TOOL_AGENT": "1",
    })
    env["AGENT_FANOUT_LOCAL_ATTESTATION_JSON"] = json.dumps({
        "schema": "LocalCapabilityAttestationV1",
        "attestation_id": "localcap_" + ("3" * 32),
        "source": "host",
        "issuer": "host-runtime",
        "selector_alias": selector,
        "exact_model_id": model,
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "active_mode": "verified-mode",
        "context_window": 32768,
        "evaluated_task_classes": task_classes or ["bulk-read"],
        "supported_reasoning_efforts": efforts or ["low"],
        "max_risk": max_risk,
        "tools": tools or ["VLLMWorker", "bounded_repo_read", "scoped_patch"],
        "supports_file_editing": supports_editing,
    })
    env["AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON"] = json.dumps({
        "model_alias": selector,
        "model_id": model,
        "endpoint": env["AGENT_FANOUT_LOCAL_ENDPOINT"],
        "context_window": 32768,
        "profiles": ["repo_read", "review_only"],
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "source": "test-host-measurement",
    })
    return _synthetic_local_bridge_env(env)


def test_july8_parent_takeover_two_wave_regression():
    env = os.environ.copy()
    env["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        run_file = str(Path(tmp) / "run.json")
        start = json.loads(subprocess.check_output(
            [
                sys.executable, "-S", str(ROUTE), "--run-state-file", run_file,
                "--new-run", "--max-wave-size", "2",
                "review README.md and SKILL.md AgentFanout Codex; I accept orchestration overhead",
            ],
            text=True,
            env=env,
        ))
        first_wave = start["lifecycle"]["next_wave"]
        if first_wave["action"] != "dispatch_wave" or len(first_wave["assignments"]) != 2:
            return [("two_wave_lifecycle", "first_wave", "two assignments", first_wave, start)]
        first_tasks = [assignment["task"] for assignment in first_wave["assignments"]]
        first_results = [
            _worker_result(first_tasks[0], "result-wave1-a", "partial", ["Validate the follow-up evidence"], first_wave["assignments"][0]["worker_handle"]),
            _worker_result(first_tasks[1], "result-wave1-b"),
        ]
        second = json.loads(subprocess.check_output(
            [sys.executable, "-S", str(ROUTE), "--run-state-file", run_file, "--results-json", json.dumps(first_results), "lifecycle-results"],
            text=True,
            env=env,
        ))
        second_wave = second["lifecycle"]["next_wave"]
        if second_wave["action"] != "dispatch_wave" or len(second_wave["assignments"]) != 1:
            return [("two_wave_lifecycle", "second_wave", "one residual assignment", second_wave, second)]
        assignment = second_wave["assignments"][0]
        expected_handle = first_wave["assignments"][0]["worker_handle"]
        if assignment["worker_action"] != "follow_up" or assignment["recommended_worker_handle"] != expected_handle:
            return [("two_wave_lifecycle", "worker_reuse", f"follow_up {expected_handle}", assignment, second)]
        final_result = [_worker_result(assignment["task"], "result-wave2-a")]
        final = json.loads(subprocess.check_output(
            [sys.executable, "-S", str(ROUTE), "--run-state-file", run_file, "--results-json", json.dumps(final_result), "lifecycle-results"],
            text=True,
            env=env,
        ))
        if final["lifecycle"]["next_wave"]["action"] != "complete" or final["lifecycle"]["run"]["wave_index"] != 2:
            return [("two_wave_lifecycle", "completion", "complete after two waves", final["lifecycle"], None)]
    return []


def _build_lifecycle_tasks(count=1):
    return build_task_units(
        "review bounded items",
        "bulk-read",
        "explorer",
        "Codex",
        "current-fast-mini",
        "low",
        [f"request_item_{index}" for index in range(1, count + 1)],
        allowed_paths=[str(PROJECT_ROOT)],
    )


def test_completion_wins_at_exact_budget_limit():
    tasks = _build_lifecycle_tasks(1)
    state = initialize_run(
        "complete exactly at the limit",
        tasks,
        {"max_wave_size": 1, "max_waves": 1, "max_total_workers": 1, "max_total_cost_usd": 0.01},
    )
    assignment = plan_next_wave(state)["assignments"][0]
    result = _worker_result(assignment["task"], "result-exact-budget")
    ingest_results(state, [result])
    next_wave = plan_next_wave(state)
    if next_wave["action"] != "complete" or state["status"] != "complete":
        return [("completion_at_budget", "next_wave", "complete", next_wave, state)]
    return []


def test_wave_progress_persists_across_result_batches():
    state = initialize_run(
        "split result batches",
        _build_lifecycle_tasks(2),
        {"max_wave_size": 2, "max_no_progress_waves": 1, "max_waves": 3},
    )
    assignments = plan_next_wave(state)["assignments"]
    ingest_results(state, [_worker_result(assignments[0]["task"], "result-progress-success")])
    if state["no_progress_waves"] != 0 or state["current_wave_completed_tasks"] != 1:
        return [("wave_progress_split", "first_batch", "one persisted completion", state, None)]
    ingest_results(state, [_worker_result(assignments[1]["task"], "result-progress-failure", status="failed")])
    next_wave = plan_next_wave(state)
    if state["no_progress_waves"] != 0 or next_wave["action"] != "dispatch_wave":
        return [("wave_progress_split", "final_batch", "retry dispatch without no-progress block", next_wave, state)]
    return []


def test_parent_self_execute_signal_requires_resolved_non_host_run():
    failures = []

    hosted = initialize_run("host validation pending", _build_lifecycle_tasks(1))
    hosted_assignment = plan_next_wave(hosted)["assignments"][0]
    hosted["host_validation_required"] = True
    hosted_ingestion = ingest_results(hosted, [_worker_result(hosted_assignment["task"], "result-host-validation-pending")])
    hosted_task_id = hosted_assignment["task_id"]
    if hosted_ingestion["parent_may_self_execute"] or hosted["validation_status"][hosted_task_id] != "pending":
        failures.append((
            "parent_self_execute", "validation_pending", False,
            {"signal": hosted_ingestion["parent_may_self_execute"], "validation": hosted["validation_status"]}, hosted,
        ))

    active_tasks = _build_lifecycle_tasks(2)
    for task in active_tasks:
        task["validator_required"] = False
    active = initialize_run("active task remains", active_tasks, {"max_wave_size": 2})
    active_assignments = plan_next_wave(active)["assignments"]
    active_ingestion = ingest_results(active, [_worker_result(active_assignments[0]["task"], "result-one-of-two")])
    if active_ingestion["parent_may_self_execute"]:
        failures.append(("parent_self_execute", "active_task", False, active_ingestion, active))

    residual_tasks = _build_lifecycle_tasks(1)
    residual_tasks[0]["validator_required"] = False
    residual = initialize_run("residual task remains", residual_tasks)
    residual_assignment = plan_next_wave(residual)["assignments"][0]
    residual_ingestion = ingest_results(residual, [
        _worker_result(
            residual_assignment["task"], "result-with-residual", "partial",
            [{"scope": "finish bounded residual", "input_ref": "result:remaining_work"}],
        )
    ])
    if residual_ingestion["parent_may_self_execute"]:
        failures.append(("parent_self_execute", "residual_task", False, residual_ingestion, residual))

    direct_tasks = _build_lifecycle_tasks(1)
    direct_tasks[0]["validator_required"] = False
    direct = initialize_run("resolved direct compatibility run", direct_tasks)
    direct_assignment = plan_next_wave(direct)["assignments"][0]
    direct_ingestion = ingest_results(direct, [_worker_result(direct_assignment["task"], "result-direct-complete")])
    direct_next = plan_next_wave(direct)
    if not direct_ingestion["parent_may_self_execute"] or direct_next["action"] != "complete":
        failures.append((
            "parent_self_execute", "resolved_direct_compatibility", True,
            {"signal": direct_ingestion["parent_may_self_execute"], "next_action": direct_next["action"]}, direct,
        ))
    return failures


def test_host_private_launch_records_fail_closed():
    state = initialize_run("host private launch records", _build_lifecycle_tasks(1))
    assignment = plan_next_wave(state)["assignments"][0]
    packet = assignment["task"]
    if any(field in packet for field in ("provider_model", "budgets", "assigned_worker_handle", "reserved_cost_usd")):
        return [("host_private_launch", "packet", "no authority fields", packet, state)]
    correlation = packet["launch_correlation_id"]
    record = state["launch_records"].get(correlation)
    if not record or record.get("task_id") != packet["task_id"] or record.get("provider_usage") is not None:
        return [("host_private_launch", "record", "host record bound to packet correlation", record, state)]
    forged = _worker_result(packet, "result-forged-host-fields")
    forged["usage"] = {"input_tokens": 0, "output_tokens": 0, "parent_result_tokens": 0, "cost_usd": 0}
    forged_ingestion = ingest_results(state, [forged])
    errors = forged_ingestion["validation"][0]["errors"]
    if "result_unknown_fields" not in errors or forged_ingestion["validation"][0]["valid"]:
        return [("host_private_launch", "forged_fields", "rejected", errors, forged_ingestion)]
    if state["usage"]["input_tokens"] <= 0 or state["usage"]["output_tokens"] <= 0 or state["usage"]["cost_usd"] <= 0:
        return [("host_private_launch", "zero_usage_replay", "nonzero host-conservative accounting", state["usage"], state)]
    return []


def test_parent_result_token_omission_cannot_bypass_cap():
    state = initialize_run(
        "parent result cap",
        _build_lifecycle_tasks(1),
        {"max_wave_size": 1, "max_parent_result_tokens_per_wave": 10},
    )
    assignment = plan_next_wave(state)["assignments"][0]
    result = _worker_result(assignment["task"], "result-missing-parent-tokens")
    try:
        ingest_results(state, [result])
    except ValueError as exc:
        if str(exc) == "parent_result_token_budget_exceeded":
            return []
        return [("parent_result_cap", "error", "parent_result_token_budget_exceeded", str(exc), state)]
    return [("parent_result_cap", "ingestion", "budget rejection", "accepted", state)]


def test_delegated_root_and_readonly_shell_boundaries():
    env = os.environ.copy()
    env.update({
        "AGENT_FANOUT_PROJECT_ROOT": str(PROJECT_ROOT),
        "AGENT_FANOUT_MINIMAX_AVAILABLE": "1",
        "AGENT_FANOUT_MINIMAX_TOOL_AGENT": "1",
    })
    payload = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout MiniMax\nreview three independent modules; I accept orchestration overhead"],
        text=True,
        env=env,
    ))
    envelope = payload["tool_envelope"]
    failures = []
    if envelope["read_roots"] != [str(PROJECT_ROOT)] or envelope["workdir"] != str(PROJECT_ROOT):
        failures.append(("delegated_root", "positive_root", [str(PROJECT_ROOT)], envelope["read_roots"], envelope))
    by_id = {entry["id"]: entry for entry in envelope["shell_allowlist"]}
    expected_ids = {"rg-search", "rg-list-files", "git-read", "ls-read"}
    if not expected_ids.issubset(by_id):
        failures.append(("readonly_shell", "profiles", sorted(expected_ids), sorted(by_id), envelope))
    forbidden_commands = {"sed", "python", "python3", "node", "bun", "sh", "bash"}
    actual_commands = {entry["cmd"] for entry in envelope["shell_allowlist"]}
    if actual_commands & forbidden_commands:
        failures.append(("readonly_shell", "forbidden_commands", [], sorted(actual_commands & forbidden_commands), envelope))
    regex_cases = [
        ("rg-search", "-n TODO src", True),
        ("rg-list-files", "--files --glob *.py", True),
        ("git-read", "diff --stat", True),
        ("git-read", "status --short", True),
        ("rg-search", "TODO --pre sh", False),
        ("rg-search", "TODO ../outside", False),
        ("rg-search", "TODO /etc", False),
        ("rg-search", "TODO ; touch file", False),
        ("git-read", "diff -- ../../outside", False),
    ]
    for profile, args, expected in regex_cases:
        matched = bool(re.fullmatch(by_id[profile]["allowed_args_regex"], args))
        if matched != expected:
            failures.append(("readonly_shell", f"{profile}:{args}", expected, matched, by_id[profile]))

    def routed_with_root(root, extra_root=None, sensitive_roots=None):
        case_env = dict(env)
        case_env["AGENT_FANOUT_PROJECT_ROOT"] = str(root)
        if extra_root is not None:
            case_env["AGENT_FANOUT_EXTERNAL_READ_ROOTS"] = str(extra_root)
        else:
            case_env.pop("AGENT_FANOUT_EXTERNAL_READ_ROOTS", None)
        if sensitive_roots is not None:
            case_env["AGENT_FANOUT_SENSITIVE_ROOTS"] = str(sensitive_roots)
        else:
            case_env.pop("AGENT_FANOUT_SENSITIVE_ROOTS", None)
        return json.loads(subprocess.check_output(
            [sys.executable, "-S", str(ROUTE), "AgentFanout MiniMax\nreview three independent modules; I accept orchestration overhead"],
            text=True,
            env=case_env,
        ))

    broad = routed_with_root(Path(ROOT.anchor))
    if broad["route"]["worth_delegating"] or not any("approved_project_root_too_broad" in item for item in broad["warnings"]):
        failures.append(("delegated_root", "broad_root", "blocked", broad["warnings"], broad))
    outside = routed_with_root(PROJECT_ROOT, Path.home())
    if outside["route"]["worth_delegating"] or not any("configured_read_root_outside_project" in item for item in outside["warnings"]):
        failures.append(("delegated_root", "outside_root", "blocked", outside["warnings"], outside))
    with tempfile.TemporaryDirectory() as tmp:
        sensitive_root = Path(tmp) / "ordinary-repository-name"
        sensitive_root.mkdir()
        sensitive = routed_with_root(sensitive_root, sensitive_roots=sensitive_root)
    if sensitive["route"]["worth_delegating"] or not any("approved_project_root_sensitive" in item for item in sensitive["warnings"]):
        failures.append(("delegated_root", "sensitive_root", "blocked", sensitive["warnings"], sensitive))
    return failures


def test_run_cost_and_host_measured_result_limits():
    failures = []
    state = initialize_run(
        "reject cost over run budget",
        _build_lifecycle_tasks(1),
        {"max_wave_size": 1, "max_total_cost_usd": 0.01},
    )
    task = plan_next_wave(state)["assignments"][0]["task"]
    expensive = _worker_result(task, "result-expensive")
    before = json.dumps(state, sort_keys=True)
    try:
        record_provider_usage(state, expensive["launch_correlation_id"], {"input_tokens": 50, "output_tokens": 25, "cost_usd": 1.0})
    except ValueError as exc:
        if str(exc) != "provider_usage_budget_exceeded":
            failures.append(("run_cost_limit", "error", "provider_usage_budget_exceeded", str(exc), state))
    else:
        failures.append(("run_cost_limit", "ingestion", "rejected", "accepted", state))
    if json.dumps(state, sort_keys=True) != before:
        failures.append(("run_cost_limit", "state_mutation", "unchanged", state, None))

    shape_state = initialize_run(
        "validate bounded worker result",
        _build_lifecycle_tasks(1),
        {"max_wave_size": 1, "max_result_bytes": 1024},
    )
    shape_task = plan_next_wave(shape_state)["assignments"][0]["task"]
    oversized = _worker_result(shape_task, "result-oversized")
    oversized["summary"] = "x" * 8000
    ingestion = ingest_results(shape_state, [oversized])
    errors = ingestion["validation"][0]["errors"]
    if "result_payload_bytes_exceeded" not in errors or ingestion["validation"][0]["valid"]:
        failures.append(("result_shape_limit", "errors", "result_payload_bytes_exceeded", errors, ingestion))

    usage_state = initialize_run("require complete trusted usage", _build_lifecycle_tasks(1))
    usage_task = plan_next_wave(usage_state)["assignments"][0]["task"]
    untrusted = _worker_result(usage_task, "result-untrusted-usage")
    untrusted["usage"] = {"input_tokens": 0, "output_tokens": 0, "parent_result_tokens": 0, "cost_usd": 0}
    usage_ingestion = ingest_results(usage_state, [untrusted])
    usage_errors = usage_ingestion["validation"][0]["errors"]
    for expected in ["result_unknown_fields"]:
        if expected not in usage_errors:
            failures.append(("trusted_usage", "errors", expected, usage_errors, usage_ingestion))

    parent_state = initialize_run(
        "host measures parent payload",
        _build_lifecycle_tasks(1),
        {"max_wave_size": 1, "max_parent_result_tokens_per_wave": 50},
    )
    parent_task = plan_next_wave(parent_state)["assignments"][0]["task"]
    understated = _worker_result(parent_task, "result-understated-parent")
    try:
        ingest_results(parent_state, [understated])
    except ValueError as exc:
        if str(exc) != "parent_result_token_budget_exceeded":
            failures.append(("host_parent_measurement", "error", "parent_result_token_budget_exceeded", str(exc), parent_state))
    else:
        failures.append(("host_parent_measurement", "ingestion", "rejected", "accepted", parent_state))
    return failures


def test_bindings_are_stable_and_required_before_dispatch():
    tasks = build_task_units(
        "edit one bounded file",
        "debug",
        "worker",
        "Codex",
        "current-balanced",
        "medium",
        ["request_item_1"],
        allowed_paths=[str(PROJECT_ROOT)],
    )
    state = initialize_run("binding-gated edit", tasks)
    first = plan_next_wave(state)
    second = plan_next_wave(state)
    if first["action"] != "blocked" or first["reason"] != second["reason"] or state["status"] != "planning":
        return [("binding_gate", "stable_block", "stable nonterminal binding block", {"first": first, "second": second, "status": state["status"]}, state)]
    target = str(PROJECT_ROOT / "bounded-output.txt")
    apply_task_bindings(state, [{"task_id": tasks[0]["task_id"], "allowed_paths": [str(PROJECT_ROOT)], "write_set": [target]}])
    wave = plan_next_wave(state)
    if wave["action"] != "dispatch_wave" or wave["assignments"][0]["task"]["write_set"] != [target]:
        return [("binding_gate", "resolved", "dispatch after host binding", wave, state)]
    return []


def test_host_issued_handles_attestations_and_residual_safety():
    failures = []
    state = initialize_run("reject forged handle", _build_lifecycle_tasks(1))
    assignment = plan_next_wave(state)["assignments"][0]
    forged = _worker_result(assignment["task"], "result-forged-handle", worker_handle="worker_" + ("f" * 32))
    forged["worker_handle_ack"] = {"source": "host", "worker_handle": "worker_" + ("f" * 32)}
    forged_ingestion = ingest_results(state, [forged])
    if "result_unknown_fields" not in forged_ingestion["validation"][0]["errors"]:
        failures.append(("host_handle", "forged", "result_unknown_fields", forged_ingestion, state))

    model_state = initialize_run("reject worker model assertion", _build_lifecycle_tasks(1))
    model_task = plan_next_wave(model_state)["assignments"][0]["task"]
    worker_model = _worker_result(model_task, "result-worker-model")
    worker_model["resolved_model_ack"] = {"source": "worker"}
    model_ingestion = ingest_results(model_state, [worker_model])
    if "result_unknown_fields" not in model_ingestion["validation"][0]["errors"]:
        failures.append(("model_attestation", "source", "result_unknown_fields", model_ingestion, model_state))

    residual_state = initialize_run("sanitize residual task", _build_lifecycle_tasks(1))
    residual_task = plan_next_wave(residual_state)["assignments"][0]["task"]
    sensitive = _worker_result(
        residual_task,
        "result-sensitive-residual",
        status="partial",
        remaining_work=[{"scope": "Continue with DATABASE_PASSWORD=notarealpassword123"}],
    )
    residual_ingestion = ingest_results(residual_state, [sensitive])
    residual_errors = residual_ingestion["validation"][0]["errors"]
    if "residual_scope_sensitive" not in residual_errors or len(residual_state["tasks"]) != 1:
        failures.append(("residual_safety", "secret", "rejected without child task", residual_errors, residual_state))
    return failures


def test_duplicate_unknown_results_do_not_consume_budget():
    state = initialize_run(
        "identity filtering before accounting",
        _build_lifecycle_tasks(2),
        {"max_wave_size": 2, "max_parent_result_tokens_per_wave": 1000},
    )
    task = plan_next_wave(state)["assignments"][0]["task"]
    unknown = _worker_result(task, "result-unknown")
    unknown["task_id"] = "task_" + ("0" * 32)
    unknown["summary"] = "x" * 8000
    before_usage = dict(state["usage"])
    unknown_ingestion = ingest_results(state, [unknown])
    if state["usage"] != before_usage or state["current_wave_parent_result_tokens"] != 0:
        return [("identity_filter", "unknown", "no budget mutation", {"before": before_usage, "after": state["usage"]}, unknown_ingestion)]
    valid = _worker_result(task, "result-valid-after-unknown")
    ingest_results(state, [valid])
    usage_after_valid = dict(state["usage"])
    duplicate = ingest_results(state, [valid])
    if state["usage"] != usage_after_valid or duplicate["validation"][0]["errors"] != ["duplicate_result_id"]:
        return [("identity_filter", "duplicate", "no budget mutation", {"before": usage_after_valid, "after": state["usage"]}, duplicate)]
    return []


def test_state_validation_locking_and_cas():
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.json"
        state = initialize_run("state integrity", _build_lifecycle_tasks(1))
        save_run_state(path, state)
        left = load_run_state(path)
        right = load_run_state(path)
        save_run_state(path, left)
        try:
            save_run_state(path, right)
        except ValueError as exc:
            if str(exc) != "run_state_revision_conflict":
                failures.append(("state_cas", "error", "run_state_revision_conflict", str(exc), right))
        else:
            failures.append(("state_cas", "save", "conflict rejected", "accepted", right))
        lock = path.with_name(path.name + ".lock")
        lock_fd = os.open(lock, os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            try:
                save_run_state(path, left)
            except ValueError as exc:
                if str(exc) != "run_state_locked":
                    failures.append(("state_lock", "error", "run_state_locked", str(exc), left))
            else:
                failures.append(("state_lock", "save", "locked", "accepted", left))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        lock.write_text("stale pid metadata\n", encoding="utf-8")
        lock.chmod(0o600)
        try:
            save_run_state(path, left)
        except ValueError as exc:
            failures.append(("state_lock", "stale_unlocked", "reclaimed through flock", str(exc), left))

    tampered = initialize_run("objective integrity", _build_lifecycle_tasks(1))
    tampered["objective"] = "changed without hash"
    try:
        validate_run_state(tampered)
    except ValueError as exc:
        if str(exc) != "run_objective_hash_mismatch":
            failures.append(("state_validation", "objective", "run_objective_hash_mismatch", str(exc), tampered))
    else:
        failures.append(("state_validation", "objective", "rejected", "accepted", tampered))

    duplicate = initialize_run("duplicate task ids", _build_lifecycle_tasks(2))
    duplicate["tasks"][1]["task_id"] = duplicate["tasks"][0]["task_id"]
    try:
        validate_run_state(duplicate)
    except ValueError:
        pass
    else:
        failures.append(("state_validation", "duplicate_task", "rejected", "accepted", duplicate))

    direct = build_task_units("main gated action", "small-edit", "main", "main-session", "main-session", "low", ["request_item_1"], allowed_paths=[str(PROJECT_ROOT)])
    blocked = initialize_run("blocked nondelegable", direct)
    blocked["tasks"][0]["status"] = "blocked"
    if plan_next_wave(blocked)["action"] != "blocked":
        failures.append(("blocked_completion", "plan", "blocked", blocked["status"], blocked))
    try:
        mark_complete(blocked)
    except ValueError:
        pass
    else:
        failures.append(("blocked_completion", "mark_complete", "rejected", "accepted", blocked))
    return failures


def test_registry_reasoning_and_local_attestation_routing():
    failures = []
    codex_env = os.environ.copy()
    codex_env.update({"AGENT_FANOUT_PROJECT_ROOT": str(PROJECT_ROOT), "AGENT_FANOUT_CODEX_SUBAGENTS": "1"})
    cases = [
        ("AgentFanout Codex\nClassify four files using Luna with light reasoning; I accept orchestration overhead", "current-fast-mini", "low"),
        ("AgentFanout Codex\nDebug a failing test using Terra with medium reasoning; I accept orchestration overhead", "current-balanced", "medium"),
        ("AgentFanout Codex\nPerform a security review using SOL with ultra reasoning; I accept orchestration overhead", "current-frontier", "ultra"),
    ]
    for prompt, selector, effort in cases:
        payload = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), prompt], text=True, env=codex_env))
        actual = (payload["route"]["executor"]["model_selector"], payload["route"]["executor"]["reasoning_effort"])
        if actual != (selector, effort):
            failures.append(("reasoning_route", prompt.splitlines()[1], (selector, effort), actual, payload))

    local_env = _attested_local_env(["bulk-read"], ["low"])
    local = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), "AgentFanout Local: review three independent modules"], text=True, env=local_env))
    if local["selected_provider"] != "Local" or local["route"]["executor"]["model_selector"] != "local-worker":
        failures.append(("cheapest_capable", "local", "Local/local-worker", local["route"]["executor"], local))
    ranking = ranked_provider_options("bulk-read", "low")
    if not ranking or ranking[0]["provider"] != "Local":
        failures.append(("registry_ranking", "first", "Local", ranking, None))

    mismatch_env = _attested_local_env(["bulk-read"], ["low"])
    mismatch = json.loads(mismatch_env["AGENT_FANOUT_LOCAL_ATTESTATION_JSON"])
    mismatch["exact_model_id"] = "different-model"
    mismatch_env["AGENT_FANOUT_LOCAL_ATTESTATION_JSON"] = json.dumps(mismatch)
    blocked = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), "AgentFanout Local\nreview three independent modules"], text=True, env=mismatch_env))
    expected = "provider_unavailable:local:capability_exact_model_mismatch"
    if blocked["route"]["worth_delegating"] or expected not in blocked["warnings"]:
        failures.append(("local_attestation", "model mismatch", expected, blocked["warnings"], blocked))
    return failures


def _synthetic_local_bridge_env(environment):
    """Give a routing-test child a synthetic issuer, never the host bridge."""
    env = dict(environment)
    if os.environ.get("AGENT_FANOUT_RUN_INSTALLED_VLLM_INTEGRATION") == "1":
        return env
    directory = tempfile.mkdtemp(prefix="agentfanout-synthetic-bridge-")
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    stub = Path(directory) / "bun"
    stub.write_text("#!" + sys.executable + "\n" + r'''import hashlib, json, os, re, sys
if len(sys.argv) != 2 or sys.argv[1] != os.environ["AF_TEST_BRIDGE_ISSUER_PATH"]:
    raise SystemExit("synthetic bridge invocation invalid")
raw = sys.stdin.read(256001)
if len(raw) > 256000:
    raise SystemExit("synthetic bridge input too large")
value = json.loads(raw)
if value.get("schema") != "AgentFanoutLocalBridgeLaunchInputV1":
    raise SystemExit("synthetic bridge input schema invalid")
request, observation = value["request"], value["observation"]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value["correlation_id"]):
    raise SystemExit("synthetic bridge correlation invalid")
with open(os.environ["AF_TEST_BRIDGE_MARKER"], "a", encoding="utf-8") as marker:
    marker.write("called\n")
if (request["capability"] != {} or request["model_alias"] != observation["model_alias"]
    or request["endpoint"] != observation["endpoint"]
    or request["envelope"]["request_id"] != request["request_id"]
    or not set(request["envelope"]["capability_profiles"]).issubset(observation["profiles"])
        or "correlation_id" in json.dumps(request)):
    raise SystemExit("synthetic bridge request invalid")
digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
request["capability"] = {"validated": True, "source": "synthetic_test_fixture",
                     "launch_contract_sha256": digest}
print(json.dumps({"schema": "AgentFanoutLocalBridgeLaunchV1", "request": request,
"usage_correlation": {"request_id": "wrong-fixture-request" if os.getenv("AF_TEST_BRIDGE_FAULT") == "request_id" else request["request_id"],
    "correlation_id": value["correlation_id"], "launch_contract_sha256": "0" * 64 if os.getenv("AF_TEST_BRIDGE_FAULT") == "digest" else digest}}))
''', encoding="utf-8")
    compile(stub.read_text(), str(stub), "exec")
    stub.chmod(0o700)
    env["PATH"] = directory + os.pathsep + env.get("PATH", os.defpath)
    issuer = Path(directory) / "synthetic-issuer-fixture.mjs"
    issuer.write_text("// Synthetic offline adapter fixture; no provider implementation.\n")
    home = Path(directory) / "empty-home"
    home.mkdir()
    env.update({"HOME": str(home), "AGENT_FANOUT_LOCAL_BRIDGE_ISSUER": str(issuer),
                "AF_TEST_BRIDGE_ISSUER_PATH": str(issuer),
                "AF_TEST_BRIDGE_MARKER": str(Path(directory) / "invocations.txt")})
    for name in ("AGENT_FANOUT_LOCAL_BRIDGE_MODULE", "PAI_DIR", "PAI_HOME", "AF_TEST_BRIDGE_FAULT"):
        env.pop(name, None)
    return env


def test_local_bridge_contract_end_to_end():
    """Test public routing wiring with a synthetic issuer, not live admission."""
    return _check_local_bridge_contract(_attested_local_env(["bulk-read"], ["low"]))


def _check_local_bridge_contract(env):
    payload = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Local\nreview three independent modules"],
        text=True,
        env=env,
    ))
    request = payload["route"]["executor"]["local_agent_request"]
    correlation = payload["route"]["executor"]["local_host_usage_correlation"]
    failures = []
    if not request or request.get("capability", {}).get("validated") is not True:
        failures.append(("local_bridge", "route_request", "bridge-issued validated request", request, payload))
        return failures
    if not correlation or correlation.get("request_id") != request.get("request_id") or correlation.get("launch_contract_sha256") != request["capability"].get("launch_contract_sha256"):
        failures.append(("local_bridge", "host_correlation", "host-only bound correlation", correlation, payload))
    if "correlation_id" in json.dumps(request):
        failures.append(("local_bridge", "worker_request", "no correlation_id", request, payload))

    # Installed integration is separate and never receives synthetic evidence.
    if os.environ.get("AGENT_FANOUT_RUN_INSTALLED_VLLM_INTEGRATION") == "1":
        agent = Path.home() / ".pai" / "skills" / "VLLMWorker" / "Tools" / "VLLMAgent.ts"
        if not agent.is_file():
            return failures + [("local_bridge", "vllm_agent", "installed VLLMAgent", str(agent), None)]
        accepted = subprocess.run(
            ["bun", str(agent), "--request-json", "-", "--json", "--timeout", "1000", "--max-tokens", "256"],
            input=json.dumps(request), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )
        try:
            accepted_result = json.loads(accepted.stdout)
        except json.JSONDecodeError:
            failures.append(("local_bridge", "vllm_agent_output", "JSON result", accepted.stdout, accepted.stderr))
        else:
            if accepted_result.get("capability_validation", {}).get("validated") is not True:
                failures.append(("local_bridge", "vllm_attestation", "validated", accepted_result, None))

    marker = Path(env["AF_TEST_BRIDGE_MARKER"]) if "AF_TEST_BRIDGE_MARKER" in env else None
    if marker and not marker.is_file():
        failures.append(("synthetic_bridge", "invocation", "fixture called", "not called", None))
        return failures
    calls_before_invalid = marker.read_bytes() if marker else None
    missing_env = dict(env)
    missing_env.pop("AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON", None)
    missing = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Local\nreview three independent modules"],
        text=True,
        env=missing_env,
    ))
    if missing["route"]["worth_delegating"] or "provider_unavailable:local:bridge_observation_missing" not in missing["warnings"]:
        failures.append(("local_bridge", "missing_observation", "fail closed", missing["warnings"], missing))

    if marker and marker.read_bytes() != calls_before_invalid:
        failures.append(("synthetic_bridge", "missing_observation", "no issuer launch", "issuer launched", None))
    bad_env = dict(env)
    bad_observation = json.loads(bad_env["AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON"])
    bad_observation["profiles"] = ["repo_read"]
    bad_env["AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON"] = json.dumps(bad_observation)
    invalid = json.loads(subprocess.check_output(
        [sys.executable, "-S", str(ROUTE), "AgentFanout Local\nreview three independent modules"],
        text=True,
        env=bad_env,
    ))
    if invalid["route"]["executor"]["local_agent_request"] is not None or invalid["dispatch"]["status"] != "fanout_blocked":
        failures.append(("local_bridge", "invalid_observation", "fanout_blocked", invalid["dispatch"], invalid))
    if marker:
        for fault in ("request_id", "digest"):
            rejected_env = dict(env, AF_TEST_BRIDGE_FAULT=fault)
            rejected = json.loads(subprocess.check_output(
                [sys.executable, "-S", str(ROUTE), "AgentFanout Local\nreview three independent modules"],
                text=True, env=rejected_env,
            ))
            if (rejected["route"]["executor"]["local_agent_request"] is not None
                    or "fanout_blocked:local:local_bridge_correlation_invalid" not in rejected["warnings"]):
                failures.append(("synthetic_bridge", fault, "correlation rejected", rejected["warnings"], rejected))
    return failures


def test_strict_roots_sanitizer_and_compatibility_defaults():
    failures = []
    credential = "DATABASE_PASSWORD=notarealpassword123"
    sanitized = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), "--sanitize", f"review this\n{credential}"], text=True, env=os.environ.copy()))
    if sanitized["delegation_safe"] or sanitized["safe"] or not sanitized["voice_safe"] or credential in sanitized["sanitized_prompt"]:
        failures.append(("sanitize_contract", "credential", "omitted and delegation_safe=false", sanitized, None))

    base = os.environ.copy()
    base.update({"AGENT_FANOUT_CODEX_SUBAGENTS": "1"})
    base.pop("AGENT_FANOUT_ENABLE_DISABLED_PROVIDERS", None)
    base["AGENT_FANOUT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    minimax = json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), "AgentFanout MiniMax\nreview three independent modules"], text=True, env=base))
    if minimax["route"]["worth_delegating"] or "provider_unavailable:minimax:provider_disabled" not in minimax["warnings"]:
        failures.append(("compatibility_default", "minimax", "disabled", minimax["warnings"], minimax))

    with tempfile.TemporaryDirectory() as tmp:
        unbound_env = dict(base)
        unbound_env.pop("AGENT_FANOUT_PROJECT_ROOT", None)
        unbound_env.pop("AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST", None)
        state_file = Path(tmp) / "conversation-state.json"
        unbound = json.loads(subprocess.check_output(
            [
                sys.executable, "-S", str(ROUTE), "--state-file", str(state_file),
                "AgentFanout Local\nreview README.md and SKILL.md; I accept orchestration overhead",
            ],
            text=True,
            env=unbound_env,
        ))
        if (
            unbound["dispatch"]["status"] != "binding_required"
            or unbound["dispatch"]["block_reason"] != "approved_project_root_missing"
            or unbound["next_state"]["provider_mode"] != "Auto"
            or json.loads(state_file.read_text(encoding="utf-8"))["provider_mode"] != "Auto"
        ):
            failures.append((
                "strict_root", "unbound_execution_route",
                "typed binding_required without sticky provider mode",
                {"dispatch": unbound["dispatch"], "next_state": unbound["next_state"]},
                unbound,
            ))

    def route_with_root(root, allowlist=None, sensitive_roots=None):
        env = dict(base)
        env["AGENT_FANOUT_PROJECT_ROOT"] = str(root)
        if allowlist:
            env["AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST"] = str(allowlist)
        else:
            env.pop("AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST", None)
        if sensitive_roots:
            env["AGENT_FANOUT_SENSITIVE_ROOTS"] = str(sensitive_roots)
        else:
            env.pop("AGENT_FANOUT_SENSITIVE_ROOTS", None)
        return json.loads(subprocess.check_output([sys.executable, "-S", str(ROUTE), "AgentFanout Codex\nreview three independent modules; I accept orchestration overhead"], text=True, env=env))

    broad = route_with_root("/")
    if broad["route"]["worth_delegating"] or not any("approved_project_root_too_broad" in item for item in broad["warnings"]):
        failures.append(("strict_root", "/", "blocked", broad["warnings"], broad))
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "plain-project"
        plain.mkdir()
        nonrepo = route_with_root(plain)
        if nonrepo["route"]["worth_delegating"] or not any("approved_project_root_not_repo_or_allowlisted" in item for item in nonrepo["warnings"]):
            failures.append(("strict_root", "nonrepo", "blocked", nonrepo["warnings"], nonrepo))
        allowlisted = route_with_root(plain, plain)
        if not allowlisted["route"]["worth_delegating"]:
            failures.append(("strict_root", "allowlisted", "delegated", allowlisted["warnings"], allowlisted))
        sensitive = Path(tmp) / ".pai" / "project"
        sensitive.mkdir(parents=True)
        sensitive_route = route_with_root(sensitive, sensitive, sensitive)
        if sensitive_route["route"]["worth_delegating"] or not any("approved_project_root_sensitive" in item for item in sensitive_route["warnings"]):
            failures.append(("strict_root", "sensitive", "blocked", sensitive_route["warnings"], sensitive_route))
    return failures


def test_new_public_assets_present():
    expected = [
        "agents/openai.yaml", "agents/thin-orchestrator.md", "agents/explorer.md", "agents/implementer.md",
        "agents/independent-validator.md", "agents/weak-local-worker.md", "agents/strong-synthesis-worker.md",
        "schemas/RunStateV2.schema.json", "schemas/TaskUnitV1.schema.json",
        "schemas/WorkerResultV1.schema.json", "schemas/NextWavePlanV1.schema.json",
        "schemas/ValidatorLaunchReceiptV1.schema.json",
        "providers/models.json", "docs/architecture.md", "references/ExecutionLoop.md",
        "references/CostAccounting.md",
        "schemas/LocalCapabilityAttestationV1.schema.json",
        "schemas/NativeCapabilityHandshakeV1.schema.json", "profiles/public-skill.json", "profiles/public-runtime.json",
        "agentfanout/native.py", "tests/test_skill_first_product.py",
    ]
    missing = [path for path in expected if not (ROOT / path).is_file()]
    invalid_json = []
    for path in expected:
        if path.endswith(".json") and (ROOT / path).is_file():
            try:
                json.loads((ROOT / path).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                invalid_json.append(path)
    if missing or invalid_json:
        return [("public_assets", "files", "all present and valid", {"missing": missing, "invalid_json": invalid_json}, None)]
    return []


def main():
    os.environ["AGENT_FANOUT_PROJECT_ROOT"] = str(PROJECT_ROOT)
    os.environ["AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST"] = str(PROJECT_ROOT)
    os.environ["AGENT_FANOUT_ENABLE_DISABLED_PROVIDERS"] = "1"
    failures = (
        test_fixtures()
        + test_malformed_state()
        + test_sanitizer()
        + test_secret_output_redaction()
        + test_state_file()
        + test_compact_route_output_preserves_continuation_without_full_packets()
        + test_readonly_infra_diagnostics_allowlist()
        + test_capability_profiles()
        + test_stdin_prompt_route()
        + test_colon_command_preserves_task()
        + test_readonly_architecture_filename_is_not_design_work()
        + test_codex_model_and_reasoning_effort_are_orthogonal()
        + test_named_files_bind_across_read_only_technical_classes()
        + test_retired_kernel_absent()
        + test_token_usage_is_not_a_secret()
        + test_external_envelope_is_sanitized_and_bounded()
        + test_local_stale_capability_blocks()
        + test_auto_falls_through_unavailable_tool_provider()
        + test_model_alias_and_risk_routing()
        + test_astra_cost_gate_and_single_worker_cap()
        + test_semantic_validator_cost_policy()
        + test_bounded_input_size_changes_cost_decision_math()
        + test_grok_registry_and_retired_claude()
        + test_july8_parent_takeover_two_wave_regression()
        + test_completion_wins_at_exact_budget_limit()
        + test_wave_progress_persists_across_result_batches()
        + test_parent_self_execute_signal_requires_resolved_non_host_run()
        + test_host_private_launch_records_fail_closed()
        + test_parent_result_token_omission_cannot_bypass_cap()
        + test_delegated_root_and_readonly_shell_boundaries()
        + test_run_cost_and_host_measured_result_limits()
        + test_bindings_are_stable_and_required_before_dispatch()
        + test_host_issued_handles_attestations_and_residual_safety()
        + test_duplicate_unknown_results_do_not_consume_budget()
        + test_state_validation_locking_and_cas()
        + test_registry_reasoning_and_local_attestation_routing()
        + test_local_bridge_contract_end_to_end()
        + test_strict_roots_sanitizer_and_compatibility_defaults()
        + test_new_public_assets_present()
    )
    if failures:
        print(json.dumps(failures, indent=2))
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main()
