"""Synthetic accounting contract tests, NOT model evaluation results."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

location = Path(__file__).resolve().parents[1] / 'benchmarks' / 'v2_pilot.py'
loader = importlib.util.spec_from_file_location('v2_pilot', location)
pilot = importlib.util.module_from_spec(loader)
loader.loader.exec_module(pilot)


def fixture():
    spec = pilot.manifest()
    runs, scores = [], []
    for item in pilot.schedule(spec):
        task = next(t for t in spec['tasks'] if t['id'] == item['task_id'])
        runs.append({'run_id': item['run_id'], 'manifest_sha256': spec['manifest_sha256'],
                     'provenance': {'kind': 'test_fixture', 'host': 'synthetic', 'runtime_revision': 'test',
                                    'executor': 'fixture-generator', 'started_at': '2026-01-01T00:00:00Z',
                                    'finished_at': '2026-01-01T00:00:10Z'},
                     'latency_seconds': 10, 'duplicate_jobs': 0,
                     'evidence': [{'path': 'synthetic.txt', 'sha256': 'a' * 64}],
                     'usage': [{'event_id': item['run_id'], 'host': 'synthetic', 'provider': 'synthetic', 'model': 'synthetic',
                                'role': 'parent', 'cost_basis': 'actual_charge', 'material_misroute': False,
                                'compensation_worker': False, 'retry_count': 0,
                                'request_id': item['run_id'], 'phase': 'execution', 'cost_usd':
                                1 if item['arm'] == 'baseline' else .8, 'input_tokens': 10, 'output_tokens': 10}],
                     'validators': [{'name': v, 'status': 'passed'} for v in task['validators']],
                     'acceptance_results': [{'id': c['id'], 'passed': True, 'evidence_sha256': 'a' * 64,
                                             'check_sha256': pilot.digest(c['code'].encode())}
                                            for c in task['acceptance_checks']],
                     'reconciliation': {'complete': True, 'reviewer': 'synthetic-reviewer',
                                        'inventory_evidence_sha256': 'a' * 64,
                                        'host_requests': [{'host': 'synthetic', 'provider': 'synthetic',
                                                           'request_id': item['run_id'], 'role': 'parent'}],
                                        'zero_request_role_evidence': {'synthesis': 'a' * 64}}})
        scores.append({'blind_id': item['blind_id'], 'blinded': True, 'scorer': 'synthetic-reviewer',
                       'quality': 1, 'accepted': True, 'evidence_sha256': 'a' * 64})
    return runs, scores


class PilotTests(unittest.TestCase):
    def test_manifest_and_alternating_schedule(self):
        spec = pilot.manifest()
        self.assertEqual(len(spec['tasks']), 12)
        plan = pilot.schedule(spec)
        self.assertEqual(len(plan), 60)
        self.assertEqual(plan[0]['arm'], 'baseline')
        self.assertEqual(plan[5]['arm'], 'lifecycle_hook')
        for task in spec['tasks']:
            self.assertTrue(task['acceptance_checks'])
            for check in task['acceptance_checks']:
                compile(check['code'], task['id'], 'exec')

    def test_synthetic_never_promotes(self):
        runs, scores = fixture()
        with self.assertRaises(ValueError):
            pilot.analyze(runs, scores)
        report = pilot.analyze(runs, scores, True)
        self.assertFalse(report['promotion_eligible'])
        self.assertTrue(report['comparisons']['lifecycle_hook']['pass'])
        self.assertEqual(report['result_kind'], 'TEST_FIXTURE_NOT_MEASURED')

    def test_unknown_not_zero(self):
        runs, scores = fixture()
        runs[-1]['usage'][0]['cost_usd'] = None
        report = pilot.analyze(runs, scores, True)
        self.assertIn('unknown usage', report['failures'][0])
        self.assertFalse(report['comparisons']['lifecycle_hook']['pass'])

    def test_duplicate_and_astra_block(self):
        runs, scores = fixture()
        runs[0]['duplicate_jobs'] = 1
        runs[0]['usage'][0]['model'] = 'gpt-6-astra'
        report = pilot.analyze(runs, scores, True)
        self.assertEqual(len(report['failures']), 2)
        self.assertFalse(report['comparisons']['lifecycle_hook']['pass'])

    def test_skipped_requires_separate_accounting(self):
        runs, scores = fixture()
        runs[0]['validators'][0]['status'] = 'skipped'
        with self.assertRaises(ValueError):
            pilot.analyze(runs, scores, True)
        audit = copy.deepcopy(runs[0]['usage'][0])
        audit.update(event_id='audit', request_id='audit', phase='skipped_validator_audit', cost_usd=.1, role='audit')
        runs[0]['usage'].append(audit)
        runs[0]['reconciliation']['host_requests'].append({'host': 'synthetic', 'provider': 'synthetic',
                                                        'request_id': 'audit', 'role': 'audit'})
        runs[0]['validators'][0]['audit'] = {'result': 'passed', 'evidence_sha256': 'a' * 64,
                                           'usage_event_id': 'audit'}
        pilot.analyze(runs, scores, True)

    def test_quality_regression_blocks(self):
        runs, scores = fixture()
        scores[4]['quality'] = .9
        report = pilot.analyze(runs, scores, True)
        self.assertFalse(report['comparisons']['lifecycle_hook']['quality_gate'])

    def test_completeness_and_provenance(self):
        runs, scores = fixture()
        with self.assertRaises(ValueError):
            pilot.analyze(runs[:-1], scores, True)
        runs[0]['provenance']['kind'] = 'measured'
        with self.assertRaises(ValueError):
            pilot.analyze(runs, scores, True)

    def test_prepare_and_verified_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(pilot.prepare(root / 'prepared')['prepared'], 60)
            runs, _ = fixture()
            evidence = root / 'evidence.txt'
            evidence.write_text(json.dumps({'requests': runs[0]['reconciliation']['host_requests'],
                                            'terminal': True,
                                            'started_at': runs[0]['provenance']['started_at'],
                                            'finished_at': runs[0]['provenance']['finished_at']}), encoding='utf-8')
            runs[0]['evidence'] = [{'path': 'evidence.txt', 'sha256': pilot.digest(evidence.read_bytes())}]
            runs[0]['reconciliation']['inventory_evidence_sha256'] = runs[0]['evidence'][0]['sha256']
            runs[0]['reconciliation']['zero_request_role_evidence']['synthesis'] = runs[0]['evidence'][0]['sha256']
            runs[0]['acceptance_results'][0]['evidence_sha256'] = runs[0]['evidence'][0]['sha256']
            source = root / 'run.json'
            source.write_text(json.dumps(runs[0]), encoding='utf-8')
            pilot.import_run(source, root / 'store', root)
            with self.assertRaises(FileExistsError):
                pilot.import_run(source, root / 'store', root)
            evidence.write_text('tampered', encoding='utf-8')
            with self.assertRaises(ValueError):
                pilot.import_run(source, root / 'other-store', root)

    def test_cost_basis_cannot_mix(self):
        runs, scores = fixture()
        runs[1]['usage'][0]['cost_basis'] = 'api_equivalent_estimate'
        with self.assertRaisesRegex(ValueError, 'cannot be compared'):
            pilot.analyze(runs, scores, True)

    def test_measured_unknown_model_rejected(self):
        runs, _ = fixture()
        run = runs[0]
        run['provenance'].update(kind='measured', enabled_features=['accounting'])
        event = run['usage'][0]
        event.update(model_observed=True, evidence_sha256='a' * 64)
        for identity in ('unknown', ' UNKNOWN ', 'unavailable', 'unspecified', 'null', 'n/a'):
            event['model'] = identity
            with self.assertRaisesRegex(ValueError, 'known model identity'):
                pilot.validate_run(run, pilot.manifest())
        event['model'] = 'none'
        with self.assertRaisesRegex(ValueError, 'local overhead'):
            pilot.validate_run(run, pilot.manifest())
        event['model'] = 'gpt-6-sol'
        pilot.validate_run(run, pilot.manifest())

    def test_latency_reconciles_execution_window(self):
        runs, _ = fixture()
        for duration in (0, 1, 123456):
            runs[0]['latency_seconds'] = duration
            with self.assertRaisesRegex(ValueError, 'execution window'):
                pilot.validate_run(runs[0], pilot.manifest())
        runs[0]['latency_seconds'] = 10
        pilot.validate_run(runs[0], pilot.manifest())

    def test_measured_host_runtime_and_capability_contract(self):
        runs, scores = fixture()
        spec = pilot.manifest()
        for run in runs:
            run['provenance'].update(kind='measured', capability_snapshot_id='snapshot-1',
                                     capability_snapshot_evidence_sha256='a' * 64,
                                     enabled_features=next(a['features'] for a in spec['arms']
                                         if a['id'] == run['run_id'].split('/')[1]))
            for event in run['usage']:
                event.update(model='gpt-6-sol', model_observed=True, evidence_sha256='a' * 64)
        for field in ('host', 'runtime_revision'):
            original = runs[-1]['provenance'][field]
            runs[-1]['provenance'][field] = 'different'
            with self.assertRaisesRegex(ValueError, 'same host and runtime'):
                pilot.analyze(runs, scores)
            runs[-1]['provenance'][field] = original
        model_run = next(r for r in runs if r['run_id'].endswith('/model'))
        del model_run['provenance']['capability_snapshot_id']
        with self.assertRaisesRegex(ValueError, 'capability snapshot'):
            pilot.validate_run(model_run, spec)

    def test_host_request_deduplication(self):
        runs, scores = fixture()
        duplicate = copy.deepcopy(runs[0]['usage'][0])
        duplicate['event_id'] = 'different-event'
        runs[0]['usage'].append(duplicate)
        with self.assertRaisesRegex(ValueError, 'Duplicate host request'):
            pilot.analyze(runs, scores, True)

    def test_reconciliation_cannot_omit_parent_or_synthesis(self):
        runs, scores = fixture()
        del runs[0]['reconciliation']['zero_request_role_evidence']['synthesis']
        with self.assertRaisesRegex(ValueError, 'Parent and synthesis'):
            pilot.analyze(runs, scores, True)
        runs, scores = fixture()
        runs[0]['reconciliation']['host_requests'].append({'host': 'synthetic', 'provider': 'synthetic',
                                                          'request_id': 'missing', 'role': 'synthesis'})
        with self.assertRaisesRegex(ValueError, 'does not reconcile'):
            pilot.analyze(runs, scores, True)

    def test_luna_disable_and_compensation_counts(self):
        runs, scores = fixture()
        for index in (0, 1, 2):
            runs[index]['usage'][0].update(model='gpt-6-luna', material_misroute=index < 2,
                                          compensation_worker=True, worker_id='worker-' + str(index), retry_count=1)
        report = pilot.analyze(runs, scores, True)
        self.assertTrue(report['class_routing']['trivial']['luna_disabled'])
        self.assertEqual(report['class_routing']['trivial']['luna_material_misroutes'], 2)
        self.assertEqual(report['class_routing']['trivial']['compensation_workers'], 3)
        self.assertEqual(report['class_routing']['trivial']['retries'], 3)
        self.assertTrue(any('after class disable' in f for f in report['failures']))


if __name__ == '__main__':
    unittest.main()
