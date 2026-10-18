import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from agentfanout.provider_registry import (
    decision_telemetry,
    load_registry,
    normalize_reasoning,
    profile_capable,
    profile_for,
    provider_config,
    provider_enabled,
    ranked_provider_options,
    requested_model_alias,
    resolve_model,
    risk_for,
)


ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "scripts" / "route.py"
REGISTRY = ROOT / "providers" / "models.json"


def reject_constant(_value):
    raise ValueError("nonfinite_json")


class ProviderRegistrySecurityTests(unittest.TestCase):
    def test_grok_registry_describes_hardened_direct_https_admission(self):
        grok = load_registry()["providers"]["Grok"]
        self.assertEqual(grok["runtime_admission"], {
            "cli_scope": "model_discovery_only",
            "credential_validation": "pinned_https_helper_offline_preflight",
            "inference_transport": "pinned_fixed_https_helper",
            "inference_origin": "https://api.x.ai",
            "chat_completions_model_families": ["grok-4.5"],
            "structured_output_contract": "strict_json_schema_worker_and_validator_packets",
            "tools_field": "absent",
            "functions_field": "absent",
            "proxies": "disabled",
            "redirects": "disabled",
            "strict_lane": "available_only_after_auth_and_helper_preflight",
        })
        self.assertEqual(
            grok["launch_overhead_source"],
            "sanitized-live-direct-https-smoke-2026-07-11-conservative-total-token-floor",
        )
        self.assertNotIn("acp", grok["launch_overhead_source"].casefold())
        self.assertEqual(grok["profiles"]["fast"]["reasoning_efforts"], ["low"])
        self.assertEqual(grok["profiles"]["balanced"]["reasoning_efforts"], ["medium"])
        self.assertEqual(grok["profiles"]["frontier"]["reasoning_efforts"], ["high"])

    def test_loader_rejects_nonstandard_json_numeric_constants(self):
        source = REGISTRY.read_text(encoding="utf-8")
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "models.json"
                path.write_text(
                    source.replace('"relative_cost": 0.25', f'"relative_cost": {literal}', 1),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "provider_registry_numeric_non_finite"):
                    load_registry(path)

    def test_every_public_registry_use_site_rejects_nonfinite_profile_metrics(self):
        base = load_registry()
        calls = {
            "normalize_reasoning": lambda data: normalize_reasoning("low", data),
            "requested_model_alias": lambda data: requested_model_alias("use luna", data),
            "provider_config": lambda data: provider_config("Codex", data),
            "provider_enabled": lambda data: provider_enabled("Codex", data),
            "profile_for": lambda data: profile_for("Codex", "bulk-read", "low", registry=data),
            "resolve_model": lambda data: resolve_model("Codex", "fast", registry=data),
            "risk_for": lambda data: risk_for("bulk-read", "low", data),
            "profile_capable": lambda data: profile_capable("Codex", "fast", "bulk-read", "low", data),
            "ranked_provider_options": lambda data: ranked_provider_options("bulk-read", "low", data),
            "decision_telemetry": lambda data: decision_telemetry(
                "review one file", "Codex", "bulk-read", "low", 1, 1, registry=data,
            ),
        }
        for field in ("relative_cost", "relative_latency"):
            for value in (
                float("nan"), float("inf"), float("-inf"), 10**400,
                0.0, -0.1, None, "0.1",
            ):
                for name, call in calls.items():
                    with self.subTest(field=field, value=repr(value), use_site=name):
                        data = deepcopy(base)
                        data["providers"]["Codex"]["profiles"]["fast"][field] = value
                        with self.assertRaisesRegex(ValueError, "invalid_provider_registry_numeric"):
                            call(data)

    def test_launch_overhead_is_missing_or_a_strictly_positive_integer(self):
        base = load_registry()
        missing = deepcopy(base)
        missing["providers"]["Codex"].pop("launch_overhead_input_tokens")
        telemetry = decision_telemetry(
            "review one file", "Codex", "bulk-read", "low", 1, 1, registry=missing,
        )
        self.assertEqual(telemetry["estimated_launch_overhead_input_tokens_per_call"], 15_000)
        self.assertEqual(telemetry["launch_overhead_source"], "unknown_conservative_default")

        for value in (
            float("nan"), float("inf"), float("-inf"), 10**400,
            0, -1, 1.5, True,
        ):
            with self.subTest(value=repr(value)):
                data = deepcopy(base)
                data["providers"]["Codex"]["launch_overhead_input_tokens"] = value
                with self.assertRaisesRegex(ValueError, "invalid_provider_registry_numeric"):
                    decision_telemetry(
                        "review one file", "Codex", "bulk-read", "low", 1, 1,
                        registry=data,
                    )

    def test_decision_telemetry_is_finite_and_strict_json(self):
        telemetry = decision_telemetry(
            "review two files", "Codex", "bulk-read", "low", 2, 2,
            validator_required=True, unit_input_tokens=[25, 50],
        )
        stack = [telemetry]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, float):
                self.assertTrue(math.isfinite(value))
        encoded = json.dumps(telemetry, allow_nan=False)
        self.assertEqual(json.loads(encoded, parse_constant=reject_constant)["schema"], "DecisionTelemetryV1")

    def test_astra_profile_is_explicit_only_and_negation_safe(self):
        registry = load_registry()
        self.assertIsNone(requested_model_alias("Do not use Astra for this review", registry))
        alias = requested_model_alias("Use Astra for this exceptional review", registry)
        self.assertEqual(alias, {"alias": "astra", "provider": "Codex", "profile": "astra"})
        self.assertEqual(profile_for("Codex", "security-review", "high", registry=registry), "frontier")
        self.assertEqual(
            profile_for("Codex", "security-review", "high", alias, registry),
            "astra",
        )
        resolution = resolve_model("Codex", "astra", alias, registry)
        self.assertTrue(resolution["explicit_only"])
        self.assertEqual(resolution["required_model_id"], "gpt-6-astra")
        self.assertEqual(resolution["max_parallel_workers"], 1)
        self.assertEqual(resolution["selection_reason"], "explicit_model_alias:astra")

    def test_router_fails_closed_before_anti_waste_on_poisoned_registry(self):
        source = REGISTRY.read_text(encoding="utf-8")
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "project"
                project.mkdir()
                (project / ".git").mkdir()
                (project / "README.md").write_text("# Fixture\n", encoding="utf-8")
                registry = root / "models.json"
                registry.write_text(
                    source.replace('"relative_cost": 0.25', f'"relative_cost": {literal}', 1),
                    encoding="utf-8",
                )
                env = os.environ.copy()
                env.update({
                    "AGENT_FANOUT_PROVIDER_REGISTRY": str(registry),
                    "AGENT_FANOUT_PROJECT_ROOT": str(project),
                    "AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST": str(project),
                    "AGENT_FANOUT_CODEX_SUBAGENTS": "1",
                })
                process = subprocess.run(
                    [
                        sys.executable, "-S", str(ROUTE),
                        "AgentFanout Codex: review README.md; I accept orchestration overhead",
                    ],
                    cwd=ROOT, env=env, text=True, capture_output=True, check=False,
                )
                self.assertNotEqual(process.returncode, 0)
                combined = process.stdout + process.stderr
                self.assertIn("provider_registry_numeric_non_finite", combined)
                self.assertNotIn('"decision": "allow"', process.stdout)
                self.assertNotIn(literal, process.stdout)

    def test_router_output_with_valid_registry_is_strict_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            (project / ".git").mkdir()
            (project / "README.md").write_text("# Fixture\n", encoding="utf-8")
            env = os.environ.copy()
            env.pop("AGENT_FANOUT_PROVIDER_REGISTRY", None)
            env.update({
                "AGENT_FANOUT_PROJECT_ROOT": str(project),
                "AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST": str(project),
                "AGENT_FANOUT_CODEX_SUBAGENTS": "1",
            })
            process = subprocess.run(
                [
                    sys.executable, "-S", str(ROUTE),
                    "AgentFanout Codex: review README.md; I accept orchestration overhead",
                ],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            payload = json.loads(process.stdout, parse_constant=reject_constant)
            json.dumps(payload, allow_nan=False)
            self.assertIn(payload["anti_waste_guard"]["decision"], {"allow", "suppress_to_main_session"})


if __name__ == "__main__":
    unittest.main()
