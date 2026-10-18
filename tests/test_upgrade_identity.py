"""Invocation identity and admission regression tests, with no provider access."""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agentfanout.build_identity import build_report
from agentfanout.provider_registry import load_registry, provider_enabled, ranked_provider_options
from agentfanout.upgrade_profiles import capability_status, validate_controls

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "agentfanout.py"


class UpgradeIdentityTests(unittest.TestCase):
    def invoke(self, *args):
        result = subprocess.run([sys.executable, str(CLI), *args], cwd=ROOT,
                                capture_output=True, text=True, timeout=20)
        return result, json.loads(result.stdout)

    def test_actual_invocation_is_bound_and_source_is_not_release_authority(self):
        result, report = self.invoke("identity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["entrypoint"]["path"], str(CLI))
        self.assertTrue(report["imported_modules"])
        expected_kind = ("staged_release" if ROOT.name.startswith(".stage-")
                         else "immutable_release") if (ROOT / ".agentfanout-manifest.json").exists() else "source"
        self.assertEqual(report["release"]["kind"], expected_kind)
        self.assertEqual(report["release"]["verified"], expected_kind == "immutable_release")
        self.assertFalse(report["capability_authority"])
        self.assertEqual(len(report["feature_fingerprint"]), 64)
        self.assertEqual(report["product_version"], (ROOT / "VERSION").read_text().strip())

    def test_shadow_import_outside_runtime_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as temp:
            module = types.ModuleType("agentfanout.shadow")
            module.__file__ = temp.name
            with patch.dict(sys.modules, {"agentfanout.shadow": module}):
                with self.assertRaisesRegex(ValueError, "runtime_import_outside_invocation"):
                    build_report(ROOT, CLI)

    def test_entrypoint_outside_runtime_refused(self):
        with tempfile.NamedTemporaryFile() as temp:
            with self.assertRaisesRegex(ValueError, "entrypoint_outside_runtime"):
                build_report(ROOT, Path(temp.name))

    def test_subscription_refuses_before_project_provider_and_state_operations(self):
        result, report = self.invoke("run", "--provider", "GrokCLI", "bounded review")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(report["block_reason"], "grok_cli_installed_boundary_unproved")
        self.assertEqual(report["mode"], "grok_cli_subscription_acp")
        self.assertFalse(report["enforcement"]["fallback_allowed"])
        result, report = self.invoke("doctor", "--provider", "GrokCLI")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(report["ready"])
        self.assertIn("build_identity", report)

    def test_environment_or_registry_cannot_enable_subscription_or_automatic_fallback(self):
        registry = load_registry()
        registry["providers"]["GrokCLI"]["enabled"] = True
        with patch.dict(os.environ, {"AGENT_FANOUT_ENABLE_DISABLED_PROVIDERS": "1"}):
            enabled, reason = provider_enabled("GrokCLI", registry)
            self.assertFalse(enabled)
            self.assertEqual(reason, "grok_cli_installed_boundary_unproved")
            self.assertNotIn("GrokCLI", [option["provider"] for option in ranked_provider_options("bulk-read", "low", registry)])

    def test_controls_require_exact_boolean_vocabulary_and_deferred_stays_off(self):
        controls = capability_status()["controls"]
        self.assertEqual(validate_controls(controls), controls)
        for override in ({"retained_sessions": True}, {"coordinator_local": 1}, {"tool_capable_workers": True}, {"unknown": False}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_controls({**controls, **override})

    def test_new_local_and_mixed_profiles_refuse_before_legacy_endpoints(self):
        for profile in ("coordinator_local", "mixed_provider_runs"):
            with self.subTest(profile=profile):
                result, report = self.invoke("run", "--execution-profile", profile, "bounded review")
                self.assertEqual(result.returncode, 4)
                self.assertFalse(report["enforcement"]["fallback_allowed"])
                result, report = self.invoke("doctor", "--execution-profile", profile)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(report["ready"])

    def test_router_explicit_subscription_never_becomes_legacy_grok_or_auto(self):
        for text in ("AgentFanout GrokCLI review two bounded pieces", "use GrokCLI for review"):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/route.py"), text],
                                    cwd=ROOT, capture_output=True, text=True, timeout=20)
            report = json.loads(result.stdout)
            self.assertEqual(report["selected_provider"], "GrokCLI")
            self.assertEqual(report["status"], "fanout_blocked")
            self.assertFalse(report["execution_allowed"])
            self.assertFalse(report["fallback_allowed"])


if __name__ == "__main__":
    unittest.main()
