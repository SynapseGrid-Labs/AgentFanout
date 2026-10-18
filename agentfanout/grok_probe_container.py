"""CPU-only disposable ABI diagnostic using an existing local Docker engine.

No image pulls, daemon changes, GPU/devices, credentials, host projects, network,
ports, tools, inference or production admission. Host verifies the exact created
container before starting the compiled boundary. Cleanup targets only its ID.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from .grok_cli import GrokCLIUnavailable
from .grok_probe import REQUEST, digest, prepare_intent, supervise

DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
TMPFS = "rw,noexec,nosuid,nodev,size=16777216,mode=0700,uid=1000,gid=1000"
POLICY = "private-container-root+landlock+seccomp-static-threads-v1"


def command(args: list[str], *, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(DOCKER + args, capture_output=True, text=True, timeout=30,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    if check and result.returncode:
        raise GrokCLIUnavailable("container_diagnostic_operation_failed", args[0])
    return result


def verify_container(record: dict, image: str, name: str, exe: Path, helper: Path, cpus: str) -> None:
    """Verify effective engine configuration, not a label or sandbox name."""
    h, c = record.get("HostConfig", {}), record.get("Config", {})
    expected = {"Runtime": "runc", "NetworkMode": "none", "IpcMode": "none", "PidMode": "",
                "CgroupnsMode": "private", "Privileged": False, "ReadonlyRootfs": True,
                "Memory": 2147483648, "MemorySwap": 2147483648, "NanoCpus": 2000000000,
                "PidsLimit": 32, "CpusetCpus": cpus, "AutoRemove": False}
    if any(h.get(k) != v for k, v in expected.items()):
        raise GrokCLIUnavailable("container_effective_boundary_mismatch")
    if (h.get("CapAdd") or set(h.get("CapDrop") or []) != {"ALL"} or
            h.get("Devices") or h.get("DeviceRequests") or h.get("Binds") or
            h.get("PortBindings") or h.get("VolumesFrom") or h.get("UsernsMode") or
            h.get("ExtraHosts") or h.get("Links") or h.get("Dns") or h.get("Sysctls") or
            h.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0} or
            h.get("LogConfig") != {"Type": "none", "Config": {}} or
            h.get("Tmpfs") != {"/work": TMPFS} or
            set(h.get("SecurityOpt") or []) not in ({"no-new-privileges:true"}, {"no-new-privileges"})):
        raise GrokCLIUnavailable("container_unsafe_effective_options")
    if (record.get("Image") != image or record.get("Name") != "/" + name or
            c.get("User") != "1000:1000" or c.get("Tty") is not False or
            c.get("OpenStdin") is not True or c.get("WorkingDir") != "/work" or
            c.get("Entrypoint") != ["/probe-boundary"] or
            c.get("Cmd") != ["/pinned-binary", "/work", "--no-auto-update", "agent", "stdio"] or
            c.get("Healthcheck", {}).get("Test") != ["NONE"] or c.get("Volumes") or
            c.get("Labels", {}).get("agentfanout.diagnostic.owner") != name or
            record.get("State", {}).get("Status") != "created"):
        raise GrokCLIUnavailable("container_identity_or_lifecycle_mismatch")
    mounts = record.get("Mounts", [])
    wanted = {(str(exe), "/pinned-binary"), (str(helper), "/probe-boundary")}
    if len(mounts) != 2 or {(m.get("Source"), m.get("Destination")) for m in mounts} != wanted:
        raise GrokCLIUnavailable("container_mounts_mismatch")
    if any(m.get("Type") != "bind" or m.get("RW") is not False for m in mounts):
        raise GrokCLIUnavailable("container_mount_authority")


def prepare_container_intent(binary: Path, expected: str, sandbox_source: Path, image: str, *, timeout=30) -> dict:
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise GrokCLIUnavailable("container_probe_timeout_bounds")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise GrokCLIUnavailable("container_image_must_be_exact_local_id")
    intent = prepare_intent(binary, expected, sandbox_source)
    intent["schema"] = "GrokBootstrapContainerProbeIntentV1"
    intent.update({"container_policy": POLICY, "filesystem_mutations": "private_16MiB_tmpfs_only",
                   "container_module_sha256": digest(Path(__file__)),
                   "image_id": image, "host_engine": DOCKER, "runtime": "runc",
                   "helper_compile_define": "AGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC",
                   "max_seconds": timeout, "admission_allowed": False})
    cpus = ",".join(str(c) for c in sorted(os.sched_getaffinity(0))[:2])
    intent["cpuset_cpus"] = cpus
    return intent


def run_container_probe(binary: Path, expected: str, sandbox_source: Path, image: str,
                        host_authorize, journal, *, timeout=30) -> dict:
    intent = prepare_container_intent(binary, expected, sandbox_source, image, timeout=timeout)
    cpus = intent["cpuset_cpus"]
    if host_authorize(intent) is not True or journal("container_probe_start_intent", intent) is not True:
        raise GrokCLIUnavailable("container_probe_host_authority_or_durability")
    # Inspect by immutable local ID. Never pull, build an image, run its original
    # entrypoint, health check, OnBuild instruction or application workload.
    metadata = json.loads(command(["image", "inspect", image]).stdout)[0]
    if metadata.get("Id") != image or metadata.get("Os") != "linux" or metadata.get("Architecture") != "amd64" or metadata.get("Config", {}).get("Volumes"):
        raise GrokCLIUnavailable("container_image_not_compatible")
    with tempfile.TemporaryDirectory(prefix="agentfanout-container-probe-") as temporary:
        root = Path(temporary)
        exe, helper = root / "pinned-binary", root / "probe-boundary"
        data = binary.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise GrokCLIUnavailable("probe_binary_changed")
        exe.write_bytes(data); exe.chmod(0o555)
        source = sandbox_source.read_bytes()
        if hashlib.sha256(source).hexdigest() != intent["sandbox_source_sha256"]:
            raise GrokCLIUnavailable("probe_boundary_source_changed")
        subprocess.run(["/usr/bin/cc", "-x", "c", "-", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                        "-DAGENTFANOUT_PRIVATE_CONTAINER_DIAGNOSTIC", "-o", str(helper)],
                       input=source, capture_output=True, check=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        helper.chmod(0o555)
        name = "agentfanout-abi-" + uuid.uuid4().hex
        argv = ["create", "--pull", "never", "--name", name, "--interactive", "--network", "none",
                "--runtime", "runc", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", "1000:1000", "--ipc", "none", "--cgroupns", "private", "--memory", "2147483648",
                "--memory-swap", "2147483648", "--cpus", "2", "--cpuset-cpus", cpus, "--pids-limit", "32",
                "--restart", "no", "--no-healthcheck", "--log-driver", "none", "--ulimit", "core=0:0",
                "--label", "agentfanout.diagnostic.owner=" + name, "--tmpfs", "/work:" + TMPFS,
                "--workdir", "/work", "--entrypoint", "/probe-boundary",
                "--mount", "type=bind,source=" + str(exe) + ",target=/pinned-binary,readonly",
                "--mount", "type=bind,source=" + str(helper) + ",target=/probe-boundary,readonly",
                image, "/pinned-binary", "/work", "--no-auto-update", "agent", "stdio"]
        if journal("container_create_intent", {"name": name, "argv": DOCKER + argv,
                                               "helper_sha256": digest(helper)}) is not True:
            raise GrokCLIUnavailable("container_create_intent_not_durable")
        created = command(argv).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", created):
            raise GrokCLIUnavailable("container_create_identity_uncertain")
        observation = None
        response_durable = False
        try:
            record = json.loads(command(["inspect", created]).stdout)[0]
            verify_container(record, image, name, exe, helper, cpus)
            if journal("container_verified_before_start", {"container_id": created, "record": record}) is not True:
                raise GrokCLIUnavailable("container_verification_not_durable")
            observation = supervise(DOCKER + ["start", "--attach", "--interactive", created], root,
                                    {"PATH": "/usr/bin:/bin", "LANG": "C"},
                                    json.dumps(REQUEST).encode() + b"\n", timeout=timeout)
            observation.update({"container_id": created, "intent": intent, "helper_sha256": digest(helper),
                                "schema": "GrokBootstrapContainerProbeObservationV1",
                                "worker_process_reconciled": False,
                                "client_process_reaped": observation.pop("process_reaped")})
            response_durable = journal("container_response_observed", observation) is True
            if not response_durable:
                raise GrokCLIUnavailable("container_response_not_durable")
        finally:
            # Docker client exit does not prove worker cancellation. Query the
            # exact container and explicitly kill/wait/remove only that ID.
            state = json.loads(command(["inspect", created]).stdout)[0]
            if state.get("Config", {}).get("Labels", {}).get("agentfanout.diagnostic.owner") != name:
                raise GrokCLIUnavailable("container_cleanup_ownership_uncertain")
            if journal("container_cleanup_intent", {"container_id": created, "state": state["State"]}) is not True:
                raise GrokCLIUnavailable("container_cleanup_not_durable")
            if state["State"].get("Running"):
                # The worker may already be stopping after client SIGTERM.
                # A failed kill is not absence/cancellation evidence: only a
                # fresh identified stopped/PID-zero observation can reconcile.
                killed = command(["kill", "--signal", "KILL", created], check=False)
                if killed.returncode == 0:
                    command(["wait", created])
            final = json.loads(command(["inspect", created]).stdout)[0]["State"]
            if final.get("Running") or final.get("Pid") != 0:
                raise GrokCLIUnavailable("container_cancellation_uncertain")
            command(["rm", created])
            if observation is not None:
                observation.update({"worker_process_reconciled": True, "container_final_state": final,
                                    "container_removed": True})
                kind = "container_probe_observation" if response_durable else "container_cleanup_reconciled"
                if journal(kind, observation) is not True:
                    raise GrokCLIUnavailable("container_reconciliation_not_durable")
        return observation
