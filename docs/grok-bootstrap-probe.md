# Credential-free Grok bootstrap diagnostic

This operator-only diagnostic sends one `initialize` request to a hash-pinned
static x86_64 Grok executable. It never authenticates, creates a session, queries
an account, requests inference, enables a feature or supplies a production receipt.
It is separate from GrokCLI subscription admission and from native skill use.

The compiled helper applies Landlock ABI 3 or 4 and a seccomp syscall allowlist
before exec. It runs unprivileged, with `no_new_privs`, private pipe stdio, closed
inherited descriptors, a clean environment and bounded CPU, memory, files and time.
It permits reading only a disposable supplied profile/workspace and the pinned
executable. All filesystem mutations are denied, including in the disposable home.
All externally addressable sockets (TCP, UDP, named/abstract Unix), io_uring, device access, process inspection,
path metadata syscalls, other executables, fork/vfork and non-thread clone are
denied. An unnamed AF_UNIX stream socket pair with protocol zero is permitted
for Tokio's in-process signal wakeup, including FIONBIO and destination-free
send/receive. Other ioctl operations, socket families/types/protocols, socket(),
bind/connect/listen/accept, sendmsg and sends with a destination remain denied.
There are no inherited sockets or child processes to share this pair with.
Only the exact pthread flag combination and that combination plus the ignored
legacy CLONE_DETACHED bit observed in the installed static runtime are allowed;
threads inherit policy. Both require CLONE_THREAD; other combinations refuse.
Tokio and Rayon pool environment settings are pinned to two threads, avoiding
host-CPU-count thread pools under the fixed memory limit. The supervisor kills
and reaps the exact process after a response, error or timeout.
Unsupported kernels, architectures, dynamic binaries and boundary failures refuse.
The helper does not change namespaces, host settings, services or GPU ownership.

This is an access restriction, not a virtual filesystem: denied path lookups may
still return existence-dependent errors. It does not prove the installed CLI's
authenticated behavior, included allowance, tool-free model execution, model/effort
readback, usage accounting or production cancellation semantics. No network access
is possible in this diagnostic. Failure to initialize under the read-only boundary
is recorded; access must not be silently widened or the operation retried. A
reviewed policy revision needs new synthetic enforcement evidence, a new exact
intent and host authorization, and reconciliation of the previous contained run.

An operator first reviews `prepare_intent(binary, sha256, sandbox_source)` from
`agentfanout.grok_probe`, then binds it with
`prepare_invocation_intent(probe_intent, authorization_path, evidence_directory)`.
The host supplies a private regular authorization file:

```json
{"schema":"GrokBootstrapProbeAuthorizationV3","authorization_id":"host-issued-unique-id","intent_sha256":"sha256-of-canonical-invocation-json","scope":"credential_free_network_denied_initialize_only"}
```

The script persists a private, exclusive `.consumed.json` marker beside the host
receipt and fsyncs that directory before any effect. It also creates its journal
exclusively and fsyncs the authority/invocation/intent before compiling/spawning.
Receipt and evidence paths are bound: changing directories, copying a receipt,
deleting the journal or restarting after a crash cannot reuse that authority.
Preserve markers and journals; reconcile uncertain effects before requesting a
distinct host decision. Never remove consumption to permit replay.
The receipt binds binary, helper source, probe module, fixed argv/request/profile,
policy and bounds. A prepared intent is not permission. The host must establish the
authorization independently; this script cannot grant itself account/inference access.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/grok-abi-probe.py \
  --binary /operator/selected/grok-static-binary \
  --sha256 EXACT_REVIEWED_BINARY_SHA256 \
  --authorization /operator/private/probe-authorization.json \
  --evidence-directory /operator/private/new-probe-evidence
```

Prerequisites are Linux x86_64, Landlock ABI 3/4, seccomp, `close_range`, and a
host compiler at `/usr/bin/cc`. Compilation is local and temporary; no package
installation or downloaded code is performed. Regular AgentFanout operation and
native startup have no compiler requirement. Newer Landlock ABIs require reviewed
policy evolution; the helper deliberately refuses rather than dropping rights.

Expected observations are a boundary-enforced marker, a correlated initialize
response or a typed diagnostic failure, bounded output, an exact reaped process
and no new disposable files. Unexpected callbacks, malformed/duplicate frames,
output/timeout failures or errors do not authorize a retry or subscription lane.
Cleanup removes only this process's temporary binary/helper/profile/workspace;
durable host evidence remains. The normal staged-profile and rollback gates apply.

## Optional disposable container root

If host-wide managed configuration lookups prevent initialization under the
read-only Landlock boundary, a separate diagnostic can use an existing local
Docker engine and an explicitly selected cached immutable image ID. This is not
an inference scheduler, service deployment or provider runtime. It does not pull
images or restart/change the daemon. The image's entrypoint, health checks, logs,
restart behavior and application workload are disabled. Only the pinned executable
and compiled static boundary are mounted, both read-only. No Docker socket,
operator home, credentials, host project, coordinator, GPU or device is mounted.

Before starting, the host verifies the actual container image, mounts, non-root
user, dropped capabilities, no-new-privileges, private PID/IPC/cgroup namespaces,
runc runtime, no network/ports/devices, read-only root, two CPU affinity/quota,
2 GiB memory/no extra swap and 32 PID bound. Writable state is restricted to one
16 MiB noexec/nosuid/nodev tmpfs. The helper requires PID 1 and zero capabilities,
creates only the disposable home/profile/empty task directory, clears environment,
and applies inherited Landlock/seccomp before Grok exec. Path metadata calls are
permitted only in this separately compiled private-root variant. Other executables,
process creation, external files, devices and network remain denied. A synthetic
executable must prove the boundary in this actual container configuration first.

This requires a `GrokBootstrapProbeAuthorizationV3` receipt, scope
`credential_free_cpu_container_initialize_only`, binding the canonical
`prepare_invocation_intent(prepare_container_intent(...), ...)` digest.
Older V1/V2 authority and consumption semantics are retained as historical
evidence; the current launcher refuses those schemas for both variants.
Its `GrokBootstrapContainerProbeIntentV1` and
`GrokBootstrapContainerProbeObservationV1` envelopes are distinct from the
read-only host diagnostic envelopes. Unsupported readers must refuse; existing
run state, guards, ledger events, launch receipts and provider accounting are not
changed. Early private diagnostic observations retain their original envelopes
as evidence and must not be reinterpreted as production session/worker results.
Add `--container-image-id sha256:EXACT_CACHED_IMAGE_ID` to the operator command.
The image ID, helper/source/module identity, engine endpoint, policy, CPU set,
effects and request are bound in the intent. The host owns durable authority;
there is no inferred spending authority or production admission.

Docker client exit is not worker cancellation evidence. Cleanup queries the
exact newly created container, checks its owner, kills/waits that ID if required,
verifies stopped state/PID zero, then removes it. An uncertain operation leaves
durable evidence for quarantine/reconciliation and never permits replay. Unrelated
containers, coordinator jobs and GPU workloads are never selected for cleanup.
The raw response is persisted before cleanup. If a kill races with worker exit,
only a fresh identified stopped/PID-zero observation can reconcile it. Failure
to persist a response leaves diagnostic completion unavailable even if cleanup
is independently confirmed. Retain all prior events and uncertain observations.
