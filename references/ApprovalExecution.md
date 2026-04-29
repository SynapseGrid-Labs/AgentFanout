# Approval Execution

When AgentFanout created a plan and the next user message clearly approves it, execute immediately.

## Clear Approval

Examples: `approved`, `implement it`, `do it`, `execute the plan`, `yes proceed`, `proceed with the plan`.

## Ambiguous Approval

Examples: `ok`, `sounds good`, `maybe`, `continue`, `interesting`.

## Exceptions

Pause instead of executing when:

- runtime Plan Mode forbids mutation
- approval is ambiguous
- repo/runtime state changed enough to stale the plan
- destructive, production, secret, or git-state operation needs separate confirmation not already included

## Rule

Do not ask “should I proceed?” after clear approval unless an exception applies.
