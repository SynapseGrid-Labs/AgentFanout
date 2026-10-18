#!/usr/bin/env python3
"""Operator-only one-shot non-inference diagnostic, with durable host authority."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentfanout.grok_probe import prepare_intent, prepare_invocation_intent, run_probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--container-image-id", help="Explicit cached immutable CPU-only image ID; never pulled")
    args = parser.parse_args()
    sandbox_source = ROOT / "scripts/grok-probe-sandbox.c"
    if args.container_image_id:
        from agentfanout.grok_probe_container import prepare_container_intent, run_container_probe
        intent = prepare_container_intent(args.binary, args.sha256, sandbox_source, args.container_image_id)
        scope = "credential_free_cpu_container_initialize_only"
    else:
        intent = prepare_intent(args.binary, args.sha256, sandbox_source)
        scope = "credential_free_network_denied_initialize_only"
    invocation = prepare_invocation_intent(intent, args.authorization, args.evidence_directory)
    intent_hash = hashlib.sha256(json.dumps(invocation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Host-local operator receipt must be a private regular file. The script
    # never creates it, and never authorizes a session, account read or prompt.
    fd = os.open(args.authorization, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("host_authorization_file_not_private")
        authorization = json.load(handle)
    if (authorization.get("schema") != "GrokBootstrapProbeAuthorizationV3" or
            authorization.get("intent_sha256") != intent_hash or
            authorization.get("scope") != scope or
            not isinstance(authorization.get("authorization_id"), str) or not authorization["authorization_id"]):
        raise ValueError("host_authorization_scope_or_identity_mismatch")
    # Consumption persists independently of the evidence directory. Copying a
    # receipt or changing directories cannot replay it because both paths are
    # bound in the host invocation digest. Never remove this marker to retry.
    consumed = args.authorization.with_name(args.authorization.name + ".consumed.json")
    consume_fd = os.open(consumed, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(consume_fd, "w") as handle:
        json.dump({"schema": "GrokBootstrapProbeConsumptionV1", "authorization_id": authorization["authorization_id"],
                   "invocation": invocation, "intent_sha256": intent_hash, "replay_allowed": False}, handle, sort_keys=True)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    authority_directory_fd = os.open(args.authorization.parent, os.O_DIRECTORY)
    try: os.fsync(authority_directory_fd)
    finally: os.close(authority_directory_fd)
    args.evidence_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal_path = args.evidence_directory / "probe-journal.jsonl"
    # Consumed before any effect, with no automatic retry/replay. A crash leaves
    # this file in place and requires host reconciliation, never overwrite.
    journal_fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        def journal(kind, payload):
            record = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, allow_nan=False).encode() + b"\n"
            with os.fdopen(os.dup(journal_fd), "ab") as handle:
                handle.write(record); handle.flush(); os.fsync(handle.fileno())
            return True
        journal("host_authorization_observed", authorization)
        journal("host_invocation_bound", invocation)
        directory_fd = os.open(args.evidence_directory, os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
        if args.container_image_id:
            observation = run_container_probe(args.binary, args.sha256, sandbox_source, args.container_image_id,
                                              lambda proposed: proposed == intent, journal)
        else:
            observation = run_probe(args.binary, args.sha256, sandbox_source,
                                    lambda proposed: proposed == intent, journal)
        print(json.dumps(observation, sort_keys=True, indent=2))
        return 0 if observation["boundary_enforced"] and observation["frames"] and not observation["failure"] else 2
    finally:
        os.close(journal_fd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "probe_unavailable", "reason": str(exc),
                          "admission_allowed": False, "replay_allowed": False}), file=sys.stderr)
        raise SystemExit(2)
