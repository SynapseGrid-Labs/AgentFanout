#!/usr/bin/env python3
"""Emit a bounded public-candidate readiness report without publishing."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "detail": detail[:500]}


def report(
    source: Path, cleanroom_report: Path | None = None,
    *, run_distribution_tests: bool = True,
) -> tuple[int, dict[str, object]]:
    source = source.resolve(strict=True)
    checks: list[dict[str, object]] = []
    version = (source / "VERSION").read_text(encoding="utf-8").strip()
    checks.append(_check("version_metadata", bool(re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version)), version))
    required = [
        "README.md", "LICENSE", "SECURITY.md", "SUPPORT.md", "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md", "requirements.txt", "requirements.lock", "scripts/install.py",
        "scripts/uninstall.py", "scripts/agentfanout.py",
    ]
    missing = [path for path in required if not (source / path).is_file()]
    checks.append(_check("public_surface", not missing, "missing=" + ",".join(missing)))
    allowlist = json.loads((source / "release/public-safe-allowlist.json").read_text(encoding="utf-8"))
    public_files = set(allowlist["profiles"]["public"]["files"])
    checks.append(_check(
        "community_files_allowlisted",
        {"CODE_OF_CONDUCT.md", "VERSION", "requirements.txt", "requirements.lock"}.issubset(public_files),
        "public profile contains required distribution metadata",
    ))
    dry = subprocess.run(
        [sys.executable, str(source / "scripts/install.py"), "--source", str(source), "--dry-run"],
        cwd=source, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    try:
        dry_payload = json.loads(dry.stdout)
    except json.JSONDecodeError:
        dry_payload = {}
    checks.append(_check(
        "installer_dry_run",
        dry.returncode == 0 and dry_payload.get("status") == "dry_run"
        and dry_payload.get("source_clean") is True,
        dry.stderr or dry.stdout,
    ))
    if cleanroom_report is None:
        cleanroom: dict[str, object] = {}
        cleanroom_detail = "a clean-room report for this exact commit is required"
    else:
        if cleanroom_report.is_symlink() or not cleanroom_report.is_file():
            raise ValueError("clean-room report must be a regular file")
        if cleanroom_report.stat().st_size > 1_000_000:
            raise ValueError("clean-room report exceeds the 1 MB limit")
        cleanroom = json.loads(cleanroom_report.read_text(encoding="utf-8"))
        cleanroom_detail = str(cleanroom_report)
    checks.append(_check(
        "cleanroom_install",
        cleanroom.get("schema") == "AgentFanoutCleanroomV1"
        and cleanroom.get("status") == "passed"
        and cleanroom.get("source_commit") == dry_payload.get("source_commit"),
        cleanroom_detail,
    ))
    if run_distribution_tests:
        tests = subprocess.run(
            [sys.executable, "-m", "unittest", "tests/test_public_distribution.py"],
            cwd=source, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
        )
        checks.append(_check("public_distribution_tests", tests.returncode == 0, tests.stdout[-500:]))
    else:
        checks.append(_check("public_distribution_tests", True, "skipped by in-process unit test"))
    passed = all(item["passed"] is True for item in checks)
    return (0 if passed else 2), {
        "schema": "AgentFanoutPublicReadinessV1", "status": "candidate_ready" if passed else "blocked",
        "version": version, "checks": checks,
        "publication_authorized": False,
        "uat_required": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--cleanroom-report", type=Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        code, value = report(args.source, args.cleanroom_report)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        code, value = 2, {
            "schema": "AgentFanoutPublicReadinessV1", "status": "blocked",
            "error": str(exc), "publication_authorized": False, "uat_required": True,
        }
    print(json.dumps(value, indent=2 if args.pretty else None, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
