import copy
import unittest
from agentfanout.evaluation_contracts import EvaluationError, compare_matched_samples, prepare_switch_window


def sample(scheduling):
    return {'schema': 'MatchedBenchmarkSampleV1', 'scheduling': scheduling, 'temperature_state': 'warm', 'task_input_sha256s': ['a' * 64, 'b' * 64], 'model_identity_sha256': 'c' * 64, 'profile_sha256': 'd' * 64, 'effort': 'medium', 'validator_identity_sha256': 'e' * 64, 'elapsed_seconds': 20 if scheduling == 'serial' else 10, 'queue_seconds': 1, 'load_seconds': 2, 'accepted_tasks': 2, 'total_tasks': 2, 'repair_tasks': 0, 'quality_score': 1, 'usage': {'input_tokens': 100, 'cached_input_tokens': 20, 'reasoning_tokens': 30, 'output_tokens': 50, 'provider_cost_usd': None, 'reservation_charge_usd': 0.5}, 'memory_high_water_by_domain': {'fixture-domain-a': 100, 'fixture-domain-b': 30}, 'subscription_quota': None, 'parent_context_tokens': 20, 'uncertain_cancellations': 0, 'evidence_sha256': 'f' * 64}


def status():
    return {'production_enabled': True, 'admission_closed': True, 'domains': [{'id': 'fixture-domain', 'observed': 'fixture-model-a', 'desired': 'fixture-model-a', 'uncertain': False, 'transitioning': False, 'active_work': [], 'gpu_observation_freshness': {'state': 'fresh', 'uncertain': False, 'sample_age_seconds': 1}}]}

class EvaluationTests(unittest.TestCase):
    def switch(self, observation, jobs=None):
        return prepare_switch_window(observation, domain_id='fixture-domain', target_capability='fixture-model-b', allowed_capabilities=frozenset({'fixture-model-b'}), identified_jobs=jobs or [], prior_serving_epoch='fixture-epoch')

    def test_candidate_never_executes_or_authorizes(self):
        candidate = self.switch(status())
        self.assertTrue(candidate['authorization_required']); self.assertFalse(candidate['executed'])
        self.assertEqual(candidate['prior_capability'], 'fixture-model-a')

    def test_unrelated_running_queued_unknown_work_blocks_switch(self):
        for stage in ('running', 'queued', 'uncertain', 'cancelled'):
            observation = status(); observation['domains'][0]['active_work'] = [{'id': 'unrelated-image', 'stage': stage}]
            with self.assertRaises(EvaluationError): self.switch(observation)

    def test_unknown_cancel_accounting_ownership_blocks_switch(self):
        job = {'job_id': 'fixture-job', 'domain_id': 'fixture-domain', 'stage': 'completed', 'ownership': 'certain', 'cancellation': 'not_requested', 'accounting_reconciled': True, 'effects_reconciled': True}
        self.switch(status(), [job])
        for change in [{'stage': 'running'}, {'cancellation': 'unknown'}, {'ownership': 'uncertain'}, {'accounting_reconciled': False}, {'effects_reconciled': False}, {'domain_id': 'other-domain'}]:
            bad = dict(job); bad.update(change)
            with self.assertRaises(EvaluationError): self.switch(status(), [bad])
        with self.assertRaises(EvaluationError): self.switch(status(), [job, job])

    def test_stale_or_uncertain_observation_blocks_switch(self):
        for change in [{'uncertain': True}, {'transitioning': True}, {'gpu_observation_freshness': {'state': 'unavailable'}}, {'gpu_observation_freshness': {'state': 'fresh', 'uncertain': False, 'sample_age_seconds': 40}}]:
            observation = status(); observation['domains'][0].update(change)
            with self.assertRaises(EvaluationError): self.switch(observation)
        observation = status(); observation['admission_closed'] = False
        with self.assertRaises(EvaluationError): self.switch(observation)

    def test_matched_fixture_is_not_savings_or_live_evidence(self):
        result = compare_matched_samples(sample('serial'), sample('concurrent'))
        self.assertEqual(result['serial_accepted_tasks_per_second'], .1)
        self.assertEqual(result['concurrent_accepted_tasks_per_second'], .2)
        self.assertFalse(result['live_provenance_verified']); self.assertFalse(result['savings_claim_authorized']); self.assertFalse(result['memory_domains_combined'])

    def test_models_profiles_effort_tasks_and_cold_warm_must_match(self):
        for field in ('model_identity_sha256', 'profile_sha256', 'validator_identity_sha256', 'effort', 'temperature_state', 'task_input_sha256s'):
            concurrent = sample('concurrent')
            concurrent[field] = ['b' * 64, 'c' * 64] if field == 'task_input_sha256s' else ('cold' if field == 'temperature_state' else 'f' * 64)
            with self.subTest(field=field), self.assertRaises(EvaluationError): compare_matched_samples(sample('serial'), concurrent)

    def test_complete_usage_cost_and_capacity_truth(self):
        for change in [{'cached_input_tokens': 101}, {'reasoning_tokens': 51}, {'reservation_charge_usd': None}, {'input_tokens': True}, {'provider_cost_usd': float('nan')}]:
            concurrent = sample('concurrent'); concurrent['usage'].update(change)
            with self.assertRaises(EvaluationError): compare_matched_samples(sample('serial'), concurrent)
        concurrent = sample('concurrent'); del concurrent['usage']['reasoning_tokens']
        with self.assertRaises(EvaluationError): compare_matched_samples(sample('serial'), concurrent)
        concurrent = sample('concurrent'); concurrent['memory_high_water_by_domain'] = {'combined': 130}
        with self.assertRaises(EvaluationError): compare_matched_samples(sample('serial'), concurrent)

    def test_cancellation_and_invalid_measurement_refuse_comparison(self):
        for change in [{'uncertain_cancellations': 1}, {'accepted_tasks': 3}, {'elapsed_seconds': 0}, {'quality_score': 2}, {'parent_context_tokens': -1}, {'queue_seconds': 100}]:
            concurrent = sample('concurrent'); concurrent.update(change)
            with self.assertRaises(EvaluationError): compare_matched_samples(sample('serial'), concurrent)
