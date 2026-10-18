import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class VLLMWorkerBrokerSmokeTests(unittest.TestCase):
    def test_broker_import_and_self_check(self):
        if os.getenv("AGENT_FANOUT_STAGE_CHECKS") == "1":
            self.skipTest(
                "immutable release checks are hermetic; the top-level preflight runs this optional host integration once"
            )
        pai_dir = Path(os.environ.get("PAI_DIR") or os.environ.get("PAI_HOME") or Path.home() / ".pai")
        explicit_path = os.environ.get("VLLM_AGENT_PATH")
        agent = Path(explicit_path or pai_dir / "skills" / "VLLMWorker" / "Tools" / "VLLMAgent.ts")
        if not agent.is_file() and not explicit_path:
            self.skipTest("VLLMWorker is not installed on this host")
        self.assertTrue(agent.is_file(), f"Configured VLLMAgent is missing: {agent}")
        if not shutil.which("bun") and not explicit_path:
            self.skipTest("bun is not installed on this host")
        self.assertIsNotNone(shutil.which("bun"), "bun is required for the configured VLLMAgent")
        proc = subprocess.run(
            ["bun", str(agent), "--self-check", "--json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["broker"], "vllmworker-local")
        self.assertEqual(payload["provider_dependencies"], [])
        self.assertFalse(payload["capability_validation"]["validated"])


if __name__ == "__main__":
    unittest.main()
