import base64
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agentfanout.telemetry import (
    JsonlSink, LangfuseHttpSink, contains_sensitive_material, sanitize_event,
)


class TelemetryTests(unittest.TestCase):
    def test_result_packet_credential_screening_decodes_and_distinguishes_safe_prose(self):
        bearer = "Author" + "ization: Be" + "arer " + ("b" * 32)
        api_key = "s" + "k-proj-" + ("a" * 24)
        encoded = base64.b64encode(api_key.encode()).decode()
        standalone_bearer = "Be" + "arer " + ("t" * 40)
        standalone_basic = "Ba" + "sic " + base64.b64encode(b"fixture-user:fixture-password").decode()
        cookie = "Cook" + "ie: session=" + ("c" * 40)
        set_cookie = "Set-" + "Cookie: sid=" + ("d" * 40) + "; HttpOnly"
        for packet in (
            {"summary": bearer},
            {"evidence": [{"api_key": api_key}]},
            {"evidence": [encoded]},
            {"evidence": [json.dumps({"nested": {"token": api_key}})]},
            {"evidence": [{"openai_api_key": "fixture-openai-" + ("o" * 24)}]},
            {"evidence": [{"oauth_client_secret": "fixture-oauth-" + ("s" * 24)}]},
            {"evidence": [{"database_url": "postgresql" + "://" + "fixture-user:fixture-pass@db.invalid/app"}]},
            {"evidence": [{"primaryConnectionString": "Server=db.invalid;Password=" + ("p" * 24)}]},
            {"evidence": [{"service": {"openaiApiKey": {"value": "fixture-nested-" + ("n" * 24)}}}]},
            {"evidence": [{"sentry_dsn": "https://fixture-public:fixture-secret@sentry.invalid/1"}]},
            {"evidence": [{"service_auth": {"opaque": "fixture-auth-" + ("h" * 24)}}]},
            {"evidence": [{"api_key": 1234567890123456}]},
            {"evidence": [{"database_password": -987654321}]},
            {"evidence": [{"access_pin": 1234}]},
            {"evidence": [{"oauth_client_secret": True}]},
            {"evidence": [{"password": "hunter2"}]},
            {"evidence": [{"api_key": "abc123"}]},
            {"evidence": [{"token": "short"}]},
            {"evidence": [{"database_url": "x"}]},
            {"summary": standalone_bearer},
            {"summary": standalone_basic},
            {"summary": cookie},
            {"summary": set_cookie},
            {"evidence": [{"session": "fixture-session-" + ("i" * 24)}]},
        ):
            with self.subTest(packet_shape=tuple(packet)):
                self.assertTrue(contains_sensitive_material(packet))
        code_fields = {
            "otp": 654321,
            "totp": "123456",
            "mfa_code": 829104,
            "verification_code": "V7X9",
            "security_code": 246810,
            "access_code": "A1B2",
            "recovery_code": "RC-7X9Q",
            "backup_code": 11223344,
            "cvv": 123,
        }
        for key, value in code_fields.items():
            with self.subTest(code_field=key):
                self.assertTrue(contains_sensitive_material({"evidence": [{key: value}]}))
        for packet in (
            {"summary": "The API key is missing and no token was available."},
            {"evidence": [{"api_key": "redacted"}, {"token": "not present"}]},
            {"evidence": [{"openai_api_key": "***REDACTED***"}]},
            {"evidence": [{"oauth_client_secret": {"value": "not configured"}}]},
            {"evidence": [{"database_url": "[removed]"}, {"connection_string": "omitted"}]},
            {"evidence": [{"api_key": None}, {"token": False}, {"access_pin": "***"}, {"otp": None}]},
        ):
            with self.subTest(safe_packet=packet):
                self.assertFalse(contains_sensitive_material(packet))

    def test_sanitizer_drops_unknown_and_sensitive_fields(self):
        event = sanitize_event("launch", {"run_id": "run_x", "provider": "Codex", "prompt": "private", "api_key": "bad", "reason_code": "/private/path"})
        self.assertNotIn("prompt", event["fields"])
        self.assertNotIn("api_key", event["fields"])
        self.assertEqual(len(event["fields"]["reason_code"]), 64)

    def test_sanitizer_hashes_provider_and_authorization_credentials_in_allowed_values(self):
        jwt = ".".join(("ey" + "J" + "h" * 18, "p" * 20, "s" * 20))
        credentialed_url = "://".join(("postgresql", "fixture-user:fixture-password@fixture-host/database"))
        values = (
            "gh" + "p_" + "a" * 28,
            "github_" + "pat_" + "b" * 28,
            "xox" + "b-" + "c" * 24,
            "x" + "app-" + "c" * 24,
            "x" + "ai-" + "d" * 24,
            "gl" + "pat-" + "e" * 24,
            "npm" + "_" + "f" * 24,
            "s" + "k-proj-" + "g" * 24,
            "Author" + "ization: Ba" + "sic " + base64.b64encode(b"fixture-user:fixture-password").decode(),
            "Author" + "ization: Be" + "arer " + "h" * 32,
            jwt,
            credentialed_url,
        )
        for value in values:
            with self.subTest(value_sha256=hashlib.sha256(value.encode()).hexdigest()):
                sanitized = sanitize_event("result", {"reason_code": value})
                self.assertEqual(sanitized["fields"]["reason_code"], hashlib.sha256(value.encode()).hexdigest())

    def test_sanitizer_hashes_nested_multiline_and_encoded_credentials(self):
        credential = "github_" + "pat_" + "a" * 28
        encoded_once = base64.b64encode(credential.encode()).decode()
        encoded_twice = base64.b64encode(encoded_once.encode()).decode()
        escaped = "github_" + "pat_" + "\\u0061" + "b" * 27
        values = (
            urllib.parse.quote(credential, safe=""),
            encoded_once,
            encoded_twice,
            json.dumps({"outer": {"value": credential}}),
            "first line\n" + "Author" + "ization: Ba" + "sic " + "Z" * 32,
            escaped,
        )
        for value in values:
            with self.subTest(value_sha256=hashlib.sha256(value.encode()).hexdigest()):
                sanitized = sanitize_event("validation", {"reason_code": value})
                self.assertEqual(sanitized["fields"]["reason_code"], hashlib.sha256(value.encode()).hexdigest())

    def test_sink_defensively_resanitizes_raw_credential_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            credential = "x" + "ai-" + "q" * 24
            JsonlSink(path).emit({
                "event_type": "result",
                "occurred_at": "2026-07-10T00:00:00Z",
                "fields": {"reason_code": credential},
            })
            row = json.loads(path.read_text())
            self.assertNotIn(credential, path.read_text())
            self.assertEqual(row["fields"]["reason_code"], hashlib.sha256(credential.encode()).hexdigest())

    def test_http_sink_defensively_resanitizes_raw_credential_value(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        credential = "gl" + "pat-" + "q" * 24
        opener = mock.Mock()
        opener.open.return_value = Response()
        sink = LangfuseHttpSink(
            "https://telemetry.example", "public", "secret",
            allowed_hosts={"telemetry.example"},
        )
        with mock.patch("agentfanout.telemetry.urllib.request.build_opener", return_value=opener):
            sink.emit({
                "event_type": "result",
                "occurred_at": "2026-07-10T00:00:00Z",
                "fields": {"reason_code": credential},
            })
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        serialized = json.dumps(body)
        self.assertNotIn(credential, serialized)
        metadata = body["batch"][0]["body"]["metadata"]
        self.assertEqual(metadata["reason_code"], hashlib.sha256(credential.encode()).hexdigest())

    def test_replan_is_the_standard_telemetry_vocabulary(self):
        event = sanitize_event("replan", {"status": "pending"})
        self.assertEqual(event["event_type"], "replan")
        with self.assertRaisesRegex(ValueError, "event_type_invalid"):
            sanitize_event("repair", {"status": "pending"})

    def test_jsonl_sink_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            sink = JsonlSink(path)
            sink.emit(sanitize_event("terminal", {"run_id": "run_x", "status": "complete"}))
            sink.emit(sanitize_event("retention", {"status": "dry_run"}))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_jsonl_sink_rejects_symlink_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.jsonl"
            target.write_text("unchanged\n")
            target.chmod(0o600)
            link = Path(tmp) / "events.jsonl"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "file_symlink"):
                JsonlSink(link)
            self.assertEqual(target.read_text(), "unchanged\n")

    def test_jsonl_sink_sets_private_modes_before_writing_under_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "events.jsonl"
            previous = os.umask(0o777)
            try:
                sink = JsonlSink(path)
                sink.emit(sanitize_event("terminal", {"status": "complete"}))
            finally:
                os.umask(previous)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_jsonl_sink_rejects_unsafe_preexisting_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "file_permissions_unsafe"):
                JsonlSink(path)

    def test_jsonl_sink_rejects_unsafe_preexisting_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            with mock.patch("agentfanout.telemetry.os.geteuid", return_value=os.geteuid() + 1):
                with self.assertRaisesRegex(ValueError, "directory_owner_unsafe"):
                    JsonlSink(path)

    def test_langfuse_sink_rejects_http_by_default(self):
        with self.assertRaisesRegex(ValueError, "https_required"):
            LangfuseHttpSink("http://127.0.0.1:3003", "public", "secret", allowed_hosts={"127.0.0.1"})

    def test_langfuse_sink_requires_explicit_host_allowlist(self):
        with self.assertRaisesRegex(ValueError, "not_allowed"):
            LangfuseHttpSink("https://telemetry.example", "public", "secret", allowed_hosts={"example.invalid"})
        with mock.patch.dict(os.environ, {"LANGFUSE_ALLOWED_HOSTS": "telemetry.example"}):
            with self.assertRaisesRegex(ValueError, "not_allowed"):
                LangfuseHttpSink("https://telemetry.example", "public", "secret", allowed_hosts=set())

    def test_langfuse_sink_accepts_allowlisted_https(self):
        sink = LangfuseHttpSink("https://telemetry.example", "public", "secret", allowed_hosts={"telemetry.example"})
        self.assertEqual(sink.url, "https://telemetry.example/api/public/ingestion")

    def test_langfuse_sink_rejects_nonfinite_or_nonpositive_timeout(self):
        for timeout in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
            with self.subTest(timeout=repr(timeout)):
                with self.assertRaisesRegex(ValueError, "timeout_invalid"):
                    LangfuseHttpSink(
                        "https://telemetry.example", "public", "secret",
                        timeout=timeout, allowed_hosts={"telemetry.example"},
                    )

    def test_langfuse_http_test_escape_is_loopback_only(self):
        sink = LangfuseHttpSink(
            "http://127.0.0.1:3003",
            "public",
            "secret",
            allowed_hosts={"127.0.0.1"},
            dangerously_allow_insecure_loopback_http_for_testing=True,
        )
        self.assertIn("127.0.0.1", sink.url)
        with self.assertRaisesRegex(ValueError, "https_required"):
            LangfuseHttpSink(
                "http://telemetry.example",
                "public",
                "secret",
                allowed_hosts={"telemetry.example"},
                dangerously_allow_insecure_loopback_http_for_testing=True,
            )


class LangfuseRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = Path(__file__).parents[1] / "scripts" / "langfuse-retention.py"
        spec = importlib.util.spec_from_file_location("agentfanout_langfuse_retention", script)
        if spec is None or spec.loader is None:
            raise RuntimeError("retention_test_import_failed")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_retention_rejects_http_by_default(self):
        with self.assertRaisesRegex(ValueError, "https_required"):
            self.module.validate_langfuse_host("http://127.0.0.1:3003", {"127.0.0.1"})

    def test_retention_http_test_escape_rejects_non_loopback(self):
        with self.assertRaisesRegex(ValueError, "https_required"):
            self.module.validate_langfuse_host(
                "http://telemetry.example",
                {"telemetry.example"},
                dangerously_allow_insecure_loopback_http_for_testing=True,
            )

    def test_retention_accepts_allowlisted_https(self):
        parsed = self.module.validate_langfuse_host("https://telemetry.example", {"telemetry.example"})
        self.assertEqual(parsed.scheme, "https")

    def test_retention_reads_private_secret_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "langfuse-secret"
            path.write_text("secret-value\n")
            path.chmod(0o600)
            self.assertEqual(self.module.read_secret_key_file(path), "secret-value")

    def test_retention_rejects_unsafe_secret_key_file_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "langfuse-secret"
            path.write_text("secret-value\n")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions_unsafe"):
                self.module.read_secret_key_file(path)

    def test_retention_rejects_symlink_secret_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("secret-value\n")
            target.chmod(0o600)
            link = Path(tmp) / "langfuse-secret"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "file_symlink"):
                self.module.read_secret_key_file(link)

    def test_retention_rejects_secret_key_argv(self):
        captured = io.StringIO()
        with redirect_stderr(captured):
            with self.assertRaises(SystemExit) as raised:
                self.module.main(["--secret-key", "must-not-appear-in-argv"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("langfuse_secret_key_argv_forbidden", captured.getvalue())
        self.assertNotIn("must-not-appear-in-argv", captured.getvalue())

    def test_retention_rejects_ambiguous_secret_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "langfuse-secret"
            path.write_text("file-secret\n")
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "sources_conflict"):
                self.module.resolve_secret_key("environment-secret", path)

    def test_retention_main_uses_private_secret_file_without_argv_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "langfuse-secret"
            path.write_text("file-secret\n")
            path.chmod(0o600)
            arguments = [
                "--host", "https://telemetry.example",
                "--public-key", "public",
                "--allowed-host", "telemetry.example",
                "--secret-key-file", str(path),
            ]
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                self.module, "request_json", return_value={"data": []}
            ) as request, redirect_stdout(io.StringIO()):
                self.assertEqual(self.module.main(arguments), 0)
            self.assertEqual(request.call_args.args[2], "file-secret")


if __name__ == "__main__":
    unittest.main()
