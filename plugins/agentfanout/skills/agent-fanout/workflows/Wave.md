# Wave

1. Decompose bounded independent work with stable task IDs and launch the
   smallest useful wave using two-phase acknowledgement accounting.
2. Wait for active owners. Accept compact, correlated results containing run,
   task, wave, attempt, receiver, result, evidence, uncertainty, contradictions,
   and residual work; reject stale, duplicate, or mismatched callbacks.
3. After every completed batch, rebuild the residual ledger in a distinct
   replan step.
4. Follow up the same worker only for unchanged scope; use a fresh worker for
   changed scope or independent validation.
5. Delegate every eligible residual. Do not perform it in the parent.
6. Parent validation is structural. Use a fresh worker for substantive checks
   when risk, contradiction, or the user requires it.
7. Finish only after the post-batch replan proves residual work empty.

Transitions: active owner -> wait; unchanged scope -> follow up; changed scope
or validator -> spawn; cancellation -> interrupt then reconcile; unavailable
capacity -> bounded wait or explicit block. Terminal states are completed,
failed, blocked, and cancelled.

Require non-empty host worker IDs. Respect cancellation and explicit worker,
attempt, wave, token, and time ceilings. Treat missing capability or launch
acknowledgement as `fanout_blocked`, never as permission for parent takeover.

On Codex, use the plugin hook's one-time decision token only in its exact hidden
completion or blocked marker. A new worker invalidates the prior token.
