#!/usr/bin/env python3
"""Run the experimental, non-enforced Docker policy-shape helper."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.host_runtime import execute_docker_tool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, help="JSON or @file")
    args = parser.parse_args()
    value = Path(args.policy[1:]).read_text() if args.policy.startswith("@") else args.policy
    print(json.dumps(execute_docker_tool(json.loads(value)), indent=2))


if __name__ == "__main__":
    main()
