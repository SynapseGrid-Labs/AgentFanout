"""Pinned, no-tool Grok HTTPS inference helper.

The hosted Grok adapter launches this file under ``python -I -S`` in an empty
temporary directory.  It receives one bounded request over stdin, reads one
prebound auth-only JSON file, and contacts only the fixed xAI Chat Completions
endpoint.  The request never contains a ``tools`` field, redirects and ambient
proxies are disabled, and only a compact validated response is returned.

This module intentionally has no package-relative imports so isolated Python
can execute it directly without importing user or site packages.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ssl
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


GROK_CHAT_COMPLETIONS_ENDPOINT = "https://api.x.ai/v1/chat/completions"
MAX_REQUEST_BYTES = 500_000
MAX_RESPONSE_BYTES = 2_000_000
MAX_EVENT_BYTES = 256_000
MAX_EVENT_COUNT = 10_000
MAX_AUTH_BYTES = 512_000
MODEL_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
REASONING_MODEL = re.compile(r"^grok-4\.5$")
EFFORTS = {"low", "medium", "high"}
OUTPUT_CONTRACTS = {"WorkerResultV1", "ValidatorResultV1"}
SYSTEM_PROMPT = (
    "Use only the supplied user text. Return only the requested JSON. "
    "No tools, functions, files, browsing, memory, or external actions are available."
)


def _string_schema(*, maximum: int = 2_048, minimum: int = 1) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _string_array_schema(*, maximum_items: int, minimum_items: int = 0) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _string_schema(),
        "minItems": minimum_items,
        "maxItems": maximum_items,
    }


def _structured_output_schema(contract: str) -> dict[str, Any]:
    """Return the only two provider-output contracts this helper accepts.

    The schemas stay inside the pinned helper instead of accepting arbitrary
    caller-supplied JSON Schema.  That prevents an outer harness from weakening
    the packet boundary while still letting the host select worker or validator
    output explicitly.
    """

    if contract == "WorkerResultV1":
        variants = []
        for status in ("success", "partial", "failed", "blocked"):
            properties = {
                "schema": {"type": "string", "const": "WorkerResultV1"},
                "result_id": _string_schema(maximum=200),
                "task_id": _string_schema(maximum=200),
                "status": {"type": "string", "const": status},
                "summary": _string_schema(),
                "evidence": _string_array_schema(
                    maximum_items=32,
                    minimum_items=1 if status == "success" else 0,
                ),
                "changed_files": _string_array_schema(maximum_items=0),
                "commands": _string_array_schema(maximum_items=0),
                "exit_codes": {
                    "type": "array", "items": {"type": "integer"},
                    "minItems": 0, "maxItems": 0,
                },
                "remaining_work": _string_array_schema(
                    maximum_items=16,
                    minimum_items=1 if status == "partial" else 0,
                ),
                "launch_correlation_id": _string_schema(maximum=200),
            }
            variants.append({
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            })
        return {"anyOf": variants}
    if contract == "ValidatorResultV1":
        contradiction = {
            "type": "object",
            "properties": {
                "claim": _string_schema(),
                "task_ids": _string_array_schema(maximum_items=32, minimum_items=2),
                "evidence_refs": _string_array_schema(maximum_items=32, minimum_items=1),
                "resolution_scope": _string_schema(),
            },
            "required": ["claim", "task_ids", "evidence_refs", "resolution_scope"],
            "additionalProperties": False,
        }
        variants = []
        for verdict in ("pass", "concerns", "fail"):
            properties = {
                "schema": {"type": "string", "const": "ValidatorResultV1"},
                "validator_id": _string_schema(maximum=200),
                "verdict": {"type": "string", "const": verdict},
                "evidence_refs": _string_array_schema(maximum_items=32, minimum_items=1),
                "material_findings": _string_array_schema(maximum_items=32),
                "contradictions": {
                    "type": "array", "items": contradiction,
                    "minItems": 0, "maxItems": 0 if verdict == "pass" else 16,
                },
                "checked_at": {"type": "string", "format": "date-time"},
            }
            variants.append({
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            })
        return {"anyOf": variants}
    raise GrokHttpError("grok_http_output_contract_invalid")


def _reject_output_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GrokHttpError("grok_http_output_duplicate_key")
        value[key] = item
    return value


def _is_nonempty_string(value: Any, maximum: int = 2_048) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _is_string_array(
    value: Any, *, maximum_items: int, minimum_items: int = 0,
) -> bool:
    return (
        isinstance(value, list)
        and minimum_items <= len(value) <= maximum_items
        and all(_is_nonempty_string(item) for item in value)
    )


def _validate_structured_content(content: str, contract: str) -> None:
    """Independently verify the provider's strict-schema claim before return."""

    try:
        value = json.loads(
            content,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_output_duplicate_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GrokHttpError("grok_http_output_json_invalid") from exc
    if not isinstance(value, dict):
        raise GrokHttpError("grok_http_output_contract_invalid")
    if contract == "WorkerResultV1":
        required = {
            "schema", "result_id", "task_id", "status", "summary", "evidence",
            "changed_files", "commands", "exit_codes", "remaining_work",
            "launch_correlation_id",
        }
        if set(value) != required:
            raise GrokHttpError("grok_http_worker_output_fields_invalid")
        if value.get("schema") != contract:
            raise GrokHttpError("grok_http_worker_output_schema_invalid")
        if value.get("status") not in {"success", "partial", "failed", "blocked"}:
            raise GrokHttpError("grok_http_worker_output_status_invalid")
        for field in ("result_id", "task_id", "launch_correlation_id"):
            if not _is_nonempty_string(value.get(field), 200):
                raise GrokHttpError(f"grok_http_worker_output_{field}_invalid")
        if not _is_nonempty_string(value.get("summary")):
            raise GrokHttpError("grok_http_worker_output_summary_invalid")
        if not _is_string_array(value.get("evidence"), maximum_items=32):
            raise GrokHttpError("grok_http_worker_output_evidence_invalid")
        if any(value.get(field) != [] for field in ("changed_files", "commands", "exit_codes")):
            raise GrokHttpError("grok_http_worker_output_tool_claim_invalid")
        if not _is_string_array(value.get("remaining_work"), maximum_items=16):
            raise GrokHttpError("grok_http_worker_output_remaining_work_invalid")
        if value.get("status") == "success" and not value.get("evidence"):
            raise GrokHttpError("grok_http_worker_output_success_evidence_required")
        if value.get("status") == "partial" and not value.get("remaining_work"):
            raise GrokHttpError("grok_http_worker_output_partial_residual_required")
        return
    if contract == "ValidatorResultV1":
        required = {
            "schema", "validator_id", "verdict", "evidence_refs",
            "material_findings", "contradictions", "checked_at",
        }
        contradictions = value.get("contradictions")
        if set(value) != required:
            raise GrokHttpError("grok_http_validator_output_fields_invalid")
        if value.get("schema") != contract:
            raise GrokHttpError("grok_http_validator_output_schema_invalid")
        if value.get("verdict") not in {"pass", "concerns", "fail"}:
            raise GrokHttpError("grok_http_validator_output_verdict_invalid")
        if not _is_nonempty_string(value.get("validator_id"), 200):
            raise GrokHttpError("grok_http_validator_output_id_invalid")
        if not _is_string_array(
            value.get("evidence_refs"), maximum_items=32, minimum_items=1,
        ):
            raise GrokHttpError("grok_http_validator_output_evidence_invalid")
        if not _is_string_array(value.get("material_findings"), maximum_items=32):
            raise GrokHttpError("grok_http_validator_output_findings_invalid")
        if not isinstance(contradictions, list) or len(contradictions) > 16:
            raise GrokHttpError("grok_http_validator_output_contradictions_invalid")
        if value.get("verdict") == "pass" and contradictions:
            raise GrokHttpError("grok_http_validator_output_pass_contradiction_invalid")
        if not _is_nonempty_string(value.get("checked_at"), 200):
            raise GrokHttpError("grok_http_validator_output_checked_at_invalid")
        try:
            checked_at = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise GrokHttpError("grok_http_validator_output_checked_at_invalid") from exc
        if checked_at.tzinfo is None:
            raise GrokHttpError("grok_http_validator_output_checked_at_invalid")
        for item in contradictions:
            if (
                not isinstance(item, dict)
                or set(item) != {"claim", "task_ids", "evidence_refs", "resolution_scope"}
                or not _is_nonempty_string(item.get("claim"))
                or not _is_string_array(item.get("task_ids"), maximum_items=32, minimum_items=2)
                or not _is_string_array(item.get("evidence_refs"), maximum_items=32, minimum_items=1)
                or not _is_nonempty_string(item.get("resolution_scope"))
            ):
                raise GrokHttpError("grok_http_validator_output_contradiction_item_invalid")
        return
    raise GrokHttpError("grok_http_output_contract_invalid")


class GrokHttpError(ValueError):
    """The fixed Grok transport or its response violated the contract."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise GrokHttpError("grok_http_redirect_denied")


def _reject_json_constant(_value: str) -> None:
    raise GrokHttpError("grok_http_nonfinite_json_denied")


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GrokHttpError("grok_http_auth_duplicate_key")
        value[key] = item
    return value


def _bounded_integer(value: Any, reason: str, maximum: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise GrokHttpError(reason)
    return value


def _read_auth_token(path_value: str, expected_sha256: str) -> str:
    if not isinstance(path_value, str) or not path_value or "\x00" in path_value:
        raise GrokHttpError("grok_http_auth_path_invalid")
    path = Path(path_value)
    if not path.is_absolute():
        raise GrokHttpError("grok_http_auth_path_invalid")
    try:
        before = path.lstat()
    except OSError as exc:
        raise GrokHttpError("grok_http_auth_unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o077
        or before.st_size <= 0
        or before.st_size > MAX_AUTH_BYTES
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
    ):
        raise GrokHttpError("grok_http_auth_security_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GrokHttpError("grok_http_auth_unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or opened.st_nlink != 1
            or opened.st_uid != before.st_uid
            or opened.st_mode != before.st_mode
        ):
            raise GrokHttpError("grok_http_auth_identity_changed")
        payload = bytearray()
        while len(payload) <= MAX_AUTH_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_AUTH_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.close(descriptor)
    try:
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if (
            not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
            or actual_sha256 != expected_sha256
            or len(payload) != before.st_size
        ):
            raise GrokHttpError("grok_http_auth_identity_changed")
        try:
            document = json.loads(
                payload,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GrokHttpError("grok_http_auth_json_invalid") from exc
    finally:
        for index in range(len(payload)):
            payload[index] = 0
    # This is AgentFanout's dedicated credential envelope, not the official
    # CLI's session store. Never replay an OIDC/subscription token to the API.
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "api_key"}
        or document.get("schema") != "GrokApiCredentialV1"
    ):
        raise GrokHttpError("grok_http_api_credential_required")
    if not isinstance(document.get("api_key"), str):
        raise GrokHttpError("grok_http_auth_token_invalid")
    token = document["api_key"]
    if (
        not 32 <= len(token) <= 16_384
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        raise GrokHttpError("grok_http_auth_token_invalid")
    return token


def _usage_packet(
    value: Any, maximum_output_tokens: int, *, require_source_proof: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GrokHttpError("grok_http_usage_missing")
    prompt_tokens = _bounded_integer(value.get("prompt_tokens"), "grok_http_usage_invalid")
    completion_tokens = _bounded_integer(
        value.get("completion_tokens"), "grok_http_usage_invalid",
    )
    total_tokens = _bounded_integer(value.get("total_tokens"), "grok_http_usage_invalid")
    completion_details = value.get("completion_tokens_details", {})
    if completion_details is None:
        completion_details = {}
    if not isinstance(completion_details, dict):
        raise GrokHttpError("grok_http_usage_invalid")
    reasoning_tokens = _bounded_integer(
        completion_details.get("reasoning_tokens", 0), "grok_http_usage_invalid",
    )
    billed_output_tokens = completion_tokens + reasoning_tokens
    if (
        total_tokens != prompt_tokens + billed_output_tokens
        or billed_output_tokens > maximum_output_tokens
    ):
        raise GrokHttpError("grok_http_usage_invalid")
    prompt_details = value.get("prompt_tokens_details", {})
    if prompt_details is None:
        prompt_details = {}
    if not isinstance(prompt_details, dict):
        raise GrokHttpError("grok_http_usage_invalid")
    cached_tokens = _bounded_integer(
        prompt_details.get("cached_tokens", 0), "grok_http_usage_invalid",
    )
    if cached_tokens > prompt_tokens:
        raise GrokHttpError("grok_http_usage_invalid")
    if require_source_proof and "num_sources_used" not in value:
        raise GrokHttpError("grok_http_usage_invalid")
    if "num_sources_used" in value:
        sources = value.get("num_sources_used")
        if _bounded_integer(sources, "grok_http_usage_invalid") != 0:
            raise GrokHttpError("grok_http_external_sources_forbidden")
    if "num_server_side_tools_used" in value and _bounded_integer(
        value.get("num_server_side_tools_used"), "grok_http_usage_invalid",
    ) != 0:
        raise GrokHttpError("grok_http_external_sources_forbidden")
    for tool_usage_field in (
        "server_side_tool_usage", "server_side_tool_usage_details", "tool_usage",
    ):
        if value.get(tool_usage_field) not in (None, {}, []):
            raise GrokHttpError("grok_http_external_sources_forbidden")
    ticks = value.get("cost_in_usd_ticks")
    if ticks is not None:
        _bounded_integer(ticks, "grok_http_usage_invalid", maximum=10**18)
    return {
        "input_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "uncached_input_tokens": prompt_tokens - cached_tokens,
        "output_tokens": billed_output_tokens,
        "visible_output_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "usage_verified": True,
        # The provider documents a tick field but not a stable public tick-to-
        # USD conversion here.  The host therefore charges its conservative
        # reservation instead of inventing a measured dollar amount.
        "cost_usd": None,
        "cost_verified": False,
        "provider_cost_ticks_present": ticks is not None,
    }


def _read_sse_response(
    response: Any, model: str, maximum_output_tokens: int, deadline: float,
) -> dict[str, Any]:
    content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    if content_type != "text/event-stream":
        raise GrokHttpError("grok_http_content_type_invalid")
    observed_models: set[str] = set()
    response_ids: set[str] = set()
    output: list[str] = []
    output_bytes = 0
    response_bytes = 0
    event_count = 0
    finish_reasons: list[str] = []
    usage_values: list[tuple[int, dict[str, Any]]] = []
    finish_event_index: int | None = None
    done_seen = False
    while True:
        # HTTPResponse iteration calls unbounded readline(), so a valid TLS
        # peer could otherwise force allocation of an arbitrarily large line
        # before the post-read size check. Always cap the socket read itself.
        raw_line = response.readline(MAX_EVENT_BYTES + 1)
        if not raw_line:
            break
        if time.monotonic() > deadline:
            raise GrokHttpError("grok_http_stream_timeout")
        response_bytes += len(raw_line)
        if response_bytes > MAX_RESPONSE_BYTES or len(raw_line) > MAX_EVENT_BYTES:
            raise GrokHttpError("grok_http_response_too_large")
        line = raw_line.strip()
        if not line or line.startswith(b":"):
            continue
        if not line.startswith(b"data:"):
            raise GrokHttpError("grok_http_sse_field_invalid")
        data = line[5:].strip()
        if data == b"[DONE]":
            if done_seen:
                raise GrokHttpError("grok_http_done_duplicated")
            done_seen = True
            continue
        if done_seen:
            raise GrokHttpError("grok_http_data_after_done")
        try:
            event = json.loads(data, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GrokHttpError("grok_http_event_json_invalid") from exc
        if not isinstance(event, dict):
            raise GrokHttpError("grok_http_event_shape_invalid")
        event_count += 1
        if event_count > MAX_EVENT_COUNT:
            raise GrokHttpError("grok_http_event_count_exceeded")
        if (
            event.get("tool_calls") not in (None, [])
            or event.get("function_call") is not None
        ):
            raise GrokHttpError("grok_http_tool_call_forbidden")
        for source_field in ("citations", "sources", "search_results"):
            if event.get(source_field) not in (None, [], {}):
                raise GrokHttpError("grok_http_external_sources_forbidden")
        observed_model = event.get("model")
        if observed_model != model:
            raise GrokHttpError("grok_http_response_model_mismatch")
        observed_models.add(observed_model)
        response_id = event.get("id")
        if not isinstance(response_id, str) or not response_id or len(response_id) > 300:
            raise GrokHttpError("grok_http_response_id_invalid")
        response_ids.add(response_id)
        if isinstance(event.get("usage"), dict):
            usage_values.append((event_count, event["usage"]))
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            raise GrokHttpError("grok_http_choices_invalid")
        if len(choices) > 1:
            raise GrokHttpError("grok_http_choices_invalid")
        if finish_event_index is not None and choices:
            raise GrokHttpError("grok_http_choices_after_finish")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index") != 0:
                raise GrokHttpError("grok_http_choice_invalid")
            if choice.get("message") is not None:
                raise GrokHttpError("grok_http_choice_invalid")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise GrokHttpError("grok_http_delta_invalid")
            if delta.get("tool_calls") not in (None, []) or delta.get("function_call") is not None:
                raise GrokHttpError("grok_http_tool_call_forbidden")
            if delta.get("role") not in (None, "assistant"):
                raise GrokHttpError("grok_http_delta_invalid")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise GrokHttpError("grok_http_content_invalid")
                output_bytes += len(content.encode("utf-8"))
                if output_bytes > MAX_RESPONSE_BYTES:
                    raise GrokHttpError("grok_http_response_too_large")
                output.append(content)
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish in {"tool_calls", "function_call"}:
                    raise GrokHttpError("grok_http_tool_call_forbidden")
                if not isinstance(finish, str):
                    raise GrokHttpError("grok_http_finish_reason_invalid")
                finish_reasons.append(finish)
                finish_event_index = event_count
    if not done_seen:
        raise GrokHttpError("grok_http_done_missing")
    if observed_models != {model} or len(response_ids) != 1:
        raise GrokHttpError("grok_http_response_binding_invalid")
    if finish_reasons != ["stop"]:
        raise GrokHttpError("grok_http_finish_reason_invalid")
    if not usage_values or finish_event_index is None:
        raise GrokHttpError("grok_http_usage_ambiguous")
    usage_packets = [
        (
            index,
            _usage_packet(
                value, maximum_output_tokens,
                require_source_proof=position == len(usage_values) - 1,
            ),
        )
        for position, (index, value) in enumerate(usage_values)
    ]
    monotonic_fields = (
        "input_tokens", "cached_tokens", "visible_output_tokens",
        "reasoning_tokens", "output_tokens", "total_tokens",
    )
    for (_prior_index, prior), (_current_index, current) in zip(
        usage_packets, usage_packets[1:], strict=False,
    ):
        if any(current[field] < prior[field] for field in monotonic_fields):
            raise GrokHttpError("grok_http_usage_nonmonotonic")
    usage_event_index, final_usage = usage_packets[-1]
    if usage_event_index < finish_event_index:
        raise GrokHttpError("grok_http_final_usage_missing")
    content = "".join(output)
    if not content.strip():
        raise GrokHttpError("grok_http_content_invalid")
    return {
        "content": content,
        "usage": final_usage,
        "response_id_sha256": hashlib.sha256(next(iter(response_ids)).encode()).hexdigest(),
        "event_count": event_count,
        "response_bytes": response_bytes,
    }


def execute_grok_request(request: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "auth_file", "auth_file_sha256", "model",
        "reasoning_effort", "reasoning_effort_mode", "prompt",
        "max_output_tokens", "timeout_seconds", "output_contract",
    }
    if not isinstance(request, dict) or set(request) != required or request.get("schema") != "GrokDirectRequestV1":
        raise GrokHttpError("grok_http_request_shape_invalid")
    model = request["model"]
    effort = request["reasoning_effort"]
    effort_mode = request["reasoning_effort_mode"]
    prompt = request["prompt"]
    maximum_output_tokens = request["max_output_tokens"]
    timeout_seconds = request["timeout_seconds"]
    output_contract = request["output_contract"]
    if not isinstance(model, str) or not MODEL_SLUG.fullmatch(model):
        raise GrokHttpError("grok_http_model_invalid")
    if not REASONING_MODEL.fullmatch(model):
        raise GrokHttpError("grok_http_model_packet_contract_unsupported")
    if effort not in EFFORTS:
        raise GrokHttpError("grok_http_reasoning_effort_invalid")
    if effort_mode not in {"fixed_low", "api_parameter"}:
        raise GrokHttpError("grok_http_reasoning_effort_mode_invalid")
    if effort_mode != "api_parameter":
        raise GrokHttpError("grok_http_reasoning_effort_mode_mismatch")
    if output_contract not in OUTPUT_CONTRACTS:
        raise GrokHttpError("grok_http_output_contract_invalid")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 400_000:
        raise GrokHttpError("grok_http_prompt_invalid")
    if (
        isinstance(maximum_output_tokens, bool)
        or not isinstance(maximum_output_tokens, int)
        or not 1 <= maximum_output_tokens <= 32_768
    ):
        raise GrokHttpError("grok_http_output_limit_invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 1 <= float(timeout_seconds) <= 300
    ):
        raise GrokHttpError("grok_http_timeout_invalid")
    token = _read_auth_token(request["auth_file"], request["auth_file_sha256"])
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": output_contract,
                "schema": _structured_output_schema(output_contract),
                "strict": True,
            },
        },
        "max_tokens": maximum_output_tokens,
    }
    payload["reasoning_effort"] = effort
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    headers = {
        "Accept": "text/event-stream",
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "AgentFanout/0.1",
    }
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _RejectRedirects(),
    )
    started = time.monotonic()
    try:
        request_value = Request(
            GROK_CHAT_COMPLETIONS_ENDPOINT,
            data=encoded,
            headers=headers,
            method="POST",
        )
        with opener.open(request_value, timeout=float(timeout_seconds)) as response:
            if response.status != 200 or response.geturl() != GROK_CHAT_COMPLETIONS_ENDPOINT:
                raise GrokHttpError("grok_http_status_invalid")
            result = _read_sse_response(
                response, model, maximum_output_tokens,
                started + float(timeout_seconds),
            )
            _validate_structured_content(result["content"], output_contract)
    except GrokHttpError:
        raise
    except HTTPError as exc:
        if exc.code in {401, 403}:
            reason = "grok_http_auth_rejected"
        elif exc.code == 400:
            reason = "grok_http_request_rejected"
        elif exc.code == 404:
            reason = "grok_http_model_unavailable"
        elif exc.code == 429:
            reason = "grok_http_rate_limited"
        elif 500 <= exc.code <= 599:
            reason = "grok_http_provider_unavailable"
        else:
            reason = "grok_http_error"
        try:
            exc.read(4096)
        except OSError:
            pass
        raise GrokHttpError(reason) from exc
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        raise GrokHttpError("grok_http_unavailable") from exc
    finally:
        token = ""
        headers.clear()
    return {
        "schema": "GrokDirectResponseV1",
        "model": model,
        "reasoning_effort": effort,
        "reasoning_effort_mode": effort_mode,
        "output_contract": output_contract,
        "endpoint_sha256": hashlib.sha256(GROK_CHAT_COMPLETIONS_ENDPOINT.encode()).hexdigest(),
        "tools_field_sent": False,
        "response_format_type": "json_schema",
        "response_schema_name": output_contract,
        "redirects_allowed": False,
        "ambient_proxy_allowed": False,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        **result,
    }


def execute_auth_preflight(request: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "auth_file", "auth_file_sha256"}
    if (
        not isinstance(request, dict)
        or set(request) != required
        or request.get("schema") != "GrokDirectAuthPreflightV1"
    ):
        raise GrokHttpError("grok_http_preflight_shape_invalid")
    token = _read_auth_token(request["auth_file"], request["auth_file_sha256"])
    token = ""
    return {
        "schema": "GrokDirectAuthPreflightResponseV1",
        "auth_entry_unambiguous": True,
        "credential_type": "xai_api_key",
        "network_verified": False,
        "endpoint_sha256": hashlib.sha256(GROK_CHAT_COMPLETIONS_ENDPOINT.encode()).hexdigest(),
        "tools_field_sent": False,
        "redirects_allowed": False,
        "ambient_proxy_allowed": False,
    }


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        print(json.dumps({"schema": "GrokDirectErrorV1", "error": "grok_http_request_too_large"}))
        return 2
    try:
        request = json.loads(raw, parse_constant=_reject_json_constant)
        if isinstance(request, dict) and request.get("schema") == "GrokDirectAuthPreflightV1":
            response = execute_auth_preflight(request)
        else:
            response = execute_grok_request(request)
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(json.dumps({"schema": "GrokDirectErrorV1", "error": "grok_http_request_json_invalid"}))
        return 2
    except GrokHttpError as exc:
        print(json.dumps({"schema": "GrokDirectErrorV1", "error": str(exc)}))
        return 2
    print(json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
