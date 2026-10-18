"""Frozen V2 pilot: preparation and evidence import, never a model simulator."""
import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

SPEC = Path(__file__).with_suffix('.json')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest():
    raw = SPEC.read_bytes()
    spec = json.loads(raw)
    spec['manifest_sha256'] = digest(raw)
    return spec


def schedule(spec):
    runs = []
    for index, task in enumerate(spec['tasks']):
        arms = spec['arms'] if index % 2 == 0 else list(reversed(spec['arms']))
        for arm in arms:
            key = task['id'] + '/' + arm['id']
            runs.append({'run_id': key, 'task_id': task['id'], 'arm': arm['id'],
                         'blind_id': digest((spec['manifest_sha256'] + key).encode())[:20],
                         'order': len(runs) + 1})
    return runs


def prepare(destination):
    spec = manifest()
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    for run in schedule(spec):
        task = next(t for t in spec['tasks'] if t['id'] == run['task_id'])
        directory = root / run['run_id']
        directory.mkdir(parents=True)
        for name, content in task['fixtures'].items():
            (directory / name).write_text(content, encoding='utf-8')
        (directory / 'TASK.json').write_text(json.dumps(task, indent=2), encoding='utf-8')
    (root / 'private-plan.json').write_text(json.dumps(
        {'manifest_sha256': spec['manifest_sha256'], 'runs': schedule(spec)}, indent=2), encoding='utf-8')
    return {'prepared': 60, 'manifest_sha256': spec['manifest_sha256'], 'measured': False}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def request_key(event):
    return (event.get('host'), event.get('provider'), event.get('request_id'))


def acceptance(task_id, workspace):
    task = next(t for t in manifest()['tasks'] if t['id'] == task_id)
    checks = []
    for check in task['acceptance_checks']:
        try:
            process = subprocess.run([sys.executable, '-c', check['code']], cwd=workspace,
                                     capture_output=True, text=True, timeout=60)
            checks.append({'id': check['id'], 'passed': process.returncode == 0,
                           'stdout': process.stdout, 'stderr': process.stderr,
                           'check_sha256': digest(check['code'].encode())})
        except subprocess.TimeoutExpired:
            checks.append({'id': check['id'], 'passed': False, 'stderr': 'timeout',
                           'check_sha256': digest(check['code'].encode())})
    return {'task_id': task_id, 'checks': checks, 'passed': all(c['passed'] for c in checks)}


def validate_run(run, spec):
    required = ['run_id', 'manifest_sha256', 'provenance', 'latency_seconds',
                'usage', 'validators', 'duplicate_jobs', 'evidence', 'reconciliation', 'acceptance_results']
    require(all(k in run for k in required), 'Missing run fields')
    require(run['run_id'] in {r['run_id'] for r in schedule(spec)}, 'Unknown run')
    require(run['manifest_sha256'] == spec['manifest_sha256'], 'Manifest mismatch')
    provenance = run['provenance']
    require(all(isinstance(provenance.get(k), str) and provenance[k].strip() for k in
                ['kind', 'host', 'runtime_revision', 'executor', 'started_at', 'finished_at']),
            'Missing provenance')
    require(provenance['kind'] in ('measured', 'test_fixture'), 'Invalid provenance kind')
    start = datetime.fromisoformat(provenance['started_at'].replace('Z', '+00:00'))
    finish = datetime.fromisoformat(provenance['finished_at'].replace('Z', '+00:00'))
    require(start.tzinfo is not None and finish.tzinfo is not None and finish >= start,
            'Invalid timezone-aware execution timestamps')
    if provenance['kind'] == 'measured':
        expected = next(a['features'] for a in spec['arms'] if a['id'] == run['run_id'].split('/')[1])
        require(provenance.get('enabled_features') == expected, 'Measured arm configuration mismatch')
    require(number(run['latency_seconds']), 'Invalid latency')
    require(math.isclose(run['latency_seconds'], (finish - start).total_seconds(),
                         rel_tol=0, abs_tol=0.001), 'Latency does not match execution window')
    require(type(run['duplicate_jobs']) is int and run['duplicate_jobs'] >= 0, 'Invalid duplicates')
    require(isinstance(run['evidence'], list) and run['evidence'], 'Evidence required')
    for evidence in run['evidence']:
        require(isinstance(evidence.get('path'), str) and evidence['path'], 'Evidence path required')
        require(isinstance(evidence.get('sha256'), str) and len(evidence['sha256']) == 64
                and all(c in '0123456789abcdef' for c in evidence['sha256']), 'Invalid evidence hash')
    if provenance['kind'] == 'measured' and 'model_selection' in provenance['enabled_features']:
        require(isinstance(provenance.get('capability_snapshot_id'), str) and
                provenance['capability_snapshot_id'].strip() and
                provenance.get('capability_snapshot_evidence_sha256') in
                {e['sha256'] for e in run['evidence']}, 'Model routing requires capability snapshot evidence')
    require(isinstance(run['usage'], list) and run['usage'], 'Usage events required')
    event_ids, request_ids = set(), set()
    for event in run['usage']:
        require(all(isinstance(event.get(k), str) and event[k] for k in
                    ['event_id', 'host', 'provider', 'model', 'request_id', 'phase', 'role']), 'Incomplete usage provenance')
        require(event['event_id'] not in event_ids, 'Duplicate usage event')
        event_ids.add(event['event_id'])
        require(request_key(event) not in request_ids, 'Duplicate host request under different event IDs')
        request_ids.add(request_key(event))
        require(event.get('cost_basis') in ('actual_charge', 'api_equivalent_estimate'), 'Explicit cost basis required')
        require(event['role'] in ('parent', 'worker', 'validator', 'retry', 'synthesis', 'audit', 'system'), 'Invalid role')
        require(type(event.get('material_misroute')) is bool and
                type(event.get('compensation_worker')) is bool and
                type(event.get('retry_count')) is int and event['retry_count'] >= 0, 'Routing outcome fields required')
        if event['compensation_worker'] or event['role'] in ('worker', 'retry'):
            require(isinstance(event.get('worker_id'), str) and event['worker_id'], 'Worker identity required')
        if provenance['kind'] == 'measured':
            require(event.get('model_observed') is True and
                    event.get('evidence_sha256') in {e['sha256'] for e in run['evidence']},
                    'Measured usage requires observed model evidence')
            identity = event['model'].strip().lower()
            require(identity not in ('', 'unknown', 'unavailable', 'unspecified', 'null', 'n/a'),
                    'Measured usage requires known model identity')
            if identity == 'none':
                require(event['role'] == 'system' and event['phase'] == 'overhead' and
                        event.get('input_tokens') == 0 and event.get('output_tokens') == 0,
                        'No-model identity is only valid for evidenced local overhead')
        require(event['phase'] in ('execution', 'validation', 'recovery', 'skipped_validator_audit', 'overhead'),
                'Unknown accounting phase')
        for key in ('cost_usd', 'input_tokens', 'output_tokens'):
            require(key in event and (event[key] is None or number(event[key])), 'Invalid usage value')
    require(len({e['cost_basis'] for e in run['usage']}) == 1, 'Mixed cost bases within run')
    reconciliation = run['reconciliation']
    require(reconciliation.get('complete') is True and
            isinstance(reconciliation.get('reviewer'), str) and reconciliation['reviewer'] and
            reconciliation['reviewer'] != provenance['executor'], 'Independent request reconciliation required')
    evidence_hashes = {e['sha256'] for e in run['evidence']}
    require(reconciliation.get('inventory_evidence_sha256') in evidence_hashes, 'Host request inventory evidence required')
    inventory = reconciliation.get('host_requests', [])
    require(isinstance(inventory, list) and len(inventory) == len(request_ids) and
            {request_key(e) for e in inventory} == request_ids, 'Host inventory does not reconcile to usage')
    inventory_roles = {request_key(e): e.get('role') for e in inventory}
    require(all(inventory_roles[request_key(e)] == e['role'] for e in run['usage']), 'Inventory role mismatch')
    for role in ('parent', 'synthesis'):
        if not any(e['role'] == role for e in run['usage']):
            require(reconciliation.get('zero_request_role_evidence', {}).get(role) in evidence_hashes,
                    'Parent and synthesis boundaries must be accounted or evidenced zero')
    task = next(t for t in spec['tasks'] if t['id'] == run['run_id'].split('/')[0])
    checks = run['acceptance_results']
    require(isinstance(checks, list) and len(checks) == len(task['acceptance_checks']) and
            {c.get('id') for c in checks} == {c['id'] for c in task['acceptance_checks']},
            'Concrete acceptance check results required')
    for check in checks:
        frozen = next(c for c in task['acceptance_checks'] if c['id'] == check['id'])
        require(type(check.get('passed')) is bool and check.get('evidence_sha256') in evidence_hashes and
                check.get('check_sha256') == digest(frozen['code'].encode()), 'Acceptance result provenance mismatch')
    validators = run['validators']
    require(isinstance(validators, list), 'Validators must be a list')
    require(len(validators) == len(task['validators']) and
            {v.get('name') for v in validators} == set(task['validators']), 'Validator inventory mismatch')
    for validator in validators:
        require(validator.get('status') in ('passed', 'failed', 'skipped'), 'Invalid validator status')
        if validator['status'] == 'skipped':
            audit = validator.get('audit', {})
            require(audit.get('result') in ('passed', 'failed') and
                    isinstance(audit.get('evidence_sha256'), str) and
                    audit['evidence_sha256'] in {e['sha256'] for e in run['evidence']}, 'Skipped validator needs audit evidence')
            require(audit.get('usage_event_id') in {e['event_id'] for e in run['usage']
                    if e['phase'] == 'skipped_validator_audit'}, 'Skipped audit cost must be separately accounted')
    return run


def import_run(source, store, evidence_root):
    run = validate_run(json.loads(Path(source).read_text(encoding='utf-8')), manifest())
    root = Path(evidence_root).resolve()
    for evidence in run['evidence']:
        target = (root / evidence['path']).resolve()
        require(target.is_relative_to(root), 'Evidence escapes root')
        require(target.is_file() and digest(target.read_bytes()) == evidence['sha256'], 'Evidence hash mismatch')
        if evidence['sha256'] == run['reconciliation']['inventory_evidence_sha256']:
            inventory = json.loads(target.read_text(encoding='utf-8'))
            require(inventory.get('requests') == run['reconciliation']['host_requests'] and
                    inventory.get('terminal') is True and
                    inventory.get('started_at') == run['provenance']['started_at'] and
                    inventory.get('finished_at') == run['provenance']['finished_at'],
                    'Raw terminal host inventory does not match reconciliation window')
    destination = Path(store)
    destination.mkdir(parents=True, exist_ok=True)
    filename = destination / (run['run_id'].replace('/', '__') + '.json')
    with filename.open('x', encoding='utf-8') as stream:
        json.dump(run, stream, indent=2)
    return {'imported': run['run_id'], 'kind': run['provenance']['kind']}


def analyze(runs, scores, allow_test_fixtures=False):
    spec = manifest()
    plan = schedule(spec)
    require(len(runs) == 60 and {r['run_id'] for r in runs} == {r['run_id'] for r in plan},
            'Exactly 60 unique runs required')
    for run in runs:
        validate_run(run, spec)
    require(len({e['cost_basis'] for r in runs for e in r['usage']}) == 1,
            'Actual charges and API-equivalent estimates cannot be compared')
    all_requests = [request_key(e) for r in runs for e in r['usage']]
    require(len(all_requests) == len(set(all_requests)), 'Host request reused across runs')
    kinds = {r['provenance']['kind'] for r in runs}
    require(len(kinds) == 1, 'Mixed provenance is forbidden')
    measured = kinds == {'measured'}
    if measured:
        require(len({(r['provenance']['host'], r['provenance']['runtime_revision']) for r in runs}) == 1,
                'Measured pilot requires the same host and runtime revision for all 60 runs')
    require(measured or allow_test_fixtures, 'Fixture data requires explicit opt-in')
    require(len(scores) == 60 and {s.get('blind_id') for s in scores} == {r['blind_id'] for r in plan},
            'Exactly 60 unique blind scores required')
    for score in scores:
        require(score.get('blinded') is True and isinstance(score.get('scorer'), str)
                and score['scorer'].strip() and isinstance(score.get('evidence_sha256'), str)
                and len(score['evidence_sha256']) == 64, 'Independent blind scoring provenance required')
        require(number(score.get('quality')) and score['quality'] <= 1 and
                type(score.get('accepted')) is bool, 'Invalid quality score')
    by_run = {r['run_id']: r for r in runs}
    by_blind = {s['blind_id']: s for s in scores}
    failures = []
    metrics = {}
    classes = {t['category']: {'luna_material_misroutes': 0, 'luna_disabled': False,
                             'compensation_workers': 0, 'retries': 0} for t in spec['tasks']}
    recovery_counts = {}
    for item in plan:
        run, score = by_run[item['run_id']], by_blind[item['blind_id']]
        category = next(t['category'] for t in spec['tasks'] if t['id'] == item['task_id'])
        state = classes[category]
        compensation = {(e['host'], e['worker_id']) for e in run['usage'] if e['compensation_worker']}
        retries = sum(e['retry_count'] for e in run['usage'])
        state['compensation_workers'] += len(compensation)
        state['retries'] += retries
        recovery_counts[item['run_id']] = {'compensation_workers': len(compensation), 'retries': retries}
        for event in run['usage']:
            if 'luna' in event['model'].lower():
                if state['luna_disabled']:
                    failures.append(item['run_id'] + ': Luna used after class disable threshold')
                if event['material_misroute']:
                    state['luna_material_misroutes'] += 1
                    if state['luna_material_misroutes'] >= 2:
                        state['luna_disabled'] = True
        require(score['evidence_sha256'] in {e['sha256'] for e in run['evidence']}, 'Score evidence not in run')
        require(score['scorer'] != run['provenance']['executor'], 'Scorer must be independent')
        unknown = any(e[k] is None for e in run['usage'] for k in ('cost_usd', 'input_tokens', 'output_tokens'))
        if unknown:
            failures.append(item['run_id'] + ': unknown usage')
        if any('astra' in e['model'].lower() for e in run['usage']):
            failures.append(item['run_id'] + ': unauthorized Astra')
        if run['duplicate_jobs']:
            failures.append(item['run_id'] + ': duplicate jobs')
        if any(v['status'] == 'failed' or v.get('audit', {}).get('result') == 'failed' for v in run['validators']):
            failures.append(item['run_id'] + ': validator failure')
        if not all(c['passed'] for c in run['acceptance_results']):
            failures.append(item['run_id'] + ': concrete acceptance failure')
        metrics[item['run_id']] = {'cost': None if unknown else sum(e['cost_usd'] for e in run['usage']),
                                  'latency': run['latency_seconds'], 'quality': score['quality'],
                                  'accepted': score['accepted']}
    comparisons = {}
    for arm in spec['arms'][1:]:
        ratios = {'cost': [], 'latency': []}
        quality_ok = True
        for task in spec['tasks']:
            base = metrics[task['id'] + '/baseline']
            current = metrics[task['id'] + '/' + arm['id']]
            quality_ok &= current['accepted'] and current['quality'] >= base['quality']
            for metric in ratios:
                if base[metric] is not None and current[metric] is not None and base[metric] > 0:
                    ratios[metric].append(current[metric] / base[metric])
        medians = {k: median(v) if len(v) == 12 else None for k, v in ratios.items()}
        cost, latency = medians['cost'], medians['latency']
        economic = cost is not None and latency is not None and (
            (cost <= 0.85 and latency <= 1.05) or (latency <= 0.85 and cost <= 1.05))
        comparisons[arm['id']] = {'paired_median_ratios': medians, 'quality_gate': bool(quality_ok),
                                  'economic_gate': economic, 'pass': bool(quality_ok and economic and not failures)}
    return {'pilot_id': spec['pilot_id'], 'manifest_sha256': spec['manifest_sha256'],
            'result_kind': 'measured' if measured else 'TEST_FIXTURE_NOT_MEASURED',
            'cost_basis': runs[0]['usage'][0]['cost_basis'],
            'class_routing': classes, 'recovery_counts': recovery_counts,
            'failures': failures, 'comparisons': comparisons,
            'promotion_eligible': measured and comparisons['lifecycle_hook']['pass']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare_parser = sub.add_parser('prepare')
    prepare_parser.add_argument('destination')
    check_parser = sub.add_parser('check')
    check_parser.add_argument('task_id')
    check_parser.add_argument('workspace')
    import_parser = sub.add_parser('import-run')
    import_parser.add_argument('source')
    import_parser.add_argument('store')
    import_parser.add_argument('evidence_root')
    analysis_parser = sub.add_parser('analyze')
    analysis_parser.add_argument('store')
    analysis_parser.add_argument('scores')
    analysis_parser.add_argument('--allow-test-fixtures', action='store_true')
    args = parser.parse_args()
    if args.command == 'prepare':
        result = prepare(args.destination)
    elif args.command == 'check':
        result = acceptance(args.task_id, args.workspace)
    elif args.command == 'import-run':
        result = import_run(args.source, args.store, args.evidence_root)
    else:
        result = analyze([json.loads(p.read_text(encoding='utf-8')) for p in Path(args.store).glob('*.json')],
                         json.loads(Path(args.scores).read_text(encoding='utf-8')), args.allow_test_fixtures)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
