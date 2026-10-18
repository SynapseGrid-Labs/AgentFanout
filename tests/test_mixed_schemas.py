"""Local schema resolution only; no schema URL fetches."""
import json
from pathlib import Path
import unittest

try:
    import jsonschema
except ImportError:
    jsonschema = None

from tests.test_mixed_contracts import contract, policy
from tests.test_mixed_state import envelope

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(jsonschema is None, "optional schema validator unavailable")
class MixedSchemaTests(unittest.TestCase):
    def validator(self, name):
        directory = ROOT/"schemas"
        store = {}
        for path in directory.glob("*.schema.json"):
            schema = json.loads(path.read_text())
            store[path.as_uri()] = schema
            store["https://synapsegrid-labs.example/schemas/agentfanout/" + path.name] = schema
        path = directory/(name+".schema.json")
        schema = store[path.as_uri()]
        jsonschema.Draft202012Validator.check_schema(schema)
        resolver = jsonschema.RefResolver(path.as_uri(), schema, store=store, handlers={"http": lambda url: store[url], "https": lambda url: store[url]})
        return jsonschema.Draft202012Validator(schema, resolver=resolver)

    def test_all_new_contracts_accept_real_fixtures(self):
        for name, packet in (("EvidenceEgressPolicyV1", policy()), ("MixedRunContractV1", contract()), ("HostedRunStateV3", envelope())):
            with self.subTest(schema=name):
                self.validator(name).validate(packet)

    def test_provider_mode_and_missing_validator_rejected_structurally(self):
        value = contract()
        value["assignments"][0]["validator"]["provider"] = "Grok"
        self.assertFalse(self.validator("MixedRunContractV1").is_valid(value))
        value = contract()
        value["assignments"][0]["validator"] = None
        self.assertFalse(self.validator("MixedRunContractV1").is_valid(value))

    def test_profiled_session_guard_shape_is_distinct_and_strict(self):
        guard={"schema":"ProfiledSessionGuardV1","run_id":"run_test","contract_sha256":"a"*64,"runtime_sha256":"b"*64,"state_path_sha256":"c"*64,"host_destination":"parent.host","parent_destination":"grok.cloud"}
        validator=self.validator("ProfiledSessionGuardV1")
        validator.validate(guard)
        for field in guard:
            broken=dict(guard)
            del broken[field]
            self.assertFalse(validator.is_valid(broken))
        legacy=dict(guard,schema="SessionGuardStateV1")
        self.assertFalse(validator.is_valid(legacy))
