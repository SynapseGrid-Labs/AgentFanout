#!/usr/bin/env python3
"""Portable AgentFanout doctor, atomic run, and guarded recovery entrypoint."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.harness import (  # noqa: E402
    HarnessContractError, create_session_guard, manifest_digest,
    validate_manifest, validate_session_guard,
)
from agentfanout.host_runtime import HostLedger  # noqa: E402
from agentfanout.lifecycle import initialize_run, load_run_state, save_run_state  # noqa: E402
from agentfanout.project_binding import (  # noqa: E402
    ProjectBindingError, default_private_state_directory, path_within,
    resolve_project_binding, secure_private_directory, secure_private_file,
    validate_project_binding,
)
from agentfanout.provider_adapters import (  # noqa: E402
    ProviderAdapterError, _secure_grok_home, codex_no_tool_runtime_attestation,
    discover_codex_models,
    discover_grok_models, executable_identity, grok_auth_file_identity,
    grok_direct_transport_preflight, grok_http_helper_identity, local_helper_binary,
    grok_reasoning_effort_mode, grok_supported_reasoning_efforts,
    resolve_codex_model, resolve_grok_model, terminate_process_group,
    validate_local_host_evidence, verify_executable_identity,
)
from agentfanout.local_endpoint_worker import normalize_local_endpoint  # noqa: E402
from agentfanout.provider_registry import provider_config  # noqa: E402
from agentfanout.build_identity import build_report  # noqa: E402
from agentfanout.upgrade_profiles import capability_status  # noqa: E402


ROUTE = ROOT / "scripts" / "route.py"
RUNNER = ROOT / "scripts" / "run-hosted-fanout.py"
RUNNER_MANIFEST = ROOT / "integrations" / "codex" / "standalone-runner-manifest.json"
MAX_CHILD_OUTPUT = 4_000_000
MAX_GUARD_BYTES = 256_000


class _ForwardedProcessSignal(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _state_home() -> Path:
    return default_private_state_directory()


def _secure_directory(path: Path) -> Path:
    return secure_private_directory(path, create=True)


def _write_json(path: Path, value: Any) -> None:
    parent = secure_private_directory(path.expanduser().parent, create=True)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _read_json(path: Path) -> dict[str, Any]:
    _secure_path, descriptor = secure_private_file(path, create=False, max_bytes=MAX_GUARD_BYTES)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, MAX_GUARD_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_GUARD_BYTES:
                raise ProjectBindingError("private_file_too_large")
    finally:
        os.close(descriptor)
    value = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(value, dict):
        raise HarnessContractError("session_guard_schema_invalid")
    return value


def _replace_json(path: Path, value: Any) -> None:
    _existing, descriptor = secure_private_file(path, create=False, max_bytes=MAX_GUARD_BYTES)
    os.close(descriptor)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        _write_json(temporary, value)
        parent = secure_private_directory(path.expanduser().parent, create=False)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.replace(temporary.name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _guard_lock(path: Path, *, exclusive: bool, blocking: bool = False):
    """Hold a crash-releasing guard lock; the runner inherits the descriptor."""

    lock_path = path.with_name(path.name + ".lock")
    _lock_path, descriptor = secure_private_file(lock_path, create=True)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(descriptor, operation)
    except BlockingIOError:
        os.close(descriptor)
        raise HarnessContractError("session_guard_orchestrator_active")
    try:
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _absolute_unresolved(path: Path) -> Path:
    """Canonicalize spelling without following the final path or ancestors."""

    return Path(os.path.abspath(os.path.normpath(str(path.expanduser()))))


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()


def _codex_boundary() -> tuple[str, dict[str, Any]]:
    return _canonical_digest({"provider": "Codex", "runtime_config": {}}), {}


def _grok_boundary(
    grok_home: Path, model_override: str | None,
) -> tuple[str, dict[str, Any]]:
    secured = _secure_grok_home(grok_home)
    info = secured.stat()
    auth_identity = grok_auth_file_identity(secured)
    helper_identity = grok_http_helper_identity()
    boundary = {
        "provider": "Grok", "grok_home_realpath": str(secured),
        "grok_home_device": info.st_dev, "grok_home_inode": info.st_ino,
        "grok_auth_file_identity": auth_identity,
        "grok_http_helper_identity": helper_identity,
    }
    return _canonical_digest(boundary), {
        "grok_home_path_sha256": hashlib.sha256(str(secured).encode()).hexdigest(),
        "grok_home_device": info.st_dev, "grok_home_inode": info.st_ino,
        "grok_model_override": model_override,
        "grok_auth_file_identity": auth_identity,
        "grok_http_helper_identity": helper_identity,
    }


def _configure_local_environment(
    args: argparse.Namespace, environment: dict[str, str],
) -> dict[str, Any]:
    required = {
        "endpoint": args.local_endpoint,
        "model": args.local_model,
        "selector": args.local_selector,
        "mode": args.local_mode,
        "attestation_file": args.local_attestation_file,
        "bridge_file": args.local_bridge_file,
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ProviderAdapterError("local_explicit_configuration_required:" + ",".join(sorted(missing)))
    endpoint, endpoint_policy = normalize_local_endpoint(
        str(args.local_endpoint), bool(args.local_allow_loopback_http),
    )
    attestation = _read_json(_absolute_unresolved(args.local_attestation_file))
    bridge = _read_json(_absolute_unresolved(args.local_bridge_file))
    environment.update({
        "AGENT_FANOUT_LOCAL_AVAILABLE": "1",
        "AGENT_FANOUT_LOCAL_HOSTED_NO_TOOL": "1",
        "AGENT_FANOUT_LOCAL_ENDPOINT": endpoint,
        "AGENT_FANOUT_LOCAL_MODEL_ID": str(args.local_model),
        "AGENT_FANOUT_LOCAL_MODEL_ALIAS": str(args.local_selector),
        "AGENT_FANOUT_LOCAL_REQUIRED_MODE": str(args.local_mode),
        "AGENT_FANOUT_LOCAL_ALLOW_LOOPBACK_HTTP": "1" if args.local_allow_loopback_http else "0",
        "AGENT_FANOUT_LOCAL_ATTESTATION_JSON": json.dumps(attestation, sort_keys=True, separators=(",", ":")),
        "AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON": json.dumps(bridge, sort_keys=True, separators=(",", ":")),
    })
    return {
        "endpoint": endpoint, "endpoint_policy": endpoint_policy,
        "attestation": attestation, "bridge": bridge,
    }


def _local_boundary(
    args: argparse.Namespace,
    local_config: dict[str, Any],
    *,
    task_class: str,
    reasoning_effort: str,
    selector: str,
) -> tuple[str, dict[str, Any]]:
    if selector != args.local_selector:
        raise ProviderAdapterError("local_route_selector_mismatch")
    validated = validate_local_host_evidence(
        local_config["attestation"], local_config["bridge"],
        selector=selector, exact_model_id=str(args.local_model),
        endpoint=local_config["endpoint"], task_class=task_class,
        reasoning_effort=reasoning_effort, required_mode=str(args.local_mode),
        allow_loopback_http=bool(args.local_allow_loopback_http),
    )
    endpoint_sha256 = hashlib.sha256(validated["endpoint"].encode()).hexdigest()
    runtime_config = {
        "endpoint_sha256": endpoint_sha256,
        "endpoint_policy": validated["endpoint_policy"],
        "allow_loopback_http": bool(args.local_allow_loopback_http),
        "selector": selector,
        "exact_model_id": validated["exact_model_id"],
        "required_mode": validated["required_mode"],
        "attestation_sha256": validated["capability_attestation_sha256"],
        "bridge_observation_sha256": validated["bridge_observation_sha256"],
    }
    boundary = {
        "provider": "Local", "endpoint": validated["endpoint"],
        "endpoint_policy": validated["endpoint_policy"],
        "allow_loopback_http": bool(args.local_allow_loopback_http),
        "selector": selector, "exact_model_id": validated["exact_model_id"],
        "required_mode": validated["required_mode"],
        "attestation_sha256": validated["capability_attestation_sha256"],
        "bridge_observation_sha256": validated["bridge_observation_sha256"],
    }
    return _canonical_digest(boundary), runtime_config


def _emit(value: Any, *, pretty: bool = True) -> None:
    print(json.dumps(value, indent=2 if pretty else None, sort_keys=True, default=str))


def _remediation(reason: str, provider: str | None = None) -> dict[str, Any]:
    """Return bounded, machine-actionable recovery without weakening a gate."""

    normalized = str(reason)
    if "approved_project_root_missing" in normalized:
        return {
            "category": "project_binding", "retryable": True,
            "summary": "Bind one exact target project before routing.",
            "safe_options": [
                {"flag": "--project-root", "value": "/exact/path/to/one/project"},
                {"flag": "--discover-current-worktree", "precondition": "current directory is inside the intended Git worktree"},
            ],
            "forbidden_fallback": "Do not substitute home, an installation directory, or a multi-project parent.",
        }
    if "approved_project_root_sensitive" in normalized or "approved_project_root_too_broad" in normalized:
        return {
            "category": "project_binding", "retryable": True,
            "summary": "The selected root is intentionally outside the worker safety boundary.",
            "safe_options": [
                {"action": "select_single_project_worktree", "requirement": "non-sensitive exact Git worktree"},
                {"action": "create_sanitized_fixture", "requirement": "copy only non-sensitive UAT inputs into a dedicated test repository"},
            ],
            "forbidden_fallback": "Do not allowlist or weaken the guard for a vault, home directory, runtime, auth tree, or project collection.",
        }
    if "approved_project_root_not_repo_or_allowlisted" in normalized:
        return {
            "category": "project_binding", "retryable": True,
            "summary": "Use an exact Git worktree, or explicitly allowlist one exact non-Git project.",
            "environment": "AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST=/exact/non-git/project",
        }
    if "grok_isolated_home_required" in normalized or "grok_explicit_home_required" in normalized:
        return {
            "category": "provider_configuration", "retryable": True,
            "summary": "Provision a dedicated owner-only Grok home and pass it explicitly.",
            "required_flag": {"flag": "--grok-home", "value": "/secure/dedicated/agentfanout-grok-home"},
            "documentation": "adapters/Grok.md",
        }
    if "local_explicit_configuration_required" in normalized:
        return {
            "category": "provider_configuration", "retryable": True,
            "summary": "Bind the exact credential-free Local endpoint, loaded model, mode, and fresh host evidence.",
            "required_flags": [
                "--local-endpoint", "--local-model", "--local-selector", "--local-mode",
                "--local-attestation-file", "--local-bridge-file",
            ],
            "documentation": "adapters/LocalLLM.md",
        }
    if "codex_native" in normalized or "real_provider_attestation" in normalized:
        return {
            "category": "provider_unavailable", "retryable": False,
            "summary": "The bundled strict Codex CLI lane is not admitted; this is a compatibility block, not an installation failure.",
            "safe_options": ["Use an already configured Grok or Local worker", "Use AgentFanout as advisory policy in an outer Codex harness"],
            "fallback_allowed": False,
        }
    if "provider_binary_unavailable" in normalized:
        return {
            "category": "provider_unavailable", "retryable": True,
            "summary": f"Install and authenticate the selected {provider or 'provider'} adapter, then rerun doctor.",
        }
    return {
        "category": "blocked", "retryable": False,
        "summary": "Inspect the exact block reason and provider documentation; parent fallback remains disabled.",
        "fallback_allowed": False,
    }


def _binding(args: argparse.Namespace) -> dict[str, Any]:
    return resolve_project_binding(
        explicit_root=args.project_root,
        discover_current_worktree=args.discover_current_worktree,
    )


def _run_json(
    argv: list[str],
    env: dict[str, str],
    *,
    timeout_seconds: float | None = 30.0,
    graceful_signal_forward: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> tuple[int, dict[str, Any]]:
    """Run a bounded JSON helper without orphaning its original process group.

    Routing helpers receive a short outer timeout.  The durable hosted runner
    deliberately receives no outer timeout: its own per-provider, wave, and
    cycle limits own cancellation, so this entrypoint never kills the runner
    while it may still supervise provider process groups.
    """

    prior_sigterm: Any = None
    signal_handler_installed = threading.current_thread() is threading.main_thread()
    if signal_handler_installed:
        prior_sigterm = signal.getsignal(signal.SIGTERM)

        def _raise_sigterm(signum: int, _frame: Any) -> None:
            raise _ForwardedProcessSignal(signum)

        signal.signal(signal.SIGTERM, _raise_sigterm)
    forwarded_signal: int | None = None
    try:
        try:
            process = subprocess.Popen(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=ROOT,
                start_new_session=os.name == "posix",
                pass_fds=pass_fds,
            )
        except OSError as exc:
            return 4, {"error": "child_launch_failed", "detail": str(exc)[:500]}
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except (KeyboardInterrupt, _ForwardedProcessSignal) as exc:
            forwarded_signal = signal.SIGINT if isinstance(exc, KeyboardInterrupt) else exc.signum
            if graceful_signal_forward:
                try:
                    os.kill(process.pid, forwarded_signal)
                except ProcessLookupError:
                    pass
                # The durable runner owns cancellation of its separately
                # sessioned provider groups. Wait for its structured shutdown
                # instead of killing only the supervisor and orphaning work.
                stdout, stderr = process.communicate(timeout=None)
            else:
                termination_confirmed = terminate_process_group(process, process.pid)
                return 4, {
                    "error": "child_interrupted",
                    "signal": signal.Signals(forwarded_signal).name,
                    "termination_confirmed": termination_confirmed,
                    "termination_scope": "process_group_only" if termination_confirmed else "unknown",
                }
        except subprocess.TimeoutExpired:
            termination_confirmed = terminate_process_group(process, process.pid)
            try:
                stdout, stderr = process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                stdout, stderr = "", ""
            return 4, {
                "error": "child_timeout",
                "timeout_seconds": timeout_seconds,
                "termination_confirmed": termination_confirmed,
                "termination_scope": "process_group_only" if termination_confirmed else "unknown",
            }
    finally:
        if signal_handler_installed:
            signal.signal(signal.SIGTERM, prior_sigterm)
    if len(stdout.encode("utf-8")) > MAX_CHILD_OUTPUT or len(stderr.encode("utf-8")) > MAX_CHILD_OUTPUT:
        return 4, {"error": "child_output_limit_exceeded"}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return 4, {"error": "child_output_not_json", "returncode": process.returncode}
    if forwarded_signal is not None and isinstance(payload, dict):
        try:
            payload.setdefault("shutdown_signal", signal.Signals(forwarded_signal).name)
        except ValueError:
            payload.setdefault("shutdown_signal", str(forwarded_signal))
    return process.returncode, payload


def _root_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project-root", type=Path)
    group.add_argument("--discover-current-worktree", action="store_true")


def _load_manifest() -> dict[str, Any]:
    manifest = json.loads(RUNNER_MANIFEST.read_text(encoding="utf-8"))
    validate_manifest(manifest, require_level="runner_enforced")
    return manifest


def command_doctor(args: argparse.Namespace) -> int:
    if args.execution_profile in {"coordinator_local", "mixed_provider_runs"}:
        status = capability_status()
        lane = "coordinator_local" if args.execution_profile == "coordinator_local" else "mixed"
        _emit({"schema": "AgentFanoutDoctorV1", "ready": False, "profile": args.execution_profile,
               "reason": status["lanes"][lane]["reason"], "upgrade_capabilities": status,
               "build_identity": build_report(ROOT, Path(__file__)), "fallback_allowed": False})
        return 2
    if args.provider == "GrokCLI":
        _emit({"schema": "AgentFanoutDoctorV1", "ready": False, "provider": "GrokCLI",
               "reason": "grok_cli_installed_boundary_unproved",
               "build_identity": build_report(ROOT, Path(__file__)),
               "upgrade_capabilities": capability_status(), "fallback_allowed": False})
        return 2
    try:
        binding = _binding(args)
        manifest = _load_manifest()
        state_directory = _secure_directory(args.state_dir or _state_home())
    except (OSError, json.JSONDecodeError, ProjectBindingError, HarnessContractError) as exc:
        reason = str(exc)
        _emit({
            "schema": "AgentFanoutDoctorV1", "ready": False, "reason": reason,
            "remediation": _remediation(reason, getattr(args, "provider", None)),
        })
        return 3
    checks: dict[str, Any] = {
        "project_binding": {"passed": True, "resolved_root": binding["resolved_root"]},
        "host_manifest": {"passed": True, "level": manifest["enforcement"]["level"]},
        "state_directory": {"passed": True, "path": str(state_directory)},
    }
    provider_ready = False
    selector = args.selector or {
        "Codex": "current-fast-mini", "Grok": "grok-fast", "Local": args.local_selector,
    }.get(args.provider)
    if args.provider == "Codex":
        binary = shutil.which(args.codex_binary)
        checks["provider_binary"] = {
            "passed": binary is not None, "path": binary,
            **({} if binary is not None else {"reason": "provider_binary_unavailable:codex"}),
        }
        if binary and not args.binary_only:
            try:
                frozen = executable_identity(binary)
                models = discover_codex_models(
                    binary, timeout_seconds=args.probe_timeout,
                    expected_executable_identity=frozen,
                )
                resolved = resolve_codex_model(selector, models)
                checks["provider_models"] = {
                    "passed": True, "count": len(models), "ids": [item["id"] for item in models],
                    "selector": selector, "resolved_model": resolved,
                }
                compatibility = codex_no_tool_runtime_attestation(
                    binary, timeout_seconds=args.probe_timeout,
                    resolved_model=resolved, session_mode="ephemeral",
                    expected_executable_identity=frozen,
                )
                transport = compatibility["transport_attestation"]
                checks["provider_compatibility"] = {
                    "passed": True,
                    "gate": compatibility["schema"],
                    "transport_schema": transport["schema"],
                    "tools_empty": transport["tools_empty"],
                    "loopback_only": transport["loopback_only"],
                }
            except ProviderAdapterError as exc:
                if "provider_models" not in checks:
                    checks["provider_models"] = {"passed": False, "reason": str(exc)}
                else:
                    checks["provider_compatibility"] = {"passed": False, "reason": str(exc)}
        elif binary:
            checks["provider_models"] = {"passed": False, "reason": "binary_only_diagnostic_not_readiness"}
            checks["provider_compatibility"] = {
                "passed": False, "reason": "binary_only_diagnostic_not_readiness",
            }
        provider_ready = bool(binary) and all(
            checks.get(name, {"passed": False})["passed"]
            for name in ("provider_models", "provider_compatibility")
        )
    elif args.provider == "Grok":
        binary = shutil.which(args.grok_binary)
        checks["provider_binary"] = {
            "passed": binary is not None, "path": binary,
            **({} if binary is not None else {"reason": "provider_binary_unavailable:grok"}),
        }
        try:
            if args.grok_home is None:
                raise ProviderAdapterError("grok_isolated_home_required")
            boundary_sha, runtime = _grok_boundary(args.grok_home, args.grok_model)
            checks["provider_boundary"] = {"passed": True, "sha256": boundary_sha}
            if not binary:
                raise ProviderAdapterError("provider_binary_unavailable:grok")
            if args.binary_only:
                raise ProviderAdapterError("binary_only_diagnostic_not_readiness")
            frozen = executable_identity(binary)
            models = discover_grok_models(
                binary, timeout_seconds=args.probe_timeout,
                expected_executable_identity=frozen,
                grok_home=args.grok_home,
                expected_grok_auth_file_identity=runtime["grok_auth_file_identity"],
                expected_grok_helper_identity=runtime["grok_http_helper_identity"],
            )
            listed = {str(item.get("id")) for item in models}
            resolved = args.grok_model or resolve_grok_model(selector, models)
            if resolved not in listed:
                raise ProviderAdapterError("grok_configured_model_not_listed")
            grok_reasoning_effort_mode(resolved)
            grok_config = provider_config("Grok") or {}
            matching_profiles = [
                profile
                for profile in grok_config.get("profiles", {}).values()
                if isinstance(profile, dict) and profile.get("selector") == selector
            ]
            if len(matching_profiles) != 1:
                raise ProviderAdapterError("grok_selector_profile_binding_invalid")
            required_efforts = matching_profiles[0].get("reasoning_efforts")
            supported_efforts = grok_supported_reasoning_efforts(resolved)
            if (
                not isinstance(required_efforts, list)
                or not required_efforts
                or any(effort not in supported_efforts for effort in required_efforts)
            ):
                raise ProviderAdapterError("grok_model_reasoning_effort_unsupported")
            checks["provider_models"] = {
                "passed": True, "count": len(models), "ids": sorted(listed),
                "selector": selector, "resolved_model": resolved,
            }
            compatibility = grok_direct_transport_preflight(
                args.grok_home,
                runtime["grok_auth_file_identity"],
                runtime["grok_http_helper_identity"],
                timeout_seconds=args.probe_timeout,
            )
            checks["provider_compatibility"] = {
                "passed": True,
                "gate": compatibility["schema"],
                "credential_type": compatibility["credential_type"],
                "credential_network_verified": compatibility["network_verified"],
                "tools_field_sent": compatibility["tools_field_sent"],
                "redirects_allowed": compatibility["redirects_allowed"],
                "ambient_proxy_allowed": compatibility["ambient_proxy_allowed"],
                "helper_identity_sha256": compatibility["helper_identity_sha256"],
            }
            provider_ready = True
        except (OSError, ValueError, ProjectBindingError, ProviderAdapterError) as exc:
            checks.setdefault("provider_boundary", {"passed": False, "reason": str(exc)})
            checks.setdefault("provider_models", {"passed": False, "reason": str(exc)})
            checks.setdefault("provider_compatibility", {"passed": False, "reason": str(exc)})
    elif args.provider == "Local":
        local_environment = os.environ.copy()
        try:
            local_config = _configure_local_environment(args, local_environment)
            boundary_sha, runtime = _local_boundary(
                args, local_config, task_class=args.local_task_class,
                reasoning_effort=args.local_reasoning_effort, selector=selector,
            )
            helper_identity = executable_identity(local_helper_binary())
            checks["provider_boundary"] = {"passed": True, "sha256": boundary_sha}
            checks["provider_model"] = {
                "passed": True, "selector": selector,
                "resolved_model": runtime["exact_model_id"],
            }
            checks["provider_helper"] = {
                "passed": True, "sha256": helper_identity["executable_sha256"],
                "argument_files": len(helper_identity["argument_file_identities"]),
            }
            provider_ready = True
        except (OSError, ValueError, ProjectBindingError, ProviderAdapterError) as exc:
            checks["provider_boundary"] = {"passed": False, "reason": str(exc)}
    else:
        checks["provider_adapter"] = {"passed": False, "reason": f"hosted_adapter_unavailable:{args.provider.lower()}"}
    ready = all(value.get("passed") is True for value in checks.values()) and provider_ready
    output = {
        "schema": "AgentFanoutDoctorV1", "ready": ready, "provider": args.provider,
        "build_identity": build_report(ROOT, Path(__file__)),
        "upgrade_capabilities": capability_status(),
        "checks": checks,
        "enforcement": {
            "level": "runner_enforced", "scope": "standalone_hosted_runner_process",
            "arbitrary_outer_harness": "not_certified", "fallback_allowed": False,
        },
    }
    if not ready:
        failed_reason = next(
            (str(value.get("reason")) for value in checks.values() if value.get("passed") is False and value.get("reason")),
            "provider_not_ready",
        )
        output["remediation"] = _remediation(failed_reason, args.provider)
    _emit(output)
    return 0 if ready else 2


def _route_prompt(provider: str, prompt: str, *, accept_overhead: bool = False) -> str:
    """Select a provider without silently accepting orchestration overhead."""

    routed = prompt if provider == "Auto" else f"use {provider} for this task: {prompt}"
    return f"I accept orchestration overhead. {routed}" if accept_overhead else routed


def _guard_transition(
    guard_file: Path,
    guard: dict[str, Any],
    target: str,
    *,
    reason: str,
) -> dict[str, Any]:
    validate_session_guard(guard)
    current = guard["status"]
    if current in {"released", "abandoned"}:
        if current != target:
            raise HarnessContractError(f"session_guard_terminal:{current}")
        return guard
    if target not in {"blocked", "released", "abandoned"}:
        raise HarnessContractError("session_guard_transition_invalid")
    updated = dict(guard)
    updated["status"] = target
    if target == "blocked":
        updated["blocked_at"] = _now()
        updated["blocked_reason"] = (reason or "runner_blocked")[:2000]
    elif target == "released":
        updated["released_at"] = _now()
        updated["released_reason"] = (reason or "terminal_authorized")[:500]
    else:
        updated["abandoned_at"] = _now()
        updated["abandoned_reason_sha256"] = hashlib.sha256(reason.encode("utf-8")).hexdigest()
    validate_session_guard(updated)
    _replace_json(guard_file, updated)
    return updated


def _guard_paths(guard: dict[str, Any]) -> tuple[Path, Path]:
    state_text = guard.get("run_state_path")
    ledger_text = guard.get("ledger_path")
    if not isinstance(state_text, str) or not isinstance(ledger_text, str):
        raise HarnessContractError("session_guard_path_binding_invalid")
    if hashlib.sha256(state_text.encode()).hexdigest() != guard.get("run_state_path_sha256"):
        raise HarnessContractError("session_guard_state_path_digest_mismatch")
    if hashlib.sha256(ledger_text.encode()).hexdigest() != guard.get("ledger_path_sha256"):
        raise HarnessContractError("session_guard_ledger_path_digest_mismatch")
    state_path = Path(state_text)
    ledger_path = Path(ledger_text)
    if not state_path.is_absolute() or not ledger_path.is_absolute():
        raise HarnessContractError("session_guard_path_not_absolute")
    return state_path, ledger_path


def _verify_guard_state_binding(
    guard: dict[str, Any],
    binding: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> tuple[Path, Path, dict[str, Any]]:
    validate_session_guard(guard)
    state_path, ledger_path = _guard_paths(guard)
    if guard["project_binding_sha256"] != binding.get("root_sha256"):
        raise HarnessContractError("session_guard_project_binding_mismatch")
    if manifest is not None and guard["host_manifest_sha256"] != manifest_digest(manifest):
        raise HarnessContractError("session_guard_manifest_binding_mismatch")
    state = load_run_state(state_path)
    if state.get("run_id") != guard["run_id"]:
        raise HarnessContractError("session_guard_run_id_mismatch")
    validate_project_binding(state.get("project_binding"), expected_root=binding["resolved_root"])
    if state.get("project_binding", {}).get("root_sha256") != guard["project_binding_sha256"]:
        raise HarnessContractError("session_guard_state_project_mismatch")
    project_root = Path(binding["resolved_root"])
    if any(path == project_root or path_within(path, project_root) for path in (state_path, ledger_path)):
        raise HarnessContractError("state_artifact_inside_project_denied")
    providers = {
        task.get("provider_model", {}).get("provider")
        for task in state.get("tasks", []) if isinstance(task, dict)
    }
    selectors = {
        task.get("provider_model", {}).get("selector")
        for task in state.get("tasks", []) if isinstance(task, dict)
    }
    if providers != {guard["provider"]} or selectors != {guard["model_selector"]}:
        raise HarnessContractError("session_guard_model_binding_mismatch")
    return state_path, ledger_path, state


def _verify_guard_self_binding(
    guard: dict[str, Any],
) -> tuple[Path, Path, dict[str, Any]]:
    state_path, _ledger_path = _guard_paths(guard)
    state = load_run_state(state_path)
    binding = state.get("project_binding")
    if not isinstance(binding, dict):
        raise HarnessContractError("session_guard_state_project_mismatch")
    return _verify_guard_state_binding(guard, binding, _load_manifest())


def _ledger_events(ledger_path: Path, run_id: str) -> list[dict[str, Any]]:
    if not os.path.lexists(ledger_path):
        return []
    ledger = HostLedger(ledger_path)
    try:
        return ledger.events(run_id)
    finally:
        ledger.close()


def _event_correlations(
    events: list[dict[str, Any]], event_type: str, key: str,
) -> set[str]:
    return {
        value
        for event in events if event.get("event_type") == event_type
        for value in [event.get("payload", {}).get(key)]
        if isinstance(value, str) and value
    }


def _recovery_assessment(
    guard: dict[str, Any],
    state: dict[str, Any],
    ledger_path: Path,
    *,
    orchestrator_active: bool,
) -> dict[str, Any]:
    hazards: list[str] = []
    planned_not_launched: list[str] = []
    try:
        events = _ledger_events(ledger_path, guard["run_id"])
    except Exception as exc:
        events = []
        hazards.append(f"ledger_integrity_unavailable:{type(exc).__name__}:{exc}")

    receipts = _event_correlations(events, "launch_receipt", "launch_correlation_id")
    accepted = _event_correlations(events, "worker_result_accepted", "launch_correlation_id")
    termination_confirmed = _event_correlations(events, "launch_termination_confirmed", "launch_correlation_id")
    cancelled = _event_correlations(events, "launch_cancelled", "launch_correlation_id")
    safely_cancelled = termination_confirmed & cancelled
    active_receipts = receipts - accepted - safely_cancelled
    for correlation in sorted(active_receipts):
        hazards.append(f"worker_launch_not_terminal:{correlation}")

    records = state.get("launch_records", {})
    if not isinstance(records, dict):
        hazards.append("launch_records_invalid")
        records = {}
    for correlation, record in records.items():
        if not isinstance(record, dict) or record.get("status") != "active":
            continue
        if correlation in receipts:
            hazards.append(f"active_external_launch:{correlation}")
        else:
            # Host contract: the durable receipt is committed before adapter.run.
            # A record without that receipt is a planned identity, not proof that
            # an external provider process started.
            planned_not_launched.append(correlation)
    for correlation in sorted(receipts - set(records)):
        hazards.append(f"orphan_launch_receipt:{correlation}")

    latest_effects: dict[tuple[str | None, str], dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "effect_record":
            continue
        effect_id = event.get("payload", {}).get("effect_id")
        if isinstance(effect_id, str):
            latest_effects[(event.get("task_id"), effect_id)] = event.get("payload", {})
    for (task_id, effect_id), effect in sorted(latest_effects.items(), key=lambda item: str(item[0])):
        if effect.get("state") in {"pending", "unknown_after_crash"}:
            hazards.append(f"unresolved_effect:{task_id}:{effect_id}:{effect.get('state')}")

    validator_starts = _event_correlations(
        events, "validator_attempt_started", "validator_launch_correlation_id"
    )
    validator_results = _event_correlations(
        events, "validator_result", "validator_launch_correlation_id"
    )
    validator_failures = _event_correlations(
        events, "validator_attempt_failed", "validator_launch_correlation_id"
    )
    for correlation in sorted(validator_starts - validator_results - validator_failures):
        hazards.append(f"validator_attempt_unmatched:{correlation}")

    always_unknown = {
        "worker_process_group_exit_unconfirmed",
        "validator_process_group_exit_unconfirmed",
        "launch_unknown_after_crash",
        "validator_attempt_unknown_after_crash",
    }
    for event in events:
        if event.get("event_type") in always_unknown:
            hazards.append(f"unknown_external_effect:{event['event_type']}:{event.get('event_id')}")
    for index, event in enumerate(events):
        if event.get("event_type") != "launch_termination_pending":
            continue
        correlation = event.get("payload", {}).get("launch_correlation_id")
        later_confirmed = any(
            later.get("event_type") == "launch_termination_confirmed"
            and later.get("payload", {}).get("launch_correlation_id") == correlation
            for later in events[index + 1:]
        )
        if not later_confirmed:
            hazards.append(f"termination_pending:{correlation}")

    terminal_authorized = bool(
        [event for event in events if event.get("event_type") == "terminal_authorized"]
    )
    if state.get("status") == "complete" and not terminal_authorized:
        hazards.append("complete_state_without_terminal_evidence")
    hazards = sorted(set(hazards))
    active_guard = guard["status"] in {"armed", "blocked"}
    return {
        "orchestrator_process_active": orchestrator_active,
        "guard_status": guard["status"],
        "run_status": state.get("status"),
        "terminal_authorized": terminal_authorized,
        "planned_not_launched": sorted(planned_not_launched),
        "hazards": hazards,
        "unknown_external_effects_present": bool(hazards),
        "resume_allowed": bool(
            active_guard and not orchestrator_active and not hazards
            and state.get("status") not in {"blocked", "complete"}
        ),
        "abandon_allowed": bool(
            active_guard and not orchestrator_active and not hazards
            and state.get("status") != "complete"
        ),
        "provider_replay_allowed": False if hazards else None,
        "parent_fallback_allowed": False,
    }


def _verify_resume_provider(
    guard: dict[str, Any], args: argparse.Namespace, state: dict[str, Any],
    environment: dict[str, str],
) -> dict[str, Any]:
    provider = guard["provider"]
    if provider == "Codex":
        binary: str | list[str] = args.codex_binary
        verify_executable_identity(binary, guard["provider_executable_identity"])
        models = discover_codex_models(
            binary, timeout_seconds=args.probe_timeout,
            expected_executable_identity=guard["provider_executable_identity"],
        )
        resolved = resolve_codex_model(guard["model_selector"], models)
        boundary_sha, runtime_config = _codex_boundary()
        options = {"binary": guard["provider_executable_identity"]["executable_realpath"]}
    elif provider == "Grok":
        if args.grok_home is None:
            raise HarnessContractError("grok_isolated_home_required")
        stored_override = guard["provider_runtime_config"].get("grok_model_override")
        if args.grok_model != stored_override:
            raise HarnessContractError("session_guard_grok_model_override_mismatch")
        home = _secure_grok_home(args.grok_home)
        boundary_sha, runtime_config = _grok_boundary(home, args.grok_model)
        # Compare the entirely local boundary before model discovery or auth
        # preflight can copy credentials into any child. A changed helper,
        # auth file, or home is never executed merely to learn that it drifted.
        if (
            boundary_sha != guard["provider_boundary_sha256"]
            or runtime_config != guard["provider_runtime_config"]
        ):
            raise HarnessContractError("session_guard_provider_boundary_changed")
        binary = args.grok_binary
        verify_executable_identity(binary, guard["provider_executable_identity"])
        models = discover_grok_models(
            binary, timeout_seconds=args.probe_timeout,
            expected_executable_identity=guard["provider_executable_identity"],
            grok_home=home,
            expected_grok_auth_file_identity=runtime_config["grok_auth_file_identity"],
            expected_grok_helper_identity=runtime_config["grok_http_helper_identity"],
        )
        listed = {str(item.get("id")) for item in models}
        if guard["resolved_model"] not in listed:
            raise HarnessContractError("session_guard_resolved_model_not_listed")
        grok_reasoning_effort_mode(guard["resolved_model"])
        grok_direct_transport_preflight(
            home,
            runtime_config["grok_auth_file_identity"],
            runtime_config["grok_http_helper_identity"],
            timeout_seconds=args.probe_timeout,
        )
        resolved = (
            args.grok_model
            if args.grok_model is not None
            else resolve_grok_model(guard["model_selector"], models)
        )
        options = {
            "binary": guard["provider_executable_identity"]["executable_realpath"],
            "grok_home": home,
            "grok_auth_file_identity": runtime_config["grok_auth_file_identity"],
            "grok_http_helper_identity": runtime_config["grok_http_helper_identity"],
        }
        environment["AGENT_FANOUT_GROK_AVAILABLE"] = "1"
    elif provider == "Local":
        local_config = _configure_local_environment(args, environment)
        task = state["tasks"][0]
        boundary_sha, runtime_config = _local_boundary(
            args, local_config, task_class=task["task_class"],
            reasoning_effort=task["reasoning_effort"], selector=guard["model_selector"],
        )
        resolved = runtime_config["exact_model_id"]
        binary = local_helper_binary()
        verify_executable_identity(binary, guard["provider_executable_identity"])
        options = {"binary": binary}
    else:
        raise HarnessContractError(f"hosted_adapter_unavailable:{provider.lower()}")
    if boundary_sha != guard["provider_boundary_sha256"] or runtime_config != guard["provider_runtime_config"]:
        raise HarnessContractError("session_guard_provider_boundary_changed")
    if resolved != guard["resolved_model"]:
        raise HarnessContractError("session_guard_resolved_model_changed")
    verify_executable_identity(binary, guard["provider_executable_identity"])
    return options


def _runner_arguments(
    guard: dict[str, Any], state_path: Path, ledger_path: Path, project_root: str,
    provider_options: dict[str, Any], max_cycles: int,
) -> list[str]:
    arguments = [
        sys.executable, "-S", str(RUNNER), "--state-file", str(state_path),
        "--ledger", str(ledger_path), "--project-root", project_root,
        "--provider", guard["provider"], "--resolved-model", guard["resolved_model"],
        "--expected-executable-identity-json",
        json.dumps(guard["provider_executable_identity"], sort_keys=True, separators=(",", ":")),
        "--expected-host-manifest-sha256", guard["host_manifest_sha256"],
        "--max-workers", str(guard["max_workers"]), "--max-cycles", str(max_cycles), "--compact",
    ]
    if guard["provider"] == "Codex":
        arguments.extend(["--codex-binary", str(provider_options["binary"])])
    elif guard["provider"] == "Grok":
        arguments.extend([
            "--grok-binary", str(provider_options["binary"]),
            "--grok-home", str(provider_options["grok_home"]),
            "--expected-grok-auth-identity-json",
            json.dumps(
                provider_options["grok_auth_file_identity"],
                sort_keys=True, separators=(",", ":"),
            ),
            "--expected-grok-helper-identity-json",
            json.dumps(
                provider_options["grok_http_helper_identity"],
                sort_keys=True, separators=(",", ":"),
            ),
        ])
    return arguments


def _run_bound_runner(
    guard: dict[str, Any], state_path: Path, ledger_path: Path, project_root: str,
    provider_options: dict[str, Any], max_cycles: int,
    environment: dict[str, str], lock_fd: int,
) -> tuple[int, dict[str, Any]]:
    runner_code, outcome = _run_json(
        _runner_arguments(guard, state_path, ledger_path, project_root, provider_options, max_cycles),
        environment,
        timeout_seconds=None,
        graceful_signal_forward=True,
        pass_fds=(lock_fd,),
    )
    if (
        runner_code == 0
        and (
            not isinstance(outcome, dict)
            or outcome.get("enforcement", {}).get("host_manifest_sha256")
            != guard["host_manifest_sha256"]
        )
    ):
        return 4, {
            "error": "runner_host_manifest_binding_mismatch",
            "termination_confirmed": True,
        }
    return runner_code, outcome


def _normalize_runner_failure(runner_code: int, outcome: dict[str, Any]) -> dict[str, Any]:
    if runner_code != 0 and outcome.get("error"):
        return {
            "status": "fanout_blocked",
            "block_reason": f"runner_child_failed:{outcome['error']}",
            "enforcement": {
                "level": "runner_enforced",
                "fallback_allowed": False,
                "unknown_external_effects_possible": outcome.get("termination_confirmed") is not True,
            },
            "provenance": {"runner_child_failure": outcome},
        }
    return outcome


def command_run(args: argparse.Namespace) -> int:
    if args.execution_profile in {"coordinator_local", "mixed_provider_runs"}:
        status = capability_status()
        lane = "coordinator_local" if args.execution_profile == "coordinator_local" else "mixed"
        _emit({"schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked", "profile": args.execution_profile,
               "block_reason": status["lanes"][lane]["reason"], "upgrade_capabilities": status,
               "enforcement": {"fallback_allowed": False}})
        return 4
    if args.provider == "GrokCLI":
        _emit({"schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
               "provider": "GrokCLI", "mode": "grok_cli_subscription_acp",
               "block_reason": "grok_cli_installed_boundary_unproved",
               "enforcement": {"fallback_allowed": False},
               "upgrade_capabilities": capability_status()})
        return 4
    prompt = " ".join(item for item in args.prompt if item != "--").strip()
    if not prompt:
        _emit({"schema": "AgentFanoutRunOutcomeV1", "status": "binding_required", "block_reason": "prompt_required"})
        return 3
    try:
        binding = _binding(args)
        manifest = _load_manifest()
    except (OSError, json.JSONDecodeError, ProjectBindingError, HarnessContractError) as exc:
        reason = str(exc)
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "binding_required",
            "block_reason": reason, "remediation": _remediation(reason, args.provider),
        })
        return 3
    environment = os.environ.copy()
    environment["AGENT_FANOUT_PROJECT_ROOT"] = binding["resolved_root"]
    environment["AGENT_FANOUT_RUNTIME_MODE"] = "1"
    grok_home: Path | None = None
    grok_boundary: tuple[str, dict[str, Any]] | None = None
    local_config: dict[str, Any] | None = None
    try:
        if args.provider == "Grok" or args.grok_home is not None or args.grok_model is not None:
            if args.grok_home is None:
                raise ProviderAdapterError("grok_isolated_home_required")
            grok_home = _secure_grok_home(args.grok_home)
            grok_boundary = _grok_boundary(grok_home, args.grok_model)
            if shutil.which(args.grok_binary):
                environment["AGENT_FANOUT_GROK_AVAILABLE"] = "1"
            elif args.provider == "Grok":
                raise ProviderAdapterError("provider_binary_unavailable:grok")
        local_requested = args.provider == "Local" or any((
            args.local_endpoint, args.local_model, args.local_selector,
            args.local_mode, args.local_attestation_file, args.local_bridge_file,
        ))
        if local_requested:
            local_config = _configure_local_environment(args, environment)
    except (OSError, ValueError, ProjectBindingError, ProviderAdapterError, HarnessContractError) as exc:
        reason = str(exc)
        _emit({
            "schema": "AgentFanoutRunOutcomeV1",
            "status": "fanout_blocked",
            "block_reason": (
                reason if reason.startswith("provider_binary_unavailable:")
                else f"provider_configuration_invalid:{reason}"
            ),
            "project_binding": binding,
            "artifacts": {"guard_file": None, "state_file": None, "ledger_file": None},
            "enforcement": {
                "level": "runner_enforced", "fallback_allowed": False,
                "native_worker_tools_authorized": False,
            },
            "remediation": _remediation(reason, args.provider),
        }, pretty=args.pretty)
        return 2
    if args.provider in {"Auto", "Codex"}:
        # Advertise the implemented hosted lane to the router. This does not
        # launch a provider or override Auto's cost/capability decision.
        environment["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
    routed_prompt = _route_prompt(args.provider, prompt, accept_overhead=args.accept_overhead)
    route_argv = [sys.executable, "-S", str(ROUTE), "--project-root", binding["resolved_root"], routed_prompt]
    route_code, route = _run_json(route_argv, environment, timeout_seconds=30.0)
    dispatch = route.get("dispatch", {}) if isinstance(route, dict) else {}
    selected_provider = route.get("selected_provider") if isinstance(route, dict) else None
    route_summary = {
        "task_class": route.get("route", {}).get("task_class") if isinstance(route.get("route"), dict) else None,
        "provider": selected_provider,
        "requested_provider": args.provider,
        "dispatch_status": dispatch.get("status"),
        "planned_workers": dispatch.get("planned_workers"),
        "block_reason": dispatch.get("block_reason"),
        "adapter_input_errors": dispatch.get("adapter_input_errors", []),
        "anti_waste_guard": route.get("anti_waste_guard"),
        "decision_stage": "pre_admission" if dispatch.get("status") != "launch_required" else "worker_admission",
        "direct_execution_authorized": dispatch.get("status") == "not_required",
    }
    guard: dict[str, Any] | None = None
    artifacts = {"guard_file": None, "state_file": None, "ledger_file": None}
    provider_rejections: list[str] = []
    if (
        not args.dry_run
        and selected_provider == "Codex"
        and dispatch.get("status") == "launch_required"
    ):
        selector_candidate = route.get("route", {}).get("executor", {}).get("model_selector")
        preflight_stage = "model"
        try:
            if not isinstance(selector_candidate, str) or not selector_candidate:
                raise ProviderAdapterError("codex_task_selector_required")
            codex_binary = shutil.which(args.codex_binary)
            if not codex_binary:
                raise ProviderAdapterError("provider_binary_unavailable:codex")
            codex_identity = executable_identity(codex_binary)
            codex_models = discover_codex_models(
                codex_binary, timeout_seconds=args.probe_timeout,
                expected_executable_identity=codex_identity,
            )
            codex_model = resolve_codex_model(selector_candidate, codex_models)
            preflight_stage = "runtime_compatibility"
            codex_no_tool_runtime_attestation(
                codex_binary, timeout_seconds=args.probe_timeout,
                resolved_model=codex_model, session_mode="ephemeral",
                expected_executable_identity=codex_identity,
            )
        except ProviderAdapterError as exc:
            rejection = f"provider_{preflight_stage}_preflight_failed:{exc}"
            provider_rejections.append(rejection)
            if args.provider != "Auto":
                route_summary["provider_rejections"] = provider_rejections
                _emit({
                    "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
                    "project_binding": binding, "session_guard": None,
                    "route": route_summary, "artifacts": artifacts,
                    "block_reason": rejection,
                    "enforcement": {
                        "level": "runner_enforced", "fallback_allowed": False,
                        "native_worker_tools_authorized": False,
                    },
                }, pretty=args.pretty)
                return 2
            # Auto may replan only among other already configured worker
            # providers.  Explicitly suppress Codex even when the parent
            # process happens to expose CODEX_THREAD_ID.
            environment.pop("AGENT_FANOUT_CODEX_SUBAGENTS", None)
            environment["AGENT_FANOUT_CODEX_COMPATIBILITY_REJECTED"] = str(exc)
            route_code, route = _run_json(route_argv, environment, timeout_seconds=30.0)
            dispatch = route.get("dispatch", {}) if isinstance(route, dict) else {}
            selected_provider = route.get("selected_provider") if isinstance(route, dict) else None
            route_summary = {
                "task_class": route.get("route", {}).get("task_class") if isinstance(route.get("route"), dict) else None,
                "provider": selected_provider,
                "requested_provider": args.provider,
                "dispatch_status": dispatch.get("status"),
                "planned_workers": dispatch.get("planned_workers"),
                "block_reason": dispatch.get("block_reason"),
                "adapter_input_errors": dispatch.get("adapter_input_errors", []),
                "anti_waste_guard": route.get("anti_waste_guard"),
                "decision_stage": "worker_replan_after_provider_rejection",
                "direct_execution_authorized": False,
                "provider_rejections": provider_rejections,
            }
            if (
                route_code != 0
                or dispatch.get("status") != "launch_required"
                or selected_provider not in {"Grok", "Local"}
            ):
                _emit({
                    "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
                    "project_binding": binding, "session_guard": None,
                    "route": route_summary, "artifacts": artifacts,
                    "block_reason": (
                        "auto_no_compatible_worker_after_codex_rejection:" + rejection
                    ),
                    "enforcement": {
                        "level": "runner_enforced", "fallback_allowed": False,
                        "native_worker_tools_authorized": False,
                        "direct_execution_authorized": False,
                    },
                }, pretty=args.pretty)
                return 2
    if route_code != 0 or dispatch.get("status") != "launch_required":
        if args.dry_run:
            _emit({
                "schema": "AgentFanoutPlanV1", "status": "planned",
                "ready_to_launch": False, "project_binding": binding,
                "route": route_summary, "cost_projection": route.get("anti_waste_guard"),
                "overhead_accepted": bool(args.accept_overhead), "artifacts": artifacts,
                "provider_launches_started": 0, "provider_processes_started": 0,
                "model_discovery_probe_started": False,
                "direct_execution_recommended": dispatch.get("status") == "not_required",
                "worker_failure_fallback_allowed": False,
                "fallback_allowed": bool(dispatch.get("fallback_allowed")),
            }, pretty=args.pretty)
            return 0
        if dispatch.get("status") == "not_required":
            _emit({
                "schema": "AgentFanoutRunOutcomeV1", "status": "direct_recommended",
                "direct_execution_recommended": True, "fanout_started": False,
                "project_binding": binding, "session_guard": None,
                "route": route_summary, "artifacts": artifacts,
                "enforcement": {
                    "level": "runner_enforced", "fallback_allowed": False,
                    "native_worker_tools_authorized": False,
                    "direct_execution_authorized": True,
                    "authorization_stage": "pre_admission_anti_waste_or_direct_route",
                    "worker_failure_fallback_allowed": False,
                },
                "block_reason": None,
            }, pretty=args.pretty)
            return 0
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": dispatch.get("status") or "fanout_blocked",
            "project_binding": binding, "session_guard": guard, "route": route_summary,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False, "native_worker_tools_authorized": False},
            "block_reason": dispatch.get("block_reason") or route.get("error") or "route_not_launchable",
            "artifacts": artifacts,
        }, pretty=args.pretty)
        return 3 if dispatch.get("status") == "binding_required" else 2
    if selected_provider not in {"Codex", "Grok", "Local"}:
        if args.dry_run:
            _emit({
                "schema": "AgentFanoutPlanV1", "status": "planned",
                "ready_to_launch": False, "project_binding": binding,
                "route": route_summary, "cost_projection": route.get("anti_waste_guard"),
                "provider_preflight": {"passed": False, "reason": f"hosted_adapter_unavailable:{str(selected_provider).lower()}"},
                "overhead_accepted": bool(args.accept_overhead), "artifacts": artifacts,
                "provider_launches_started": 0, "provider_processes_started": 0,
                "model_discovery_probe_started": False,
                "fallback_allowed": False,
            }, pretty=args.pretty)
            return 0
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked", "project_binding": binding,
            "session_guard": guard, "route": route_summary,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
            "block_reason": f"hosted_adapter_unavailable:{str(selected_provider).lower()}", "artifacts": artifacts,
        }, pretty=args.pretty)
        return 2
    if selected_provider == "Codex":
        selected_binary: str | list[str] = args.codex_binary
    elif selected_provider == "Grok":
        selected_binary = args.grok_binary
        if grok_home is None or grok_boundary is None:
            _emit({
                "schema": "AgentFanoutRunOutcomeV1", "status": "binding_required",
                "block_reason": "grok_explicit_home_required", "project_binding": binding,
                "artifacts": artifacts,
                "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
            }, pretty=args.pretty)
            return 3
    else:
        selected_binary = local_helper_binary()
        if local_config is None:
            _emit({
                "schema": "AgentFanoutRunOutcomeV1", "status": "binding_required",
                "block_reason": "local_explicit_configuration_required", "project_binding": binding,
                "artifacts": artifacts,
                "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
            }, pretty=args.pretty)
            return 3
    binary = shutil.which(selected_binary) if isinstance(selected_binary, str) else selected_binary
    frozen_executable: dict[str, Any] | None = None
    if not binary:
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
            "project_binding": binding,
            "block_reason": f"provider_binary_unavailable:{str(selected_provider).lower()}",
            "artifacts": artifacts,
            "enforcement": {
                "level": "runner_enforced", "fallback_allowed": False,
                "native_worker_tools_authorized": False,
            },
        }, pretty=args.pretty)
        return 2
    try:
        frozen_executable = executable_identity(binary)
        if isinstance(binary, str):
            binary = frozen_executable["executable_realpath"]
    except ProviderAdapterError as exc:
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
            "project_binding": binding, "block_reason": f"provider_executable_preflight_failed:{exc}",
            "artifacts": artifacts,
        }, pretty=args.pretty)
        return 2
    selector = route.get("route", {}).get("executor", {}).get("model_selector")
    if not isinstance(selector, str) or not selector:
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
            "project_binding": binding, "session_guard": guard, "route": route_summary,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
            "block_reason": "provider_model_preflight_failed:codex_task_selector_required",
            "artifacts": artifacts,
        }, pretty=args.pretty)
        return 2
    task_class = route_summary["task_class"]
    reasoning_effort = route.get("route", {}).get("executor", {}).get("reasoning_effort")
    try:
        if selected_provider == "Codex":
            provider_boundary_sha256, provider_runtime_config = _codex_boundary()
            static_resolved_model = None
        elif selected_provider == "Grok":
            assert grok_boundary is not None
            provider_boundary_sha256, provider_runtime_config = grok_boundary
            static_resolved_model = args.grok_model
        else:
            if not isinstance(task_class, str) or not isinstance(reasoning_effort, str):
                raise ProviderAdapterError("local_route_capability_binding_required")
            assert local_config is not None
            provider_boundary_sha256, provider_runtime_config = _local_boundary(
                args, local_config, task_class=task_class,
                reasoning_effort=reasoning_effort, selector=selector,
            )
            static_resolved_model = provider_runtime_config["exact_model_id"]
    except (OSError, ValueError, ProjectBindingError, ProviderAdapterError, HarnessContractError) as exc:
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
            "block_reason": f"provider_boundary_preflight_failed:{exc}",
            "project_binding": binding, "route": route_summary, "artifacts": artifacts,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
        }, pretty=args.pretty)
        return 2
    if args.dry_run:
        _emit({
            "schema": "AgentFanoutPlanV1", "status": "planned",
            "ready_to_launch": selected_provider == "Local", "project_binding": binding,
            "route": route_summary, "cost_projection": route.get("anti_waste_guard"),
            "provider_preflight": {
                "passed": True, "scope": "static_no_process",
                "provider": selected_provider, "model_selector": selector,
                "resolved_model": static_resolved_model,
                "provider_executable_sha256": frozen_executable["executable_sha256"],
                "provider_boundary_sha256": provider_boundary_sha256,
                "model_discovery_required_before_launch": selected_provider in {"Codex", "Grok"},
            },
            "overhead_accepted": bool(args.accept_overhead), "artifacts": artifacts,
            "provider_launches_started": 0, "provider_processes_started": 0,
            "model_discovery_probe_started": False,
            "fallback_allowed": False,
        }, pretty=args.pretty)
        return 0
    try:
        state_dir = _secure_directory(args.state_dir or _state_home())
        run_key = uuid.uuid4().hex
        state_file = _absolute_unresolved(args.run_state_file or state_dir / f"run-{run_key}.json")
        ledger_file = _absolute_unresolved(args.ledger or state_dir / "host.sqlite3")
        _secure_directory(state_file.parent)
        _secure_directory(ledger_file.parent)
        guard_file = state_dir / f"guard-{run_key}.json"
    except (OSError, ProjectBindingError) as exc:
        _emit({"schema": "AgentFanoutRunOutcomeV1", "status": "binding_required", "block_reason": f"state_path_unsafe:{exc}"})
        return 3
    project_root = Path(binding["resolved_root"])
    if any(path == project_root or path_within(path, project_root) for path in (state_dir, state_file, ledger_file, guard_file)):
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
            "block_reason": "state_artifact_inside_project_denied", "project_binding": binding,
        })
        return 4
    try:
        if selected_provider == "Codex":
            discovered_models = discover_codex_models(
                binary, timeout_seconds=args.probe_timeout,
                expected_executable_identity=frozen_executable,
            )
            resolved_model = resolve_codex_model(selector, discovered_models)
            codex_no_tool_runtime_attestation(
                binary, timeout_seconds=args.probe_timeout,
                resolved_model=resolved_model, session_mode="ephemeral",
                expected_executable_identity=frozen_executable,
            )
        elif selected_provider == "Grok":
            discovered_models = discover_grok_models(
                binary, timeout_seconds=args.probe_timeout,
                expected_executable_identity=frozen_executable,
                grok_home=grok_home,
                expected_grok_auth_file_identity=provider_runtime_config["grok_auth_file_identity"],
                expected_grok_helper_identity=provider_runtime_config["grok_http_helper_identity"],
            )
            listed_models = {str(item.get("id")) for item in discovered_models}
            resolved_model = args.grok_model or resolve_grok_model(selector, discovered_models)
            if resolved_model not in listed_models:
                raise ProviderAdapterError("grok_configured_model_not_listed")
            grok_reasoning_effort_mode(resolved_model)
            if reasoning_effort not in grok_supported_reasoning_efforts(resolved_model):
                raise ProviderAdapterError("grok_model_reasoning_effort_unsupported")
            grok_direct_transport_preflight(
                grok_home,
                provider_runtime_config["grok_auth_file_identity"],
                provider_runtime_config["grok_http_helper_identity"],
                timeout_seconds=args.probe_timeout,
            )
        else:
            resolved_model = provider_runtime_config["exact_model_id"]
        verify_executable_identity(binary, frozen_executable or {})
    except ProviderAdapterError as exc:
        reason = str(exc)
        category = (
            "provider_runtime_compatibility_failed"
            if reason.startswith("codex_native_") or reason.startswith("codex_transport_probe_")
            else "provider_model_preflight_failed"
        )
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked", "project_binding": binding,
            "session_guard": guard, "route": route_summary,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
            "block_reason": f"{category}:{reason}", "artifacts": artifacts,
        }, pretty=args.pretty)
        return 2
    if state_file.exists():
        _emit({"schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked", "block_reason": "run_state_file_already_exists"})
        return 4
    try:
        task_units = route.get("task_units")
        if not isinstance(task_units, list) or not task_units:
            raise ValueError("delegable_task_units_required_to_start_run")
        run_state = initialize_run(routed_prompt, task_units)
        run_state["project_binding"] = binding
        save_run_state(state_file, run_state)
    except (OSError, ValueError, ProjectBindingError) as exc:
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked", "project_binding": binding,
            "session_guard": guard, "route": route_summary, "block_reason": f"run_state_create_failed:{exc}",
            "artifacts": artifacts,
        }, pretty=args.pretty)
        return 4
    artifacts.update({"state_file": str(state_file), "ledger_file": str(ledger_file)})
    try:
        guard = create_session_guard(
            binding,
            manifest,
            run_id=run_state["run_id"],
            run_state_path=str(state_file),
            ledger_path=str(ledger_file),
            provider=selected_provider,
            model_selector=selector,
            resolved_model=resolved_model,
            provider_executable_identity=frozen_executable or {},
            max_workers=args.max_workers,
            provider_boundary_sha256=provider_boundary_sha256,
            provider_runtime_config=provider_runtime_config,
        )
        _write_json(guard_file, guard)
        artifacts["guard_file"] = str(guard_file)
    except (OSError, ValueError, ProjectBindingError, HarnessContractError) as exc:
        _emit({
            "schema": "AgentFanoutRunOutcomeV1", "status": "fanout_blocked",
            "block_reason": f"session_guard_persist_failed:{exc}",
            "project_binding": binding, "artifacts": artifacts,
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
        }, pretty=args.pretty)
        return 4
    try:
        with _guard_lock(guard_file, exclusive=True, blocking=True) as lock_fd:
            persisted_guard = _load_guard(guard_file)
            if (
                persisted_guard.get("guard_id") != guard.get("guard_id")
                or persisted_guard.get("guard_binding_sha256") != guard.get("guard_binding_sha256")
                or persisted_guard.get("status") != "armed"
            ):
                raise HarnessContractError("session_guard_changed_before_launch")
            guard = persisted_guard
            provider_options = {
                "binary": binary,
                "grok_home": grok_home,
                "grok_auth_file_identity": provider_runtime_config.get("grok_auth_file_identity"),
                "grok_http_helper_identity": provider_runtime_config.get("grok_http_helper_identity"),
            }
            runner_code, outcome = _run_bound_runner(
                guard, state_file, ledger_file, binding["resolved_root"], provider_options,
                args.max_cycles, environment, lock_fd,
            )
            outcome = _normalize_runner_failure(runner_code, outcome)
            if runner_code == 0 and outcome.get("status") == "complete":
                guard = _guard_transition(
                    guard_file, guard, "released", reason="terminal_authorized"
                )
            else:
                guard = _guard_transition(
                    guard_file,
                    guard,
                    "blocked",
                    reason=str(outcome.get("block_reason") or outcome.get("reason") or "runner_blocked"),
                )
    except (OSError, ValueError, ProjectBindingError, HarnessContractError) as exc:
        outcome = {
            "status": "fanout_blocked",
            "block_reason": f"session_guard_runtime_failed:{exc}",
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
        }
        try:
            guard = _guard_transition(guard_file, guard, "blocked", reason=outcome["block_reason"])
        except Exception:
            guard = {**guard, "release_persistence_error": True}
        runner_code = 4
    _emit({
        "schema": "AgentFanoutRunOutcomeV1", "status": outcome.get("status", "fanout_blocked"),
        "project_binding": binding, "session_guard": guard, "route": route_summary,
        "lifecycle": outcome.get("lifecycle"), "accepted_results": outcome.get("accepted_results", []),
        "validator_counts": outcome.get("validator_counts", {}),
        "enforcement": outcome.get("enforcement", {"level": "runner_enforced", "fallback_allowed": False}),
        "block_reason": outcome.get("block_reason"), "artifacts": artifacts,
        "shutdown_signal": outcome.get("shutdown_signal"),
        "provenance": outcome.get("provenance", {}),
    }, pretty=args.pretty)
    return 0 if runner_code == 0 and outcome.get("status") == "complete" else 4


def _load_guard(path: Path) -> dict[str, Any]:
    return validate_session_guard(_read_json(path))


def _status_fields(state: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for task in state["tasks"]:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return {
        "run_id": state["run_id"], "status": state["status"],
        "wave_index": state["wave_index"], "task_counts": counts, "usage": state["usage"],
        "block_reason": state.get("block_reason"), "project_binding": state.get("project_binding"),
    }


def command_resume(args: argparse.Namespace) -> int:
    guard_file = _absolute_unresolved(args.guard_file)
    try:
        with _guard_lock(guard_file, exclusive=True, blocking=False) as lock_fd:
            guard = _load_guard(guard_file)
            binding = _binding(args)
            manifest = _load_manifest()
            state_path, ledger_path, state = _verify_guard_state_binding(guard, binding, manifest)
            if guard["status"] in {"released", "abandoned"}:
                raise HarnessContractError(f"session_guard_terminal:{guard['status']}")
            environment = os.environ.copy()
            environment["AGENT_FANOUT_PROJECT_ROOT"] = binding["resolved_root"]
            if guard["provider"] == "Codex":
                environment["AGENT_FANOUT_CODEX_SUBAGENTS"] = "1"
            provider_options = _verify_resume_provider(guard, args, state, environment)
            recovery = _recovery_assessment(
                guard, state, ledger_path, orchestrator_active=False
            )
            if state.get("status") == "complete" and recovery["terminal_authorized"] and not recovery["hazards"]:
                guard = _guard_transition(
                    guard_file, guard, "released", reason="terminal_authorized_reconciled"
                )
                _emit({
                    "schema": "AgentFanoutRecoveryOutcomeV1", "status": "complete",
                    "session_guard": guard, "recovery": recovery,
                    "artifacts": {"guard_file": str(guard_file), "state_file": str(state_path), "ledger_file": str(ledger_path)},
                    "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
                }, pretty=args.pretty)
                return 0
            if not recovery["resume_allowed"]:
                reason = (
                    "recovery_unknown_external_effect:" + "|".join(recovery["hazards"])
                    if recovery["hazards"] else f"run_not_resumable:{state.get('status')}"
                )
                guard = _guard_transition(guard_file, guard, "blocked", reason=reason)
                _emit({
                    "schema": "AgentFanoutRecoveryOutcomeV1", "status": "fanout_blocked",
                    "block_reason": reason, "session_guard": guard, "recovery": recovery,
                    "artifacts": {"guard_file": str(guard_file), "state_file": str(state_path), "ledger_file": str(ledger_path)},
                    "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
                }, pretty=args.pretty)
                return 4
            runner_code, outcome = _run_bound_runner(
                guard, state_path, ledger_path, binding["resolved_root"], provider_options,
                args.max_cycles, environment, lock_fd,
            )
            outcome = _normalize_runner_failure(runner_code, outcome)
            if runner_code == 0 and outcome.get("status") == "complete":
                guard = _guard_transition(
                    guard_file, guard, "released", reason="terminal_authorized"
                )
            else:
                guard = _guard_transition(
                    guard_file,
                    guard,
                    "blocked",
                    reason=str(outcome.get("block_reason") or outcome.get("reason") or "runner_blocked"),
                )
            refreshed = load_run_state(state_path)
            post_recovery = _recovery_assessment(
                guard, refreshed, ledger_path, orchestrator_active=False
            )
            _emit({
                "schema": "AgentFanoutRecoveryOutcomeV1",
                "status": outcome.get("status", "fanout_blocked"),
                "block_reason": outcome.get("block_reason") or outcome.get("reason"),
                "session_guard": guard, "recovery": post_recovery,
                "lifecycle": outcome.get("lifecycle"),
                "accepted_results": outcome.get("accepted_results", []),
                "validator_counts": outcome.get("validator_counts", {}),
                "provenance": outcome.get("provenance", {}),
                "artifacts": {"guard_file": str(guard_file), "state_file": str(state_path), "ledger_file": str(ledger_path)},
                "enforcement": outcome.get("enforcement", {"level": "runner_enforced", "fallback_allowed": False}),
            }, pretty=args.pretty)
            return 0 if runner_code == 0 and outcome.get("status") == "complete" else 4
    except (OSError, ValueError, json.JSONDecodeError, ProjectBindingError, HarnessContractError, ProviderAdapterError) as exc:
        _emit({
            "schema": "AgentFanoutRecoveryOutcomeV1", "status": "fanout_blocked",
            "block_reason": f"recovery_binding_invalid:{exc}",
            "artifacts": {"guard_file": str(guard_file)},
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
        }, pretty=args.pretty)
        return 4


def command_abandon(args: argparse.Namespace) -> int:
    guard_file = _absolute_unresolved(args.guard_file)
    try:
        with _guard_lock(guard_file, exclusive=True, blocking=False):
            guard = _load_guard(guard_file)
            binding = _binding(args)
            state_path, ledger_path, state = _verify_guard_state_binding(guard, binding, None)
            if guard["status"] == "abandoned":
                _emit({
                    "schema": "AgentFanoutRecoveryOutcomeV1", "status": "abandoned",
                    "session_guard": guard, "idempotent": True,
                    "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
                }, pretty=args.pretty)
                return 0
            if guard["status"] == "released":
                raise HarnessContractError("session_guard_terminal:released")
            recovery = _recovery_assessment(
                guard, state, ledger_path, orchestrator_active=False
            )
            if state.get("status") == "complete" and recovery["terminal_authorized"] and not recovery["hazards"]:
                guard = _guard_transition(
                    guard_file, guard, "released", reason="terminal_authorized_reconciled"
                )
                raise HarnessContractError("run_already_complete")
            if not recovery["abandon_allowed"]:
                reason = (
                    "abandon_unknown_external_effect:" + "|".join(recovery["hazards"])
                    if recovery["hazards"] else f"run_not_abandonable:{state.get('status')}"
                )
                guard = _guard_transition(guard_file, guard, "blocked", reason=reason)
                _emit({
                    "schema": "AgentFanoutRecoveryOutcomeV1", "status": "fanout_blocked",
                    "block_reason": reason, "session_guard": guard, "recovery": recovery,
                    "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
                }, pretty=args.pretty)
                return 4
            ledger = HostLedger(ledger_path)
            try:
                ledger.append(
                    guard["run_id"],
                    "session_guard_abandoned",
                    {
                        "guard_id": guard["guard_id"],
                        "reason_sha256": hashlib.sha256(args.reason.encode("utf-8")).hexdigest(),
                        "planned_not_launched": recovery["planned_not_launched"],
                        "unknown_external_effects_present": False,
                        "parent_fallback": False,
                        "abandoned_at": _now(),
                    },
                )
            finally:
                ledger.close()
            guard = _guard_transition(
                guard_file, guard, "abandoned", reason=args.reason
            )
            recovery = _recovery_assessment(
                guard, state, ledger_path, orchestrator_active=False
            )
            _emit({
                "schema": "AgentFanoutRecoveryOutcomeV1", "status": "abandoned",
                "session_guard": guard, "recovery": recovery,
                "artifacts": {"guard_file": str(guard_file), "state_file": str(state_path), "ledger_file": str(ledger_path)},
                "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
            }, pretty=args.pretty)
            return 0
    except (OSError, ValueError, json.JSONDecodeError, ProjectBindingError, HarnessContractError) as exc:
        _emit({
            "schema": "AgentFanoutRecoveryOutcomeV1", "status": "fanout_blocked",
            "block_reason": f"abandon_binding_invalid:{exc}",
            "artifacts": {"guard_file": str(guard_file)},
            "enforcement": {"level": "runner_enforced", "fallback_allowed": False},
        }, pretty=args.pretty)
        return 4


def command_status(args: argparse.Namespace) -> int:
    guard_file = _absolute_unresolved(args.guard_file) if args.guard_file else None
    if guard_file is not None:
        try:
            try:
                with _guard_lock(guard_file, exclusive=False, blocking=False):
                    guard = _load_guard(guard_file)
                    state_path, ledger_path, state = _verify_guard_self_binding(guard)
                    recovery = _recovery_assessment(
                        guard, state, ledger_path, orchestrator_active=False
                    )
            except HarnessContractError as exc:
                if str(exc) != "session_guard_orchestrator_active":
                    raise
                guard = _load_guard(guard_file)
                state_path, ledger_path, state = _verify_guard_self_binding(guard)
                recovery = {
                    "orchestrator_process_active": True,
                    "guard_status": guard["status"],
                    "run_status": state.get("status"),
                    "hazards": [],
                    "unknown_external_effects_present": None,
                    "resume_allowed": False,
                    "abandon_allowed": False,
                    "provider_replay_allowed": False,
                    "parent_fallback_allowed": False,
                }
            _emit({
                "schema": "AgentFanoutStatusV1", **_status_fields(state),
                "session_guard": guard, "recovery": recovery,
                "artifacts": {"guard_file": str(guard_file), "state_file": str(state_path), "ledger_file": str(ledger_path)},
                "fallback_allowed": False,
            })
            return 0
        except (OSError, ValueError, json.JSONDecodeError, ProjectBindingError, HarnessContractError) as exc:
            _emit({"schema": "AgentFanoutStatusV1", "status": "unavailable", "reason": str(exc)})
            return 2
    try:
        state = load_run_state(args.run_state_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _emit({"schema": "AgentFanoutStatusV1", "status": "unavailable", "reason": str(exc)})
        return 2
    _emit({
        "schema": "AgentFanoutStatusV1", **_status_fields(state),
        "session_guard": None,
        "recovery": {"status": "guard_file_required_for_recovery_assessment"},
        "fallback_allowed": False,
    })
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "--version", action="version",
        version=(ROOT / "VERSION").read_text(encoding="utf-8").strip(),
    )
    commands = root.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", description="Verify project binding, state-path safety, and exact provider readiness without launching a worker.")
    doctor.add_argument("--execution-profile", choices=["legacy", "coordinator_local", "mixed_provider_runs"], default="legacy")
    _root_arguments(doctor)
    doctor.add_argument("--provider", choices=["Codex", "Grok", "GrokCLI", "Local"], default="Codex")
    doctor.add_argument("--codex-binary", default="codex")
    doctor.add_argument("--grok-binary", default="grok")
    doctor.add_argument("--grok-home", type=Path)
    doctor.add_argument("--grok-model")
    doctor.add_argument("--local-endpoint")
    doctor.add_argument("--local-model")
    doctor.add_argument("--local-selector", default="local-worker")
    doctor.add_argument("--local-mode")
    doctor.add_argument("--local-allow-loopback-http", action="store_true")
    doctor.add_argument("--local-attestation-file", type=Path)
    doctor.add_argument("--local-bridge-file", type=Path)
    doctor.add_argument("--local-task-class", default="bulk-read")
    doctor.add_argument("--local-reasoning-effort", default="low")
    doctor.add_argument("--state-dir", type=Path)
    doctor.add_argument("--selector")
    doctor.add_argument("--binary-only", action="store_true", help="Diagnostic only; can never report ready=true")
    doctor.add_argument("--probe-models", action="store_true", help=argparse.SUPPRESS)
    doctor.add_argument("--probe-timeout", type=float, default=15)
    doctor.set_defaults(handler=command_doctor)

    run = commands.add_parser("run", description="Route and execute a guarded bounded-file fanout, or emit an actionable pre-admission outcome.")
    _root_arguments(run)
    run.add_argument("--provider", choices=["Auto", "Codex", "Grok", "GrokCLI", "Local"], default="Auto")
    run.add_argument("--execution-profile", choices=["legacy", "coordinator_local", "mixed_provider_runs"], default="legacy")
    run.add_argument("--codex-binary", default="codex")
    run.add_argument("--grok-binary", default="grok")
    run.add_argument("--grok-home", type=Path)
    run.add_argument("--grok-model")
    run.add_argument("--local-endpoint")
    run.add_argument("--local-model")
    run.add_argument("--local-selector")
    run.add_argument("--local-mode")
    run.add_argument("--local-allow-loopback-http", action="store_true")
    run.add_argument("--local-attestation-file", type=Path)
    run.add_argument("--local-bridge-file", type=Path)
    run.add_argument("--state-dir", type=Path)
    run.add_argument("--run-state-file", type=Path)
    run.add_argument("--ledger", type=Path)
    run.add_argument("--max-workers", type=int, default=4)
    run.add_argument("--max-cycles", type=int, default=100)
    run.add_argument("--probe-timeout", type=float, default=15)
    run.add_argument("--accept-overhead", action="store_true", help="Explicitly permit fanout when the anti-waste projection prefers direct execution")
    run.add_argument("--dry-run", action="store_true", help="Route and preflight without creating state, guard, ledger, or worker launches")
    run.add_argument("--pretty", action="store_true")
    run.add_argument("prompt", nargs=argparse.REMAINDER)
    run.set_defaults(handler=command_run)

    resume = commands.add_parser(
        "resume",
        description=(
            "Resume an interrupted guarded run only with its original project, executable, model, "
            "and provider boundary. Grok requires the same isolated-home inode/model override; "
            "Local requires unchanged, still-fresh attestation and bridge evidence."
        ),
    )
    _root_arguments(resume)
    resume.add_argument("--guard-file", type=Path, required=True)
    resume.add_argument("--codex-binary", default="codex")
    resume.add_argument("--grok-binary", default="grok")
    resume.add_argument("--grok-home", type=Path, help="Original dedicated owner-only Grok home; replacement directories are denied")
    resume.add_argument("--grok-model", help="Repeat the original exact override when the run used one")
    resume.add_argument("--local-endpoint", help="Original exact Local endpoint")
    resume.add_argument("--local-model", help="Original exact loaded Local model ID")
    resume.add_argument("--local-selector", help="Original Local routing selector")
    resume.add_argument("--local-mode", help="Original active Local mode")
    resume.add_argument("--local-allow-loopback-http", action="store_true")
    resume.add_argument("--local-attestation-file", type=Path, help="Original unchanged and still-fresh host attestation")
    resume.add_argument("--local-bridge-file", type=Path, help="Original unchanged and still-fresh bridge observation")
    resume.add_argument("--max-cycles", type=int, default=100)
    resume.add_argument("--probe-timeout", type=float, default=15)
    resume.add_argument("--pretty", action="store_true")
    resume.set_defaults(handler=command_resume)

    abandon = commands.add_parser("abandon")
    _root_arguments(abandon)
    abandon.add_argument("--guard-file", type=Path, required=True)
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--pretty", action="store_true")
    abandon.set_defaults(handler=command_abandon)

    identity = commands.add_parser("identity", help="Report release authority, invoked code and feature fingerprints")
    identity.set_defaults(handler=lambda args: (_emit(build_report(ROOT, Path(__file__))) or 0))

    status = commands.add_parser("status")
    status_source = status.add_mutually_exclusive_group(required=True)
    status_source.add_argument("--run-state-file", type=Path)
    status_source.add_argument("--guard-file", type=Path)
    status.set_defaults(handler=command_status)
    return root


def main() -> int:
    args = parser().parse_args()
    prior_sigterm = signal.getsignal(signal.SIGTERM)

    def _interrupt(signum: int, _frame: Any) -> None:
        raise _ForwardedProcessSignal(signum)

    signal.signal(signal.SIGTERM, _interrupt)
    try:
        return int(args.handler(args))
    except (KeyboardInterrupt, _ForwardedProcessSignal) as exc:
        signum = signal.SIGINT if isinstance(exc, KeyboardInterrupt) else exc.signum
        _emit({
            "schema": "AgentFanoutInterruptedV1",
            "status": "fanout_blocked",
            "block_reason": f"operator_signal:{signal.Signals(signum).name}",
            "termination_scope": "active_child_process_group_reaped_or_not_started",
            "fallback_allowed": False,
        })
        return 4
    finally:
        signal.signal(signal.SIGTERM, prior_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
