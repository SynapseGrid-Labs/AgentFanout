import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from release_lib import make_writable, parse_porcelain_v2_z


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PARITY = load("agentfanout_runtime_parity", "check-runtime-parity.py")
INSTALL = load("agentfanout_runtime_install", "install-runtime.py")


class RuntimeParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "source"
        shutil.copytree(ROOT, self.source, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        # These tests also run through post-switch verification where ROOT is an
        # immutable release. copytree preserves read-only modes, but the fixture
        # source must be writable to initialize its independent Git worktree.
        make_writable(self.source)
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "AgentFanout test")
        self._git("add", ".")
        self._git("commit", "-m", "fixture")
        result = INSTALL.install_runtime(
            self.source, root / "skills", "test-review", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        self.runtime = Path(result["release"])

    def tearDown(self):
        make_writable(Path(self.tmp.name))
        self.tmp.cleanup()

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.source), *args], check=True, stdout=subprocess.DEVNULL)

    def _report(self, mode="release"):
        return PARITY.build_report(self.source, self.runtime, mode=mode)

    def _mutate_runtime(self, relative, content):
        make_writable(self.runtime)
        path = self.runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_identical_verified_release_matches(self):
        report = self._report()
        self.assertEqual(report["status"], "match")
        self.assertEqual(report["exit_code"], 0)

    def test_missing_or_invalid_provenance_fails(self):
        self._mutate_runtime(PARITY.PROVENANCE_NAME, "{}\n")
        report = self._report()
        self.assertEqual(report["exit_code"], 1)
        self.assertIn("invalid_or_missing_provenance", report["critical_reasons"])

    def test_tampered_manifest_fails(self):
        self._mutate_runtime(".agentfanout-manifest.json", "{\"schema_version\": 1}\n")
        report = self._report()
        self.assertIn("invalid_or_missing_provenance", report["critical_reasons"])

    def test_dirty_source_fails_release_but_diagnostic_can_inspect(self):
        overlay = self.source / ".runtime" / "developer.json"
        overlay.parent.mkdir()
        overlay.write_text("{}\n", encoding="utf-8")
        diagnostic = self._report("diagnostic")
        self.assertEqual(diagnostic["status"], "match")
        release = self._report("release")
        self.assertIn("source_dirty", release["critical_reasons"])

    def test_source_only_untracked_public_file_fails(self):
        (self.source / "docs" / "new-public-file.md").write_text("new\n", encoding="utf-8")
        report = self._report("diagnostic")
        self.assertIn("public_safe_tree_drift", report["critical_reasons"])
        self.assertIn("provenance_source_tree_digest_mismatch", report["critical_reasons"])

    def test_provider_schema_and_core_drift_remain_failures(self):
        self._mutate_runtime("adapters/Unexpected.md", "# unexpected\n")
        report = self._report()
        self.assertIn("provider_set_drift", report["critical_reasons"])
        self._mutate_runtime("scripts/route.py", "PROVIDERS = {'changed'}\n")
        report = self._report()
        self.assertIn("router_schema_drift", report["critical_reasons"])
        self.assertIn("core_file_drift", report["critical_reasons"])

    def test_porcelain_v2_parser_preserves_spaces_and_rename_endpoints(self):
        payload = (
            b"1 .M N... 100644 100644 100644 abc abc docs/file with spaces.md\0"
            b"2 R. N... 100644 100644 100644 abc abc R100 docs/new name.md\0docs/old name.md\0"
            b"? docs/untracked name.md\0"
        )
        self.assertEqual(
            parse_porcelain_v2_z(payload),
            ["docs/file with spaces.md", "docs/new name.md", "docs/old name.md", "docs/untracked name.md"],
        )


if __name__ == "__main__":
    unittest.main()
