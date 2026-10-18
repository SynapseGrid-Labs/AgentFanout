"""Operator-bound, synthetic observability evaluation for AgentFanout.

The trace contains no live prompt, tool output, path, credential, or run payload.
It uses fixed synthetic events sanitized with the production allowlist. Only
explicit ingestion can send them to the operator-configured private Phoenix
OTLP endpoint and existing Langfuse sink. Reports omit device measurements,
endpoint addresses and local paths; configuration remains host-local.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import struct
import sys
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agentfanout.telemetry import LangfuseHttpSink, sanitize_event


PHOENIX_OTLP_ENDPOINT = os.environ.get("AGENT_FANOUT_EVAL_PHOENIX_OTLP_ENDPOINT", "")
SYNTHETIC_SERVICE = "agentfanout-observability-eval"
SYNTHETIC_RUN_ID = "run_synthetic_00000000000000000000000000000001"
SYNTHETIC_TRACE_ID = bytes.fromhex("a1" * 16)
SYNTHETIC_ROOT_SPAN_ID = bytes.fromhex("b1" * 8)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require_evaluation_host() -> None:
    """Require an explicit exact host binding before any ingestion."""
    expected = os.environ.get("AGENT_FANOUT_EVAL_HOSTNAME", "").strip().lower()
    if not expected:
        raise RuntimeError("evaluation_hostname_required")
    hostname = socket.gethostname().split(".", 1)[0].lower()
    if hostname != expected:
        raise RuntimeError("evaluation_host_mismatch")


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf_unsigned_value_required")
    chunks = bytearray()
    while value > 0x7F:
        chunks.append((value & 0x7F) | 0x80)
        value >>= 7
    chunks.append(value)
    return bytes(chunks)


def _key(field: int, wire_type: int) -> bytes:
    return _varint((field << 3) | wire_type)


def _bytes(field: int, value: bytes) -> bytes:
    return _key(field, 2) + _varint(len(value)) + value


def _string(field: int, value: str) -> bytes:
    return _bytes(field, value.encode("utf-8"))


def _bool(field: int, value: bool) -> bytes:
    return _key(field, 0) + _varint(1 if value else 0)


def _int(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


def _double(field: int, value: float) -> bytes:
    return _key(field, 1) + struct.pack("<d", value)


def _fixed64(field: int, value: int) -> bytes:
    return _key(field, 1) + struct.pack("<Q", value)


def _any_value(value: Any) -> bytes:
    if isinstance(value, bool):
        return _bool(2, value)
    if isinstance(value, int):
        return _int(3, value)
    if isinstance(value, float):
        return _double(4, value)
    if value is None:
        return _string(1, "null")
    return _string(1, str(value))


def _attribute(key: str, value: Any) -> bytes:
    return _string(1, key) + _bytes(2, _any_value(value))


def _span(name: str, span_id: bytes, parent_span_id: bytes, start_ns: int, duration_ms: int, attributes: dict[str, Any]) -> bytes:
    body = (
        _bytes(1, SYNTHETIC_TRACE_ID)
        + _bytes(2, span_id)
        + (_bytes(4, parent_span_id) if parent_span_id else b"")
        + _string(5, name)
        + _int(6, 1)  # INTERNAL
        + _fixed64(7, start_ns)
        + _fixed64(8, start_ns + duration_ms * 1_000_000)
    )
    for key, value in sorted(attributes.items()):
        body += _bytes(9, _attribute(key, value))
    return body


def otlp_trace_protobuf(spans: list[dict[str, Any]]) -> bytes:
    """Encode the minimal OTLP ExportTraceServiceRequest without a third-party SDK."""
    instrumentation_scope = _string(1, "agentfanout-observability-eval")
    scope_spans = _bytes(1, instrumentation_scope)
    for item in spans:
        scope_spans += _bytes(2, _span(**item))
    resource = b"".join(
        _bytes(1, _attribute(key, value))
        for key, value in {
            "service.name": SYNTHETIC_SERVICE,
            "service.version": "evaluation-v1",
            "deployment.environment": "private-service-evaluation",
        }.items()
    )
    resource_spans = _bytes(1, resource) + _bytes(2, scope_spans)
    return _bytes(1, resource_spans)


def synthetic_events() -> list[dict[str, Any]]:
    """Fixed events deliberately containing redaction probes, then production-sanitize them."""
    raw = [
        ("route", {"run_id": SYNTHETIC_RUN_ID, "wave_id": "wave_01", "provider": "Codex", "model": "fixture-fast", "reasoning_effort": "low", "action": "dispatch", "cost_usd": 0.001, "duration_ms": 4, "path": "/private/must-not-export", "api_key": "must-not-export"}),
        ("launch", {"run_id": SYNTHETIC_RUN_ID, "wave_id": "wave_01", "task_id": "task_synthetic_01", "worker_id": "worker_synthetic_01", "provider": "Codex", "model": "fixture-fast", "reasoning_effort": "low", "status": "started", "duration_ms": 20}),
        ("result", {"run_id": SYNTHETIC_RUN_ID, "wave_id": "wave_01", "task_id": "task_synthetic_01", "worker_id": "worker_synthetic_01", "provider": "Codex", "model": "fixture-fast", "status": "partial", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001, "duration_ms": 125, "residual_count": 1, "evidence_sha256": "e" * 64}),
        ("validation", {"run_id": SYNTHETIC_RUN_ID, "wave_id": "wave_01", "task_id": "task_synthetic_01", "status": "pass", "validator_verdict": "pass", "duration_ms": 15}),
        ("terminal", {"run_id": SYNTHETIC_RUN_ID, "status": "authorized", "action": "complete", "cost_usd": 0.001, "duration_ms": 200}),
    ]
    return [sanitize_event(event_type, fields) for event_type, fields in raw]


def synthetic_spans(events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Map sanitized events to a deterministic root/child OTLP trace hierarchy."""
    values = events or synthetic_events()
    start_ns = 1_770_000_000_000_000_000  # fixed synthetic timestamp, never host time
    spans: list[dict[str, Any]] = [{
        "name": "agentfanout.run",
        "span_id": SYNTHETIC_ROOT_SPAN_ID,
        "parent_span_id": b"",
        "start_ns": start_ns,
        "duration_ms": 200,
        "attributes": {"agentfanout.run_id": SYNTHETIC_RUN_ID, "synthetic": True, "span.role": "orchestrator"},
    }]
    for index, event in enumerate(values, start=1):
        fields = event["fields"]
        attributes = {f"agentfanout.{key}": value for key, value in fields.items()}
        attributes.update({"agentfanout.event_type": event["event_type"], "synthetic": True, "span.role": "event"})
        spans.append({
            "name": "agentfanout." + event["event_type"],
            "span_id": index.to_bytes(8, "big"),
            "parent_span_id": SYNTHETIC_ROOT_SPAN_ID,
            "start_ns": start_ns + index * 10_000_000,
            "duration_ms": int(fields.get("duration_ms", 1)),
            "attributes": attributes,
        })
    return spans


def phoenix_request(payload: bytes) -> urllib.request.Request:
    parsed = urllib.parse.urlsplit(PHOENIX_OTLP_ENDPOINT)
    allowed_hosts = {
        item.strip() for item in os.environ.get("AGENT_FANOUT_EVAL_ALLOWED_HOSTS", "").split(",") if item.strip()
    }
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.hostname not in allowed_hosts
        or parsed.path != "/v1/traces"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("phoenix_evaluation_endpoint_not_allowed")
    return urllib.request.Request(
        PHOENIX_OTLP_ENDPOINT,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-protobuf", "Content-Length": str(len(payload))},
    )


def send_phoenix(payload: bytes, timeout: float) -> int:
    request = phoenix_request(payload)
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"phoenix_otlp_ingestion_failed:{response.status}")
        return response.status


def _read_langfuse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key in {"LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_ALLOWED_HOSTS"}:
                values[key] = value.strip()
    if set(values) != {"LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_ALLOWED_HOSTS"}:
        raise ValueError("langfuse_evaluation_config_incomplete")
    return values


def send_langfuse(events: list[dict[str, Any]], env_file: Path) -> int:
    values = _read_langfuse_env(env_file)
    parsed = urllib.parse.urlsplit(values["LANGFUSE_HOST"])
    allowed_hosts = {item.strip() for item in values["LANGFUSE_ALLOWED_HOSTS"].split(",") if item.strip()}
    if parsed.username or parsed.password or parsed.hostname not in allowed_hosts or parsed.query or parsed.fragment:
        raise ValueError("langfuse_evaluation_host_not_allowed")
    sink = LangfuseHttpSink(
        values["LANGFUSE_HOST"], values["LANGFUSE_PUBLIC_KEY"], values["LANGFUSE_SECRET_KEY"],
        allowed_hosts=allowed_hosts,
    )
    for event in events:
        sink.emit(event)
    return len(events)


def resource_observations() -> dict[str, Any]:
    """Keep the report field compatible without collecting device information."""
    return {
        "schema": "AgentFanoutObservabilityResourceObservationV1",
        "synthetic_data_only": True,
        "collection": "not_collected",
        "host_resource_data_included": False,
    }


def weighted_score(scores: dict[str, float], rubric: dict[str, Any]) -> dict[str, Any]:
    criteria = rubric["criteria"]
    unknown = sorted(set(scores) - {item["id"] for item in criteria})
    if unknown:
        raise ValueError("rubric_unknown_scores:" + ",".join(unknown))
    weighted = 0.0
    missing: list[str] = []
    for item in criteria:
        value = scores.get(item["id"])
        if value is None:
            missing.append(item["id"])
            continue
        if not isinstance(value, (int, float)) or not 0 <= value <= 5:
            raise ValueError("rubric_score_out_of_range:" + item["id"])
        weighted += float(value) / 5 * item["weight"]
    return {"weighted_score": round(weighted, 2), "missing_criteria": missing, "complete": not missing}


def report_payload(phoenix_status: int | None, langfuse_event_count: int | None, rubric: dict[str, Any]) -> dict[str, Any]:
    events = synthetic_events()
    spans = synthetic_spans(events)
    payload = otlp_trace_protobuf(spans)
    return {
        "schema": "AgentFanoutObservabilityEvaluationReportV1",
        "synthetic_data_only": True,
        "trace": {
            "trace_id_sha256": hashlib.sha256(SYNTHETIC_TRACE_ID).hexdigest(),
            "span_count": len(spans),
            "root_span": "agentfanout.run",
            "child_spans": [span["name"] for span in spans[1:]],
            "phoenix_otlp_protobuf_bytes": len(payload),
        },
        "query_keys": [
            "agentfanout.run_id", "agentfanout.wave_id", "agentfanout.task_id", "agentfanout.worker_id",
            "agentfanout.provider", "agentfanout.model", "agentfanout.reasoning_effort",
            "agentfanout.input_tokens", "agentfanout.output_tokens", "agentfanout.cost_usd",
            "agentfanout.duration_ms", "agentfanout.residual_count", "agentfanout.validator_verdict",
            "agentfanout.reason_code",
        ],
        "ingestion": {
            "phoenix": {"destination": "operator_configured_private_sink", "http_status": phoenix_status, "trace_hierarchy_mode": "OTLP parent_span_id"},
            "langfuse": {"events_sent": langfuse_event_count, "trace_hierarchy_mode": "existing_event_sink_only; group manually by sanitized run_id"},
        },
        "redaction": {
            "sanitizer": "agentfanout.telemetry.sanitize_event",
            "allowlist_only": True,
            "synthetic_probes": ["path", "api_key"],
            "raw_prompt_or_tool_output_included": False,
        },
        "resource_observations": resource_observations(),
        "rubric": rubric,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingest", action="store_true", help="Allow explicitly bound private-host ingestion. Default is offline report only.")
    parser.add_argument("--target", choices=["phoenix", "langfuse", "both"], default="both")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    parser.add_argument(
        "--langfuse-env", type=Path,
        default=Path(os.environ.get(
            "AGENT_FANOUT_TELEMETRY_ENV",
            config_home / "agentfanout" / "langfuse.env",
        )).expanduser(),
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--report-out", type=Path, required=True)
    args = parser.parse_args(argv)
    rubric = json.loads((Path(__file__).with_name("rubric.json")).read_text(encoding="utf-8"))
    phoenix_status = None
    langfuse_event_count = None
    events = synthetic_events()
    if args.ingest:
        require_evaluation_host()
        if args.target in {"phoenix", "both"}:
            phoenix_status = send_phoenix(otlp_trace_protobuf(synthetic_spans(events)), args.timeout)
        if args.target in {"langfuse", "both"}:
            langfuse_event_count = send_langfuse(events, args.langfuse_env)
    report = report_payload(phoenix_status, langfuse_event_count, rubric)
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(args.report_out, 0o600)
    print(json.dumps({"schema": report["schema"], "synthetic_data_only": True, "report_written": True, "ingest": args.ingest}, sort_keys=True))
    return 0


def cli(argv: list[str] | None = None) -> int:
    """Return stable diagnostics without echoing host, endpoint or path details."""
    try:
        return main(argv)
    except (OSError, HTTPException, RuntimeError, ValueError) as exc:
        safe_reasons = {
            "evaluation_hostname_required", "evaluation_host_mismatch",
            "phoenix_evaluation_endpoint_not_allowed", "langfuse_evaluation_host_not_allowed",
            "langfuse_evaluation_config_incomplete",
        }
        detail = exc.args[0] if exc.args else None
        reason = detail if isinstance(detail, str) and detail in safe_reasons else "evaluation_failed"
        print(json.dumps({"schema": "AgentFanoutObservabilityEvaluationReportV1", "status": "blocked", "reason": reason}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
