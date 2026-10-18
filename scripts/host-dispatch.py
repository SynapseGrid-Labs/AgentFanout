#!/usr/bin/env python3
"""Operate the mandatory AgentFanout host enforcement wrapper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentfanout.host_runtime import HostDispatcher, HostLedger
from agentfanout.harness import manifest_digest, validate_manifest
from agentfanout.lifecycle import load_run_state, save_run_state
from agentfanout.project_binding import (
    default_private_state_directory, path_within, validate_project_binding,
)
from agentfanout.telemetry import LangfuseHttpSink


DEFAULT_HOST_MANIFEST = ROOT / "integrations" / "codex" / "standalone-runner-manifest.json"


def read_trusted_text(
    path: Path,
    *,
    max_bytes: int,
    private: bool = False,
) -> tuple[str, dict[str, object]]:
    """Read one trust-root file without following or racing its final path."""

    candidate = path.expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise ValueError("trusted_file_open_denied") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("trusted_file_not_regular_single_link")
        if before.st_uid != os.geteuid():
            raise ValueError("trusted_file_owner_untrusted")
        mode = stat.S_IMODE(before.st_mode)
        if private:
            if mode & 0o077:
                raise ValueError("trusted_private_file_permissions_unsafe")
        elif mode & 0o022:
            raise ValueError("trusted_file_writable_by_group_or_other")
        if before.st_size > max_bytes:
            raise ValueError("trusted_file_too_large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("trusted_file_too_large")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise ValueError("trusted_file_changed_during_read")
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("trusted_file_utf8_required") from exc
    return text, {
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": f"{mode:04o}",
        "owner_uid": before.st_uid,
    }


def validated_context(args):
    state = load_run_state(args.state_file)
    project_root = validate_project_binding(state.get("project_binding"))
    state_path = args.state_file.expanduser().resolve(strict=True)
    ledger_path = args.ledger.expanduser().resolve(strict=False)
    if any(path == project_root or path_within(path, project_root) for path in (state_path, ledger_path)):
        raise ValueError("state_artifact_inside_project_denied")
    manifest_text, manifest_identity = read_trusted_text(
        args.host_manifest, max_bytes=1_000_000,
    )
    manifest = json.loads(manifest_text)
    validate_manifest(manifest, require_level="runner_enforced")
    adapters = {item["provider"]: item for item in manifest["provider_adapters"]}
    return state, manifest, adapters, manifest_identity


def enforced_dispatcher(ledger, manifest, adapters, **kwargs):
    return HostDispatcher(
        ledger,
        require_capability=True,
        require_adapter_identity=True,
        host_manifest_sha256=manifest_digest(manifest),
        adapter_manifests=adapters,
        **kwargs,
    )


def read_json(value: str, max_bytes: int | None = None):
    if value.startswith("@"):
        path = Path(value[1:])
        if max_bytes is not None and path.stat().st_size > max_bytes:
            raise ValueError("json_input_too_large")
        content = path.read_text(encoding="utf-8")
        if max_bytes is not None and len(content.encode("utf-8")) > max_bytes:
            raise ValueError("json_input_too_large")
        return json.loads(content)
    if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
        raise ValueError("json_input_too_large")
    return json.loads(value)


def run_requeue_exclusive(args) -> int:
    """Serialize requeue load/check/ledger/state persistence as one host operation."""

    state, manifest, adapters, manifest_identity = validated_context(args)
    ledger = HostLedger(args.ledger)
    try:
        with ledger.operation_lock():
            # Reload under the cross-process operation lock.  The earlier load
            # establishes path authority only; it is never used for mutation.
            state, _manifest_check, _adapter_check, manifest_check_identity = validated_context(args)
            if manifest_check_identity != manifest_identity:
                raise ValueError("host_manifest_changed_during_operation")
            requeue = read_json(args.requeue_json, max_bytes=4096)
            if not isinstance(requeue, dict) or set(requeue) != {"task_id", "reason"}:
                raise ValueError("requeue_json_object_required")
            dispatcher = enforced_dispatcher(ledger, manifest, adapters)
            event_id = dispatcher.requeue_cancelled_task(
                state, requeue["task_id"], requeue["reason"]
            )
            save_run_state(args.state_file, state)
        print(json.dumps({
            "schema": "HostDispatchOperationV1", "run_id": state["run_id"],
            "status": "ok", "requeue_event_id": event_id,
            "trust_file_identities": {"host_manifest": manifest_identity},
        }, indent=2 if args.pretty else None, sort_keys=True))
        return 0
    finally:
        ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument(
        "--ledger", type=Path,
        default=(Path(os.environ["AGENT_FANOUT_HOST_LEDGER"]).expanduser()
                 if os.environ.get("AGENT_FANOUT_HOST_LEDGER") else None),
    )
    parser.add_argument("--host-manifest", type=Path, default=DEFAULT_HOST_MANIFEST)
    parser.add_argument("--results-json")
    parser.add_argument("--usage-json")
    parser.add_argument("--validator-results-json")
    parser.add_argument("--validator-launch-receipt-json")
    parser.add_argument("--launch-receipt-json")
    parser.add_argument("--capability-json")
    parser.add_argument("--freeze-providers-json")
    parser.add_argument("--heartbeat-json")
    parser.add_argument("--cancel-json")
    parser.add_argument(
        "--requeue-json",
        help="One confirmed-cancellation requeue object with task_id and operator reason.",
    )
    parser.add_argument(
        "--local-public-key-file",
        type=Path,
        default=Path(os.environ["AGENT_FANOUT_LOCAL_PUBLIC_KEY_FILE"]).expanduser()
        if os.environ.get("AGENT_FANOUT_LOCAL_PUBLIC_KEY_FILE")
        else None,
        help="File containing the base64 Ed25519 public key for signed Local launch attestations.",
    )
    parser.add_argument(
        "--local-sandbox-public-key-file",
        type=Path,
        default=Path(os.environ["AGENT_FANOUT_LOCAL_SANDBOX_PUBLIC_KEY_FILE"]).expanduser()
        if os.environ.get("AGENT_FANOUT_LOCAL_SANDBOX_PUBLIC_KEY_FILE")
        else None,
        help="File containing the base64 Ed25519 public key for host-signed Local sandbox observations.",
    )
    parser.add_argument(
        "--telemetry-env", type=Path,
        default=Path(os.environ.get(
            "AGENT_FANOUT_TELEMETRY_ENV",
            config_home / "agentfanout" / "langfuse.env",
        )).expanduser(),
    )
    parser.add_argument("--authorize-parent", choices=["worker_research", "worker_edit", "worker_debug", "worker_validation"])
    parser.add_argument("--authorize-terminal", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if args.ledger is None:
        args.ledger = default_private_state_directory() / "host.sqlite3"
    if args.requeue_json and any((
        args.results_json, args.usage_json, args.validator_results_json,
        args.validator_launch_receipt_json, args.launch_receipt_json,
        args.capability_json, args.freeze_providers_json, args.heartbeat_json,
        args.cancel_json, args.authorize_parent, args.authorize_terminal,
    )):
        raise ValueError("requeue_operation_must_be_exclusive")
    if args.requeue_json:
        return run_requeue_exclusive(args)
    state, manifest, adapters, manifest_identity = validated_context(args)
    trust_file_identities = {"host_manifest": manifest_identity}
    telemetry = None
    try:
        telemetry_text, telemetry_identity = read_trusted_text(
            args.telemetry_env, max_bytes=64_000, private=True,
        )
    except FileNotFoundError:
        telemetry_text = None
    if telemetry_text is not None:
        trust_file_identities["telemetry_env"] = telemetry_identity
        values = {}
        for line in telemetry_text.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key in {"LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_ALLOWED_HOSTS"}:
                    values[key] = value
        if {"LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_ALLOWED_HOSTS"} <= values.keys():
            telemetry = LangfuseHttpSink(values["LANGFUSE_HOST"], values["LANGFUSE_PUBLIC_KEY"], values["LANGFUSE_SECRET_KEY"],
                                         allowed_hosts=set(values["LANGFUSE_ALLOWED_HOSTS"].split(",")))
    local_public_key = None
    if args.local_public_key_file:
        local_public_key_text, local_key_identity = read_trusted_text(
            args.local_public_key_file, max_bytes=4096,
        )
        trust_file_identities["local_public_key"] = local_key_identity
        local_public_key = local_public_key_text.strip()
        if not local_public_key:
            raise ValueError("local_public_key_empty")
    local_sandbox_public_key = None
    if args.local_sandbox_public_key_file:
        sandbox_key_text, sandbox_key_identity = read_trusted_text(
            args.local_sandbox_public_key_file, max_bytes=4096,
        )
        trust_file_identities["local_sandbox_public_key"] = sandbox_key_identity
        local_sandbox_public_key = sandbox_key_text.strip()
        if not local_sandbox_public_key:
            raise ValueError("local_sandbox_public_key_empty")
    ledger = HostLedger(args.ledger)
    dispatcher = enforced_dispatcher(
        ledger,
        manifest,
        adapters,
        telemetry_sink=telemetry,
        local_public_key_b64=local_public_key,
        local_sandbox_public_key_b64=local_sandbox_public_key,
    )
    output = {
        "schema": "HostDispatchOperationV1",
        "run_id": state["run_id"],
        "status": "ok",
        "trust_file_identities": trust_file_identities,
    }
    state_changed = False
    try:
        if args.freeze_providers_json:
            dispatcher.freeze_providers(state, read_json(args.freeze_providers_json))
        if args.capability_json:
            capabilities = read_json(args.capability_json)
            for capability in capabilities if isinstance(capabilities, list) else [capabilities]:
                dispatcher.record_capability(state, capability)
        if args.launch_receipt_json:
            receipts = read_json(args.launch_receipt_json)
            for receipt in receipts if isinstance(receipts, list) else [receipts]:
                dispatcher.record_launch(state, receipt)
            state_changed = True
        if args.heartbeat_json:
            heartbeat = read_json(args.heartbeat_json)
            dispatcher.record_heartbeat(state, heartbeat["task_id"], heartbeat["launch_correlation_id"], heartbeat["observed_at"])
        if args.cancel_json:
            cancellation = read_json(args.cancel_json)
            dispatcher.cancel_launch(state, cancellation["task_id"], cancellation["reason"])
            state_changed = True
        if args.results_json:
            if not args.usage_json:
                raise ValueError("usage_json_required_with_results")
            output["ingestion"] = dispatcher.ingest_worker_batch(state, read_json(args.results_json), read_json(args.usage_json))
            state_changed = True
        if args.validator_launch_receipt_json:
            receipts = read_json(args.validator_launch_receipt_json)
            for receipt in receipts if isinstance(receipts, list) else [receipts]:
                dispatcher.record_validator_launch(state, receipt)
            state_changed = True
        for result in read_json(args.validator_results_json) if args.validator_results_json else []:
            dispatcher.record_validator_result(state, result)
            state_changed = True
        if args.authorize_parent:
            dispatcher.authorize_parent_action(state, args.authorize_parent)
            output["parent_authorized"] = args.authorize_parent
        if args.authorize_terminal:
            dispatcher.authorize_terminal(state)
            output["terminal_authorized"] = True
        if state_changed:
            save_run_state(args.state_file, state)
    finally:
        ledger.close()
    print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
