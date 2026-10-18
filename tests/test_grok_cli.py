import json
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from agentfanout.grok_cli import (ACPClient, AdmissionEvidence, HostAuthorization,
    GrokCLIUnavailable, REQUIRED_PROOFS, UsageReconciler, capability_status,
    isolated_environment, normalize_usage)


SEMANTICS = {"scope": "whole_turn", "cache": "included", "reasoning": "included", "cumulative": False}


def evidence():
    return AdmissionEvidence("a" * 64, "b" * 64, "grok-fixture", "low", "cached_token",
        REQUIRED_PROOFS, False, "included_only_enforced", "host-fixture-only")


class FakeTransport:
    def __init__(self, epoch="epoch-1", session="session-1", output='{"fixture":true}', fail=None):
        self.epoch, self.session, self.output, self.fail = epoch, session, output, fail
        self.sent, self.queue = [], []
        self.closed = False
        self.exit_confirmed = False

    def send(self, frame):
        self.sent.append(frame)
        if "method" not in frame:
            return
        method = frame["method"]
        result = {"initialize": {"protocolVersion": 1},
                  "authenticate": {"acceptedMethod": "cached_token"},
                  "session/new": {"sessionId": self.session, "effectiveModel": "grok-fixture", "effectiveEffort": "low"},
                  "session/prompt": {"stopReason": "end_turn", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "reasoning_tokens": 1}}}.get(method, {})
        if method == "session/prompt":
            if self.fail == "crash":
                return
            self.queue.append({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": self.session, "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": self.output}}}})
        self.queue.append({"jsonrpc": "2.0", "id": frame["id"], "result": result})

    def receive(self, deadline):
        if not self.queue:
            raise GrokCLIUnavailable("transport_eof")
        frame = self.queue.pop(0)
        if self.fail == "wrong_session" and "method" in frame:
            frame["params"]["sessionId"] = "other-session"
        if self.fail == "wrong_request" and frame.get("id") == 4:
            frame["id"] = 500
        if self.fail == "auth" and frame.get("id") == 2:
            frame["result"]["acceptedMethod"] = "api_key"
        if self.fail == "model" and frame.get("id") == 3:
            frame["result"]["effectiveModel"] = "wrong-model"
        if self.fail == "tool" and "method" in frame:
            frame["params"]["update"]["sessionUpdate"] = "tool_call"
        if self.fail == "callback" and "method" in frame:
            frame = {"jsonrpc": "2.0", "id": 999, "method": "fs/read_text_file", "params": {}}
        return frame

    def close(self):
        self.closed = True
        self.exit_confirmed = True


class ACPTests(unittest.TestCase):
    def client(self, fail=None):
        transport = FakeTransport(fail=fail)
        journal = []
        client = ACPClient(transport, evidence(), lambda ev: True, lambda kind, data: (journal.append(kind), True)[1])
        return client, transport, journal

    def test_status_unavailable_even_with_proofs(self):
        self.assertFalse(capability_status()["available"])

    def test_every_boundary_proof_required(self):
        for proof in REQUIRED_PROOFS:
            with self.subTest(proof=proof), self.assertRaisesRegex(GrokCLIUnavailable, "boundary_unproved"):
                replace(evidence(), proofs=REQUIRED_PROOFS - {proof}).verify(lambda ev: True)

    def test_host_verifies_not_adapter(self):
        with self.assertRaisesRegex(GrokCLIUnavailable, "host_evidence_unverified"):
            evidence().verify(lambda ev: False)

    def test_spending_separate_from_auth(self):
        for value in (replace(evidence(), billing_regime="unknown"), replace(evidence(), allow_paid_overage=True)):
            with self.assertRaises(GrokCLIUnavailable):
                value.verify(lambda ev: True)

    def test_no_ambient_api_credentials_or_config(self):
        value = isolated_environment({"PATH": "/usr/bin", "HOME": "/operator", "XAI_API_KEY": "synthetic", "HTTP_PROXY": "synthetic", "GROK_SANDBOX": "bypass"})
        self.assertEqual(value, {"PATH": "/usr/bin"})

    def test_durable_authorization_before_prompt(self):
        client, transport, journal = self.client()
        prepared = client.prepare("task-1", "bounded", "worker", "/empty")
        auth = HostAuthorization("receipt-1", prepared, 1.25)
        with self.assertRaisesRegex(GrokCLIUnavailable, "authorization_not_durable"):
            client.execute("bounded", auth, lambda ev: False)
        self.assertFalse(any(x.get("method") == "session/prompt" for x in transport.sent))
        result = client.execute("bounded", auth, lambda ev: True)
        self.assertEqual(result["reservation_usd"], 1.25)
        self.assertIsNone(result["provider_cost_usd"])
        self.assertEqual(journal, ["inference_start_intent", "inference_response_observed"])
        with self.assertRaisesRegex(GrokCLIUnavailable, "fresh_session_required"):
            client.execute("bounded", auth, lambda ev: True)

    def test_start_journal_failure_prevents_prompt(self):
        client, transport, _ = self.client()
        prepared = client.prepare("task-1", "bounded", "worker", "/empty")
        def fail(*args):
            raise OSError("durable store failed")
        client.journal = fail
        with self.assertRaises(OSError):
            client.execute("bounded", HostAuthorization("receipt-1", prepared, 1), lambda ev: True)
        self.assertFalse(any(x.get("method") == "session/prompt" for x in transport.sent))

    def test_mismatched_input_or_authorization(self):
        client, _, _ = self.client()
        prepared = client.prepare("task-1", "bounded", "worker", "/empty")
        for text, auth in (("changed", HostAuthorization("r", prepared, 1)), ("bounded", HostAuthorization("r", replace(prepared, task_id="other"), 1))):
            with self.assertRaises(GrokCLIUnavailable):
                client.execute(text, auth, lambda ev: True)

    def test_failed_auth_model_and_protocol_before_prompt(self):
        for failure in ("auth", "model"):
            client, transport, _ = self.client(failure)
            with self.assertRaises(GrokCLIUnavailable):
                client.prepare("task-1", "bounded", "worker", "/empty")
            self.assertFalse(any(x.get("method") == "session/prompt" for x in transport.sent))

    def test_callback_hidden_tool_session_request_and_crash_quarantine(self):
        for failure in ("callback", "tool", "wrong_session", "wrong_request", "crash"):
            with self.subTest(failure=failure):
                client, transport, journal = self.client(failure)
                prepared = client.prepare("task-1", "bounded", "worker", "/empty")
                with self.assertRaises(GrokCLIUnavailable):
                    client.execute("bounded", HostAuthorization("r", prepared, 1), lambda ev: True)
                self.assertTrue(client.uncertain)
                self.assertIn("inference_effect_uncertain", journal)
                if failure == "callback":
                    self.assertIn("error", transport.sent[-1])
                self.assertFalse(client.cancel()["replay_allowed"])

    def test_fresh_concurrent_sessions_and_validator(self):
        clients = [self.client()[0] for _ in range(3)]
        sessions = [client.prepare(str(i), "bounded", "validator" if i == 1 else "worker", "/empty") for i, client in enumerate(clients)]
        self.assertIsNot(clients[0].transport, clients[1].transport)
        self.assertEqual(sessions[1].role, "validator")
        # Host callback must attest unique process/session epochs; transports with
        # repeated epochs are deliberately not accepted as production evidence.

    def test_frontend_default_native_and_strict_refusal(self):
        root = Path(__file__).resolve().parents[1]
        native = subprocess.run([sys.executable, str(root / "scripts/grok-agentfanout.py"), "task", "bounded"], capture_output=True, text=True)
        self.assertEqual(native.returncode, 0)
        self.assertEqual(json.loads(native.stdout)["status"], "awaiting_native_host_handshake")
        strict = subprocess.run([sys.executable, str(root / "scripts/grok-agentfanout.py"), "--profile", "grok_cli_subscription_acp", "task", "bounded"], capture_output=True, text=True)
        self.assertEqual(strict.returncode, 2)
        self.assertFalse(json.loads(strict.stdout)["available"])


class UsageTests(unittest.TestCase):
    def observe(self, **kwargs):
        raw = {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 5, "reasoning_tokens": 2}
        raw.update(kwargs.pop("raw", {}))
        return normalize_usage(raw, process_epoch=kwargs.pop("epoch", "e1"), session_id="s1", request_id=kwargs.pop("request", "r1"), semantics=kwargs.pop("semantics", SEMANTICS))

    def test_cache_and_reasoning_never_double_count(self):
        included = self.observe()
        excluded = self.observe(semantics={**SEMANTICS, "cache": "excluded", "reasoning": "excluded"})
        self.assertEqual((included.input_tokens, included.output_tokens), (10, 5))
        self.assertEqual((excluded.input_tokens, excluded.output_tokens), (13, 7))

    def test_last_call_partial_missing_or_inconsistent_refused(self):
        for semantics, raw in (({**SEMANTICS, "scope": "last_call"}, {}), (SEMANTICS, {"input_tokens": None}), (SEMANTICS, {"cached_input_tokens": 11}), (SEMANTICS, {"reasoning_tokens": 6}), (SEMANTICS, {"provider_cost_usd": float("nan")})):
            with self.assertRaises(GrokCLIUnavailable):
                self.observe(semantics=semantics, raw=raw)

    def test_duplicate_recovery_and_conflict(self):
        reconciler = UsageReconciler()
        self.assertFalse(reconciler.reconcile(self.observe())["duplicate"])
        restored = UsageReconciler(json.loads(json.dumps(reconciler.state)))
        self.assertTrue(restored.reconcile(self.observe())["duplicate"])
        with self.assertRaisesRegex(GrokCLIUnavailable, "duplicate_usage_conflict"):
            restored.reconcile(self.observe(raw={"input_tokens": 11}))

    def test_cumulative_delta_reset_and_new_process(self):
        semantics = {**SEMANTICS, "cumulative": True}
        reconciler = UsageReconciler()
        first = reconciler.reconcile(self.observe(semantics=semantics))
        self.assertEqual(first["tokens"]["input_tokens"], 10)
        second = reconciler.reconcile(self.observe(semantics=semantics, request="r2", raw={"input_tokens": 15, "output_tokens": 7}))
        self.assertEqual(second["tokens"]["input_tokens"], 5)
        with self.assertRaisesRegex(GrokCLIUnavailable, "usage_reset_same_epoch"):
            reconciler.reconcile(self.observe(semantics=semantics, request="r3"))
        fresh = reconciler.reconcile(self.observe(semantics=semantics, epoch="e2"))
        self.assertEqual(fresh["tokens"]["input_tokens"], 10)
        self.assertIsNone(fresh["provider_cost_usd"])

    def test_unsupported_reader_schema(self):
        with self.assertRaisesRegex(GrokCLIUnavailable, "usage_schema_unsupported"):
            UsageReconciler({"schema": "old"})

class AdditionalACPTests(unittest.TestCase):
    def test_concurrent_exact_session_outputs_do_not_cross(self):
        from concurrent.futures import ThreadPoolExecutor
        def run(index):
            transport = FakeTransport(epoch=f"epoch-{index}", session=f"session-{index}", output=str(index))
            client = ACPClient(transport, evidence(), lambda ev: True, lambda kind, data: True)
            prepared = client.prepare(f"task-{index}", str(index), "worker", "/empty")
            output = client.execute(str(index), HostAuthorization(f"receipt-{index}", prepared, 0.03), lambda auth: True)
            return prepared, output["text"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        self.assertEqual([output for _, output in results], ["0", "1", "2", "3"])
        self.assertEqual(len({prepared.session_id for prepared, _ in results}), 4)
        self.assertEqual(len({prepared.process_epoch for prepared, _ in results}), 4)

    def test_failed_bootstrap_cannot_create_second_session(self):
        client = ACPClient(FakeTransport(fail="model"), evidence(), lambda ev: True, lambda *args: True)
        with self.assertRaises(GrokCLIUnavailable):
            client.prepare("task-1", "bounded", "worker", "/empty")
        with self.assertRaisesRegex(GrokCLIUnavailable, "fresh_session_required"):
            client.prepare("task-1", "bounded", "worker", "/empty")

    def test_auth_expired_missing_and_api_fallback_never_prompt(self):
        import os
        from unittest.mock import patch
        for kind in ("expired", "missing"):
            transport = FakeTransport()
            original = transport.receive
            def receive(deadline):
                frame = original(deadline)
                if frame.get("id") == 2:
                    frame = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "cached auth unavailable"}}
                return frame
            transport.receive = receive
            with self.subTest(kind=kind), patch.dict(os.environ, {"XAI_API_KEY": "fixture-api-present"}):
                self.assertNotIn("XAI_API_KEY", isolated_environment(os.environ))
                client = ACPClient(transport, evidence(), lambda ev: True, lambda *args: True)
                with self.assertRaisesRegex(GrokCLIUnavailable, "request_rejected"):
                    client.prepare("task-1", "bounded", "worker", "/empty")
                self.assertFalse(any(frame.get("method") == "session/prompt" for frame in transport.sent))

    def test_malformed_excessive_and_nontext_notifications_quarantine(self):
        from agentfanout.grok_cli import MAX_OUTPUT
        for variant in ("params", "content", "excess", "nontext"):
            transport = FakeTransport(output="x" * (MAX_OUTPUT + 1) if variant == "excess" else "bounded")
            original = transport.receive
            def receive(deadline):
                frame = original(deadline)
                if "method" in frame:
                    if variant == "params": frame["params"] = []
                    if variant == "content": frame["params"]["update"]["content"] = []
                    if variant == "nontext": frame["params"]["update"]["content"]["type"] = "image"
                return frame
            transport.receive = receive
            client = ACPClient(transport, evidence(), lambda ev: True, lambda *args: True)
            prepared = client.prepare("task-1", "bounded", "worker", "/empty")
            with self.subTest(variant=variant), self.assertRaises(GrokCLIUnavailable):
                client.execute("bounded", HostAuthorization("receipt", prepared, 0.03), lambda auth: True)
            self.assertTrue(client.uncertain)

    def test_multi_call_whole_turn_and_mode_change(self):
        # Verified whole-turn extension includes all calls; last-call mode rejected.
        raw = {"input_tokens": 30, "cached_input_tokens": 4, "output_tokens": 9, "reasoning_tokens": 3}
        usage = normalize_usage(raw, process_epoch="epoch", session_id="session", request_id="r1", semantics=SEMANTICS)
        reconciler = UsageReconciler()
        self.assertEqual(reconciler.reconcile(usage)["tokens"]["input_tokens"], 30)
        cumulative = replace(usage, request_id="r2", cumulative=True)
        with self.assertRaisesRegex(GrokCLIUnavailable, "usage_mode_changed_same_epoch"):
            reconciler.reconcile(cumulative)



class AccountingAndDurabilityTests(unittest.TestCase):
    def test_truthy_host_evidence_and_receipt_are_not_authority(self):
        with self.assertRaisesRegex(GrokCLIUnavailable, "host_evidence_unverified"):
            evidence().verify(lambda ev: {"approved": True})
        client = ACPClient(FakeTransport(), evidence(), lambda ev: True, lambda *args: True)
        prepared = client.prepare("task", "bounded", "worker", "/empty")
        with self.assertRaisesRegex(GrokCLIUnavailable, "authorization_not_durable"):
            client.execute("bounded", HostAuthorization("receipt", prepared, 0.03), lambda auth: 1)

    def test_journal_none_cannot_authorize_and_cancel_always_closes(self):
        transport = FakeTransport()
        client = ACPClient(transport, evidence(), lambda ev: True, lambda *args: None)
        prepared = client.prepare("task", "bounded", "worker", "/empty")
        with self.assertRaisesRegex(GrokCLIUnavailable, "journal_not_durable"):
            client.execute("bounded", HostAuthorization("receipt", prepared, 0.03), lambda auth: True)
        self.assertFalse(any(frame.get("method") == "session/prompt" for frame in transport.sent))
        with self.assertRaisesRegex(GrokCLIUnavailable, "journal_not_durable"):
            client.cancel()
        self.assertTrue(transport.closed)

    def observation(self, request, raw):
        return normalize_usage(raw, process_epoch="epoch", session_id="session", request_id=request,
            semantics={**SEMANTICS, "cumulative": True})

    def test_invalid_cache_reasoning_delta_never_committed(self):
        for first, second in ((
            {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 10, "reasoning_tokens": 2},
            {"input_tokens": 11, "cached_input_tokens": 4, "output_tokens": 11, "reasoning_tokens": 2}), (
            {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 10, "reasoning_tokens": 2},
            {"input_tokens": 11, "cached_input_tokens": 2, "output_tokens": 11, "reasoning_tokens": 4})):
            reconciler = UsageReconciler()
            reconciler.reconcile(self.observation("r1", first))
            snapshot = json.dumps(reconciler.state, sort_keys=True)
            with self.assertRaisesRegex(GrokCLIUnavailable, "usage_delta_inconsistent"):
                reconciler.reconcile(self.observation("r2", second))
            self.assertEqual(json.dumps(reconciler.state, sort_keys=True), snapshot)

    def test_restored_corrupt_baseline_and_mode_refused(self):
        reconciler = UsageReconciler()
        reconciler.reconcile(self.observation("r1", {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "reasoning_tokens": 1}))
        for corruption in ("cache", "type", "mode", "fields"):
            state = json.loads(json.dumps(reconciler.state))
            key = next(iter(state["baselines"]))
            if corruption == "cache": state["baselines"][key]["cached_input_tokens"] = 11
            if corruption == "type": state["baselines"][key]["input_tokens"] = True
            if corruption == "mode": state["modes"][key] = "true"
            if corruption == "fields": state["baselines"][key].pop("reasoning_tokens")
            with self.subTest(corruption=corruption), self.assertRaises(GrokCLIUnavailable):
                UsageReconciler(state)

    def test_provider_numeric_price_is_observation_not_verified_cost(self):
        raw = {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "reasoning_tokens": 1,
               "provider_cost_usd": .0123456789, "provider_cost_verified": True}
        unverified = normalize_usage(raw, process_epoch="e", session_id="s", request_id="r", semantics=SEMANTICS)
        self.assertEqual(unverified.provider_cost_usd, .0123456789)
        self.assertFalse(unverified.provider_cost_verified)
        verified = normalize_usage(raw, process_epoch="e", session_id="s", request_id="r", semantics={**SEMANTICS, "price_evidence_verified": True})
        self.assertTrue(verified.provider_cost_verified)

    def test_cumulative_verified_cost_needs_verified_prior_baseline(self):
        base = {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "reasoning_tokens": 1, "provider_cost_usd": .1}
        semantics = {**SEMANTICS, "cumulative": True, "price_evidence_verified": True}
        reconciler = UsageReconciler()
        first = normalize_usage(base, process_epoch="e", session_id="s", request_id="r1", semantics=semantics)
        reconciler.reconcile(first)
        second = normalize_usage({**base, "input_tokens": 20, "output_tokens": 10, "provider_cost_usd": .2, "provider_cost_verified": True}, process_epoch="e", session_id="s", request_id="r2", semantics=semantics)
        self.assertFalse(reconciler.reconcile(second)["provider_cost_verified"])

    def test_sensitive_supplied_text_is_refused_before_session_creation(self):
        for content in ("-----BEGIN " + "PRIVATE KEY-----\nfixture-only", json.dumps({"api_key": "fixture-sensitive-credential-value"})):
            transport = FakeTransport()
            client = ACPClient(transport, evidence(), lambda ev: True, lambda *args: True)
            with self.subTest(content=content), self.assertRaisesRegex(GrokCLIUnavailable, "input_content_sensitive"):
                client.prepare("task", content, "worker", "/empty")
            self.assertFalse(transport.sent)

    def test_declared_partial_tokens_and_wrong_flag_types_refuse_before_commit(self):
        raw = {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "reasoning_tokens": 1}
        for flag, value, code in (("usage_is_incomplete", True, "usage_incomplete"),
                ("usage_is_partial", True, "usage_incomplete"),
                ("usage_is_partial", 1, "usage_flag_invalid"),
                ("usage_is_incomplete", "false", "usage_flag_invalid"),
                ("cost_is_partial", None, "usage_flag_invalid")):
            reconciler = UsageReconciler()
            before = json.dumps(reconciler.state, sort_keys=True)
            with self.subTest(flag=flag, value=value), self.assertRaisesRegex(GrokCLIUnavailable, code):
                observation = normalize_usage({**raw, flag: value}, process_epoch="e", session_id="s", request_id="r",
                    semantics={**SEMANTICS, "cumulative": True})
                reconciler.reconcile(observation)
            self.assertEqual(json.dumps(reconciler.state, sort_keys=True), before)

    def test_partial_price_preserves_valid_tokens_and_unverified_numeric_observation(self):
        raw = {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "reasoning_tokens": 1,
            "provider_cost_usd": .012345, "provider_cost_verified": True, "cost_is_partial": True}
        observation = normalize_usage(raw, process_epoch="e", session_id="s", request_id="r",
            semantics={**SEMANTICS, "price_evidence_verified": True})
        self.assertFalse(observation.provider_cost_verified)
        self.assertEqual(observation.provider_cost_usd, .012345)
        self.assertEqual(UsageReconciler().reconcile(observation)["tokens"]["input_tokens"], 10)

    def test_prepared_identity_has_config_and_auth(self):
        client = ACPClient(FakeTransport(), evidence(), lambda ev: True, lambda *args: True)
        prepared = client.prepare("task", "bounded", "worker", "/empty")
        self.assertEqual(prepared.config_sha256, evidence().config_sha256)
        self.assertEqual(prepared.auth_kind, "cached_token")

class StdioTransportTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "empty"
        self.workspace.mkdir()
        self.transports = []

    def tearDown(self):
        for transport in self.transports:
            transport.close()
        self.tmp.cleanup()

    def transport(self, body, ev=None, env=None, expected=None):
        from agentfanout.grok_cli import StdioTransport
        from agentfanout.provider_adapters import executable_identity
        script = self.root / ("fake-" + str(len(self.transports)) + ".py")
        script.write_text("#!" + sys.executable + "\nimport sys, time, json\n" + body)
        script.chmod(0o700)
        identity = expected or executable_identity(str(script))
        ev = ev or replace(evidence(), binary_sha256=identity["executable_sha256"])
        transport = StdioTransport(str(script), str(self.workspace), "subprocess-fixture", ev,
            lambda ev: True, env or {"PATH": "/usr/bin"}, identity)
        self.transports.append(transport)
        return transport

    def test_real_framing_and_large_write(self):
        import time
        transport = self.transport('line = sys.stdin.readline()\npacket = json.loads(line)\nprint(json.dumps({"jsonrpc":"2.0","id":packet["id"],"result":{"size":len(line)}}), flush=True)\n')
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "fixture", "params": {"text": "x" * 120000}})
        response = transport.receive(time.monotonic() + 2)
        self.assertGreater(response["result"]["size"], 120000)
        transport.close()
        self.assertTrue(transport.exit_confirmed)

    def test_nonfinite_duplicate_nonobject_and_bad_json(self):
        import time
        for line in ('{"id":1,"id":2}', '{"result":NaN}', '[1]', 'malformed'):
            with self.subTest(line=line):
                transport = self.transport('print(' + repr(line) + ', flush=True)\ntime.sleep(.2)\n')
                with self.assertRaisesRegex(GrokCLIUnavailable, "malformed_frame"):
                    transport.receive(time.monotonic() + 2)
                transport.close()

    def test_excessive_frame_and_stderr(self):
        import time
        from agentfanout.grok_cli import MAX_FRAME, MAX_OUTPUT
        for body, code in ((f'print("x" * {MAX_FRAME + 1}, flush=True)\ntime.sleep(.2)\n', "frame_bounds"),
                           (f'sys.stderr.write("x" * {MAX_OUTPUT + 1}); sys.stderr.flush()\ntime.sleep(.2)\n', "stderr_bounds")):
            with self.subTest(code=code):
                transport = self.transport(body)
                with self.assertRaisesRegex(GrokCLIUnavailable, code):
                    transport.receive(time.monotonic() + 2)
                transport.close()

    def test_timeout_and_eof_cleanly_supervised(self):
        import time
        for body, code in (('time.sleep(2)\n', "transport_timeout"), ('pass\n', "transport_eof")):
            transport = self.transport(body)
            with self.assertRaisesRegex(GrokCLIUnavailable, code):
                transport.receive(time.monotonic() + .2)
            transport.close()
            self.assertTrue(transport.exit_confirmed)

    def test_credentials_and_effective_overrides_rejected_before_spawn(self):
        with self.assertRaisesRegex(GrokCLIUnavailable, "ambient_environment_rejected"):
            self.transport('pass\n', env={"PATH": "/usr/bin", "XAI_API_KEY": "fixture-only"})
        with self.assertRaisesRegex(GrokCLIUnavailable, "binary_drift"):
            self.transport('pass\n', ev=evidence())

    def test_changed_executable_full_identity_rejected(self):
        from agentfanout.provider_adapters import executable_identity, ProviderAdapterError
        script = self.root / "fake-0.py"
        script.write_text("#!" + sys.executable + "\npass\n")
        script.chmod(0o700)
        expected = executable_identity(str(script))
        with self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"):
            self.transport('time.sleep(2)\n', expected=expected)
