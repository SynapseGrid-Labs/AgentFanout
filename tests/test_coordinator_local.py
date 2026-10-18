import copy
import hashlib
import concurrent.futures
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid
from agentfanout.coordinator_local import CoordinatorBroker, CoordinatorError, CoordinatorUncertain, FixedClientTransport, ResourceBinding, observed_readiness, validate_operation
from agentfanout.local_profiles import identity_digest, build_inference_request, LocalProfileError
from tests.test_local_profiles import identity, configured


def resource():
    model = identity()
    evidence = {'schema': 'CoordinatorResourceBindingV1', 'reservation_id': str(uuid.uuid4()), 'owner_token': 'fixture-fence', 'serving_epoch': model['serving_epoch'], 'identity_sha256': identity_digest(model), 'role': 'worker', 'expires_at': time.time() + 300, 'cancellation_state': 'none', 'coordinator_attestation_sha256': 'a' * 64}
    return ResourceBinding(model, evidence)


def ready():
    return {'production_enabled': True, 'admission_closed': False, 'domains': [{'id': 'fixture-domain', 'desired': 'worker', 'observed': 'worker', 'uncertain': False, 'transitioning': False, 'admission_closed': False, 'gpu_observation_freshness': {'state': 'fresh', 'uncertain': False, 'sample_age_seconds': 1}, 'active_work': []}]}


class FakeTransport:
    def __init__(self): self.calls = []
    def call(self, arguments, raw):
        self.calls.append((arguments, raw))
        if arguments[0] == 'request':
            identifier = json.loads(raw)['request_id']
            return {'transition_id': identifier, 'status_url': '/transitions/' + identifier}
        return {'fixture': True}

class CoordinatorTests(unittest.TestCase):
    def test_host_allowlist_cannot_expose_administrative_targets(self):
        transport = FakeTransport()
        for target in ('off', 'maint'):
            with self.subTest(target=target):
                caps = frozenset({'worker', target})
                with self.assertRaisesRegex(CoordinatorError, 'administrative_capability_denied'):
                    CoordinatorBroker(transport, caps)
                with self.assertRaisesRegex(CoordinatorError, 'administrative_capability_denied'):
                    validate_operation('request', {'target': target, 'behavior': 'reject', 'timeout': 30,
                                                  'request_id': str(uuid.uuid4())}, caps)
        self.assertEqual(transport.calls, [])

    def test_operation_and_shell_denials(self):
        caps = frozenset({'worker'})
        for operation in ['switch', 'admission', 'download', 'reconcile-absent', 'submit', 'off', 'arbitrary']:
            with self.subTest(operation=operation), self.assertRaises(CoordinatorError): validate_operation(operation, {}, caps)
        for identifier in ['--help', '$(echo unsafe)', '../../id', 'not-a-uuid']:
            with self.assertRaises(CoordinatorError): validate_operation('cancel', {'identifier': identifier}, caps)
        with self.assertRaises(CoordinatorError): validate_operation('status', {'endpoint': 'bad'}, caps)

    def test_request_bounds_and_idempotent_identity(self):
        caps = frozenset({'worker'}); request = {'target': 'worker', 'behavior': 'reject', 'timeout': 30, 'request_id': str(uuid.uuid4())}
        arguments, raw = validate_operation('request', request, caps)
        self.assertEqual(arguments, ['request']); self.assertEqual(json.loads(raw), request)
        for change in [{'command': 'shell'}, {'target': 'unapproved'}, {'behavior': 'kill'}, {'timeout': float('nan')}, {'request_id': 'bad'}]:
            body = dict(request); body.update(change)
            with self.assertRaises(CoordinatorError): validate_operation('request', body, caps)

    def test_inference_is_bounded_text_no_tools(self):
        request = {'model': 'worker', 'messages': [{'role': 'user', 'content': 'text'}], 'stream': False, 'max_tokens': 10}
        self.assertTrue(validate_operation('inference', request, frozenset({'worker'}))[1])
        for change in [{'tools': []}, {'stream': True}, {'messages': [{'role': 'system', 'content': 'text'}]}, {'max_tokens': True}, {'messages': [{'role': 'user', 'content': 'x' * 400001}]}]:
            body = dict(request); body.update(change)
            with self.assertRaises(CoordinatorError): validate_operation('inference', body, frozenset({'worker'}))

    def test_no_mutation_before_host_authorization(self):
        transport = FakeTransport(); broker = CoordinatorBroker(transport, frozenset({'worker'}))
        request = {'target': 'worker', 'behavior': 'reject', 'timeout': 30, 'request_id': str(uuid.uuid4())}
        with self.assertRaises(CoordinatorError): broker.execute('request', request, authorize_effect=lambda *args: False)
        self.assertEqual(transport.calls, [])
        journal = []
        def authorize(operation, digest):
            self.assertEqual(transport.calls, []); journal.append((operation, digest)); return True
        self.assertEqual(broker.execute('request', request, authorize_effect=authorize)['transition_id'], request['request_id'])
        self.assertEqual(len(journal), 1)

    def test_inference_requires_real_binding(self):
        transport = FakeTransport(); broker = CoordinatorBroker(transport, frozenset({'worker'}))
        request = {'model': 'worker', 'messages': [{'role': 'user', 'content': 'text'}], 'stream': False, 'max_tokens': 10}
        with self.assertRaises(CoordinatorError): broker.execute('inference', request, authorize_effect=lambda *args: True)
        self.assertEqual(transport.calls, [])
        model, profile = configured(); binding = resource(); binding.identity = model; binding.evidence['identity_sha256'] = identity_digest(model)
        request = build_inference_request(model, profile, 'text')
        broker.execute('inference', request, authorize_effect=lambda *args: True, binding=binding, current_identity=binding.identity, profile=profile)
        self.assertEqual(len(transport.calls), 1)

    def test_sensitive_prompt_denied_before_effect_authorization(self):
        transport = FakeTransport(); broker = CoordinatorBroker(transport, frozenset({'worker'}))
        model, profile = configured(); binding = resource(); binding.identity = model; binding.evidence['identity_sha256'] = identity_digest(model)
        request = build_inference_request(model, profile, 'text'); request['messages'][0]['content'] = 'xai-' + 'a' * 32
        journal = []
        with self.assertRaisesRegex(LocalProfileError, 'local_prompt_sensitive'):
            broker.execute('inference', request, authorize_effect=lambda *args: journal.append(args) or True, binding=binding, current_identity=model, profile=profile)
        self.assertEqual(transport.calls, []); self.assertEqual(journal, [])

    def test_profile_overrides_denied_before_spawn(self):
        transport = FakeTransport(); broker = CoordinatorBroker(transport, frozenset({'worker'}))
        model, profile = configured(); binding = resource(); binding.identity = model; binding.evidence['identity_sha256'] = identity_digest(model)
        request = build_inference_request(model, profile, 'text'); request['temperature'] = 0
        with self.assertRaises(CoordinatorError): broker.execute('inference', request, authorize_effect=lambda *args: True, binding=binding, current_identity=model, profile=profile)
        self.assertEqual(transport.calls, [])

    def test_unknown_resource_schema_refuses_migration(self):
        binding = resource(); binding.evidence['schema'] = 'CoordinatorResourceBindingV2'
        with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'worker', binding.identity)

    def test_wrong_job_and_transition_correlation(self):
        broker = CoordinatorBroker(FakeTransport(), frozenset({'worker'}))
        for operation in ('job', 'transition'):
            with self.assertRaises(CoordinatorError): broker.observe(operation, {'identifier': str(uuid.uuid4())})

    def test_unknown_inference_effect_quarantines_resource(self):
        class FailingTransport:
            def call(self, arguments, raw): raise CoordinatorUncertain('fixture_remote_effect_unknown')
        broker = CoordinatorBroker(FailingTransport(), frozenset({'worker'}))
        model, profile = configured(); binding = resource(); binding.identity = model; binding.evidence['identity_sha256'] = identity_digest(model)
        request = build_inference_request(model, profile, 'text')
        with self.assertRaises(CoordinatorUncertain): broker.execute('inference', request, authorize_effect=lambda *args: True, binding=binding, current_identity=model, profile=profile)
        self.assertTrue(binding.quarantined)
        with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'worker', model)

    def test_status_not_reservation(self):
        self.assertEqual(observed_readiness(ready(), 'fixture-domain', 'worker')['observed'], 'worker')
        binding = ResourceBinding(identity(), ready())
        with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'worker', identity())

    def test_readiness_uncertainty_stale_and_contention(self):
        for change in [{'uncertain': True}, {'transitioning': True}, {'observed': 'image'}, {'admission_closed': True}, {'active_work': [{'stage': 'running'}]}, {'gpu_observation_freshness': {'state': 'unavailable'}}, {'gpu_observation_freshness': {'state': 'fresh', 'uncertain': False, 'sample_age_seconds': 31}}]:
            status = ready(); status['domains'][0].update(change)
            with self.subTest(change=change), self.assertRaises(CoordinatorError): observed_readiness(status, 'fixture-domain', 'worker')

    def test_resource_stale_epoch_alias_role_and_expiry(self):
        binding = resource(); binding.verify(time.time(), 'worker', binding.identity)
        for key in ['served_name', 'artifact_revision', 'config_sha256', 'serving_epoch']:
            model = copy.deepcopy(binding.identity); model[key] = 'f' * 40 if key == 'artifact_revision' else ('f' * 64 if key.endswith('sha256') else 'drift')
            with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'worker', model)
        with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'validator', binding.identity)
        binding.evidence['expires_at'] = 0
        with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'worker', binding.identity)

    def test_validator_has_separate_resource(self):
        worker = resource(); validator = resource(); validator.evidence['role'] = 'validator'
        self.assertNotEqual(worker.evidence['reservation_id'], validator.evidence['reservation_id'])
        validator.verify(time.time(), 'validator', validator.identity)
        with self.assertRaises(CoordinatorError): worker.verify(time.time(), 'validator', worker.identity)

    def test_cancel_uncertainty_quarantines_no_replay(self):
        binding = resource()
        with self.assertRaises(CoordinatorUncertain): binding.reconcile_cancellation({'stage': 'cancelled'})
        self.assertTrue(binding.quarantined)
        with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'worker', binding.identity)
        binding.reconcile_cancellation({'reservation_id': binding.evidence['reservation_id'], 'serving_epoch': binding.evidence['serving_epoch'], 'effects_absent': True, 'accounting_reconciled': True})
        self.assertFalse(binding.quarantined)
        # Reconciled cancellation still must not admit the cancelled reservation.
        binding.evidence['cancellation_state'] = 'confirmed'
        with self.assertRaises(CoordinatorError): binding.verify(time.time(), 'worker', binding.identity)

    def test_generic_after_authorization_quarantines_restart_binding(self):
        for error in (OSError('fixture-after-send'), BlockingIOError('fixture-backpressure'), RuntimeError('fixture-after-send')):
            class FailingTransport:
                def call(self, arguments, raw): raise error
            broker = CoordinatorBroker(FailingTransport(), frozenset({'worker'}))
            model, profile = configured(); binding = resource(); binding.identity = model; binding.evidence['identity_sha256'] = identity_digest(model)
            request = build_inference_request(model, profile, 'text'); journal = []
            with self.assertRaises(CoordinatorUncertain):
                broker.execute('inference', request, authorize_effect=lambda *args: journal.append(args) or True, binding=binding, current_identity=model, profile=profile)
            self.assertEqual(len(journal), 1)
            self.assertTrue(binding.quarantined); self.assertEqual(binding.evidence['cancellation_state'], 'uncertain')
            restarted = ResourceBinding(model, copy.deepcopy(binding.evidence))
            with self.assertRaises(CoordinatorError): restarted.verify(time.time(), 'worker', model)
            with self.assertRaises(CoordinatorError): broker.execute('inference', request, authorize_effect=lambda *args: True, binding=binding, current_identity=model, profile=profile)

    def test_interrupt_after_authorization_quarantines_without_swallowing(self):
        for error in (KeyboardInterrupt(), SystemExit(7)):
            class InterruptedTransport:
                def call(self, arguments, raw): raise error
            broker = CoordinatorBroker(InterruptedTransport(), frozenset({'worker'}))
            model, profile = configured(); binding = resource(); binding.identity = model; binding.evidence['identity_sha256'] = identity_digest(model)
            request = build_inference_request(model, profile, 'text')
            with self.assertRaises(type(error)):
                broker.execute('inference', request, authorize_effect=lambda *args: True, binding=binding, current_identity=model, profile=profile)
            self.assertTrue(binding.quarantined); self.assertEqual(binding.evidence['cancellation_state'], 'uncertain')

    def test_transport_post_spawn_exception_is_uncertain_and_cleans_up(self):
        with mock.patch('agentfanout.coordinator_local.selectors.DefaultSelector', side_effect=OSError('fixture-selector-failed')):
            with self.assertRaises(CoordinatorUncertain): self.run_client('import time; time.sleep(3)')

    def test_transport_pin(self):
        with self.assertRaises(CoordinatorError): FixedClientTransport((sys.executable,), 'a' * 64)

    def run_client(self, source, timeout=1, raw=b''):
        with tempfile.TemporaryDirectory() as directory:
            script = pathlib.Path(directory) / 'fixture.py'; script.write_text(source)
            argv = (sys.executable, str(script))
            digest = hashlib.sha256(json.dumps(argv, separators=(',', ':')).encode()).hexdigest()
            return FixedClientTransport(argv, digest, timeout).call(['status'], raw)

    def test_transport_bounded_json_and_supervision(self):
        self.assertEqual(self.run_client('print(\'{"ok":true}\')'), {'ok': True})
        for source in ["print('x' * 1000001)", "print('not-json')", 'import time; time.sleep(3)', 'import sys; sys.exit(1)']:
            with self.subTest(source=source), self.assertRaises(CoordinatorUncertain): self.run_client(source)

    def test_transport_duplicate_and_nonfinite_response_denied(self):
        for source in ["print('{\"ok\":true,\"ok\":false}')", "print('{\"quota\":NaN}')"]:
            with self.subTest(source=source), self.assertRaises(CoordinatorUncertain): self.run_client(source)

    def test_transport_concurrent_processes_isolated(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda number: self.run_client('print(\'{"request":' + str(number) + '}\')'), [1, 2]))
        self.assertEqual(results, [{'request': 1}, {'request': 2}])

    def test_operation_identifier_never_shell_authority(self):
        transport = FakeTransport(); broker = CoordinatorBroker(transport, frozenset({'worker'}))
        for identifier in ['--timeout 3600', 'x; touch marker', '`touch marker`', '$(touch marker)']:
            with self.assertRaises(CoordinatorError): broker.execute('cancel', {'identifier': identifier}, authorize_effect=lambda *args: True)
        self.assertEqual(transport.calls, [])

    def test_transport_stdin_not_consumed_timeout(self):
        with self.assertRaises(CoordinatorUncertain): self.run_client('import time; time.sleep(3)', raw=b'x' * 300000)
