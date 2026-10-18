#!/usr/bin/env python3
"""Build and atomically promote a verified AgentFanout runtime release.

This program never merges a source tree into an existing runtime.  It copies the
runtime allowlist to a same-filesystem stage, verifies it, freezes it, and only
then switches the active path through an atomic symlink replacement.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from release_lib import (
    INSTALLER_VERSION, MANIFEST_NAME, PROVENANCE_NAME, ReleaseError, atomic_switch,
    archive_directory, copy_release_files, ensure_regular_directory, git_identity,
    make_immutable, make_writable, manifest_for, runtime_transaction_lock, sha256_file,
    utc_now, validate_frozen_release, validate_manifest, validate_provenance, write_json,
    write_state,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.provider_adapters import terminate_process_group


def default_skills_root() -> Path:
    pai_dir = Path(os.environ.get("PAI_DIR") or os.environ.get("PAI_HOME") or Path.home() / ".pai")
    return pai_dir / "skills"


def run_checked(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    timeout_seconds: float = 240.0,
) -> None:
    """Run one release check with bounded time, output, and process scope."""

    orphaned_group_detected = False
    orphaned_group_termination_confirmed: bool | None = None
    with tempfile.TemporaryFile(mode="w+b") as output:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise ReleaseError(f"Release check could not launch ({' '.join(command)}): {exc}") from exc
        try:
            process.wait(timeout=max(0.05, float(timeout_seconds)))
        except subprocess.TimeoutExpired as exc:
            terminated = terminate_process_group(process, process.pid)
            raise ReleaseError(
                f"Release check timed out after {timeout_seconds}s; "
                f"process_group_exit_confirmed={terminated} ({' '.join(command)})"
            ) from exc
        except KeyboardInterrupt as exc:
            terminated = terminate_process_group(process, process.pid)
            raise ReleaseError(
                f"Release check interrupted; process_group_exit_confirmed={terminated} "
                f"({' '.join(command)})"
            ) from exc
        if os.name == "posix" and hasattr(os, "killpg"):
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                orphaned_group_detected = True
            else:
                orphaned_group_detected = True
            if orphaned_group_detected:
                # The session leader already exited, so any surviving member is
                # forbidden background work. Kill the original group and fail
                # this check regardless of the leader's return code.
                orphaned_group_termination_confirmed = terminate_process_group(process, process.pid)
        output.flush()
        output.seek(0, os.SEEK_END)
        size = output.tell()
        output.seek(max(0, size - 1_000_000))
        captured = output.read().decode("utf-8", errors="replace")
    if orphaned_group_detected:
        raise ReleaseError(
            f"Release check left a background process group; "
            f"process_group_exit_confirmed={orphaned_group_termination_confirmed} "
            f"leader_returncode={process.returncode} ({' '.join(command)}):\n{captured}"
        )
    if process.returncode:
        raise ReleaseError(f"Pre/post-switch check failed ({' '.join(command)}):\n{captured}")


def run_release_checks(stage: Path, source: Path) -> None:
    """Run release activation checks without ever writing into ``stage``.

    The same function is used before freezing a staged tree and after switching
    the active symlink.  Python otherwise writes bytecode beside imports, so a
    dedicated cache keeps post-switch verification valid for immutable releases.
    The installer-pipeline test is deliberately CI/preflight-only: recursively
    invoking it here would make a runtime activation create another promotion.
    """
    py_files = sorted(str(path.relative_to(stage)) for path in stage.rglob("*.py"))
    with tempfile.TemporaryDirectory(prefix="agentfanout-pycache-") as pycache:
        env = {
            **os.environ,
            "PYTHONPYCACHEPREFIX": pycache,
            # Full staged discovery includes the release-pipeline tests. Those
            # tests invoke this installer and would recursively promote again.
            "AGENT_FANOUT_STAGE_CHECKS": "1",
            # Distribution tests need the source checkout because an immutable
            # staged runtime intentionally has no .git directory.
            "AGENT_FANOUT_TEST_SOURCE_ROOT": str(source),
        }
        run_checked([sys.executable, "tests/run_tests.py"], stage, env)
        run_checked([sys.executable, "-m", "py_compile", *py_files], stage, env)
        run_checked([
            sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
            "discover", "-s", "tests", "-p", "test_*.py",
        ], stage, env)
        run_checked([sys.executable, "-m", "unittest", "tests/test_runtime_parity.py"], stage, env)
        run_checked([sys.executable, "-m", "unittest", "tests/test_vllmworker_broker_smoke.py"], stage, env)
        run_checked([sys.executable, "-m", "unittest", "tests/test_host_runtime.py"], stage, env)
        run_checked([sys.executable, "-m", "unittest", "tests/test_local_hosted_adapter.py"], stage, env)
        run_checked([sys.executable, "-m", "unittest", "tests/test_telemetry.py"], stage, env)
        run_checked([
            sys.executable, "scripts/check-runtime-parity.py", "--source", str(source),
            "--runtime", str(stage), "--mode", "diagnostic",
        ], stage, env)


def release_id_for(identity: dict[str, Any], tree_sha256: str) -> str:
    return f"{identity['head'][:12]}-{tree_sha256[:12]}"


def validate_source_stability(
    source: Path,
    expected_identity: dict[str, Any],
    expected_manifest: dict[str, Any],
) -> None:
    """Reject a source worktree or commit that changed during release assembly."""

    current_identity = git_identity(source)
    current_manifest = manifest_for(source, "runtime")
    if current_identity != expected_identity or current_manifest != expected_manifest:
        raise ReleaseError("Source identity or runtime bytes changed during release assembly")


def validate_reusable_release(
    release: Path,
    *,
    release_id: str,
    identity: dict[str, Any],
    source_tree_sha256: str,
    review_ref: str | None,
    promotion_requested: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prove an existing frozen stage is the exact release being requested."""

    manifest, provenance = validate_frozen_release(release)
    expected = {
        "release_id": release_id,
        "source_commit": identity["head"],
        "source_git_tree": identity["tree"],
        "source_dirty": identity["dirty"],
        "source_dirty_paths": identity["dirty_paths"],
        "source_tree_sha256": source_tree_sha256,
        "release_tree_sha256": manifest["tree_sha256"],
    }
    if any(provenance.get(key) != value for key, value in expected.items()):
        raise ReleaseError("Existing immutable release does not match the requested source identity")
    if manifest["tree_sha256"] != source_tree_sha256:
        raise ReleaseError("Existing immutable release does not match the requested runtime tree")
    if promotion_requested and (
        not review_ref or provenance.get("review_ref") == "diagnostic-stage-only"
    ):
        raise ReleaseError(
            "Existing diagnostic stage is not promotable; stage again with the exact --review-ref"
        )
    if review_ref is not None and provenance.get("review_ref") != review_ref:
        raise ReleaseError("Existing immutable release review reference does not match")
    return manifest, provenance


def _install_runtime_locked(
    source: Path,
    skills_root: Path,
    review_ref: str | None,
    *,
    stage_only: bool = False,
    dry_run: bool = False,
    allow_dirty: bool = False,
    run_checks: Callable[[Path, Path], None] = run_release_checks,
    post_switch_checks: Callable[[Path, Path], None] = run_release_checks,
) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_dir():
        raise ReleaseError(f"Source directory is missing: {source}")
    identity = git_identity(source)
    if identity["dirty"] and not allow_dirty:
        raise ReleaseError("Refusing dirty or unreviewed source; commit/review it first")
    if allow_dirty and not (stage_only or dry_run):
        raise ReleaseError("--allow-dirty is diagnostic-only and requires --stage-only or --dry-run")
    if not (stage_only or dry_run) and not review_ref:
        raise ReleaseError("Refusing unreviewed promotion: --review-ref is required")
    if review_ref is not None and not review_ref.strip():
        raise ReleaseError("--review-ref must not be blank")

    # The source tree digest is calculated from the exact same copied profile as
    # the installed release, not from git's broader repository tree.
    source_manifest = manifest_for(source, "runtime")
    release_id = release_id_for(identity, source_manifest["tree_sha256"])
    plan = {
        "source_commit": identity["head"], "source_git_tree": identity["tree"],
        "source_dirty": identity["dirty"], "source_dirty_paths": identity["dirty_paths"],
        "release_id": release_id, "source_tree_sha256": source_manifest["tree_sha256"],
        "skills_root": str(skills_root), "stage_only": stage_only, "dry_run": dry_run,
    }
    if dry_run:
        return {"status": "dry_run", **plan}

    skills_root = skills_root.resolve()
    releases = skills_root / ".agentfanout-releases"
    archives = skills_root / ".agentfanout-archives"
    active = skills_root / "AgentFanout"
    ensure_regular_directory(skills_root, "Skills root")
    ensure_regular_directory(releases, "Release root")
    ensure_regular_directory(archives, "Archive root")
    if os.stat(skills_root).st_dev != os.stat(releases).st_dev:
        raise ReleaseError("Release stage must share the active skill filesystem")
    release = releases / release_id
    reused_existing_release = release.exists() or release.is_symlink()
    already_active = False
    if reused_existing_release:
        validate_reusable_release(
            release,
            release_id=release_id,
            identity=identity,
            source_tree_sha256=source_manifest["tree_sha256"],
            review_ref=review_ref,
            promotion_requested=not stage_only,
        )
        if not stage_only and active.is_symlink():
            try:
                already_active = active.resolve(strict=True) == release.resolve(strict=True)
            except OSError as error:
                raise ReleaseError("Active runtime link is unreadable") from error
        if not stage_only and not already_active:
            # Revalidate executable behavior immediately before activation;
            # manifest/provenance parity alone is not a release check.
            run_checks(release, source)
    else:
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=releases))
        try:
            manifest = copy_release_files(source, stage)
            write_json(stage / MANIFEST_NAME, manifest)
            provenance = {
                "schema_version": 1,
                "installer_version": INSTALLER_VERSION,
                "tool_version": "1",
                "release_id": release_id,
                "review_ref": review_ref or "diagnostic-stage-only",
                "source_commit": identity["head"],
                "source_git_tree": identity["tree"],
                "source_dirty": identity["dirty"],
                "source_dirty_paths": identity["dirty_paths"],
                "source_tree_sha256": source_manifest["tree_sha256"],
                "release_tree_sha256": manifest["tree_sha256"],
                "release_manifest_sha256": sha256_file(stage / MANIFEST_NAME),
                "created_at": utc_now(),
            }
            write_json(stage / PROVENANCE_NAME, provenance)
            validate_manifest(stage)
            validate_provenance(stage, manifest)
            run_checks(stage, source)
            os.replace(stage, release)
            make_immutable(release)
        except Exception:
            if stage.exists():
                make_writable(stage)
                shutil.rmtree(stage, ignore_errors=True)
            raise

    # Recheck after executable checks and after freezing. This prevents a check
    # from producing a successful but mutated or non-promotable stage.
    try:
        validate_source_stability(source, identity, source_manifest)
        validate_reusable_release(
            release,
            release_id=release_id,
            identity=identity,
            source_tree_sha256=source_manifest["tree_sha256"],
            review_ref=review_ref,
            promotion_requested=not stage_only,
        )
    except Exception:
        # A newly built release that fails its post-check freeze proof is not a
        # reusable stage. Remove only the directory created by this invocation;
        # never delete a pre-existing or symlinked path on validation failure.
        if not reused_existing_release and release.is_dir() and not release.is_symlink():
            make_writable(release)
            shutil.rmtree(release, ignore_errors=True)
        raise

    if stage_only:
        return {
            "status": "staged", "release": str(release),
            "reused_existing_release": reused_existing_release, **plan,
        }

    if already_active:
        # Never execute release checks against the live tree. A repeated
        # promotion of the exact reviewed candidate is a verified no-op.
        return {
            "status": "already_active", "release": str(release), "active": str(active),
            "previous_target": str(release), "legacy_archive": None,
            "reused_existing_release": True, **plan,
        }

    previous_target: Path | None = None
    legacy_archive: Path | None = None
    if active.is_symlink():
        previous_target = Path(os.path.realpath(active))
    elif active.exists():
        if not active.is_dir():
            raise ReleaseError(f"Active runtime is not a directory or symlink: {active}")
        legacy_archive = archive_directory(active, archives)
        previous_target = legacy_archive
    try:
        # Keep the final verification adjacent to the atomic switch. Legacy
        # archival can be lengthy, so validation before that operation is not
        # sufficiently close to activation.
        validate_source_stability(source, identity, source_manifest)
        validate_reusable_release(
            release,
            release_id=release_id,
            identity=identity,
            source_tree_sha256=source_manifest["tree_sha256"],
            review_ref=review_ref,
            promotion_requested=True,
        )
        atomic_switch(active, release)
        post_switch_checks(active, source)
        # Post-switch checks execute from the active tree. Treat them as
        # untrusted with respect to release integrity and prove both the source
        # identity and active bytes again before recording promotion success.
        validate_source_stability(source, identity, source_manifest)
        validate_reusable_release(
            release,
            release_id=release_id,
            identity=identity,
            source_tree_sha256=source_manifest["tree_sha256"],
            review_ref=review_ref,
            promotion_requested=True,
        )
        write_state(releases, release, previous_target)
    except Exception as error:
        if previous_target is not None:
            atomic_switch(active, previous_target)
        elif active.is_symlink():
            active.unlink()
        if not reused_existing_release and release.is_dir() and not release.is_symlink():
            make_writable(release)
            shutil.rmtree(release, ignore_errors=True)
        raise ReleaseError(f"Post-switch verification failed; rollback completed: {error}") from error
    return {
        "status": "promoted", "release": str(release), "active": str(active),
        "previous_target": str(previous_target) if previous_target else None,
        "legacy_archive": str(legacy_archive) if legacy_archive else None,
        "reused_existing_release": reused_existing_release, **plan,
    }


def install_runtime(
    source: Path,
    skills_root: Path,
    review_ref: str | None,
    *,
    stage_only: bool = False,
    dry_run: bool = False,
    allow_dirty: bool = False,
    run_checks: Callable[[Path, Path], None] = run_release_checks,
    post_switch_checks: Callable[[Path, Path], None] = run_release_checks,
) -> dict[str, Any]:
    """Plan without mutation, or serialize the complete staged install transaction."""

    if dry_run:
        return _install_runtime_locked(
            source, skills_root, review_ref, stage_only=stage_only, dry_run=True,
            allow_dirty=allow_dirty, run_checks=run_checks,
            post_switch_checks=post_switch_checks,
        )
    with runtime_transaction_lock(skills_root) as locked_root:
        return _install_runtime_locked(
            source, locked_root, review_ref, stage_only=stage_only, dry_run=False,
            allow_dirty=allow_dirty, run_checks=run_checks,
            post_switch_checks=post_switch_checks,
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--skills-root", type=Path, default=default_skills_root())
    parser.add_argument("--review-ref", help="Required review/audit reference for a live promotion.")
    parser.add_argument("--stage-only", action="store_true", help="Build, verify, and freeze a release without switching runtime.")
    parser.add_argument("--dry-run", action="store_true", help="Validate source/release identity without writing files.")
    parser.add_argument("--allow-dirty", action="store_true", help="Diagnostic-only; permitted only with --stage-only or --dry-run.")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = install_runtime(
            args.source, args.skills_root, args.review_ref, stage_only=args.stage_only,
            dry_run=args.dry_run, allow_dirty=args.allow_dirty,
        )
    except ReleaseError as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2 if args.pretty else None, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
