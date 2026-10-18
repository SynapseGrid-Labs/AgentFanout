# Architecture

## Optional Codex plugin path

The Codex plugin keeps the Markdown activation surface small and puts the
task-scoped control loop in lifecycle hooks:

```text
explicit prompt → UserPromptSubmit arms task lease
                         │
                         ▼
                  native worker wave
                         │ SubagentStart/Stop
                         ▼
                 completed result batch
                         │
                         ▼
Stop → one-time decision token → residual ledger → next wave or final synthesis
```

Every observed completed batch generates a new decision token. A subsequent worker start
invalidates the old token. The hook accepts completion only on a later `Stop` with
the current token, an orchestrator-attested zero-residual marker, no active
workers, and at least one observed worker. A new trusted terminal callback from
a known worker also tracks same-worker follow-up when Codex does not emit
another `SubagentStart`. Supported parent tools are denied before first
dispatch, during cancellation, and between batches; only exact known
orchestration controls are exempt. Lease state is SQLite-serialized,
task/session/workspace identity is hashed, terminal rows are deleted, and
prompt/transcript bodies are not stored.

These checks apply only to intercepted events and supported tools. The residual
marker is an orchestrator assertion, not independent proof of correctness or
completion. Neither hooks nor passive observation prevent every tool path or
unsupported parent success statement. Codex documents incomplete `PreToolUse` interception and
does not expose parent/subagent identity on every tool callback. The optional
standalone runtime below remains the stronger isolated controller boundary.

AgentFanout separates a portable task-scoped skill from host-specific
enforcement. It does not embed secret-bearing account access. Native host
agents are the primary public execution path; the atomic CLI
binds one exact project, verifies adapter input modes, creates a persistent run
state, and then arms a session guard before giving a host runtime authority over worker launch, waiting, logical
follow-up, validation, cancellation accounting, and terminal completion. The
bundled POSIX adapter confirms its launched process group, not descendants that
detach into another session or remote provider work.

## Components

The default public path is:

```text
positive activation → compact task packets → native spawn/wait/callback
    → compact result ingestion → replan → next wave or completion
```

It does not load the optional runtime, inspect conversation provider state, or
claim model/reasoning control that the host does not expose. The `public-runtime`
profile below remains explicit and fail-closed for operators who need its
Linux/POSIX enforcement and receipts.

```text
user request
    │
    ▼
scripts/agentfanout.py
    ├── ProjectBindingV1 + adapter/model/executable preflight
    ├── RunStateV2 creation before any launch
    ├── SessionGuardStateV1 armed after state creation
    └── locked resume/status/abandon recovery control
             │
             ▼
scripts/route.py
    ├── classification and safety gates
    ├── providers/models.json
    ├── bounded TaskUnitV1 packets
    └── cost/context decision telemetry
             │
             ▼
agentfanout/lifecycle.py ── RunStateV2 on a host-selected state path
    │       │       │
    │       │       └── validate + replan residual work
    │       └────────── dispatch next bounded wave
    └────────────────── recommend spawn or logical follow-up; reuse is adapter-dependent
             │
             ▼
agentfanout/runner.py (hosted bounded-file execution core)
    ├── consumes route-planned collecting state
    ├── launches each independent wave concurrently
    ├── embeds explicitly assigned bounded file inputs
    ├── records total/cached/uncached usage
    ├── dispatches separate-process, receipt-bound validator batches
    └── loops through repair/residual waves
             │
             ▼
provider adapters
    ├── Codex native compatibility diagnostic (worker admission blocked)
    ├── Grok fixed-endpoint HTTPS no-tool workers
    ├── host-attested Local no-tool HTTP data client
    ├── tool-capable Local contract (signed sandbox proof required)
    └── compatibility-disabled adapters
             │
             ▼
mandatory HostDispatcher
    ├── append-only SQLite evidence ledger
    ├── measured usage + launch receipts
    ├── validator/effect/sandbox receipts
    └── parent and terminal authorization

cross-harness contract
    ├── advisory: prompt/policy only
    ├── runner_enforced: standalone controller has no native worker tools
    └── harness_enforced: trusted adapter mediates or hard-denies all native worker tools
```

The host runtime is responsible for proving the resolved provider model,
reasoning effort, measured usage, and assigned worker handle. It stores these
authorities in private `RunStateV2.launch_records`, keyed by an opaque launch
correlation. Worker-authored control-plane identities and usage claims are not
part of `WorkerResultV1` and fail validation.

`ProjectBindingV1` is persisted before execution and revalidated by the hosted
runner before capability admission or a launch receipt. Exact configured
sensitive roots are denied; common repository names such as `config`, `auth`,
and `runtime` are not globally forbidden. The current Codex manifest declares
only `bounded_file_text`, so symbolic task dimensions and generic
`request.items[]` are rejected before state creation by the atomic CLI.
Concrete file tasks carry a SHA-256 and byte-count binding; the runner rereads
and verifies that binding during its all-wave preflight before any receipt.

No inner skill can compel an arbitrary outer harness. `scripts/check-harness.py`
lints manifest and trace invariants. A pinned digest identifies reviewed bytes;
it does not authenticate their producer or prove real host execution. Any
`harness_enforced` claim needs an external trusted capture/enforcement boundary.

## Hosted-runner multi-wave invariant

The parent follows this state machine:

```text
planning → dispatching → collecting → validating → replanning
              ▲                                      │
              └──────── eligible residual work ──────┘
                                                     │
                                      no residual ───┴→ complete
```

`complete` is illegal while any task remains unfinished. A result can
add `remaining_work`; the controller turns each item into a new bounded task and
recommends a previous handle only when the adapter explicitly supports retained
context and the residual uses the same bounded input. None of the currently
admitted bundled strict-runner adapters claims retained provider context. Codex
compatibility diagnostics are ephemeral and strict-runner Codex workers remain
unavailable; Grok and Local follow-up waves use fresh isolated calls. This does
not describe native Codex plugin agents. Validators and cross-input residuals
remain independent.
Residual text is length-bounded,
control-character normalized, and blocked when known credential patterns match.
That detector is incomplete defense in depth, not DLP. Wave size, total workers,
total waves, result bytes, parent context, attempts, and no-progress loops stop
future orchestration. Cost is reserved and checked for acceptance, but provider
spend already incurred by a call cannot be clawed back.

## Validator process isolation, decision, and escalation

Validation is a host-controlled terminal gate, not a parent prompt. Planned
non-main-session tasks set `validator_required`; recording a host launch makes
`host_validation_required` true for the run. The hosted runner consequently
creates a bounded validator wave for every completed required task whose status
is not already `passed`.

Each admitted Grok or Local path uses a separate provider-specific validator
process with receipt-bound model, capability, effort, task, and usage evidence.
It may deliberately reuse the task's provider, selector, and reasoning effort;
it does **not** claim cross-provider or model-diverse validation. Native Codex
worker and validator execution remains compatibility-blocked.
A same-model passing verdict does not independently establish correctness and is
not an injection-independent security boundary.

| Condition / decision | Required host evidence and binding | Next legal outcome |
|---|---|---|
| Worker task is launched outside the main session | `TaskUnitV1.validator_required` is true for non-main tasks, and the host launch record turns on run-level validation. | A completed worker packet waits for a validator wave; a missing validator adapter blocks the task. |
| Validator is selected and launched | Its prompt limits validation to the assigned task and accepted result packet. The separate launch binds the task ID, accepted result ID/SHA-256, policy SHA-256 derived from both, approved provider, resolved model, effort, capability digest, lease, and verified usage. | Only a receipt-bound result from that launch may change validation state; stale, replayed, unapproved, or mismatched output blocks rather than being accepted. |
| Validator verdict is `pass` | `ValidatorResultV1` has non-empty evidence references and all host-bound provenance validates. | The task's validation status becomes `passed`. It remains subject to residual-work, usage, effect, and terminal checks. |
| Validator verdict is `concerns` or `fail` | The verdict and material findings are durably recorded; both verdicts map to `repair_required`. A reported contradiction is represented by one of these non-pass verdicts and its findings—there is no separate conflict verdict. | Replan the same bounded task while attempts remain; otherwise block the run. The parent may not replace the worker's repair work. |
| Worker output is partial, failed, blocked, or structurally invalid | A partial packet must declare bounded `remaining_work`; accepted residual items become child tasks. Failed/blocked/invalid output returns the task to pending while attempts remain, otherwise blocks it. | Dispatch residual or repair work in a later wave. Parent synthesis cannot bypass the outstanding delegable task. |
| Validator adapter, receipt, capability, usage, or result contract fails | The host records a `runner_blocked` reason and leaves validation at `repair_required`; validator calls have no state or ledger authority. | Fail closed as blocked delegable work. Do not silently validate in the parent or substitute an unapproved worker. |
| Terminal authorization is requested | Lifecycle completion requires no residual or incomplete task and all required validation passed. The host additionally requires verified worker usage and no pending or `unknown_after_crash` effect. | Only `HostDispatcher.authorize_terminal` appends `terminal_authorized`; otherwise terminal synthesis is denied. |

Normative contracts: [TaskUnitV1](../schemas/TaskUnitV1.schema.json),
[ValidatorLaunchReceiptV1](../schemas/ValidatorLaunchReceiptV1.schema.json),
[ValidatorResultV1](../schemas/ValidatorResultV1.schema.json),
[lifecycle completion and replan](../agentfanout/lifecycle.py#L681), and
[host validator and terminal authorization](../agentfanout/host_runtime.py).

## Data contracts

- `TaskUnitV1`: bounded scope, provider profile, budgets, allowed paths, write
  set, dependencies, and validation requirement.
- `WorkerResultV1`: compact evidence, changes, residual work, and an opaque
  correlation to a host-private launch record.
- `RunStateV2`: persistent ledger of tasks, waves, budget, usage, failures,
  project binding, and state transitions.
- `NextWavePlanV1`: the next legal host action and spawn/follow-up assignments.
- `HostCapabilityManifestV1` and `SessionGuardStateV1`: the supported host
  surface, enforcement level, adapter modes, and a no-fallback guard bound to
  one run, project, state/ledger path pair, provider/model, executable identity,
  and worker limit. Guard states are `armed`, `blocked`, `released`, and
  `abandoned`.

JSON schemas are in `schemas/`. Runtime code performs dependency-free structural
checks before mutation, filters unknown/duplicate identities before accounting,
and persists state with an exclusive lock and revision compare-and-swap.

## Guard recovery boundary

The atomic parent holds a private `flock` guard lock across runner execution and
passes the locked descriptor into the runner process. A parent `SIGKILL` does
not release orchestration authority while the child runner is still alive.
Guard JSON and lock files are owner-only and opened without following symlinks.

`resume` rechecks the immutable guard digest, run ID, exact project binding,
current host-manifest digest, state/ledger path digests, provider, selector,
resolved model, and full executable identity. It may continue a clean
pre-launch or cycle-budget interruption. It does not replay a launch receipt
without terminal evidence, a pending/unknown effect, or an unmatched validator
start. Those conditions keep the guard blocked and parent fallback denied.

`abandon` is a terminal operator transition, not a replay or completion claim.
It is allowed only when the lock is free and append-only ledger plus run state
show no ambiguous external effect. A planned launch identity without a host
launch receipt is pre-launch evidence; the receipt is the durable boundary that
must be committed before an adapter process starts. The guard binding digest is
tamper-evident cooperative integrity, not a signature against the file owner.

## Provider and model policy

`providers/models.json` contains configurable profiles rather than unverifiable
live model claims. `fast`, `balanced`, and `frontier` profiles describe
provisional relative cost, latency, supported task classes/efforts, and risk
ceiling. Auto mode sorts eligible profiles by that registry-relative metadata,
using registry tie order only when those values tie. This is neither calibrated
quality evidence nor a measured-savings claim. Requested aliases SOL, Terra,
and Luna map to Codex frontier, balanced, and fast profiles, but the host must
attest the actual resolved model.

Claude and MiniMax are compatibility-disabled by default. Codex, Grok, and
Local have routing profiles, but only Grok and Local currently have admitted
bounded-file hosted adapters; Codex is compatibility-gated. An entrypoint must
still explicitly expose and manifest-pin each adapter. Local no-tool work needs
fresh exact capability/bridge evidence and verified response usage. Local work
that exposes tools remains blocked without signed launch attestation and
sandbox proof.

## Safety boundaries

- Sensitive inputs, private account tools, git mutation, destructive actions,
  and final review remain parent-owned.
- Conceptual phrases such as “token budget” are not credentials.
- External workers receive allowlisted fields and explicitly bounded inputs.
  Known path/pattern screening reduces accidental disclosure but does not prove
  accepted text is secret-free; sensitive repositories and files are out of scope.
- No provider automatically receives a home directory, agent runtime state, or
  an entire private knowledge base.
- Local capability claims require `LocalCapabilityAttestationV1` from the host,
  binding selector alias separately from exact model identity plus fresh mode,
  context, task/risk evaluation, reasoning efforts, and tools.
- Implementers cannot write until the host binds a non-overlapping write set.

## Current non-goals

AgentFanout does not itself hold provider credentials, open private accounts,
commit changes, or deploy. Its controller emits legal actions; the mandatory
host wrapper enforces normalized evidence around host execution. Provider-native
process launch remains adapter-owned. The hosted reference entrypoint implements
bounded-file Grok fixed-endpoint HTTPS and credential-free attested Local lanes.
Codex is
currently unavailable: its synthetic-provider probe cannot prove real-provider
parity, and no trusted real-provider attestation mechanism is bundled. Codex
CLI 0.144.1 also advertises built-in tools. No provider
failure authorizes parent fallback. Every admitted path accepts only bounded
files that pass configured path and known-pattern checks. Those checks are not
a confidentiality attestation; sensitive inputs remain prohibited.
