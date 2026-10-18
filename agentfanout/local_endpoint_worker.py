"""Credential-free OpenAI-compatible Local endpoint helper.

This module is launched as a supervised subprocess by the hosted Local adapter.
It receives no project path, tool, shell, or credential configuration.  Its
code issues requests only to the endpoint in one exact-shape stdin request; it
does not claim an OS-level network sandbox.
"""

from __future__ import annotations

import ipaddress
import json
import re
import ssl
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


MAX_REQUEST_BYTES = 400_000
MAX_RESPONSE_BYTES = 1_000_000
MODEL_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
EFFORTS = {"low", "medium", "high", "xhigh", "ultra"}
DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]*$")


class LocalEndpointError(ValueError):
    """The endpoint or its response did not satisfy the no-tool contract."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise LocalEndpointError("local_endpoint_redirect_denied")


def normalize_local_endpoint(endpoint: str, allow_loopback_http: bool) -> tuple[str, str]:
    """Return a canonical base URL and its policy class.

    Plain HTTP is deliberately limited to an explicitly enabled *numeric*
    loopback address.  Names such as ``localhost`` are not accepted because
    they introduce resolver and rebinding behavior into the exception path.
    """

    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 500:
        raise LocalEndpointError("local_endpoint_invalid")
    if any(ord(char) < 0x20 or char.isspace() or char == "\\" for char in endpoint):
        raise LocalEndpointError("local_endpoint_invalid")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise LocalEndpointError("local_endpoint_invalid") from exc
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise LocalEndpointError("local_endpoint_scheme_denied")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise LocalEndpointError("local_endpoint_ambient_authority_denied")
    if parsed.path and (
        not SAFE_PATH.fullmatch(parsed.path)
        or "%" in parsed.path
        or "//" in parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise LocalEndpointError("local_endpoint_path_invalid")
    if port is not None and not 1 <= port <= 65535:
        raise LocalEndpointError("local_endpoint_port_invalid")

    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if len(host) > 253 or any(not DOMAIN_LABEL.fullmatch(label) for label in host.split(".")):
            raise LocalEndpointError("local_endpoint_host_invalid")

    if parsed.scheme == "http":
        if address is None or not address.is_loopback or allow_loopback_http is not True:
            raise LocalEndpointError("local_endpoint_http_denied")
        policy = "numeric_loopback_http"
    else:
        policy = "https"

    canonical_host = f"[{address.compressed}]" if address is not None and address.version == 6 else (
        address.compressed if address is not None else host.lower()
    )
    authority = canonical_host + (f":{port}" if port is not None else "")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{authority}{path}", policy


def _read_json_response(response: Any) -> dict[str, Any]:
    content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise LocalEndpointError("local_endpoint_content_type_invalid")
    raw_length = response.headers.get("Content-Length")
    if raw_length:
        try:
            parsed_length = int(raw_length)
            if parsed_length < 0 or parsed_length > MAX_RESPONSE_BYTES:
                raise LocalEndpointError("local_endpoint_response_too_large")
        except ValueError as exc:
            raise LocalEndpointError("local_endpoint_content_length_invalid") from exc
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise LocalEndpointError("local_endpoint_response_too_large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalEndpointError("local_endpoint_response_json_invalid") from exc
    if not isinstance(value, dict):
        raise LocalEndpointError("local_endpoint_response_shape_invalid")
    return value


def _open_json(opener: Any, request: Request, timeout_seconds: float) -> dict[str, Any]:
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise LocalEndpointError("local_endpoint_status_invalid")
            return _read_json_response(response)
    except LocalEndpointError:
        raise
    except HTTPError as exc:
        raise LocalEndpointError("local_endpoint_http_error") from exc
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        raise LocalEndpointError("local_endpoint_unavailable") from exc


def execute_local_request(request: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "endpoint", "allow_loopback_http", "model", "reasoning_effort",
        "prompt", "max_output_tokens", "timeout_seconds",
    }
    if not isinstance(request, dict) or set(request) != required or request.get("schema") != "LocalEndpointRequestV1":
        raise LocalEndpointError("local_endpoint_request_shape_invalid")
    if not isinstance(request["allow_loopback_http"], bool):
        raise LocalEndpointError("local_endpoint_policy_invalid")
    endpoint, endpoint_policy = normalize_local_endpoint(
        request["endpoint"], request["allow_loopback_http"],
    )
    model = request["model"]
    effort = request["reasoning_effort"]
    prompt = request["prompt"]
    max_output_tokens = request["max_output_tokens"]
    timeout_seconds = request["timeout_seconds"]
    if not isinstance(model, str) or not MODEL_SLUG.fullmatch(model):
        raise LocalEndpointError("local_endpoint_model_invalid")
    if effort not in EFFORTS:
        raise LocalEndpointError("local_endpoint_reasoning_effort_invalid")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 300_000:
        raise LocalEndpointError("local_endpoint_prompt_invalid")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= 32_768:
        raise LocalEndpointError("local_endpoint_output_limit_invalid")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 1 <= float(timeout_seconds) <= 120:
        raise LocalEndpointError("local_endpoint_timeout_invalid")

    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _RejectRedirects(),
    )
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    models = _open_json(
        opener,
        Request(f"{endpoint}/models", headers={"Accept": "application/json"}, method="GET"),
        float(timeout_seconds),
    )
    listed = models.get("data")
    if not isinstance(listed, list) or model not in {
        item.get("id") for item in listed if isinstance(item, dict) and isinstance(item.get("id"), str)
    }:
        raise LocalEndpointError("local_endpoint_exact_model_not_loaded")

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": effort,
        "temperature": 0,
        "max_tokens": max_output_tokens,
        "stream": False,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    response = _open_json(
        opener,
        Request(f"{endpoint}/chat/completions", data=payload, headers=headers, method="POST"),
        float(timeout_seconds),
    )
    if response.get("model") != model:
        raise LocalEndpointError("local_endpoint_response_model_mismatch")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise LocalEndpointError("local_endpoint_choices_invalid")
    choice = choices[0]
    message = choice.get("message")
    if (
        not isinstance(message, dict)
        or message.get("role") != "assistant"
        or not isinstance(message.get("content"), str)
        or not message["content"].strip()
    ):
        raise LocalEndpointError("local_endpoint_content_invalid")
    if message.get("tool_calls") not in (None, []):
        raise LocalEndpointError("local_endpoint_tool_call_denied")
    if choice.get("finish_reason") not in {"stop", "length"}:
        raise LocalEndpointError("local_endpoint_finish_reason_invalid")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise LocalEndpointError("local_endpoint_usage_missing")
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (input_tokens, output_tokens)):
        raise LocalEndpointError("local_endpoint_usage_invalid")
    if output_tokens > max_output_tokens:
        raise LocalEndpointError("local_endpoint_output_limit_exceeded")
    total_tokens = usage.get("total_tokens")
    if total_tokens is not None and (
        isinstance(total_tokens, bool)
        or not isinstance(total_tokens, int)
        or total_tokens != input_tokens + output_tokens
    ):
        raise LocalEndpointError("local_endpoint_usage_invalid")
    prompt_details = usage.get("prompt_tokens_details", {})
    if prompt_details is None:
        prompt_details = {}
    if not isinstance(prompt_details, dict):
        raise LocalEndpointError("local_endpoint_usage_invalid")
    cached_tokens = prompt_details.get("cached_tokens", 0)
    if isinstance(cached_tokens, bool) or not isinstance(cached_tokens, int) or not 0 <= cached_tokens <= input_tokens:
        raise LocalEndpointError("local_endpoint_usage_invalid")
    return {
        "schema": "LocalEndpointResponseV1",
        "model": model,
        "reasoning_effort": effort,
        "endpoint_policy": endpoint_policy,
        "content": message["content"],
        "usage": {
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "uncached_input_tokens": input_tokens - cached_tokens,
            "output_tokens": output_tokens,
            "cost_usd": None,
            "usage_verified": True,
            "cost_verified": False,
        },
    }


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        print(json.dumps({"schema": "LocalEndpointErrorV1", "error": "local_endpoint_request_too_large"}))
        return 2
    try:
        request = json.loads(raw)
        response = execute_local_request(request)
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(json.dumps({"schema": "LocalEndpointErrorV1", "error": "local_endpoint_request_json_invalid"}))
        return 2
    except LocalEndpointError as exc:
        print(json.dumps({"schema": "LocalEndpointErrorV1", "error": str(exc)}))
        return 2
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
