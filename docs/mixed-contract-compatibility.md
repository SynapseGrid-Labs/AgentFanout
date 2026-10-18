# Hosted mixed contract compatibility

Native skill and native V2 packet contracts are unchanged. Mixed hosted runs
use `HostedRunStateV3` with the host-frozen `MixedRunContractV1` and an embedded
`RunStateV2` lifecycle payload. This is an additive envelope, not a rename of V2
or a second release registry. Runtime pins compose verified release provenance
with the actual entrypoint/contracts. An unverified runtime declaration cannot
establish release integrity.

| Writer / active run | Reader | Result |
| --- | --- | --- |
| Legacy V2 state / V1 guards | Legacy supported runtime | Original provider meanings and behavior |
| Legacy V2 state | New legacy path | Original contract; no automatic mixed migration |
| HostedRunStateV3 | Legacy lifecycle reader | `invalid_run_schema`; no downgrade |
| MixedRunContractV1 | Reader lacking the schema | Typed `reader_schema_unsupported` refusal |
| HostedRunStateV3 | Matching runtime and contracts | Validate envelope, routing, ancestry and lifecycle before mutation |
| HostedRunStateV3 | Different release/source/entrypoint/contracts | `active_run_runtime_mismatch`; retain the pinned runtime |
| New contract while feature disabled | Matching runtime | New admission refused; reconciliation remains possible |

`GrokCLI/grok_cli_subscription_acp` and `Grok/grok_direct_https` are distinct.
Coordinator Local uses `Local/coordinator_local`; old credential-free Local
contracts keep their original meanings. Hosted mixed Codex uses
`codex_cli_no_tool`, not a native worker result packet. Claude/Gemini cloud
presentation destinations do not grant hosted adapter capability.

Every assignment freezes worker and required-validator bindings, model/effort,
exact capability identity digest, numeric host reservation, destination and
resource evidence. Local roles require distinct reservations and independent
current coordinator observation matching domain, lease, fencing and serving
epoch; waiting and uncertain ownership block before worker dispatch. The host
must verify observation provenance through the coordinator adapter. JSON data
or flags alone cannot prove readiness. Queue waiting does not increment waves
or attempts. Missing provider dollars never erase host reservation charges.

State writes use private permissions, a lock, compare-and-swap revision, fsync
and atomic replacement. The envelope contract cannot change during a save.
Every task must bind to a frozen root or an acyclic residual descendant whose
worker and validation policy remain identical. Residuals cannot expand allowed
paths/write authority. Explicit transition hypotheses can be validated against
the frozen allowlist; V1 persisted routing changes remain refused; new runs may explicitly select
MixedRunContractV2 with HostedRunStateV4 and the host transaction below.

Egress policy lists exact destinations for inputs, results, peers, artifacts,
errors, residuals, telemetry, retained sessions, validators and parent synthesis.
Host-created policy accompanies packets; model-supplied policies have no
admission authority. Peers are filtered under each source task's policy before
cross-provider validation, without exposing denied task identifiers or error
text. Cloud parents require cloud authorization even if all workers are local.
An authorized minimal summary is a new host-reviewed packet, not automatic
declassification. Existing secret screening remains mandatory in every lane.
Retained-session destinations do not enable deferred session reuse.

Rollback first disables new admission and reconciles exact effects and numeric
accounting. Unknown ownership/cancellation, outstanding inference or missing
accounting blocks activation changes. Preserve/quarantine unresolved runs under
the compatible runtime. Activate only an independently verified immutable prior
release and restore scoped frontend mappings. Never restore old databases over
new ledger work, rewrite event hashes, map GrokCLI into Grok, or bypass identity,
coordinator or evidence checks. Empty active-effect lists must come from the
host's durable journal, not from an adapter claim.

Tests here are synthetic authority, egress and persistence checks. They are not
Grok subscription, exact-model, resource, or live mixed-run conformance evidence.

`evaluation.py` validates complete matched serial/concurrent or separately
matched routing measurements. Model/profile/effort and warm/cold conditions must
match for a concurrency comparison. Cancellation uncertainty or incomplete
usage prevents comparison. Memory remains per domain; parent-context reduction
never generates a savings claim. Missing provider cost stays unknown. Synthetic
records remain synthetic; even a `live` evidence label cannot automatically
permit a live claim without independent evidence review. These helpers perform
no inference, switching, account changes or benchmark collection.


Profiled hosted orchestration also holds a private, owner-only, no-symlink
`.driver.lock` sibling for the complete run lifetime, before state loading or
ledger mutation. This nonblocking lock is separate from the state-save CAS lock.
A competing controller receives `profiled_driver_already_active` before creating
sessions, appending events or dispatching inference. The lock inode is retained;
process exit releases ownership, but unresolved effects still require normal
ledger reconciliation and cannot authorize replay.

The additive `ProfiledSessionGuardV1` ledger event binds the exact run, contract,
runtime, state path and host/parent destinations. Recovery requires exactly one
identical guard event; changed or duplicate events refuse admission without
rewriting history. Existing mixed history lacking this guard is retained under
its compatible runtime and receives `active_run_profiled_guard_missing`. Legacy
SessionGuardStateV1 is not reinterpreted. Old state readers already refuse the
HostedRunStateV3 envelope; a guard is not an adapter-created authorization receipt.


## Persisted routing transactions

New V2 contracts freeze every edge's exact assignment, role, source binding,
target binding, reason and maximum count. V4 state adds an append-only transition
history; V3 and V1 readers refuse these schemas. Existing V3 runs are not silently
migrated, and active runs remain pinned to their original runtime. No contract,
egress policy or prior ledger hash is rewritten.

The host-only `ProfiledHostedRunner.transition` shares the controller lifetime
lock. It requires current, independently reconciled absence of effects and
complete accounting, pinned to the exact state revision and ledger tip. Pending
or unconsumed launches, uncertain cancellation, accepted worker results, passed
validation, unavailable validators, changed epochs, denied egress and insufficient
numeric reservations refuse before authorization. Failed/blocked worker tasks
may return to pending only after these proofs; attempts and wave limits remain.

The host durably authorizes the transaction, then the runner appends its intent,
atomically saves the new binding/history, and appends its commit. Recovery
completes a journaled intent once, or refuses conflicting state, missing authority,
changed proofs or later effects. Both crash windows preserve existing guards and
ledger history. A repeated transaction UUID is idempotent only for the identical
change. This transaction changes routing data; it cannot acquire, release, load,
cancel or claim ownership of any runtime. New production controls remain disabled.

Worker transitions preserve the existing numeric task reservation basis. V2
contracts refuse edges that change that charge; they cannot silently lower it or
promise a receipt the current worker planner cannot produce. Required-validator
reservations remain separately bound and checked against the run budget.
