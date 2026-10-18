import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

from tests.test_local_hosted_adapter import _FakeEndpoint, _evidence


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "agentfanout.py"


FAKE_CODEX = r'''#!__PYTHON__
import json, os, re, sys, uuid, urllib.error, urllib.request
from datetime import datetime, timezone
args = sys.argv[1:]
if args[-2:] == ["features", "list"]:
    disabled = {args[index + 1] for index, value in enumerate(args[:-1]) if value == "--disable"}
    print(f"{'unified_exec':<36} stable             {'false' if 'unified_exec' in disabled else 'true'}")
    print(f"{'fast_mode':<36} stable             true")
elif args == ["debug", "models"]:
    print(json.dumps({"models": [
        {"id": "codex-luna-fixture", "profiles": ["luna"]},
        {"id": "codex-terra-fixture", "profiles": ["terra"]},
        {"id": "codex-sol-fixture", "profiles": ["sol"]},
    ]}))
elif "exec" in args:
    if 'model_provider="agentfanout_probe"' in args:
        provider = next(value for value in args if value.startswith("model_providers.agentfanout_probe="))
        base_url = re.search(r'base_url="([^"]+)"', provider).group(1)
        request = urllib.request.Request(
            base_url + "/responses",
            data=json.dumps({"tools": [], "tool_choice": "none", "model": args[args.index("--model") + 1]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AGENT_FANOUT_CODEX_PROBE_KEY"]}, method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3).read()
        except urllib.error.HTTPError:
            pass
        raise SystemExit(0)
    prompt = sys.stdin.read()
    payload = json.loads(prompt.split("\n", 1)[1])
    if "resume" in args:
        session_id = args[-2]
    else:
        session_seed = payload.get("task_id") or payload.get("worker_result", {}).get("task_id") or "validator"
        session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "agentfanout:" + session_seed))
    print(json.dumps({"type": "thread.started", "thread_id": session_id}))
    if "worker_result" in payload:
        packet = {
            "schema": "ValidatorResultV1", "validator_id": "fixture-validator", "verdict": "pass",
            "evidence_refs": ["fixture"], "material_findings": [], "contradictions": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        partial = not payload.get("parent_task_id")
        packet = {
            "schema": "WorkerResultV1", "result_id": payload["expected_result_id"],
            "task_id": payload["task_id"], "status": "partial" if partial else "success", "summary": "fixture result",
            "evidence": [payload["input_ref"]], "changed_files": [], "commands": [], "exit_codes": [],
            "remaining_work": [{"scope": "finish the same bounded file"}] if partial else [],
            "launch_correlation_id": payload["launch_correlation_id"],
        }
    print(json.dumps({"type": "result", "packet": packet}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 20, "cached_tokens": 0, "output_tokens": 10, "cost_usd": 0.01,
        "price_card_version": "fixture-v1", "cost_source": "fixture"
    }}))
else:
    sys.exit(19)
'''


SLOW_FAKE_CODEX = r'''#!__PYTHON__
import json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path
args = sys.argv[1:]
if args[-2:] == ["features", "list"]:
    disabled = {args[index + 1] for index, value in enumerate(args[:-1]) if value == "--disable"}
    print(f"{'unified_exec':<36} stable             {'false' if 'unified_exec' in disabled else 'true'}")
    print(f"{'fast_mode':<36} stable             true")
elif args == ["debug", "models"]:
    print(json.dumps({"models": [{"id": "codex-luna-fixture", "profiles": ["luna"]}]}))
elif "exec" in args:
    if 'model_provider="agentfanout_probe"' in args:
        provider = next(value for value in args if value.startswith("model_providers.agentfanout_probe="))
        base_url = re.search(r'base_url="([^"]+)"', provider).group(1)
        request = urllib.request.Request(
            base_url + "/responses",
            data=json.dumps({"tools": [], "tool_choice": "none", "model": args[args.index("--model") + 1]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AGENT_FANOUT_CODEX_PROBE_KEY"]}, method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3).read()
        except urllib.error.HTTPError:
            pass
        raise SystemExit(0)
    Path(__PID_FILE__).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
else:
    sys.exit(19)
'''


BROKEN_FAKE_CODEX = r'''#!__PYTHON__
import json, os, re, sys, urllib.error, urllib.request
args = sys.argv[1:]
if args[-2:] == ["features", "list"]:
    disabled = {args[index + 1] for index, value in enumerate(args[:-1]) if value == "--disable"}
    print(f"{'unified_exec':<36} stable             {'false' if 'unified_exec' in disabled else 'true'}")
    print(f"{'fast_mode':<36} stable             true")
elif args == ["debug", "models"]:
    print(json.dumps({"models": [{"id": "codex-luna-fixture", "profiles": ["luna"]}]}))
elif "exec" in args:
    if 'model_provider="agentfanout_probe"' in args:
        provider = next(value for value in args if value.startswith("model_providers.agentfanout_probe="))
        base_url = re.search(r'base_url="([^"]+)"', provider).group(1)
        request = urllib.request.Request(
            base_url + "/responses",
            data=json.dumps({"tools": [], "tool_choice": "none", "model": args[args.index("--model") + 1]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AGENT_FANOUT_CODEX_PROBE_KEY"]}, method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3).read()
        except urllib.error.HTTPError:
            pass
        raise SystemExit(0)
    print("provider returned no structured result")
else:
    sys.exit(19)
'''


FAKE_GROK_ACP = r'''#!__PYTHON__
import json, sys, uuid

args = sys.argv[1:]
if args == ["models"]:
    print("Available models:\n  - grok-composer-2.5-fast\n  - grok-4.5")
    raise SystemExit(0)

model = args[args.index("--model") + 1]
effort = args[args.index("--reasoning-effort") + 1]
session = str(uuid.uuid4())
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{
            "protocolVersion":1,"agentCapabilities":{},"authMethods":[],"_meta":{
                "modelState":{"currentModelId":model,"availableModels":[{"modelId":model,"_meta":{
                    "supportsReasoningEffort":True,"reasoningEffort":effort,
                    "reasoningEfforts":[{"id":effort,"value":effort}]
                }}]},"mcpServers":[]
            }
        }}), flush=True)
    elif method == "session/new":
        params = request["params"]
        if params.get("mcpServers") != [] or params.get("additionalDirectories") != []:
            raise SystemExit(71)
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{
            "sessionId":session,"_meta":{"currentModelId":model}
        }}), flush=True)
    elif method == "session/prompt":
        prompt = request["params"]["prompt"][0]["text"]
        packet_input = json.loads(prompt.rsplit("\n", 1)[-1])
        if "ValidatorResultV1" in prompt:
            task = packet_input["task"]
            packet = {
                "schema":"ValidatorResultV1","validator_id":"fake-grok-validator",
                "verdict":"pass","evidence_refs":[task["input_ref"]],
                "material_findings":[],"contradictions":[],
                "checked_at":"2026-07-10T00:00:00Z"
            }
        else:
            partial = not packet_input.get("parent_task_id")
            packet = {
                "schema":"WorkerResultV1","result_id":packet_input["expected_result_id"],
                "task_id":packet_input["task_id"],"status":"partial" if partial else "success",
                "summary":"The bounded fixture says alpha.",
                "evidence":[packet_input["input_ref"] + ": alpha"],
                "changed_files":[],"commands":[],"exit_codes":[],
                "remaining_work":[{"scope":"finish the same bounded file"}] if partial else [],
                "launch_correlation_id":packet_input["launch_correlation_id"]
            }
        rendered = json.dumps(packet)
        midpoint = len(rendered) // 2
        for fragment in (rendered[:midpoint], rendered[midpoint:]):
            print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{
                "sessionId":session,"update":{"sessionUpdate":"agent_message_chunk","content":{
                    "type":"text","text":fragment
                }}
            }}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{
            "stopReason":"end_turn","_meta":{"totalTokens":31}
        }}), flush=True)
        break
'''


class AtomicCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / "target-project"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        (self.project / "one.md").write_text("one\n", encoding="utf-8")
        (self.project / "two.md").write_text("two\n", encoding="utf-8")
        self.unrelated = base / "unrelated"
        self.unrelated.mkdir()
        (self.unrelated / ".git").mkdir()
        self.state_dir = base / "state"
        self.fake = base / "fake-codex"
        self.fake.write_text(FAKE_CODEX.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.unsafe_codex_worker_marker = base / "unsafe-codex-worker-started"
        unsafe_codex_source = FAKE_CODEX.replace("__PYTHON__", sys.executable).replace(
            '{"tools": [], "tool_choice": "none", "model": args[args.index("--model") + 1]}',
            '{"tools": [{"type": "function", "name": "update_plan"}], '
            '"tool_choice": "auto", "model": args[args.index("--model") + 1]}',
        ).replace(
            "    prompt = sys.stdin.read()",
            f"    open({str(self.unsafe_codex_worker_marker)!r}, 'w').write('started')\n"
            "    prompt = sys.stdin.read()",
        )
        self.unsafe_codex_fake = base / "unsafe-transport-codex"
        self.unsafe_codex_fake.write_text(unsafe_codex_source, encoding="utf-8")
        self.unsafe_codex_fake.chmod(self.unsafe_codex_fake.stat().st_mode | stat.S_IXUSR)
        self.bad_model_fake = base / "fake-codex-no-matching-model"
        self.bad_model_fake.write_text(
            FAKE_CODEX.replace("__PYTHON__", sys.executable).replace(
                '"codex-luna-fixture", "profiles": ["luna"]',
                '"codex-unmatched-fixture", "profiles": ["unmatched"]',
            ),
            encoding="utf-8",
        )
        self.bad_model_fake.chmod(self.bad_model_fake.stat().st_mode | stat.S_IXUSR)
        self.broken_fake = base / "broken-fake-codex"
        self.broken_fake.write_text(BROKEN_FAKE_CODEX.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.broken_fake.chmod(self.broken_fake.stat().st_mode | stat.S_IXUSR)
        self.marker_fake = base / "marker-fake-codex"
        self.marker_fake.write_text(
            FAKE_CODEX.replace("__PYTHON__", sys.executable).replace(
                "import json, sys, uuid", "import json, sys, uuid\nfrom pathlib import Path\nPath(__file__).with_suffix('.invoked').write_text('invoked')",
            ),
            encoding="utf-8",
        )
        self.marker_fake.chmod(self.marker_fake.stat().st_mode | stat.S_IXUSR)
        self.grok_home = base / "grok-home"
        self.grok_home.mkdir(mode=0o700)
        self.grok_home.chmod(0o700)
        grok_auth = self.grok_home / "auth.json"
        grok_auth.write_text(json.dumps({
            "schema": "GrokApiCredentialV1",
            "api_key": "fixture-api-key-" + "x" * 64,
        }) + "\n", encoding="utf-8")
        grok_auth.chmod(0o600)
        self.grok_fake = base / "fake-grok"
        self.grok_fake.write_text(
            FAKE_GROK_ACP.replace("__PYTHON__", sys.executable), encoding="utf-8",
        )
        self.grok_fake.chmod(self.grok_fake.stat().st_mode | stat.S_IXUSR)
        self.broken_grok_fake = base / "broken-fake-grok"
        self.broken_grok_fake.write_text(
            FAKE_GROK_ACP.replace("__PYTHON__", sys.executable).replace(
                '    elif method == "session/prompt":\n        prompt = request["params"]["prompt"][0]["text"]',
                '    elif method == "session/prompt":\n        raise SystemExit(79)',
            ),
            encoding="utf-8",
        )
        self.broken_grok_fake.chmod(self.broken_grok_fake.stat().st_mode | stat.S_IXUSR)
        self.marker_grok_fake = base / "marker-fake-grok"
        self.marker_grok_fake.write_text(
            FAKE_GROK_ACP.replace("__PYTHON__", sys.executable).replace(
                "import json, sys, uuid",
                "import json, sys, uuid\nfrom pathlib import Path\nPath(__file__).with_suffix('.invoked').write_text('invoked')",
            ),
            encoding="utf-8",
        )
        self.marker_grok_fake.chmod(
            self.marker_grok_fake.stat().st_mode | stat.S_IXUSR
        )
        self.guard_fixture_index = 0

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args, env_updates=None):
        env = os.environ.copy()
        env.pop("AGENT_FANOUT_PROJECT_ROOT", None)
        env.pop("AGENT_FANOUT_CODEX_FAST_MODEL", None)
        for name in (
            "AGENT_FANOUT_GROK_FAST_MODEL", "AGENT_FANOUT_GROK_BALANCED_MODEL",
            "AGENT_FANOUT_GROK_FRONTIER_MODEL", "AGENT_FANOUT_GROK_AVAILABLE",
            "AGENT_FANOUT_GROK_HOME", "AGENT_FANOUT_LOCAL_AVAILABLE",
            "AGENT_FANOUT_LOCAL_HOSTED_NO_TOOL", "AGENT_FANOUT_LOCAL_ENDPOINT",
            "AGENT_FANOUT_LOCAL_MODEL_ID", "AGENT_FANOUT_LOCAL_MODEL_ALIAS",
            "AGENT_FANOUT_LOCAL_REQUIRED_MODE", "AGENT_FANOUT_LOCAL_ALLOW_LOOPBACK_HTTP",
            "AGENT_FANOUT_LOCAL_ATTESTATION_JSON",
            "AGENT_FANOUT_LOCAL_BRIDGE_OBSERVATION_JSON",
        ):
            env.pop(name, None)
        for name, value in (env_updates or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = str(value)
        process = subprocess.run(
            [sys.executable, "-S", str(CLI), *map(str, args)], cwd=self.unrelated,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30,
        )
        return process, json.loads(process.stdout)

    def test_doctor_falls_back_when_default_xdg_ancestor_is_group_writable(self):
        home = Path(self.tmp.name) / "portable-home"
        home.mkdir(mode=0o700)
        local = home / ".local"
        local.mkdir(mode=0o775)
        local.chmod(0o775)
        process, result = self.invoke(
            "doctor", "--project-root", self.project,
            *self.grok_arguments(),
            env_updates={"HOME": home, "XDG_STATE_HOME": None},
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertTrue(result["ready"])
        self.assertEqual(
            result["checks"]["state_directory"]["path"],
            str(home / ".agentfanout" / "state"),
        )

    def test_doctor_rejects_explicit_xdg_state_home_with_writable_ancestor(self):
        home = Path(self.tmp.name) / "explicit-xdg-home"
        home.mkdir(mode=0o700)
        unsafe = home / "shared"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        process, result = self.invoke(
            "doctor", "--project-root", self.project, "--provider", "Codex",
            "--codex-binary", self.fake,
            env_updates={"HOME": home, "XDG_STATE_HOME": unsafe / "state"},
        )
        self.assertEqual(process.returncode, 3, process.stderr + process.stdout)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "private_directory_ancestor_permissions_unsafe")

    def test_codex_doctor_reports_nonempty_transport_tools_as_incompatible(self):
        process, result = self.invoke(
            "doctor", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Codex", "--codex-binary", self.unsafe_codex_fake,
        )
        self.assertEqual(process.returncode, 2, process.stderr + process.stdout)
        self.assertFalse(result["ready"])
        compatibility = result["checks"]["provider_compatibility"]
        self.assertFalse(compatibility["passed"])
        self.assertIn(
            "codex_native_no_tool_transport_unavailable:update_plan",
            compatibility["reason"],
        )
        self.assertFalse(self.unsafe_codex_worker_marker.exists())

    def local_arguments(self, endpoint, *, include_provider=True):
        evidence_dir = Path(self.tmp.name) / "local-evidence"
        evidence_dir.mkdir(mode=0o700, exist_ok=True)
        evidence_dir.chmod(0o700)
        attestation, bridge = _evidence(endpoint)
        attestation_file = evidence_dir / "attestation.json"
        bridge_file = evidence_dir / "bridge.json"
        attestation_file.write_text(json.dumps(attestation) + "\n", encoding="utf-8")
        bridge_file.write_text(json.dumps(bridge) + "\n", encoding="utf-8")
        attestation_file.chmod(0o600)
        bridge_file.chmod(0o600)
        arguments = [
            "--local-endpoint", endpoint,
            "--local-model", "fixture-local-model", "--local-selector", "local-worker",
            "--local-mode", "verified-local-mode", "--local-allow-loopback-http",
            "--local-attestation-file", attestation_file,
            "--local-bridge-file", bridge_file,
        ]
        if include_provider:
            arguments[:0] = ["--provider", "Local"]
        return arguments, attestation_file, bridge_file

    def grok_arguments(self, *, binary=None, home=None, model="grok-4.5", include_provider=True):
        arguments = [
            "--grok-binary", binary or self.grok_fake,
            "--grok-home", home or self.grok_home,
            "--grok-model", model,
        ]
        if include_provider:
            arguments[:0] = ["--provider", "Grok"]
        return arguments

    def guard_fixture(self, *, discovery_binary=None, helper_identity_override=None):
        from agentfanout.harness import create_session_guard
        from agentfanout.lifecycle import initialize_run, save_run_state
        from agentfanout.project_binding import resolve_project_binding
        from agentfanout.provider_adapters import (
            executable_identity, grok_auth_file_identity,
            grok_http_helper_identity,
        )

        self.guard_fixture_index += 1
        fixture_dir = Path(self.tmp.name) / f"recovery-{self.guard_fixture_index}"
        fixture_dir.mkdir(mode=0o700)
        state_path = fixture_dir / "run.json"
        ledger_path = fixture_dir / "host.sqlite3"
        guard_path = fixture_dir / "guard.json"
        binding = resolve_project_binding(explicit_root=self.project)
        route_env = os.environ.copy()
        route_env["AGENT_FANOUT_PROJECT_ROOT"] = binding["resolved_root"]
        route_env["AGENT_FANOUT_GROK_AVAILABLE"] = "1"
        routed = subprocess.run(
            [
                sys.executable, "-S", str(ROOT / "scripts" / "route.py"),
                "--project-root", str(self.project),
                "i accept orchestration overhead. use Grok for this task: review one.md",
            ],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=route_env, check=True,
        )
        route = json.loads(routed.stdout)
        self.assertEqual(route["dispatch"]["status"], "launch_required")
        tasks = route["task_units"]
        state = initialize_run("review one.md", tasks)
        state["project_binding"] = binding
        save_run_state(state_path, state)
        manifest = json.loads(
            (ROOT / "integrations" / "codex" / "standalone-runner-manifest.json").read_text(encoding="utf-8")
        )
        home_info = self.grok_home.stat()
        auth_identity = grok_auth_file_identity(self.grok_home)
        helper_identity = helper_identity_override or grok_http_helper_identity()
        boundary = {
            "provider": "Grok", "grok_home_realpath": str(self.grok_home.resolve()),
            "grok_home_device": home_info.st_dev, "grok_home_inode": home_info.st_ino,
            "grok_auth_file_identity": auth_identity,
            "grok_http_helper_identity": helper_identity,
        }
        runtime_config = {
            "grok_home_path_sha256": hashlib.sha256(str(self.grok_home.resolve()).encode()).hexdigest(),
            "grok_home_device": home_info.st_dev, "grok_home_inode": home_info.st_ino,
            "grok_model_override": "grok-4.5",
            "grok_auth_file_identity": auth_identity,
            "grok_http_helper_identity": helper_identity,
        }
        guard = create_session_guard(
            binding,
            manifest,
            run_id=state["run_id"],
            run_state_path=str(state_path.resolve()),
            ledger_path=str(ledger_path.resolve()),
            provider="Grok",
            model_selector="grok-fast",
            resolved_model="grok-4.5",
            provider_executable_identity=executable_identity(str(discovery_binary or self.grok_fake)),
            max_workers=2,
            provider_boundary_sha256=hashlib.sha256(json.dumps(
                boundary, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            ).encode()).hexdigest(),
            provider_runtime_config=runtime_config,
        )
        descriptor = os.open(guard_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(guard, handle)
            handle.write("\n")
        return guard_path, state_path, ledger_path, guard, state

    def test_raw_runner_rejects_manifest_digest_mismatch_before_provider_setup(self):
        _guard_path, state_path, ledger_path, _guard, _state = self.guard_fixture()
        process = subprocess.run(
            [
                sys.executable, "-S", str(ROOT / "scripts" / "run-hosted-fanout.py"),
                "--state-file", str(state_path), "--ledger", str(ledger_path),
                "--project-root", str(self.project), "--provider", "Grok",
                "--expected-host-manifest-sha256", "0" * 64, "--compact",
            ],
            cwd=self.unrelated, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(process.returncode, 3, process.stderr + process.stdout)
        result = json.loads(process.stdout)
        self.assertIn("host_manifest_binding_mismatch", result["block_reason"])

    def test_atomic_parent_passes_and_verifies_guard_manifest_digest(self):
        _guard_path, state_path, ledger_path, guard, _state = self.guard_fixture()
        spec = importlib.util.spec_from_file_location("agentfanout_atomic_manifest_test", CLI)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        mismatch = "0" * 64
        with patch.object(
            module, "_run_json",
            return_value=(0, {"enforcement": {"host_manifest_sha256": mismatch}}),
        ) as run_json:
            code, outcome = module._run_bound_runner(
                guard, state_path, ledger_path, str(self.project),
                {
                    "binary": self.grok_fake, "grok_home": self.grok_home,
                    "grok_auth_file_identity": guard["provider_runtime_config"]["grok_auth_file_identity"],
                    "grok_http_helper_identity": guard["provider_runtime_config"]["grok_http_helper_identity"],
                },
                1, os.environ.copy(), 9,
            )
        self.assertEqual(code, 4)
        self.assertEqual(outcome["error"], "runner_host_manifest_binding_mismatch")
        arguments = run_json.call_args.args[0]
        digest_index = arguments.index("--expected-host-manifest-sha256") + 1
        self.assertEqual(arguments[digest_index], guard["host_manifest_sha256"])

    def test_atomic_run_from_unrelated_cwd_completes(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--pretty", "--", "review one.md and two.md",
            )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["project_binding"]["resolved_root"], str(self.project.resolve()))
        self.assertEqual(result["project_binding"]["cwd_relation"], "outside")
        self.assertEqual(result["enforcement"]["level"], "runner_enforced")
        self.assertFalse(result["enforcement"]["fallback_allowed"])
        self.assertEqual(result["lifecycle"]["wave_index"], 1)
        self.assertTrue(Path(result["artifacts"]["state_file"]).is_file())

    def test_explicit_codex_transport_rejection_precedes_all_run_artifacts(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Codex", "--codex-binary", self.unsafe_codex_fake,
            "--accept-overhead", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 2, process.stderr + process.stdout)
        self.assertEqual(result["status"], "fanout_blocked")
        self.assertIn(
            "codex_native_no_tool_transport_unavailable:update_plan",
            result["block_reason"],
        )
        self.assertEqual(
            result["artifacts"],
            {"guard_file": None, "state_file": None, "ledger_file": None},
        )
        self.assertFalse(self.unsafe_codex_worker_marker.exists())
        self.assertFalse((self.state_dir / "host.sqlite3").exists())

    def test_auto_selects_configured_compatible_local_without_parent_fallback(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(
                endpoint.endpoint, include_provider=False,
            )
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                "--codex-binary", self.unsafe_codex_fake,
                *local_args, "--accept-overhead", "--", "review one.md",
            )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["route"]["provider"], "Local")
        self.assertFalse(result["enforcement"]["fallback_allowed"])
        self.assertFalse(self.unsafe_codex_worker_marker.exists())

    def test_relative_grok_binary_is_discovered_from_invocation_cwd(self):
        relative_fake = self.unrelated / "fake-grok"
        relative_fake.write_text(FAKE_GROK_ACP.replace("__PYTHON__", sys.executable), encoding="utf-8")
        relative_fake.chmod(relative_fake.stat().st_mode | stat.S_IXUSR)
        process, result = self.invoke(
            "doctor", "--project-root", self.project, "--state-dir", self.state_dir,
            *self.grok_arguments(binary="./fake-grok"),
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertTrue(result["ready"])
        self.assertTrue(result["checks"]["provider_models"]["passed"])

    def test_high_grok_security_review_of_named_files_plans_without_provider_io(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            *self.grok_arguments(model="grok-4.5"), "--accept-overhead", "--dry-run", "--",
            "perform a security review of one.md and two.md with high reasoning",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["route"]["task_class"], "security-review")
        self.assertEqual(result["route"]["planned_workers"], 2)
        self.assertEqual(result["provider_processes_started"], 0)

    def test_generic_inputs_block_before_run_state_or_ledger(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Codex", "--codex-binary", self.fake,
            "--accept-overhead", "--", "review four independent modules",
        )
        self.assertEqual(process.returncode, 3)
        self.assertEqual(result["status"], "binding_required")
        self.assertEqual(result["block_reason"], "adapter_input_binding_required")
        self.assertIsNone(result["artifacts"]["state_file"])
        self.assertIsNone(result["artifacts"]["ledger_file"])

    def test_anti_waste_direct_recommendation_is_pre_admission_not_fallback(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Codex", "--codex-binary", self.fake,
            "--dry-run", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "planned")
        self.assertTrue(result["direct_execution_recommended"])
        self.assertTrue(result["route"]["direct_execution_authorized"])
        self.assertEqual(result["route"]["decision_stage"], "pre_admission")
        self.assertFalse(result["worker_failure_fallback_allowed"])
        self.assertTrue(result["fallback_allowed"])
        self.assertEqual(result["provider_processes_started"], 0)

    def test_missing_grok_binary_is_provider_block_not_configuration_binding(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Grok", "--grok-binary", self.project / "missing-grok",
            "--grok-home", self.grok_home, "--grok-model", "grok-4.5",
            "--accept-overhead", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 2, process.stderr + process.stdout)
        self.assertEqual(result["status"], "fanout_blocked")
        self.assertEqual(result["block_reason"], "provider_binary_unavailable:grok")
        self.assertFalse(result["enforcement"]["fallback_allowed"])
        self.assertFalse(self.state_dir.exists())

    def test_wrong_environment_root_fails_before_state(self):
        env = os.environ.copy()
        env["AGENT_FANOUT_PROJECT_ROOT"] = str(self.unrelated)
        process = subprocess.run(
            [sys.executable, "-S", str(CLI), "run", "--project-root", str(self.project),
             "--state-dir", str(self.state_dir), "--codex-binary", str(self.fake), "--", "review one.md"],
            cwd=self.unrelated, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 3)
        self.assertEqual(result["status"], "binding_required")
        self.assertIn("project_root_not_host_approved", result["block_reason"])
        self.assertFalse(self.state_dir.exists())

    def test_missing_root_returns_bounded_machine_json(self):
        process, result = self.invoke(
            "run", "--provider", "Local", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 3, process.stderr + process.stdout)
        self.assertEqual(result["status"], "binding_required")
        self.assertEqual(result["block_reason"], "approved_project_root_missing")

    def test_explicit_worktree_discovery_from_nested_directory(self):
        nested = self.project / "src" / "nested"
        nested.mkdir(parents=True)
        env = os.environ.copy()
        env.pop("AGENT_FANOUT_PROJECT_ROOT", None)
        process = subprocess.run(
            [
                sys.executable, "-S", str(CLI), "run",
                "--discover-current-worktree", "--provider", "Codex",
                "--codex-binary", str(self.marker_fake), "--accept-overhead",
                "--dry-run", "--", "review one.md",
            ],
            cwd=nested, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=30,
        )
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["project_binding"]["resolved_root"], str(self.project.resolve()))
        self.assertEqual(result["project_binding"]["source"], "explicit_worktree_discovery")

    def test_worktree_discovery_rejects_conflicting_host_root(self):
        nested = self.project / "nested"
        nested.mkdir()
        env = os.environ.copy()
        env["AGENT_FANOUT_PROJECT_ROOT"] = str(self.unrelated)
        process = subprocess.run(
            [
                sys.executable, "-S", str(CLI), "run",
                "--discover-current-worktree", "--provider", "Local",
                "--", "review one.md",
            ],
            cwd=nested, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=30,
        )
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 3, process.stderr + process.stdout)
        self.assertEqual(result["block_reason"], "project_root_not_host_approved")

    def test_insecure_existing_state_directory_is_rejected_without_chmod(self):
        self.state_dir.mkdir(mode=0o755)
        self.state_dir.chmod(0o755)
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            *self.grok_arguments(), "--accept-overhead", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 3)
        self.assertEqual(result["status"], "binding_required")
        self.assertIn("private_directory_permissions_unsafe", result["block_reason"])
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o755)

    def test_state_artifacts_inside_project_are_denied(self):
        # Keep the project ancestor safe under group-writable host umasks so
        # this case reaches the in-project artifact boundary.
        self.project.chmod(0o755)
        inside = self.project / ".agentfanout-state"
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", inside,
            *self.grok_arguments(), "--accept-overhead", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 4)
        self.assertEqual(result["block_reason"], "state_artifact_inside_project_denied")
        self.assertEqual(list(inside.iterdir()), [])

    def test_model_preflight_blocks_before_run_state_or_ledger(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Codex", "--codex-binary", self.bad_model_fake,
            "--accept-overhead", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("provider_model_preflight_failed", result["block_reason"])
        self.assertIsNone(result["artifacts"]["state_file"])
        self.assertIsNone(result["artifacts"]["ledger_file"])
        self.assertFalse((self.state_dir / "host.sqlite3").exists())

    def test_dry_run_projects_cost_without_state_guard_ledger_or_provider_launch(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Codex", "--codex-binary", self.marker_fake,
            "--accept-overhead", "--dry-run", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["schema"], "AgentFanoutPlanV1")
        self.assertFalse(result["ready_to_launch"])
        self.assertEqual(result["provider_launches_started"], 0)
        self.assertEqual(result["provider_processes_started"], 0)
        self.assertFalse(result["model_discovery_probe_started"])
        self.assertTrue(result["provider_preflight"]["model_discovery_required_before_launch"])
        self.assertEqual(result["artifacts"], {"guard_file": None, "state_file": None, "ledger_file": None})
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.marker_fake.with_suffix(".invoked").exists())

    def test_dry_run_with_implicit_unsafe_xdg_creates_no_fallback_state(self):
        home = Path(self.tmp.name) / "dry-run-home"
        home.mkdir(mode=0o700)
        local = home / ".local"
        local.mkdir(mode=0o775)
        local.chmod(0o775)
        process, result = self.invoke(
            "run", "--project-root", self.project, "--codex-binary", self.marker_fake,
            "--accept-overhead", "--dry-run", "--", "review one.md",
            env_updates={"HOME": home, "XDG_STATE_HOME": None},
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "planned")
        self.assertFalse((home / ".agentfanout").exists())
        self.assertFalse((home / ".local" / "state").exists())
        self.assertFalse(self.marker_fake.with_suffix(".invoked").exists())

    def test_grok_doctor_discovers_and_freezes_fast_model(self):
        process, result = self.invoke(
            "doctor", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Grok", "--grok-binary", self.grok_fake,
            "--grok-home", self.grok_home,
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertTrue(result["ready"])
        self.assertEqual(result["provider"], "Grok")
        self.assertEqual(
            result["checks"]["provider_models"]["resolved_model"],
            "grok-4.5",
        )

    def test_atomic_grok_plan_exposes_no_isolated_home_or_artifacts(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Grok", "--grok-binary", self.grok_fake,
            "--grok-home", self.grok_home, "--accept-overhead", "--dry-run", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["route"]["provider"], "Grok")
        self.assertEqual(result["provider_processes_started"], 0)
        self.assertIsNone(result["artifacts"]["guard_file"])
        self.assertNotIn(str(self.grok_home), json.dumps(result, sort_keys=True))

    def test_grok_doctor_exact_model_override_is_frozen(self):
        process, result = self.invoke(
            "doctor", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Grok", "--grok-binary", self.grok_fake,
            "--grok-home", self.grok_home, "--grok-model", "grok-4.5",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertTrue(result["ready"])
        self.assertEqual(result["checks"]["provider_models"]["resolved_model"], "grok-4.5")

    def test_grok_doctor_rejects_selector_model_effort_mismatch(self):
        process, result = self.invoke(
            "doctor", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Grok", "--selector", "grok-balanced",
            "--grok-binary", self.grok_fake, "--grok-home", self.grok_home,
            "--grok-model", "grok-composer-2.5-fast",
        )
        self.assertEqual(process.returncode, 2, process.stderr + process.stdout)
        self.assertFalse(result["ready"])
        self.assertIn(
            "grok_model_packet_contract_unsupported",
            result["checks"]["provider_models"]["reason"],
        )

    def test_atomic_interrupted_run_resumes_with_same_boundary(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--max-cycles", "1", "--", "review one.md",
            )
            self.assertEqual(process.returncode, 4, process.stderr + process.stdout)
            self.assertEqual(result["session_guard"]["status"], "blocked")
            resumed, recovery = self.invoke(
                "resume", "--project-root", self.project,
                "--guard-file", result["artifacts"]["guard_file"], *local_args[2:],
            )
        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        self.assertEqual(recovery["status"], "complete")
        self.assertEqual(recovery["session_guard"]["status"], "released")

    def test_atomic_grok_resume_rejects_isolated_home_replacement(self):
        guard_path, _state_path, _ledger_path, _guard, _state = self.guard_fixture()
        prior_home = self.grok_home.with_name("grok-home-prior")
        self.grok_home.rename(prior_home)
        self.grok_home.mkdir(mode=0o700)
        self.grok_home.chmod(0o700)
        replacement_auth = self.grok_home / "auth.json"
        replacement_auth.write_text(json.dumps({
            "https://auth.x.ai::replacement": {
                "key": "replacement-token-" + "y" * 64,
                "expires_at": "2099-01-01T00:00:00Z",
            },
        }) + "\n", encoding="utf-8")
        replacement_auth.chmod(0o600)
        resumed, recovery = self.invoke(
            "resume", "--project-root", self.project,
            "--guard-file", guard_path,
            "--grok-binary", self.grok_fake, "--grok-home", self.grok_home,
            "--grok-model", "grok-4.5",
        )
        self.assertEqual(resumed.returncode, 4, resumed.stderr + resumed.stdout)
        self.assertIn("session_guard_provider_boundary_changed", recovery["block_reason"])

    def test_atomic_grok_resume_rejects_auth_file_replacement(self):
        guard_path, _state_path, _ledger_path, _guard, _state = self.guard_fixture()
        auth_file = self.grok_home / "auth.json"
        auth_file.unlink()
        auth_file.write_text(json.dumps({
            "https://auth.x.ai::different": {
                "key": "different-token-" + "z" * 64,
                "expires_at": "2099-01-01T00:00:00Z",
            },
        }) + "\n", encoding="utf-8")
        auth_file.chmod(0o600)
        resumed, recovery = self.invoke(
            "resume", "--project-root", self.project,
            "--guard-file", guard_path,
            "--grok-binary", self.grok_fake, "--grok-home", self.grok_home,
            "--grok-model", "grok-4.5",
        )
        self.assertEqual(resumed.returncode, 4, resumed.stderr + resumed.stdout)
        self.assertIn("session_guard_provider_boundary_changed", recovery["block_reason"])

    def test_atomic_grok_resume_rejects_helper_drift_before_discovery_executes(self):
        from agentfanout.provider_adapters import grok_http_helper_identity

        drifted = json.loads(json.dumps(grok_http_helper_identity()))
        self.assertTrue(drifted["argument_file_identities"])
        drifted["argument_file_identities"][-1]["sha256"] = "f" * 64
        marker = self.marker_grok_fake.with_suffix(".invoked")
        marker.unlink(missing_ok=True)
        guard_path, _state_path, _ledger_path, _guard, _state = self.guard_fixture(
            discovery_binary=self.marker_grok_fake,
            helper_identity_override=drifted,
        )
        resumed, recovery = self.invoke(
            "resume", "--project-root", self.project,
            "--guard-file", guard_path,
            "--grok-binary", self.marker_grok_fake,
            "--grok-home", self.grok_home,
            "--grok-model", "grok-4.5",
        )
        self.assertEqual(resumed.returncode, 4, resumed.stderr + resumed.stdout)
        self.assertIn("session_guard_provider_boundary_changed", recovery["block_reason"])
        self.assertFalse(marker.exists())

    def test_atomic_grok_dry_run_starts_no_provider_or_discovery_process(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Grok", "--grok-binary", self.marker_grok_fake,
            "--grok-home", self.grok_home, "--accept-overhead", "--dry-run", "--",
            "review one.md",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["provider_processes_started"], 0)
        self.assertFalse(result["model_discovery_probe_started"])
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.marker_grok_fake.with_suffix(".invoked").exists())

    def test_atomic_local_run_completes_without_exposing_endpoint(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--", "review one.md",
            )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "complete")
        guard = result["session_guard"]
        self.assertEqual(guard["provider"], "Local")
        self.assertEqual(guard["resolved_model"], "fixture-local-model")
        self.assertEqual(guard["provider_runtime_config"]["endpoint_policy"], "numeric_loopback_http")
        self.assertNotIn(endpoint.endpoint, json.dumps(result, sort_keys=True))

    def test_atomic_local_interrupted_run_resumes_with_same_evidence(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--max-cycles", "1", "--", "review one.md",
            )
            self.assertEqual(process.returncode, 4, process.stderr + process.stdout)
            resume_args = local_args[2:]
            resumed, recovery = self.invoke(
                "resume", "--project-root", self.project,
                "--guard-file", result["artifacts"]["guard_file"], *resume_args,
            )
        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        self.assertEqual(recovery["status"], "complete")
        self.assertEqual(recovery["session_guard"]["status"], "released")

    def test_atomic_local_resume_rejects_fresh_evidence_change(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--max-cycles", "1", "--", "review one.md",
            )
            self.assertEqual(process.returncode, 4, process.stderr + process.stdout)
            bridge = json.loads(bridge_file.read_text(encoding="utf-8"))
            bridge["source"] = "changed-fresh-host-probe"
            bridge_file.write_text(json.dumps(bridge) + "\n", encoding="utf-8")
            bridge_file.chmod(0o600)
            resume_args = local_args[2:]
            resumed, recovery = self.invoke(
                "resume", "--project-root", self.project,
                "--guard-file", result["artifacts"]["guard_file"], *resume_args,
            )
        self.assertEqual(resumed.returncode, 4, resumed.stderr + resumed.stdout)
        self.assertIn("session_guard_provider_boundary_changed", recovery["block_reason"])

    def test_atomic_local_dry_run_makes_no_endpoint_request(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--dry-run", "--", "review one.md",
            )
            requests = list(endpoint.requests)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertTrue(result["ready_to_launch"])
        self.assertEqual(result["provider_processes_started"], 0)
        self.assertFalse(result["model_discovery_probe_started"])
        self.assertEqual(requests, [])
        self.assertFalse(self.state_dir.exists())

    def test_explicit_provider_does_not_accept_overhead_without_separate_flag(self):
        process, result = self.invoke(
            "run", "--project-root", self.project, "--state-dir", self.state_dir,
            "--provider", "Codex", "--codex-binary", self.fake, "--dry-run", "--", "review one.md",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertFalse(result["overhead_accepted"])
        self.assertFalse(result["ready_to_launch"])
        self.assertEqual(
            result["route"]["anti_waste_guard"]["decision"],
            "suppress_to_main_session",
        )
        self.assertFalse(self.state_dir.exists())

    def test_post_arm_runner_failure_marks_guard_blocked(self):
        with _FakeEndpoint(mode="missing_usage") as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--", "review one.md",
            )
        self.assertEqual(process.returncode, 4, process.stderr + process.stdout)
        self.assertIn(result["status"], {"blocked", "fanout_blocked"})
        self.assertEqual(result["session_guard"]["status"], "blocked")
        persisted = json.loads(Path(result["artifacts"]["guard_file"]).read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "blocked")
        self.assertEqual(persisted["run_id"], result["session_guard"]["run_id"])

    def test_cycle_budget_block_can_resume_without_replaying_completed_wave(self):
        with _FakeEndpoint() as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            process, result = self.invoke(
                "run", "--project-root", self.project, "--state-dir", self.state_dir,
                *local_args, "--accept-overhead", "--max-cycles", "1", "--", "review one.md",
            )
            self.assertEqual(process.returncode, 4, process.stderr + process.stdout)
            self.assertEqual(result["session_guard"]["status"], "blocked")
            guard_file = result["artifacts"]["guard_file"]
            resumed, recovery = self.invoke(
                "resume", "--project-root", self.project, "--guard-file", guard_file,
                *local_args[2:],
            )
        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        self.assertEqual(recovery["status"], "complete")
        self.assertEqual(recovery["session_guard"]["status"], "released")
        self.assertFalse(recovery["enforcement"]["fallback_allowed"])

    def test_armed_crash_guard_is_inspectable_before_provider_launch(self):
        guard_path, _state_path, _ledger_path, _guard, _state = self.guard_fixture()
        status_process, status = self.invoke("status", "--guard-file", guard_path)
        self.assertEqual(status_process.returncode, 0, status_process.stderr + status_process.stdout)
        self.assertEqual(status["session_guard"]["status"], "armed")
        self.assertTrue(status["recovery"]["resume_allowed"])
        self.assertTrue(status["recovery"]["abandon_allowed"])
        self.assertFalse(status["recovery"]["unknown_external_effects_present"])
        self.assertFalse(status["recovery"]["parent_fallback_allowed"])

    def test_abandon_allowed_only_before_launch_without_unknown_effects(self):
        guard_path, _state_path, _ledger_path, _guard, _state = self.guard_fixture()
        process, result = self.invoke(
            "abandon", "--project-root", self.project, "--guard-file", guard_path,
            "--reason", "operator intentionally stopped prelaunch recovery fixture",
        )
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(result["session_guard"]["status"], "abandoned")
        self.assertFalse(result["enforcement"]["fallback_allowed"])

    def test_abandon_denied_for_active_unknown_effect_and_unmatched_validator(self):
        from agentfanout.host_runtime import HostLedger

        cases = [
            ("launch_receipt", {"launch_correlation_id": "launch_unknown_fixture"}),
            ("effect_record", {"effect_id": "effect_fixture", "state": "pending"}),
            ("worker_process_group_exit_unconfirmed", {"launch_correlation_id": "launch_unknown_fixture"}),
            ("validator_attempt_started", {"validator_launch_correlation_id": "validator_unknown_fixture"}),
        ]
        for event_type, payload in cases:
            with self.subTest(event_type=event_type):
                guard_path, _state_path, ledger_path, guard, _state = self.guard_fixture()
                ledger = HostLedger(ledger_path)
                try:
                    ledger.append(guard["run_id"], event_type, payload)
                finally:
                    ledger.close()
                process, result = self.invoke(
                    "abandon", "--project-root", self.project, "--guard-file", guard_path,
                    "--reason", "must be denied",
                )
                self.assertEqual(process.returncode, 4, process.stderr + process.stdout)
                self.assertEqual(result["status"], "fanout_blocked")
                self.assertIn("abandon_unknown_external_effect", result["block_reason"])
                persisted = json.loads(guard_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["status"], "blocked")

    def test_resume_rejects_guard_binding_tamper(self):
        guard_path, _state_path, _ledger_path, guard, _state = self.guard_fixture()
        guard["resolved_model"] = "different-model"
        guard_path.write_text(json.dumps(guard) + "\n", encoding="utf-8")
        process, result = self.invoke(
            "resume", "--project-root", self.project, "--guard-file", guard_path,
            *self.grok_arguments(include_provider=False),
        )
        self.assertEqual(process.returncode, 4)
        self.assertIn("session_guard_binding_digest_mismatch", result["block_reason"])

    def test_resume_refuses_unmatched_launch_receipt_without_replay(self):
        from agentfanout.host_runtime import HostLedger

        guard_path, _state_path, ledger_path, guard, _state = self.guard_fixture()
        ledger = HostLedger(ledger_path)
        try:
            ledger.append(
                guard["run_id"], "launch_receipt",
                {"launch_correlation_id": "launch_unknown_fixture"},
            )
        finally:
            ledger.close()
        process, result = self.invoke(
            "resume", "--project-root", self.project, "--guard-file", guard_path,
            *self.grok_arguments(include_provider=False),
        )
        self.assertEqual(process.returncode, 4, process.stderr + process.stdout)
        self.assertIn("recovery_unknown_external_effect", result["block_reason"])
        ledger = HostLedger(ledger_path)
        try:
            receipts = ledger.events(guard["run_id"], "launch_receipt")
        finally:
            ledger.close()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(result["session_guard"]["status"], "blocked")

    def test_guard_symlink_is_not_followed(self):
        guard_path, _state_path, _ledger_path, _guard, _state = self.guard_fixture()
        real_guard = guard_path.with_name("real-guard.json")
        guard_path.rename(real_guard)
        guard_path.symlink_to(real_guard)
        process, result = self.invoke("status", "--guard-file", guard_path)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("private_file_symlink_or_unavailable", result["reason"])

    def test_active_orchestrator_lock_blocks_resume_and_abandon(self):
        guard_path, _state_path, _ledger_path, _guard, _state = self.guard_fixture()
        spec = importlib.util.spec_from_file_location("agentfanout_atomic_cli_lock", CLI)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with module._guard_lock(guard_path, exclusive=True, blocking=False):
            status_process, status = self.invoke("status", "--guard-file", guard_path)
            self.assertEqual(status_process.returncode, 0)
            self.assertTrue(status["recovery"]["orchestrator_process_active"])
            self.assertFalse(status["recovery"]["resume_allowed"])
            abandon_process, abandon = self.invoke(
                "abandon", "--project-root", self.project, "--guard-file", guard_path,
                "--reason", "must not race active runner",
            )
            self.assertEqual(abandon_process.returncode, 4)
            self.assertIn("session_guard_orchestrator_active", abandon["block_reason"])

    def test_json_helper_timeout_terminates_process_group_without_traceback(self):
        spec = importlib.util.spec_from_file_location("agentfanout_atomic_cli", CLI)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        code, result = module._run_json(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            os.environ.copy(),
            timeout_seconds=0.05,
        )
        self.assertEqual(code, 4)
        self.assertEqual(result["error"], "child_timeout")
        self.assertTrue(result["termination_confirmed"])
        self.assertEqual(result["termination_scope"], "process_group_only")

    @unittest.skipUnless(os.name == "posix", "signal/process-group contract is POSIX-only")
    def test_sigterm_gracefully_cancels_runner_and_provider_without_orphan(self):
        with _FakeEndpoint(mode="delay") as endpoint:
            local_args, _attestation_file, _bridge_file = self.local_arguments(endpoint.endpoint)
            env = os.environ.copy()
            env.pop("AGENT_FANOUT_PROJECT_ROOT", None)
            process = subprocess.Popen(
                [
                    sys.executable, "-S", str(CLI), "run",
                    "--project-root", str(self.project),
                    "--state-dir", str(self.state_dir),
                    *map(str, local_args),
                    "--accept-overhead", "--", "review one.md",
                ],
                cwd=self.unrelated,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            deadline = __import__("time").monotonic() + 8
            while not endpoint.requests and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.02)
            self.assertTrue(endpoint.requests, "provider did not start")
            os.kill(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 4, stderr + stdout)
        outcome = json.loads(stdout)
        self.assertEqual(outcome["status"], "blocked")
        self.assertIn("runner_shutdown_requested:SIGTERM", outcome["block_reason"])
        from agentfanout.host_runtime import HostLedger
        ledger = HostLedger(outcome["artifacts"]["ledger_file"])
        try:
            terminated = ledger.events(outcome["session_guard"]["run_id"], "launch_termination_confirmed")
        finally:
            ledger.close()
        self.assertTrue(terminated)
        self.assertTrue(all(event["payload"]["termination_scope"] == "process_group_only" for event in terminated))


if __name__ == "__main__":
    unittest.main()
