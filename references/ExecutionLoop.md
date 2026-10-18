# Execution Loop

This protocol is mandatory whenever routing returns delegable tasks.

## Parent invariant

The parent is a thin orchestrator. While eligible delegated work remains, it may
route, dispatch, wait, collect, validate, replan, follow up, enforce budgets, or
report a real block. It must not perform the worker's research, implementation,
debugging, filesystem sweep, or substantive synthesis itself.

Private-tool operations, secrets, git state, destructive actions with authority,
and final compact review remain parent-owned.

## Start

1. Route the request.
2. Create a `RunStateV2` from the emitted `TaskUnitV1` packets.
3. Resolve every required path/write binding with host-authored absolute paths;
   a binding block is stable and non-dispatchable.
4. Ask the controller for `NextWavePlanV1`.
5. Launch every assignment in that bounded wave concurrently when independent.
6. Persist the run state before waiting. State writes use lock + revision CAS;
   a conflict must be reloaded and replanned, never overwritten.

## Collect and validate

For each worker result:

1. Accept only a compact `WorkerResultV1`, not the full transcript.
2. Confirm the task is active and the result ID/task identity is new before
   reserving parent context or cost.
3. Require a summary plus evidence or changed files for success.
4. Require the opaque launch correlation to match an active host-private launch
   record. Never accept model, usage, or worker-handle fields from the worker.
5. Measure serialized result bytes and parent-ingested tokens on the host; use a
   host-recorded provider measurement when present, otherwise conservative
   non-zero accounting, and reject projected run-cost overflow before mutation.
6. Run the separately launched, receipt-bound validator when required; record
   whether it is same-model, model-diverse, or provider-diverse.
7. Treat malformed or unsupported success as a repairable failure, never as
   completion.

## Replan after every batch

After ingestion, call the controller again before taking substantive action.

- `wait`: more results from the current wave are required.
- `dispatch_wave`: launch the next assignments immediately.
- `blocked`: report the exact budget, dependency, capability, or no-progress
  reason.
- `complete`: perform only the compact final review.

Every safe bounded `remaining_work` item becomes a new task with fresh launch and
usage IDs. The controller—not the result—carries the host-issued assigned worker
handle into a logical `follow_up` recommendation. Reuse it only when the adapter
explicitly supports retained context, the bounded input is unchanged, and the
host ledger proves an unexpired provider-session binding to the same model,
effort, executable, and project. Otherwise dispatch a fresh worker. None of the
currently admitted bundled adapters claims retained context: Grok and Local use
fresh calls, and native Codex worker admission is blocked. Validation always
uses a separate process and never shares worker context.

## Termination

Completion requires every task to be `completed`; blocked or cancelled work does
not count as success.
The controller blocks on wave, worker, cost, retry, dependency, or no-progress
limits. A budget block is not permission for the parent to silently finish the
work itself.

An approval after a block does not change this rule. The only legal next step is
a new bounded plan using an already-approved capable worker, or an explicit
stop. Never substitute parent research, debugging, implementation, or residual
synthesis for `fanout_blocked` work.

The atomic CLI forwards `SIGINT` and `SIGTERM` to the durable runner instead of
killing only its supervisor. The runner requests cancellation from every active
worker and validator, waits for process-group confirmation, persists a blocked
shutdown outcome, and authorizes no terminal result. Use `agentfanout.py status`
with the emitted state path after an interrupted run. An uncatchable `SIGKILL`
can still leave an unknown external effect; restart recovery blocks ambiguous
receipts and never replays them automatically.

## Crash, effect, and state-store recovery

Treat a crash during or after a launch as an external-effect ambiguity, not as
a retry signal. The controller's durable artifacts have distinct roles:

- `RunStateV2` is the mutable control state: task status, launch records,
  budgets, validation status, wave index, and revision. Its normative schema is
  [`schemas/RunStateV2.schema.json`](../schemas/RunStateV2.schema.json).
- `HostLedger` is the append-only SQLite evidence record for host receipts,
  usage, accepted results, validator evidence, effects, and terminal
  authorization. It uses WAL with `synchronous=FULL`; it is not a replacement
  for the JSON control state.
- `EffectRecordV1` records an externally visible effect using its effect ID,
  idempotency key, intent hash, policy hash, and state. Its normative schema is
  [`schemas/EffectRecordV1.schema.json`](../schemas/EffectRecordV1.schema.json).

The two stores are persisted independently; there is **no cross-store
transaction or automatic reconciliation**. A valid JSON state and a valid
ledger therefore do not by themselves prove that an external effect completed.
Do not hand-edit either store to make their histories appear consistent.

### Safe operator procedure

1. Stop new dispatch for the affected run. Do not rerun the worker, validator,
   or effect as a first response.
2. Read the `RunStateV2` file and the ledger for the same run ID. Record only
   run/task/effect IDs, event types, timestamps, hashes, and terminal status in
   the incident record; never copy prompts, credentials, raw worker output, or
   access tokens.
3. For every `EffectRecordV1` in `pending` or `unknown_after_crash`, verify the
   effect against the authoritative external system using its idempotency key
   and intent hash. Absence of a result packet is not evidence that no effect
   occurred.
4. Do not replay an ambiguous effect automatically. `unknown_after_crash` is a
   terminal effect state in the current host implementation, and terminal
   authorization rejects both `pending` and `unknown_after_crash` effects.
   Leave the affected run non-terminal and stop; escalate for an explicit,
   separately authorized reconciliation or a new bounded plan after external
   verification.
5. Do not delete a state lock automatically. State persistence uses an
   exclusive `<state>.lock`, revision compare-and-swap, fsync, and atomic
   replace. On `run_state_locked` or `run_state_revision_conflict`, reload the
   state, establish that the prior process is quiescent, and replan; never
   overwrite a newer revision.

### Current recovery boundary

The runner recognizes a persisted launch receipt without an acknowledged
result as `launch_unknown_after_crash` and must never replay it automatically.
There is no operator command that converts an unknown pre-crash launch into a
confirmed cancellation: live adapter process-group evidence is held by the
runner's in-memory control and cannot be reconstructed after a crash. It does
not cover descendants that detach into another session or remote work. Preserve
the artifacts, verify the external effect, and escalate rather than editing
SQLite/JSON or reissuing the launch.

For a cancellation confirmed while the live host still controls the process,
`scripts/host-dispatch.py --requeue-json` provides a separate, auditable
transition back to `pending`. It accepts only exact receipt → termination →
cancellation evidence, refuses unresolved or applied effects and exhausted
budgets, and leaves the old launch record cancelled. The next controller plan
mints a fresh correlation ID. The single-task operation is idempotently
reconcilable when its ledger event committed before JSON state persistence; a
retry must use the same operator-reason hash. This is not recovery for an
unknown pre-crash launch and must never be described as such.

Normative implementation and regression references:

- `agentfanout/lifecycle.py`: `load_run_state()` and `save_run_state()` enforce
  size validation, exclusive lock, revision CAS, fsync, and atomic replace.
- `agentfanout/host_runtime.py`: `HostLedger`, `record_effect()`, and
  `authorize_terminal()` enforce append-only effect transitions and deny
  unresolved effects at terminal authorization.
- `agentfanout/runner.py`: `_resume_planned_wave()` identifies a persisted
  receipt without a result and attempts the fail-closed block path instead of
  replaying.
- `tests/run_tests.py::test_state_validation_locking_and_cas` and
  `tests/test_host_runtime.py::test_unresolved_effect_blocks_terminal` cover
  the CAS/lock and unresolved-effect invariants. No automated test presently
  proves end-to-end reconciliation of a pre-crash live process; that is an
  implementation gap, not an operator guarantee.

## CLI lifecycle

Use the atomic public path:

```bash
python3 scripts/agentfanout.py run \
  --project-root /exact/git/worktree --provider Grok \
  --grok-home /secure/agentfanout-grok-home \
  --grok-model grok-composer-2.5-fast --pretty -- \
  "review README.md SKILL.md docs/architecture.md references/ExecutionLoop.md"
```

It binds the root, arms the guard, preflights inputs, creates the run, executes
every worker/validator wave, and returns the state path. The lower-level
commands below are for adapter developers and compatibility tests.

Ingest a result batch and receive the next-wave plan:

```bash
python3 scripts/route.py --run-state-file RUN.json \
  --results-json '[{"schema":"WorkerResultV1", ...}]'
```

Resolve host path/write bindings for an existing run:

```bash
python3 scripts/route.py --run-state-file RUN.json \
  --bindings-json '[{"task_id":"task_...","allowed_paths":["/exact/repo"],"write_set":["/exact/repo/file"]}]'
```

The state path is selected by the host. Do not store credentials or raw worker
transcripts in it.

For a manually constructed state, the lower-level hosted runner additionally
requires guard-frozen provider, executable/helper, auth/capability, exact-model,
manifest, and ledger arguments. It is an embedding interface, not a public
quickstart; use `scripts/run-hosted-fanout.py --help` and the host-enforcement
contract rather than invoking an incomplete Codex example. Native Codex worker
admission is currently blocked.

The runner accepts both a pending run and the `collecting` state already
planned and persisted by `route.py`. Follow the crash/effect procedure above
for a persisted receipt without an acknowledged result; it must never be
replayed automatically.

The public CLI uses `--project-root`; the environment variable is a compatibility
input and a conflicting value is rejected. Do not use root, home, multi-project
ancestors, host-configured sensitive roots, or a full private knowledge base as
the project root.
