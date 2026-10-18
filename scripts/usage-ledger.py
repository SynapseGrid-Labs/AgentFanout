#!/usr/bin/env python3
"""Append sanitized host request deltas or summarize a task; no provider calls."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentfanout.v2_telemetry import record_event, summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.run_id:
        print(json.dumps(summarize(args.ledger, args.run_id), allow_nan=False))
    else:
        try:
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                raise ValueError("event too large")
            ok = record_event(args.ledger, json.loads(raw))
        except (ValueError, TypeError):
            ok = False
        print(json.dumps({"recorded": ok, "task_may_continue": True}))


if __name__ == "__main__":
    main()
