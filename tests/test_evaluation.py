from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentfanout.evaluation import EvaluationError, compare_trials


def trial(serial=True):
    return {"schema":"EvaluationTrialV1", "trial_id":"serial" if serial else "parallel", "evidence_kind":"synthetic", "evidence_sha256":("a" if serial else "b")*64,"assignment_sha256":"c"*64,"bindings_sha256":"d"*64,"inference_profile_sha256":"e"*64,"workload_condition":"warm","concurrency":1 if serial else 2,"usage_complete":True,"metrics":{"elapsed_seconds":10 if serial else 5,"queue_seconds":1,"load_seconds":0,"accepted_tasks":2,"quality_score":0.8,"evidence_pass_rate":1,"repair_rate":0,"input_tokens":100,"cached_input_tokens":20,"reasoning_tokens":40,"output_tokens":50,"parent_context_tokens":70,"cancellation_uncertain":False,"memory_high_water_by_domain":{"domain-a":1000,"domain-b":2000},"provider_cost_usd":None,"subscription_quota_remaining":None}}


class EvaluationTests(unittest.TestCase):
    def test_synthetic_matched_comparison_cannot_claim_live_or_dollar_savings(self):
        result=compare_trials(trial(),trial(False),experiment="concurrency")
        self.assertFalse(result["live_claim_permitted"])
        self.assertIsNone(result["provider_cost_difference_usd"])
        self.assertIsNone(result["performance_savings_claim"])
        self.assertEqual(result["accepted_tasks_per_second"],{"serial":0.2,"candidate":0.4})
        self.assertEqual(set(result["memory_high_water_by_domain"]["candidate"]),{"domain-a","domain-b"})

    def test_unlike_models_efforts_workloads_and_warmth_not_concurrency_evidence(self):
        for field in ("assignment_sha256","bindings_sha256","inference_profile_sha256","workload_condition"):
            candidate=trial(False)
            candidate[field]="f"*64 if field!="workload_condition" else "cold"
            with self.subTest(field=field), self.assertRaisesRegex(EvaluationError,"unmatched"):
                compare_trials(trial(),candidate,experiment="concurrency")

    def test_routing_is_separate_experiment_at_matched_concurrency(self):
        candidate=trial(False)
        candidate["bindings_sha256"]="f"*64
        with self.assertRaises(EvaluationError):
            compare_trials(trial(),candidate,experiment="routing")
        candidate["concurrency"]=1
        self.assertEqual(compare_trials(trial(),candidate,experiment="routing")["experiment"],"routing")

    def test_incomplete_usage_or_uncertain_cancellation_deny_comparison(self):
        for field in ("usage_complete","cancellation_uncertain"):
            candidate=trial(False)
            if field=="usage_complete":candidate[field]=False
            else:candidate["metrics"][field]=True
            with self.subTest(field=field), self.assertRaises(EvaluationError):
                compare_trials(trial(),candidate,experiment="concurrency")

    def test_live_and_synthetic_records_cannot_be_combined(self):
        candidate=trial(False);candidate["evidence_kind"]="live"
        with self.assertRaisesRegex(EvaluationError,"mixed_synthetic_live"):
            compare_trials(trial(),candidate,experiment="concurrency")
