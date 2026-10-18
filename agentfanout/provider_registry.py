"""Data-driven provider/model profiles and cost-decision telemetry."""

from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "providers" / "models.json"


def _reject_nonfinite_json_constant(_value: str) -> None:
    raise ValueError("provider_registry_numeric_non_finite")


def _finite_positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_provider_registry_numeric:{field}")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid_provider_registry_numeric:{field}") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"invalid_provider_registry_numeric:{field}")
    return normalized


def _finite_positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_provider_registry_numeric:{field}")
    try:
        finite = math.isfinite(float(value))
    except OverflowError as exc:
        raise ValueError(f"invalid_provider_registry_numeric:{field}") from exc
    if not finite:
        raise ValueError(f"invalid_provider_registry_numeric:{field}")
    return value


def _validate_registry(data: Any) -> dict[str, Any]:
    """Validate routing-critical registry numerics at every trust boundary.

    Callers may inject an already-decoded registry directly, bypassing
    :func:`load_registry`, so public use sites invoke this validator too.
    Missing, zero, negative, NaN, or infinite cost/latency data must never be
    converted into a cheap candidate or an anti-waste comparison result.
    """

    if not isinstance(data, dict) or data.get("schema") != "ProviderRegistryV1" or not isinstance(data.get("providers"), dict):
        raise ValueError("invalid_provider_registry")
    _finite_positive_integer(data.get("version"), "version")
    for provider, config in data["providers"].items():
        if not isinstance(provider, str) or not provider or not isinstance(config, dict):
            raise ValueError("invalid_provider_registry")
        overhead = config.get("launch_overhead_input_tokens")
        if overhead is not None:
            _finite_positive_integer(
                overhead, f"{provider}:launch_overhead_input_tokens",
            )
        profiles = config.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("invalid_provider_registry")
        for profile, profile_config in profiles.items():
            if not isinstance(profile, str) or not profile or not isinstance(profile_config, dict):
                raise ValueError("invalid_provider_registry")
            _finite_positive_number(
                profile_config.get("relative_cost"), f"{provider}:{profile}:relative_cost",
            )
            _finite_positive_number(
                profile_config.get("relative_latency"), f"{provider}:{profile}:relative_latency",
            )
            if "explicit_only" in profile_config and not isinstance(profile_config["explicit_only"], bool):
                raise ValueError(f"invalid_provider_registry_profile_policy:{provider}:{profile}:explicit_only")
            if "max_parallel_workers" in profile_config:
                _finite_positive_integer(
                    profile_config["max_parallel_workers"],
                    f"{provider}:{profile}:max_parallel_workers",
                )
    return data


def _registry_data(registry: dict[str, Any] | None) -> dict[str, Any]:
    return load_registry() if registry is None else _validate_registry(registry)


def _profile_numeric(
    profile_config: dict[str, Any], provider: str, profile: str, field: str,
) -> float:
    return _finite_positive_number(
        profile_config.get(field), f"{provider}:{profile}:{field}",
    )


def load_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path or os.getenv("AGENT_FANOUT_PROVIDER_REGISTRY") or DEFAULT_REGISTRY).expanduser()
    data = json.loads(
        registry_path.read_text(encoding="utf-8"),
        parse_constant=_reject_nonfinite_json_constant,
    )
    return _validate_registry(data)


def normalize_reasoning(value: str, registry: dict[str, Any] | None = None) -> str:
    data = _registry_data(registry)
    normalized = re.sub(r"\s+", " ", value.strip().lower().replace("_", " "))
    aliases = data.get("reasoning_aliases", {})
    return str(aliases.get(normalized, aliases.get(normalized.replace(" ", "-"), normalized)))


def requested_model_alias(prompt: str, registry: dict[str, Any] | None = None) -> dict[str, str] | None:
    data = _registry_data(registry)
    for alias, target in data.get("requested_model_aliases", {}).items():
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", re.IGNORECASE)
        for match in pattern.finditer(prompt):
            prefix = prompt[max(0, match.start() - 64):match.start()]
            if re.search(
                r"\b(?:do\s+not|don't|never|avoid|without|no)\s+"
                r"(?:(?:use|select|route\s+to|launch|allow)\s+)?$",
                prefix,
                re.IGNORECASE,
            ):
                continue
            return {"alias": alias, "provider": target["provider"], "profile": target["profile"]}
    return None


def provider_config(provider: str, registry: dict[str, Any] | None = None) -> dict[str, Any] | None:
    data = _registry_data(registry)
    config = data.get("providers", {}).get(provider)
    return deepcopy(config) if isinstance(config, dict) else None


def provider_enabled(provider: str, registry: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    config = provider_config(provider, registry)
    if not config:
        return False, "provider_not_registered"
    if provider == "GrokCLI":
        # Generic disabled-provider overrides cannot bypass the subscription
        # binary/auth/no-tool/accounting conformance gate.
        return False, "grok_cli_installed_boundary_unproved"
    if config.get("retired") and not os.getenv("AGENT_FANOUT_ENABLE_RETIRED_PROVIDERS", "").lower() in {"1", "true", "yes"}:
        return False, "provider_retired"
    if not config.get("enabled", False) and not os.getenv("AGENT_FANOUT_ENABLE_DISABLED_PROVIDERS", "").lower() in {"1", "true", "yes"}:
        return False, "provider_disabled"
    return True, None


def profile_for(
    provider: str,
    task_class: str,
    effort: str,
    requested_alias: dict[str, str] | None = None,
    registry: dict[str, Any] | None = None,
) -> str:
    data = _registry_data(registry)
    if requested_alias and requested_alias.get("provider") == provider:
        return requested_alias["profile"]
    config = provider_config(provider, data)
    profiles = config.get("profiles", {}) if config else {}
    capable: list[tuple[float, float, str]] = []
    for profile, profile_config in profiles.items():
        if not isinstance(profile_config, dict):
            continue
        if profile_config.get("explicit_only") and not (
            requested_alias and requested_alias.get("profile") == profile
        ):
            continue
        accepted, _ = profile_capable(provider, profile, task_class, effort, data)
        if accepted:
            capable.append((
                _profile_numeric(profile_config, provider, profile, "relative_cost"),
                _profile_numeric(profile_config, provider, profile, "relative_latency"),
                profile,
            ))
    if capable:
        return min(capable)[2]
    # Preserve a deterministic registered profile so readiness can return the
    # real capability reason instead of failing model resolution first.
    required_risk = risk_for(task_class, effort, data)
    fallback = "frontier" if required_risk in {"critical", "high"} else "balanced" if required_risk == "medium" else "fast"
    return fallback if fallback in profiles else next(iter(profiles), fallback)


def resolve_model(
    provider: str,
    profile: str,
    requested_alias: dict[str, str] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _registry_data(registry)
    config = provider_config(provider, data)
    if not config:
        return {
            "provider": provider,
            "profile": profile,
            "selector": None,
            "requested_alias": requested_alias.get("alias") if requested_alias else None,
            "resolved_model": None,
            "ack_required": True,
            "error": "provider_not_registered",
        }
    profile_config = config.get("profiles", {}).get(profile)
    if not isinstance(profile_config, dict):
        raise ValueError(f"profile_not_registered:{provider}:{profile}")
    resolved_env = profile_config.get("resolved_model_env")
    resolved_model = os.getenv(resolved_env, "").strip() if resolved_env else ""
    return {
        "provider": provider,
        "profile": profile,
        "selector": profile_config.get("selector"),
        "requested_alias": requested_alias.get("alias") if requested_alias else None,
        "selection_reason": (
            requested_alias.get("selection_reason")
            if requested_alias and requested_alias.get("selection_reason")
            else f"explicit_model_alias:{requested_alias.get('alias')}"
            if requested_alias
            else "registry_lowest_eligible_profile"
        ),
        "resolved_model": resolved_model or None,
        "resolved_model_source": f"env:{resolved_env}" if resolved_model else "host_launch_ack_required",
        "ack_required": True,
        "relative_cost": _profile_numeric(profile_config, provider, profile, "relative_cost"),
        "relative_latency": _profile_numeric(profile_config, provider, profile, "relative_latency"),
        "max_risk": profile_config.get("max_risk"),
        "capabilities": list(profile_config.get("capabilities", [])),
        "task_classes": list(profile_config.get("task_classes", [])),
        "reasoning_efforts": list(profile_config.get("reasoning_efforts", [])),
        "requires_recent_eval": bool(profile_config.get("requires_recent_eval", False)),
        "explicit_only": bool(profile_config.get("explicit_only", False)),
        "required_model_id": profile_config.get("required_model_id"),
        "forbidden_model_ids": list(profile_config.get("forbidden_model_ids", [])),
        "max_parallel_workers": profile_config.get("max_parallel_workers"),
    }


RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def risk_for(
    task_class: str, effort: str, registry: dict[str, Any] | None = None,
) -> str:
    # Task risk and requested reasoning effort are independent dimensions. A
    # simple bounded read can legitimately use Luna+high; that does not turn it
    # into a critical-risk task. Model capability is driven by task class while
    # each profile separately declares the efforts its exact model supports.
    normalize_reasoning(effort, registry)
    if task_class == "security-review":
        return "critical"
    if task_class in {"plan", "reason", "refactor"}:
        return "high"
    if task_class in {"debug", "test-gen", "data-xform"}:
        return "medium"
    return "low"


def profile_capable(provider: str, profile: str, task_class: str, effort: str, registry: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    data = _registry_data(registry)
    config = provider_config(provider, data)
    if not config:
        return False, "provider_not_registered"
    profile_config = config.get("profiles", {}).get(profile)
    if not isinstance(profile_config, dict):
        return False, "profile_not_registered"
    required_risk = risk_for(task_class, effort, data)
    maximum_risk = str(profile_config.get("max_risk", "low"))
    if RISK_ORDER.get(maximum_risk, -1) < RISK_ORDER[required_risk]:
        return False, f"risk_exceeds_profile:{required_risk}>{maximum_risk}"
    task_classes = profile_config.get("task_classes", [])
    if not isinstance(task_classes, list) or ("*" not in task_classes and task_class not in task_classes):
        return False, f"task_class_not_supported:{task_class}"
    efforts = profile_config.get("reasoning_efforts", [])
    normalized_effort = normalize_reasoning(effort, data)
    if not isinstance(efforts, list) or normalized_effort not in efforts:
        return False, f"reasoning_effort_not_supported:{normalized_effort}"
    return True, None


def ranked_provider_options(
    task_class: str,
    effort: str,
    registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return enabled providers sorted by cost, then latency, then registry tie order."""

    data = _registry_data(registry)
    tie_order = {name: index for index, name in enumerate(data.get("provider_tie_break_order", []))}
    options: list[dict[str, Any]] = []
    for provider in data.get("providers", {}):
        enabled, _ = provider_enabled(provider, data)
        if not enabled:
            continue
        profile = profile_for(provider, task_class, effort, None, data)
        capable, reason = profile_capable(provider, profile, task_class, effort, data)
        if not capable:
            continue
        resolution = resolve_model(provider, profile, None, data)
        options.append({
            "provider": provider,
            "profile": profile,
            "selector": resolution.get("selector") or "",
            "reasoning_effort": normalize_reasoning(effort, data),
            "relative_cost": _finite_positive_number(
                resolution.get("relative_cost"), f"{provider}:{profile}:relative_cost",
            ),
            "relative_latency": _finite_positive_number(
                resolution.get("relative_latency"), f"{provider}:{profile}:relative_latency",
            ),
            "capability_reason": reason,
        })
    options.sort(key=lambda item: (
        item["relative_cost"], item["relative_latency"],
        tie_order.get(item["provider"], len(tie_order)), item["provider"],
    ))
    return options


def decision_telemetry(
    prompt: str,
    provider: str,
    task_class: str,
    effort: str,
    requested_units: int,
    planned_workers: int,
    requested_alias: dict[str, str] | None = None,
    validator_required: bool | None = None,
    unit_input_tokens: list[int] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _registry_data(registry)
    profile = profile_for(provider, task_class, effort, requested_alias, data) if provider != "main-session" else "main"
    resolution = resolve_model(provider, profile, requested_alias, data) if provider != "main-session" else {
        "provider": "main-session", "profile": "main", "selector": "main-session", "relative_cost": 1.0
    }
    prompt_input_tokens = max(1, math.ceil(len(prompt) / 4))
    normalized_unit_tokens = list(unit_input_tokens or [])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in normalized_unit_tokens):
        raise ValueError("unit_input_tokens_invalid")
    if len(normalized_unit_tokens) < requested_units:
        normalized_unit_tokens.extend([0] * (requested_units - len(normalized_unit_tokens)))
    normalized_unit_tokens = normalized_unit_tokens[:requested_units]
    wave_unit_tokens = normalized_unit_tokens[:planned_workers]
    bounded_tokens_this_wave = sum(wave_unit_tokens)
    bounded_tokens_request = sum(normalized_unit_tokens)
    estimated_payload_input_tokens = prompt_input_tokens + (
        math.ceil(bounded_tokens_this_wave / planned_workers)
        if planned_workers else 0
    )
    output_defaults = {
        "bulk-classify": 350,
        "bulk-read": 650,
        "creative": 700,
        "data-xform": 850,
        "debug": 1000,
        "test-gen": 1100,
        "refactor": 1400,
        "plan": 1200,
        "security-review": 1300,
        "reason": 1200,
    }
    main_input_defaults_per_unit = {
        "bulk-classify": 500,
        "bulk-read": 4_000,
        "creative": 1_000,
        "data-xform": 2_000,
        "debug": 4_000,
        "test-gen": 3_000,
        "refactor": 5_000,
        "plan": 3_000,
        "security-review": 5_000,
        "reason": 3_000,
    }
    estimated_output_tokens = output_defaults.get(task_class, 700)
    relative_cost = _finite_positive_number(
        resolution.get("relative_cost"), f"{provider}:{profile}:relative_cost",
    )
    selected_provider = str(resolution.get("provider", provider))
    provider_settings = provider_config(selected_provider, data) if selected_provider != "main-session" else None
    raw_launch_overhead = provider_settings.get("launch_overhead_input_tokens") if provider_settings else None
    if raw_launch_overhead is None:
        # Missing overhead is never treated as free. Runtime usage remains the
        # authority; this is conservative review telemetry for routing policy.
        launch_overhead_tokens = 15_000 if selected_provider != "main-session" else 0
        launch_overhead_source = "unknown_conservative_default"
    else:
        launch_overhead_tokens = _finite_positive_integer(
            raw_launch_overhead,
            f"{selected_provider}:launch_overhead_input_tokens",
        )
        launch_overhead_source = str(provider_settings.get("launch_overhead_source") or "registry") if provider_settings else "main_session_sunk"
    if validator_required is None:
        validator_required = (
            selected_provider != "main-session"
            and (
                task_class not in {"bulk-classify", "data-xform", "creative"}
                or normalize_reasoning(effort, data) in {"high", "xhigh", "ultra"}
            )
        )
    validator_calls_per_worker = 1 if validator_required and planned_workers else 0
    estimated_input_tokens = estimated_payload_input_tokens + launch_overhead_tokens
    average_wave_unit_tokens = (
        math.ceil(bounded_tokens_this_wave / planned_workers)
        if planned_workers else 0
    )
    estimated_validator_input_tokens = (
        launch_overhead_tokens + prompt_input_tokens + average_wave_unit_tokens
        + min(estimated_output_tokens, 1_400)
    )
    estimated_total_provider_calls = planned_workers * (1 + validator_calls_per_worker)
    estimated_orchestration_input_tokens = sum(
        launch_overhead_tokens + prompt_input_tokens + unit_tokens
        + validator_calls_per_worker * (
            launch_overhead_tokens + prompt_input_tokens + unit_tokens
            + min(estimated_output_tokens, 1_400)
        )
        for unit_tokens in wave_unit_tokens
    )
    estimated_orchestration_output_tokens = planned_workers * (
        estimated_output_tokens + validator_calls_per_worker * 350
    )
    estimated_total_provider_calls_request = requested_units * (1 + validator_calls_per_worker)
    estimated_orchestration_input_tokens_request = sum(
        launch_overhead_tokens + prompt_input_tokens + unit_tokens
        + validator_calls_per_worker * (
            launch_overhead_tokens + prompt_input_tokens + unit_tokens
            + min(estimated_output_tokens, 1_400)
        )
        for unit_tokens in normalized_unit_tokens
    )
    estimated_orchestration_output_tokens_request = requested_units * (
        estimated_output_tokens + validator_calls_per_worker * 350
    )
    estimated_relative_cost_units = round(
        (estimated_orchestration_input_tokens + estimated_orchestration_output_tokens) / 1000 * relative_cost,
        4,
    )
    estimated_relative_cost_units_request = round(
        (estimated_orchestration_input_tokens_request + estimated_orchestration_output_tokens_request) / 1000 * relative_cost,
        4,
    )
    main_default = main_input_defaults_per_unit.get(task_class, 1_000)
    main_input_tokens_per_unit = max(
        prompt_input_tokens + (
            math.ceil(bounded_tokens_request / requested_units)
            if requested_units else 0
        ),
        main_default,
    )
    main_session_baseline_tokens = sum(
        max(main_default, prompt_input_tokens + unit_tokens) + estimated_output_tokens
        for unit_tokens in normalized_unit_tokens
    )
    main_session_baseline_units = round(main_session_baseline_tokens / 1000, 4)
    payload = {
        "schema": "DecisionTelemetryV1",
        "task_class": task_class,
        "risk": risk_for(task_class, effort, data),
        "reasoning_effort": normalize_reasoning(effort, data),
        "requested_units": requested_units,
        "planned_workers_this_wave": planned_workers,
        "estimated_payload_input_tokens_per_worker": estimated_payload_input_tokens,
        "estimated_prompt_input_tokens_per_worker": prompt_input_tokens,
        "estimated_bounded_input_tokens_this_wave": bounded_tokens_this_wave,
        "estimated_bounded_input_tokens_request": bounded_tokens_request,
        "estimated_launch_overhead_input_tokens_per_call": launch_overhead_tokens,
        "launch_overhead_source": launch_overhead_source,
        "estimated_input_tokens_per_worker": estimated_input_tokens,
        "estimated_output_tokens_per_worker": estimated_output_tokens,
        "estimated_validator_calls_per_worker": validator_calls_per_worker,
        "estimated_total_provider_calls_this_wave": estimated_total_provider_calls,
        "estimated_total_provider_calls_request": estimated_total_provider_calls_request,
        "estimated_orchestration_input_tokens_this_wave": estimated_orchestration_input_tokens,
        "estimated_orchestration_input_tokens_request": estimated_orchestration_input_tokens_request,
        "estimated_relative_cost_units_this_wave": estimated_relative_cost_units,
        "estimated_relative_cost_units_request": estimated_relative_cost_units_request,
        "estimated_main_session_input_tokens_per_unit": main_input_tokens_per_unit,
        "estimated_main_session_baseline_units": main_session_baseline_units,
        "estimated_orchestration_overhead_dominates_baseline": (
            estimated_relative_cost_units_request > main_session_baseline_units
        ),
        "estimated_savings_requires_runtime_usage_ack": True,
        "model_resolution": resolution,
        "cost_objective": "minimize_total_usage_subject_to_capability_safety_and_validation",
    }
    # Keep this telemetry valid RFC 8259 JSON even when a future arithmetic
    # field is added without an explicit finite-value guard.
    json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return payload
