#!/usr/bin/env python3
"""Fail-closed source/runtime parity verifier for AgentFanout releases.

Exit 0 only when the runtime exactly matches the runtime allowlist, its sorted
manifest and provenance validate, and the selected source mode permits the
source worktree state.  ``diagnostic`` is intentionally the only mode that can
inspect a dirty developer tree.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_lib import (  # noqa: E402
    MANIFEST_NAME, PROVENANCE_NAME, ReleaseError, git_identity, manifest_for,
    records_for_paths, sha256_file, tree_digest as records_tree_digest,
    validate_manifest, validate_provenance,
)


REPORT_VERSION = 2
CORE_FILES = (
    "SKILL.md", "scripts/route.py", "scripts/run-hosted-fanout.py", "agentfanout/__init__.py",
    "agentfanout/lifecycle.py", "agentfanout/host_runtime.py", "agentfanout/runner.py",
    "agentfanout/provider_adapters.py", "agentfanout/calibration.py",
    "agentfanout/provider_registry.py", "providers/models.json", "schemas/NextWavePlanV1.schema.json",
    "schemas/RunStateV2.schema.json", "schemas/TaskUnitV1.schema.json",
    "schemas/WorkerResultV1.schema.json", "schemas/ValidatorLaunchReceiptV1.schema.json",
    "references/CostPolicy.md",
    "references/ImplementationNotes.md", "references/Providers.md", "references/Routing.md",
    "references/SessionModes.md", "workflows/Activate.md", "workflows/Route.md", "workflows/Validate.md",
)
ROUTER_SCHEMA_NAMES = (
    "PROVIDERS", "EFFORTS", "DEFAULT_STATE", "VALID_COMMANDS",
    "LOCAL_DEFAULT_WORKER_ALIAS", "LOCAL_DEFAULT_MODEL_ID",
)
HOST_OVERLAY_PATTERNS = (
    PROVENANCE_NAME, MANIFEST_NAME, ".runtime/**", "config/local/**", "overlays/**",
    "runtime.local.*", "**/*.local.*",
)
IGNORED_PATTERNS = (
    ".git/**", ".pytest_cache/**", "**/.pytest_cache/**", "**/__pycache__/**",
    "**/*.pyc", "**/*.pyo", "**/.DS_Store",
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    source_default = Path(__file__).resolve().parents[1]
    pai_dir = Path(os.environ.get("PAI_DIR") or os.environ.get("PAI_HOME") or Path.home() / ".pai")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=source_default)
    parser.add_argument("--runtime", type=Path, default=pai_dir / "skills" / "AgentFanout")
    parser.add_argument("--mode", choices=("diagnostic", "install", "release"), default="release")
    parser.add_argument("--overlay-pattern", action="append", default=[])
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): normalized(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (set, frozenset, tuple, list)):
        items = [normalized(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True)) if isinstance(value, (set, frozenset)) else items
    return value


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def sha256(path: Path) -> str:
    return sha256_file(path)


def tree_digest(files: dict[str, str]) -> str:
    return records_tree_digest([{"path": name, "sha256": digest, "size": 0} for name, digest in files.items()])


def raw_runtime_map(root: Path, overlay_patterns: tuple[str, ...]) -> tuple[dict[str, str], dict[str, str]]:
    public: dict[str, str] = {}
    overlays: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not (path.is_file() or path.is_symlink()):
            continue
        relative = path.relative_to(root).as_posix()
        if matches_any(relative, IGNORED_PATTERNS):
            continue
        target = overlays if matches_any(relative, overlay_patterns) else public
        if path.is_symlink():
            target[relative] = "symlink-forbidden"
        else:
            target[relative] = sha256(path)
    return public, overlays


def provider_set(root: Path) -> list[str]:
    adapters = root / "adapters"
    return sorted(path.stem for path in adapters.glob("*.md") if path.is_file()) if adapters.is_dir() else []


def router_schema(root: Path) -> dict[str, Any]:
    route_path = root / "scripts" / "route.py"
    if not route_path.is_file():
        return {"error": "route_missing"}
    try:
        module = ast.parse(route_path.read_text(encoding="utf-8"), filename=str(route_path))
    except (OSError, SyntaxError) as error:
        return {"error": f"route_unreadable:{type(error).__name__}"}
    values: dict[str, Any] = {}
    for node in module.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in ROUTER_SCHEMA_NAMES or value_node is None:
                continue
            try:
                values[target.id] = normalized(ast.literal_eval(value_node))
            except (ValueError, TypeError, SyntaxError):
                values[target.id] = {"error": "non_literal"}
    return values


def core_comparison(source: Path, runtime: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for relative in CORE_FILES:
        source_path, runtime_path = source / relative, runtime / relative
        source_hash = sha256(source_path) if source_path.is_file() else None
        runtime_hash = sha256(runtime_path) if runtime_path.is_file() else None
        status = "match" if source_hash and source_hash == runtime_hash else (
            "missing_source" if source_hash is None else "missing_runtime" if runtime_hash is None else "hash_mismatch"
        )
        results.append({"path": relative, "status": status, "source_sha256": source_hash, "runtime_sha256": runtime_hash})
    return results


def changed_files(source_files: dict[str, str], runtime_files: dict[str, str]) -> list[dict[str, Any]]:
    changes = []
    for relative in sorted(set(source_files) | set(runtime_files)):
        source_hash, runtime_hash = source_files.get(relative), runtime_files.get(relative)
        if source_hash == runtime_hash:
            continue
        status = "runtime_only" if source_hash is None else "source_only" if runtime_hash is None else "hash_mismatch"
        changes.append({"path": relative, "status": status, "source_sha256": source_hash, "runtime_sha256": runtime_hash})
    return changes


def git_provenance(root: Path) -> dict[str, Any]:
    try:
        identity = git_identity(root)
    except ReleaseError as error:
        return {"git_root_detected": False, "error": str(error), "head": None, "tree": None, "dirty": None, "dirty_paths": []}
    return {"git_root_detected": True, "head": identity["head"], "tree": identity["tree"], "dirty": identity["dirty"], "dirty_paths": identity["dirty_paths"]}


def installed_provenance(runtime: Path) -> dict[str, Any]:
    try:
        manifest = validate_manifest(runtime)
        provenance = validate_provenance(runtime, manifest)
    except ReleaseError as error:
        return {"present": (runtime / PROVENANCE_NAME).is_file(), "valid": False, "error": str(error)}
    return {"present": True, "valid": True, **provenance}


def build_report(source: Path, runtime: Path, extra_overlay_patterns: Iterable[str] = (), mode: str = "release") -> dict[str, Any]:
    source, runtime = source.expanduser().resolve(), runtime.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source}")
    if not runtime.is_dir():
        raise FileNotFoundError(f"Runtime directory does not exist: {runtime}")
    if mode not in {"diagnostic", "install", "release"}:
        raise ValueError(f"Unknown parity mode: {mode}")

    overlay_patterns = tuple(dict.fromkeys((*HOST_OVERLAY_PATTERNS, *extra_overlay_patterns)))
    source_manifest = manifest_for(source, "runtime")
    source_public = {record["path"]: record["sha256"] for record in source_manifest["files"]}
    runtime_public, runtime_overlays = raw_runtime_map(runtime, overlay_patterns)
    runtime_public.pop(MANIFEST_NAME, None)
    runtime_public.pop(PROVENANCE_NAME, None)
    source_providers, runtime_providers = provider_set(source), provider_set(runtime)
    source_schema, runtime_schema = router_schema(source), router_schema(runtime)
    core, public_drift = core_comparison(source, runtime), changed_files(source_public, runtime_public)
    source_git = git_provenance(source)
    runtime_prov = installed_provenance(runtime)
    critical: list[str] = []
    if source_providers != runtime_providers:
        critical.append("provider_set_drift")
    if source_schema != runtime_schema:
        critical.append("router_schema_drift")
    if any(item["status"] != "match" for item in core):
        critical.append("core_file_drift")
    if public_drift:
        critical.append("public_safe_tree_drift")
    if not runtime_prov.get("valid"):
        critical.append("invalid_or_missing_provenance")
    else:
        provenance = runtime_prov
        if provenance["source_tree_sha256"] != source_manifest["tree_sha256"]:
            critical.append("provenance_source_tree_digest_mismatch")
        if source_git["git_root_detected"]:
            if provenance["source_commit"] != source_git["head"]:
                critical.append("provenance_source_commit_mismatch")
            if provenance["source_git_tree"] != source_git["tree"]:
                critical.append("provenance_source_git_tree_mismatch")
        elif mode != "diagnostic":
            critical.append("source_git_unavailable")
        if mode in {"install", "release"} and source_git.get("dirty"):
            critical.append("source_dirty")
        if mode in {"install", "release"} and provenance["source_dirty"]:
            critical.append("provenance_dirty_source")

    return {
        "schema_version": REPORT_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode, "status": "match" if not critical else "drift", "exit_code": 0 if not critical else 1,
        "source": {"root": str(source), "tree_sha256": source_manifest["tree_sha256"], "public_safe_file_count": len(source_public), "providers": source_providers, "router_schema": source_schema, "git": source_git},
        "runtime": {"root": str(runtime), "tree_sha256": records_tree_digest([{"path": key, "sha256": value, "size": 0} for key, value in runtime_public.items()]), "public_safe_file_count": len(runtime_public), "host_overlay_file_count": len(runtime_overlays), "providers": runtime_providers, "router_schema": runtime_schema, "installed_provenance": runtime_prov},
        "classification": {"host_overlay_patterns": list(overlay_patterns), "runtime_host_overlays": sorted(runtime_overlays)},
        "core_files": core, "public_safe_drift": public_drift, "critical_reasons": critical,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = build_report(args.source, args.runtime, args.overlay_pattern, args.mode)
    except (OSError, ValueError, ReleaseError) as error:
        report = {"schema_version": REPORT_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(), "status": "error", "exit_code": 2, "error": {"type": type(error).__name__, "message": str(error)}}
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
