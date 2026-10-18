import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentfanout.v2_telemetry import clean_event, record_event, summarize


class V2TelemetryTests(unittest.TestCase):
    def event(self, **changes):
        return dict(event_id="e1", run_id="r1", event_type="usage", role="worker",
                    host="test-host", provider="Codex", request_id="request1", usage_basis="request_delta",
                    input_tokens=100, cached_input_tokens=80, output_tokens=20,
                    reasoning_tokens=10, **changes)

    def test_no_double_count_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            self.assertTrue(record_event(path, self.event()))
            self.assertTrue(record_event(path, self.event()))
            report = summarize(path, "r1")
            self.assertEqual(report["usage_records"], 1)
            self.assertEqual(report["totals"]["total_tokens"], 120)
            self.assertIsNone(report["totals"]["cost_usd"])
            self.assertEqual(report["unknown_model_records"], 1)
            self.assertFalse(report["complete_task_accounting_verified"])

    def test_unknown_fields_not_persisted(self):
        payload = clean_event(self.event(prompt="private text", raw_response="private text"))
        self.assertNotIn("prompt", payload)
        self.assertNotIn("raw_response", payload)

    def test_conflicting_replay_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            self.assertTrue(record_event(path, self.event()))
            event = self.event()
            event["output_tokens"] = 25
            self.assertFalse(record_event(path, event))
            self.assertEqual(summarize(path, "r1")["totals"]["output_tokens"], 20)

    def test_request_replay_new_event_id_not_double_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            self.assertTrue(record_event(path, self.event()))
            event = self.event()
            event["event_id"] = "new-delivery"
            self.assertTrue(record_event(path, event))
            self.assertEqual(summarize(path, "r1")["usage_records"], 1)

    def test_cumulative_usage_rejected(self):
        event = self.event()
        event["usage_basis"] = "session_cumulative"
        with self.assertRaises(ValueError):
            clean_event(event)

    def test_earlier_experimental_schema_rejected_on_read_and_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE v2_events (event_id TEXT PRIMARY KEY, run_id TEXT, payload TEXT)")
            connection.commit()
            connection.close()
            self.assertFalse(record_event(path, self.event()))
            self.assertEqual(summarize(path, "r1")["status"], "unavailable")

    def test_invalid_metrics_and_labels(self):
        for key, value in (("input_tokens", -1), ("duration_ms", float("nan")),
                           ("model_requests", True), ("assignment_id", "a/path"),
                           ("requested_model", "sk-" + "a" * 48)):
            event = self.event()
            event[key] = value
            with self.assertRaises(ValueError):
                clean_event(event)

    def test_unknown_component_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            record_event(path, self.event())
            record_event(path, {"event_id": "e2", "run_id": "r1", "event_type": "usage", "role": "parent", "host": "test-host", "provider": "Codex", "request_id": "request2", "usage_basis": "request_delta"})
            self.assertIsNone(summarize(path, "r1")["totals"]["input_tokens"])

    def test_fail_open_missing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(record_event(Path(directory) / "missing" / "usage.sqlite3", self.event()))

    def test_mixed_cost_bases_not_summed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.sqlite3"
            for i, basis in enumerate(("actual_charge", "api_equivalent_estimate")):
                event = self.event(cost_usd=0.1, cost_basis=basis)
                event["event_id"] = str(i)
                event["request_id"] = "request" + str(i)
                record_event(path, event)
            self.assertIsNone(summarize(path, "r1")["totals"]["cost_usd"])


if __name__ == "__main__":
    unittest.main()
