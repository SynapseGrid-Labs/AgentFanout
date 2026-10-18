# Orchestration telemetry

Telemetry records whether work was genuinely offloaded, continued through repair
waves, and satisfied host acceptance limits. It does not prove that a provider
prevented an over-budget call. Provider invocation by itself is not a successful
offload.

## Event identity

Every event carries:

```json
{
  "schema_version": 1,
  "timestamp": "UTC timestamp",
  "run_id": "unique orchestration run",
  "wave_id": "monotonic wave within the run",
  "task_id": "unique bounded task",
  "parent_task_id": null,
  "worker_id": "stable worker or continuation handle",
  "event": "route|launch|policy_denied|result|validation|replan|terminal|retention"
}
```

IDs must not be constant placeholders. An adapter-supported continuation retains
`worker_id` and increments its continuation counter. Spawn-only adapters mint a
replacement ID and record why reuse was unavailable. The currently admitted
Grok and Local strict-runner lanes are spawn-only; strict-runner Codex worker
admission is blocked. Native Codex plugin waves use hook lease counters and do
not claim provider-runtime receipts.

## Route decision

Record:

- normalized task class and independent work units;
- safety constraints and write ownership;
- requested and selected provider;
- requested selector and actual model identifier;
- requested and actual reasoning level;
- capability snapshot source, age, model context window, and task profiles;
- planned workers, concurrency limit, and rationale;
- per-task turn, token, time, and tool-output budgets; and
- expected validator and result contracts.

Unknown model identity, context window, capability profile, or selector
resolution is recorded as unknown and `validated=false`. Never infer a
validated capability from endpoint reachability or the presence of a wrapper
file.

## Worker accounting

For every model turn and worker result, record:

- prompt, cached, completion, and total tokens;
- cumulative worker and run tokens;
- estimated hosted-provider cost or local compute duration;
- model identifier returned by the provider;
- latency, retry count, and finish reason;
- tool name, status, elapsed time, input/output byte counts, and denial code;
- files touched and verification commands;
- compact evidence references rather than full tool output;
- final status and normalized error codes; and
- whether structural and adversarial validation passed.

Token counts must be promoted into the normalized result. They must not exist
only in an optional transcript.

## Wave lifecycle

Emit explicit events for:

1. `wave_planned`
2. `worker_launched`
3. `worker_result_received`
4. `structural_validation_completed`
5. `gap_analysis_completed`
6. `repair_dispatched` or `integration_authorized`
7. `wave_completed`

Gap analysis records each unmet acceptance criterion and its disposition:

- follow up with the same worker;
- replace the worker;
- cross-provider validation;
- fail closed; or
- replan to another already approved adapter when route policy explicitly
  permits provider substitution.

Main-session implementation after a failed wave is not a legal fallback for a
delegable residual. Record the block, budget state, failed route decision, and
policy evidence; keep terminal authorization denied.

## Session-guard recovery

Record and expose the guard status (`armed`, `blocked`, `released`, or
`abandoned`), immutable binding digest, run ID, and recovery assessment. The
assessment reports whether an orchestrator process still owns the lock,
pre-launch planned correlations, unknown-effect hazards, and separate
`resume_allowed` / `abandon_allowed` decisions. Never turn missing ledger
evidence into permission to replay an already receipted launch. Explicit
provider choice is route policy, not economic consent; record
`overhead_accepted` only when the dedicated host flag supplied the router's
acceptance phrase.

## Efficiency metrics

Calculate at completion:

- `offload_success_ratio`: successful bounded tasks divided by launched tasks;
- `delegated_repair_ratio`: repair tasks delegated divided by all repair tasks;
- `main_session_execution_actions`: substantive main-session tool/edit actions;
- `main_session_execution_tokens`: main-session tokens after first dispatch;
- `waves_completed` and `followup_dispatch_latency_ms`;
- `duplicate_research_actions` detected across workers and main session;
- `token_budget_utilization` by worker, wave, provider, and run; and
- `validation_overhead_ratio`.

The core regression metric is whether the orchestrator resumes substantive
execution after results arrive without first dispatching eligible repairs.

## Budget and circuit-breaker events

Budget failures use stable codes:

- `model_turn_budget_exhausted`
- `token_budget_exceeded`
- `tool_output_budget_exceeded`
- `worker_timeout`
- `worker_failure_threshold`
- `required_worker_launches_missing`
- `capability_not_validated`

Include configured limit, observed value, wave, worker, and next action. Do not
silently raise a limit or change providers.

## Privacy

Do not log prompts containing secrets, raw credentials, private-account data,
or unrestricted tool output. Store hashes, sizes, bounded evidence references,
and redacted error summaries. Provider-side usage metadata and local compute
timings are safe only after this sanitation step.

Allowlisting and sanitation reduce disclosure risk but do not establish that
data is safe. Do not enable telemetry for sensitive source material solely
because it passed these checks.

`agentfanout.telemetry.sanitize_event` implements the client-side allowlist.
`JsonlSink` provides a private append-only fallback and `LangfuseHttpSink`
forwards only sanitized operational events. A sink failure must never cause raw
content to be sent as a fallback.

Operational telemetry uses `replan` for the post-validation routing decision;
`repair` describes a task outcome, not a telemetry event type. Telemetry sinks
are diagnostic: delivery failures are isolated from control-plane mutations and
are recorded as `telemetry_delivery_failed` ledger evidence when that ledger is
available. They never roll back or misreport an already durable requeue.

Langfuse endpoints must use HTTPS and an explicit hostname allowlist. Plaintext
HTTP is rejected by default. The only exception is the loudly named
`dangerously_allow_insecure_loopback_http_for_testing` option (or the matching
retention-script flag), which accepts numeric loopback addresses only and must
never be enabled in a deployed configuration.

The retention utility never accepts the Langfuse secret key as a command-line
argument. Prefer `--secret-key-file` (or `LANGFUSE_SECRET_KEY_FILE`) with a
regular, single-link file owned by the current user and set to exactly `0600`;
`LANGFUSE_SECRET_KEY` remains available for controlled environments. Do not
place secrets in shell arguments, command examples, or process supervisors'
public argument lists. If both sources are configured, the utility fails closed
instead of choosing one. The file check covers POSIX ownership and mode bits,
not ACL grants or a compromised host, and Python cannot guarantee that secret
bytes are immediately erased from process memory.

`JsonlSink` creates its private directory and file with modes `0700` and `0600`
before writing data. It rejects symlinks, non-regular files, changed inode
identities, multiple hard links, unexpected ownership, and pre-existing modes
that are not exactly private. These checks cover POSIX mode bits; they are not a
substitute for reviewing ACLs, mount options, backup access, or host compromise.
