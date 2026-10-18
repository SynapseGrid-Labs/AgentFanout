# Cost and Context Accounting

AgentFanout attempts to reduce and measure total usage subject to capability,
safety, and validation. “Cheapest capable” is a future calibrated-routing
objective; current registry-relative routing proves neither a cost minimum nor
model capability.

## Inputs

Each routing decision records:

- estimated input and output tokens per worker
- requested task units and workers in the current wave
- provider profile relative cost and latency
- task risk and reasoning effort
- parent-only baseline cost units
- requested alias and host-private resolved-model launch record

Registry values are relative defaults. Exact provider prices and live model names
must be supplied by runtime configuration; AgentFanout does not publish guessed
prices as facts.

## Budgets

`RunStateV2` applies host-side reservation and acceptance limits to:

- workers per wave
- total waves and total workers
- attempts per task
- total verified-USD cost when available, otherwise conservative reservation units
- worker prompt and result tokens
- parent-ingested result tokens per wave
- serialized bytes per worker result
- consecutive no-progress waves

Before dispatch, the controller reserves a fair per-task accounting cap from the
remaining run budget; the sum of used and reserved accounting units may never
exceed the run cap. Before ingestion, it releases only the active tasks'
reservations, projects their host-attested usage, and rejects the entire batch
if the cap would be exceeded. A 0.01-unit run therefore cannot ingest a
1.00-unit result. These host controls stop acceptance and later waves but cannot
prevent tokens or charges already consumed by a call. A cap is provider-enforced
only when an adapter supplies and verifies a native pre-call limit. Do not label
reservation units as dollars.

The host computes serialized result bytes and conservative parent tokens itself.
A worker cannot bypass a parent-context cap by omitting or understating a usage
field. Unknown and duplicate identities are filtered before they can reserve
context or cost.

## Decision objective

For an active eligible provider profile:

```text
expected relative usage
  = workers × (
      launch overhead + bounded payload + estimated output
      + required validator launch overhead + validator input/output
    ) × profile relative cost
```

Routing filters profiles by task class, requested reasoning effort, and risk
ceiling, then sorts the survivors by registry relative cost and latency.
Registry tie order is used only for equal scores. Provider selection does not
waive the anti-waste comparison: the route still compares the selected profile
with a parent-only baseline unless a separate overhead-acceptance or force
control is present. Low/medium classification, transformation, and creative
packets normally use deterministic structural/evidence checks; higher-risk or
explicitly independent work includes semantic validator calls. Savings remain
unverified until the host records measured usage.

## Model and effort separation

Profiles and effort are independent axes:

- `fast`, `balanced`, and `frontier` select the cheapest registered capability
  profile that admits the task class and risk.
- `low/light`, `medium`, `high`, `extra-high/xhigh`, and `ultra` select the
  reasoning effort within a capable profile.
- A fast profile may run higher reasoning for a bounded task when the registry
  proves that combination; high effort alone must not promote the model.
- A frontier profile may run low effort when the task needs frontier capability
  but little deliberation.

Requested SOL/Terra/Luna labels are aliases mapped through the registry. They are
not treated as verified live model identifiers. The host must record the actual
provider model, effort, usage, and launch binding outside the worker result.

## Escalation and de-escalation

Escalate a task—not the entire run—when capability evidence, validation, or
repeated failure requires it. After a hard task is resolved, route later simple
tasks back to a lower registry-relative profile. Context reuse can reduce prompt
tokens only for adapters that separately implement and attest it. None of the
currently admitted bundled adapters claims retained provider context; Grok and
Local follow-up waves and every validator pay independent startup and context
overhead.
