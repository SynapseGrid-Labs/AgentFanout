import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentfanout.provider_adapters import (
    GROK_DIRECT_ENDPOINT_SHA256, ProviderAdapterError, grok_auth_file_identity,
    grok_http_helper_identity,
)
from agentfanout.runner import GrokValidatorAdapter, GrokWorkerAdapter
from agentfanout.runner import HostedFanoutRunner
from agentfanout.lifecycle import build_task_units, initialize_run, save_run_state
from agentfanout.project_binding import resolve_project_binding


FAKE_GROK_DISCOVERY = r'''#!__PYTHON__
import sys

if sys.argv[1:] == ["models"]:
    print("Available models:\n  - grok-4.5")
    raise SystemExit(0)
raise SystemExit(19)
'''


class GrokHostedAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.document = self.project / "README.md"
        self.document.write_text("alpha\n", encoding="utf-8")

        self.grok_home = Path(self.tmp.name) / "grok-home"
        self.grok_home.mkdir(mode=0o700)
        self.grok_home.chmod(0o700)
        auth = self.grok_home / "auth.json"
        auth.write_text(json.dumps({
            "schema": "GrokApiCredentialV1",
            "api_key": "fixture-api-key-" + "x" * 64,
        }) + "\n", encoding="utf-8")
        auth.chmod(0o600)
        self.auth_identity = grok_auth_file_identity(self.grok_home)
        self.helper_identity = grok_http_helper_identity()

        self.fake = Path(self.tmp.name) / "fake-grok-discovery.py"
        self.fake.write_text(
            FAKE_GROK_DISCOVERY.replace("__PYTHON__", sys.executable),
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.binary = [sys.executable, str(self.fake)]

        content = self.document.read_bytes()
        self.task = {
            "schema": "WorkerTaskV1", "task_id": "task_grok_fixture",
            "title": "Read one bounded fixture", "scope": "README only",
            "input_ref": "file:README.md",
            "input_sha256": hashlib.sha256(content).hexdigest(),
            "input_bytes": len(content), "allowed_paths": [str(self.project)],
            "task_class": "bulk-read", "role": "explorer",
            "reasoning_effort": "low", "parent_task_id": "root",
            "launch_correlation_id": "launch_grok_fixture",
            "provider_model": {"profile": "fast", "selector": "grok-fast"},
            "budgets": {
                "max_prompt_tokens": 4096,
                "max_output_tokens": 321,
                "max_cost_usd": 1.0,
            },
        }
        self.helper_requests = []

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self):
        return GrokWorkerAdapter(
            self.project, self.binary, {"grok-fast": "grok-4.5"},
            grok_home=self.grok_home,
            expected_auth_file_identity=self.auth_identity,
            expected_helper_identity=self.helper_identity,
        )

    def validator(self):
        return GrokValidatorAdapter(
            self.project, self.binary, {"grok-fast": "grok-4.5"},
            grok_home=self.grok_home,
            expected_auth_file_identity=self.auth_identity,
            expected_helper_identity=self.helper_identity,
        )

    def fake_helper_process(self, *args, **kwargs):
        argv, serialized = args[:2]
        self.assertEqual(argv[1:3], ["-I", "-S"])
        request = json.loads(serialized)
        self.helper_requests.append(request)
        prompt_packet = json.loads(request["prompt"].rsplit("\n", 1)[-1])
        if "worker_result" in prompt_packet:
            task = prompt_packet["task"]
            packet = {
                "schema": "ValidatorResultV1",
                "validator_id": "fake-grok-https-validator",
                "verdict": "pass", "evidence_refs": [task["input_ref"]],
                "material_findings": [], "contradictions": [],
                "checked_at": "2026-07-10T00:00:00Z",
            }
        else:
            packet = {
                "schema": "WorkerResultV1",
                "result_id": prompt_packet["expected_result_id"],
                "task_id": prompt_packet["task_id"], "status": "success",
                "summary": "The bounded fixture says alpha.",
                "evidence": [prompt_packet["input_ref"] + ": alpha"],
                "changed_files": [], "commands": [], "exit_codes": [],
                "remaining_work": [],
                "launch_correlation_id": prompt_packet["launch_correlation_id"],
            }
        content = json.dumps(packet, sort_keys=True)
        response = {
            "schema": "GrokDirectResponseV1", "model": request["model"],
            "reasoning_effort": request["reasoning_effort"],
            "reasoning_effort_mode": request["reasoning_effort_mode"],
            "output_contract": request["output_contract"],
            "endpoint_sha256": GROK_DIRECT_ENDPOINT_SHA256, "tools_field_sent": False,
            "response_format_type": "json_schema",
            "response_schema_name": request["output_contract"],
            "redirects_allowed": False, "ambient_proxy_allowed": False,
            "response_id_sha256": "b" * 64, "event_count": 4,
            "response_bytes": len(content.encode("utf-8")), "content": content,
            "usage": {
                "input_tokens": 18, "cached_tokens": 2,
                "uncached_input_tokens": 16, "output_tokens": 7,
                "total_tokens": 25, "usage_verified": True,
                "cost_usd": None, "cost_verified": False,
                "provider_cost_ticks_present": True,
            },
        }
        return {
            "status": "completed", "returncode": 0,
            "stdout": json.dumps(response), "stderr": "",
            "output_truncated": False, "duration_ms": 12.5,
            "process_group_termination_confirmed": None,
            "termination_scope": "process_group_only",
            "detached_session_descendants_covered": False,
            "process_group_exit_confirmed": True,
            "environment_audit": {
                "preserved": [], "stripped_credential_names": [],
            },
            "executable_pinning": {"method": "sealed_memfd"},
        }

    def assignment_and_receipt(self, adapter):
        assignment = adapter.preflight({
            "task": dict(self.task), "task_id": self.task["task_id"],
            "launch_correlation_id": self.task["launch_correlation_id"],
            "host_task": dict(self.task),
        })
        receipt = {
            "launch_correlation_id": self.task["launch_correlation_id"],
            "model_selector": "grok-fast", "reasoning_effort": "low",
            "resolved_model": "grok-4.5",
        }
        return assignment, receipt

    def test_worker_uses_direct_https_helper_and_authoritative_usage(self):
        adapter = self.worker()
        self.assertFalse(adapter.supports_context_reuse)
        identity = adapter.identity()
        self.assertEqual(
            identity["adapter_id"],
            "agentfanout.grok.https.no-tools.bounded-file",
        )
        self.assertEqual(identity["adapter_version"], "2")
        self.assertEqual(identity["transport"], "direct_xai_chat_completions_sse")
        self.assertFalse(identity["tool_schema_sent"])
        self.assertRegex(identity["grok_http_helper_identity_sha256"], r"^[a-f0-9]{64}$")

        assignment, receipt = self.assignment_and_receipt(adapter)
        with patch(
            "agentfanout.provider_adapters.run_json_process",
            side_effect=self.fake_helper_process,
        ) as helper:
            output = adapter.run(assignment, receipt, lambda _observed: None)

        self.assertEqual(helper.call_count, 1)
        self.assertEqual(self.helper_requests[0]["schema"], "GrokDirectRequestV1")
        self.assertEqual(self.helper_requests[0]["output_contract"], "WorkerResultV1")
        self.assertEqual(self.helper_requests[0]["max_output_tokens"], 321)
        self.assertNotIn("tools", self.helper_requests[0])
        self.assertTrue(output["process_group_exit_confirmed"])
        self.assertEqual(output["termination_scope"], "process_group_only")
        self.assertEqual(output["usage"]["input_tokens"], 18)
        self.assertEqual(output["usage"]["cached_tokens"], 2)
        self.assertEqual(output["usage"]["uncached_input_tokens"], 16)
        self.assertEqual(output["usage"]["output_tokens"], 7)
        self.assertEqual(output["usage"]["total_tokens"], 25)
        self.assertTrue(output["usage"]["usage_verified"])
        self.assertTrue(output["usage"]["provider_cost_ticks_present"])
        self.assertEqual(output["result"]["summary"], "The bounded fixture says alpha.")
        self.assertEqual(output["result"]["changed_files"], [])

    def test_worker_budget_is_bound_before_receipt_or_provider_call(self):
        adapter = self.worker()
        task = dict(self.task)
        task["budgets"] = {
            **self.task["budgets"],
            "max_prompt_tokens": 512,
            "max_output_tokens": 17,
        }
        with self.assertRaisesRegex(
            ProviderAdapterError, "grok_prompt_exceeds_prelaunch_token_ceiling",
        ):
            adapter.preflight({
                "task": task, "task_id": task["task_id"],
                "launch_correlation_id": task["launch_correlation_id"],
                "host_task": task,
            })
        self.assertEqual(self.helper_requests, [])

    def test_validator_is_a_separate_direct_https_helper_call(self):
        worker = self.worker()
        assignment, receipt = self.assignment_and_receipt(worker)
        validator = self.validator()

        with patch(
            "agentfanout.provider_adapters.run_json_process",
            side_effect=self.fake_helper_process,
        ) as helper:
            worker_output = worker.run(assignment, receipt, lambda _observed: None)
            prepared = validator.prepare(self.task, worker_output["result"], "a" * 64)
            self.assertEqual(prepared["resolved_model"], "grok-4.5")
            validator_output = validator.run(self.task, worker_output["result"])

        self.assertEqual(helper.call_count, 2)
        self.assertEqual(len(self.helper_requests), 2)
        self.assertIn("WorkerResultV1", self.helper_requests[0]["prompt"])
        self.assertIn("ValidatorResultV1", self.helper_requests[1]["prompt"])
        self.assertNotEqual(
            self.helper_requests[0]["prompt"], self.helper_requests[1]["prompt"],
        )
        self.assertTrue(validator_output["process_group_exit_confirmed"])
        self.assertEqual(validator_output["result"]["verdict"], "pass")
        self.assertEqual(validator_output["result"]["contradictions"], [])
        self.assertTrue(validator_output["usage"]["usage_verified"])

    def test_hosted_runner_binds_nondefault_caps_into_worker_and_validator_requests(self):
        state_file = Path(self.tmp.name) / "run.json"
        ledger_file = Path(self.tmp.name) / "ledger.sqlite3"
        second = self.project / "SECOND.md"
        second.write_text("beta\n", encoding="utf-8")
        tasks = build_task_units(
            "review README.md", "bulk-read", "explorer", "Grok", "grok-fast",
            "low", ["file:README.md", "file:SECOND.md"],
            allowed_paths=[str(self.project)],
            budget={
                "max_worker_prompt_tokens": 4096,
                "max_worker_output_tokens": 321,
                "max_total_cost_usd": 2.0,
            },
        )
        for task in tasks:
            content = (self.project / task["input_ref"].removeprefix("file:")).read_bytes()
            task["input_sha256"] = hashlib.sha256(content).hexdigest()
            task["input_bytes"] = len(content)
        state = initialize_run(
            "review README.md", tasks,
            {
                "max_worker_prompt_tokens": 4096,
                "max_worker_output_tokens": 321,
                "max_total_cost_usd": 2.0,
            },
        )
        state["project_binding"] = resolve_project_binding(
            explicit_root=self.project, env_root="",
        )
        save_run_state(state_file, state)
        manifest = json.loads((
            Path(__file__).resolve().parents[1]
            / "integrations" / "codex" / "standalone-runner-manifest.json"
        ).read_text(encoding="utf-8"))
        with patch(
            "agentfanout.provider_adapters.run_json_process",
            side_effect=self.fake_helper_process,
        ):
            outcome = HostedFanoutRunner(
                state_file, ledger_file, {"Grok": self.worker()}, self.validator(),
                max_workers=1, host_manifest=manifest, require_adapter_identity=True,
            ).run()
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(len(self.helper_requests), 4)
        self.assertTrue(all(
            request["max_output_tokens"] == 321
            for request in self.helper_requests
        ))
        validator_packets = [
            json.loads(request["prompt"].rsplit("\n", 1)[-1])
            for request in self.helper_requests
            if "ValidatorResultV1" in request["prompt"]
        ]
        self.assertEqual(len(validator_packets), 2)
        self.assertTrue(all(
            len(packet["peer_worker_results"]) == 1
            for packet in validator_packets
        ))
        self.assertEqual(
            {packet["peer_worker_results"][0]["task_id"] for packet in validator_packets},
            {task["task_id"] for task in tasks},
        )
        self.assertTrue(all(
            packet["peer_worker_results"][0]["evidence"]
            for packet in validator_packets
        ))


if __name__ == "__main__":
    unittest.main()
