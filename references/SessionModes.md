# Task-scoped provider selection

Public v1 has no conversation mode and no persistent provider preference.

Supported commands are:

- `AgentFanout <task>`
- `AgentFanout Codex|Grok|Local <task>`
- `AgentFanout Help`
- `AgentFanout Status` only while a run is active
- `AgentFanout Cancel` where the host supports cancellation

Provider selection is an input to one task. A completed or cancelled task
discards it. Unrelated later turns do not inspect state, call a router, launch
workers, or display AgentFanout output.

The native host reports spawn, wait, follow-up, cancel, result callback, model
selection, reasoning selection, and enforcement capabilities. Missing model or
reasoning controls disable related claims but do not disable native waves.

The optional Linux/POSIX runtime may persist an explicit per-run `RunStateV2`
for crash recovery and receipts. That runtime state is never a public v1
conversation mode and never activates AgentFanout on a later unrelated turn.
