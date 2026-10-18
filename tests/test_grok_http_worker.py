import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentfanout.grok_http_worker import (
    GROK_CHAT_COMPLETIONS_ENDPOINT,
    GrokHttpError,
    _RejectRedirects,
    execute_auth_preflight,
    execute_grok_request,
)


class FakeResponse:
    status = 200

    def __init__(self, lines, *, content_type="text/event-stream", url=GROK_CHAT_COMPLETIONS_ENDPOINT):
        self.lines = lines
        self.index = 0
        self.headers = {"Content-Type": content_type}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self, size=-1):
        if self.index >= len(self.lines):
            return b""
        line = self.lines[self.index]
        if size >= 0 and len(line) > size:
            head, self.lines[self.index] = line[:size], line[size:]
            return head
        self.index += 1
        return line

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


def sse_event(value):
    return ("data: " + json.dumps(value, separators=(",", ":")) + "\n").encode()


def worker_result_content():
    return json.dumps({
        "schema": "WorkerResultV1",
        "result_id": "result_fixture",
        "task_id": "task_fixture",
        "status": "success",
        "summary": "Bounded fixture passed.",
        "evidence": ["fixture:1"],
        "changed_files": [],
        "commands": [],
        "exit_codes": [],
        "remaining_work": [],
        "launch_correlation_id": "launch_fixture",
    }, separators=(",", ":"))


def validator_result_content():
    return json.dumps({
        "schema": "ValidatorResultV1",
        "validator_id": "validator_fixture",
        "verdict": "pass",
        "evidence_refs": ["fixture:1"],
        "material_findings": [],
        "contradictions": [],
        "checked_at": "2026-07-10T12:00:00Z",
    }, separators=(",", ":"))


def valid_sse(
    *, model="grok-4.5", delta=None, sources=0, usage=True,
    reasoning_tokens=0,
):
    response_id = "chatcmpl-fixture"
    lines = [sse_event({
        "id": response_id, "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", **(delta or {"content": worker_result_content()})},
                     "finish_reason": None}],
    })]
    final = {
        "id": response_id, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    if usage:
        final["usage"] = {
            "prompt_tokens": 12, "completion_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": 16 + reasoning_tokens,
            "prompt_tokens_details": {"cached_tokens": 2},
            "num_sources_used": sources, "cost_in_usd_ticks": 1234,
        }
    lines.extend([sse_event(final), b"data: [DONE]\n"])
    return lines


class GrokHttpWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.auth = self.root / "auth.json"
        self.auth.write_text(json.dumps({
            "schema": "GrokApiCredentialV1",
            "api_key": "fixture-api-key-" + "x" * 64,
        }), encoding="utf-8")
        self.auth.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, **overrides):
        value = {
            "schema": "GrokDirectRequestV1",
            "auth_file": str(self.auth),
            "auth_file_sha256": hashlib.sha256(self.auth.read_bytes()).hexdigest(),
            "model": "grok-4.5",
            "reasoning_effort": "low",
            "reasoning_effort_mode": "api_parameter",
            "prompt": "Return bounded JSON.",
            "output_contract": "WorkerResultV1",
            "max_output_tokens": 100,
            "timeout_seconds": 5,
        }
        value.update(overrides)
        return value

    def execute(self, lines, **request_overrides):
        opener = FakeOpener(FakeResponse(lines))
        handlers = []

        def builder(*values):
            handlers.extend(values)
            return opener

        with patch("agentfanout.grok_http_worker.build_opener", side_effect=builder):
            result = execute_grok_request(self.request(**request_overrides))
        return result, opener, handlers

    def test_success_has_no_tool_schema_and_exact_usage_binding(self):
        result, opener, handlers = self.execute(valid_sse())

        self.assertEqual(result["schema"], "GrokDirectResponseV1")
        self.assertEqual(result["model"], "grok-4.5")
        self.assertFalse(result["tools_field_sent"])
        self.assertEqual(result["response_format_type"], "json_schema")
        self.assertEqual(result["response_schema_name"], "WorkerResultV1")
        self.assertFalse(result["redirects_allowed"])
        self.assertFalse(result["ambient_proxy_allowed"])
        self.assertEqual(result["usage"]["input_tokens"], 12)
        self.assertEqual(result["usage"]["cached_tokens"], 2)
        self.assertEqual(result["usage"]["output_tokens"], 4)
        self.assertEqual(result["usage"]["reasoning_tokens"], 0)
        self.assertTrue(result["usage"]["usage_verified"])
        self.assertFalse(result["usage"]["cost_verified"])

        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, GROK_CHAT_COMPLETIONS_ENDPOINT)
        self.assertEqual(timeout, 5.0)
        payload = json.loads(request.data)
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("functions", payload)
        response_format = payload["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "WorkerResultV1")
        self.assertTrue(response_format["json_schema"]["strict"])
        worker_variants = response_format["json_schema"]["schema"]["anyOf"]
        self.assertEqual(len(worker_variants), 4)
        self.assertEqual(
            worker_variants[0]["properties"]["schema"]["const"],
            "WorkerResultV1",
        )
        self.assertEqual(worker_variants[0]["properties"]["status"]["const"], "success")
        self.assertEqual(worker_variants[0]["properties"]["evidence"]["minItems"], 1)
        self.assertEqual(worker_variants[1]["properties"]["remaining_work"]["minItems"], 1)
        self.assertTrue(all(not item["additionalProperties"] for item in worker_variants))
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["model"], "grok-4.5")
        self.assertTrue(any(type(item).__name__ == "ProxyHandler" and item.proxies == {} for item in handlers))
        self.assertTrue(any(isinstance(item, _RejectRedirects) for item in handlers))

    def test_reasoning_model_sends_exact_effort(self):
        result, opener, _handlers = self.execute(
            valid_sse(model="grok-4.5"),
            model="grok-4.5", reasoning_effort="high",
            reasoning_effort_mode="api_parameter",
        )
        payload = json.loads(opener.requests[0][0].data)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(result["reasoning_effort"], "high")

    def test_validator_contract_is_request_bound_and_wrong_packet_fails_closed(self):
        result, opener, _handlers = self.execute(
            valid_sse(delta={"content": validator_result_content()}),
            output_contract="ValidatorResultV1",
        )
        payload = json.loads(opener.requests[0][0].data)
        self.assertEqual(result["output_contract"], "ValidatorResultV1")
        self.assertEqual(result["response_schema_name"], "ValidatorResultV1")
        validator_variants = payload["response_format"]["json_schema"]["schema"]["anyOf"]
        self.assertEqual(
            validator_variants[0]["properties"]["schema"]["const"],
            "ValidatorResultV1",
        )
        self.assertEqual(validator_variants[0]["properties"]["verdict"]["const"], "pass")
        self.assertEqual(validator_variants[0]["properties"]["contradictions"]["maxItems"], 0)
        with self.assertRaisesRegex(GrokHttpError, "validator_output_fields_invalid"):
            self.execute(
                valid_sse(delta={"content": worker_result_content()}),
                output_contract="ValidatorResultV1",
            )

    def test_unknown_output_contract_is_rejected_before_network(self):
        with patch("agentfanout.grok_http_worker.build_opener") as opener:
            with self.assertRaisesRegex(GrokHttpError, "output_contract_invalid"):
                execute_grok_request(self.request(output_contract="ArbitrarySchema"))
        opener.assert_not_called()

    def test_effort_mode_is_bound_to_exact_model_not_route_alias(self):
        with self.assertRaisesRegex(GrokHttpError, "reasoning_effort_mode_mismatch"):
            self.execute(
                valid_sse(model="grok-4.5"), model="grok-4.5",
                reasoning_effort="low", reasoning_effort_mode="fixed_low",
            )
        with self.assertRaisesRegex(GrokHttpError, "reasoning_effort_mode_mismatch"):
            self.execute(
                valid_sse(), reasoning_effort="low",
                reasoning_effort_mode="fixed_low",
            )
        with self.assertRaisesRegex(GrokHttpError, "reasoning_effort_invalid"):
            self.execute(
                valid_sse(model="grok-4.5"), model="grok-4.5",
                reasoning_effort="xhigh", reasoning_effort_mode="api_parameter",
            )

    def test_unapproved_reasoning_model_is_rejected_before_network_setup(self):
        with patch("agentfanout.grok_http_worker.build_opener") as opener:
            with self.assertRaisesRegex(
                GrokHttpError, "grok_http_model_packet_contract_unsupported",
            ):
                execute_grok_request(self.request(
                    model="grok-4.20-multi-agent",
                    reasoning_effort="high",
                    reasoning_effort_mode="api_parameter",
                ))
        opener.assert_not_called()

    def test_tool_call_model_mismatch_usage_and_sources_fail_closed(self):
        cases = (
            (valid_sse(delta={"tool_calls": [{"id": "bad"}]}), "grok_http_tool_call_forbidden"),
            (valid_sse(model="wrong-model"), "grok_http_response_model_mismatch"),
            (valid_sse(usage=False), "grok_http_usage_ambiguous"),
            (valid_sse(sources=1), "grok_http_external_sources_forbidden"),
        )
        for lines, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(GrokHttpError, expected):
                    self.execute(lines)

        missing_source_proof = valid_sse()
        final = json.loads(missing_source_proof[1][6:])
        final["usage"].pop("num_sources_used")
        missing_source_proof[1] = sse_event(final)
        with self.assertRaisesRegex(GrokHttpError, "grok_http_usage_invalid"):
            self.execute(missing_source_proof)

    def test_reasoning_usage_and_monotonic_cumulative_streams_are_supported(self):
        reasoning = valid_sse(
            model="grok-4.5", reasoning_tokens=94,
        )
        result, _opener, _handlers = self.execute(
            reasoning, model="grok-4.5", reasoning_effort="high",
            reasoning_effort_mode="api_parameter", max_output_tokens=200,
        )
        self.assertEqual(result["usage"]["visible_output_tokens"], 4)
        self.assertEqual(result["usage"]["reasoning_tokens"], 94)
        self.assertEqual(result["usage"]["output_tokens"], 98)
        self.assertEqual(result["usage"]["total_tokens"], 110)

        cumulative = valid_sse()
        first = json.loads(cumulative[0][6:])
        first["usage"] = {
            "prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 2},
        }
        cumulative[0] = sse_event(first)
        cumulative_result, _opener, _handlers = self.execute(cumulative)
        self.assertEqual(cumulative_result["usage"]["total_tokens"], 16)

        decreasing = list(cumulative)
        final = json.loads(decreasing[1][6:])
        final["usage"]["completion_tokens"] = 0
        final["usage"]["total_tokens"] = 12
        decreasing[1] = sse_event(final)
        with self.assertRaisesRegex(GrokHttpError, "usage_nonmonotonic"):
            self.execute(decreasing)

        server_tool = valid_sse()
        final = json.loads(server_tool[1][6:])
        final["usage"]["num_server_side_tools_used"] = 1
        server_tool[1] = sse_event(final)
        with self.assertRaisesRegex(GrokHttpError, "external_sources_forbidden"):
            self.execute(server_tool)

    def test_stream_rejects_data_after_done_duplicate_choices_and_source_fields(self):
        after_done = valid_sse() + [sse_event({
            "id": "chatcmpl-fixture", "model": "grok-4.5",
            "choices": [],
        })]
        duplicate_choices = valid_sse()
        duplicate_choices[0] = sse_event({
            "id": "chatcmpl-fixture", "model": "grok-4.5",
            "choices": [
                {"index": 0, "delta": {"content": "a"}, "finish_reason": None},
                {"index": 0, "delta": {"content": "b"}, "finish_reason": None},
            ],
        })
        external_sources = valid_sse()
        first = json.loads(external_sources[0][6:])
        first["citations"] = ["https://example.invalid"]
        external_sources[0] = sse_event(first)
        for lines, expected in (
            (after_done, "grok_http_data_after_done"),
            (duplicate_choices, "grok_http_choices_invalid"),
            (external_sources, "grok_http_external_sources_forbidden"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(GrokHttpError, expected):
                    self.execute(lines)

    def test_stream_rejects_content_after_terminal_finish_before_done(self):
        lines = valid_sse()
        lines.insert(2, sse_event({
            "id": "chatcmpl-fixture", "model": "grok-4.5",
            "choices": [{
                "index": 0, "delta": {"content": "UNBILLED-AFTER-STOP"},
                "finish_reason": None,
            }],
        }))
        with self.assertRaisesRegex(GrokHttpError, "choices_after_finish"):
            self.execute(lines)

    def test_each_content_event_requires_exact_response_and_model_binding(self):
        for missing, expected in (
            ("id", "grok_http_response_id_invalid"),
            ("model", "grok_http_response_model_mismatch"),
        ):
            lines = valid_sse()
            first = json.loads(lines[0][6:])
            first.pop(missing)
            lines[0] = sse_event(first)
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(GrokHttpError, expected):
                    self.execute(lines)

    def test_stream_line_is_bounded_before_full_allocation(self):
        oversized_no_newline = b"data: " + b"x" * 300_000
        with self.assertRaisesRegex(GrokHttpError, "grok_http_response_too_large"):
            self.execute([oversized_no_newline])

    def test_api_auth_is_prebound_exact_and_private(self):
        self.auth.chmod(0o644)
        with self.assertRaisesRegex(GrokHttpError, "auth_security_invalid"):
            self.execute(valid_sse())
        self.auth.chmod(0o600)
        self.auth.write_text(json.dumps({
            "one": {"key": "x" * 64}, "two": {"key": "y" * 64},
        }), encoding="utf-8")
        with self.assertRaisesRegex(GrokHttpError, "api_credential_required"):
            self.execute(valid_sse(), auth_file_sha256=hashlib.sha256(self.auth.read_bytes()).hexdigest())

    def test_legacy_sessions_and_malformed_api_credentials_fail_before_network(self):
        token = "fixture-sensitive-token-" + ("z" * 64)
        cases = (
            ({"https://auth.x.ai::fixture": {"key": token, "auth_mode": "oidc",
              "expires_at": "2099-01-01T00:00:00Z"}}, "api_credential_required"),
            ({"schema": "GrokApiCredentialV1", "api_key": token,
              "session": {"key": token}}, "api_credential_required"),
            ({"schema": "GrokApiCredentialV1", "api_key": ""}, "auth_token_invalid"),
            ({"schema": "GrokApiCredentialV1", "api_key": 123}, "auth_token_invalid"),
            ({"schema": "GrokApiCredentialV1", "api_key": token + "\n"}, "auth_token_invalid"),
            ({"schema": "GrokApiCredentialV1", "api_key": token + "\ud800"}, "auth_token_invalid"),
            ({"schema": "GrokApiCredentialV1", "api_key": token + "\u00e9"}, "auth_token_invalid"),
            ({"schema": "GrokApiCredentialV1", "api_key": "x" * 16385}, "auth_token_invalid"),
        )
        for document, expected in cases:
            with self.subTest(expected=expected):
                self.auth.write_text(json.dumps(document), encoding="utf-8")
                self.auth.chmod(0o600)
                digest = hashlib.sha256(self.auth.read_bytes()).hexdigest()
                with patch("agentfanout.grok_http_worker.build_opener") as opener_builder:
                    with self.assertRaisesRegex(GrokHttpError, expected) as rejected:
                        execute_grok_request(self.request(auth_file_sha256=digest))
                    opener_builder.assert_not_called()
                self.assertNotIn(token, str(rejected.exception))

    def test_duplicate_auth_keys_are_rejected_recursively(self):
        token_a = "fixture-duplicate-a-" + ("a" * 64)
        token_b = "fixture-duplicate-b-" + ("b" * 64)
        documents = (
            ('{"schema":"GrokApiCredentialV1","api_key":"' + token_a
             + '","api_key":"' + token_b + '"}'),
            (
                '{"https://auth.x.ai::fixture":{"key":"' + token_a
                + '","auth_mode":"oidc","expires_at":"2099-01-01T00:00:00Z"},'
                + '"https://auth.x.ai::fixture":{"key":"' + token_b
                + '","auth_mode":"oidc","expires_at":"2099-01-01T00:00:00Z"}}'
            ),
            (
                '{"https://auth.x.ai::fixture":{"key":"' + token_a
                + '","key":"' + token_b
                + '","auth_mode":"oidc","expires_at":"2099-01-01T00:00:00Z"}}'
            ),
        )
        for document in documents:
            with self.subTest(document_sha256=hashlib.sha256(document.encode()).hexdigest()):
                self.auth.write_text(document, encoding="utf-8")
                self.auth.chmod(0o600)
                request = {
                    "schema": "GrokDirectAuthPreflightV1",
                    "auth_file": str(self.auth),
                    "auth_file_sha256": hashlib.sha256(self.auth.read_bytes()).hexdigest(),
                }
                with self.assertRaisesRegex(GrokHttpError, "auth_duplicate_key"):
                    execute_auth_preflight(request)

    def test_redirect_handler_never_forwards_authorization(self):
        handler = _RejectRedirects()
        with self.assertRaisesRegex(GrokHttpError, "redirect_denied"):
            handler.redirect_request(None, None, 307, "redirect", {}, "https://example.invalid")

    def test_hard_linked_api_credential_is_rejected_before_network(self):
        os.link(self.auth, self.root / "other-credential")
        with patch("agentfanout.grok_http_worker.build_opener") as opener:
            with self.assertRaisesRegex(GrokHttpError, "auth_security_invalid"):
                execute_grok_request(self.request())
            opener.assert_not_called()

    def test_auth_preflight_returns_only_nonsecret_transport_facts(self):
        response = execute_auth_preflight({
            "schema": "GrokDirectAuthPreflightV1",
            "auth_file": str(self.auth),
            "auth_file_sha256": hashlib.sha256(self.auth.read_bytes()).hexdigest(),
        })
        rendered = json.dumps(response, sort_keys=True)
        self.assertEqual(response["schema"], "GrokDirectAuthPreflightResponseV1")
        self.assertTrue(response["auth_entry_unambiguous"])
        self.assertEqual(response["credential_type"], "xai_api_key")
        self.assertFalse(response["network_verified"])
        self.assertNotIn("auth_unexpired", response)
        self.assertFalse(response["tools_field_sent"])
        self.assertNotIn("fixture-api-key-" + "x" * 64, rendered)
        self.assertNotIn(str(self.auth), rendered)


if __name__ == "__main__":
    unittest.main()
