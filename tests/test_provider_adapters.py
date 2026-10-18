import hashlib
import json
import os
import stat
import sys
import shutil
import subprocess
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agentfanout.provider_adapters import (
    CODEX_DENIED_TOOL_FEATURES,
    ProviderAdapterError,
    _codex_no_tool_feature_preflight,
    _codex_platform_target,
    _primary_executable_observation,
    _file_observation,
    _prepare_pinned_executable,
    codex_no_tool_runtime_attestation,
    discover_codex_models,
    discover_grok_models,
    executable_identity,
    GROK_DIRECT_ENDPOINT_SHA256,
    grok_auth_file_identity,
    grok_direct_transport_preflight,
    grok_http_helper_identity,
    launch_codex,
    launch_grok,
    resolve_codex_model,
    resolve_grok_model,
    _run_rejected_grok_acp_diagnostic,
    run_json_process,
    verify_executable_identity,
)


FAKE = r'''#!__PYTHON__
import json, os, re, sys, time, urllib.error, urllib.request
args = sys.argv[1:]
if args[-2:] == ["features", "list"]:
    disabled = {args[index + 1] for index, value in enumerate(args[:-1]) if value == "--disable"}
    known = __DENIED_FEATURES__
    for name in known:
        lifecycle = "deprecated" if name in {"web_search_cached", "web_search_request"} else "stable"
        enabled = False if lifecycle == "deprecated" else name not in disabled
        print(f"{name:<36} {lifecycle:<18} {str(enabled).lower()}")
    print(f"{'fast_mode':<36} stable             true")
elif args == ["debug", "models"]:
    if os.environ.get("AGENT_FANOUT_TEST_TOKEN"):
        sys.exit(91)
    print(json.dumps({"models": [{"id": "codex-luna-current", "profiles": ["luna"]}, {"id": "codex-terra-current", "profiles": ["terra"]}, {"id": "codex-sol-current", "profiles": ["sol"]}]}))
elif args == ["models"]:
    if os.environ.get("AGENT_FANOUT_TEST_TOKEN"):
        sys.exit(91)
    print("Available models:\n  * grok-frontier-current (default)\n  - grok-fast-current")
elif args == ["sleep"]:
    time.sleep(1)
elif args == ["flood"]:
    sys.stdout.write("x" * 200000)
    sys.stdout.flush()
elif "exec" in args:
    if 'model_provider="agentfanout_probe"' in args:
        provider = next(value for value in args if value.startswith("model_providers.agentfanout_probe="))
        base_url = re.search(r'base_url="([^"]+)"', provider).group(1)
        wrong = urllib.request.Request(
            base_url + "/responses",
            data=json.dumps({"tools": [], "tool_choice": "none", "model": "attacker"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong-token"},
            method="POST",
        )
        try:
            urllib.request.urlopen(wrong, timeout=3).read()
        except urllib.error.HTTPError:
            pass
        request = urllib.request.Request(
            base_url + "/responses",
            data=json.dumps({"tools": [], "tool_choice": "none", "model": args[args.index("--model") + 1]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AGENT_FANOUT_CODEX_PROBE_KEY"]},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3).read()
        except urllib.error.HTTPError:
            pass
        raise SystemExit(0)
    prompt = sys.stdin.read()
    session_id = "11111111-1111-4111-8111-111111111111"
    if "resume" in args:
        candidate = args[-2]
        if candidate != session_id:
            sys.exit(92)
    print(json.dumps({"type": "thread.started", "thread_id": session_id}))
    if "attempt unified_exec" in prompt and not any(
        args[index:index + 2] == ["--disable", "unified_exec"] for index in range(len(args) - 1)
    ):
        print(json.dumps({"type":"item.started","item":{"type":"command_execution","command":"id"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "cached_tokens": 2, "output_tokens": 7, "cost_usd": 0.02, "price_card_version": "fixture-v1", "cost_source": "fixture"}, "prompt_length": len(prompt), "cwd": os.getcwd()}))
elif "agent" in args and args[-1] == "stdio":
    model = args[args.index("--model") + 1]
    effort = args[args.index("--reasoning-effort") + 1]
    profile = args[args.index("--agent-profile") + 1]
    profile_text = open(profile, encoding="utf-8").read()
    session_id = "22222222-2222-4222-8222-222222222222"
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"mcpCapabilities": {"http": True, "sse": True}},
                "authMethods": [],
                "_meta": {
                    "modelState": {"currentModelId": model, "availableModels": [{
                        "modelId": model,
                        "_meta": {"supportsReasoningEffort": True, "reasoningEffort": effort,
                                  "reasoningEfforts": [{"id": effort, "value": effort}]},
                    }]},
                    "mcpServers": [], "profileHasEmptyTools": "tools: []" in profile_text,
                    "cwd": os.getcwd(),
                },
            }}), flush=True)
        elif method == "session/new":
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
                "sessionId": session_id, "_meta": {"currentModelId": model},
            }}), flush=True)
        elif method == "session/prompt":
            text = request["params"]["prompt"][0]["text"]
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "agent_message_chunk", "content": {
                    "type": "text", "text": json.dumps({"text": "bounded", "prompt_length": len(text)})
                }},
            }}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
                "stopReason": "end_turn", "_meta": {"totalTokens": 18},
            }}), flush=True)
            break
        elif method == "session/cancel":
            break
elif "--prompt-file" in args:
    prompt_path = args[args.index("--prompt-file") + 1]
    prompt = open(prompt_path, encoding="utf-8").read()
    print(json.dumps({"text": "bounded", "prompt_length": len(prompt)}))
else:
    sys.exit(19)
'''


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.fake = Path(self.tmp.name) / "fake_provider.py"
        self.fake.write_text(
            FAKE.replace("__PYTHON__", sys.executable).replace(
                "__DENIED_FEATURES__", repr(list(CODEX_DENIED_TOOL_FEATURES)),
            ),
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.binary = [sys.executable, str(self.fake)]

    def grok_home(self, name):
        root = Path(self.tmp.name) / name
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        auth = root / "auth.json"
        auth.write_text(json.dumps({
            "schema": "GrokApiCredentialV1",
            "api_key": "fixture-api-key-" + "x" * 64,
        }) + "\n", encoding="utf-8")
        auth.chmod(0o600)
        return root

    @staticmethod
    def grok_direct_process_result(
        *, model="grok-4.5", effort="low", content='{"ok":true}', error=None,
        output_contract="WorkerResultV1",
    ):
        if error is not None:
            stdout = json.dumps({"schema": "GrokDirectErrorV1", "error": error})
            status, returncode = "failed", 2
        else:
            stdout = json.dumps({
                "schema": "GrokDirectResponseV1", "model": model,
                "reasoning_effort": effort,
                "reasoning_effort_mode": "api_parameter",
                "output_contract": output_contract,
                "endpoint_sha256": GROK_DIRECT_ENDPOINT_SHA256, "tools_field_sent": False,
                "response_format_type": "json_schema",
                "response_schema_name": output_contract,
                "redirects_allowed": False, "ambient_proxy_allowed": False,
                "response_id_sha256": "b" * 64, "event_count": 4,
                "response_bytes": 512, "content": content,
                "usage": {
                    "input_tokens": 18, "cached_tokens": 2,
                    "uncached_input_tokens": 16, "output_tokens": 7,
                    "total_tokens": 25, "usage_verified": True,
                    "cost_usd": None, "cost_verified": False,
                    "provider_cost_ticks_present": True,
                },
            })
            status, returncode = "completed", 0
        return {
            "status": status, "returncode": returncode, "stdout": stdout,
            "stderr": "", "output_truncated": False, "duration_ms": 12.5,
            "process_group_termination_confirmed": None,
            "termination_scope": "process_group_only",
            "detached_session_descendants_covered": False,
            "process_group_exit_confirmed": True,
            "environment_audit": {"preserved": [], "stripped_credential_names": []},
            "executable_pinning": {"method": "sealed_memfd"},
        }

    def fixture_codex_runtime_attestation(
        self, binary, timeout_seconds=15, *, resolved_model,
        session_mode="ephemeral", expected_executable_identity=None,
    ):
        """Unit-only injection; production Codex admission always blocks."""
        if session_mode != "ephemeral":
            raise ProviderAdapterError(
                f"codex_native_session_transport_attestation_unavailable:{session_mode}"
            )
        feature = _codex_no_tool_feature_preflight(
            binary, timeout_seconds=timeout_seconds,
            expected_executable_identity=expected_executable_identity,
        )
        return {
            "schema": "CodexNoToolRuntimeCompatibilityV1", "compatible": True,
            "feature_preflight": feature,
            "transport_attestation": {
                "schema": "CodexNoToolTransportAttestationV1",
                "tools_empty": True, "web_search_present": False,
                "tool_names": [], "tool_choice": "none", "loopback_only": True,
                "provider_credentials_used": False, "exact_model_id": resolved_model,
                "observed_request_model": resolved_model, "session_mode": session_mode,
                "rejected_unauthenticated_requests": 1,
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_discovers_current_slug_and_reports_measured_usage(self):
        models = discover_codex_models(self.binary)
        self.assertEqual(models[0]["id"], "codex-luna-current")
        beats = []
        with patch.dict(os.environ, {
            "GROK_HOME": "/tmp/not-for-codex-home",
            "GROK_AUTH_SOCK": "/tmp/not-for-codex.sock",
        }, clear=False):
            with patch(
                "agentfanout.provider_adapters.codex_no_tool_runtime_attestation",
                side_effect=self.fixture_codex_runtime_attestation,
            ):
                result = launch_codex(
                    binary=self.binary, project_root=self.root, selector="luna",
                    reasoning_effort="low", prompt="Classify this bounded fixture.",
                    timeout_seconds=3, heartbeat=beats.append,
                )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["resolved_model"], "codex-luna-current")
        self.assertEqual(result["usage"]["input_tokens"], 11)
        self.assertEqual(result["usage"]["uncached_input_tokens"], 9)
        self.assertEqual(result["usage"]["cost_usd"], 0.02)
        self.assertTrue(result["usage"]["usage_verified"])
        self.assertTrue(result["usage"]["cost_verified"])
        self.assertIn("read-only", result["argv"])
        self.assertFalse(result["sandbox"]["legacy_landlock_fallback"])
        self.assertTrue(result["sandbox"]["feature_preflight"]["legacy_landlock_requested"])
        self.assertFalse(result["sandbox"]["tools_enabled"])
        self.assertEqual(result["sandbox"]["workspace"], "isolated_empty_ephemeral")
        self.assertFalse(result["sandbox"]["project_root_mounted"])
        self.assertIn("--skip-git-repo-check", result["argv"])
        usage_event = next(event for event in result["events"] if "cwd" in event)
        self.assertNotEqual(Path(usage_event["cwd"]), self.root)
        self.assertIn("shell_tool", result["argv"])
        self.assertIn("unified_exec", result["argv"])
        self.assertIn(("--config", 'web_search="disabled"'), list(zip(result["argv"], result["argv"][1:])))
        self.assertNotIn(("--disable", "web_search_cached"), list(zip(result["argv"], result["argv"][1:])))
        self.assertNotIn(("--disable", "web_search_request"), list(zip(result["argv"], result["argv"][1:])))
        self.assertFalse(result["sandbox"]["feature_preflight"]["unified_exec"])
        transport = result["sandbox"]["transport_attestation"]
        self.assertTrue(transport["tools_empty"])
        self.assertTrue(transport["loopback_only"])
        self.assertFalse(transport["provider_credentials_used"])
        self.assertEqual(transport["tool_names"], [])
        self.assertEqual(transport["exact_model_id"], "codex-luna-current")
        self.assertEqual(transport["session_mode"], "ephemeral")
        self.assertGreaterEqual(transport["rejected_unauthenticated_requests"], 1)
        self.assertEqual(
            result["executable_pinning"]["method"],
            "sealed_memfd" if hasattr(os, "memfd_create") and sys.platform.startswith("linux")
            else "unlinked_descriptor",
        )
        self.assertNotIn("GROK_HOME", result["environment_audit"]["preserved"])
        self.assertNotIn("GROK_AUTH_SOCK", result["environment_audit"]["preserved"])
        self.assertGreaterEqual(len(beats), 1)

    @unittest.skipUnless(os.name == "posix" and Path("/proc/self/fd").is_dir(), "Linux descriptor fixture")
    def test_unlinked_executable_is_readonly_and_keeps_verified_bytes(self):
        import fcntl

        original = Path(self.tmp.name) / "native-fixture"
        shutil.copyfile(shutil.which("true"), original)
        original.chmod(0o500)
        identity = _file_observation(original)
        with patch("agentfanout.provider_adapters.sys.platform", "fixture-no-memfd"):
            descriptor, path, method = _prepare_pinned_executable(identity)
        try:
            self.assertEqual(method, "unlinked_descriptor")
            self.assertEqual(fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
            with self.assertRaises(OSError):
                os.write(descriptor, b"tamper")
            original.unlink()
            original.write_text("replacement, not executable")
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), identity["sha256"])
            result = subprocess.run([path], pass_fds=(descriptor,), timeout=3, check=False)
            self.assertEqual(result.returncode, 0)
        finally:
            os.close(descriptor)

    @unittest.skipUnless(os.name == "posix" and Path("/proc/self/fd").is_dir(), "Linux descriptor fixture")
    def test_unlinked_executable_rejects_writable_reopen_and_closes_descriptors(self):
        import fcntl

        identity = _file_observation(Path(sys.executable))
        before = set(Path("/proc/self/fd").iterdir())
        real_fcntl = fcntl.fcntl

        def wrong_access(descriptor, command, *args):
            return os.O_RDWR if command == fcntl.F_GETFL else real_fcntl(descriptor, command, *args)

        for failure, expected in ((wrong_access, ProviderAdapterError), (KeyboardInterrupt, KeyboardInterrupt)):
            with self.subTest(failure=expected.__name__):
                with (
                    patch("agentfanout.provider_adapters.sys.platform", "fixture-no-memfd"),
                    patch("agentfanout.provider_adapters.fcntl.fcntl", side_effect=failure),
                    self.assertRaises(expected),
                ):
                    _prepare_pinned_executable(identity)
                self.assertEqual(set(Path("/proc/self/fd").iterdir()), before)

    def test_codex_blocks_when_actual_transport_advertises_any_native_tool(self):
        unsafe = Path(self.tmp.name) / "unsafe-transport-codex.py"
        unsafe.write_text(
            self.fake.read_text(encoding="utf-8").replace(
                '{"tools": [], "tool_choice": "none", "model": args[args.index("--model") + 1]}',
                '{"tools": [{"type": "function", "name": "update_plan"}], '
                '"tool_choice": "auto", "model": args[args.index("--model") + 1]}',
            ),
            encoding="utf-8",
        )
        unsafe.chmod(0o700)
        with self.assertRaisesRegex(
            ProviderAdapterError,
            "codex_native_no_tool_transport_unavailable:update_plan",
        ):
            launch_codex(
                binary=[sys.executable, str(unsafe)], project_root=self.root,
                selector="luna", reasoning_effort="low", prompt="safe",
                timeout_seconds=3,
            )

    def test_interpreted_codex_cannot_auto_admit_from_synthetic_probe(self):
        observed = executable_identity(self.binary)
        with (
            patch("agentfanout.provider_adapters.verify_executable_identity", return_value=observed),
            patch("agentfanout.provider_adapters._codex_no_tool_feature_preflight", return_value={"ok": True}),
            patch("agentfanout.provider_adapters._codex_no_tool_transport_attestation", return_value={
                "schema": "CodexNoToolTransportAttestationV1", "tools_empty": True,
                "tool_names": [], "loopback_only": True,
            }),
        ):
            with self.assertRaisesRegex(
                ProviderAdapterError, "codex_native_real_provider_attestation_required",
            ):
                codex_no_tool_runtime_attestation(
                    self.binary, resolved_model="codex-luna-current",
                    expected_executable_identity=observed,
                )

    def test_official_native_codex_cannot_auto_admit_from_synthetic_probe(self):
        observed = {
            **executable_identity(self.binary),
            "codex_native_executable_identity": {
                "schema": "ExecutableFileIdentityV1",
                "path": "/fixture/official/codex",
                "sha256": "a" * 64,
                "size": 1,
                "mode": 0o700,
            },
        }
        with (
            patch("agentfanout.provider_adapters.verify_executable_identity", return_value=observed),
            patch("agentfanout.provider_adapters._codex_no_tool_feature_preflight", return_value={"ok": True}),
            patch("agentfanout.provider_adapters._codex_no_tool_transport_attestation", return_value={
                "schema": "CodexNoToolTransportAttestationV1", "tools_empty": True,
                "tool_names": [], "loopback_only": True,
            }),
        ):
            with self.assertRaisesRegex(
                ProviderAdapterError, "codex_native_real_provider_attestation_required",
            ):
                codex_no_tool_runtime_attestation(
                    self.binary, resolved_model="codex-luna-current",
                    expected_executable_identity=observed,
                )

    def test_codex_blocks_empty_tool_attestation_from_the_wrong_model(self):
        wrong_model = Path(self.tmp.name) / "wrong-model-transport-codex.py"
        wrong_model.write_text(
            self.fake.read_text(encoding="utf-8").replace(
                '{"tools": [], "tool_choice": "none", "model": args[args.index("--model") + 1]}',
                '{"tools": [], "tool_choice": "none", "model": "different-model"}',
            ),
            encoding="utf-8",
        )
        wrong_model.chmod(0o700)
        with self.assertRaisesRegex(
            ProviderAdapterError, "codex_transport_probe_model_mismatch",
        ):
            launch_codex(
                binary=[sys.executable, str(wrong_model)], project_root=self.root,
                selector="luna", reasoning_effort="low", prompt="safe",
                timeout_seconds=3,
            )

    def test_codex_unified_exec_surface_is_explicitly_denied(self):
        with patch(
            "agentfanout.provider_adapters.codex_no_tool_runtime_attestation",
            side_effect=self.fixture_codex_runtime_attestation,
        ):
            result = launch_codex(
                binary=self.binary, project_root=self.root, selector="luna",
                reasoning_effort="low", prompt="attempt unified_exec",
                timeout_seconds=3,
            )
        pairs = list(zip(result["argv"], result["argv"][1:]))
        self.assertIn(("--disable", "unified_exec"), pairs)
        self.assertFalse(any(event.get("type") == "item.started" for event in result["events"]))

    def test_codex_tool_event_is_rejected_even_after_feature_preflight(self):
        malicious = Path(self.tmp.name) / "malicious-codex.py"
        source = self.fake.read_text(encoding="utf-8").replace(
            'and not any(\n        args[index:index + 2] == ["--disable", "unified_exec"] for index in range(len(args) - 1)\n    )',
            "and True",
        )
        malicious.write_text(source, encoding="utf-8")
        malicious.chmod(0o700)
        with (
            patch(
                "agentfanout.provider_adapters.codex_no_tool_runtime_attestation",
                side_effect=self.fixture_codex_runtime_attestation,
            ),
            self.assertRaisesRegex(ProviderAdapterError, "codex_tool_item_forbidden:command_execution"),
        ):
            launch_codex(
                binary=[sys.executable, str(malicious)], project_root=self.root,
                selector="luna", reasoning_effort="low",
                prompt="attempt unified_exec", timeout_seconds=3,
            )

    def test_codex_persistent_and_resumed_sessions_require_separate_transport_proof(self):
        with self.assertRaisesRegex(
            ProviderAdapterError,
            "codex_native_session_transport_attestation_unavailable:persistent",
        ):
            launch_codex(
                binary=self.binary, project_root=self.root, selector="luna",
                reasoning_effort="low", prompt="First bounded pass.",
                timeout_seconds=3, persist_session=True,
            )
        with self.assertRaisesRegex(
            ProviderAdapterError,
            "codex_native_session_transport_attestation_unavailable:resumed",
        ):
            launch_codex(
                binary=self.binary, project_root=self.root, selector="luna",
                reasoning_effort="low", prompt="Follow up.", timeout_seconds=3,
                persist_session=True,
                resume_session_id="11111111-1111-4111-8111-111111111111",
                resolved_model="codex-luna-current",
            )

    def test_codex_resume_rejects_frozen_launcher_drift(self):
        frozen = executable_identity(self.binary)
        with patch(
            "agentfanout.provider_adapters.codex_no_tool_runtime_attestation",
            side_effect=self.fixture_codex_runtime_attestation,
        ):
            launch_codex(
                binary=self.binary, project_root=self.root, selector="luna",
                reasoning_effort="low", prompt="First bounded pass.", timeout_seconds=3,
                expected_executable_identity=frozen,
            )
        self.fake.write_text(self.fake.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        with (
            patch(
                "agentfanout.provider_adapters.codex_no_tool_runtime_attestation",
                side_effect=self.fixture_codex_runtime_attestation,
            ),
            self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"),
        ):
            launch_codex(
                binary=self.binary, project_root=self.root, selector="luna",
                reasoning_effort="low", prompt="Second bounded pass.", timeout_seconds=3,
                resolved_model="codex-luna-current", expected_executable_identity=frozen,
            )

    def test_frozen_executable_identity_detects_wrapper_argument_replacement(self):
        frozen = executable_identity(self.binary)
        self.assertEqual(len(frozen["argument_file_identities"]), 1)
        self.fake.write_text(
            FAKE.replace("__PYTHON__", sys.executable) + "\n# replacement\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"):
            verify_executable_identity(self.binary, frozen)

    def test_official_codex_wrapper_identity_binds_node_package_and_native_target(self):
        target = _codex_platform_target()
        if target is None:
            self.skipTest("fixture platform is not supported by the official wrapper map")
        triple, package_name = target
        package_root = Path(self.tmp.name) / "codex-package"
        wrapper = package_root / "bin" / "codex.js"
        native = package_root / "node_modules" / "@openai" / package_name / "vendor" / triple / "bin" / "codex"
        node = Path(self.tmp.name) / "node-bin" / "node"
        wrapper.parent.mkdir(parents=True)
        native.parent.mkdir(parents=True)
        node.parent.mkdir(parents=True)
        (package_root / "package.json").write_text('{"name":"@openai/codex"}', encoding="utf-8")
        (native.parents[3] / "package.json").write_text('{"name":"@openai/platform"}', encoding="utf-8")
        native.write_bytes(b"native-v1")
        native.chmod(0o700)
        node.write_bytes(b"node-v1")
        node.chmod(0o700)
        wrapper.write_text(
            "#!/usr/bin/env node\nPLATFORM_PACKAGE_BY_TARGET; findCodexExecutable; "
            "@openai/codex-linux-x64; targetTriple; vendor; bin; spawn(binaryPath);\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        with patch.dict(os.environ, {"PATH": str(node.parent)}, clear=False):
            frozen = executable_identity(str(wrapper))
            self.assertEqual(frozen["codex_wrapper_argv_index"], 0)
            self.assertEqual(frozen["codex_native_executable_identity"]["sha256"], hashlib.sha256(b"native-v1").hexdigest())
            node.write_bytes(b"node-v2")
            with self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"):
                verify_executable_identity(str(wrapper), frozen)
            node.write_bytes(b"node-v1")
            native.write_bytes(b"native-v2")
            with self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"):
                verify_executable_identity(str(wrapper), frozen)

    def test_exact_configured_model_precedes_alias_and_must_be_listed(self):
        models = discover_codex_models(self.binary)
        with patch.dict(os.environ, {"AGENT_FANOUT_CODEX_FAST_MODEL": "codex-luna-current"}, clear=False):
            self.assertEqual(resolve_codex_model("current-fast-mini", models), "codex-luna-current")
        with patch.dict(os.environ, {"AGENT_FANOUT_CODEX_FAST_MODEL": "codex-not-listed"}, clear=False):
            with self.assertRaisesRegex(ProviderAdapterError, "configured_model_not_listed"):
                resolve_codex_model("current-fast-mini", models)

    def test_astra_requires_explicit_selector_and_exact_model(self):
        models = [
            {"id": "gpt-5.6-sol", "profiles": ["sol"]},
            {"id": "gpt-6-astra", "profiles": ["astra"]},
        ]
        with patch.dict(os.environ, {
            "AGENT_FANOUT_CODEX_FRONTIER_MODEL": "",
            "AGENT_FANOUT_CODEX_ASTRA_MODEL": "",
        }, clear=False):
            self.assertEqual(resolve_codex_model("current-frontier", models), "gpt-5.6-sol")
            self.assertEqual(resolve_codex_model("astra-explicit", models), "gpt-6-astra")
        with patch.dict(os.environ, {"AGENT_FANOUT_CODEX_FRONTIER_MODEL": "gpt-6-astra"}, clear=False):
            with self.assertRaisesRegex(ProviderAdapterError, "astra_requires_explicit_selector"):
                resolve_codex_model("current-frontier", models)
        with patch.dict(os.environ, {"AGENT_FANOUT_CODEX_ASTRA_MODEL": "gpt-5.6-sol"}, clear=False):
            with self.assertRaisesRegex(ProviderAdapterError, "astra_exact_model_required"):
                resolve_codex_model("astra-explicit", models)

    def test_model_discovery_strips_parent_credentials_for_both_providers(self):
        old = os.environ.get("AGENT_FANOUT_TEST_TOKEN")
        os.environ["AGENT_FANOUT_TEST_TOKEN"] = "not-for-discovery"
        try:
            self.assertEqual(discover_codex_models(self.binary)[0]["id"], "codex-luna-current")
            self.assertEqual(discover_grok_models(self.binary)[0]["id"], "grok-frontier-current")
        finally:
            if old is None:
                os.environ.pop("AGENT_FANOUT_TEST_TOKEN", None)
            else:
                os.environ["AGENT_FANOUT_TEST_TOKEN"] = old

    def test_grok_discovery_forces_disposable_home_and_ignores_ambient_config(self):
        ambient_home = Path(self.tmp.name) / "ambient-user-home"
        ambient_grok = Path(self.tmp.name) / "ambient-grok-home"
        ambient_home.mkdir(mode=0o700)
        ambient_grok.mkdir(mode=0o700)
        (ambient_home / ".grok").mkdir(mode=0o700)
        (ambient_home / ".grok" / "ambient-marker").write_text("private", encoding="utf-8")
        (ambient_grok / "ambient-marker").write_text("private", encoding="utf-8")
        dedicated_home = self.grok_home("dedicated-discovery-home")
        (dedicated_home / "config.json").write_text('{"private":"config"}\n', encoding="utf-8")
        auth_identity = grok_auth_file_identity(dedicated_home)
        report = Path(self.tmp.name) / "grok-discovery-environment.json"
        fixture = Path(self.tmp.name) / "isolated-grok-discovery.py"
        fixture.write_text(textwrap.dedent(f'''\
            #!{sys.executable}
            import json, os, sys
            from pathlib import Path
            if sys.argv[1:] != ["models"]:
                raise SystemExit(19)
            home = Path(os.environ["HOME"])
            grok_home = Path(os.environ["GROK_HOME"])
            Path({str(report)!r}).write_text(json.dumps({{
                "home": str(home), "grok_home": str(grok_home),
                "home_marker_visible": (home / ".grok" / "ambient-marker").exists(),
                "grok_marker_visible": (grok_home / "ambient-marker").exists(),
                "auth_visible": (grok_home / "auth.json").is_file(),
                "explicit_api_key_present": os.environ.get("XAI_API_KEY") == "fixture-api-key-" + "x" * 64,
                "config_visible": (grok_home / "config.json").exists(),
            }}), encoding="utf-8")
            print("Available models:\\n  - grok-fast-isolated")
        '''), encoding="utf-8")
        fixture.chmod(0o700)
        with patch.dict(os.environ, {
            "HOME": str(ambient_home), "GROK_HOME": str(ambient_grok),
            "XAI_API_KEY": "ambient-key-must-not-be-inherited",
        }, clear=False):
            models = discover_grok_models(
                [sys.executable, str(fixture)], grok_home=dedicated_home,
                expected_grok_auth_file_identity=auth_identity,
                expected_grok_helper_identity=grok_http_helper_identity(),
            )
        observed = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(models[0]["id"], "grok-fast-isolated")
        self.assertNotEqual(observed["home"], str(ambient_home))
        self.assertNotEqual(observed["grok_home"], str(ambient_grok))
        self.assertIn("agentfanout-grok-discovery-", observed["home"])
        self.assertIn("agentfanout-grok-discovery-", observed["grok_home"])
        self.assertFalse(observed["home_marker_visible"])
        self.assertFalse(observed["grok_marker_visible"])
        self.assertFalse(observed["auth_visible"])
        self.assertTrue(observed["explicit_api_key_present"])
        self.assertFalse(observed["config_visible"])

    def test_grok_discovery_rejects_system_auth_policy_before_child_launch(self):
        home = self.grok_home("managed-policy-credential")
        identity = grok_auth_file_identity(home)
        actual_exists = Path.exists

        def system_policy_exists(path):
            return str(path) == "/etc/grok" or actual_exists(path)

        with patch.object(Path, "exists", system_policy_exists):
            with patch("agentfanout.provider_adapters.run_json_process") as child:
                with self.assertRaisesRegex(ProviderAdapterError, "grok_system_configuration_requires_review"):
                    discover_grok_models(
                        self.binary, grok_home=home,
                        expected_grok_auth_file_identity=identity,
                        expected_grok_helper_identity=grok_http_helper_identity(),
                    )
                child.assert_not_called()

    def test_grok_discovery_rejects_credential_echo_without_returning_key(self):
        home = self.grok_home("echo-credential")
        identity = grok_auth_file_identity(home)
        fake = Path(self.tmp.name) / "echo-discovery.py"
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                fake.write_text(
                    "import os, sys\nprint(os.environ['XAI_API_KEY'], file=sys." + stream + ")\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ProviderAdapterError, "grok_discovery_credential_echo_denied") as error:
                    discover_grok_models(
                        [sys.executable, str(fake)], grok_home=home,
                        expected_grok_auth_file_identity=identity,
                        expected_grok_helper_identity=grok_http_helper_identity(),
                    )
                self.assertNotIn("fixture-api-key", str(error.exception))

    def test_grok_hard_linked_credential_cannot_be_bound(self):
        home = self.grok_home("hard-link-credential")
        os.link(home / "auth.json", home / "other-name")
        with self.assertRaisesRegex(ProviderAdapterError, "grok_auth_file_security_invalid"):
            grok_auth_file_identity(home)

    def test_api_preflight_rejects_legacy_unexpired_claims(self):
        home = self.grok_home("legacy-preflight-response")
        identity = grok_auth_file_identity(home)
        legacy = {
            "schema": "GrokDirectAuthPreflightResponseV1",
            "auth_entry_unambiguous": True, "auth_unexpired": True,
            "endpoint_sha256": GROK_DIRECT_ENDPOINT_SHA256,
            "tools_field_sent": False, "redirects_allowed": False,
            "ambient_proxy_allowed": False,
        }
        result = {"status": "completed", "returncode": 0,
                  "stdout": json.dumps(legacy), "stderr": ""}
        with patch("agentfanout.provider_adapters.run_json_process", return_value=result):
            with self.assertRaisesRegex(ProviderAdapterError, "grok_http_preflight_response_invalid"):
                grok_direct_transport_preflight(home, identity, grok_http_helper_identity())

    def test_grok_discovery_rejects_unicode_escaped_key_before_catalog_decode(self):
        home = self.grok_home("encoded-echo-credential")
        identity = grok_auth_file_identity(home)
        fake = Path(self.tmp.name) / "encoded-echo-discovery.py"
        fixture = textwrap.dedent(r'''
            import os
            key = os.environ['XAI_API_KEY']
            encoded = ''.join(chr(92) + 'u%04x' % ord(char) for char in key)
            print('{"models":[{"id":"' + encoded + '","profiles":["fast"]}]}')
        ''')
        for diagnostic in ("", 'warning "', 'warning "\t'):
            with self.subTest(diagnostic=diagnostic):
                fake.write_text("print(" + repr(diagnostic) + ")\n" + fixture, encoding="utf-8")
                with self.assertRaisesRegex(ProviderAdapterError, "grok_discovery_credential_echo_denied") as error:
                    discover_grok_models(
                        [sys.executable, str(fake)], grok_home=home,
                        expected_grok_auth_file_identity=identity,
                        expected_grok_helper_identity=grok_http_helper_identity(),
                    )
                self.assertNotIn("fixture-api-key", str(error.exception))

    def test_codex_discovery_and_feature_preflight_pin_before_execution(self):
        original = self.fake.read_text(encoding="utf-8")
        for operation in ("models", "features"):
            with self.subTest(operation=operation):
                self.fake.write_text(original, encoding="utf-8")
                frozen = executable_identity(self.binary)
                marker = Path(self.tmp.name) / f"codex-{operation}-replacement-invoked"

                def verify_then_replace(binary, expected):
                    observed = verify_executable_identity(binary, expected)
                    self.fake.write_text(
                        original + f"\nopen({str(marker)!r}, 'w').write('invoked')\n",
                        encoding="utf-8",
                    )
                    return observed

                with patch(
                    "agentfanout.provider_adapters.verify_executable_identity",
                    side_effect=verify_then_replace,
                ):
                    with self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"):
                        if operation == "models":
                            discover_codex_models(
                                self.binary, expected_executable_identity=frozen,
                            )
                        else:
                            _codex_no_tool_feature_preflight(
                                self.binary,
                                expected_executable_identity=frozen,
                            )
                self.assertFalse(marker.exists())
        self.fake.write_text(original, encoding="utf-8")

    def test_grok_discovery_pins_before_execution(self):
        original = self.fake.read_text(encoding="utf-8")
        grok_home = self.grok_home("grok-home-toctou")
        auth_identity = grok_auth_file_identity(grok_home)
        for operation in ("models",):
            with self.subTest(operation=operation):
                self.fake.write_text(original, encoding="utf-8")
                frozen = executable_identity(self.binary)
                marker = Path(self.tmp.name) / f"grok-{operation}-replacement-invoked"

                def verify_then_replace(binary, expected):
                    observed = verify_executable_identity(binary, expected)
                    self.fake.write_text(
                        original + f"\nopen({str(marker)!r}, 'w').write('invoked')\n",
                        encoding="utf-8",
                    )
                    return observed

                with patch(
                    "agentfanout.provider_adapters.verify_executable_identity",
                    side_effect=verify_then_replace,
                ):
                    with self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"):
                        discover_grok_models(
                            self.binary, expected_executable_identity=frozen,
                        )
                self.assertFalse(marker.exists())
        self.fake.write_text(original, encoding="utf-8")

    def test_model_discovery_output_is_bounded(self):
        flood = Path(self.tmp.name) / "flood_discovery.py"
        flood.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 2000000)\n",
            encoding="utf-8",
        )
        flood.chmod(flood.stat().st_mode | stat.S_IXUSR)
        with self.assertRaisesRegex(ProviderAdapterError, "provider_model_discovery_unavailable"):
            discover_codex_models([sys.executable, str(flood)])

    def test_grok_ambiguous_auth_blocks_before_discovery_cli_starts(self):
        grok_home = self.grok_home("grok-home-duplicate-auth")
        token_a = "fixture-duplicate-a-" + ("a" * 64)
        token_b = "fixture-duplicate-b-" + ("b" * 64)
        (grok_home / "auth.json").write_text(
            '{"https://auth.x.ai::fixture":{"key":"' + token_a
            + '","auth_mode":"oidc","expires_at":"2099-01-01T00:00:00Z"},'
            + '"https://auth.x.ai::fixture":{"key":"' + token_b
            + '","auth_mode":"oidc","expires_at":"2099-01-01T00:00:00Z"}}',
            encoding="utf-8",
        )
        (grok_home / "auth.json").chmod(0o600)
        auth_identity = grok_auth_file_identity(grok_home)
        marker = Path(self.tmp.name) / "ambiguous-auth-discovery-started"
        discovery = Path(self.tmp.name) / "ambiguous-auth-grok.py"
        discovery.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('started')\n"
            "print('Available models:\\n  - grok-composer-2.5-fast')\n",
            encoding="utf-8",
        )
        discovery.chmod(0o700)
        with self.assertRaisesRegex(ProviderAdapterError, "grok_http_auth_duplicate_key"):
            discover_grok_models(
                [sys.executable, str(discovery)],
                grok_home=grok_home,
                expected_grok_auth_file_identity=auth_identity,
                expected_grok_helper_identity=grok_http_helper_identity(),
            )
        self.assertFalse(marker.exists())

    def test_grok_discovery_rejects_helper_drift_before_cli_starts(self):
        grok_home = self.grok_home("grok-home-discovery-helper-drift")
        auth_identity = grok_auth_file_identity(grok_home)
        helper = Path(self.tmp.name) / "discovery-helper.py"
        helper.write_text("raise SystemExit(0)\n", encoding="utf-8")
        helper.chmod(0o700)
        helper_argv = [sys.executable, "-I", "-S", str(helper)]
        frozen_helper = executable_identity(helper_argv)
        helper.write_text("raise SystemExit(7)\n", encoding="utf-8")
        marker = Path(self.tmp.name) / "helper-drift-discovery-started"
        discovery = Path(self.tmp.name) / "helper-drift-grok.py"
        discovery.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('started')\n"
            "print('Available models:\\n  - grok-composer-2.5-fast')\n",
            encoding="utf-8",
        )
        discovery.chmod(0o700)
        with patch(
            "agentfanout.provider_adapters.grok_http_helper_binary",
            return_value=helper_argv,
        ):
            with self.assertRaisesRegex(
                ProviderAdapterError, "provider_executable_identity_changed",
            ):
                discover_grok_models(
                    [sys.executable, str(discovery)],
                    grok_home=grok_home,
                    expected_grok_auth_file_identity=auth_identity,
                    expected_grok_helper_identity=frozen_helper,
                )
        self.assertFalse(marker.exists())

    def test_grok_direct_https_isolated_no_tool_and_measured_usage(self):
        grok_home = self.grok_home("grok-home")
        auth_identity = grok_auth_file_identity(grok_home)
        with patch(
            "agentfanout.provider_adapters.run_json_process",
            return_value=self.grok_direct_process_result(),
        ) as process:
            result = launch_grok(
                binary=self.binary, project_root=self.root, selector="grok-fast",
                reasoning_effort="low", prompt="Summarize this bounded fixture.",
                output_contract="WorkerResultV1",
                max_prompt_tokens=2048, max_output_tokens=123,
                timeout_seconds=3, grok_home=grok_home,
                resolved_model="grok-4.5",
                expected_auth_file_identity=auth_identity,
                expected_helper_identity=grok_http_helper_identity(),
            )
        helper_request = json.loads(process.call_args.args[1])
        self.assertEqual(helper_request["max_output_tokens"], 123)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["resolved_model"], "grok-4.5")
        self.assertTrue(result["usage"]["usage_verified"])
        self.assertEqual(result["usage"]["input_tokens"], 18)
        self.assertEqual(result["usage"]["output_tokens"], 7)
        self.assertFalse(result["usage"]["cost_verified"])
        self.assertIsNone(result["usage"]["cost_usd"])
        self.assertIn("-I", result["argv"])
        self.assertTrue(result["argv"][-1].endswith("grok_http_worker.py"))
        self.assertFalse(result["sandbox"]["filesystem_capability"])
        self.assertFalse(result["sandbox"]["terminal_capability"])
        self.assertFalse(result["sandbox"]["project_root_mounted"])
        self.assertEqual(result["sandbox"]["mcp_servers"], [])
        self.assertFalse(result["transport_attestation"]["tools_field_sent"])
        self.assertFalse(result["transport_attestation"]["redirects_allowed"])
        self.assertEqual(result["model_acknowledgement"]["current_model_id"], "grok-4.5")
        self.assertTrue(result["auth_isolation"]["auth_identity_prebound"])
        self.assertFalse(result["auth_isolation"]["ambient_auth_socket_allowed"])

    def test_grok_auth_file_is_prebound_and_ambient_socket_is_not_inherited(self):
        grok_home = self.grok_home("grok-home-auth-boundary")
        frozen = grok_auth_file_identity(grok_home)
        with self.assertRaisesRegex(ProviderAdapterError, "grok_auth_file_identity_required"):
            launch_grok(
                binary=self.binary, project_root=self.root, selector="grok-fast",
                reasoning_effort="low", prompt="safe", output_contract="WorkerResultV1", timeout_seconds=3,
                grok_home=grok_home, resolved_model="grok-4.5",
            )
        with self.assertRaisesRegex(ProviderAdapterError, "grok_http_helper_identity_required"):
            launch_grok(
                binary=self.binary, project_root=self.root, selector="grok-fast",
                reasoning_effort="low", prompt="safe", output_contract="WorkerResultV1", timeout_seconds=3,
                grok_home=grok_home, resolved_model="grok-4.5",
                expected_auth_file_identity=frozen,
            )
        with (
            patch.dict(os.environ, {
                "GROK_AUTH_SOCK": "/tmp/ambient-grok.sock",
                "XDG_RUNTIME_DIR": "/tmp/ambient-runtime",
            }, clear=False),
            patch(
                "agentfanout.provider_adapters.run_json_process",
                return_value=self.grok_direct_process_result(),
            ) as process,
        ):
            result = launch_grok(
                binary=self.binary, project_root=self.root, selector="grok-fast",
                reasoning_effort="low", prompt="safe", output_contract="WorkerResultV1", timeout_seconds=3,
                grok_home=grok_home, resolved_model="grok-4.5",
                expected_auth_file_identity=frozen,
                expected_helper_identity=grok_http_helper_identity(),
            )
        helper_allowlist = process.call_args.kwargs["environment_allowlist"]
        self.assertNotIn("SSL_CERT_FILE", helper_allowlist)
        self.assertNotIn("SSL_CERT_DIR", helper_allowlist)
        preserved = result["environment_audit"]["preserved"]
        self.assertNotIn("GROK_AUTH_SOCK", preserved)
        self.assertNotIn("XDG_RUNTIME_DIR", preserved)
        (grok_home / "auth.json").write_text('{"fixture":"changed"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ProviderAdapterError, "grok_auth_file_identity_changed"):
            launch_grok(
                binary=self.binary, project_root=self.root, selector="grok-fast",
                reasoning_effort="low", prompt="safe", output_contract="WorkerResultV1", timeout_seconds=3,
                grok_home=grok_home, resolved_model="grok-4.5",
                expected_auth_file_identity=frozen,
                expected_helper_identity=grok_http_helper_identity(),
            )

    def test_grok_prompt_budget_blocks_before_auth_copy_or_provider_process(self):
        grok_home = self.grok_home("grok-home-budget-preflight")
        auth_identity = grok_auth_file_identity(grok_home)
        with patch("agentfanout.provider_adapters.run_json_process") as process:
            with self.assertRaisesRegex(
                ProviderAdapterError, "grok_prompt_exceeds_prelaunch_token_ceiling",
            ):
                launch_grok(
                    binary=self.binary, project_root=self.root, selector="grok-fast",
                    reasoning_effort="low", prompt="x" * 1024,
                    output_contract="WorkerResultV1",
                    max_prompt_tokens=700, max_output_tokens=50,
                    timeout_seconds=3, grok_home=grok_home,
                    resolved_model="grok-4.5",
                    expected_auth_file_identity=auth_identity,
                    expected_helper_identity=grok_http_helper_identity(),
                )
        process.assert_not_called()

    def test_grok_helper_replacement_after_freeze_fails_before_execution(self):
        grok_home = self.grok_home("grok-home-helper-toctou")
        auth_identity = grok_auth_file_identity(grok_home)
        helper = Path(self.tmp.name) / "grok-helper.py"
        marker = Path(self.tmp.name) / "replacement-invoked"
        helper.write_text("raise SystemExit(0)\n", encoding="utf-8")
        helper.chmod(0o700)
        helper_argv = [sys.executable, "-I", "-S", str(helper)]
        frozen_helper = executable_identity(helper_argv)
        helper.write_text(
            f"open({str(marker)!r}, 'w').write('invoked')\n",
            encoding="utf-8",
        )
        with patch(
            "agentfanout.provider_adapters.grok_http_helper_binary",
            return_value=helper_argv,
        ):
            with self.assertRaisesRegex(ProviderAdapterError, "provider_executable_identity_changed"):
                launch_grok(
                    binary=self.binary, project_root=self.root,
                    selector="grok-fast", reasoning_effort="low", prompt="safe",
                    output_contract="WorkerResultV1",
                    timeout_seconds=3, grok_home=grok_home,
                    resolved_model="grok-4.5",
                    expected_auth_file_identity=auth_identity,
                    expected_helper_identity=frozen_helper,
                )
        self.assertFalse(marker.exists())

    def test_grok_exact_model_transport_and_effort_compatibility_fail_closed(self):
        grok_home = self.grok_home("grok-home-model-compatibility")
        auth_identity = grok_auth_file_identity(grok_home)
        helper_identity = grok_http_helper_identity()
        for model, effort, expected in (
            ("grok-4.20-multi-agent", "high", "packet_contract_unsupported"),
            ("grok-composer-2.5-fast", "low", "packet_contract_unsupported"),
            ("grok-4.5", "xhigh", "reasoning_effort_unsupported"),
        ):
            with self.subTest(model=model, effort=effort):
                with self.assertRaisesRegex(ProviderAdapterError, expected):
                    launch_grok(
                        binary=self.binary, project_root=self.root,
                        selector="grok-frontier", reasoning_effort=effort,
                        prompt="safe", output_contract="WorkerResultV1", timeout_seconds=3, grok_home=grok_home,
                        resolved_model=model,
                        expected_auth_file_identity=auth_identity,
                        expected_helper_identity=helper_identity,
                    )

    def test_grok_missing_dedicated_auth_file_fails_closed(self):
        grok_home = Path(self.tmp.name) / "grok-home-no-auth"
        grok_home.mkdir(mode=0o700)
        with self.assertRaisesRegex(ProviderAdapterError, "grok_dedicated_auth_file_required"):
            grok_auth_file_identity(grok_home)

    def test_grok_default_or_broad_home_is_rejected(self):
        broad = Path(self.tmp.name) / "broad-grok-home"
        broad.mkdir(mode=0o755)
        broad.chmod(0o755)
        with self.assertRaisesRegex(ProviderAdapterError, "permissions_too_broad"):
            launch_grok(
                binary=self.binary, project_root=self.root, selector="grok-fast",
                reasoning_effort="low", prompt="safe", output_contract="WorkerResultV1", timeout_seconds=3,
                grok_home=broad,
            )

    def test_grok_acp_fails_closed_on_mcp_tool_or_missing_usage(self):
        grok_home = self.grok_home("grok-home-violations")
        auth_identity = grok_auth_file_identity(grok_home)
        for mode, expected in (
            ("mcp", "mcp_servers_forbidden"),
            ("tool", "tool_call_forbidden"),
            ("usage", "total_tokens_missing"),
        ):
            with self.subTest(mode=mode):
                process_exit_confirmed = threading.Event()
                fake = Path(self.tmp.name) / f"fake_grok_{mode}.py"
                fake.write_text(textwrap.dedent(f'''\
                    #!{sys.executable}
                    import json, sys
                    model = sys.argv[sys.argv.index("--model") + 1]
                    effort = sys.argv[sys.argv.index("--reasoning-effort") + 1]
                    session = "33333333-3333-4333-8333-333333333333"
                    for line in sys.stdin:
                        request = json.loads(line)
                        method = request.get("method")
                        if method == "initialize":
                            print(json.dumps({{"jsonrpc":"2.0","id":request["id"],"result":{{
                                "protocolVersion":1,"agentCapabilities":{{}},"authMethods":[],"_meta":{{
                                    "modelState":{{"currentModelId":model,"availableModels":[{{"modelId":model,"_meta":{{
                                        "supportsReasoningEffort":True,"reasoningEffort":effort,
                                        "reasoningEfforts":[{{"id":effort,"value":effort}}]
                                    }}}}]}},"mcpServers":[]
                                }}
                            }}}}), flush=True)
                            if {mode!r} == "mcp":
                                print(json.dumps({{"jsonrpc":"2.0","method":"_x.ai/mcp/servers_updated","params":{{
                                    "mcpServers":[{{"name":"forbidden"}}]
                                }}}}), flush=True)
                        elif method == "session/new":
                            print(json.dumps({{"jsonrpc":"2.0","id":request["id"],"result":{{"sessionId":session}}}}), flush=True)
                        elif method == "session/prompt":
                            if {mode!r} == "tool":
                                print(json.dumps({{"jsonrpc":"2.0","method":"session/update","params":{{
                                    "sessionId":session,"update":{{"sessionUpdate":"tool_call","toolCallId":"bad"}}
                                }}}}), flush=True)
                            else:
                                print(json.dumps({{"jsonrpc":"2.0","method":"session/update","params":{{
                                    "sessionId":session,"update":{{"sessionUpdate":"agent_message_chunk","content":{{
                                        "type":"text","text":"{{\\"ok\\":true}}"
                                    }}}}
                                }}}}), flush=True)
                            print(json.dumps({{"jsonrpc":"2.0","id":request["id"],"result":{{
                                "stopReason":"end_turn","_meta":{{}} if {mode!r} == "usage" else {{"totalTokens":9}}
                            }}}}), flush=True)
                            break
                '''), encoding="utf-8")
                fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
                identity = executable_identity([sys.executable, str(fake)])
                execution_root = Path(self.tmp.name) / f"legacy-acp-{mode}"
                execution_root.mkdir()
                with self.assertRaisesRegex(ProviderAdapterError, expected):
                    _run_rejected_grok_acp_diagnostic(
                        [
                            sys.executable, str(fake), "--model", "grok-fast-current",
                            "--reasoning-effort", "low",
                        ],
                        "safe", execution_root, grok_home,
                        "grok-fast-current", "low", 3,
                        pinned_executable_identity=_primary_executable_observation(identity),
                        pinned_argument_file_identities=identity["argument_file_identities"],
                        on_process_group_exit_confirmed=process_exit_confirmed.set,
                    )
                self.assertTrue(process_exit_confirmed.is_set())

    def test_grok_acp_cancellation_confirms_process_group_exit(self):
        grok_home = self.grok_home("grok-home-cancel")
        execution_root = Path(self.tmp.name) / "legacy-acp-cancel"
        execution_root.mkdir()
        profile = execution_root / "profile.md"
        profile.write_text("---\ntools: []\n---\n", encoding="utf-8")
        identity = executable_identity(self.binary)
        result = _run_rejected_grok_acp_diagnostic(
            [
                *self.binary, "--model", "grok-fast-current",
                "--reasoning-effort", "low", "--agent-profile", str(profile),
            ],
            "safe", execution_root, grok_home,
            "grok-fast-current", "low", 3,
            cancellation_requested=lambda: True,
            pinned_executable_identity=_primary_executable_observation(identity),
            pinned_argument_file_identities=identity["argument_file_identities"],
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["process_group_exit_confirmed"])
        self.assertEqual(result["termination_scope"], "process_group_only")

    def test_grok_direct_helper_failure_preserves_specific_reason_and_exit(self):
        grok_home = self.grok_home("grok-home-direct-failure")
        auth_identity = grok_auth_file_identity(grok_home)
        for expected in (
            "grok_http_response_model_mismatch",
            "grok_http_tool_call_forbidden",
            "grok_http_usage_missing",
        ):
            with self.subTest(expected=expected):
                process_exit_confirmed = threading.Event()

                def failed_process(*_args, **kwargs):
                    callback = kwargs.get("on_process_group_exit_confirmed")
                    if callable(callback):
                        callback()
                    return self.grok_direct_process_result(error=expected)

                with patch(
                    "agentfanout.provider_adapters.run_json_process",
                    side_effect=failed_process,
                ):
                    with self.assertRaisesRegex(ProviderAdapterError, expected):
                        launch_grok(
                            binary=self.binary, project_root=self.root,
                            selector="grok-fast", reasoning_effort="low", prompt="safe",
                            output_contract="WorkerResultV1",
                            timeout_seconds=3, grok_home=grok_home,
                            resolved_model="grok-4.5",
                            expected_auth_file_identity=auth_identity,
                            expected_helper_identity=grok_http_helper_identity(),
                            on_process_group_exit_confirmed=process_exit_confirmed.set,
                        )
                self.assertTrue(process_exit_confirmed.is_set())

    def test_sensitive_prompt_and_nonroot_are_rejected(self):
        with self.assertRaisesRegex(ProviderAdapterError, "sensitive"):
            launch_codex(binary=self.binary, project_root=self.root, selector="luna", reasoning_effort="low",
                        prompt="api_key=not-for-workers", timeout_seconds=3)
        from agentfanout.provider_adapters import sanitized_prompt
        self.assertEqual(
            sanitized_prompt("Review how private key handling is documented."),
            "Review how private key handling is documented.",
        )
        with self.assertRaisesRegex(ProviderAdapterError, "sensitive"):
            sanitized_prompt("-----BEGIN " + "PRIVATE KEY-----\nfixture")
        nonroot = Path(self.tmp.name) / "not-a-worktree"
        nonroot.mkdir()
        grok_home = self.grok_home("grok-home-sensitive")
        with self.assertRaisesRegex(ProviderAdapterError, "approved_project_root_not_repo_or_allowlisted"):
            launch_grok(binary=self.binary, project_root=nonroot, selector="grok-fast", reasoning_effort="low",
                        prompt="safe", output_contract="WorkerResultV1", timeout_seconds=3, grok_home=grok_home)

    def test_timeout_cancels_and_heartbeats(self):
        beats = []
        result = run_json_process([*self.binary, "sleep"], "safe", self.root, timeout_seconds=0.1,
                                  heartbeat=beats.append, heartbeat_interval_seconds=0.05)
        self.assertEqual(result["status"], "timeout")
        self.assertGreaterEqual(len(beats), 1)

    def test_explicit_cancellation_stops_child(self):
        result = run_json_process([*self.binary, "sleep"], "safe", self.root, timeout_seconds=3,
                                  cancellation_requested=lambda: True)
        self.assertEqual(result["status"], "cancelled")

    def test_child_environment_is_minimal_and_output_cap_fails_closed(self):
        old = os.environ.get("AGENT_FANOUT_TEST_TOKEN")
        os.environ["AGENT_FANOUT_TEST_TOKEN"] = "not-for-child"
        try:
            result = run_json_process([*self.binary, "flood"], "safe", self.root, timeout_seconds=3,
                                      max_output_bytes=4096)
        finally:
            if old is None:
                os.environ.pop("AGENT_FANOUT_TEST_TOKEN", None)
            else:
                os.environ["AGENT_FANOUT_TEST_TOKEN"] = old
        self.assertEqual(result["status"], "output_limit")
        self.assertTrue(result["output_truncated"])
        self.assertNotIn("AGENT_FANOUT_TEST_TOKEN", result["environment_audit"]["preserved"])
        self.assertIn("AGENT_FANOUT_TEST_TOKEN", result["environment_audit"]["stripped_credential_names"])

    def test_incomplete_or_zero_placeholder_usage_is_unverified(self):
        from agentfanout.provider_adapters import _usage_from_events
        incomplete = _usage_from_events([{"type": "turn.completed", "usage": {"cached_tokens": 1}}])
        self.assertFalse(incomplete["usage_verified"])
        placeholder = _usage_from_events([{"type": "turn.completed", "usage": {
            "input_tokens": 1, "output_tokens": 1, "cost_usd": 0,
            "price_card_version": "fixture", "cost_source": "fixture",
        }}])
        self.assertTrue(placeholder["usage_verified"])
        self.assertFalse(placeholder["cost_verified"])

    def test_default_or_option_like_models_do_not_gain_capability(self):
        with self.assertRaisesRegex(ProviderAdapterError, "packet_capable_model_not_listed"):
            resolve_grok_model("grok-frontier", [{"id": "grok-default", "profiles": []}])
        with self.assertRaisesRegex(ProviderAdapterError, "profile_not_listed"):
            resolve_codex_model("luna", [{"id": "--luna", "profiles": ["luna"]}])

    def test_grok_routes_only_to_live_proven_packet_model(self):
        models = [
            {"id": "grok-composer-2.5-fast", "profiles": []},
            {"id": "grok-4.5", "profiles": ["fast", "balanced", "frontier"]},
        ]
        for selector in ("grok-fast", "grok-balanced", "grok-frontier"):
            with self.subTest(selector=selector):
                self.assertEqual(resolve_grok_model(selector, models), "grok-4.5")
        with patch.dict(
            os.environ,
            {"AGENT_FANOUT_GROK_FAST_MODEL": "grok-composer-2.5-fast"},
        ):
            with self.assertRaisesRegex(
                ProviderAdapterError, "configured_model_packet_contract_unsupported",
            ):
                resolve_grok_model("grok-fast", models)


if __name__ == "__main__":
    unittest.main()
