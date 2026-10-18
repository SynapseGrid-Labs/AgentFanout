"""Diagnostic composition of release authority and the code actually imported.

This view never authorizes a release, provider, or feature. The existing
release_lib manifest/provenance verifier remains the release authority.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_report(root: Path, entrypoint: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    entrypoint = entrypoint.resolve(strict=True)
    if not entrypoint.is_relative_to(root):
        raise ValueError("invoked_entrypoint_outside_runtime")
    # Load only the helper belonging to this invocation, not one on PYTHONPATH.
    helper = root / "scripts" / "release_lib.py"
    spec = importlib.util.spec_from_file_location("_agentfanout_identity_release_lib", helper)
    if spec is None or spec.loader is None:
        raise ValueError("release_authority_unavailable")
    authority = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(authority)
    manifest = authority.manifest_for(root, "runtime")
    release: dict[str, Any] = {"kind": "source", "verified": False}
    if (root / authority.MANIFEST_NAME).exists() or (root / authority.PROVENANCE_NAME).exists():
        try:
            declared_manifest = authority.validate_manifest(root)
            provenance = authority.validate_provenance(root, declared_manifest)
            if root.name.startswith(".stage-"):
                # The existing installer runs checks before freezing/renaming.
                # This diagnostic view verifies its manifest/provenance but
                # cannot claim immutable release authority for that stage.
                release = {"kind": "staged_release", "verified": False,
                           "manifest": declared_manifest, "provenance": provenance}
            else:
                frozen, provenance = authority.validate_frozen_release(root)
                release = {"kind": "immutable_release", "verified": True,
                           "manifest": frozen, "provenance": provenance}
        except authority.ReleaseError as exc:
            raise ValueError("runtime_release_verification_failed:" + str(exc)) from exc
    else:
        # A source tree is a diagnostic candidate, never an installed release.
        try:
            release["source"] = authority.git_identity(root)
        except authority.ReleaseError:
            release["source"] = None
    imported = []
    for name, module in sorted(sys.modules.copy().items()):
        if name != "agentfanout" and not name.startswith("agentfanout."):
            continue
        filename = getattr(module, "__file__", None)
        if not filename:
            raise ValueError("runtime_import_identity_unavailable:" + name)
        path = Path(filename).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("runtime_import_outside_invocation:" + name)
        imported.append({"module": name, "path": str(path), "sha256": _digest(path)})
    features = {}
    for name, path in {
        "native": "agentfanout/native.py", "native_v2": "agentfanout/v2_policy.py",
        "hosted": "agentfanout/runner.py", "grok_cli": "agentfanout/grok_cli.py",
        "coordinator_local": "agentfanout/coordinator_local.py",
        "grok_abi": "agentfanout/grok_abi.py",
        "grok_probe": "agentfanout/grok_probe.py",
        "grok_probe_boundary": "scripts/grok-probe-sandbox.c",
        "grok_probe_container": "agentfanout/grok_probe_container.py",
        "mixed": "agentfanout/mixed_contracts.py",
    }.items():
        target = root / path
        if target.is_file():
            features[name] = {"path": path, "sha256": _digest(target)}
    fingerprint = hashlib.sha256(json.dumps(features, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema": "BuildIdentityReportV1", "product_version": (root / "VERSION").read_text().strip(),
            "runtime_root": str(root), "entrypoint": {"path": str(entrypoint), "sha256": _digest(entrypoint)},
            "interpreter": {"path": str(Path(sys.executable).resolve()), "version": sys.version},
            "imported_modules": imported, "features": features, "feature_fingerprint": fingerprint,
            "runtime_tree_sha256": manifest["tree_sha256"], "release": release,
            "capability_authority": False}
