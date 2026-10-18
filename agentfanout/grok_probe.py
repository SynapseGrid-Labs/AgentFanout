"""Host-owned, one-shot credential-free ABI diagnostic; never admission.

This boundary intentionally denies all filesystem mutations and all network,
other processes, devices, tools and path metadata outside supplied descriptors.
No authentication, session creation, billing, usage or inference requests exist.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
import json
import os
import selectors
import signal
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .grok_cli import GrokCLIUnavailable, _strict_object, _reject_constant

REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": 1, "clientCapabilities": {
        "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}}
PROFILE = '[auth]\npreferred_method = "oidc"\ndisable_api_key_auth = true\n'
POLICY = "landlock-abi3-or4-readonly+seccomp-x86_64-allowlist-v3-static-threads"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def static_elf(data: bytes) -> bool:
    """Reject interpreters/scripts/dynamic ELF before applying a one-file rule."""
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        return False
    if struct.unpack_from("<H", data, 18)[0] != 62:
        return False
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    if size != 56 or count == 0 or offset + size * count > len(data):
        return False
    return all(struct.unpack_from("<I", data, offset + size * i)[0] != 3 for i in range(count))


def prepare_intent(binary: Path, expected: str, sandbox_source: Path) -> dict:
    data = binary.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected or not static_elf(data):
        raise GrokCLIUnavailable("probe_binary_identity_or_format")
    return {"schema": "GrokBootstrapProbeIntentV1", "binary_sha256": expected,
            "sandbox_source_sha256": digest(sandbox_source),
            "probe_module_sha256": digest(Path(__file__)), "policy": POLICY,
            "argv": ["pinned-static-binary", "--no-auto-update", "agent", "stdio"],
            "request": deepcopy(REQUEST), "profile_sha256": hashlib.sha256(PROFILE.encode()).hexdigest(),
            "max_seconds": 30, "max_output_bytes": 524288, "max_frame_bytes": 262144,
            "max_frames": 64, "credentials": "none", "network": "denied",
            "thread_pool_environment": {"TOKIO_WORKER_THREADS": "2", "RAYON_NUM_THREADS": "2"},
            "filesystem_mutations": "denied", "inference_requests": 0,
            "admission_allowed": False}


def prepare_invocation_intent(probe_intent: dict, authorization: Path, evidence_directory: Path) -> dict:
    """Bind host authority to one exact location, not an adapter-made receipt."""
    return {"schema": "GrokBootstrapProbeInvocationV1", "probe_intent": deepcopy(probe_intent),
            "authorization_path": str(authorization.resolve()),
            "evidence_directory": str(evidence_directory.resolve())}


def supervise(argv: list[str], cwd: Path, env: dict, payload: bytes,
              *, timeout: float = 30, max_output: int = 524288) -> dict:
    """Bound pipes/time, kill and reap the exact isolated process on every exit."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            close_fds=True, start_new_session=True)
    status = "process_exited"
    started = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            proc.stdin.write(payload)
            proc.stdin.flush()
            # Keep stdin open until response/timeout: EOF itself can exit ACP.
            while selector.get_map():
                if time.monotonic() - started >= timeout:
                    status = "timeout"; break
                for key, _ in selector.select(min(.1, timeout)):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    remaining = max_output - sum(map(len, buffers.values()))
                    buffers[key.data].extend(chunk[:max(0, remaining)])
                    if len(chunk) > remaining:
                        status = "output_limit"; break
                if status == "output_limit":
                    break
                if b"\n" in buffers["stdout"]:
                    status = "frame_observed"; break
                if proc.poll() is not None and not selector.get_map():
                    break
    except BrokenPipeError:
        status = "input_pipe_closed"
    finally:
        # Fork/vfork and non-thread clone are denied. Group signals also handle
        # helper failure before enforcement. Parent owns cancellation evidence.
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        # Capture already emitted trailing frames/errors after reaping, still
        # under the single combined cap. Never discard a queued callback and
        # label the first response sufficient conformance evidence.
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            while True:
                try:
                    chunk = os.read(stream.fileno(), 16384)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                remaining = max_output - sum(map(len, buffers.values()))
                buffers[name].extend(chunk[:max(0, remaining)])
                if len(chunk) > remaining:
                    status = "output_limit"; break
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
    frames = []
    failure = None
    for line in bytes(buffers["stdout"]).splitlines():
        if len(line) > 262144 or len(frames) >= 64:
            failure = "frame_bounds"; break
        try:
            frame = json.loads(line, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            failure = "malformed_frame"; break
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0" or frame.get("id") != 1 or "method" in frame:
            failure = "unexpected_frame_or_callback"; break
        if frames or (("result" in frame) == ("error" in frame)):
            failure = "response_correlation"; break
        frames.append(frame)
    enforced = b"probe_boundary_enforced:" in buffers["stderr"]
    return {"schema": "GrokBootstrapProbeObservationV1", "status": status,
            "boundary_enforced": enforced, "frames": frames,
            "failure": failure, "stderr": bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
            "stdout": bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
            "returncode": proc.returncode, "process_reaped": True,
            "elapsed_seconds": time.monotonic() - started, "admission_allowed": False,
            "replay_allowed": False, "inference_requests": 0}


def run_probe(binary: Path, expected: str, sandbox_source: Path,
              host_authorize: Callable[[dict], bool], journal: Callable[[str, dict], bool],
              *, compiler: str = "/usr/bin/cc") -> dict:
    intent = prepare_intent(binary, expected, sandbox_source)
    # This is not a provider session receipt and cannot authorize any prompt.
    # Authority and fsync durability belong to the calling host, before spawn.
    if host_authorize(intent) is not True:
        raise GrokCLIUnavailable("probe_host_authorization_missing")
    if journal("probe_start_intent", intent) is not True:
        raise GrokCLIUnavailable("probe_intent_not_durable")
    with tempfile.TemporaryDirectory(prefix="agentfanout-abi-probe-") as temporary:
        root = Path(temporary)
        exe, helper, workspace = root / "pinned-binary", root / "boundary", root / "workspace"
        data = binary.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise GrokCLIUnavailable("probe_binary_changed")
        exe.write_bytes(data); exe.chmod(0o500)
        workspace.mkdir(mode=0o700)
        home = workspace / "home"; home.mkdir(mode=0o700)
        (home / ".grok").mkdir(mode=0o700)
        (home / ".grok" / "config.toml").write_text(PROFILE)
        # Read the exact source whose hash the host approved; no compiler env
        # inheritance, downloaded dependency, privileged helper or installation.
        source = sandbox_source.read_bytes()
        if hashlib.sha256(source).hexdigest() != intent["sandbox_source_sha256"]:
            raise GrokCLIUnavailable("probe_boundary_source_changed")
        built = subprocess.run([compiler, "-x", "c", "-", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(helper)],
                               input=source, capture_output=True, timeout=30,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        if built.returncode:
            raise GrokCLIUnavailable("probe_boundary_compile_failed")
        observation = supervise([str(helper), str(exe), str(workspace), "--no-auto-update", "agent", "stdio"],
                                workspace, {"HOME": str(home), "XDG_CONFIG_HOME": str(home),
                                            "PATH": "", "LANG": "C", "TMPDIR": str(workspace),
                                            **intent["thread_pool_environment"]},
                                json.dumps(REQUEST, separators=(",", ":")).encode() + b"\n")
        observation["intent"] = intent
        observation["helper_sha256"] = digest(helper)
        observation["disposable_files_after"] = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*"))
        if journal("probe_observation", observation) is not True:
            raise GrokCLIUnavailable("probe_reconciliation_not_durable")
    return observation
