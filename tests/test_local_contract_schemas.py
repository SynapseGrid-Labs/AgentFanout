import copy
import json
import pathlib
import unittest
try:
    import jsonschema
except ImportError:
    jsonschema = None
from tests.test_local_profiles import configured
from tests.test_coordinator_local import resource

@unittest.skipIf(jsonschema is None, 'optional schema validator unavailable')
class LocalSchemaTests(unittest.TestCase):
    def validator(self, name):
        path = pathlib.Path(__file__).resolve().parent.parent / 'schemas' / (name + '.schema.json')
        schema = json.loads(path.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        return jsonschema.Draft202012Validator(schema)

    def test_runtime_identity_and_profile_fixtures_match_schema(self):
        model, profile = configured()
        self.validator('LocalModelIdentityV1').validate(model)
        self.validator('LocalInferenceProfileV1').validate(profile)
        self.validator('CoordinatorResourceBindingV1').validate(resource().evidence)

    def test_identity_schema_blocks_alias_path_precision_digest_errors(self):
        model, _ = configured(); validator = self.validator('LocalModelIdentityV1')
        for change in [{'precision': 'NVFP4'}, {'artifact_revision': 'main'}, {'logical_selector': 'org/model'}, {'artifact_repository': '../../model'}, {'artifact_sha256': 'unknown'}, {'schema': 'LocalModelIdentityV2'}]:
            bad = dict(model); bad.update(change)
            with self.subTest(change=change), self.assertRaises(jsonschema.ValidationError): validator.validate(bad)
        bad = dict(model); bad['artifact_repository'] = 'nvidia/Nemotron'; bad['precision'] = 'AuTo'
        with self.assertRaises(jsonschema.ValidationError): validator.validate(bad)

    def test_profile_schema_blocks_unattested_controls_and_unknown_fields(self):
        _, profile = configured(); validator = self.validator('LocalInferenceProfileV1')
        for change in [{'thinking_control': {'mechanism': 'shell', 'value': 'command'}}, {'attestation_sha256': ''}, {'sampling': {'arbitrary': 2}}, {'max_output_tokens': True}, {'constrained_output': 'json_object'}, {'tools': []}]:
            bad = copy.deepcopy(profile); bad.update(change)
            with self.subTest(change=change), self.assertRaises(jsonschema.ValidationError): validator.validate(bad)

    def test_resource_schema_cannot_be_ready_status_or_unknown_state(self):
        validator = self.validator('CoordinatorResourceBindingV1')
        for change in [{'schema': 'CoordinatorResourceBindingV2'}, {'role': 'parent'}, {'cancellation_state': 'maybe'}, {'reservation_id': '--help'}, {'coordinator_attestation_sha256': ''}, {'expires_at': True}]:
            bad = resource().evidence; bad.update(change)
            with self.subTest(change=change), self.assertRaises(jsonschema.ValidationError): validator.validate(bad)
