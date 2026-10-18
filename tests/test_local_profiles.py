import copy
import hashlib
import json
import pathlib
import unittest
from agentfanout.local_profiles import AEON_REPOSITORY, LocalProfileError, LocalSensitiveResponseError, build_inference_request, identity_digest, model_identifier, parse_inference_response, validate_identity, validate_profile


def identity():
    return {'schema': 'LocalModelIdentityV1', 'logical_selector': 'worker', 'artifact_repository': AEON_REPOSITORY, 'artifact_revision': 'a' * 40, 'artifact_sha256': 'a' * 64, 'precision': 'BF16', 'served_name': AEON_REPOSITORY, 'backend': 'vllm', 'backend_version': 'fixture', 'config_sha256': 'b' * 64, 'template_sha256': 'c' * 64, 'endpoint_identity': 'fixture-endpoint', 'resource_domain': 'fixture-domain', 'serving_epoch': 'epoch-1', 'inference_profile_sha256': 'd' * 64, 'evaluation_capability': 'structured-packet'}


def configured():
    model = identity()
    profile = {'schema': 'LocalInferenceProfileV1', 'profile_id': 'fixture-aeon', 'identity_sha256': 'a' * 64, 'attestation_sha256': 'b' * 64, 'thinking_control': {'mechanism': 'chat_template_kwargs.enable_thinking', 'value': False}, 'sampling': {'temperature': 0.6, 'top_p': 0.95, 'top_k': 20}, 'constrained_output': 'json_schema', 'reasoning_field': 'reasoning_content', 'max_output_tokens': 1024}
    material = {k: v for k, v in profile.items() if k != 'identity_sha256'}
    model['inference_profile_sha256'] = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    profile['identity_sha256'] = identity_digest(model)
    return model, profile

class LocalProfilesTests(unittest.TestCase):
    def test_namespaced_data_and_safe_slug(self):
        self.assertEqual(model_identifier(AEON_REPOSITORY), AEON_REPOSITORY)
        for value in ['../../bad', 'org/../bad', 'org/$(touch x)', 'org/a/b', '/absolute', 'x\n']:
            with self.subTest(value=value), self.assertRaises(LocalProfileError): model_identifier(value)
        model = identity(); model['logical_selector'] = AEON_REPOSITORY
        with self.assertRaises(LocalProfileError): validate_identity(model)

    def test_exact_bf16_and_nemotron_explicit_precision(self):
        model = identity(); model['precision'] = 'NVFP4'
        with self.assertRaises(LocalProfileError): validate_identity(model)
        model['artifact_repository'] = 'nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4'
        self.assertEqual(validate_identity(model)['precision'], 'NVFP4')
        model['precision'] = 'auto'
        with self.assertRaises(LocalProfileError): validate_identity(model)

    def test_profile_controls_and_schema(self):
        model, profile = configured()
        schemas = pathlib.Path(__file__).resolve().parent.parent / 'schemas'
        worker_schema = json.loads((schemas / 'WorkerResultV1.schema.json').read_text())
        validator_schema = json.loads((schemas / 'ValidatorResultV1.schema.json').read_text())
        body = build_inference_request(model, profile, 'bounded prompt', worker_schema)
        self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': False})
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(body['temperature'], .6)
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertEqual(body['model'], 'worker')
        with self.assertRaises(LocalProfileError): build_inference_request(model, profile, 'prompt', {'title': 'WorkerResultV1', '$ref': 'https://unapproved.example/schema'})
        body['response_format']['json_schema']['schema']['type'] = 'array'
        self.assertEqual(build_inference_request(model, profile, 'bounded prompt', validator_schema)['response_format']['json_schema']['schema']['type'], 'object')

    def test_profile_drift_and_unsupported_controls(self):
        model, profile = configured(); profile['sampling']['temperature'] = 0
        with self.assertRaisesRegex(LocalProfileError, 'digest_mismatch'): build_inference_request(model, profile, 'prompt')
        model, profile = configured(); profile['identity_sha256'] = 'f' * 64
        with self.assertRaisesRegex(LocalProfileError, 'identity_mismatch'): build_inference_request(model, profile, 'prompt')
        for change in [{'thinking_control': {'mechanism': 'arbitrary', 'value': True}}, {'sampling': {'temperature': float('nan')}}, {'max_output_tokens': True}, {'attestation_sha256': ''}]:
            _, profile = configured(); profile.update(change)
            with self.assertRaises(LocalProfileError): validate_profile(profile)

    def test_no_universal_parameters(self):
        model, profile = configured(); profile['thinking_control'] = {'mechanism': 'unsupported', 'value': None}; profile['sampling'] = {}; profile['constrained_output'] = 'unsupported'
        material = {k: v for k, v in profile.items() if k != 'identity_sha256'}
        model['inference_profile_sha256'] = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest(); profile['identity_sha256'] = identity_digest(model)
        body = build_inference_request(model, profile, 'text')
        self.assertNotIn('temperature', body); self.assertNotIn('reasoning_effort', body)
        with self.assertRaises(LocalProfileError): build_inference_request(model, profile, 'text', {'title': 'WorkerResultV1'})

    def test_sensitive_supplied_text_rejected(self):
        model, profile = configured()
        for secret in ['xai-' + 'a' * 32, '-----BEGIN RSA ' + 'PRIVATE KEY-----\ncontent', 'password' + '=' + 'fixturecredential123']:
            with self.subTest(secret_kind=secret[:4]), self.assertRaisesRegex(LocalProfileError, 'local_prompt_sensitive'):
                build_inference_request(model, profile, secret)

    def test_sensitive_content_and_reasoning_rejected_usage_retained(self):
        model, profile = configured()
        for field in ('content', 'reasoning_content'):
            response = {'model': model['served_name'], 'choices': [{'finish_reason': 'stop', 'message': {'content': '{}', 'reasoning_content': 'reasoning'}}], 'usage': {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12, 'completion_tokens_details': {'reasoning_tokens': 2}, 'untrusted_field': 'xai-' + 'b' * 32}}
            response['choices'][0]['message'][field] = 'xai-' + 'a' * 32
            with self.assertRaises(LocalSensitiveResponseError) as raised: parse_inference_response(response, model, profile)
            self.assertEqual(raised.exception.usage_observation['total_tokens'], 12)
            self.assertEqual(raised.exception.usage_observation['completion_tokens_details'], {'reasoning_tokens': 2})
            self.assertNotIn('untrusted_field', raised.exception.usage_observation)
            self.assertIsNone(raised.exception.provider_cost_usd)
            self.assertNotIn(response['choices'][0]['message'][field], str(raised.exception))

    def test_response_identity_usage_and_reasoning(self):
        model, profile = configured()
        response = {'model': model['served_name'], 'choices': [{'finish_reason': 'stop', 'message': {'content': '{}', 'reasoning_content': 'reasoning'}}], 'usage': {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}}
        result = parse_inference_response(response, model, profile)
        self.assertEqual(result['reasoning'], 'reasoning'); self.assertIsNone(result['provider_cost_usd'])
        for mutate in [lambda r: r.update(model='alias'), lambda r: r['choices'][0].update(finish_reason='length'), lambda r: r['choices'][0]['message'].update(tool_calls=[{}]), lambda r: r['choices'][0]['message'].update(reasoning='unattested'), lambda r: r['usage'].update(total_tokens=13), lambda r: r['usage'].update(prompt_tokens=True)]:
            r = copy.deepcopy(response); mutate(r)
            with self.assertRaises(LocalProfileError): parse_inference_response(r, model, profile)
