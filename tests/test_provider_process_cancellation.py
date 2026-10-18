import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentfanout.provider_adapters import ProviderAdapterError, run_json_process


CHILD = """
import json
import os
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(
    json.dumps({"child_pid": os.getpid(), "process_group_id": os.getpgrp()}),
    encoding="utf-8",
)
while True:
    time.sleep(1)
"""


LEADER = """
import subprocess
import sys
import time
from pathlib import Path

subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
deadline = time.monotonic() + 2
while not Path(sys.argv[2]).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(72)
    time.sleep(0.01)
if len(sys.argv) > 3 and sys.argv[3] == "exit-after-spawn":
    raise SystemExit(0)
while True:
    time.sleep(1)
"""


@unittest.skipUnless(os.name == "posix" and hasattr(os, "killpg"), "requires POSIX process groups")
class ProviderProcessCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.child_script = self.root / "stubborn_child.py"
        self.leader_script = self.root / "group_leader.py"
        self.child_script.write_text(CHILD, encoding="utf-8")
        self.leader_script.write_text(LEADER, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _assert_group_gone(self, process_group_id: int) -> None:
        deadline = time.monotonic() + 0.5
        while True:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                self.fail(f"provider process group {process_group_id} survived cancellation")
            time.sleep(0.02)

    def _run_stubborn_group(self, *, cancel: bool) -> tuple[dict, dict]:
        marker = self.root / ("cancelled.json" if cancel else "timed-out.json")
        result = run_json_process(
            [sys.executable, str(self.leader_script), str(self.child_script), str(marker)],
            "",
            self.root,
            timeout_seconds=5 if cancel else 1,
            cancellation_requested=(lambda: marker.exists()) if cancel else None,
        )
        process_data = json.loads(marker.read_text(encoding="utf-8"))
        return result, process_data

    def test_cancellation_kills_group_after_leader_exits_first(self):
        result, process_data = self._run_stubborn_group(cancel=True)

        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["process_group_termination_confirmed"])
        self.assertEqual(result["termination_scope"], "process_group_only")
        self.assertEqual(result["returncode"], -signal.SIGTERM)
        self._assert_group_gone(process_data["process_group_id"])

    def test_timeout_kills_sigterm_ignoring_descendant(self):
        result, process_data = self._run_stubborn_group(cancel=False)

        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["process_group_termination_confirmed"])
        self.assertEqual(result["returncode"], -signal.SIGTERM)
        self._assert_group_gone(process_data["process_group_id"])

    def test_leader_exit_leaves_same_group_descendant_detected_and_killed(self):
        marker = self.root / "detached.json"
        result = run_json_process(
            [
                sys.executable,
                str(self.leader_script),
                str(self.child_script),
                str(marker),
                "exit-after-spawn",
            ],
            "",
            self.root,
            timeout_seconds=5,
        )
        process_data = json.loads(marker.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["process_group_termination_confirmed"])
        self.assertFalse(result["detached_session_descendants_covered"])
        self.assertEqual(result["returncode"], 0)
        self._assert_group_gone(process_data["process_group_id"])

    def test_clean_exit_is_reported_at_process_boundary_before_output_validation(self):
        confirmed = threading.Event()
        result = run_json_process(
            [sys.executable, "-c", "print('not-provider-json')"],
            "",
            self.root,
            timeout_seconds=5,
            on_process_group_exit_confirmed=confirmed.set,
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["process_group_exit_confirmed"])
        self.assertTrue(confirmed.is_set())

    def test_unconfirmed_cancellation_fails_closed(self):
        with patch("agentfanout.provider_adapters.terminate_process_group", return_value=False):
            with self.assertRaisesRegex(
                ProviderAdapterError, "provider_process_group_termination_unconfirmed",
            ):
                run_json_process(
                    [sys.executable, "-c", "import time; time.sleep(0.1)"],
                    "",
                    self.root,
                    timeout_seconds=5,
                    cancellation_requested=lambda: True,
                )

    def test_parent_interrupt_reaps_detached_provider_group_before_propagating(self):
        marker = self.root / "interrupted.json"

        def interrupt_after_child_starts():
            if marker.exists():
                raise KeyboardInterrupt()
            return False

        with self.assertRaises(KeyboardInterrupt):
            run_json_process(
                [
                    sys.executable,
                    str(self.leader_script),
                    str(self.child_script),
                    str(marker),
                ],
                "",
                self.root,
                timeout_seconds=5,
                cancellation_requested=interrupt_after_child_starts,
            )
        process_data = json.loads(marker.read_text(encoding="utf-8"))
        self._assert_group_gone(process_data["process_group_id"])

    def test_real_sigterm_reaps_provider_group_before_wrapper_exits(self):
        marker = self.root / "sigterm.json"
        wrapper = self.root / "sigterm_wrapper.py"
        repository_root = Path(__file__).resolve().parents[1]
        wrapper.write_text(
            "\n".join((
                "import sys",
                f"sys.path.insert(0, {str(repository_root)!r})",
                "from pathlib import Path",
                "from agentfanout.provider_adapters import run_json_process",
                "try:",
                "    run_json_process(",
                "        [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],",
                "        '', Path(sys.argv[4]), timeout_seconds=30,",
                "    )",
                "except BaseException:",
                "    raise SystemExit(23)",
            )) + "\n",
            encoding="utf-8",
        )
        wrapper_process = subprocess.Popen([
            sys.executable, str(wrapper), str(self.leader_script),
            str(self.child_script), str(marker), str(self.root),
        ])
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "provider child did not start")
        process_data = json.loads(marker.read_text(encoding="utf-8"))
        os.kill(wrapper_process.pid, signal.SIGTERM)
        self.assertEqual(wrapper_process.wait(timeout=5), 23)
        self._assert_group_gone(process_data["process_group_id"])

    def test_unconfirmed_timeout_fails_closed(self):
        with patch("agentfanout.provider_adapters.terminate_process_group", return_value=False):
            with self.assertRaisesRegex(
                ProviderAdapterError, "provider_process_group_termination_unconfirmed",
            ):
                run_json_process(
                    [sys.executable, "-c", "import time; time.sleep(1.1)"],
                    "",
                    self.root,
                    timeout_seconds=0.1,
                )


if __name__ == "__main__":
    unittest.main()
