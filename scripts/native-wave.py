#!/usr/bin/env python3
"""Advisory V2 native wave planner. The host launches and interrupts workers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentfanout.native import NativeWaveError  # noqa: E402
from agentfanout.v2_policy import process_native_wave  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True, help="JSON object or - for stdin")
    parser.add_argument("--telemetry-file", help="Optional host-local metadata ledger")
    args = parser.parse_args()
    try:
        if args.request_json == "-":
            raw = sys.stdin.read(1_048_577)
        else:
            raw = args.request_json
        if len(raw.encode("utf-8")) > 1_048_576:
            raise NativeWaveError("v2_request_too_large")
        request = json.loads(raw)
        response = process_native_wave(request, args.telemetry_file)
        print(json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (NativeWaveError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({"schema": "NativeWaveResponseV2", "status": "blocked", "error": str(exc)}))
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
