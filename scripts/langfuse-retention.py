#!/usr/bin/env python3
"""Delete only AgentFanout Langfuse traces older than the configured cutoff."""

from __future__ import annotations

import argparse
import base64
import errno
import ipaddress
import json
import os
import stat
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


MAX_SECRET_KEY_BYTES = 4096


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_langfuse_host(host, allowed_hosts, *, dangerously_allow_insecure_loopback_http_for_testing=False):
    parsed = urllib.parse.urlsplit(host or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("langfuse_retention_host_invalid")
    if parsed.scheme != "https":
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not dangerously_allow_insecure_loopback_http_for_testing or not is_loopback:
            raise ValueError("langfuse_retention_https_required")
    configured = {item.casefold().rstrip(".") for item in allowed_hosts}
    hostname = parsed.hostname.casefold().rstrip(".")
    if parsed.username or parsed.password or hostname not in configured:
        raise ValueError("langfuse_retention_host_not_allowed")
    return parsed


def _validate_secret_key_file(status):
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("langfuse_secret_key_file_not_regular")
    if status.st_uid != os.geteuid():
        raise ValueError("langfuse_secret_key_file_owner_unsafe")
    if stat.S_IMODE(status.st_mode) != 0o600:
        raise ValueError("langfuse_secret_key_file_permissions_unsafe")
    if status.st_nlink != 1:
        raise ValueError("langfuse_secret_key_file_link_count_unsafe")


def _validate_secret_key_value(value):
    if not value or not value.strip() or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("langfuse_secret_key_invalid")
    if len(value.encode("utf-8")) > MAX_SECRET_KEY_BYTES:
        raise ValueError("langfuse_secret_key_too_large")
    return value


def read_secret_key_file(path):
    secret_path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    try:
        initial = os.lstat(secret_path)
    except FileNotFoundError as exc:
        raise ValueError("langfuse_secret_key_file_missing") from exc
    if stat.S_ISLNK(initial.st_mode):
        raise ValueError("langfuse_secret_key_file_symlink")
    _validate_secret_key_file(initial)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(secret_path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("langfuse_secret_key_file_symlink") from exc
        raise
    try:
        opened = os.fstat(descriptor)
        _validate_secret_key_file(opened)
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise ValueError("langfuse_secret_key_file_identity_changed")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024, MAX_SECRET_KEY_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_SECRET_KEY_BYTES:
                raise ValueError("langfuse_secret_key_too_large")
        current = os.lstat(secret_path)
        if stat.S_ISLNK(current.st_mode):
            raise ValueError("langfuse_secret_key_file_symlink")
        _validate_secret_key_file(current)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("langfuse_secret_key_file_identity_changed")
    finally:
        os.close(descriptor)

    try:
        decoded = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("langfuse_secret_key_encoding_invalid") from exc
    if decoded.endswith("\r\n"):
        decoded = decoded[:-2]
    elif decoded.endswith("\n"):
        decoded = decoded[:-1]
    return _validate_secret_key_value(decoded)


def resolve_secret_key(environment_secret, secret_key_file):
    if environment_secret and secret_key_file:
        raise ValueError("langfuse_secret_key_sources_conflict")
    if secret_key_file:
        return read_secret_key_file(secret_key_file)
    if environment_secret:
        return _validate_secret_key_value(environment_secret)
    raise ValueError("langfuse_secret_key_missing")


def request_json(url, public_key, secret_key, method="GET"):
    auth = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    request = urllib.request.Request(url, method=method, headers={"Authorization": auth, "Content-Type": "application/json"})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        data = response.read()
        return json.loads(data) if data else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--host", default=os.environ.get("LANGFUSE_HOST"))
    parser.add_argument("--public-key", default=os.environ.get("LANGFUSE_PUBLIC_KEY"))
    parser.add_argument(
        "--secret-key-file",
        default=os.environ.get("LANGFUSE_SECRET_KEY_FILE"),
        help="path to an owner-only (0600), non-symlink secret-key file",
    )
    parser.add_argument("--allowed-host", default=os.environ.get("LANGFUSE_ALLOWED_HOSTS"))
    parser.add_argument(
        "--dangerously-allow-insecure-loopback-http-for-testing",
        action="store_true",
        help="allow plaintext HTTP only to a numeric loopback address for isolated tests",
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-delete", type=int, default=50)
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(item == "--secret-key" or item.startswith("--secret-key=") for item in arguments):
        parser.error("langfuse_secret_key_argv_forbidden")
    args = parser.parse_args(arguments)
    allowed = {item.strip() for item in (args.allowed_host or "").split(",") if item.strip()}
    try:
        validate_langfuse_host(
            args.host,
            allowed,
            dangerously_allow_insecure_loopback_http_for_testing=args.dangerously_allow_insecure_loopback_http_for_testing,
        )
        secret_key = resolve_secret_key(os.environ.get("LANGFUSE_SECRET_KEY"), args.secret_key_file)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.public_key or args.days < 3 or args.max_delete < 1:
        parser.error("valid keys, days >= 3, and max-delete >= 1 are required")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    query = urllib.parse.urlencode({"toTimestamp": cutoff.isoformat().replace("+00:00", "Z"), "limit": min(args.max_delete, 50)})
    payload = request_json(args.host.rstrip("/") + "/api/public/traces?" + query, args.public_key, secret_key)
    traces = payload.get("data") or payload.get("traces") or []
    ids = [trace.get("id") for trace in traces if isinstance(trace, dict) and trace.get("id")][:args.max_delete]
    deleted = []
    if args.apply:
        for trace_id in ids:
            request_json(args.host.rstrip("/") + "/api/public/traces/" + urllib.parse.quote(trace_id, safe=""), args.public_key, secret_key, "DELETE")
            deleted.append(trace_id)
    print(json.dumps({"schema": "LangfuseRetentionReportV1", "cutoff": cutoff.isoformat(), "dry_run": not args.apply,
                      "eligible_count": len(ids), "deleted_count": len(deleted), "trace_id_sha256": [__import__("hashlib").sha256(i.encode()).hexdigest() for i in ids]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
