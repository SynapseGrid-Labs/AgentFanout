from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentfanout.grok_cli import GrokCLIUnavailable
from agentfanout.grok_probe import prepare_intent, prepare_invocation_intent, run_probe, static_elf, supervise

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/grok-probe-sandbox.c"


class ProcessBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="agentfanout-boundary-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.helper, cls.fixture = cls.root / "boundary", cls.root / "fixture"
        for source, target, extra in ((SOURCE, cls.helper, []),
                (ROOT / "tests/grok_probe_fixture.c", cls.fixture, ["-static", "-pthread"])):
            subprocess.run(["/usr/bin/cc", str(source), "-O2", "-Wall", "-Wextra", "-Werror", *extra, "-o", str(target)],
                           check=True, capture_output=True, timeout=30)
        cls.workspace = cls.root / "workspace"; cls.workspace.mkdir()
        config = cls.workspace / "home/.grok"; config.mkdir(parents=True)
        (config / "config.toml").write_text("synthetic profile")
        cls.secret = cls.root / "outside-secret"; cls.secret.write_text("host canary")
        (cls.workspace / "escape").symlink_to(cls.secret)
        with cls.secret.open() as fd:
            p = subprocess.run([str(cls.helper), str(cls.fixture), str(cls.workspace), str(cls.secret), str(fd.fileno()), "checks"],
                               input=b"", capture_output=True, timeout=10, pass_fds=(fd.fileno(),),
                               env={"HOME": str(cls.workspace / "home"), "PATH": ""})
        cls.result = json.loads(p.stdout)
        cls.stderr = p.stderr.decode()
        if p.returncode:
            raise AssertionError((p.returncode, cls.stderr))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_filesystem_content_metadata_and_mutations(self):
        for key in ("read_host_denied", "legacy_open_host_denied", "legacy_opath_denied", "legacy_mkdir_host_denied", "write_host_denied", "write_workspace_denied",
                    "truncate_host_denied", "chmod_host_denied", "stat_host_denied", "opath_host_denied",
                    "symlink_escape_denied", "traversal_denied", "proc_denied", "device_denied"):
            with self.subTest(key=key): self.assertTrue(self.result[key])
        self.assertEqual(self.secret.read_text(), "host canary")
        self.assertFalse((self.workspace / "new-file").exists())

    def test_network_and_alternative_kernel_interfaces(self):
        for key in ("tcp_denied", "udp_denied", "unix_denied", "pair_other_family_denied", "pair_datagram_denied", "pair_protocol_denied",
                    "connect_denied", "bind_denied", "listen_denied", "named_send_denied", "sendmsg_denied", "io_uring_denied",
                    "unknown_syscall_denied", "x32_denied", "ioctl_denied", "process_vm_denied", "limit_raise_denied"):
            with self.subTest(key=key): self.assertTrue(self.result[key])

    def test_no_tools_processes_or_cross_process_effects(self):
        for key in ("fork_denied", "vfork_denied", "clone_process_denied", "clone_high_flags_denied", "signal_other_denied", "ptrace_denied",
                    "exec_other_denied", "exec_memfd_denied", "fcntl_signal_denied", "async_signal_denied"):
            with self.subTest(key=key): self.assertTrue(self.result[key])

    def test_descriptors_credentials_and_readonly_profile(self):
        for key in ("fd_closed", "ambient_key_absent", "profile_read_allowed", "legacy_profile_read_allowed", "thread_allowed", "static_thread_flags_allowed", "private_pair_allowed"):
            with self.subTest(key=key): self.assertTrue(self.result[key])
        self.assertIn("probe_boundary_enforced:", self.stderr)

    def test_timeout_is_reaped_without_replay(self):
        r = supervise([str(self.helper), str(self.fixture), str(self.workspace), str(self.secret), "50", "timeout"],
                      self.workspace, {}, b"", timeout=.15)
        self.assertEqual(r["status"], "timeout")
        self.assertTrue(r["process_reaped"])
        self.assertFalse(r["replay_allowed"])

    def test_output_bound_is_reaped(self):
        r = supervise([str(self.helper), str(self.fixture), str(self.workspace), str(self.secret), "50", "flood"],
                      self.workspace, {}, b"", timeout=2, max_output=1024)
        self.assertEqual(r["status"], "output_limit")
        self.assertTrue(r["process_reaped"])
        self.assertLessEqual(len(r["stdout"].encode()) + len(r["stderr"].encode()), 1024)

    def test_authorization_and_journal_precede_compiler_and_spawn(self):
        expected = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        with patch("agentfanout.grok_probe.subprocess.run") as compile_call, patch("agentfanout.grok_probe.supervise") as spawn:
            with self.assertRaisesRegex(GrokCLIUnavailable, "probe_host_authorization_missing"):
                run_probe(self.fixture, expected, SOURCE, lambda intent: False, lambda *args: True)
            compile_call.assert_not_called(); spawn.assert_not_called()
            with self.assertRaisesRegex(GrokCLIUnavailable, "probe_intent_not_durable"):
                run_probe(self.fixture, expected, SOURCE, lambda intent: True, lambda *args: False)
            compile_call.assert_not_called(); spawn.assert_not_called()

    def test_binary_identity_and_static_format(self):
        self.assertTrue(static_elf(self.fixture.read_bytes()))
        self.assertFalse(static_elf(Path("/usr/bin/true").read_bytes()))
        self.assertFalse(static_elf(b"#!/bin/sh\n"))
        with self.assertRaisesRegex(GrokCLIUnavailable, "identity_or_format"):
            prepare_intent(self.fixture, "0" * 64, SOURCE)

    def test_unsupported_boundary_never_executes(self):
        # A non-pipe stdin must fail before exec, even if all kernel primitives
        # are available. Exit 125 distinguishes boundary denial from the child.
        with open(os.devnull, "rb") as fd:
            p = subprocess.run([str(self.helper), str(self.fixture), str(self.workspace)],
                               stdin=fd, capture_output=True, timeout=3)
        self.assertEqual(p.returncode, 125)
        self.assertIn(b"stdio_not_pipe", p.stderr)

    def test_real_spawn_order_clean_environment_and_isolation(self):
        expected = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        observations = []
        events = []
        def authorize(intent):
            events.append("host_authorized")
            return intent["inference_requests"] == 0
        def journal(kind, payload):
            events.append(kind)
            return True
        with patch.dict(os.environ, {"XAI_API_KEY": "synthetic-key", "LD_PRELOAD": "forbidden.so"}):
            for _ in range(2):
                observations.append(run_probe(self.fixture, expected, SOURCE, authorize, journal))
        self.assertEqual(events, ["host_authorized", "probe_start_intent", "probe_observation"] * 2)
        homes = []
        for r in observations:
            self.assertTrue(r["boundary_enforced"])
            self.assertIsNone(r["failure"])
            self.assertTrue(r["process_reaped"])
            self.assertFalse(r["admission_allowed"])
            homes.append(r["frames"][0]["result"]["home"])
            self.assertFalse(Path(homes[-1]).exists())
            self.assertEqual(r["disposable_files_after"], ["home", "home/.grok", "home/.grok/config.toml"])
        self.assertNotEqual(*homes)

    def test_malformed_wrong_session_callbacks_and_duplicate_frames(self):
        for mode, failure in (("malformed", "malformed_frame"), ("duplicate-key", "malformed_frame"),
                              ("callback", "unexpected_frame_or_callback"),
                              ("wrong-id", "unexpected_frame_or_callback"), ("duplicate", "response_correlation")):
            with self.subTest(mode=mode):
                r = supervise([str(self.helper), str(self.fixture), str(self.workspace), str(self.secret), "50", mode],
                              self.workspace, {}, b"", timeout=2)
                self.assertEqual(r["failure"], failure)
                self.assertTrue(r["process_reaped"])
                self.assertFalse(r["admission_allowed"])

    def test_crash_reconciliation_never_authorizes_replay(self):
        r = supervise([str(self.helper), str(self.fixture), str(self.workspace), str(self.secret), "50", "crash"],
                      self.workspace, {}, b"", timeout=2)
        self.assertEqual(r["returncode"], 9)
        self.assertFalse(r["frames"])
        self.assertFalse(r["replay_allowed"])
        self.assertTrue(r["boundary_enforced"])

    def test_cli_host_journal_is_exclusive_and_preserves_history(self):
        with tempfile.TemporaryDirectory(prefix="agentfanout-probe-authority-") as temporary:
            root = Path(temporary)
            expected = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
            intent = prepare_intent(self.fixture, expected, SOURCE)
            auth = root / "authorization.json"
            evidence = root / "evidence"
            invocation = prepare_invocation_intent(intent, auth, evidence)
            receipt = {"schema": "GrokBootstrapProbeAuthorizationV3", "authorization_id": "synthetic-host-1",
                       "scope": "credential_free_network_denied_initialize_only",
                       "intent_sha256": hashlib.sha256(json.dumps(invocation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            auth.write_text(json.dumps(receipt)); auth.chmod(0o600)
            argv = [sys.executable, str(ROOT / "scripts/grok-abi-probe.py"), "--binary", str(self.fixture),
                    "--sha256", expected, "--authorization", str(auth), "--evidence-directory", str(evidence)]
            p = subprocess.run(argv, capture_output=True, timeout=5)
            self.assertEqual(p.returncode, 0, p.stderr)
            path = evidence / "probe-journal.jsonl"
            old = path.read_bytes()
            records = [json.loads(line) for line in old.splitlines()]
            self.assertEqual([r["kind"] for r in records],
                             ["host_authorization_observed", "host_invocation_bound", "probe_start_intent", "probe_observation"])
            self.assertEqual(records[2]["payload"], intent)
            second = subprocess.run(argv, capture_output=True, timeout=5)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(path.read_bytes(), old)
            # Simulate crash after durable intent: retained history still refuses.
            path.write_bytes(b"\n".join(old.splitlines()[:2]) + b"\n")
            crash_history = path.read_bytes()
            third = subprocess.run(argv, capture_output=True, timeout=5)
            self.assertEqual(third.returncode, 2)
            self.assertEqual(path.read_bytes(), crash_history)
            # Changing the evidence directory or copying the receipt cannot
            # create another effect; a deleted journal still leaves consumption.
            altered = argv.copy(); altered[-1] = str(root / "other-evidence")
            moved = subprocess.run(altered, capture_output=True, timeout=5)
            self.assertEqual(moved.returncode, 2)
            self.assertFalse((root / "other-evidence").exists())
            copy = root / "copied-authority.json"; copy.write_bytes(auth.read_bytes()); copy.chmod(0o600)
            altered = argv.copy(); altered[altered.index("--authorization")+1] = str(copy)
            self.assertEqual(subprocess.run(altered, capture_output=True, timeout=5).returncode, 2)
            path.unlink()
            self.assertEqual(subprocess.run(argv, capture_output=True, timeout=5).returncode, 2)
            self.assertFalse(path.exists())
            self.assertTrue(auth.with_name(auth.name+".consumed.json").exists())

    def test_unsupported_landlock_abi_denies_before_exec(self):
        modified = SOURCE.read_text().replace("int abi=syscall(SYS_landlock_create_ruleset,NULL,0,LANDLOCK_CREATE_RULESET_VERSION);", "int abi=2;")
        helper = self.root / "unsupported-boundary"
        subprocess.run(["/usr/bin/cc", "-x", "c", "-", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(helper)],
                       input=modified.encode(), capture_output=True, check=True, timeout=10)
        p = subprocess.run([str(helper), str(self.fixture), str(self.workspace)], input=b"", capture_output=True, timeout=3)
        self.assertEqual(p.returncode, 125)
        self.assertIn(b"landlock_abi", p.stderr)
        self.assertFalse(p.stdout)


if __name__ == "__main__":
    unittest.main()
