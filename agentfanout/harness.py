"""Cross-harness enforcement declarations and conformance checks.

This module never assumes that an outer agent harness obeys prompt text.  It
distinguishes advisory policy from the standalone runner boundary and from a
host integration that can prove mandatory control of native worker tools.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class HarnessContractError(ValueError):
    """A host manifest or conformance trace overclaims its enforcement."""


ENFORCEMENT_LEVELS = {"advisory", "runner_enforced", "harness_enforced"}
NATIVE_CONTROLS = {"absent", "hard_denied", "mediated", "detect_only", "unknown"}
SESSION_GUARD_STATUSES = {"armed", "blocked", "released", "abandoned"}
SESSION_GUARD_IMMUTABLE_FIELDS = (
    "schema",
    "guard_id",
    "run_id",
    "project_binding_sha256",
    "host_manifest_sha256",
    "run_state_path",
    "run_state_path_sha256",
    "ledger_path",
    "ledger_path_sha256",
    "provider",
    "model_selector",
    "resolved_model",
    "provider_executable_sha256",
    "provider_executable_identity",
    "provider_boundary_sha256",
    "provider_runtime_config",
    "max_workers",
    "enforcement_level",
    "native_worker_tool_control",
    "armed_at",
    "fallback_allowed",
)
MAX_MANIFEST_CLOCK_SKEW_SECONDS = 300
MAX_MANIFEST_LIFETIME_SECONDS = {
    "advisory": 366 * 24 * 60 * 60,
    "runner_enforced": 366 * 24 * 60 * 60,
    "harness_enforced": 24 * 60 * 60,
}
WORKER_TOOL_ALIASES = {
    "spawn_agent", "followup_task", "send_message", "task",
    "collaboration.spawn_agent", "collaboration.followup_task",
    "multi_agent_v1__spawn_agent", "multi_agent_v1__send_input",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise HarnessContractError("harness_manifest_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HarnessContractError("harness_manifest_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise HarnessContractError("harness_manifest_timestamp_invalid")
    return parsed


def manifest_digest(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def trace_digest(events: list[dict[str, Any]]) -> str:
    """Return a content digest; authenticity still requires an operator pin."""

    encoded = json.dumps(events, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_manifest(
    manifest: dict[str, Any], *, trusted_authorities: Iterable[str] = (),
    trusted_manifest_digests: Iterable[str] = (), require_level: str | None = None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema") != "HostCapabilityManifestV1":
        raise HarnessContractError("host_capability_manifest_required")
    harness = manifest.get("harness")
    enforcement = manifest.get("enforcement")
    if not isinstance(harness, dict) or not all(isinstance(harness.get(key), str) and harness[key] for key in ("name", "version")):
        raise HarnessContractError("harness_identity_invalid")
    if not isinstance(enforcement, dict):
        raise HarnessContractError("harness_enforcement_invalid")
    level = enforcement.get("level")
    control = enforcement.get("native_worker_tool_control")
    if level not in ENFORCEMENT_LEVELS or control not in NATIVE_CONTROLS:
        raise HarnessContractError("harness_enforcement_invalid")
    if enforcement.get("unknown_tool_policy") not in {"deny", "detect", "allow"}:
        raise HarnessContractError("harness_unknown_tool_policy_invalid")
    anti_bypass = control in {"absent", "hard_denied", "mediated"}
    if enforcement.get("anti_bypass_certified") is True:
        raise HarnessContractError("anti_bypass_certification_requires_external_verifier")
    if level in {"runner_enforced", "harness_enforced"} and not anti_bypass:
        raise HarnessContractError("enforced_native_tool_control_insufficient")
    if level in {"runner_enforced", "harness_enforced"} and enforcement.get("unknown_tool_policy") != "deny":
        raise HarnessContractError("enforced_unknown_tool_policy_must_deny")
    if level in {"runner_enforced", "harness_enforced"} and enforcement.get("enforcement_contract_declared") is not True:
        raise HarnessContractError("enforced_contract_declaration_required")
    native_inventory = enforcement.get("native_worker_tools")
    if level in {"runner_enforced", "harness_enforced"}:
        if not isinstance(native_inventory, list) or not all(isinstance(item, str) and item for item in native_inventory):
            raise HarnessContractError("native_worker_tool_inventory_required")
        if len(native_inventory) != len(set(native_inventory)):
            raise HarnessContractError("native_worker_tool_inventory_duplicate")
        if control != "absent" and not native_inventory:
            raise HarnessContractError("native_worker_tool_inventory_empty")
        if control == "absent" and native_inventory:
            raise HarnessContractError("absent_native_worker_tool_inventory_nonempty")
    if level == "harness_enforced":
        attestation = manifest.get("attestation")
        trusted = set(trusted_authorities)
        if not isinstance(attestation, dict) or attestation.get("authority") not in trusted:
            raise HarnessContractError("harness_attestation_untrusted")
        if manifest_digest(manifest) not in set(trusted_manifest_digests):
            raise HarnessContractError("harness_manifest_digest_untrusted")
    adapters = manifest.get("provider_adapters")
    if not isinstance(adapters, list) or not all(isinstance(item, dict) for item in adapters):
        raise HarnessContractError("provider_adapter_manifest_invalid")
    if level in {"runner_enforced", "harness_enforced"} and not adapters:
        raise HarnessContractError("provider_adapter_manifest_required")
    providers: set[str] = set()
    adapter_ids: set[str] = set()
    for adapter in adapters:
        required = ("provider", "adapter_id", "version", "input_modes", "usage_proof", "cancellation")
        if not all(adapter.get(key) not in (None, "", []) for key in required):
            raise HarnessContractError("provider_adapter_manifest_invalid")
        provider, adapter_id, input_modes = adapter["provider"], adapter["adapter_id"], adapter["input_modes"]
        if not isinstance(provider, str) or not isinstance(adapter_id, str):
            raise HarnessContractError("provider_adapter_manifest_invalid")
        if provider in providers or adapter_id in adapter_ids:
            raise HarnessContractError("provider_adapter_manifest_duplicate")
        if (
            not isinstance(input_modes, list)
            or not all(isinstance(mode, str) and mode for mode in input_modes)
            or len(input_modes) != len(set(input_modes))
        ):
            raise HarnessContractError("provider_adapter_manifest_invalid")
        providers.add(provider)
        adapter_ids.add(adapter_id)
    issued = _parse_time(manifest.get("issued_at"))
    now = datetime.now(timezone.utc)
    if issued.timestamp() > now.timestamp() + MAX_MANIFEST_CLOCK_SKEW_SECONDS:
        raise HarnessContractError("harness_manifest_issued_in_future")
    lifetime_policy = manifest.get("lifetime_policy", "time_bound")
    if lifetime_policy == "release_bound":
        if level != "runner_enforced" or manifest.get("expires_at") is not None:
            raise HarnessContractError("release_bound_manifest_invalid")
    elif lifetime_policy == "time_bound":
        expires = _parse_time(manifest.get("expires_at"))
        lifetime = (expires - issued).total_seconds()
        if expires <= issued or expires <= now:
            raise HarnessContractError("harness_manifest_expired")
        if lifetime > MAX_MANIFEST_LIFETIME_SECONDS[level]:
            raise HarnessContractError("harness_manifest_lifetime_invalid")
    else:
        raise HarnessContractError("harness_manifest_lifetime_policy_invalid")
    if manifest.get("manifest_sha256") not in {None, manifest_digest(manifest)}:
        raise HarnessContractError("harness_manifest_digest_mismatch")
    if require_level is not None and level != require_level:
        raise HarnessContractError(f"enforcement_level_required:{require_level}")
    return manifest


def classify_native_action(tool_name: str) -> str:
    normalized = tool_name.strip().lower()
    if normalized in WORKER_TOOL_ALIASES or normalized.endswith("__spawn_agent") or normalized.endswith(".spawn_agent"):
        return "worker_spawn"
    if normalized.endswith("__send_input") or normalized.endswith(".followup_task"):
        return "worker_followup"
    return "unknown_tool"


def authorize_native_action(
    manifest: dict[str, Any], tool_name: str, *, guard_active: bool, delegable_residual: bool,
    trusted_authorities: Iterable[str] = (), trusted_manifest_digests: Iterable[str] = (),
) -> dict[str, Any]:
    validate_manifest(
        manifest, trusted_authorities=trusted_authorities,
        trusted_manifest_digests=trusted_manifest_digests,
    )
    action = classify_native_action(tool_name)
    control = manifest["enforcement"]["native_worker_tool_control"]
    denied = bool(guard_active and delegable_residual and action in {"worker_spawn", "worker_followup"})
    if denied and control not in {"absent", "hard_denied", "mediated"}:
        return {"authorized": False, "certified": False, "contract_enforced": False, "reason": "native_tool_surface_not_enforceable", "action": action}
    if denied:
        return {"authorized": False, "certified": False, "contract_enforced": True, "reason": "native_worker_action_must_use_host_adapter", "action": action}
    if action == "unknown_tool" and manifest["enforcement"]["unknown_tool_policy"] == "deny":
        return {"authorized": False, "certified": False, "contract_enforced": control in {"absent", "hard_denied", "mediated"}, "reason": "unknown_tool_denied", "action": action}
    return {"authorized": True, "certified": False, "contract_enforced": control in {"absent", "hard_denied", "mediated"}, "reason": "allowed", "action": action}


def session_guard_binding_digest(guard: dict[str, Any]) -> str:
    """Digest the immutable guard fields.

    This detects accidental or partial mutation.  It is not a signature and
    does not protect against the account that owns both the guard and runtime
    evidence; stronger authenticity needs a separately trusted signer.
    """

    payload = {key: guard.get(key) for key in SESSION_GUARD_IMMUTABLE_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_session_guard(guard: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(guard, dict) or guard.get("schema") != "SessionGuardStateV1":
        raise HarnessContractError("session_guard_schema_invalid")
    required_strings = (
        "guard_id", "run_id", "project_binding_sha256", "host_manifest_sha256",
        "run_state_path", "run_state_path_sha256", "ledger_path", "ledger_path_sha256",
        "provider", "model_selector", "resolved_model", "provider_executable_sha256",
        "provider_boundary_sha256", "armed_at", "guard_binding_sha256",
    )
    if any(not isinstance(guard.get(key), str) or not guard[key] for key in required_strings):
        raise HarnessContractError("session_guard_binding_invalid")
    allowed = set(SESSION_GUARD_IMMUTABLE_FIELDS) | {
        "status", "guard_binding_sha256", "blocked_at", "blocked_reason",
        "released_at", "released_reason", "abandoned_at",
        "abandoned_reason_sha256", "release_persistence_error",
    }
    if set(guard) - allowed:
        raise HarnessContractError("session_guard_unknown_field")
    sha_fields = (
        "project_binding_sha256", "host_manifest_sha256", "run_state_path_sha256",
        "ledger_path_sha256", "provider_executable_sha256", "guard_binding_sha256",
        "provider_boundary_sha256",
    )
    if any(len(guard[key]) != 64 or any(char not in "0123456789abcdef" for char in guard[key]) for key in sha_fields):
        raise HarnessContractError("session_guard_digest_invalid")
    if (
        not guard["guard_id"].startswith("guard_")
        or len(guard["guard_id"]) != 38
        or any(char not in "0123456789abcdef" for char in guard["guard_id"][6:])
    ):
        raise HarnessContractError("session_guard_id_invalid")
    if (
        not guard["run_id"].startswith("run_")
        or len(guard["run_id"]) != 36
        or any(char not in "0123456789abcdef" for char in guard["run_id"][4:])
    ):
        raise HarnessContractError("session_guard_run_id_invalid")
    if guard.get("status") not in SESSION_GUARD_STATUSES:
        raise HarnessContractError("session_guard_status_invalid")
    transition_requirements = {
        "blocked": ("blocked_at", "blocked_reason"),
        "released": ("released_at", "released_reason"),
        "abandoned": ("abandoned_at", "abandoned_reason_sha256"),
    }
    if any(
        not isinstance(guard.get(key), str) or not guard[key]
        for key in transition_requirements.get(guard["status"], ())
    ):
        raise HarnessContractError("session_guard_transition_evidence_missing")
    if guard.get("enforcement_level") not in ENFORCEMENT_LEVELS or guard.get("native_worker_tool_control") not in NATIVE_CONTROLS:
        raise HarnessContractError("session_guard_enforcement_invalid")
    if guard.get("fallback_allowed") is not False:
        raise HarnessContractError("session_guard_fallback_invalid")
    if not Path(guard["run_state_path"]).is_absolute() or not Path(guard["ledger_path"]).is_absolute():
        raise HarnessContractError("session_guard_path_not_absolute")
    if isinstance(guard.get("max_workers"), bool) or not isinstance(guard.get("max_workers"), int) or guard["max_workers"] < 1:
        raise HarnessContractError("session_guard_worker_limit_invalid")
    executable = guard.get("provider_executable_identity")
    executable_fields = (
        "executable_realpath", "executable_sha256", "executable_bytes",
        "executable_device", "executable_inode", "launch_argv_sha256",
        "argument_file_identities", "launcher_chain_identities",
        "codex_native_executable_identity", "codex_wrapper_argv_index",
    )
    if not isinstance(executable, dict) or any(key not in executable for key in executable_fields):
        raise HarnessContractError("session_guard_executable_identity_invalid")
    if executable.get("executable_sha256") != guard["provider_executable_sha256"]:
        raise HarnessContractError("session_guard_executable_digest_mismatch")
    if (
        not isinstance(executable.get("argument_file_identities"), list)
        or not isinstance(executable.get("launcher_chain_identities"), list)
        or (
            executable.get("codex_native_executable_identity") is not None
            and not isinstance(executable.get("codex_native_executable_identity"), dict)
        )
        or (
            executable.get("codex_wrapper_argv_index") is not None
            and (
                isinstance(executable.get("codex_wrapper_argv_index"), bool)
                or not isinstance(executable.get("codex_wrapper_argv_index"), int)
                or executable["codex_wrapper_argv_index"] < 0
            )
        )
    ):
        raise HarnessContractError("session_guard_executable_identity_invalid")
    runtime_config = guard.get("provider_runtime_config")
    if not isinstance(runtime_config, dict):
        raise HarnessContractError("session_guard_provider_runtime_config_invalid")
    if guard["provider"] == "Codex":
        if runtime_config:
            raise HarnessContractError("session_guard_codex_runtime_config_invalid")
    elif guard["provider"] == "Grok":
        required = {
            "grok_home_path_sha256", "grok_home_device", "grok_home_inode",
            "grok_model_override", "grok_auth_file_identity",
            "grok_http_helper_identity",
        }
        if set(runtime_config) != required:
            raise HarnessContractError("session_guard_grok_runtime_config_invalid")
        path_sha = runtime_config.get("grok_home_path_sha256")
        model_override = runtime_config.get("grok_model_override")
        auth_identity = runtime_config.get("grok_auth_file_identity")
        helper_identity = runtime_config.get("grok_http_helper_identity")
        auth_sha_fields = ("auth_file_path_sha256", "auth_file_sha256")
        auth_integer_fields = (
            "auth_file_bytes", "auth_file_device", "auth_file_inode",
            "auth_file_mode", "auth_file_uid",
        )
        if (
            not isinstance(path_sha, str)
            or len(path_sha) != 64
            or any(char not in "0123456789abcdef" for char in path_sha)
            or any(
                isinstance(runtime_config.get(key), bool)
                or not isinstance(runtime_config.get(key), int)
                or runtime_config[key] < 0
                for key in ("grok_home_device", "grok_home_inode")
            )
            or (model_override is not None and (not isinstance(model_override, str) or not model_override))
            or not isinstance(auth_identity, dict)
            or set(auth_identity) != {"schema", *auth_sha_fields, *auth_integer_fields}
            or auth_identity.get("schema") != "GrokAuthFileIdentityV1"
            or any(
                not isinstance(auth_identity.get(key), str)
                or len(auth_identity[key]) != 64
                or any(char not in "0123456789abcdef" for char in auth_identity[key])
                for key in auth_sha_fields
            )
            or any(
                isinstance(auth_identity.get(key), bool)
                or not isinstance(auth_identity.get(key), int)
                or auth_identity[key] < 0
                for key in auth_integer_fields
            )
            or not isinstance(helper_identity, dict)
            or any(key not in helper_identity for key in executable_fields)
            or not isinstance(helper_identity.get("executable_sha256"), str)
            or len(helper_identity["executable_sha256"]) != 64
            or any(
                char not in "0123456789abcdef"
                for char in helper_identity["executable_sha256"]
            )
            or not isinstance(helper_identity.get("argument_file_identities"), list)
            or not isinstance(helper_identity.get("launcher_chain_identities"), list)
        ):
            raise HarnessContractError("session_guard_grok_runtime_config_invalid")
    elif guard["provider"] == "Local":
        required = {
            "endpoint_sha256", "endpoint_policy", "allow_loopback_http",
            "selector", "exact_model_id", "required_mode",
            "attestation_sha256", "bridge_observation_sha256",
        }
        if set(runtime_config) != required:
            raise HarnessContractError("session_guard_local_runtime_config_invalid")
        local_sha_fields = ("endpoint_sha256", "attestation_sha256", "bridge_observation_sha256")
        if (
            any(
                not isinstance(runtime_config.get(key), str)
                or len(runtime_config[key]) != 64
                or any(char not in "0123456789abcdef" for char in runtime_config[key])
                for key in local_sha_fields
            )
            or runtime_config.get("endpoint_policy") not in {"numeric_loopback_http", "https"}
            or not isinstance(runtime_config.get("allow_loopback_http"), bool)
            or any(
                not isinstance(runtime_config.get(key), str) or not runtime_config[key]
                for key in ("selector", "exact_model_id", "required_mode")
            )
        ):
            raise HarnessContractError("session_guard_local_runtime_config_invalid")
    elif runtime_config:
        raise HarnessContractError("session_guard_provider_runtime_config_unsupported")
    if session_guard_binding_digest(guard) != guard["guard_binding_sha256"]:
        raise HarnessContractError("session_guard_binding_digest_mismatch")
    return guard


def create_session_guard(
    binding: dict[str, Any],
    manifest: dict[str, Any],
    *,
    run_id: str,
    run_state_path: str,
    ledger_path: str,
    provider: str,
    model_selector: str,
    resolved_model: str,
    provider_executable_identity: dict[str, Any],
    max_workers: int,
    provider_boundary_sha256: str | None = None,
    provider_runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_manifest(manifest)
    runtime_config = dict(provider_runtime_config or {})
    if provider_boundary_sha256 is None:
        provider_boundary_sha256 = hashlib.sha256(json.dumps(
            {"provider": provider, "runtime_config": runtime_config},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode()).hexdigest()
    guard = {
        "schema": "SessionGuardStateV1",
        "guard_id": "guard_" + uuid.uuid4().hex,
        "run_id": run_id,
        "status": "armed",
        "project_binding_sha256": binding.get("root_sha256"),
        "host_manifest_sha256": manifest_digest(manifest),
        "run_state_path": run_state_path,
        "run_state_path_sha256": hashlib.sha256(run_state_path.encode()).hexdigest(),
        "ledger_path": ledger_path,
        "ledger_path_sha256": hashlib.sha256(ledger_path.encode()).hexdigest(),
        "provider": provider,
        "model_selector": model_selector,
        "resolved_model": resolved_model,
        "provider_executable_sha256": provider_executable_identity.get("executable_sha256"),
        "provider_executable_identity": provider_executable_identity,
        "provider_boundary_sha256": provider_boundary_sha256,
        "provider_runtime_config": runtime_config,
        "max_workers": max_workers,
        "enforcement_level": manifest["enforcement"]["level"],
        "native_worker_tool_control": manifest["enforcement"]["native_worker_tool_control"],
        "armed_at": _now(),
        "fallback_allowed": False,
    }
    guard["guard_binding_sha256"] = session_guard_binding_digest(guard)
    return validate_session_guard(guard)


def check_conformance(
    manifest: dict[str, Any], events: list[dict[str, Any]], *, trusted_authorities: Iterable[str] = (),
    trusted_manifest_digests: Iterable[str] = (), trusted_trace_digests: Iterable[str] = (),
    require_level: str | None = None,
) -> dict[str, Any]:
    validate_manifest(
        manifest, trusted_authorities=trusted_authorities,
        trusted_manifest_digests=trusted_manifest_digests, require_level=require_level,
    )
    failures: list[str] = []
    digest = trace_digest(events)
    if manifest["enforcement"]["level"] == "harness_enforced" and digest not in set(trusted_trace_digests):
        failures.append("trace:operator_pinned_digest_required")
    identities = {
        key: {event.get(key) for event in events if isinstance(event, dict)}
        for key in ("run_id", "guard_id", "trace_nonce")
    }
    for key, values in identities.items():
        if len(values) != 1 or None in values or "" in values:
            failures.append(f"trace:{key}_binding_invalid")
    sequences = [event.get("sequence") for event in events if isinstance(event, dict)]
    if (
        len(sequences) != len(events)
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in sequences)
        or sequences != list(range(1, len(events) + 1))
    ):
        failures.append("trace:sequence_invalid")
    event_types = {event.get("type") for event in events if isinstance(event, dict)}
    if "session_guard_armed" not in event_types:
        failures.append("trace:session_guard_armed_missing")
    if not ({"terminal_transition", "run_blocked"} & event_types):
        failures.append("trace:terminal_or_blocked_outcome_missing")
    inventory_events = [
        event for event in events
        if isinstance(event, dict) and event.get("type") == "native_tool_inventory" and event.get("complete") is True
    ]
    expected_inventory = set(manifest["enforcement"].get("native_worker_tools", []))
    if not inventory_events:
        failures.append("trace:complete_native_tool_inventory_missing")
    elif set(inventory_events[-1].get("tools", [])) != expected_inventory:
        failures.append("trace:native_tool_inventory_mismatch")
    known_denial = any(
        event.get("type") == "native_tool_attempt"
        and classify_native_action(str(event.get("tool", ""))) in {"worker_spawn", "worker_followup"}
        and event.get("guard_active") is True and event.get("delegable_residual") is True
        and event.get("authorized") is False
        for event in events if isinstance(event, dict)
    )
    unknown_denial = any(
        event.get("type") == "native_tool_attempt"
        and classify_native_action(str(event.get("tool", ""))) == "unknown_tool"
        and event.get("guard_active") is True and event.get("delegable_residual") is True
        and event.get("authorized") is False
        for event in events if isinstance(event, dict)
    )
    residual_terminal_denial = any(
        event.get("type") == "terminal_transition"
        and event.get("delegable_residual") is True and event.get("authorized") is False
        for event in events if isinstance(event, dict)
    )
    approved_providers = {adapter["provider"] for adapter in manifest["provider_adapters"]}
    unavailable_no_parent = any(
        event.get("type") == "worker_unavailable"
        and event.get("fallback_allowed") is False
        and (event.get("fallback_target") is None or event.get("fallback_target") in approved_providers)
        for event in events if isinstance(event, dict)
    )
    post_batch_replan = any(
        event.get("type") == "result_batch_replanned"
        and isinstance(event.get("residual_before"), int) and event.get("residual_before") > 0
        and event.get("action") in {"dispatch_wave", "follow_up"}
        for event in events if isinstance(event, dict)
    )
    def validated_wave(items: list[dict[str, Any]], wave_id: int) -> bool:
        launches = [item for item in items if item.get("type") == "worker_launch" and item.get("wave_id") == wave_id]
        for launch in launches:
            provider, model = launch.get("provider"), launch.get("model")
            task_id, launch_id, worker_id = launch.get("task_id"), launch.get("launch_id"), launch.get("worker_id")
            if (
                provider not in approved_providers or not all(isinstance(value, str) and value for value in (model, task_id, launch_id, worker_id))
            ):
                continue
            result = next((
                item for item in items
                if item.get("type") == "worker_result" and item.get("wave_id") == wave_id
                and item.get("task_id") == task_id and item.get("launch_id") == launch_id
                and item.get("worker_id") == worker_id and item.get("provider") == provider
                and item.get("model") == model
            ), None)
            validation = next((
                item for item in items
                if item.get("type") == "validator_result" and item.get("wave_id") == wave_id
                and item.get("task_id") == task_id and item.get("validated_launch_id") == launch_id
                and item.get("worker_id") == worker_id and item.get("verdict") == "passed"
                and item.get("validator_provider") in approved_providers
                and isinstance(item.get("validator_model"), str) and item.get("validator_model")
                and isinstance(item.get("validator_id"), str) and item.get("validator_id") != worker_id
            ), None)
            if result is not None and validation is not None:
                return True
        return False

    happy_path = False
    for replan_index, event in enumerate(events):
        if not (
            isinstance(event, dict) and event.get("type") == "result_batch_replanned"
            and event.get("action") in {"dispatch_wave", "follow_up"}
            and isinstance(event.get("wave_id"), int) and event.get("wave_id") >= 1
        ):
            continue
        first_wave = event["wave_id"]
        before, after = events[:replan_index], events[replan_index + 1:]
        second_wave = first_wave + 1
        terminal = [item for item in after if item.get("type") == "terminal_transition" and item.get("delegable_residual") is False and item.get("authorized") is True]
        if validated_wave(before, first_wave) and validated_wave(after, second_wave) and terminal:
            happy_path = True
            break
    for passed, name in (
        (known_denial, "native_worker_bypass_denied"),
        (unknown_denial, "unknown_tool_denied"),
        (residual_terminal_denial, "terminal_residual_denied"),
        (unavailable_no_parent, "worker_unavailable_no_parent_fallback"),
        (post_batch_replan, "post_batch_replan"),
        (happy_path, "two_wave_validated_happy_path"),
    ):
        if not passed:
            failures.append(f"trace:required_scenario_missing:{name}")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            failures.append(f"event_{index}:invalid")
            continue
        if event.get("type") == "native_tool_attempt":
            decision = authorize_native_action(
                manifest, str(event.get("tool", "")),
                guard_active=event.get("guard_active") is True,
                delegable_residual=event.get("delegable_residual") is True,
                trusted_authorities=trusted_authorities,
                trusted_manifest_digests=trusted_manifest_digests,
            )
            if event.get("authorized") != decision["authorized"]:
                failures.append(f"event_{index}:native_tool_decision_mismatch")
        if event.get("type") == "terminal_transition" and event.get("delegable_residual") is True and event.get("authorized") is True:
            failures.append(f"event_{index}:terminal_with_delegable_residual")
        if event.get("type") == "worker_unavailable":
            target = event.get("fallback_target")
            if target in {"parent", "main_session"}:
                failures.append(f"event_{index}:silent_parent_fallback")
            elif target is not None and target not in approved_providers:
                failures.append(f"event_{index}:unapproved_provider_fallback")
    return {
        "schema": "HarnessConformanceResultV1",
        "passed": not failures,
        "enforcement_level": manifest["enforcement"]["level"],
        "manifest_sha256": manifest_digest(manifest),
        "trace_sha256": digest,
        "trace_assurance": "operator_pinned" if digest in set(trusted_trace_digests) else "content_digest_only",
        "events_checked": len(events),
        "failures": failures,
    }
