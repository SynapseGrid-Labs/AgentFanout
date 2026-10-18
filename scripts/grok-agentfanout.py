#!/usr/bin/env python3
"""Task-scoped Grok frontend; no Codex dependency or global startup mutation."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentfanout.grok_cli import capability_status, input_digest, GrokCLIUnavailable


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("native", "grok_cli_subscription_acp"), default="native")
    parser.add_argument("command", choices=("status", "task"))
    parser.add_argument("text", nargs="?")
    args = parser.parse_args(argv)
    if args.profile == "grok_cli_subscription_acp":
        result = capability_status()
        result["schema"] = "GrokCLIProfileStatusV1"
        print(json.dumps(result, sort_keys=True))
        return 2 if args.command == "task" else 0
    if args.command == "status":
        result = {"profile": "native", "enforcement": "advisory", "task_scoped": True,
                  "startup_bytes": 0, "host_capabilities": "must_handshake"}
    else:
        try:
            digest = input_digest(args.text)
        except GrokCLIUnavailable as exc:
            parser.error(str(exc))
        result = {"profile": "native", "enforcement": "advisory", "input_sha256": digest,
                  "task": args.text, "status": "awaiting_native_host_handshake",
                  "instructions": "Use bounded native task packets; collect compact evidence and residual work. Validation is risk-based. No inference has been launched."}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
