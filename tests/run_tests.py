#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "scripts" / "route.py"
FIXTURES = Path(__file__).with_name("fixtures.json")


def get_path(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def run_route(case):
    cmd = [sys.executable, "-S", str(ROUTE), case["prompt"]]
    env = os.environ.copy()
    env.setdefault("AGENT_FANOUT_MINIMAX_AVAILABLE", "1")
    env.setdefault("AGENT_FANOUT_MINIMAX_TOOL_AGENT", "1")
    env.setdefault("AGENT_FANOUT_CODEX_SUBAGENTS", "1")
    for name in case.get("unset_env", []):
        env.pop(name, None)
    for name, value in case.get("env", {}).items():
        env[name] = str(value)
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
            got = get_path(actual, key)
            if got != expected:
                failures.append((case["name"], key, expected, got, actual))
        for key, expected in case.get("expect_contains", {}).items():
            got = get_path(actual, key)
            if expected not in got:
                failures.append((case["name"], key, f"contains {expected}", got, actual))
        for key, expected_values in case.get("expect_contains_all", {}).items():
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
    legacy = "normal line\ncurl http://localhost:8888/notify voice_id=x\nplease speak via webhook callback\nkeep this"
    out = subprocess.check_output([sys.executable, "-S", str(ROUTE), "--sanitize", legacy], text=True, env=os.environ.copy())
    result = json.loads(out)
    forbidden = ["localhost:8888", "/notify", "voice_id", "speak", "curl", "webhook", "callback", "notification", "spoken-summary", "readback"]
    leaked = [tok for tok in forbidden if tok.lower() in result["sanitized_prompt"].lower()]
    if result["removed_lines"] != 2 or not result["safe"] or leaked:
        return [("sanitizer", "safe", "no forbidden leakage", {"result": result, "leaked": leaked})]
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


def test_stdin_prompt_route():
    prompt = "AgentFanout Codex\nClassify these independent notes into categories with Codex subagents."
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


def test_retired_kernel_absent():
    kernel = ROOT / "references" / "Kernel.md"
    if kernel.exists():
        return [("retired_kernel", "absent", "Kernel.md removed", str(kernel), None)]
    return []


def main():
    failures = test_fixtures() + test_malformed_state() + test_sanitizer() + test_secret_output_redaction() + test_stdin_prompt_route() + test_retired_kernel_absent()
    if failures:
        print(json.dumps(failures, indent=2))
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main()
