import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentfanout.project_binding import (
    ProjectBindingError,
    resolve_bounded_file,
    resolve_project_binding,
    secure_private_directory,
    validate_project_binding,
)


class ProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "config" / "auth-service"
        self.root.mkdir(parents=True)
        (self.root / ".git").mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "auth.py").write_text("print('ordinary source')\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_common_repository_names_are_not_globally_sensitive(self):
        binding = resolve_project_binding(explicit_root=self.root, env_root="", cwd=Path(self.tmp.name))
        self.assertEqual(validate_project_binding(binding), self.root.resolve())
        path, content = resolve_bounded_file(binding, "file:src/auth.py")
        self.assertEqual(path.name, "auth.py")
        self.assertIn("ordinary source", content)

    def test_explicit_sensitive_root_is_denied(self):
        with patch.dict(os.environ, {"AGENT_FANOUT_SENSITIVE_ROOTS": str(self.root)}, clear=False):
            with self.assertRaisesRegex(ProjectBindingError, "approved_project_root_sensitive"):
                resolve_project_binding(explicit_root=self.root, env_root="")

    def test_discovery_is_explicit_and_accepts_git_worktree_file(self):
        nested = self.root / "src" / "nested"
        nested.mkdir()
        discovered = resolve_project_binding(
            explicit_root=None, env_root="", cwd=nested, discover_current_worktree=True,
        )
        self.assertEqual(discovered["resolved_root"], str(self.root.resolve()))
        worktree = Path(self.tmp.name) / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")
        self.assertEqual(
            resolve_project_binding(explicit_root=worktree, env_root="")["resolved_root"],
            str(worktree.resolve()),
        )

    def test_discovery_cannot_override_conflicting_host_pinned_root(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (other / ".git").mkdir()
        with self.assertRaisesRegex(ProjectBindingError, "project_root_not_host_approved"):
            resolve_project_binding(
                explicit_root=None, env_root=other, cwd=self.root / "src",
                discover_current_worktree=True,
            )

    def test_allowlisted_non_git_root_is_labeled_truthfully(self):
        non_git = Path(self.tmp.name) / "non-git-project"
        non_git.mkdir()
        with patch.dict(
            os.environ,
            {"AGENT_FANOUT_APPROVED_ROOT_ALLOWLIST": str(non_git)},
            clear=False,
        ):
            binding = resolve_project_binding(explicit_root=non_git, env_root="")
            self.assertEqual(binding["vcs"], "none")
            self.assertEqual(binding["vcs_root"], str(non_git.resolve()))
            self.assertEqual(validate_project_binding(binding), non_git.resolve())

    def test_git_marker_symlink_and_malformed_file_are_rejected(self):
        target = Path(self.tmp.name) / "git-target"
        target.mkdir()
        symlink_root = Path(self.tmp.name) / "symlink-root"
        symlink_root.mkdir()
        (symlink_root / ".git").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ProjectBindingError, "not_repo_or_allowlisted"):
            resolve_project_binding(explicit_root=symlink_root, env_root="")

        malformed_root = Path(self.tmp.name) / "malformed-root"
        malformed_root.mkdir()
        (malformed_root / ".git").write_text("not a worktree marker\n", encoding="utf-8")
        with self.assertRaisesRegex(ProjectBindingError, "not_repo_or_allowlisted"):
            resolve_project_binding(explicit_root=malformed_root, env_root="")

    def test_conflicting_environment_root_fails_before_use(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (other / ".git").mkdir()
        with self.assertRaisesRegex(ProjectBindingError, "project_root_not_host_approved"):
            resolve_project_binding(explicit_root=self.root, env_root=other)

    def test_sensitive_and_escaped_files_fail_closed(self):
        binding = resolve_project_binding(explicit_root=self.root, env_root="")
        (self.root / ".env").write_text("NO_REAL_SECRET=fixture\n", encoding="utf-8")
        with self.assertRaisesRegex(ProjectBindingError, "bounded_file_reference_unsafe"):
            resolve_bounded_file(binding, "file:.env")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (self.root / "escaped").symlink_to(outside)
        with self.assertRaisesRegex(ProjectBindingError, "bounded_file_symlink_or_unavailable"):
            resolve_bounded_file(binding, "file:escaped")

    def test_credential_content_is_blocked_before_json_serialization(self):
        binding = resolve_project_binding(explicit_root=self.root, env_root="")
        target = self.root / "settings.json"
        samples = [
            '{"password":"CorrectHorseBatteryStaple"}',
            '{"api_key":"ABCDEFGHIJKLMNOPQRSTUVWX"}',
            "AWS_ACCESS_KEY_ID=" + "AK" + "IAABCDEFGHIJKLMNOP",
            "aws_secret_access_key=abcdefghijklmnopqrstuvwxyz123456",
            "DATABASE_URL=postgres://user:pass@example.com/db",
            "//registry.npmjs.org/:_authToken=" + "npm" + "_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "machine example.com login buildbot password CorrectHorseBatteryStaple",
            '{"auths":{"registry.example":{"auth":"dXNlcjpwYXNzd29yZA=="}}}',
            "google_api_key=" + "AI" + "zaABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "gitlab_token=" + "gl" + "pat-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "DefaultEndpointsProtocol=https;AccountName=fixture;AccountKey=ABCDEFGHIJKLMNOPQRSTUVWXYZ123456;EndpointSuffix=core.windows.net",
            "OPENAI_API_KEY=" + "sk" + "-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "ANTHROPIC_API_KEY=" + "sk" + "-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "XAI_API_KEY=" + "x" + "ai-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "MY_APP_PASSWORD=CorrectHorseBatteryStaple",
            '{\n  "auths": {\n    "registry.example": {\n      "auth": "dXNlcjpwYXNzd29yZA=="\n    }\n  }\n}',
        ]
        for sample in samples:
            with self.subTest(sample=sample.split("=", 1)[0]):
                target.write_text(sample + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ProjectBindingError, "bounded_file_content_sensitive"):
                    resolve_bounded_file(binding, "file:settings.json")

    def test_credential_config_filename_policy_matches_router_and_runner(self):
        binding = resolve_project_binding(explicit_root=self.root, env_root="")
        (self.root / "oauth.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ProjectBindingError, "bounded_file_reference_unsafe"):
            resolve_bounded_file(binding, "file:oauth.json")

    def test_portable_credential_store_paths_are_denied(self):
        binding = resolve_project_binding(explicit_root=self.root, env_root="")
        for relative in (".npmrc", ".netrc", ".pypirc", ".docker/config.json", ".kube/config"):
            with self.subTest(relative=relative):
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture\n", encoding="utf-8")
                with self.assertRaisesRegex(ProjectBindingError, "bounded_file_reference_unsafe"):
                    resolve_bounded_file(binding, f"file:{relative}")

    def test_private_state_directory_rejects_writable_intermediate_ancestor(self):
        unsafe = Path(self.tmp.name) / "unsafe-parent"
        unsafe.mkdir(mode=0o700)
        target = unsafe / "private-state"
        target.mkdir(mode=0o700)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(ProjectBindingError, "private_directory_ancestor_permissions_unsafe"):
            secure_private_directory(target)

    def test_private_state_directory_rejects_group_writable_ancestor(self):
        parent = Path(self.tmp.name) / "shared-parent"
        parent.mkdir(mode=0o700)
        target = parent / "private-state"
        target.mkdir(mode=0o700)
        parent.chmod(0o770)
        with self.assertRaisesRegex(ProjectBindingError, "private_directory_ancestor_permissions_unsafe"):
            secure_private_directory(target)


if __name__ == "__main__":
    unittest.main()
