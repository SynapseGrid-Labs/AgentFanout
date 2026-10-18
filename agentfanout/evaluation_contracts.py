"""Offline U19/U20 evidence validation; never switches GPUs or runs inference.

These checks prepare a reviewable controlled window and matched experiment.
They do not confer authorization or turn submitted measurements into conformance.
"""
from __future__ import annotations
import hashlib
import json
import math
from typing import Any

class EvaluationError(ValueError):
    pass

def _number(value: Any, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise EvaluationError('evaluation_numeric_invalid')
    return float(value)

def _digest(value: Any) -> str:
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise EvaluationError('evaluation_json_invalid') from exc

def prepare_switch_window(status: dict[str, Any], *, domain_id: str, target_capability: str, allowed_capabilities: frozenset[str], identified_jobs: list[dict[str, Any]], prior_serving_epoch: str) -> dict[str, Any]:
    """Require positively reconciled identified work before proposing a request.

    No remote effect occurs here. Actual coordinator drain/release is external;
    a public caller cannot substitute this evidence for host authorization.
    """
    if not isinstance(status, dict) or status.get('admission_closed') is not True or status.get('production_enabled') is not True:
        raise EvaluationError('switch_window_admission_not_closed')
    if target_capability not in allowed_capabilities or not isinstance(prior_serving_epoch, str) or not prior_serving_epoch:
        raise EvaluationError('switch_window_target_or_epoch_invalid')
    domains = status.get('domains')
    if not isinstance(domains, list):
        raise EvaluationError('switch_window_domain_missing')
    selected = [d for d in domains if isinstance(d, dict) and d.get('id') == domain_id]
    if len(selected) != 1:
        raise EvaluationError('switch_window_domain_missing')
    domain = selected[0]
    if domain.get('uncertain') is not False or domain.get('transitioning') is not False or domain.get('active_work') != [] or domain.get('desired') != domain.get('observed'):
        raise EvaluationError('switch_window_domain_unreconciled')
    freshness = domain.get('gpu_observation_freshness', {})
    if freshness.get('state') != 'fresh' or freshness.get('uncertain') is not False or _number(freshness.get('sample_age_seconds')) > 30:
        raise EvaluationError('switch_window_observation_stale')
    if not isinstance(identified_jobs, list):
        raise EvaluationError('switch_window_jobs_invalid')
    ids = set()
    for job in identified_jobs:
        fields = {'job_id', 'domain_id', 'stage', 'ownership', 'cancellation', 'accounting_reconciled', 'effects_reconciled'}
        if not isinstance(job, dict) or set(job) != fields or not isinstance(job['job_id'], str) or not job['job_id'] or job['job_id'] in ids:
            raise EvaluationError('switch_window_job_identity_invalid')
        ids.add(job['job_id'])
        if job['domain_id'] != domain_id or job['stage'] not in {'completed', 'failed', 'cancelled'} or job['ownership'] != 'certain' or job['cancellation'] not in {'not_requested', 'confirmed'} or job['accounting_reconciled'] is not True or job['effects_reconciled'] is not True:
            raise EvaluationError('switch_window_job_unreconciled')
    return {'schema': 'ControlledSwitchCandidateV1', 'domain_id': domain_id, 'prior_capability': domain['observed'], 'target_capability': target_capability, 'prior_serving_epoch': prior_serving_epoch, 'observation_sha256': _digest(status), 'identified_jobs_sha256': _digest(identified_jobs), 'operation': 'capability_request', 'authorization_required': True, 'executed': False, 'rollback': 'close admission; reconcile exact jobs/effects; request prior capability through coordinator; observe new serving epoch'}


def compare_matched_samples(serial: dict[str, Any], concurrent: dict[str, Any]) -> dict[str, Any]:
    """Compare matched scheduling samples without claiming live provenance/savings.

    Routing experiments are intentionally distinct. Input/cache/reasoning/output
    are independent observations; cache/reasoning are subsets of input/output.
    """
    keys = {'schema', 'scheduling', 'temperature_state', 'task_input_sha256s', 'model_identity_sha256', 'profile_sha256', 'effort', 'validator_identity_sha256', 'elapsed_seconds', 'queue_seconds', 'load_seconds', 'accepted_tasks', 'total_tasks', 'repair_tasks', 'quality_score', 'usage', 'memory_high_water_by_domain', 'subscription_quota', 'parent_context_tokens', 'uncertain_cancellations', 'evidence_sha256'}
    for sample, scheduling in ((serial, 'serial'), (concurrent, 'concurrent')):
        if not isinstance(sample, dict) or set(sample) != keys or sample.get('schema') != 'MatchedBenchmarkSampleV1' or sample['scheduling'] != scheduling:
            raise EvaluationError('benchmark_sample_schema_invalid')
        if sample['temperature_state'] not in {'warm', 'cold'}:
            raise EvaluationError('benchmark_temperature_state_invalid')
        tasks = sample['task_input_sha256s']
        if not isinstance(tasks, list) or not tasks or len(tasks) != sample['total_tasks'] or any(not isinstance(d, str) or len(d) != 64 or any(c not in '0123456789abcdef' for c in d) for d in tasks):
            raise EvaluationError('benchmark_task_identity_invalid')
        for name in ('model_identity_sha256', 'profile_sha256', 'validator_identity_sha256', 'evidence_sha256'):
            digest = sample[name]
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise EvaluationError('benchmark_digest_invalid')
        for name in ('queue_seconds', 'load_seconds', 'quality_score', 'parent_context_tokens', 'uncertain_cancellations', 'accepted_tasks', 'total_tasks', 'repair_tasks'):
            _number(sample[name])
        _number(sample['elapsed_seconds'], positive=True)
        for name in ('accepted_tasks', 'total_tasks', 'repair_tasks', 'parent_context_tokens', 'uncertain_cancellations'):
            if not isinstance(sample[name], int) or isinstance(sample[name], bool):
                raise EvaluationError('benchmark_count_invalid')
        if sample['accepted_tasks'] > sample['total_tasks'] or sample['repair_tasks'] > sample['total_tasks'] or sample['quality_score'] > 1 or sample['queue_seconds'] + sample['load_seconds'] > sample['elapsed_seconds']:
            raise EvaluationError('benchmark_measurements_inconsistent')
        usage = sample['usage']
        if not isinstance(usage, dict) or set(usage) != {'input_tokens', 'cached_input_tokens', 'reasoning_tokens', 'output_tokens', 'provider_cost_usd', 'reservation_charge_usd'}:
            raise EvaluationError('benchmark_complete_usage_required')
        for name in ('input_tokens', 'cached_input_tokens', 'reasoning_tokens', 'output_tokens'):
            _number(usage[name])
            if not isinstance(usage[name], int) or isinstance(usage[name], bool):
                raise EvaluationError('benchmark_token_count_invalid')
        if usage['cached_input_tokens'] > usage['input_tokens'] or usage['reasoning_tokens'] > usage['output_tokens']:
            raise EvaluationError('benchmark_usage_semantics_inconsistent')
        if usage['provider_cost_usd'] is not None:
            _number(usage['provider_cost_usd'])
        _number(usage['reservation_charge_usd'])
        memory = sample['memory_high_water_by_domain']
        if not isinstance(memory, dict) or not memory or any(not isinstance(domain, str) or not domain for domain in memory):
            raise EvaluationError('benchmark_memory_domains_required')
        for value in memory.values(): _number(value)
        quota = sample['subscription_quota']
        if quota is not None:
            if not isinstance(quota, dict) or set(quota) != {'before', 'after', 'unit'} or not isinstance(quota['unit'], str) or not quota['unit']:
                raise EvaluationError('benchmark_quota_invalid')
            _number(quota['before']); _number(quota['after'])
    matched = ('temperature_state', 'task_input_sha256s', 'model_identity_sha256', 'profile_sha256', 'effort', 'validator_identity_sha256')
    for name in matched:
        left, right = serial[name], concurrent[name]
        if (sorted(left) if name == 'task_input_sha256s' else left) != (sorted(right) if name == 'task_input_sha256s' else right):
            raise EvaluationError('benchmark_unmatched:' + name)
    if set(serial['memory_high_water_by_domain']) != set(concurrent['memory_high_water_by_domain']):
        raise EvaluationError('benchmark_resource_domain_mismatch')
    if serial['uncertain_cancellations'] or concurrent['uncertain_cancellations']:
        raise EvaluationError('benchmark_cancellation_unreconciled')
    return {'schema': 'MatchedBenchmarkComparisonV1', 'serial_accepted_tasks_per_second': serial['accepted_tasks'] / serial['elapsed_seconds'], 'concurrent_accepted_tasks_per_second': concurrent['accepted_tasks'] / concurrent['elapsed_seconds'], 'quality_delta': concurrent['quality_score'] - serial['quality_score'], 'repair_tasks_delta': concurrent['repair_tasks'] - serial['repair_tasks'], 'evidence_sha256s': [serial['evidence_sha256'], concurrent['evidence_sha256']], 'live_provenance_verified': False, 'savings_claim_authorized': False, 'memory_domains_combined': False}
