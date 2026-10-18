#!/usr/bin/env python3
"""Run a persisted AgentFanout RunStateV2 through the host-owned runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.host_runtime import HostLedger
from agentfanout.harness import HarnessContractError, manifest_digest, validate_manifest
from agentfanout.lifecycle import load_run_state
from agentfanout.project_binding import (
    ProjectBindingError, default_private_state_directory, path_within,
    validate_project_binding,
)
from agentfanout.provider_adapters import (
    ProviderAdapterError, _secure_grok_home, discover_codex_models,
    discover_grok_models, executable_identity, grok_auth_file_identity, local_helper_binary,
    grok_http_helper_binary,
    local_host_evidence_from_environment, resolve_codex_model,
    verify_executable_identity,
)
from agentfanout.runner import (
    CodexValidatorAdapter, CodexWorkerAdapter, GrokValidatorAdapter,
    GrokWorkerAdapter, HostedFanoutRunner, LocalValidatorAdapter,
    LocalWorkerAdapter,
)


SENSITIVE_OUTPUT = re.compile(
    r"(?i)((?:proxy[-_]?authorization|authorization|cookie|set[-_]?cookie|x[-_]?api[-_]?key)\s*[=:]|"
    r"bearer\s+[a-z0-9._~+/=-]{12,}|"
    r"(?:api[_-]?key|secret|password|credential|private[_-]?key|token|access[_-]?token|"
    r"refresh[_-]?token|auth[_-]?token|service[_-]?account)\s*[=:]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[a-z0-9_-]{12,}|\bghp_[a-z0-9_]{12,}|"
    r"\bgithub_pat_[a-z0-9_]{12,}|\bxox[baprs]-[a-z0-9-]{12,}|\bxai-[a-z0-9_-]{12,}|"
    r"\bglpat-[a-z0-9_-]{12,}|\bnpm_[a-z0-9_-]{12,}|\bAKIA[0-9A-Z]{16}|"
    r"\bAIza[0-9A-Za-z_-]{20,}|\beyJ[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}|"
    r"\b[a-z][a-z0-9+.-]*://[^/:@\s]+:[^@\s/]+@[^\s]+)"
)
SENSITIVE_KEY = re.compile(
    r"(?i)^(?:proxy[-_]?authorization|authorization|cookie|set[-_]?cookie|x[-_]?api[-_]?key|"
    r"api[-_]?key|secret|password|credential|private[-_]?key|token|access[-_]?token|"
    r"refresh[-_]?token|auth[-_]?token|session|sessionid|sid|session[-_]?(?:id|key|token)|"
    r"csrf|xsrf|(?:csrf|xsrf)[-_]?(?:token|key)|service[-_]?account|auth|"
    r"database[-_]?(?:url|uri|dsn)|db[-_]?(?:url|uri|dsn)|connection[-_]?(?:string|url|uri)|dsn|"
    r"(?:.*[-_])?(?:secret|password|credential|token|access[-_]?key|private[-_]?key)|"
    r"secret[-_].*)$"
)
SAFE_EVIDENCE_KEYS = frozenset({
    "claim", "claim_id", "confidence", "evidence", "excerpt", "fact", "facts",
    "file", "finding", "line", "line_end", "line_start", "lines", "path",
    "reason", "ref", "reference", "section", "severity", "source", "status",
    "summary", "support", "test", "uncertainty", "value",
})


class _RunnerStartupSignal(Exception):
    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _compact_text(value, limit=300):
    if not isinstance(value, str):
        return None
    if SENSITIVE_OUTPUT.search(value):
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
    compacted = " ".join(value.split())
    return compacted if len(compacted) <= limit else compacted[:limit - 1] + "…"


def _task_counts(state):
    counts = {}
    for task in state.get("tasks", []) if isinstance(state, dict) else []:
        status = str(task.get("status", "unknown")) if isinstance(task, dict) else "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _compact_evidence(value, depth=0):
    """Preserve bounded evidence semantics without exposing credential material."""

    if depth > 3:
        return {"value_sha256": hashlib.sha256(repr(value).encode()).hexdigest()}
    if isinstance(value, str):
        return _compact_text(value, 300)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_compact_evidence(item, depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        output = {}
        redacted_fields = []
        for key, item in list(value.items())[:12]:
            safe_key = _compact_text(str(key), 80)
            if (
                safe_key is None
                or safe_key.lower() not in SAFE_EVIDENCE_KEYS
                or SENSITIVE_KEY.fullmatch(safe_key)
                or SENSITIVE_OUTPUT.search(safe_key + "=")
            ):
                redacted_fields.append(hashlib.sha256(
                    json.dumps({str(key): item}, sort_keys=True, default=str).encode()
                ).hexdigest())
                continue
            output[safe_key] = _compact_evidence(item, depth + 1)
        if redacted_fields:
            output["redacted_field_sha256"] = redacted_fields
        return output
    return {"value_sha256": hashlib.sha256(repr(value).encode()).hexdigest()}


def compact_outcome(outcome, state_file, ledger_file):
    """Bounded parent-facing runner handoff; never return the full RunState."""

    state = outcome.get("state") if isinstance(outcome, dict) and isinstance(outcome.get("state"), dict) else {}
    run_id = outcome.get("run_id") if isinstance(outcome, dict) else None
    accepted, validators, launches, validator_launches, usage, validator_usage, terminal_events = [], [], [], [], [], [], []
    ledger_error = None
    if isinstance(run_id, str):
        ledger = HostLedger(ledger_file)
        try:
            accepted = ledger.events(run_id, "accepted_result_packet")
            validators = ledger.events(run_id, "validator_result")
            launches = ledger.events(run_id, "launch_receipt")
            validator_launches = ledger.events(run_id, "validator_launch_receipt")
            usage = ledger.events(run_id, "usage_basis")
            validator_usage = ledger.events(run_id, "validator_usage_basis")
            terminal_events = ledger.events(run_id, "terminal_authorized")
        except Exception as exc:  # Preserve bounded output even if private evidence is unavailable.
            ledger_error = type(exc).__name__
        finally:
            ledger.close()
    result_summaries = []
    for event in accepted[:32]:
        payload = event.get("payload", {}) if isinstance(event, dict) else {}
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        if not isinstance(result, dict):
            continue
        result_summaries.append({
            "task_id": event.get("task_id"),
            "result_id": result.get("result_id"),
            "status": result.get("status"),
            "summary": _compact_text(result.get("summary")),
            "evidence": [_compact_evidence(item) for item in result.get("evidence", [])[:3]],
            "evidence_count": len(result.get("evidence", [])) if isinstance(result.get("evidence"), list) else 0,
            "changed_file_count": len(result.get("changed_files", [])) if isinstance(result.get("changed_files"), list) else 0,
            "remaining_work_count": len(result.get("remaining_work", [])) if isinstance(result.get("remaining_work"), list) else 0,
        })
    worker_provenance = [
        {
            "task_id": event.get("task_id"), "provider": event["payload"].get("provider"),
            "model": event["payload"].get("resolved_model"), "reasoning_effort": event["payload"].get("reasoning_effort"),
        }
        for event in launches[:32] if isinstance(event.get("payload"), dict)
    ]
    validator_provenance = [
        {
            "task_id": event.get("task_id"), "provider": event["payload"].get("validator_provider"),
            "model": event["payload"].get("validator_resolved_model"), "reasoning_effort": event["payload"].get("validator_reasoning_effort"),
        }
        for event in validator_launches[:32] if isinstance(event.get("payload"), dict)
    ]
    validator_counts = {}
    for event in validators:
        verdict = event.get("payload", {}).get("verdict", "unknown") if isinstance(event, dict) else "unknown"
        validator_counts[verdict] = validator_counts.get(verdict, 0) + 1
    return {
        "schema": "AgentFanoutCompactRunnerV1",
        "status": outcome.get("status"),
        "run_id": run_id,
        "terminal_authorized": bool(terminal_events),
        "block_reason": outcome.get("reason") or state.get("block_reason"),
        "state_file": str(state_file),
        "ledger_file": str(ledger_file),
        "lifecycle": {"status": state.get("status"), "wave_index": state.get("wave_index"), "task_counts": _task_counts(state)},
        "accepted_results": result_summaries,
        "validator_counts": validator_counts,
        "provenance": {
            "worker_launches": worker_provenance,
            "validator_launches": validator_provenance,
            "worker_usage_records": len(usage),
            "validator_usage_records": len(validator_usage),
            "run_usage": state.get("usage"),
        },
        "ledger_read_error": ledger_error,
        "full_state_omitted": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, type=Path)
    default_ledger = (Path(os.environ["AGENT_FANOUT_HOST_LEDGER"]).expanduser()
                      if os.environ.get("AGENT_FANOUT_HOST_LEDGER") else None)
    parser.add_argument("--ledger", type=Path, default=default_ledger)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--provider", choices=["Codex", "Grok", "GrokCLI", "Local"], default="Codex")
    parser.add_argument("--execution-profile", choices=["legacy", "coordinator_local", "mixed_provider_runs"], default="legacy")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--grok-binary", default="grok")
    parser.add_argument("--grok-home", type=Path)
    parser.add_argument("--expected-grok-auth-identity-json", help="Guard-frozen dedicated Grok auth-file identity")
    parser.add_argument("--expected-grok-helper-identity-json", help="Guard-frozen Grok HTTPS helper identity")
    parser.add_argument("--resolved-model", help="Exact model frozen by the atomic preflight")
    parser.add_argument("--expected-executable-identity-json", help="Host-frozen executable identity from atomic preflight")
    parser.add_argument(
        "--expected-host-manifest-sha256", required=True,
        help="Session-guard digest of the exact host manifest authorized for this run",
    )
    parser.add_argument("--host-manifest", type=Path, default=ROOT / "integrations" / "codex" / "standalone-runner-manifest.json")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-cycles", type=int, default=100)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--heartbeat-seconds", type=int, default=30)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--compact", action="store_true", help="Emit bounded parent-facing results and provenance; omit full RunState")
    args = parser.parse_args(argv)
    if args.execution_profile != "legacy":
        from agentfanout.upgrade_profiles import capability_status
        lane = "coordinator_local" if args.execution_profile == "coordinator_local" else "mixed"
        print(json.dumps({"schema": "HostedFanoutOutcomeV1", "status": "blocked", "profile": args.execution_profile,
                          "reason": capability_status()["lanes"][lane]["reason"], "fallback_allowed": False}))
        return 4
    if args.provider == "GrokCLI":
        print(json.dumps({"schema": "HostedFanoutOutcomeV1", "status": "blocked",
                          "provider": "GrokCLI", "reason": "grok_cli_installed_boundary_unproved",
                          "fallback_allowed": False}))
        return 4
    if args.ledger is None:
        args.ledger = default_private_state_directory() / "host.sqlite3"

    received_signals = []
    runner_ref = []
    prior_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def request_shutdown(signum, _frame):
        received_signals.append(signum)
        if runner_ref:
            runner_ref[0].request_shutdown(
                f"runner_shutdown_requested:{signal.Signals(signum).name}"
            )
            return
        # Raising here unwinds provider-discovery TemporaryDirectory scopes,
        # including their least-privilege auth copy, before process exit.
        raise _RunnerStartupSignal(signum)

    def restore_signal_handlers():
        for signum, prior in prior_handlers.items():
            signal.signal(signum, prior)

    for signum in prior_handlers:
        signal.signal(signum, request_shutdown)

    try:
        persisted = load_run_state(args.state_file)
        project_root = validate_project_binding(persisted.get("project_binding"), expected_root=args.project_root)
        state_path = args.state_file.expanduser().resolve(strict=True)
        ledger_path = args.ledger.expanduser().resolve(strict=False)
        if any(path == project_root or path_within(path, project_root) for path in (state_path, ledger_path)):
            raise ProjectBindingError("state_artifact_inside_project_denied")
        manifest = json.loads(args.host_manifest.read_text(encoding="utf-8"))
        validate_manifest(manifest, require_level="runner_enforced")
        if (
            not re.fullmatch(r"[a-f0-9]{64}", args.expected_host_manifest_sha256)
            or manifest_digest(manifest) != args.expected_host_manifest_sha256
        ):
            raise HarnessContractError("host_manifest_binding_mismatch")
        provider_binary = (
            args.codex_binary if args.provider == "Codex"
            else args.grok_binary if args.provider == "Grok"
            else local_helper_binary()
        )
        grok_home = None
        expected_grok_auth_identity = None
        expected_grok_helper_identity = None
        if args.provider == "Grok":
            if args.grok_home is None:
                raise ProviderAdapterError("grok_isolated_home_required")
            grok_home = _secure_grok_home(args.grok_home)
            if not args.expected_grok_auth_identity_json:
                raise ProviderAdapterError("grok_expected_auth_file_identity_required")
            expected_grok_auth_identity = json.loads(args.expected_grok_auth_identity_json)
            if not isinstance(expected_grok_auth_identity, dict):
                raise ProviderAdapterError("grok_expected_auth_file_identity_invalid")
            if grok_auth_file_identity(grok_home) != expected_grok_auth_identity:
                raise ProviderAdapterError("grok_auth_file_identity_changed")
            if not args.expected_grok_helper_identity_json:
                raise ProviderAdapterError("grok_http_helper_identity_required")
            expected_grok_helper_identity = json.loads(args.expected_grok_helper_identity_json)
            if not isinstance(expected_grok_helper_identity, dict):
                raise ProviderAdapterError("grok_http_helper_identity_invalid")
            verify_executable_identity(
                grok_http_helper_binary(), expected_grok_helper_identity,
            )
        if args.expected_executable_identity_json:
            expected_executable_identity = json.loads(args.expected_executable_identity_json)
            verify_executable_identity(provider_binary, expected_executable_identity)
        else:
            expected_executable_identity = executable_identity(provider_binary)
        persisted_providers = {
            task.get("provider_model", {}).get("provider") for task in persisted["tasks"]
        }
        if persisted_providers != {args.provider}:
            raise ProviderAdapterError("persisted_provider_binding_mismatch")
        selectors = {
            task["provider_model"]["selector"] for task in persisted["tasks"]
            if task.get("provider_model", {}).get("provider") == args.provider
        }
        if not selectors:
            raise ProviderAdapterError(f"{args.provider.lower()}_task_selector_required")
        if args.resolved_model:
            if len(selectors) != 1:
                raise ProviderAdapterError("single_frozen_model_selector_required")
            frozen_models = {next(iter(selectors)): args.resolved_model}
        elif args.provider in {"Grok", "Local"}:
            raise ProviderAdapterError(f"{args.provider.lower()}_resolved_model_required")
        else:
            discovered = discover_codex_models(
                provider_binary,
                expected_executable_identity=expected_executable_identity,
            )
            frozen_models = {selector: resolve_codex_model(selector, discovered) for selector in selectors}
        if args.provider == "Grok":
            discovered = discover_grok_models(
                provider_binary,
                expected_executable_identity=expected_executable_identity,
                grok_home=grok_home,
                expected_grok_auth_file_identity=expected_grok_auth_identity,
                expected_grok_helper_identity=expected_grok_helper_identity,
            )
            listed_models = {str(item.get("id")) for item in discovered}
            if any(exact_model not in listed_models for exact_model in frozen_models.values()):
                raise ProviderAdapterError("grok_frozen_model_not_listed")
        if args.provider == "Local":
            for task in persisted["tasks"]:
                evidence = local_host_evidence_from_environment(
                    selector=task["provider_model"]["selector"],
                    task_class=task["task_class"], reasoning_effort=task["reasoning_effort"],
                )
                if evidence["exact_model_id"] != args.resolved_model:
                    raise ProviderAdapterError("local_frozen_model_binding_changed")
        verify_executable_identity(provider_binary, expected_executable_identity)
    except _RunnerStartupSignal as exc:
        restore_signal_handlers()
        print(json.dumps({
            "schema": "AgentFanoutCompactRunnerV1", "status": "fanout_blocked",
            "block_reason": (
                "runner_startup_shutdown_requested:"
                + signal.Signals(exc.signum).name
            ),
            "terminal_authorized": False,
            "shutdown_signal": signal.Signals(exc.signum).name,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
        }, sort_keys=True))
        return 2
    except (OSError, ValueError, json.JSONDecodeError, ProjectBindingError, HarnessContractError, ProviderAdapterError) as exc:
        restore_signal_handlers()
        print(json.dumps({
            "schema": "AgentFanoutCompactRunnerV1", "status": "fanout_blocked",
            "block_reason": f"project_binding_invalid:{exc}", "terminal_authorized": False,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
        }, sort_keys=True))
        return 3

    if args.provider == "Grok":
        worker = GrokWorkerAdapter(
            args.project_root, provider_binary, frozen_models,
            expected_executable_identity, grok_home, expected_grok_auth_identity,
            expected_grok_helper_identity,
        )
        validator = GrokValidatorAdapter(
            args.project_root, provider_binary, frozen_models,
            expected_executable_identity, grok_home, expected_grok_auth_identity,
            expected_grok_helper_identity,
        )
    elif args.provider == "Local":
        worker = LocalWorkerAdapter(
            args.project_root, provider_binary, expected_executable_identity,
        )
        validator = LocalValidatorAdapter(
            args.project_root, provider_binary, expected_executable_identity,
        )
    else:
        worker = CodexWorkerAdapter(
            args.project_root, provider_binary, frozen_models, expected_executable_identity,
        )
        validator = CodexValidatorAdapter(
            args.project_root, provider_binary, frozen_models, expected_executable_identity,
        )
    runner = HostedFanoutRunner(
        args.state_file, args.ledger, {args.provider: worker}, validator,
        max_workers=args.max_workers, lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        host_manifest=manifest, require_adapter_identity=True,
    )
    runner_ref.append(runner)
    try:
        outcome = runner.run(args.max_cycles)
    finally:
        restore_signal_handlers()
    output = compact_outcome(outcome, args.state_file, args.ledger) if args.compact else outcome
    if isinstance(output, dict):
        output["project_binding"] = persisted.get("project_binding")
        output["enforcement"] = {
            "level": "runner_enforced", "native_worker_tool_control": "absent",
            "native_spawn_policy": "host_adapter_only", "anti_bypass_certified": False,
            "standalone_scope_enforced": True,
            "enforcement_scope": "standalone_hosted_runner_process",
            "host_manifest_sha256": manifest_digest(manifest),
            "fallback_allowed": False,
        }
        output.setdefault("provenance", {})["provider_executable"] = {
            "provider": args.provider,
            "sha256": expected_executable_identity["executable_sha256"],
            "bytes": expected_executable_identity["executable_bytes"],
            "device": expected_executable_identity["executable_device"],
            "inode": expected_executable_identity["executable_inode"],
            "role": "model_discovery_only" if args.provider == "Grok" else "inference_launcher",
            "assurance": (
                "host_observed_discovery_binding_inference_uses_pinned_https_helper"
                if args.provider == "Grok"
                else "host_observed_release_run_binding_not_vendor_attestation"
            ),
        }
        if received_signals:
            output["shutdown_signal"] = signal.Signals(received_signals[-1]).name
    print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True, default=str))
    return 0 if outcome["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
