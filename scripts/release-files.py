#!/usr/bin/env python3
"""Print the fail-closed AgentFanout public/runtime release file set."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from release_lib import ReleaseError, collect_allowed_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--profile", choices=("public", "runtime"), default="public")
    parser.add_argument("--null", action="store_true", help="Use NUL delimiters for safe shell consumption.")
    args = parser.parse_args(argv)
    try:
        files = collect_allowed_paths(args.source.resolve(), args.profile)
    except ReleaseError as error:
        print(f"release file selection failed: {error}", file=sys.stderr)
        return 2
    separator = "\0" if args.null else "\n"
    sys.stdout.write(separator.join(files))
    if files:
        sys.stdout.write(separator)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
