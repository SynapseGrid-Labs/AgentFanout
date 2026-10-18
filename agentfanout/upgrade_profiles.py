"""Explicit upgrade vocabulary; feature switches never establish capability."""
from __future__ import annotations

from typing import Any

CONTROLS = {"grok_cli_subscription", "coordinator_local", "mixed_provider_runs",
            "native_grok_profile", "retained_sessions", "tool_capable_workers"}
MODES = {"GrokCLI": "grok_cli_subscription_acp", "Local": "coordinator_local"}


def validate_controls(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != CONTROLS or any(type(v) is not bool for v in value.values()):
        raise ValueError("upgrade_feature_controls_invalid")
    if value["retained_sessions"] or value["tool_capable_workers"]:
        raise ValueError("deferred_upgrade_feature_unavailable")
    return dict(value)


def capability_status() -> dict[str, Any]:
    # No environment variable, registry override or caller-authored receipt can
    # substitute for installed-binary conformance or the coordinator contract.
    return {"schema": "UpgradeCapabilityStatusV1", "native_default": True,
            "controls": {name: False for name in sorted(CONTROLS)},
            "lanes": {
                "GrokCLI": {"mode": MODES["GrokCLI"], "admitted": False,
                            "reason": "grok_cli_installed_boundary_unproved"},
                "coordinator_local": {"mode": MODES["Local"], "admitted": False,
                                      "reason": "coordinator_exact_resource_contract_unproved"},
                "mixed": {"admitted": False, "reason": "mixed_live_conformance_pending"}},
            "old_provider_meanings_preserved": True, "fallback_allowed": False}


def refuse_unconformed_provider(provider: str) -> None:
    if provider == "GrokCLI":
        raise ValueError("grok_cli_installed_boundary_unproved")
