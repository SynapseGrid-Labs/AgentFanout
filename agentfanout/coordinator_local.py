"""Host-only bounded adapter for an existing fixed authenticated coordinator CLI.

Deployment supplies the reviewed immutable transport argv. No keys, SSH route,
commands, endpoints or administrative operations are accepted from worker JSON.
This adapter does not create GPU ownership or synthesize missing reservations.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import selectors
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from .local_profiles import identity_digest, validate_identity, build_inference_request

MAX_INPUT = 400000
MAX_OUTPUT = 1000000
READS = frozenset({'status', 'transition', 'job', 'fanout-attest', 'fanout-reservation', 'fanout-request'})
OPERATIONS = READS | {'request', 'inference', 'cancel', 'fanout-reserve', 'fanout-inference', 'fanout-release', 'fanout-cancel'}

class CoordinatorError(ValueError):
    pass

class CoordinatorUncertain(CoordinatorError):
    """Effects may have occurred; retain their journal and never auto-replay."""

def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate coordinator JSON key')
        value[key] = item
    return value

def _reject_constant(value):
    raise ValueError('nonfinite coordinator JSON value')

def _uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError) as exc:
        raise CoordinatorError('coordinator_identifier_invalid') from exc

def _finite(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise CoordinatorError('coordinator_numeric_invalid')
    return float(value)

def validate_operation(operation: str, payload: dict[str, Any], capabilities: frozenset[str]) -> tuple[list[str], bytes]:
    if operation not in OPERATIONS or not isinstance(payload, dict):
        raise CoordinatorError('coordinator_operation_denied')
    arguments = [operation]
    body = None
    if operation == 'status':
        if payload:
            raise CoordinatorError('coordinator_status_shape_invalid')
    elif operation in {'transition', 'job', 'cancel', 'fanout-reservation', 'fanout-request', 'fanout-release', 'fanout-cancel'}:
        if set(payload) != {'identifier'}:
            raise CoordinatorError('coordinator_identifier_shape_invalid')
        arguments.append(_uuid(payload['identifier']))
    elif operation == 'fanout-attest':
        if set(payload) != {'capability'} or payload['capability'] not in capabilities:
            raise CoordinatorError('coordinator_capability_not_allowed')
        arguments.append(payload['capability'])
    elif operation == 'fanout-reserve':
        keys = {'reservation_id','run_id','assignment_id','capability','role','identity_sha256','seconds','reservation_cost_usd'}
        if set(payload) != keys or payload['capability'] not in capabilities or payload['role'] not in {'worker','validator'}:
            raise CoordinatorError('coordinator_role_reservation_invalid')
        _uuid(payload['reservation_id'])
        for key in ('run_id','assignment_id'):
            if not isinstance(payload[key],str) or not 1 <= len(payload[key]) <= 128:
                raise CoordinatorError('coordinator_assignment_binding_invalid')
        _finite(payload['seconds'],1,300)
        _finite(payload['reservation_cost_usd'],0,1000)
        if not isinstance(payload['identity_sha256'],str) or len(payload['identity_sha256']) != 64 or any(c not in '0123456789abcdef' for c in payload['identity_sha256']):
            raise CoordinatorError('coordinator_identity_digest_invalid')
        body = payload
    elif operation == 'fanout-inference':
        if set(payload) != {'request_id','reservation_id','role','identity_sha256','payload'} or payload['role'] not in {'worker','validator'}:
            raise CoordinatorError('coordinator_identified_inference_invalid')
        _uuid(payload['request_id']); _uuid(payload['reservation_id'])
        validate_operation('inference',payload['payload'],capabilities)
        body = payload
    elif operation == 'request':
        if payload.get('target') in {'off', 'maint'}:
            raise CoordinatorError('coordinator_administrative_capability_denied')
        if set(payload) != {'target', 'behavior', 'timeout', 'request_id'} or payload['target'] not in capabilities or payload['behavior'] not in {'wait', 'reject'}:
            raise CoordinatorError('coordinator_request_shape_invalid')
        _uuid(payload['request_id'])
        _finite(payload['timeout'], 1, 300)
        body = payload
    else:
        allowed = {'model', 'messages', 'stream', 'max_tokens', 'temperature', 'top_p', 'top_k', 'reasoning_effort', 'chat_template_kwargs', 'response_format'}
        required = {'model', 'messages', 'stream', 'max_tokens'}
        if not required <= set(payload) <= allowed or payload['model'] not in capabilities or payload['stream'] is not False:
            raise CoordinatorError('coordinator_inference_shape_invalid')
        messages = payload['messages']
        if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict) or set(messages[0]) != {'role', 'content'} or messages[0]['role'] != 'user' or not isinstance(messages[0]['content'], str) or not messages[0]['content'].strip():
            raise CoordinatorError('coordinator_bounded_text_required')
        _finite(payload['max_tokens'], 1, 32768)
        if not isinstance(payload['max_tokens'], int):
            raise CoordinatorError('coordinator_output_limit_invalid')
        body = payload
    try:
        raw = b'' if body is None else json.dumps(body, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
    except (TypeError, ValueError) as exc:
        raise CoordinatorError('coordinator_json_invalid') from exc
    if len(raw) > MAX_INPUT:
        raise CoordinatorError('coordinator_request_too_large')
    return arguments, raw

@dataclass(frozen=True)
class FixedClientTransport:
    """A host-selected CLI invocation, never a worker-controlled command template.

    For SSH, the fixed prefix must end in the reviewed remote CLI command.
    Only allowlisted operation words, UUIDs and bounded numeric flags append.
    The digest is reviewed deployment data, not an independent trust authority.
    """
    argv: tuple[str, ...]
    invocation_sha256: str
    timeout_seconds: float = 120

    def __post_init__(self):
        if not isinstance(self.argv, tuple) or not self.argv or any(not isinstance(s, str) or not s or '\x00' in s or '\n' in s for s in self.argv):
            raise CoordinatorError('coordinator_transport_invalid')
        digest = hashlib.sha256(json.dumps(self.argv, separators=(',', ':')).encode()).hexdigest()
        if digest != self.invocation_sha256:
            raise CoordinatorError('coordinator_transport_pin_mismatch')
        _finite(self.timeout_seconds, 1, 300)

    def call(self, arguments: list[str], raw: bytes) -> dict[str, Any]:
        # Input is bounded before spawning; outputs are read incrementally.
        command = [*self.argv, *arguments, '--timeout', str(int(self.timeout_seconds))]
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, start_new_session=True)
        selector = None
        output = bytearray()
        count = 0
        try:
            selector = selectors.DefaultSelector()
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            # Nonblocking stdin avoids waiting on a client that never consumes it.
            os.set_blocking(process.stdin.fileno(), False)
            if raw:
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                process.stdin.close()
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            offset = 0
            end = time.monotonic() + self.timeout_seconds
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise CoordinatorUncertain('coordinator_timeout_effects_unknown')
                for key, _ in selector.select(min(remaining, .1)):
                    if key.data == 'stdin':
                        try:
                            offset += os.write(key.fileobj.fileno(), raw[offset:offset + 65536])
                        except BrokenPipeError:
                            offset = len(raw)
                        if offset == len(raw):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    count += len(chunk)
                    if count > MAX_OUTPUT:
                        raise CoordinatorUncertain('coordinator_output_limit_effects_unknown')
                    if key.data == 'stdout':
                        output.extend(chunk)
            try:
                returncode = process.wait(timeout=max(.01, end - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise CoordinatorUncertain('coordinator_timeout_effects_unknown') from exc
            if returncode != 0:
                raise CoordinatorUncertain('coordinator_failed_effects_unknown')
            try:
                result = json.loads(output, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise CoordinatorUncertain('coordinator_response_invalid_effects_unknown') from exc
            if not isinstance(result, dict):
                raise CoordinatorUncertain('coordinator_response_shape_invalid')
            return result
        except CoordinatorUncertain:
            raise
        except (KeyboardInterrupt, SystemExit):
            # Preserve interruption; finally still cleans the owned local client.
            raise
        except Exception as exc:
            raise CoordinatorUncertain('coordinator_transport_effects_unknown') from exc
        finally:
            # Cleanup failures must not bypass uncertainty or mask cancellation.
            import sys
            active_error = sys.exc_info()[0] is not None
            cleanup_error = None
            if selector is not None:
                try:
                    selector.close()
                except Exception as exc:
                    cleanup_error = exc
            try:
                if process.poll() is None:
                    # Only the launched client group; not remote cancellation.
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=2)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception as exc:
                            cleanup_error = cleanup_error or exc
            if cleanup_error is not None and not active_error:
                raise CoordinatorUncertain('coordinator_cleanup_effects_unknown') from cleanup_error


def observed_readiness(status: dict[str, Any], domain_id: str, capability: str) -> dict[str, Any]:
    """Status is observation only, never a reservation or execution receipt."""
    if not isinstance(status, dict) or status.get('production_enabled') is not True or status.get('admission_closed') is not False:
        raise CoordinatorError('coordinator_admission_unavailable')
    domains = status.get('domains')
    if not isinstance(domains, list):
        raise CoordinatorError('coordinator_domain_observation_missing')
    matching = [d for d in domains if isinstance(d, dict) and d.get('id') == domain_id]
    if len(matching) != 1:
        raise CoordinatorError('coordinator_domain_observation_missing')
    domain = matching[0]
    freshness = domain.get('gpu_observation_freshness', {})
    if domain.get('uncertain') is not False or domain.get('transitioning') is not False or domain.get('admission_closed') is not False or domain.get('observed') != capability or domain.get('desired') != capability or freshness.get('state') != 'fresh' or freshness.get('uncertain') is not False:
        raise CoordinatorError('coordinator_domain_not_ready')
    _finite(freshness.get('sample_age_seconds'), 0, 30)
    if domain.get('active_work') != []:
        raise CoordinatorError('coordinator_domain_contention')
    return domain

@dataclass
class ResourceBinding:
    """Evidence supplied by the coordinator; no AgentFanout shadow ownership."""
    identity: dict[str, Any]
    evidence: dict[str, Any]
    quarantined: bool = field(default=False, init=False)

    def verify(self, now: float, role: str, current_identity: dict[str, Any]) -> None:
        validate_identity(self.identity)
        fields = {'schema', 'reservation_id', 'owner_token', 'serving_epoch', 'identity_sha256', 'role', 'expires_at', 'cancellation_state', 'coordinator_attestation_sha256'}
        e = self.evidence
        if self.quarantined or not isinstance(e, dict) or set(e) != fields or e.get('schema') != 'CoordinatorResourceBindingV1':
            raise CoordinatorError('coordinator_resource_evidence_unavailable')
        _uuid(e['reservation_id'])
        for name in ('owner_token', 'serving_epoch'):
            if not isinstance(e[name], str) or not 1 <= len(e[name]) <= 256:
                raise CoordinatorError('coordinator_resource_identity_invalid')
        for name in ('identity_sha256', 'coordinator_attestation_sha256'):
            if not isinstance(e[name], str) or len(e[name]) != 64 or any(c not in '0123456789abcdef' for c in e[name]):
                raise CoordinatorError('coordinator_resource_attestation_missing')
        if role not in {'worker', 'validator'} or e['role'] != role or e['identity_sha256'] != identity_digest(self.identity) or identity_digest(current_identity) != e['identity_sha256'] or e['serving_epoch'] != self.identity['serving_epoch']:
            raise CoordinatorError('coordinator_resource_epoch_or_role_mismatch')
        if _finite(e['expires_at'], 0, 1e12) <= _finite(now, 0, 1e12) or e['cancellation_state'] != 'none':
            raise CoordinatorError('coordinator_resource_expired_or_uncertain')

    def reconcile_cancellation(self, response: dict[str, Any]) -> None:
        # The fixed CLI's identified job cancellation does not prove synchronous
        # inference cancellation. Require explicit effects/accounting correlation.
        confirmed = isinstance(response, dict) and response.get('reservation_id') == self.evidence.get('reservation_id') and response.get('serving_epoch') == self.evidence.get('serving_epoch') and response.get('effects_absent') is True and response.get('accounting_reconciled') is True
        self.quarantined = not confirmed
        self.evidence['cancellation_state'] = 'confirmed' if confirmed else 'uncertain'
        if not confirmed:
            raise CoordinatorUncertain('coordinator_cancellation_quarantined')

class CoordinatorBroker:
    def __init__(self, transport: FixedClientTransport, capabilities: frozenset[str]):
        if not isinstance(capabilities, frozenset) or not capabilities or any(not isinstance(c, str) or not c or len(c) > 64 or any(x not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for x in c) for c in capabilities):
            raise CoordinatorError('coordinator_capability_allowlist_invalid')
        if capabilities & {'off', 'maint'}:
            raise CoordinatorError('coordinator_administrative_capability_denied')
        self.transport, self.capabilities = transport, capabilities

    def observe(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in READS:
            raise CoordinatorError('coordinator_read_operation_required')
        arguments, raw = validate_operation(operation, payload, self.capabilities)
        response = self.transport.call(arguments, raw)
        if operation in {'transition', 'job'} and response.get('id') != payload['identifier']:
            raise CoordinatorError('coordinator_observation_correlation_mismatch')
        if operation in {'fanout-reservation','fanout-request'} and response.get('reservation_id' if operation == 'fanout-reservation' else 'request_id') != payload['identifier']:
            raise CoordinatorError('coordinator_observation_correlation_mismatch')
        return response

    def execute(self, operation: str, payload: dict[str, Any], *, authorize_effect: Any, binding: ResourceBinding | None = None, current_identity: dict[str, Any] | None = None, role: str = 'worker', profile: dict[str, Any] | None = None) -> dict[str, Any]:
        if operation in READS:
            raise CoordinatorError('coordinator_effect_operation_required')
        arguments, raw = validate_operation(operation, payload, self.capabilities)
        identified_payload = payload
        if operation == 'fanout-inference':
            if binding is None or payload['reservation_id'] != binding.evidence['reservation_id'] or payload['role'] != role or payload['identity_sha256'] != identity_digest(binding.identity):
                raise CoordinatorError('coordinator_identified_resource_mismatch')
            payload = payload['payload']
        if operation in {'inference','fanout-inference'}:
            if binding is None or current_identity is None:
                raise CoordinatorError('coordinator_resource_evidence_unavailable')
            binding.verify(time.time(), role, current_identity)
            if payload['model'] != binding.identity['logical_selector']:
                raise CoordinatorError('coordinator_resource_model_mismatch')
            if profile is None:
                raise CoordinatorError('coordinator_attested_profile_required')
            packet_schema = None
            if 'response_format' in payload:
                fmt = payload['response_format']
                if not isinstance(fmt, dict) or not isinstance(fmt.get('json_schema'), dict):
                    raise CoordinatorError('coordinator_schema_format_invalid')
                packet_schema = fmt['json_schema'].get('schema')
                if packet_schema is None:
                    raise CoordinatorError('coordinator_schema_format_invalid')
            expected = build_inference_request(binding.identity, profile, payload['messages'][0]['content'], packet_schema)
            if payload != expected:
                raise CoordinatorError('coordinator_inference_profile_mismatch')
        # Host callback must durably authorize AND journal before any process spawn.
        # The broker cannot generate its own receipt or decide bootstrap is harmless.
        digest = hashlib.sha256(raw or json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if not callable(authorize_effect) or authorize_effect(operation, digest) is not True:
            raise CoordinatorError('coordinator_durable_host_authorization_required')
        try:
            response = self.transport.call(arguments, raw)
            if not isinstance(response, dict):
                raise CoordinatorUncertain('coordinator_response_shape_invalid')
            correlation = {'fanout-reserve':('reservation_id',identified_payload.get('reservation_id')),'fanout-inference':('request_id',identified_payload.get('request_id')),'fanout-release':('reservation_id',identified_payload.get('identifier')),'fanout-cancel':('request_id',identified_payload.get('identifier'))}.get(operation)
            if correlation is not None and response.get(correlation[0]) != correlation[1]:
                raise CoordinatorUncertain('coordinator_effect_correlation_unknown')
            if operation == 'request':
                transition_id = response.get('transition_id')
                try:
                    _uuid(transition_id)
                except CoordinatorError as exc:
                    raise CoordinatorUncertain('coordinator_transition_correlation_unknown') from exc
                if response.get('status_url') != '/transitions/' + transition_id:
                    raise CoordinatorUncertain('coordinator_transition_correlation_unknown')
            return response
        except (KeyboardInterrupt, SystemExit):
            if binding is not None:
                binding.quarantined = True
                binding.evidence['cancellation_state'] = 'uncertain'
            raise
        except Exception as exc:
            if binding is not None:
                binding.quarantined = True
                # Persistable evidence must refuse reuse after restart too.
                binding.evidence['cancellation_state'] = 'uncertain'
            if isinstance(exc, CoordinatorUncertain):
                raise
            raise CoordinatorUncertain('coordinator_transport_effects_unknown') from exc


def resource_from_reservation(record: dict[str, Any], *, role: str, current_identity: dict[str, Any]) -> ResourceBinding:
    """Translate independently observed coordinator evidence, never ownership."""
    if not isinstance(record,dict) or record.get('schema') != 'CoordinatorRoleReservationV1' or record.get('state') != 'reserved' or record.get('role') != role:
        raise CoordinatorError('coordinator_role_reservation_unavailable')
    model = validate_identity(record.get('identity'))
    e = {'schema':'CoordinatorResourceBindingV1','reservation_id':record.get('reservation_id'),
         'owner_token':record.get('owner_token'),'serving_epoch':record.get('serving_epoch'),
         'identity_sha256':record.get('identity_sha256'),'role':role,'expires_at':record.get('expires_at'),
         'cancellation_state':record.get('cancellation_state'),
         'coordinator_attestation_sha256':record.get('coordinator_attestation_sha256')}
    binding = ResourceBinding(model,e)
    binding.verify(time.time(),role,current_identity)
    return binding


def reconcile_identified_request(binding: ResourceBinding, response: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    """No timeout, local exit, or cancel acknowledgement proves remote absence."""
    if (not isinstance(response,dict) or response.get('schema') != 'CoordinatorInferenceRequestV1'
            or response.get('reservation_id') != binding.evidence['reservation_id']
            or response.get('identity_sha256') != binding.evidence['identity_sha256']
            or response.get('role') != binding.evidence['role']):
        raise CoordinatorUncertain('coordinator_identified_response_mismatch')
    _uuid(request_id)
    if response.get('request_id') != request_id or response.get('serving_epoch') != binding.evidence['serving_epoch']:
        raise CoordinatorUncertain('coordinator_identified_response_mismatch')
    if response.get('stage') not in {'completed','cancelled'} or response.get('effects_absent') is not True or response.get('accounting_reconciled') is not True:
        binding.quarantined = True
        binding.evidence['cancellation_state'] = 'uncertain'
        raise CoordinatorUncertain('coordinator_identified_effects_unknown')
    usage = response.get('usage')
    if response['stage'] == 'completed':
        usage = response.get('response',{}).get('usage')
    elif usage is None:
        # Cancellation before durable begin proves no inference, not zero
        # reservation cost. Numeric host reservation authority is unchanged.
        if response.get('response') is not None or response.get('started') is not False or response.get('cancel_requested') is not True:
            raise CoordinatorUncertain('coordinator_cancelled_usage_unknown')
        return response
    if not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('prompt_tokens','completion_tokens','total_tokens')) or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
        raise CoordinatorUncertain('coordinator_identified_accounting_unknown')
    return response


def mixed_resource_from_reservation(record: dict[str, Any], observation: dict[str, Any], *, role: str) -> dict[str, Any]:
    """Translate a fresh host-owned observation into the mixed role contract."""
    if (not isinstance(observation,dict) or observation.get('ready') is not True or observation.get('certain') is not True
            or observation.get('owner_token') != record.get('owner_token')
            or observation.get('fencing_token') != record.get('fencing_token')
            or observation.get('attestation_sha256') != record.get('coordinator_attestation_sha256')):
        raise CoordinatorError('coordinator_current_role_observation_unproved')
    _finite(observation.get('observed_at'),time.time()-30,time.time()+1)
    binding = resource_from_reservation(record,role=role,current_identity=observation.get('identity'))
    return {'reservation_id':binding.evidence['reservation_id'],'domain':binding.identity['resource_domain'],
            'lease_id':binding.evidence['owner_token'],'serving_epoch':binding.evidence['serving_epoch'],
            'fencing_token':record['fencing_token'],'readiness':'ready','ownership':'certain',
            'cancellation':binding.evidence['cancellation_state'],'expires_at':binding.evidence['expires_at'],'purpose':role}
