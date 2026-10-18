from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALL = _load("agentfanout_public_install", "scripts/install.py")
UNINSTALL = _load("agentfanout_public_uninstall", "scripts/uninstall.py")
READINESS = _load("agentfanout_public_readiness", "scripts/public-readiness.py")


class PublicDistributionTests(unittest.TestCase):
    def test_version_dependency_and_community_files_are_release_allowlisted(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("cryptography", requirements)
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        for requirement in requirements.splitlines():
            if requirement.strip() and not requirement.startswith("#"):
                self.assertIn(requirement.strip(), lock)
        self.assertIn("--hash=sha256:", lock)
        allowlist = json.loads((ROOT / "release/public-safe-allowlist.json").read_text(encoding="utf-8"))
        for profile in ("public", "runtime"):
            files = allowlist["profiles"][profile]["files"]
            self.assertIn("VERSION", files)
            self.assertIn("requirements.lock", files)
            self.assertIn("requirements.txt", files)
            self.assertIn("CODE_OF_CONDUCT.md", files)
        completed = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts/agentfanout.py"), "--version"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), version)

    def test_codex_plugin_is_in_public_projection_with_honest_capability_profile(self):
        source = Path(os.environ.get("AGENT_FANOUT_TEST_SOURCE_ROOT", ROOT))
        allowlist = json.loads((source / "release/public-safe-allowlist.json").read_text(encoding="utf-8"))
        self.assertIn("plugins", allowlist["profiles"]["public"]["directories"])
        self.assertIn(".agents", allowlist["profiles"]["public"]["directories"])
        self.assertIn("plugins", allowlist["profiles"]["runtime"]["directories"])
        self.assertIn(".agents", allowlist["profiles"]["runtime"]["directories"])
        self.assertTrue((ROOT / "plugins/agentfanout/.codex-plugin/plugin.json").is_file())
        profile = json.loads((source / "profiles/codex-plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["enforcement"], "hook_continuation_and_supported_tool_guard")
        self.assertEqual(profile["claim_limit"], "not_universal_anti_bypass")
        selected = subprocess.run(
            [sys.executable, str(source / "scripts/release-files.py"), "--profile", "public"],
            cwd=source, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(selected.returncode, 0, selected.stderr)
        projected = set(selected.stdout.splitlines())
        self.assertIn("plugins/agentfanout/.codex-plugin/plugin.json", projected)
        self.assertIn("plugins/agentfanout/hooks/agentfanout_hook.py", projected)
        self.assertIn(".agents/plugins/marketplace.json", projected)

    def test_public_install_dry_run_is_bounded_and_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Path(os.environ.get("AGENT_FANOUT_TEST_SOURCE_ROOT", ROOT))
            result = INSTALL.install(source, root / "prefix", root / "bin", dry_run=True)
            self.assertEqual(result["schema"], "AgentFanoutPublicInstallV1")
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse((root / "prefix").exists())
            self.assertFalse((root / "bin").exists())
        with self.assertRaisesRegex(INSTALL.InstallError, "must be absolute"):
            INSTALL.install(ROOT, Path("relative-prefix"), Path("relative-bin"), dry_run=True)

    def test_launcher_is_owner_managed_and_uninstall_preserves_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            runtime = prefix / "runtime"
            release = runtime / ".agentfanout-releases" / "example"
            release.mkdir(parents=True)
            active = runtime / "AgentFanout"
            active.symlink_to(release, target_is_directory=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            launcher = bin_dir / "agentfanout"
            content = INSTALL._managed_launcher(launcher, prefix / "venv" / "bin" / "python", active)
            INSTALL._write_launcher(launcher, content)
            self.assertTrue(stat.S_IMODE(launcher.stat().st_mode) & stat.S_IXUSR)

            result = UNINSTALL.deactivate(prefix, launcher)
            self.assertEqual(result["status"], "deactivated")
            self.assertFalse(launcher.exists())
            self.assertFalse(active.exists())
            self.assertTrue(release.is_dir())

    def test_installer_refuses_to_replace_unmanaged_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "agentfanout"
            launcher.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
            launcher.chmod(0o644)
            with self.assertRaisesRegex(INSTALL.InstallError, "unmanaged launcher"):
                INSTALL._write_launcher(
                    launcher,
                    INSTALL._managed_launcher(launcher, Path(sys.executable), Path(tmp) / "active"),
                )
            self.assertIn("unrelated", launcher.read_text(encoding="utf-8"))

    def test_installer_refuses_marker_spoof_and_writable_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "agentfanout"
            desired = INSTALL._managed_launcher(
                launcher, Path(sys.executable), root / "runtime" / "AgentFanout",
            )
            launcher.write_text("# prefix\n" + INSTALL.LAUNCHER_MARKER + "\n", encoding="utf-8")
            launcher.chmod(0o644)
            with self.assertRaisesRegex(INSTALL.InstallError, "unmanaged launcher"):
                INSTALL._write_launcher(launcher, desired)
            launcher.write_text(desired, encoding="utf-8")
            launcher.chmod(0o777)
            with self.assertRaisesRegex(INSTALL.InstallError, "ownership or mode is unsafe"):
                INSTALL._write_launcher(launcher, desired)

    def test_missing_venv_bootstrap_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(INSTALL, "_git_identity", return_value=("a" * 40, True)),
                patch.object(
                    INSTALL.venv.EnvBuilder, "create",
                    side_effect=subprocess.CalledProcessError(1, ["python", "-m", "ensurepip"]),
                ),
            ):
                with self.assertRaisesRegex(INSTALL.InstallError, "venv/ensurepip bootstrap is unavailable"):
                    INSTALL.install(ROOT, root / "prefix", root / "bin")

    def test_launcher_shell_quotes_metacharacter_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "bin $value' agentfanout"
            active = root / "active `value` $HOME ' runtime"
            content = INSTALL._managed_launcher(launcher, Path(sys.executable), active)
            script = root / "launcher.sh"
            script.write_text(content, encoding="utf-8")
            checked = subprocess.run(
                ["sh", "-n", str(script)], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("'", content)

    def test_root_binding_failure_includes_machine_actionable_recovery(self):
        completed = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts/agentfanout.py"), "run", "--provider", "Local", "--", "review README.md"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={key: value for key, value in os.environ.items() if key != "AGENT_FANOUT_PROJECT_ROOT"},
            timeout=30,
        )
        self.assertEqual(completed.returncode, 3, completed.stderr + completed.stdout)
        value = json.loads(completed.stdout)
        self.assertEqual(value["block_reason"], "approved_project_root_missing")
        self.assertEqual(value["remediation"]["category"], "project_binding")
        self.assertTrue(value["remediation"]["retryable"])
        self.assertIn("--project-root", [item["flag"] for item in value["remediation"]["safe_options"]])

    def test_sensitive_root_recovery_preserves_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "fixture-project"
            project.mkdir()
            (project / ".git").mkdir()
            environment = {
                key: value for key, value in os.environ.items()
                if key != "AGENT_FANOUT_PROJECT_ROOT"
            }
            environment["AGENT_FANOUT_SENSITIVE_ROOTS"] = str(project.resolve())
            completed = subprocess.run(
                [
                    sys.executable, "-S", str(ROOT / "scripts/agentfanout.py"),
                    "run", "--project-root", str(project), "--provider", "Local",
                    "--", "review README.md",
                ],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, timeout=30,
            )
            self.assertEqual(completed.returncode, 3, completed.stderr + completed.stdout)
            value = json.loads(completed.stdout)
            self.assertEqual(value["block_reason"], "approved_project_root_sensitive")
            remediation = value["remediation"]
            self.assertEqual(remediation["category"], "project_binding")
            self.assertIn("Do not allowlist", remediation["forbidden_fallback"])

    def test_readiness_without_exact_cleanroom_evidence_is_bounded(self):
        code, value = READINESS.report(ROOT, run_distribution_tests=False)
        self.assertEqual(code, 2)
        self.assertEqual(value["schema"], "AgentFanoutPublicReadinessV1")
        self.assertEqual(value["status"], "blocked")
        cleanroom = next(item for item in value["checks"] if item["name"] == "cleanroom_install")
        self.assertFalse(cleanroom["passed"])
        self.assertFalse(value["publication_authorized"])


if __name__ == "__main__":
    unittest.main()
