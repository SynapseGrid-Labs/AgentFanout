import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentfanout.harness import validate_manifest
from agentfanout.host_runtime import HostLedger
from agentfanout.lifecycle import build_task_units, initialize_run, save_run_state
from agentfanout.local_endpoint_worker import LocalEndpointError, normalize_local_endpoint
from agentfanout.project_binding import resolve_project_binding
from agentfanout.provider_adapters import (
    ProviderAdapterError,
    executable_identity,
    launch_local_endpoint,
    local_helper_binary,
    validate_local_host_evidence,
)
from agentfanout.runner import HostedFanoutRunner, LocalValidatorAdapter, LocalWorkerAdapter


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "integrations" / "codex" / "standalone-runner-manifest.json"


class _EndpointHandler(BaseHTTPRequestHandler):
    server_version = "AgentFanoutFakeLocal/1"

    def log_message(self, _format, *_args):
        return

    def _json(self, value, status=200):
        encoded = json.dumps(value).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @property
    def state(self):
        return self.server.state

    def do_GET(self):
        self.state["requests"].append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        if self.state["mode"] == "redirect_models":
            self.send_response(302)
            self.send_header("Location", self.state["endpoint"] + "/models-elsewhere")
            self.end_headers()
            return
        if self.state["mode"] == "delay":
            time.sleep(2)
        if self.path != "/v1/models":
            self._json({"error": "not found"}, 404)
            return
        listed_model = "different-loaded-model" if self.state["mode"] == "model_not_loaded" else self.state["model"]
        self._json({"object": "list", "data": [{"id": listed_model}]})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        self.state["requests"].append({
            "method": "POST", "path": self.path, "headers": dict(self.headers),
            "payload": payload,
        })
        if self.path != "/v1/chat/completions" or not isinstance(payload, dict):
            self._json({"error": "not found"}, 404)
            return
        prompt = payload.get("messages", [{}])[0].get("content", "")
        if "ValidatorResultV1" in prompt:
            content = json.dumps({
                "schema": "ValidatorResultV1", "validator_id": "local-validator",
                "verdict": "pass", "evidence_refs": ["file:sample.txt"],
                "material_findings": [], "contradictions": [],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        else:
            content = json.dumps({
                "schema": "WorkerResultV1", "result_id": "worker-supplied",
                "task_id": "worker-supplied", "status": "success",
                "summary": "The bounded fixture contains alpha.",
                "evidence": ["file:sample.txt: alpha"], "changed_files": [],
                "commands": [], "exit_codes": [], "remaining_work": [],
                "launch_correlation_id": "worker-supplied",
            })
        response = {
            "id": "fixture-response", "model": self.state["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 41, "completion_tokens": 17,
                      "prompt_tokens_details": {"cached_tokens": 3}},
        }
        if self.state["mode"] == "model_mismatch":
            response["model"] = "different-loaded-model"
        elif self.state["mode"] == "missing_usage":
            response.pop("usage")
        elif self.state["mode"] == "tool_call":
            response["choices"][0]["message"]["tool_calls"] = [{"id": "denied"}]
            response["choices"][0]["finish_reason"] = "tool_calls"
        elif self.state["mode"] == "usage_total_mismatch":
            response["usage"]["total_tokens"] = 999
        elif self.state["mode"] == "output_tokens_exceeds_limit":
            response["usage"]["completion_tokens"] = 999999
        elif self.state["mode"] == "finish_reason_invalid":
            response["choices"][0]["finish_reason"] = "unknown"
        self._json(response)


class _FakeEndpoint:
    def __init__(self, model="fixture-local-model", mode="ok"):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _EndpointHandler)
        host, port = self.server.server_address
        endpoint = f"http://{host}:{port}/v1"
        self.server.state = {"model": model, "mode": mode, "endpoint": endpoint, "requests": []}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self):
        return self.server.state["endpoint"]

    @property
    def requests(self):
        return self.server.state["requests"]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _evidence(endpoint, *, model="fixture-local-model", selector="local-worker", effort="low",
              task_class="bulk-read", tools=None, supports_editing=False, observed=None):
    now = observed or datetime.now(timezone.utc)
    attestation = {
        "schema": "LocalCapabilityAttestationV1",
        "attestation_id": "localcap_" + ("5" * 32),
        "source": "host", "issuer": "host-runtime", "selector_alias": selector,
        "exact_model_id": model, "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "active_mode": "verified-local-mode", "context_window": 32768,
        "evaluated_task_classes": [task_class],
        "supported_reasoning_efforts": [effort], "max_risk": "medium",
        "tools": [] if tools is None else tools,
        "supports_file_editing": supports_editing,
    }
    bridge = {
        "model_alias": selector, "model_id": model, "endpoint": endpoint,
        "context_window": 32768, "profiles": ["bounded_file_text", "no_tools"],
        "checked_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "source": "fixture-host-probe",
    }
    return attestation, bridge


def _environment(project, endpoint):
    attestation, bridge = _evidence(endpoint)
    return {
        "AGENT_FANOUT_PROJECT_ROOT": str(project),
        "AGENT_FANOUT_LOCAL_AVAILABLE": "1",
        "AGENT_FANOUT_LOCAL_HOSTED_NO_TOOL": "1",
        "AGENT_FANOUT_LOCAL_ALLOW_LOOPBACK_HTTP": "1",
        "AGENT_FANOUT_LOCAL_ENDPOINT": endpoint,
        "AGENT_FANOUT_LOCAL_MODEL_ID": "fixture-local-model",
        "AGENT_FANOUT_LOCAL_MODEL_ALIAS": "local-worker",
        "AGENT_FANOUT_LOCAL_REQUIRED_MODE": "verified-local-mode",
        "AGENT_FANOUT_LOCAL_ATTESTATION_JSON": json.dumps(attestation),
        "AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON": json.dumps(bridge),
    }


class TamperedLocalWorker(LocalWorkerAdapter):
    def prepare(self, assignment, tool_policy_sha256):
        prepared = super().prepare(assignment, tool_policy_sha256)
        prepared["local_no_tool_boundary"]["endpoint_sha256"] = "0" * 64
        return prepared


class TamperedLocalValidator(LocalValidatorAdapter):
    def prepare(self, task, result, tool_policy_sha256):
        prepared = super().prepare(task, result, tool_policy_sha256)
        prepared["local_no_tool_boundary"]["endpoint_sha256"] = "0" * 64
        return prepared


class LocalHostedAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.source = self.root / "sample.txt"
        self.source.write_text("alpha\nbeta\n", encoding="utf-8")
        self.run_counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_helper_uses_isolated_python_and_binds_interpreter_and_helper(self):
        argv = local_helper_binary()
        self.assertEqual(argv[1:3], ["-I", "-S"])
        identity = executable_identity(argv)
        self.assertEqual(identity["executable_realpath"], str(Path(sys.executable).resolve()))
        helper = next(item for item in identity["argument_file_identities"] if item["argv_index"] == 3)
        self.assertTrue(helper["realpath"].endswith("agentfanout/local_endpoint_worker.py"))

    def _state(self):
        task = build_task_units(
            "Summarize the bounded file", "bulk-read", "explorer", "Local",
            "local-worker", "low", ["file:sample.txt"],
            allowed_paths=[str(self.root)], validator_required=True,
        )[0]
        content = self.source.read_text(encoding="utf-8")
        task["input_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        task["input_bytes"] = len(content.encode())
        state = initialize_run("Summarize one bounded Local fixture", [task])
        state["project_binding"] = resolve_project_binding(explicit_root=self.root)
        return state

    def _run(
        self, endpoint, worker_class=LocalWorkerAdapter,
        validator_class=LocalValidatorAdapter,
    ):
        self.run_counter += 1
        state_file = Path(self.tmp.name) / f"state-{self.run_counter}.json"
        ledger_file = Path(self.tmp.name) / f"ledger-{self.run_counter}.sqlite3"
        save_run_state(state_file, self._state())
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        validate_manifest(manifest, require_level="runner_enforced")
        worker = worker_class(self.root)
        validator = validator_class(self.root)
        runner = HostedFanoutRunner(
            state_file, ledger_file, {"Local": worker}, validator,
            max_workers=1, host_manifest=manifest, require_adapter_identity=True,
        )
        outcome = runner.run(max_cycles=10)
        return outcome, ledger_file

    def test_endpoint_policy_requires_https_or_explicit_numeric_loopback(self):
        with self.assertRaisesRegex(LocalEndpointError, "http_denied"):
            normalize_local_endpoint("http://127.0.0.1:8000/v1", False)
        with self.assertRaisesRegex(LocalEndpointError, "http_denied"):
            normalize_local_endpoint("http://localhost:8000/v1", True)
        endpoint, policy = normalize_local_endpoint("http://127.0.0.1:8000/v1/", True)
        self.assertEqual(endpoint, "http://127.0.0.1:8000/v1")
        self.assertEqual(policy, "numeric_loopback_http")
        self.assertEqual(normalize_local_endpoint("https://models.example.test/v1", False)[1], "https")
        for unsafe in (
            "https://user:pass@models.example.test/v1",
            "https://models.example.test/v1?redirect=x",
            "https://models.example.test/v1/../admin",
        ):
            with self.assertRaises(LocalEndpointError):
                normalize_local_endpoint(unsafe, False)

    def test_host_evidence_is_exact_fresh_no_tool_and_effort_bound(self):
        attestation, bridge = _evidence("http://127.0.0.1:8000/v1")
        validated = validate_local_host_evidence(
            attestation, bridge, selector="local-worker", exact_model_id="fixture-local-model",
            endpoint="http://127.0.0.1:8000/v1", task_class="bulk-read",
            reasoning_effort="low", required_mode="verified-local-mode",
            allow_loopback_http=True,
        )
        self.assertEqual(validated["endpoint_policy"], "numeric_loopback_http")
        changed = dict(attestation)
        changed["supported_reasoning_efforts"] = ["medium"]
        with self.assertRaisesRegex(ProviderAdapterError, "effort_not_attested"):
            validate_local_host_evidence(
                changed, bridge, selector="local-worker", exact_model_id="fixture-local-model",
                endpoint="http://127.0.0.1:8000/v1", task_class="bulk-read",
                reasoning_effort="low", required_mode="verified-local-mode", allow_loopback_http=True,
            )
        tool_capable = dict(attestation)
        tool_capable.update({"tools": ["shell"], "supports_file_editing": True})
        with self.assertRaisesRegex(ProviderAdapterError, "no_tool_capability_required"):
            validate_local_host_evidence(
                tool_capable, bridge, selector="local-worker", exact_model_id="fixture-local-model",
                endpoint="http://127.0.0.1:8000/v1", task_class="bulk-read",
                reasoning_effort="low", required_mode="verified-local-mode", allow_loopback_http=True,
            )
        stale_at = datetime.now(timezone.utc) - timedelta(hours=1)
        stale_attestation, stale_bridge = _evidence("http://127.0.0.1:8000/v1", observed=stale_at)
        with self.assertRaisesRegex(ProviderAdapterError, "stale"):
            validate_local_host_evidence(
                stale_attestation, stale_bridge, selector="local-worker",
                exact_model_id="fixture-local-model", endpoint="http://127.0.0.1:8000/v1",
                task_class="bulk-read", reasoning_effort="low",
                required_mode="verified-local-mode", allow_loopback_http=True,
            )

    def test_supervised_helper_requires_exact_model_usage_and_no_tool_response(self):
        for mode, expected in (
            ("model_mismatch", "process_not_clean"),
            ("missing_usage", "process_not_clean"),
            ("tool_call", "process_not_clean"),
            ("redirect_models", "process_not_clean"),
            ("model_not_loaded", "process_not_clean"),
            ("usage_total_mismatch", "process_not_clean"),
            ("output_tokens_exceeds_limit", "process_not_clean"),
            ("finish_reason_invalid", "process_not_clean"),
        ):
            with self.subTest(mode=mode), _FakeEndpoint(mode=mode) as endpoint:
                env = _environment(self.root, endpoint.endpoint)
                with patch.dict(os.environ, env, clear=False):
                    outcome, ledger_file = self._run(endpoint.endpoint)
                self.assertEqual(outcome["status"], "blocked")
                ledger = HostLedger(ledger_file)
                try:
                    blocked = ledger.events(outcome["run_id"], "runner_blocked")
                finally:
                    ledger.close()
                self.assertTrue(blocked)
                self.assertIn(expected, blocked[-1]["payload"]["reason"])

    def test_local_runner_completes_with_verified_usage_and_no_credentials(self):
        with _FakeEndpoint() as endpoint:
            env = _environment(self.root, endpoint.endpoint)
            env["AGENT_FANOUT_TEST_TOKEN"] = "must-not-reach-helper-or-endpoint"
            with patch.dict(os.environ, env, clear=False):
                outcome, ledger_file = self._run(endpoint.endpoint)
            self.assertEqual(outcome["status"], "complete", outcome)
            posts = [item for item in endpoint.requests if item["method"] == "POST"]
            self.assertEqual(len(posts), 2)
            for request in posts:
                lowered = {key.lower(): value for key, value in request["headers"].items()}
                self.assertNotIn("authorization", lowered)
                self.assertNotIn("cookie", lowered)
                self.assertEqual(request["payload"]["model"], "fixture-local-model")
                self.assertEqual(request["payload"]["reasoning_effort"], "low")
                self.assertNotIn("tools", request["payload"])
            ledger = HostLedger(ledger_file)
            try:
                boundaries = ledger.events(outcome["run_id"], "local_no_tool_boundary")
                usages = ledger.events(outcome["run_id"], "usage_basis")
                validator_usages = ledger.events(outcome["run_id"], "validator_usage_basis")
            finally:
                ledger.close()
            self.assertEqual(len(boundaries), 3)
            self.assertEqual(len({event["payload"]["launch_correlation_id"] for event in boundaries}), 2)
            self.assertEqual(
                {event["payload"]["phase"] for event in boundaries},
                {"worker_launch", "validator_preflight", "validator_receipt"},
            )
            for boundary in boundaries:
                self.assertEqual(boundary["payload"]["auth_mode"], "none")
                self.assertFalse(boundary["payload"]["tools_enabled"])
            self.assertEqual(usages[0]["payload"]["total_input_tokens"], 41)
            self.assertEqual(validator_usages[0]["payload"]["input_tokens"], 41)

    def test_router_emits_hosted_no_tool_contract_without_vllm_tool_bridge(self):
        with _FakeEndpoint() as endpoint:
            env = os.environ.copy()
            env.update(_environment(self.root, endpoint.endpoint))
            proc = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "route.py"),
                    "AgentFanout Local\nreview sample.txt with low reasoning; I accept orchestration overhead",
                ],
                cwd=self.root, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["selected_provider"], "Local")
        self.assertFalse(payload["fanout"]["blocked"])
        launch = payload["route"]["executor"]["runtime_launch"]
        self.assertIn("local_hosted_no_tool", launch)
        self.assertNotIn("local_vllm_worker", launch)
        self.assertNotIn("local_tool_agent", launch)
        self.assertIsNone(payload["route"]["executor"]["local_agent_request"])
        self.assertEqual(launch["local_hosted_no_tool"]["auth_mode"], "none")

    def test_tampered_boundary_is_rejected_before_endpoint_launch(self):
        with _FakeEndpoint() as endpoint:
            env = _environment(self.root, endpoint.endpoint)
            with patch.dict(os.environ, env, clear=False):
                outcome, _ = self._run(endpoint.endpoint, TamperedLocalWorker)
            self.assertEqual(outcome["status"], "blocked")
            self.assertIn("local_no_tool_evidence_hash_mismatch", str(outcome))
            self.assertFalse(endpoint.requests)

    def test_tampered_validator_boundary_is_rejected_before_validator_network_call(self):
        with _FakeEndpoint() as endpoint:
            env = _environment(self.root, endpoint.endpoint)
            with patch.dict(os.environ, env, clear=False):
                outcome, _ = self._run(
                    endpoint.endpoint, validator_class=TamperedLocalValidator,
                )
            self.assertEqual(outcome["status"], "blocked")
            self.assertIn("local_no_tool_evidence_hash_mismatch", str(outcome))
            self.assertEqual(len(endpoint.requests), 2)  # worker /models + completion only

    def test_helper_process_is_cancelled_and_parent_credentials_are_stripped(self):
        with _FakeEndpoint(mode="delay") as endpoint:
            with patch.dict(os.environ, {"AGENT_FANOUT_PROJECT_ROOT": str(self.root),
                                         "AGENT_FANOUT_TEST_TOKEN": "never-in-child",
                                         "CODEX_AUTH_SOCK": "/tmp/not-for-local-codex.sock",
                                         "GROK_AUTH_SOCK": "/tmp/not-for-local-grok.sock"}, clear=False):
                result = launch_local_endpoint(
                    binary=local_helper_binary(), project_root=self.root,
                    endpoint=endpoint.endpoint, allow_loopback_http=True,
                    selector="local-worker", resolved_model="fixture-local-model",
                    reasoning_effort="low", prompt="Summarize bounded alpha.",
                    timeout_seconds=5, cancellation_requested=lambda: True,
                )
            self.assertEqual(result["status"], "cancelled")
            self.assertTrue(result["process_group_exit_confirmed"])
            self.assertEqual(result["executable_pinning"]["method"], "sealed_memfd")
            self.assertEqual(result["executable_pinning"]["argument_file_count"], 1)
            self.assertIn("AGENT_FANOUT_TEST_TOKEN", result["environment_audit"]["stripped_credential_names"])
            preserved = result["environment_audit"]["preserved"]
            for ambient in ("HOME", "CODEX_HOME", "CODEX_AUTH_SOCK", "GROK_HOME", "GROK_AUTH_SOCK", "XDG_RUNTIME_DIR"):
                self.assertNotIn(ambient, preserved)
        with _FakeEndpoint(mode="delay") as endpoint:
            with patch.dict(os.environ, {"AGENT_FANOUT_PROJECT_ROOT": str(self.root)}, clear=False):
                timed_out = launch_local_endpoint(
                    project_root=self.root, endpoint=endpoint.endpoint,
                    allow_loopback_http=True, selector="local-worker",
                    resolved_model="fixture-local-model", reasoning_effort="low",
                    prompt="Summarize bounded alpha.", timeout_seconds=1,
                )
            self.assertEqual(timed_out["status"], "timeout")
            self.assertTrue(timed_out["process_group_exit_confirmed"])


if __name__ == "__main__":
    unittest.main()
