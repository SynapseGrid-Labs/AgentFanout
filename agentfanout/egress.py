"""Host-owned destination authorization for bounded hosted packets.

A worker cannot supply its own policy. Callers must carry the policy frozen by
host admission through results, validators, errors, telemetry and synthesis.
Destination labels are exact host bindings, not claims made by model output.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Iterable

from .project_binding import bounded_content_is_sensitive


class EgressError(ValueError):
    """A packet has no authorized, safe destination."""


CATEGORIES = (
    "input", "worker_result", "peer_evidence", "artifact", "error", "residual",
    "telemetry", "retained_session", "validator_input", "parent_synthesis",
)
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
CLOUD_PROVIDERS = frozenset({"GrokCLI", "Grok", "Codex", "Claude", "Gemini"})
MAX_PACKET_BYTES = 1024 * 1024


def validate_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict) or set(policy) != {"schema", "destinations", "allowed"} or policy.get("schema") != "EvidenceEgressPolicyV1":
        raise EgressError("unsupported_egress_policy")
    destinations = policy["destinations"]
    if not isinstance(destinations, dict) or not 1 <= len(destinations) <= 64:
        raise EgressError("invalid_egress_destinations")
    for destination_id, destination in destinations.items():
        if not isinstance(destination_id, str) or not _ID.fullmatch(destination_id):
            raise EgressError("invalid_egress_destination_id")
        if not isinstance(destination, dict) or set(destination) != {"kind", "provider"}:
            raise EgressError("invalid_egress_destination")
        kind, provider = destination["kind"], destination["provider"]
        if not isinstance(provider, str) or not (
            (kind == "host" and provider == "Host") or
            (kind == "local" and provider == "Local") or
            (kind == "cloud" and provider in CLOUD_PROVIDERS)
        ):
            raise EgressError("egress_destination_provider_mismatch")
    allowed = policy["allowed"]
    if not isinstance(allowed, dict) or set(allowed) != set(CATEGORIES):
        raise EgressError("egress_categories_incomplete")
    for values in allowed.values():
        if not isinstance(values, list) or len(values) > 64 or any(not isinstance(value, str) for value in values):
            raise EgressError("invalid_egress_allowlist")
        if len(set(values)) != len(values) or any(value not in destinations for value in values):
            raise EgressError("invalid_egress_allowlist")
    return policy


def destination_allowed(policy: dict[str, Any], category: str, destination_id: str) -> bool:
    validate_policy(policy)
    if category not in CATEGORIES:
        raise EgressError("unknown_egress_category")
    return destination_id in policy["allowed"][category]


def authorize_packet(policy: dict[str, Any], category: str, destination_id: str, payload: Any) -> Any:
    """Check *before* serialization to a provider, log, parent or validator.

    Secret screening applies even for local destinations. This does not grant
    file access or declassify data: artifacts are bounded JSON/text descriptors,
    and approved summaries need their own host-created policy.
    """
    if not destination_allowed(policy, category, destination_id):
        raise EgressError("egress_destination_denied")
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise EgressError("egress_packet_not_bounded_json") from exc
    if size > MAX_PACKET_BYTES:
        raise EgressError("egress_packet_too_large")
    # Scan string leaves too: JSON escaping must not hide PEM or line-based
    # credentials from the existing project secret screen.
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, str) and bounded_content_is_sensitive(value):
            raise EgressError("egress_sensitive_content")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise EgressError("egress_packet_not_bounded_json")
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise EgressError("egress_packet_not_bounded_json")
    if bounded_content_is_sensitive(encoded):
        raise EgressError("egress_sensitive_content")
    return deepcopy(payload)


def filter_peer_evidence(peers: Iterable[tuple[dict[str, Any], Any]], destination_id: str) -> list[Any]:
    """Filter by each source task's policy before cross-provider validation.

    Denied packets leave no text, identifiers or refusal reason in the result.
    Invalid policies and unsafe allowed packets fail closed rather than being
    silently reclassified. Peers are policy/payload pairs assembled by the host.
    """
    result = []
    for policy, payload in peers:
        if destination_allowed(policy, "peer_evidence", destination_id):
            result.append(authorize_packet(policy, "peer_evidence", destination_id, payload))
    return result
