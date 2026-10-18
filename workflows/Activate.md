# Activate

Select this skill only when the request explicitly asks for AgentFanout, fanout,
parallel agents, independent worker waves, delegated workers, or validators, or
when the task plainly names two or more independent bounded work units.

Do not activate for a simple answer, one small edit, ordinary coding, a single
private/tool-backed action, secrets, git mutation, destructive work, or a
question merely mentioning multi-agent systems.

When selected:

1. Classify the task and keep final synthesis in the parent after residual work reaches zero.
2. If parallelism is not worthwhile, continue normally without AgentFanout text,
   queue messages, router calls, state checks, or `not_required` output.
3. Snapshot the host capability handshake: spawn, wait, follow-up, cancel,
   callback, launch acknowledgement, model/effort selection, and enforcement.
4. Prefer native Codex agents; treat Grok and Local as optional host capabilities.
5. Launch the smallest independent wave and read only compact result packets.

V2 remains opt-in pending measured pilot acceptance. For an enabled candidate,
load `workflows/V2.md` before dispatch; the V2 planner, not category-derived
counts, supplies every assignment. Automatic V2 routing is Codex-only.

Public activation is task-scoped. Provider choice applies only to this task; never
store or infer a conversation-wide provider preference.
