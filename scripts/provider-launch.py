#!/usr/bin/env python3
"""Launch one bounded Codex or Grok worker through host-owned adapters.

The delegated prompt is read only from stdin and is never included in stdout.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.provider_adapters import (  # noqa: E402
    ProviderAdapterError,
    discover_codex_models,
    discover_grok_models,
    executable_identity,
    grok_auth_file_identity,
    grok_http_helper_identity,
    launch_codex,
    launch_grok,
)


class _ProviderLaunchSignal(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["codex", "grok"])
    parser.add_argument("--binary", required=True, help="Executable path; no shell fragments.")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--selector")
    parser.add_argument("--resolved-model", help="Exact pre-discovered model ID required for launch.")
    parser.add_argument("--grok-home", type=Path, help="Dedicated owner-only Grok home containing only auth.json.")
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high", "xhigh", "ultra"])
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--max-prompt-tokens", type=int, default=32768)
    parser.add_argument("--max-output-tokens", type=int, default=1400)
    parser.add_argument("--discover-models", action="store_true")
    parser.add_argument("--disable-legacy-landlock-fallback", action="store_true")
    args = parser.parse_args(argv)
    binary = args.binary
    try:
        frozen_executable = executable_identity(binary)
        grok_auth_identity = None
        grok_helper_identity = None
        if args.provider == "grok":
            if args.grok_home is None:
                parser.error("--grok-home is required for Grok discovery and launch")
            grok_auth_identity = grok_auth_file_identity(args.grok_home)
            grok_helper_identity = grok_http_helper_identity()
        if args.discover_models:
            models = (
                discover_codex_models(
                    binary, expected_executable_identity=frozen_executable,
                )
                if args.provider == "codex"
                else discover_grok_models(
                    binary, expected_executable_identity=frozen_executable,
                    grok_home=args.grok_home,
                    expected_grok_auth_file_identity=grok_auth_identity,
                    expected_grok_helper_identity=grok_helper_identity,
                )
            )
            print(json.dumps({"schema": "ProviderModelDiscoveryV1", "provider": args.provider.title(), "models": models}, sort_keys=True))
            return 0
        if not args.project_root or not args.selector or not args.reasoning_effort or not args.resolved_model:
            parser.error("--project-root, --selector, --resolved-model, and --reasoning-effort are required for launch")
        prompt = sys.stdin.read()
        if args.provider == "codex":
            result = launch_codex(binary=binary, project_root=args.project_root, selector=args.selector,
                                  reasoning_effort=args.reasoning_effort, prompt=prompt,
                                  timeout_seconds=args.timeout_seconds,
                                  legacy_landlock_fallback=not args.disable_legacy_landlock_fallback,
                                  resolved_model=args.resolved_model,
                                  expected_executable_identity=frozen_executable)
        else:
            result = launch_grok(binary=binary, project_root=args.project_root, selector=args.selector,
                                reasoning_effort=args.reasoning_effort, prompt=prompt,
                                max_prompt_tokens=args.max_prompt_tokens,
                                max_output_tokens=args.max_output_tokens,
                                timeout_seconds=args.timeout_seconds,
                                resolved_model=args.resolved_model,
                                grok_home=args.grok_home,
                                expected_executable_identity=frozen_executable,
                                expected_auth_file_identity=grok_auth_identity,
                                expected_helper_identity=grok_helper_identity)
    except ProviderAdapterError as exc:
        print(json.dumps({"schema": "ProviderAdapterLaunchV1", "status": "blocked", "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "completed" else 3


if __name__ == "__main__":
    prior_sigterm = signal.getsignal(signal.SIGTERM)

    def _interrupt(signum, _frame):
        raise _ProviderLaunchSignal(signum)

    signal.signal(signal.SIGTERM, _interrupt)
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, _ProviderLaunchSignal) as exc:
        signum = signal.SIGINT if isinstance(exc, KeyboardInterrupt) else exc.signum
        print(json.dumps({
            "schema": "ProviderAdapterLaunchV1", "status": "blocked",
            "reason": f"operator_signal:{signal.Signals(signum).name}",
            "fallback_allowed": False,
        }, sort_keys=True))
        raise SystemExit(4)
    finally:
        signal.signal(signal.SIGTERM, prior_sigterm)
