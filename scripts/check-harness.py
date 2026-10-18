#!/usr/bin/env python3
"""Lint a host manifest and bound conformance trace; pins add operator trust."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.harness import HarnessContractError, check_conformance  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--require-level", choices=["advisory", "runner_enforced", "harness_enforced"])
    parser.add_argument("--trust-authority", action="append", default=[])
    parser.add_argument("--trust-manifest-sha256", action="append", default=[])
    parser.add_argument("--trust-trace-sha256", action="append", default=[])
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        events = json.loads(args.events.read_text(encoding="utf-8"))
        if not isinstance(events, list):
            raise HarnessContractError("conformance_events_must_be_array")
        outcome = check_conformance(
            manifest, events, trusted_authorities=args.trust_authority,
            trusted_manifest_digests=args.trust_manifest_sha256,
            trusted_trace_digests=args.trust_trace_sha256,
            require_level=args.require_level,
        )
    except (OSError, json.JSONDecodeError, HarnessContractError) as exc:
        print(json.dumps({"schema": "HarnessConformanceResultV1", "passed": False, "failures": [str(exc)]}, sort_keys=True))
        return 2
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
