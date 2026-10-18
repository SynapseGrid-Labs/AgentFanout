from copy import deepcopy
from pathlib import Path
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.lifecycle import build_task_units, initialize_run, load_run_state, _residual_task
from agentfanout.mixed_contracts import MixedContractError, freeze_contract
from agentfanout.mixed_state import assignment_for_task, load_hosted_state, save_hosted_state, validate_hosted_state, wrap_hosted_state
from tests.test_mixed_contracts import assignment, runtime


def envelope():
    tasks = build_task_units("Review assigned bounded text", "bulk-read", "explorer", "Codex", "default", "high", ["item:1"])
    state = initialize_run("Review assigned bounded text", tasks)
    a = assignment()
    a["assignment_id"] = state["tasks"][0]["task_id"]
    expected = a["worker"]
    state["tasks"][0]["provider_model"].update(provider=expected["provider"], selector=expected["model_selector"], resolved_model=expected["resolved_model"])
    state["tasks"][0]["reasoning_effort"] = expected["reasoning_effort"]
    contract = freeze_contract(state["run_id"], runtime(), [a], feature_controls={"mixed_provider_runs": True,"grok_cli_subscription": True,"coordinator_local": True})
    return wrap_hosted_state(state, contract, runtime())


class MixedStateTests(unittest.TestCase):
    def test_old_reader_rejects_envelope_new_reader_requires_exact_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            path = Path(tmp)/"state.json"
            value = envelope()
            save_hosted_state(path, value, runtime())
            with self.assertRaisesRegex(ValueError, "invalid_run_schema"):
                load_run_state(path)
            loaded = load_hosted_state(path, runtime())
            self.assertEqual(loaded["contract"], value["contract"])
            changed = runtime()
            changed["release_id"] = "other-release"
            with self.assertRaisesRegex(MixedContractError, "runtime_mismatch"):
                load_hosted_state(path, changed)

    def test_compare_and_swap_prevents_lost_work_and_contract_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            path = Path(tmp)/"state.json"
            value = envelope()
            save_hosted_state(path, value, runtime())
            first = load_hosted_state(path, runtime())
            stale = load_hosted_state(path, runtime())
            save_hosted_state(path, first, runtime())
            with self.assertRaisesRegex(MixedContractError, "revision_conflict"):
                save_hosted_state(path, stale, runtime())
            self.assertEqual(load_hosted_state(path, runtime())["legacy_state"]["revision"], first["legacy_state"]["revision"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_worker_model_effort_and_validator_policy_drift_reject(self):
        for field in ("provider", "selector", "resolved_model", "reasoning_effort", "validator_required"):
            value = envelope()
            task = value["legacy_state"]["tasks"][0]
            if field in ("reasoning_effort", "validator_required"):
                task[field] = "low" if field == "reasoning_effort" else False
            else:
                task["provider_model"][field] = "other"
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_hosted_state(value, runtime())

    def test_residual_inherits_root_but_not_authority_expansion(self):
        value = envelope()
        state = value["legacy_state"]
        root = state["tasks"][0]
        residual = _residual_task(root, {"scope":"Finish remaining bounded check"}, None)
        state["tasks"].append(residual)
        state["validation_status"][residual["task_id"]] = "pending"
        validate_hosted_state(value, runtime())
        self.assertEqual(assignment_for_task(value["contract"], state, residual["task_id"])["assignment_id"], root["task_id"])
        residual["provider_model"]["resolved_model"] = "substitute"
        with self.assertRaisesRegex(MixedContractError, "binding_drift"):
            validate_hosted_state(value, runtime())
        residual["provider_model"] = deepcopy(root["provider_model"])
        residual["allowed_paths"] = [str(ROOT)]
        with self.assertRaisesRegex(MixedContractError, "authority_expansion"):
            validate_hosted_state(value, runtime())

    def test_unknown_root_and_cycles_not_frozen_authority(self):
        value = envelope()
        state = value["legacy_state"]
        root = state["tasks"][0]
        extra = _residual_task(root, "Additional task", None)
        state["tasks"].append(extra)
        extra["parent_task_id"] = None
        with self.assertRaisesRegex(MixedContractError, "not_frozen"):
            assignment_for_task(value["contract"], state, extra["task_id"])
        extra["parent_task_id"] = extra["task_id"]
        with self.assertRaisesRegex(MixedContractError, "cycle"):
            assignment_for_task(value["contract"], state, extra["task_id"])


if __name__ == "__main__":
    unittest.main()
