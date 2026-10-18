# When not to fan out

Continue in the parent, silently, when the work is a simple answer, one small
edit, one tightly coupled change, private/tool-backed work, secrets, git state,
destructive action, or when worker launch/context overhead exceeds the useful
parallel work.

Do not emit a no-op status, queue message, router result, provider warning, or
internal no-op label. A later unrelated turn never inherits AgentFanout or a
provider preference from a completed task.

Suppress low-risk fanout when the measured or conservative estimate exceeds the
configured engineering gate. Savings language requires a controlled benchmark
with parent, worker, validator, result-ingestion, cost, wall-time, and quality
measurements.
