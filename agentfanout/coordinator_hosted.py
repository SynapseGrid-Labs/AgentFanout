"""Opt-in hosted Local bridge over coordinator-owned identified inference.

No transport, conformance proof, resource reservation or authorization is selected
here. Hosts supply those authorities; preparation only freezes evidence. Legacy
Local endpoint and signed sandbox receipts retain their existing meanings.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from .coordinator_local import (CoordinatorError, CoordinatorUncertain,
    resource_from_reservation, mixed_resource_from_reservation, reconcile_identified_request)
from .local_profiles import (identity_digest, build_inference_request, parse_inference_response)
from .mixed_contracts import canonical_digest
from .provider_adapters import sanitized_prompt


def verify_boundary(value, *, run_id, task, role, binding, capability, assignment_id=None):
    keys = {'schema','run_id','task_id','role','request_id','reservation','observation',
            'profile','payload_sha256','adapter_identity_sha256','input_mode','tool_mode'}
    if not isinstance(value, dict) or set(value) != keys or value['schema'] != 'CoordinatorHostedBoundaryV1':
        raise CoordinatorError('coordinator_hosted_boundary_schema_invalid')
    if value['run_id'] != run_id or value['task_id'] != task['task_id'] or value['role'] != role:
        raise CoordinatorError('coordinator_hosted_task_role_mismatch')
    try:
        if str(uuid.UUID(value['request_id'])) != value['request_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise CoordinatorError('coordinator_hosted_request_invalid') from exc
    if (binding['provider'] != 'Local' or binding['mode'] != 'coordinator_local'
            or value['adapter_identity_sha256'] != binding['identity_sha256']
            or value['input_mode'] != 'bounded_file_text' or value['tool_mode'] != 'no_tools'):
        raise CoordinatorError('coordinator_hosted_binding_invalid')
    record = value['reservation']
    if record.get('run_id') != run_id or record.get('assignment_id') != (assignment_id or task['task_id']):
        raise CoordinatorError('coordinator_hosted_reservation_scope_invalid')
    resource = mixed_resource_from_reservation(record, value['observation'], role=role)
    if resource != binding['resource'] or record.get('reservation_cost_usd') != binding['reservation_cost_usd']:
        raise CoordinatorError('coordinator_hosted_reservation_binding_invalid')
    model = record['identity']
    if (model['logical_selector'] != binding['model_selector'] or model['served_name'] != binding['resolved_model']
            or capability.get('coordinator_boundary_sha256') != canonical_digest(value)
            or capability.get('exact_model_id') != model['served_name']
            or capability.get('reasoning_effort') != binding['reasoning_effort']):
        raise CoordinatorError('coordinator_hosted_capability_binding_invalid')
    # Verify profile/identity reciprocity, without making inference or observing GPU.
    build_inference_request(model, value['profile'], 'host profile verification')
    if not isinstance(value['payload_sha256'], str) or len(value['payload_sha256']) != 64 or any(c not in '0123456789abcdef' for c in value['payload_sha256']):
        raise CoordinatorError('coordinator_hosted_payload_digest_invalid')
    return value


class CoordinatorLocalWorkerAdapter:
    provider = 'Local'
    supports_context_reuse = False
    no_tool_retry_safe = False

    def __init__(self, *, broker, adapter_identity, reservation_observer,
                 host_verify, capability_factory, authorize, journal,
                 project_root=None, bounded_task_factory=None, timeout_seconds=120):
        if not 0 < timeout_seconds <= 120:
            raise CoordinatorError('coordinator_hosted_timeout_invalid')
        call_timeout = getattr(broker.transport, 'timeout_seconds', None)
        if isinstance(call_timeout, bool) or not isinstance(call_timeout, (int,float)) or not 0 < call_timeout <= 10:
            raise CoordinatorError('coordinator_hosted_transport_supervision_unproved')
        self.supervision_bound = timeout_seconds + 3 * call_timeout
        self.broker = broker
        self._identity = deepcopy(adapter_identity)
        self.reservation_observer = reservation_observer
        self.host_verify = host_verify
        self.capability_factory = capability_factory
        self.authorize = authorize
        self.journal = journal
        self.project_root = project_root
        self.bounded_task_factory = bounded_task_factory
        self.timeout_seconds = timeout_seconds

    def identity(self):
        return deepcopy(self._identity)

    def _bounded_task(self, task):
        from .grok_cli_hosted import GrokCLIWorkerAdapter
        return GrokCLIWorkerAdapter._bounded_task(self, task)

    def preflight(self, assignment):
        return {**assignment, '_bounded_task': self._bounded_task(assignment['task'])}

    def _prepare(self, task, policy, role, prompt):
        evidence = self.reservation_observer(task, role)
        if self.host_verify(deepcopy(evidence), role) is not True:
            raise CoordinatorError('coordinator_hosted_conformance_unproved')
        record, observation, profile = (deepcopy(evidence[k]) for k in ('reservation','observation','profile'))
        if self._identity.get('local_identity_sha256') != identity_digest(record['identity']):
            raise CoordinatorError('coordinator_hosted_frozen_artifact_identity_drift')
        mixed_resource_from_reservation(record, observation, role=role)
        if record['expires_at'] - time.time() < self.supervision_bound:
            raise CoordinatorError('coordinator_hosted_lease_too_short')
        if profile['max_output_tokens'] > task['budgets']['max_output_tokens']:
            raise CoordinatorError('coordinator_hosted_output_budget_exceeded')
        schema = json.loads((Path(__file__).resolve().parent.parent / 'schemas' /
            ('WorkerResultV1.schema.json' if role == 'worker' else 'ValidatorResultV1.schema.json')).read_text())
        payload = build_inference_request(record['identity'], profile, sanitized_prompt(prompt),
            schema if profile['constrained_output'] == 'json_schema' else None)
        controls = {k:v for k,v in payload.items() if k not in {'model','messages','stream','response_format'}}
        if observation.get('request_controls') != controls:
            raise CoordinatorError('coordinator_hosted_installed_controls_mismatch')
        if 'response_format' in payload and observation.get('schema_sha256',{}).get(role) != canonical_digest(schema):
            raise CoordinatorError('coordinator_hosted_installed_schema_unproved')
        boundary = {'schema':'CoordinatorHostedBoundaryV1','run_id':record['run_id'],
            'task_id':task['task_id'],'role':role,'request_id':str(uuid.uuid4()),
            'reservation':record,'observation':observation,'profile':profile,
            'payload_sha256':canonical_digest(payload),'adapter_identity_sha256':canonical_digest(self.identity()),
            'input_mode':'bounded_file_text','tool_mode':'no_tools'}
        capability = self.capability_factory(task, policy, deepcopy(evidence))
        capability = {**capability, 'coordinator_boundary_sha256':canonical_digest(boundary)}
        return {'resolved_model':record['identity']['served_name'],'capability':capability,
                'coordinator_hosted_boundary':boundary,'_coordinator_payload':payload}

    def prepare(self, assignment, tool_policy_sha256):
        task = dict(assignment['_bounded_task'])
        task['launch_correlation_id'] = assignment['launch_correlation_id']
        task['expected_result_id'] = 'result_' + assignment['launch_correlation_id'].removeprefix('launch_')
        prompt = json.dumps({'output_contract':'WorkerResultV1','task':task}, sort_keys=True)
        return self._prepare(assignment['host_task'], tool_policy_sha256, 'worker', prompt)

    def _execute(self, task, receipt, boundary, payload, role, confirm, cancelled, heartbeat):
        start = time.monotonic()
        record = boundary['reservation']
        identity = record['identity']
        binding = resource_from_reservation(record, role=role, current_identity=identity)
        if canonical_digest(payload) != boundary['payload_sha256']:
            raise CoordinatorError('coordinator_hosted_payload_drift')
        if cancelled and cancelled():
            raise CoordinatorError('coordinator_hosted_cancelled_before_dispatch')
        if self.host_verify({'reservation':record,'observation':boundary['observation'],'profile':boundary['profile']},role) is not True:
            raise CoordinatorError('coordinator_hosted_conformance_unproved')
        mixed_resource_from_reservation(record, boundary['observation'], role=role)
        request = {'request_id':boundary['request_id'],'reservation_id':record['reservation_id'],
                   'role':role,'identity_sha256':identity_digest(identity),'payload':payload}
        def authorize(operation, digest):
            return self.authorize(task, role, deepcopy(receipt), operation, digest) is True
        try:
            self.broker.execute('fanout-inference', request, authorize_effect=authorize,
                binding=binding, current_identity=identity, role=role, profile=boundary['profile'])
            deadline = start + self.timeout_seconds
            while True:
                response = self.broker.observe('fanout-request', {'identifier':request['request_id']})
                if response.get('stage') in {'completed','cancelled','uncertain'}:
                    break
                if (cancelled and cancelled()) or time.monotonic() >= deadline:
                    self.broker.execute('fanout-cancel', {'identifier':request['request_id']},
                        authorize_effect=authorize, binding=binding)
                    response = self.broker.observe('fanout-request', {'identifier':request['request_id']})
                    break
                if heartbeat:
                    heartbeat(datetime.now(timezone.utc).isoformat())
                time.sleep(.05)
            reconcile_identified_request(binding, response, request_id=request['request_id'])
            if self.journal(task, role, 'coordinator_request_reconciled', {
                'request_id':request['request_id'],'reservation_id':record['reservation_id'],
                'response_sha256':canonical_digest(response),'stage':response['stage'],
                'effects_absent':True,'accounting_reconciled':True,
                'observed_usage':{k:v for k,v in (response.get('response') or {}).get('usage', {}).items()
                    if k in {'prompt_tokens','completion_tokens','total_tokens'} and type(v) is int and v >= 0},
                'provider_cost_usd':None}) is not True:
                raise CoordinatorUncertain('coordinator_hosted_reconciliation_not_durable')
            if response['stage'] != 'completed':
                raise CoordinatorError('coordinator_hosted_request_cancelled')
            parsed = parse_inference_response(response['response'], identity, boundary['profile'])
            result = json.loads(parsed['content'])
            expected = 'WorkerResultV1' if role == 'worker' else 'ValidatorResultV1'
            if not isinstance(result,dict) or result.get('schema') != expected or result.get('task_id') != task['task_id']:
                raise CoordinatorError('coordinator_hosted_result_correlation_invalid')
            if role == 'worker' and result.get('launch_correlation_id') != receipt['launch_correlation_id']:
                raise CoordinatorError('coordinator_hosted_result_correlation_invalid')
            if any(result.get(k) not in (None,[]) for k in ('changed_files','commands','exit_codes')):
                raise CoordinatorError('coordinator_hosted_no_tool_claim_invalid')
            usage = parsed['usage']
            details = usage.get('prompt_tokens_details', {})
            cached = details.get('cached_tokens')
            out_details = usage.get('completion_tokens_details', {})
            reasoning = out_details.get('reasoning_tokens')
            if type(cached) is not int or not 0 <= cached <= usage['prompt_tokens'] or type(reasoning) is not int or not 0 <= reasoning <= usage['completion_tokens']:
                raise CoordinatorError('coordinator_hosted_usage_semantics_unproved')
            # Confirm host closure only after result and accounting acceptance.
            # Rejected/partial observations retain the numeric reservation.
            confirm()
            return {'resolved_model':identity['served_name'],'process_group_exit_confirmed':True,
                'duration_ms':int((time.monotonic()-start)*1000),'result':result,
                'usage':{'usage_verified':True,'cost_verified':False,'cost_usd':None,
                    'input_tokens':usage['prompt_tokens'],'output_tokens':usage['completion_tokens'],
                    'cached_tokens':cached,'uncached_input_tokens':usage['prompt_tokens']-cached,
                    'reasoning_tokens':reasoning,'visible_output_tokens':usage['completion_tokens']-reasoning}}
        except Exception:
            if self.journal(task,role,'coordinator_request_reconciliation_required',
                {'request_id':request['request_id'],'reservation_id':record['reservation_id'],'replay_authorized':False}) is not True:
                raise CoordinatorUncertain('coordinator_hosted_uncertainty_not_durable')
            raise

    def run(self, assignment, receipt, heartbeat):
        return self._execute(assignment['host_task'], receipt, receipt['coordinator_hosted_boundary'],
            assignment['_coordinator_payload'], 'worker', assignment['_confirm_process_group_exit'],
            assignment.get('_cancellation_requested'), heartbeat)


class CoordinatorLocalValidatorAdapter(CoordinatorLocalWorkerAdapter):
    def prepare(self, task, result, tool_policy_sha256):
        prompt = json.dumps({'output_contract':'ValidatorResultV1','task':self._bounded_task(task),
            'worker_result':result,'peer_results':task.get('_peer_worker_results',[])},sort_keys=True)
        return self._prepare(task, tool_policy_sha256, 'validator', prompt)

    def run(self, task, result):
        receipt = task['_validator_start_receipt']
        return self._execute(task, receipt, receipt['coordinator_hosted_boundary'],task['_coordinator_payload'],
            'validator',task['_confirm_process_group_exit'],task.get('_cancellation_requested'),None)
