"""Offline-only tests for the synthetic observability evaluation harness."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))

import harness


class ObservabilityHarnessTests(unittest.TestCase):
    def test_synthetic_events_are_allowlisted_and_redacted(self):
        events = harness.synthetic_events()
        self.assertEqual([event["event_type"] for event in events], ["route", "launch", "result", "validation", "terminal"])
        route = events[0]["fields"]
        self.assertNotIn("path", route)
        self.assertNotIn("api_key", route)
        serialized = json.dumps(events)
        self.assertNotIn("must-not-export", serialized)
        self.assertIn("cost_usd", serialized)

    def test_otlp_payload_has_root_and_linked_children(self):
        spans = harness.synthetic_spans()
        self.assertEqual(len(spans), 6)
        self.assertEqual(spans[0]["parent_span_id"], b"")
        self.assertTrue(all(item["parent_span_id"] == harness.SYNTHETIC_ROOT_SPAN_ID for item in spans[1:]))
        self.assertEqual(len({item["span_id"] for item in spans}), 6)
        payload = harness.otlp_trace_protobuf(spans)
        self.assertGreater(len(payload), 200)
        self.assertIn(b"agentfanout.run", payload)
        self.assertIn(b"agentfanout.cost_usd", payload)
        self.assertNotIn(b"must-not-export", payload)

    def test_phoenix_request_is_exact_private_otlp_protobuf(self):
        with patch.object(harness, "PHOENIX_OTLP_ENDPOINT", "http://telemetry.example.invalid:6006/v1/traces"), patch.dict(
            "harness.os.environ", {"AGENT_FANOUT_EVAL_ALLOWED_HOSTS": "telemetry.example.invalid"}
        ):
            request = harness.phoenix_request(b"fixture")
        self.assertEqual(request.full_url, "http://telemetry.example.invalid:6006/v1/traces")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/x-protobuf")
        self.assertEqual(request.data, b"fixture")

    def test_host_guard_requires_explicit_exact_binding(self):
        with patch.dict("harness.os.environ", {}, clear=True), self.assertRaisesRegex(RuntimeError, "evaluation_hostname_required"):
            harness.require_evaluation_host()
        with patch.dict("harness.os.environ", {"AGENT_FANOUT_EVAL_HOSTNAME": "worker"}), patch(
            "harness.socket.gethostname", return_value="other"
        ), self.assertRaisesRegex(RuntimeError, "evaluation_host_mismatch"):
            harness.require_evaluation_host()
        with patch.dict("harness.os.environ", {"AGENT_FANOUT_EVAL_HOSTNAME": "worker"}), patch(
            "harness.socket.gethostname", return_value="worker.example"
        ):
            harness.require_evaluation_host()

    def test_weighted_rubric_is_deterministic_and_requires_complete_scores(self):
        rubric = json.loads((Path(harness.__file__).with_name("rubric.json")).read_text(encoding="utf-8"))
        complete = {item["id"]: 5 for item in rubric["criteria"]}
        self.assertEqual(harness.weighted_score(complete, rubric), {"weighted_score": 100.0, "missing_criteria": [], "complete": True})
        partial = harness.weighted_score({"trace_hierarchy": 5}, rubric)
        self.assertEqual(partial["weighted_score"], 20.0)
        self.assertFalse(partial["complete"])

    def test_offline_report_writes_only_synthetic_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            self.assertEqual(harness.main(["--report-out", str(path)]), 0)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(report["synthetic_data_only"])
        self.assertIsNone(report["ingestion"]["phoenix"]["http_status"])
        self.assertEqual(report["trace"]["span_count"], 6)
        self.assertIn("agentfanout.cost_usd", report["query_keys"])
        self.assertEqual(report["ingestion"]["langfuse"]["trace_hierarchy_mode"], "existing_event_sink_only; group manually by sanitized run_id")

    def test_report_never_probes_device_or_includes_configured_destination(self):
        rubric = json.loads((Path(harness.__file__).with_name("rubric.json")).read_text(encoding="utf-8"))
        endpoint = "http://fixture-user:fixture-pass@telemetry.example.invalid/v1/traces?fixture=private-query#private-fragment"
        with patch.object(harness, "PHOENIX_OTLP_ENDPOINT", endpoint), patch(
            "shutil.disk_usage", side_effect=AssertionError("device probe forbidden")
        ) as disk, patch.object(harness.Path, "read_text", side_effect=AssertionError("device read forbidden")) as reads, patch(
            "harness.socket.gethostname", side_effect=AssertionError("device name forbidden")
        ) as hostname:
            report = harness.report_payload(None, None, rubric)
        serialized = json.dumps(report)
        for value in (endpoint, "fixture-user", "fixture-pass", "private-query", "private-fragment"):
            self.assertNotIn(value, serialized)
        for field in ("disk_total_bytes", "disk_free_bytes", "memory_available_kib", "phoenix_endpoint"):
            self.assertNotIn(field, serialized)
        self.assertEqual(report["resource_observations"]["collection"], "not_collected")
        disk.assert_not_called()
        reads.assert_not_called()
        hostname.assert_not_called()

    def test_offline_receipt_does_not_echo_local_report_path(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(output):
            path = Path(tmp) / "fixture-private-output" / "report.json"
            with patch.object(harness, "send_phoenix", side_effect=AssertionError("network forbidden")), patch.object(
                harness, "send_langfuse", side_effect=AssertionError("network forbidden")
            ):
                self.assertEqual(harness.cli(["--report-out", str(path)]), 0)
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        receipt = output.getvalue()
        self.assertNotIn(str(path), receipt)
        self.assertNotIn("report_out", receipt)
        self.assertTrue(json.loads(receipt)["report_written"])

    def test_host_mismatch_reports_code_without_device_identity(self):
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(errors), patch.dict(
            "harness.os.environ", {"AGENT_FANOUT_EVAL_HOSTNAME": "fixture-expected-device"}
        ), patch("harness.socket.gethostname", return_value="fixture-observed-device"), patch.object(
            harness, "send_phoenix", side_effect=AssertionError("network forbidden")
        ) as send:
            path = Path(tmp) / "report.json"
            self.assertEqual(harness.cli(["--ingest", "--target", "phoenix", "--report-out", str(path)]), 2)
            self.assertFalse(path.exists())
        self.assertEqual(json.loads(errors.getvalue())["reason"], "evaluation_host_mismatch")
        for value in ("fixture-expected-device", "fixture-observed-device", str(path)):
            self.assertNotIn(value, errors.getvalue())
        send.assert_not_called()

    def test_endpoint_rejects_userinfo_query_and_fragment(self):
        base = "http://telemetry.example.invalid/v1/traces"
        urls = {
            "userinfo": "http://fixture-user:fixture-pass@telemetry.example.invalid/v1/traces",
            "query": base + "?fixture=private-query",
            "fragment": base + "#private-fragment",
        }
        for kind, endpoint in urls.items():
            with self.subTest(endpoint_kind=kind), patch.object(harness, "PHOENIX_OTLP_ENDPOINT", endpoint), patch.dict(
                "harness.os.environ", {"AGENT_FANOUT_EVAL_ALLOWED_HOSTS": "telemetry.example.invalid"}
            ), self.assertRaisesRegex(ValueError, "^phoenix_evaluation_endpoint_not_allowed$"):
                harness.phoenix_request(b"fixture")

    def test_cli_failure_omits_network_error_details(self):
        failures = (OSError("fixture-endpoint fixture-local-path"), BadStatusLine("fixture-server-detail"), IncompleteRead(b"fixture-server-detail"))
        for failure in failures:
            errors = io.StringIO()
            with self.subTest(error_type=type(failure).__name__), tempfile.TemporaryDirectory() as tmp, redirect_stderr(errors), patch.object(
                harness, "require_evaluation_host"
            ), patch.object(harness, "send_phoenix", side_effect=failure):
                self.assertEqual(harness.cli(["--ingest", "--target", "phoenix", "--report-out", str(Path(tmp) / "report.json")]), 2)
            self.assertEqual(json.loads(errors.getvalue())["reason"], "evaluation_failed")
            for value in ("fixture-endpoint", "fixture-local-path", "fixture-server-detail", "Traceback"):
                self.assertNotIn(value, errors.getvalue())

    def test_langfuse_rejects_endpoint_metadata_before_creating_sink(self):
        base = "https://telemetry.example.invalid"
        urls = {
            "userinfo": "https://fixture-user:fixture-pass@telemetry.example.invalid",
            "query": base + "?fixture=private-query",
            "fragment": base + "#private-fragment",
        }
        for kind, endpoint in urls.items():
            config = {"LANGFUSE_HOST": endpoint, "LANGFUSE_ALLOWED_HOSTS": "telemetry.example.invalid", "LANGFUSE_PUBLIC_KEY": "fixture", "LANGFUSE_SECRET_KEY": "fixture"}
            with self.subTest(endpoint_kind=kind), patch.object(harness, "_read_langfuse_env", return_value=config), patch.object(
                harness, "LangfuseHttpSink", side_effect=AssertionError("sink construction forbidden")
            ) as sink, self.assertRaisesRegex(ValueError, "^langfuse_evaluation_host_not_allowed$"):
                harness.send_langfuse(harness.synthetic_events(), Path("fixture.env"))
            sink.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
