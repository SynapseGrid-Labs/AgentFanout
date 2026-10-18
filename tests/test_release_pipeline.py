import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import release_lib as RELEASE_LIB
from release_lib import (
    ARCHIVE_MANIFEST_NAME, ReleaseError, archive_directory, collect_allowed_paths,
    make_immutable, make_writable, validate_manifest, validate_provenance,
)


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALL = load("agentfanout_install_runtime", "install-runtime.py")
ROLLBACK = load("agentfanout_rollback_runtime", "rollback-runtime.py")


@unittest.skipIf(
    os.getenv("AGENT_FANOUT_STAGE_CHECKS") == "1",
    "installer stage checks must not recursively invoke another promotion",
)
class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.skills = self.root / "skills"
        shutil.copytree(ROOT, self.source, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "AgentFanout test")
        self._git("add", ".")
        self._git("commit", "-m", "fixture")

    def tearDown(self):
        make_writable(self.root)
        self.tmp.cleanup()

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.source), *args], check=True, stdout=subprocess.DEVNULL)

    def _promote(self, post=None):
        return INSTALL.install_runtime(
            self.source, self.skills, "test-review", run_checks=lambda stage, source: None,
            post_switch_checks=post or (lambda stage, source: None),
        )

    def _directory_allowlist(self, name, directories):
        path = self.root / f"{name}-allowlist.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "profiles": {"test": {"files": [], "directories": directories}},
            "exclude": [],
        }), encoding="utf-8")
        return path

    def _orphan_check_command(self, exit_code):
        marker = self.root / f"orphan-group-{exit_code}.txt"
        child = self.root / f"orphan-child-{exit_code}.py"
        leader = self.root / f"orphan-leader-{exit_code}.py"
        child.write_text(
            "import os,signal,sys,time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path(sys.argv[1]).write_text(str(os.getpgrp()), encoding='utf-8')\n"
            "while True: time.sleep(1)\n",
            encoding="utf-8",
        )
        leader.write_text(
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
            "deadline=time.monotonic()+2\n"
            "while not Path(sys.argv[2]).exists():\n"
            "    if time.monotonic() >= deadline: raise SystemExit(73)\n"
            "    time.sleep(0.01)\n"
            "raise SystemExit(int(sys.argv[3]))\n",
            encoding="utf-8",
        )
        return [sys.executable, str(leader), str(child), str(marker), str(exit_code)], marker

    def _assert_process_group_gone(self, process_group_id):
        deadline = time.monotonic() + 1
        while True:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                self.fail(f"release-check process group {process_group_id} survived")
            time.sleep(0.02)

    def _legacy_runtime(self, name):
        active = self.skills / "AgentFanout"
        active.mkdir(parents=True)
        payload = active / name
        payload.write_text("legacy payload\n", encoding="utf-8")
        return active, payload, self.skills / ".agentfanout-archives"

    def test_release_check_timeout_terminates_process_group(self):
        with self.assertRaisesRegex(ReleaseError, "Release check timed out") as raised:
            INSTALL.run_checked(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                self.root,
                os.environ.copy(),
                timeout_seconds=0.05,
            )
        self.assertIn("process_group_exit_confirmed=True", str(raised.exception))

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "killpg"), "requires POSIX process groups")
    def test_release_check_rejects_exit_zero_with_surviving_group_member(self):
        command, marker = self._orphan_check_command(0)
        with self.assertRaisesRegex(ReleaseError, "left a background process group") as raised:
            INSTALL.run_checked(command, self.root, os.environ.copy(), timeout_seconds=5)
        self.assertIn("leader_returncode=0", str(raised.exception))
        self.assertIn("process_group_exit_confirmed=True", str(raised.exception))
        self._assert_process_group_gone(int(marker.read_text(encoding="utf-8")))

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "killpg"), "requires POSIX process groups")
    def test_release_check_rejects_exit_nonzero_with_surviving_group_member(self):
        command, marker = self._orphan_check_command(9)
        with self.assertRaisesRegex(ReleaseError, "left a background process group") as raised:
            INSTALL.run_checked(command, self.root, os.environ.copy(), timeout_seconds=5)
        self.assertIn("leader_returncode=9", str(raised.exception))
        self.assertIn("process_group_exit_confirmed=True", str(raised.exception))
        self._assert_process_group_gone(int(marker.read_text(encoding="utf-8")))

    def test_release_allowlist_rejects_top_level_symlink_directory(self):
        outside = self.root / "outside-top-level"
        outside.mkdir()
        (outside / "payload.txt").write_text("outside\n", encoding="utf-8")
        (self.source / "linked-directory").symlink_to(outside, target_is_directory=True)
        allowlist = self._directory_allowlist("top-level-symlink", ["linked-directory"])
        with self.assertRaisesRegex(ReleaseError, "Symlinked release paths are forbidden"):
            collect_allowed_paths(self.source, "test", allowlist)

    def test_release_allowlist_rejects_nested_symlink_directory(self):
        allowed = self.source / "allowed-directory"
        allowed.mkdir()
        (allowed / "safe.txt").write_text("inside\n", encoding="utf-8")
        outside = self.root / "outside-nested"
        outside.mkdir()
        (outside / "payload.txt").write_text("outside\n", encoding="utf-8")
        (allowed / "nested-link").symlink_to(outside, target_is_directory=True)
        allowlist = self._directory_allowlist("nested-symlink", ["allowed-directory"])
        with self.assertRaisesRegex(ReleaseError, "Symlinked release paths are forbidden"):
            collect_allowed_paths(self.source, "test", allowlist)

    def _promote_with_default_post_switch_checks(self):
        # Keep setup inexpensive, but leave post_switch_checks unspecified. This
        # exercises the production default against the immutable active link.
        return INSTALL.install_runtime(
            self.source, self.skills, "test-review", run_checks=lambda stage, source: None,
        )

    def test_dry_run_and_stage_only_never_switch_live_runtime(self):
        dry = INSTALL.install_runtime(self.source, self.skills, None, dry_run=True)
        self.assertEqual(dry["status"], "dry_run")
        self.assertFalse(self.skills.exists())
        staged = INSTALL.install_runtime(self.source, self.skills, None, stage_only=True, run_checks=lambda stage, source: None)
        self.assertEqual(staged["status"], "staged")
        self.assertTrue(Path(staged["release"]).is_dir())
        self.assertFalse((self.skills / "AgentFanout").exists())

    def test_review_bound_frozen_stage_is_reused_for_live_promotion(self):
        checked = []
        staged = INSTALL.install_runtime(
            self.source, self.skills, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        restaged = INSTALL.install_runtime(
            self.source, self.skills, "review-a", stage_only=True,
            run_checks=lambda stage, source: self.fail("frozen stage should be reused"),
        )
        self.assertTrue(restaged["reused_existing_release"])
        self.assertEqual(restaged["release"], staged["release"])
        promoted = INSTALL.install_runtime(
            self.source, self.skills, "review-a",
            run_checks=lambda stage, source: checked.append(stage),
            post_switch_checks=lambda stage, source: None,
        )
        self.assertEqual(promoted["status"], "promoted")
        self.assertTrue(promoted["reused_existing_release"])
        self.assertEqual(promoted["release"], staged["release"])
        self.assertEqual(checked, [Path(staged["release"])])
        self.assertEqual((self.skills / "AgentFanout").resolve(), Path(staged["release"]))

    def test_repeat_promotion_of_active_release_is_verified_noop(self):
        first = self._promote()
        release = Path(first["release"])
        before = (release / "README.md").read_bytes()
        callbacks = []

        def forbidden_check(stage, source):
            callbacks.append(stage)
            make_writable(stage.resolve())
            (stage.resolve() / "README.md").write_text("must not execute\n", encoding="utf-8")
            raise ReleaseError("repeat promotion executed a live-tree check")

        repeated = INSTALL.install_runtime(
            self.source, self.skills, "test-review",
            run_checks=forbidden_check,
            post_switch_checks=forbidden_check,
        )
        self.assertEqual(repeated["status"], "already_active")
        self.assertTrue(repeated["reused_existing_release"])
        self.assertEqual(callbacks, [])
        self.assertEqual((release / "README.md").read_bytes(), before)
        self.assertEqual((self.skills / "AgentFanout").resolve(), release)

    def test_existing_stage_requires_same_nondiagnostic_review_reference(self):
        mismatch_root = self.root / "mismatch-skills"
        INSTALL.install_runtime(
            self.source, mismatch_root, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        with self.assertRaisesRegex(ReleaseError, "review reference does not match"):
            INSTALL.install_runtime(
                self.source, mismatch_root, "review-b",
                run_checks=lambda stage, source: None,
            )
        self.assertFalse((mismatch_root / "AgentFanout").exists())

        diagnostic_root = self.root / "diagnostic-skills"
        INSTALL.install_runtime(
            self.source, diagnostic_root, None, stage_only=True,
            run_checks=lambda stage, source: None,
        )
        with self.assertRaisesRegex(ReleaseError, "diagnostic stage is not promotable"):
            INSTALL.install_runtime(
                self.source, diagnostic_root, "review-a",
                run_checks=lambda stage, source: None,
            )
        self.assertFalse((diagnostic_root / "AgentFanout").exists())

    def test_stage_mutated_by_executable_check_is_rejected_and_removed(self):
        def mutate_stage(stage, source):
            (stage / "README.md").write_text("mutated by check\n", encoding="utf-8")

        with self.assertRaisesRegex(ReleaseError, "manifest does not match"):
            INSTALL.install_runtime(
                self.source, self.skills, "review-a", stage_only=True,
                run_checks=mutate_stage,
            )
        releases = self.skills / ".agentfanout-releases"
        self.assertFalse(any(path.is_dir() for path in releases.iterdir()))
        self.assertFalse((self.skills / "AgentFanout").exists())

    def test_source_changed_during_release_assembly_is_rejected(self):
        def mutate_source(stage, source):
            (source / "README.md").write_text("concurrent source change\n", encoding="utf-8")

        with self.assertRaisesRegex(ReleaseError, "Source identity or runtime bytes changed"):
            INSTALL.install_runtime(
                self.source, self.skills, "review-a", stage_only=True,
                run_checks=mutate_source,
            )
        releases = self.skills / ".agentfanout-releases"
        self.assertFalse(any(path.is_dir() for path in releases.iterdir()))
        self.assertFalse((self.skills / "AgentFanout").exists())

    def test_existing_stage_rejects_mutability_extra_paths_and_internal_symlinks(self):
        mutable_root = self.root / "mutable-skills"
        mutable = INSTALL.install_runtime(
            self.source, mutable_root, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        make_writable(Path(mutable["release"]))
        with self.assertRaisesRegex(ReleaseError, "not immutable"):
            INSTALL.install_runtime(
                self.source, mutable_root, "review-a",
                run_checks=lambda stage, source: None,
            )
        self.assertFalse((mutable_root / "AgentFanout").exists())

        extra_root = self.root / "extra-skills"
        extra = INSTALL.install_runtime(
            self.source, extra_root, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        extra_release = Path(extra["release"])
        make_writable(extra_release)
        (extra_release / "sitecustomize.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
        make_immutable(extra_release)
        with self.assertRaisesRegex(ReleaseError, "unexpected or missing path"):
            INSTALL.install_runtime(
                self.source, extra_root, "review-a",
                run_checks=lambda stage, source: self.fail("unexpected stage must not execute"),
            )
        self.assertFalse((extra_root / "AgentFanout").exists())

        link_root = self.root / "link-skills"
        linked = INSTALL.install_runtime(
            self.source, link_root, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        linked_release = Path(linked["release"])
        make_writable(linked_release)
        (linked_release / "unexpected-link").symlink_to(linked_release / "README.md")
        make_immutable(linked_release)
        with self.assertRaisesRegex(ReleaseError, "Symlinks are forbidden"):
            INSTALL.install_runtime(
                self.source, link_root, "review-a",
                run_checks=lambda stage, source: self.fail("unsafe stage must not execute"),
            )
        self.assertFalse((link_root / "AgentFanout").exists())

    def test_release_root_and_broken_release_symlinks_fail_closed(self):
        outside = self.root / "outside-release-root"
        outside.mkdir()
        symlink_root = self.root / "symlink-skills"
        symlink_root.mkdir()
        (symlink_root / ".agentfanout-releases").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ReleaseError, "Release root must not be a symlink"):
            INSTALL.install_runtime(
                self.source, symlink_root, "review-a", stage_only=True,
                run_checks=lambda stage, source: None,
            )

        broken_root = self.root / "broken-skills"
        plan = INSTALL.install_runtime(self.source, broken_root, "review-a", dry_run=True)
        releases = broken_root / ".agentfanout-releases"
        releases.mkdir(parents=True)
        (releases / plan["release_id"]).symlink_to(self.root / "missing-release", target_is_directory=True)
        with self.assertRaisesRegex(ReleaseError, "not a regular directory"):
            INSTALL.install_runtime(
                self.source, broken_root, "review-a", stage_only=True,
                run_checks=lambda stage, source: None,
            )
        self.assertTrue((releases / plan["release_id"]).is_symlink())
        self.assertFalse((broken_root / "AgentFanout").exists())

    def test_concurrent_installer_for_same_skills_root_fails_closed(self):
        with RELEASE_LIB.runtime_transaction_lock(self.skills):
            with self.assertRaisesRegex(ReleaseError, "runtime transaction is active"):
                INSTALL.install_runtime(
                    self.source, self.skills, "review-a", stage_only=True,
                    run_checks=lambda stage, source: None,
                )
        self.assertFalse((self.skills / "AgentFanout").exists())

    def test_dirty_source_refused_except_explicit_stage_diagnostic(self):
        overlay = self.source / ".runtime" / "developer.json"
        overlay.parent.mkdir()
        overlay.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "dirty"):
            self._promote()
        with self.assertRaisesRegex(ReleaseError, "diagnostic-only"):
            INSTALL.install_runtime(self.source, self.skills, "test-review", allow_dirty=True, run_checks=lambda a, b: None)
        staged = INSTALL.install_runtime(self.source, self.skills, None, stage_only=True, allow_dirty=True, run_checks=lambda a, b: None)
        self.assertEqual(staged["status"], "staged")

    def test_legacy_directory_is_archived_without_merging_runtime_artifacts(self):
        legacy = self.skills / "AgentFanout"
        (legacy / ".runtime").mkdir(parents=True)
        (legacy / ".runtime" / "private.json").write_text('{"private": true}\n', encoding="utf-8")
        (legacy / "__pycache__").mkdir()
        (legacy / "__pycache__" / "old.pyc").write_bytes(b"cache")
        result = self._promote()
        self.assertTrue((self.skills / "AgentFanout").is_symlink())
        archive = Path(result["legacy_archive"])
        self.assertTrue((archive / ".runtime" / "private.json").is_file())
        self.assertFalse((Path(result["release"]) / ".runtime").exists())
        self.assertFalse((Path(result["release"]) / "__pycache__").exists())

    def test_legacy_archive_rejects_symlink_before_active_moves(self):
        active, payload, archives = self._legacy_runtime("safe.txt")
        outside = self.root / "outside-legacy.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (active / "escape.txt").symlink_to(outside)
        with self.assertRaisesRegex(ReleaseError, "symlinks are forbidden"):
            archive_directory(active, archives)
        self.assertTrue(active.is_dir())
        self.assertEqual(payload.read_text(encoding="utf-8"), "legacy payload\n")

    def test_legacy_archive_rejects_unreadable_file_before_active_moves(self):
        active, payload, archives = self._legacy_runtime("unreadable.txt")
        original = RELEASE_LIB.sha256_file

        def injected_unreadable(path):
            if path.name == payload.name:
                raise PermissionError("injected unreadable file")
            return original(path)

        with patch.object(RELEASE_LIB, "sha256_file", side_effect=injected_unreadable):
            with self.assertRaisesRegex(ReleaseError, "file is unreadable"):
                archive_directory(active, archives)
        self.assertTrue(active.is_dir())
        self.assertTrue(payload.is_file())

    def test_legacy_archive_rejects_injected_reserved_manifest_before_move(self):
        active, payload, archives = self._legacy_runtime("safe.txt")
        (active / ARCHIVE_MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "reserved archive manifest"):
            archive_directory(active, archives)
        self.assertTrue(active.is_dir())
        self.assertTrue(payload.is_file())
        self.assertTrue((active / ARCHIVE_MANIFEST_NAME).is_file())

    def test_legacy_archive_post_move_hash_failure_restores_active(self):
        active, payload, archives = self._legacy_runtime("hash-target.txt")
        original = RELEASE_LIB.sha256_file
        calls = 0

        def fail_second_hash(path):
            nonlocal calls
            if path.name == payload.name:
                calls += 1
                if calls == 2:
                    raise ReleaseError("injected post-move hash failure")
            return original(path)

        with patch.object(RELEASE_LIB, "sha256_file", side_effect=fail_second_hash):
            with self.assertRaisesRegex(ReleaseError, "active runtime restored"):
                archive_directory(active, archives)
        self.assertTrue(active.is_dir())
        self.assertEqual(payload.read_text(encoding="utf-8"), "legacy payload\n")
        self.assertFalse(list(archives.glob("legacy-*")))

    def test_legacy_archive_manifest_write_failure_restores_active(self):
        active, payload, archives = self._legacy_runtime("manifest-target.txt")
        with patch.object(RELEASE_LIB, "write_json", side_effect=OSError("injected manifest write failure")):
            with self.assertRaisesRegex(ReleaseError, "active runtime restored"):
                archive_directory(active, archives)
        self.assertTrue(active.is_dir())
        self.assertEqual(payload.read_text(encoding="utf-8"), "legacy payload\n")
        self.assertFalse((active / ARCHIVE_MANIFEST_NAME).exists())
        self.assertFalse(list(archives.glob("legacy-*")))

    def test_post_switch_failure_restores_previous_release(self):
        first = self._promote()
        first_release = Path(first["release"])
        changed = self.source / "docs" / "release-note.md"
        changed.write_text("second\n", encoding="utf-8")
        self._git("add", "docs/release-note.md")
        self._git("commit", "-m", "second")
        with self.assertRaisesRegex(ReleaseError, "rollback completed"):
            self._promote(post=lambda stage, source: (_ for _ in ()).throw(ReleaseError("simulated verification failure")))
        self.assertEqual(Path((self.skills / "AgentFanout").resolve()), first_release)

    def test_zero_exit_post_switch_mutation_is_detected_and_rolled_back(self):
        first = self._promote()
        first_release = Path(first["release"])
        changed = self.source / "docs" / "release-note.md"
        changed.write_text("second\n", encoding="utf-8")
        self._git("add", "docs/release-note.md")
        self._git("commit", "-m", "second")

        def mutate_active(active, source):
            release = active.resolve()
            make_writable(release)
            (release / "unexpected-post-check.py").write_text("raise SystemExit(98)\n", encoding="utf-8")
            make_immutable(release)

        with self.assertRaisesRegex(ReleaseError, "rollback completed"):
            self._promote(post=mutate_active)
        self.assertEqual((self.skills / "AgentFanout").resolve(), first_release)
        self.assertFalse(any(
            path.name != first_release.name
            for path in (self.skills / ".agentfanout-releases").iterdir()
            if path.is_dir()
        ))

    def test_default_post_switch_checks_succeed_after_release_is_immutable(self):
        result = self._promote_with_default_post_switch_checks()
        active = self.skills / "AgentFanout"
        release = Path(result["release"])
        self.assertTrue(active.is_symlink())
        self.assertEqual(active.resolve(), release)
        validate_provenance(release, validate_manifest(release))
        self.assertFalse(list(release.rglob("__pycache__")))

    def test_stage_checks_use_safe_fixture_root_inside_nested_runtime_layout(self):
        """A real ~/.pai-style stage must not be treated as a delegable project root."""

        nested_skills = self.root / ".pai" / "skills"
        result = INSTALL.install_runtime(self.source, nested_skills, None, stage_only=True)
        release = Path(result["release"])
        self.assertEqual(result["status"], "staged")
        self.assertTrue(release.is_dir())
        self.assertIn(".pai", release.parts)
        self.assertFalse((nested_skills / "AgentFanout").exists())

    def test_verified_rollback_switches_between_immutable_releases(self):
        first = self._promote()
        old = Path(first["release"])
        (self.source / "docs" / "release-note.md").write_text("second\n", encoding="utf-8")
        self._git("add", "docs/release-note.md")
        self._git("commit", "-m", "second")
        self._promote()
        result = ROLLBACK.rollback(self.skills, old)
        self.assertEqual(Path(result["to"]), old)
        self.assertEqual((self.skills / "AgentFanout").resolve(), old)

    def test_rollback_rejects_mutable_extra_diagnostic_and_outside_releases(self):
        staged = INSTALL.install_runtime(
            self.source, self.skills, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        release = Path(staged["release"])
        make_writable(release)
        with self.assertRaisesRegex(ReleaseError, "not immutable"):
            ROLLBACK.rollback(self.skills, release)
        self.assertFalse((self.skills / "AgentFanout").exists())

        (release / "sitecustomize.py").write_text("raise SystemExit(97)\n", encoding="utf-8")
        make_immutable(release)
        with self.assertRaisesRegex(ReleaseError, "unexpected or missing path"):
            ROLLBACK.rollback(self.skills, release)
        self.assertFalse((self.skills / "AgentFanout").exists())

        diagnostic_root = self.root / "rollback-diagnostic-skills"
        diagnostic = INSTALL.install_runtime(
            self.source, diagnostic_root, None, stage_only=True,
            run_checks=lambda stage, source: None,
        )
        with self.assertRaisesRegex(ReleaseError, "Diagnostic-only stages"):
            ROLLBACK.rollback(diagnostic_root, Path(diagnostic["release"]))

        outside_root = self.root / "rollback-outside-skills"
        outside = INSTALL.install_runtime(
            self.source, outside_root, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        with self.assertRaisesRegex(ReleaseError, "inside the configured release root"):
            ROLLBACK.rollback(self.skills, Path(outside["release"]))

    def test_rollback_shares_transaction_lock_with_installer(self):
        staged = INSTALL.install_runtime(
            self.source, self.skills, "review-a", stage_only=True,
            run_checks=lambda stage, source: None,
        )
        with RELEASE_LIB.runtime_transaction_lock(self.skills):
            with self.assertRaisesRegex(ReleaseError, "runtime transaction is active"):
                ROLLBACK.rollback(self.skills, Path(staged["release"]))
        self.assertFalse((self.skills / "AgentFanout").exists())

    def test_rollback_state_write_failure_restores_prior_runtime(self):
        first = self._promote()
        old = Path(first["release"])
        (self.source / "docs" / "release-note.md").write_text("second\n", encoding="utf-8")
        self._git("add", "docs/release-note.md")
        self._git("commit", "-m", "second")
        second = self._promote()
        current = Path(second["release"])
        with patch.object(ROLLBACK, "write_state", side_effect=OSError("injected state failure")):
            with self.assertRaisesRegex(ReleaseError, "prior runtime restored"):
                ROLLBACK.rollback(self.skills, old)
        self.assertEqual((self.skills / "AgentFanout").resolve(), current)

    def test_tampered_manifest_or_provenance_is_rejected(self):
        staged = INSTALL.install_runtime(self.source, self.skills, None, stage_only=True, run_checks=lambda a, b: None)
        release = Path(staged["release"])
        make_writable(release)
        (release / ".agentfanout-manifest.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ReleaseError):
            validate_manifest(release)
        # A fresh staged release lets this half exercise provenance validation
        # independently of the intentionally corrupted manifest above.
        staged = INSTALL.install_runtime(self.source, self.root / "other-skills", None, stage_only=True, run_checks=lambda a, b: None)
        other = Path(staged["release"])
        make_writable(other)
        (other / ".agentfanout-provenance.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ReleaseError):
            validate_provenance(other, validate_manifest(other))

    def test_public_profile_covers_experiments_but_runtime_stays_minimal(self):
        public_paths = collect_allowed_paths(self.source, "public")
        runtime_paths = collect_allowed_paths(self.source, "runtime")
        self.assertTrue(any(path.startswith("experiments/") for path in public_paths))
        self.assertFalse(any(path.startswith("experiments/") for path in runtime_paths))

    def test_top_level_preflight_rejects_recursive_stage_skip_marker(self):
        env = {**os.environ, "AGENT_FANOUT_STAGE_CHECKS": "1"}
        completed = subprocess.run(
            ["bash", "scripts/github-preflight.sh"],
            cwd=self.source,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Refusing top-level public preflight", completed.stdout)

    def test_public_preflight_scans_binary_release_bytes_instead_of_ignoring_them(self):
        script = (self.source / "scripts" / "github-preflight.sh").read_text(encoding="utf-8")
        self.assertNotIn("grep -nH -I", script)
        self.assertNotIn("grep -nH -I -E -f", script)
        self.assertGreaterEqual(script.count("grep -a -nH -E"), 2)


if __name__ == "__main__":
    unittest.main()
