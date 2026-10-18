"""Bounded fresh-session GrokCLI ACP primitives.

This is a distinct subscription lane, never the legacy Grok HTTPS adapter.
The installed CLI has not conformed to the strict boundary: production admission
is unavailable. Transports and host receipts are injectable for offline contract
verification; a prepared session is never permission to infer.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

PROVIDER = "GrokCLI"
MODE = "grok_cli_subscription_acp"
SCHEMA = "GrokCLISessionV1"
MAX_FRAME = 262144
MAX_INPUT = 131072
MAX_OUTPUT = 524288
MAX_EVENTS = 2048
# These facts require installed-binary host verification, not adapter assertions.
REQUIRED_PROOFS = frozenset({
    "session_auth_pinned", "api_auth_disabled", "effective_config_isolated",
    "builtins_denied", "callbacks_denied", "extensions_denied",
    "background_access_denied", "nested_fanout_denied", "workspace_isolated",
    "credential_protection", "bootstrap_harmless", "model_effort_readback",
    "usage_semantics", "cancellation_semantics", "spending_enforced",
})


class GrokCLIUnavailable(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(code + (": " + detail if detail else ""))


def capability_status() -> dict:
    """Honest shipped capability, independent of flags or auth presence."""
    return {"provider": PROVIDER, "mode": MODE, "available": False,
            "code": "installed_cli_not_conformed",
            "missing_proofs": sorted(REQUIRED_PROOFS),
            "native_profile": "advisory", "retained_sessions": False}


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise GrokCLIUnavailable("malformed_frame") from exc


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON constant")


def input_digest(text: str) -> str:
    if not isinstance(text, str) or not text or len(text.encode()) > MAX_INPUT:
        raise GrokCLIUnavailable("input_bounds")
    from .project_binding import bounded_content_is_sensitive
    if bounded_content_is_sensitive(text):
        raise GrokCLIUnavailable("input_content_sensitive")
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class AdmissionEvidence:
    binary_sha256: str
    config_sha256: str
    model: str
    effort: str
    accepted_auth: str
    proofs: frozenset[str]
    allow_paid_overage: bool
    billing_regime: str
    evidence_id: str

    def verify(self, host_verify: Callable[["AdmissionEvidence"], bool]) -> None:
        for digest in (self.binary_sha256, self.config_sha256):
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise GrokCLIUnavailable("identity_invalid")
        if self.accepted_auth != "cached_token":
            raise GrokCLIUnavailable("subscription_auth_not_confirmed")
        if not isinstance(self.model, str) or not 0 < len(self.model) <= 200 or not isinstance(self.effort, str) or self.effort not in {"low", "medium", "high"}:
            raise GrokCLIUnavailable("effective_selection_invalid")
        if not isinstance(self.proofs, frozenset) or not all(isinstance(item, str) for item in self.proofs):
            raise GrokCLIUnavailable("boundary_unproved")
        if REQUIRED_PROOFS - self.proofs:
            raise GrokCLIUnavailable("boundary_unproved", ",".join(sorted(REQUIRED_PROOFS - self.proofs)))
        if type(self.allow_paid_overage) is not bool or self.billing_regime not in {"included_only_enforced", "paid_overage_authorized"}:
            raise GrokCLIUnavailable("spending_policy_unknown")
        if self.allow_paid_overage != (self.billing_regime == "paid_overage_authorized"):
            raise GrokCLIUnavailable("spending_policy_conflict")
        if not self.evidence_id or host_verify(self) is not True:
            raise GrokCLIUnavailable("host_evidence_unverified")


@dataclass(frozen=True)
class PreparedSession:
    schema: str
    session_id: str
    process_epoch: str
    task_id: str
    input_sha256: str
    model: str
    effort: str
    binary_sha256: str
    config_sha256: str
    auth_kind: str
    evidence_id: str
    role: str


@dataclass(frozen=True)
class HostAuthorization:
    """Host-owned durable receipt reference, never issued by ACPClient."""
    receipt_id: str
    prepared: PreparedSession
    reservation_usd: float

    def verify(self, prepared: PreparedSession, durable_check: Callable[["HostAuthorization"], bool]) -> None:
        if self.prepared != prepared or not self.receipt_id:
            raise GrokCLIUnavailable("authorization_binding_mismatch")
        if type(self.reservation_usd) not in {int, float} or not 0 <= self.reservation_usd <= 1e12 or not math.isfinite(self.reservation_usd):
            raise GrokCLIUnavailable("reservation_invalid")
        if durable_check(self) is not True:
            raise GrokCLIUnavailable("authorization_not_durable")


class Transport(Protocol):
    epoch: str
    def send(self, frame: dict) -> None: ...
    def receive(self, deadline: float) -> dict: ...
    def close(self) -> None: ...


def isolated_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Allowlist environment; never inherit credentials/proxies/config overrides.

    Official cached login provisioning remains a host-local conformance concern.
    This does not copy the operator's auth store or mutate their global config.
    """
    return {key: base[key] for key in ("PATH", "LANG", "LC_ALL") if key in base}


class StdioTransport:
    """NDJSON ACP subprocess with bounded frames, stderr and deadlines.

    Host must prove harmless bootstrap before construction. This low-level
    transport alone is not an admitted worker and performs no authentication.
    """
    def __init__(self, binary: str, workspace: str, epoch: str,
                 evidence: AdmissionEvidence, host_verify: Callable,
                 environment: Mapping[str, str], expected_executable_identity: dict):
        evidence.verify(host_verify)
        path = Path(binary)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise GrokCLIUnavailable("binary_identity_invalid")
        from .provider_adapters import (verify_executable_identity,
            _generic_effective_launch_prefix, _prepare_pinned_launch_argv)
        identity = verify_executable_identity(binary, expected_executable_identity)
        if identity["executable_sha256"] != evidence.binary_sha256:
            raise GrokCLIUnavailable("binary_drift")
        root = Path(workspace)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink() or any(root.iterdir()):
            raise GrokCLIUnavailable("workspace_not_empty")
        # HOME/config are supplied only by an explicitly verified host profile.
        if set(environment) - {"PATH", "LANG", "LC_ALL", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}:
            raise GrokCLIUnavailable("ambient_environment_rejected")
        self.epoch = epoch
        argv, executable, arguments = _generic_effective_launch_prefix(binary, identity, PROVIDER)
        argv.extend(["--no-auto-update", "agent", "stdio"])
        argv, descriptors, self.pinning = _prepare_pinned_launch_argv(argv, executable, arguments)
        try:
            self.process = subprocess.Popen(argv, cwd=workspace, env=dict(environment),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, pass_fds=tuple(descriptors), shell=False)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        self.process_group_id = self.process.pid
        self.selector = selectors.DefaultSelector()
        for stream in (self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.buffer = bytearray()
        self.stderr_bytes = 0
        self.closed = False
        self.exit_confirmed = False

    def send(self, frame: dict) -> None:
        data = _canonical(frame) + b"\n"
        if len(data) > MAX_FRAME:
            raise GrokCLIUnavailable("frame_bounds")
        # Bound write backpressure too; full supplied-text frames may exceed a pipe.
        deadline = time.monotonic() + 30
        offset = 0
        while offset < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.process.poll() is not None:
                raise GrokCLIUnavailable("transport_write_timeout_or_exit")
            _, writable, _ = select.select([], [self.process.stdin], [], min(remaining, 0.25))
            if not writable:
                continue
            try:
                written = os.write(self.process.stdin.fileno(), data[offset:])
            except BlockingIOError:
                continue
            except OSError as exc:
                raise GrokCLIUnavailable("transport_write_failed") from exc
            if written <= 0:
                raise GrokCLIUnavailable("transport_partial_write")
            offset += written

    def receive(self, deadline: float) -> dict:
        while True:
            if b"\n" in self.buffer:
                line, _, tail = self.buffer.partition(b"\n")
                self.buffer = bytearray(tail)
                if len(line) > MAX_FRAME:
                    raise GrokCLIUnavailable("frame_bounds")
                try:
                    frame = json.loads(line, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
                except (ValueError, UnicodeError) as exc:
                    raise GrokCLIUnavailable("malformed_frame") from exc
                if not isinstance(frame, dict):
                    raise GrokCLIUnavailable("malformed_frame")
                return frame
            if len(self.buffer) > MAX_FRAME:
                raise GrokCLIUnavailable("frame_bounds")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GrokCLIUnavailable("transport_timeout")
            events = self.selector.select(min(remaining, 0.25))
            for key, _ in events:
                chunk = os.read(key.fd, min(65536, MAX_FRAME + 1))
                if not chunk:
                    if key.fileobj is self.process.stderr:
                        self.selector.unregister(key.fileobj)
                        continue
                    raise GrokCLIUnavailable("transport_eof")
                if key.fileobj is self.process.stderr:
                    self.stderr_bytes += len(chunk)
                    if self.stderr_bytes > MAX_OUTPUT:
                        raise GrokCLIUnavailable("stderr_bounds")
                else:
                    self.buffer.extend(chunk)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.selector.close()
        from .provider_adapters import terminate_process_group
        self.exit_confirmed = terminate_process_group(self.process, self.process_group_id)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
        # Process exit is not proof that remote provider work was cancelled.


class ACPClient:
    """One fresh session and exactly one prompt, with no tool callbacks."""
    def __init__(self, transport: Transport, evidence: AdmissionEvidence,
                 host_verify: Callable, journal: Callable[[str, dict], None], timeout: float = 30,
                 wire_mapping=None):
        evidence.verify(host_verify)
        if type(timeout) not in {int, float} or not 0 < timeout <= 300 or not transport.epoch:
            raise GrokCLIUnavailable("session_bounds")
        self.transport, self.evidence, self.journal = transport, evidence, journal
        self.wire_mapping = wire_mapping
        self._wire_session_id = None
        self._wire_selection_locked = False
        if wire_mapping is not None:
            wire_mapping.verify(evidence)
        self.timeout = timeout
        self.sequence = 0
        self.prepared = None
        self.session_attempted = False
        self.prompted = False
        self.uncertain = False

    def _journal(self, kind: str, payload: dict) -> None:
        if self.journal(kind, payload) is not True:
            raise GrokCLIUnavailable("journal_not_durable")

    def _request(self, method: str, params: dict, *, updates: bool = False) -> tuple[dict, str]:
        if self.wire_mapping is not None and method not in self.wire_mapping.binding.methods:
            raise GrokCLIUnavailable("wire_method_not_attested")
        self.sequence += 1
        request_id = self.sequence
        wire_method = "_" + method if self.wire_mapping is not None and method.startswith("x.ai/") else method
        self.transport.send({"jsonrpc": "2.0", "id": request_id, "method": wire_method, "params": params})
        deadline = time.monotonic() + self.timeout
        output = []
        output_size = 0
        for _ in range(MAX_EVENTS):
            frame = self.transport.receive(deadline)
            if len(_canonical(frame)) > MAX_FRAME or frame.get("jsonrpc") != "2.0":
                raise GrokCLIUnavailable("malformed_frame")
            # Client requests are denied, including hidden filesystem/terminal/auth callbacks.
            if "method" in frame:
                if "id" in frame:
                    self.transport.send({"jsonrpc": "2.0", "id": frame["id"],
                        "error": {"code": -32601, "message": "client callbacks denied"}})
                    raise GrokCLIUnavailable("callback_denied")
                if frame["method"] != "session/update" or (not updates and self.wire_mapping is None):
                    raise GrokCLIUnavailable("unexpected_notification")
                params = frame.get("params", {})
                if not isinstance(params, dict):
                    raise GrokCLIUnavailable("malformed_frame")
                active_session = self.prepared.session_id if self.prepared else self._wire_session_id
                if not active_session or params.get("sessionId") != active_session:
                    raise GrokCLIUnavailable("wrong_session")
                if not isinstance(params, dict):
                    raise GrokCLIUnavailable("malformed_frame")
                update = params.get("update", {})
                if self.wire_mapping is not None and isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
                    from .grok_abi import selection
                    observed_selection = selection(update.get("configOptions"))
                    if self._wire_selection_locked and observed_selection != (self.evidence.model, self.evidence.effort):
                        raise GrokCLIUnavailable("effective_selection_changed")
                    continue
                if not updates:
                    raise GrokCLIUnavailable("unexpected_notification")
                if not isinstance(update, dict) or not isinstance(update.get("content"), dict):
                    raise GrokCLIUnavailable("malformed_frame")
                thought = self.wire_mapping is not None and update.get("sessionUpdate") == "agent_thought_chunk"
                if (update.get("sessionUpdate") != "agent_message_chunk" and not thought) or update.get("content", {}).get("type") != "text":
                    raise GrokCLIUnavailable("nontext_or_tool_update")
                text = update["content"].get("text")
                if not isinstance(text, str):
                    raise GrokCLIUnavailable("malformed_frame")
                output_size += len(text.encode())
                if output_size > MAX_OUTPUT:
                    raise GrokCLIUnavailable("output_bounds")
                if not thought:
                    output.append(text)
                continue
            if type(frame.get("id")) is not int or frame["id"] != request_id:
                raise GrokCLIUnavailable("wrong_request")
            if "error" in frame or not isinstance(frame.get("result"), dict):
                raise GrokCLIUnavailable("request_rejected")
            return frame["result"], "".join(output)
        raise GrokCLIUnavailable("event_bounds")

    def prepare(self, task_id: str, text: str, role: str, workspace: str) -> PreparedSession:
        if self.session_attempted or role not in {"worker", "validator"} or not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise GrokCLIUnavailable("fresh_session_required")
        digest = input_digest(text)
        self.session_attempted = True
        if self.wire_mapping is not None:
            self.prepared = self.wire_mapping.prepare(self, task_id, text, role, workspace, self.evidence)
            return self.prepared
        # Method shapes below form an offline contract; installed negotiation must
        # supply verified conformance before this client may be admitted live.
        init, _ = self._request("initialize", {"protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        if init.get("protocolVersion") != 1:
            raise GrokCLIUnavailable("protocol_unsupported")
        auth, _ = self._request("authenticate", {"methodId": "cached_token"})
        if auth.get("acceptedMethod") != "cached_token":
            raise GrokCLIUnavailable("subscription_auth_not_confirmed")
        session, _ = self._request("session/new", {"cwd": workspace, "mcpServers": []})
        if not isinstance(session.get("sessionId"), str) or not session["sessionId"] or len(session["sessionId"]) > 200:
            raise GrokCLIUnavailable("session_missing")
        # Effective readback is a separate attested observation, not setter success.
        if session.get("effectiveModel") != self.evidence.model or session.get("effectiveEffort") != self.evidence.effort:
            raise GrokCLIUnavailable("effective_selection_mismatch")
        self.prepared = PreparedSession(SCHEMA, session["sessionId"], self.transport.epoch,
            task_id, digest, self.evidence.model, self.evidence.effort,
            self.evidence.binary_sha256, self.evidence.config_sha256,
            self.evidence.accepted_auth, self.evidence.evidence_id, role)
        return self.prepared

    def execute(self, text: str, authorization: HostAuthorization,
                durable_check: Callable[[HostAuthorization], bool]) -> dict:
        if self.prepared is None or self.prompted or self.uncertain:
            raise GrokCLIUnavailable("fresh_session_required")
        if input_digest(text) != self.prepared.input_sha256:
            raise GrokCLIUnavailable("input_binding_mismatch")
        if self.wire_mapping is not None:
            self.wire_mapping.verify(self.evidence)
            if self.wire_mapping.binding.digest() != self.prepared.wire_binding_sha256:
                raise GrokCLIUnavailable("wire_binding_changed")
        authorization.verify(self.prepared, durable_check)
        # Journal must durably persist or raise before prompt. Adapter has no
        # authority to invent the receipt or accept its mere in-memory presence.
        self._journal("inference_start_intent", {"receipt_id": authorization.receipt_id,
            "session_id": self.prepared.session_id, "process_epoch": self.transport.epoch})
        self.prompted = True
        try:
            result, output = self._request("session/prompt", {"sessionId": self.prepared.session_id,
                "prompt": [{"type": "text", "text": text}]}, updates=True)
            if result.get("stopReason") != "end_turn":
                raise GrokCLIUnavailable("turn_incomplete")
            mapped_usage = self.wire_mapping.observe_turn(self, result) if self.wire_mapping is not None else None
            if mapped_usage is not None:
                self._journal("wire_usage_observed", {"session_id":self.prepared.session_id,
                    "process_epoch":self.transport.epoch,"request_id":authorization.receipt_id,
                    "audit":mapped_usage["audit"]})
            self._journal("inference_response_observed", {"receipt_id": authorization.receipt_id,
                "response_sha256": hashlib.sha256(output.encode()).hexdigest()})
            return {"text": output, "result": result, "prepared": self.prepared,
                    "mapped_usage": mapped_usage,
                    "provider_cost_usd": None, "reservation_usd": authorization.reservation_usd}
        except Exception:
            self.uncertain = True
            self._journal("inference_effect_uncertain", {"receipt_id": authorization.receipt_id})
            raise

    def cancel(self) -> dict:
        # ACP notification alone has no acknowledged remote cancellation evidence.
        self.uncertain = True
        try:
            self._journal("cancellation_uncertain", {"process_epoch": self.transport.epoch,
                "session_id": self.prepared.session_id if self.prepared else None})
        finally:
            self.transport.close()
        return {"remote_effect": "unknown", "replay_allowed": False, "quarantined": True}


@dataclass(frozen=True)
class UsageObservation:
    process_epoch: str
    session_id: str
    request_id: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    provider_cost_usd: float | None
    cumulative: bool
    provider_cost_verified: bool = False


def normalize_usage(raw: dict, *, process_epoch: str, session_id: str,
                    request_id: str, semantics: dict) -> UsageObservation:
    """Normalize only verified whole-turn semantics; last-call data is incomplete."""
    if semantics.get("scope") != "whole_turn" or semantics.get("cache") not in {"included", "excluded"} or semantics.get("reasoning") not in {"included", "excluded"} or type(semantics.get("cumulative")) is not bool:
        raise GrokCLIUnavailable("usage_semantics_unproved")
    if not isinstance(raw, dict):
        raise GrokCLIUnavailable("usage_incomplete")
    for flag in ("usage_is_incomplete", "usage_is_partial", "cost_is_partial"):
        if flag in raw and type(raw[flag]) is not bool:
            raise GrokCLIUnavailable("usage_flag_invalid")
    if raw.get("usage_is_incomplete") is True or raw.get("usage_is_partial") is True:
        raise GrokCLIUnavailable("usage_incomplete")
    values = []
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
        value = raw.get(key)
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise GrokCLIUnavailable("usage_incomplete")
        values.append(value)
    inp, cache, output, reasoning = values
    if semantics["cache"] == "excluded":
        inp += cache
    elif cache > inp:
        raise GrokCLIUnavailable("usage_inconsistent")
    if semantics["reasoning"] == "excluded":
        output += reasoning
    elif reasoning > output:
        raise GrokCLIUnavailable("usage_inconsistent")
    cost = raw.get("provider_cost_usd")
    if cost is not None and (type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0):
        raise GrokCLIUnavailable("provider_cost_invalid")
    if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in (process_epoch, session_id, request_id)):
        raise GrokCLIUnavailable("usage_correlation_missing")
    return UsageObservation(process_epoch, session_id, request_id, inp, cache, output, reasoning,
        cost, semantics["cumulative"], cost is not None and raw.get("provider_cost_verified") is True and
        raw.get("cost_is_partial") is not True and semantics.get("price_evidence_verified") is True)


class UsageReconciler:
    """Serializable per-session baselines and dedup; unknown cost stays unknown."""
    MAX_RECORDS = 10000

    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {"schema": "GrokCLIUsageV1", "seen": {}, "baselines": {}, "modes": {}}
        if not isinstance(self.state, dict) or self.state.get("schema") != "GrokCLIUsageV1":
            raise GrokCLIUnavailable("usage_schema_unsupported")
        if set(self.state) != {"schema", "seen", "baselines", "modes"}:
            raise GrokCLIUnavailable("usage_state_malformed")
        for key in ("seen", "baselines", "modes"):
            if not isinstance(self.state[key], dict) or len(self.state[key]) > self.MAX_RECORDS:
                raise GrokCLIUnavailable("usage_state_bounds")
        def correlation(key, length):
            if not isinstance(key, str) or len(key) > 1024:
                raise GrokCLIUnavailable("usage_state_correlation_invalid")
            try:
                values = json.loads(key)
            except ValueError as exc:
                raise GrokCLIUnavailable("usage_state_correlation_invalid") from exc
            if not isinstance(values, list) or len(values) != length or not all(isinstance(value, str) and 0 < len(value) <= 200 for value in values):
                raise GrokCLIUnavailable("usage_state_correlation_invalid")
            return values
        for key, digest in self.state["seen"].items():
            correlation(key, 3)
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise GrokCLIUnavailable("usage_state_fingerprint_invalid")
        for key, mode in self.state["modes"].items():
            correlation(key, 2)
            if type(mode) is not bool:
                raise GrokCLIUnavailable("usage_state_mode_invalid")
        for key, baseline in self.state["baselines"].items():
            epochs = correlation(key, 2)
            if not isinstance(baseline, dict) or set(baseline) != set(UsageObservation.__dataclass_fields__):
                raise GrokCLIUnavailable("usage_state_baseline_invalid")
            if [baseline["process_epoch"], baseline["session_id"]] != epochs or self.state["modes"].get(key) is not True or baseline["cumulative"] is not True:
                raise GrokCLIUnavailable("usage_state_baseline_invalid")
            if type(baseline["provider_cost_verified"]) is not bool or not isinstance(baseline["request_id"], str) or not 0 < len(baseline["request_id"]) <= 200:
                raise GrokCLIUnavailable("usage_state_baseline_invalid")
            for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
                if type(baseline[field]) is not int or not 0 <= baseline[field] <= 2**63 - 1:
                    raise GrokCLIUnavailable("usage_state_baseline_invalid")
            if baseline["cached_input_tokens"] > baseline["input_tokens"] or baseline["reasoning_tokens"] > baseline["output_tokens"]:
                raise GrokCLIUnavailable("usage_state_baseline_invalid")
            cost = baseline["provider_cost_usd"]
            if cost is not None and (type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0):
                raise GrokCLIUnavailable("usage_state_baseline_invalid")

    def reconcile(self, observation: UsageObservation) -> dict:
        if not isinstance(observation, UsageObservation):
            raise GrokCLIUnavailable("usage_observation_invalid")
        data = dict(observation.__dict__)
        checked = normalize_usage(data, process_epoch=observation.process_epoch,
            session_id=observation.session_id, request_id=observation.request_id,
            semantics={"scope": "whole_turn", "cache": "included", "reasoning": "included",
                "cumulative": observation.cumulative, "price_evidence_verified": observation.provider_cost_verified})
        if checked != observation:
            raise GrokCLIUnavailable("usage_observation_invalid")
        key = json.dumps([observation.process_epoch, observation.session_id, observation.request_id])
        fingerprint = hashlib.sha256(_canonical(data)).hexdigest()
        prior = self.state["seen"].get(key)
        if prior:
            if prior != fingerprint:
                raise GrokCLIUnavailable("duplicate_usage_conflict")
            return {"duplicate": True, "tokens": None, "provider_cost_usd": None}
        if len(self.state["seen"]) >= self.MAX_RECORDS:
            raise GrokCLIUnavailable("usage_state_bounds")
        epoch = json.dumps([observation.process_epoch, observation.session_id])
        fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
        modes = self.state.setdefault("modes", {})
        if epoch in modes and modes[epoch] != observation.cumulative:
            raise GrokCLIUnavailable("usage_mode_changed_same_epoch")
        baseline = self.state["baselines"].get(epoch, {})
        deltas = {}
        for name in fields:
            previous = baseline.get(name, 0) if observation.cumulative else 0
            delta = data[name] - previous
            if delta < 0:
                raise GrokCLIUnavailable("usage_reset_same_epoch")
            deltas[name] = delta
        if deltas["cached_input_tokens"] > deltas["input_tokens"] or deltas["reasoning_tokens"] > deltas["output_tokens"]:
            raise GrokCLIUnavailable("usage_delta_inconsistent")
        cost = observation.provider_cost_usd
        if observation.cumulative and cost is not None:
            if baseline and baseline.get("provider_cost_usd") is None:
                raise GrokCLIUnavailable("usage_cost_baseline_unknown")
            cost -= baseline.get("provider_cost_usd", 0) or 0
            if cost < 0:
                raise GrokCLIUnavailable("usage_cost_reset")
        self.state["seen"][key] = fingerprint
        modes[epoch] = observation.cumulative
        if observation.cumulative:
            self.state["baselines"][epoch] = data
        verified_cost = observation.provider_cost_verified
        if observation.cumulative and baseline:
            verified_cost = verified_cost and baseline.get("provider_cost_verified") is True
        return {"duplicate": False, "tokens": deltas, "provider_cost_usd": cost,
                "provider_cost_verified": verified_cost}
