# Wave

Use an ephemeral task-scoped run:

For an enabled V2 candidate use `workflows/V2.md` instead; never mix run states.

1. Parent decomposes independent work units and gives each a stable task ID.
2. Launch the smallest concurrent wave with bounded task packets.
3. Wait for active owners; ingest only compact, correlated result packets.
4. Rebuild the residual ledger from scope, evidence, uncertainty, contradictions, failures, and worker-reported residual work.
5. Follow up the same worker only when its scope is unchanged and the host supports it.
6. Start a fresh worker for changed scope or independent validation.
7. Use one validator only when risk, contradiction, or the user requires it.
8. Launch the next wave automatically while delegable residual work exists; do not perform it in the parent.
9. Complete only after a distinct post-batch replan proves residual delegable work empty; then release task state.
10. Cancel stops future waves, requests best-effort interruption of active workers, and reconciles their late callbacks without reopening work.
11. Host-enforced worker, attempt, and wave ceilings end in an explicit blocked state.
12. Use two-phase launch accounting: `dispatch_pending` consumes no wave or attempt; a non-empty host receiver ID atomically commits both. Failed acknowledgement removes the provisional assignment and records retry or block disposition.

Transition rules:

- active owner: `wait`;
- unchanged completed scope: `follow_up`;
- changed scope or validator: `spawn`;
- cancellation: `interrupt/cancel`, then reconcile;
- unavailable capacity: bounded `wait` or explicit blocked state, never parent takeover.

Lifecycle states are `pending`, `dispatch_pending`, `active`, `partial`,
`validation_required`, `completed`, `failed`, `blocked`, and `cancelled`.
Only `completed`, `failed`, `blocked`, and `cancelled` are terminal.

Capability handshake:

`spawn | wait | follow_up | interrupt/cancel | result_callback | launch_acknowledgement | model_selection: available|unavailable | reasoning_selection: available|unavailable | enforcement: advisory|harness_enforced|runner_enforced`

Worker task packets contain `run_id`, `task_id`, `wave`, `attempt`, task, scope,
inputs, limits, forbidden actions, and the compact result contract. Results add
`receiver_id`, `result_id`, summary, evidence refs, commands/outcomes,
uncertainty, residual work, and escalation. Reject stale, duplicate,
unacknowledged, or mismatched callbacks without mutating the ledger. Keep each
packet under 250 tokens and each ingested result under 300 tokens.

If model or reasoning selection is unavailable, continue orchestration and mark
those controls unavailable internally. Never turn advisory behavior into an
enforcement claim or raw transcript into parent context.

On Codex, a verified AgentFanout plugin lease supplies a one-time decision token
after every completed batch. Use the hook-provided token only in its exact
hidden completion or blocked marker. A new worker invalidates the prior token.
Without that verified lease, this workflow is advisory.
