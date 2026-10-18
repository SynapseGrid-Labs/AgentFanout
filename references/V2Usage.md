# V2 complete-task measurement

The host supplies request-delta usage; native launch intent is not proof of the
model used. Never copy requested_model into actual_model. Unknown usage and
unknown identity remain unknown, including for the parent and final synthesis.

`scripts/usage-ledger.py --ledger HOST_LOCAL_FILE` reads one metadata event on
stdin. `--run-id ID` returns a summary. Usage requires host, provider, request_id,
and usage_basis=request_delta; cumulative session samples are rejected. The ledger's parent directory must already
exist and cannot traverse symlinks. Keep it outside portable skills and source.
This optional ledger never calls a provider. Failure to record is observability
loss, not task failure. It does not replace the hardened runner's security ledger.
Earlier experimental three-column ledgers are incompatible and rejected on both
read and write; retain those files and select a fresh V2 ledger. No implicit
migration or deletion of historical records occurs.

Events require event_id, run_id, event_type and role. Use a stable host request ID
as event_id for usage; repeat delivery cannot increase totals even with a changed
event ID, because host/provider/request_id is also unique. Roles are parent,
worker, validator, retry, synthesis, audit, or system. Event types are plan, queue,
launch, ack, first_output, usage, result, validation, retry, reuse, cancel,
terminal, and hook. Host integrations must record one usage event per request,
not repeated cumulative snapshots. Attribute final synthesis separately only if
its request is excluded from parent usage. Input includes cached input; output
includes reasoning tokens. Do not add those subsets a second time.

Allowed metadata includes assignment/worker lineage, policy revision, surface,
configuration, requested/actual model and effort, counters, duration, classified
failure, and evidence hashes. Free-text prompts, tool output, local paths, and
unrecognized fields are not stored. Labels are bounded and screened for secrets.
Evidence content stays in the existing authorized host-local storage.

Dollar values require cost_basis: actual_charge or api_equivalent_estimate. Never
sum different bases or describe estimates as bills. Subscription credits are not
API dollars. Incomplete usage records produce unknown totals rather than zero.
Parallel worker durations must not be summed into wall time; measure elapsed
time at the root task. Include audit calls in the experiment's total cost.

Reconcile host request IDs across parent, workers, validators, retry layers,
audits and synthesis before marking any pilot run complete. The summary always
leaves complete_task_accounting_verified false: a local ledger cannot itself
prove that an upstream host omitted no calls. If the launch surface cannot
report actual models or complete usage, mark that pilot arm unavailable instead
of guessing. Deterministic fixtures do not establish model quality or savings.
