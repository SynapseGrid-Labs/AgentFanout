"""Measure isolated hook subprocess latency, not agent task performance."""
import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins/agentfanout/hooks/agentfanout_hook.py"


def measure(samples=30):
    if not 3 <= samples <= 1000:
        raise ValueError("samples must be between 3 and 1000")
    values = {"baseline": [], "v2": []}
    with tempfile.TemporaryDirectory(prefix="agentfanout-hook-bench-") as directory:
        for index in range(samples):
            for arm in (("baseline", "v2") if index % 2 == 0 else ("v2", "baseline")):
                state = Path(directory) / arm
                state.mkdir(exist_ok=True, mode=0o700)
                env = {**os.environ, "PLUGIN_DATA": str(state),
                       "AGENT_FANOUT_V2_MASTER": "1" if arm == "v2" else "0",
                       "AGENT_FANOUT_V2_HOOK": "1" if arm == "v2" else "0"}
                event = {"session_id": "benchmark", "hook_event_name": "PreToolUse",
                         "cwd": str(ROOT), "tool_name": "Read", "tool_input": {}}
                start = time.perf_counter_ns()
                result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                                        text=True, capture_output=True, env=env, timeout=10, check=True)
                values[arm].append((time.perf_counter_ns() - start) / 1_000_000)
                if json.loads(result.stdout) != {}:
                    raise ValueError("unexpected no-lease hook response")
    return {"schema": "AgentFanoutHookMicrobenchmarkV2", "measured": True,
            "scope": "no-lease PreToolUse subprocess elapsed including startup",
            "samples_per_arm": samples, "hook_sha256": hashlib.sha256(HOOK.read_bytes()).hexdigest(),
            "median_ms": {arm: statistics.median(times) for arm, times in values.items()},
            "lock_wait_ms": None, "end_to_end_agent_savings_measured": False,
            "limitations": ["No active lease or contention workload; does not isolate lock wait.",
                            "No model calls, model quality, or complete-task costs measured."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    print(json.dumps(measure(parser.parse_args().samples), indent=2))
