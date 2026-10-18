# Compact Agent Roles

The six Markdown role files define AgentFanout's built-in operational roles. They are small
prompt capsules, not personality profiles, provider model names, or executable
processes. A host runtime binds a role to a `TaskUnitV1`, chooses a provider from
the registry, and launches a worker or schedules a later logical wave. Retained
context follow-up is adapter-dependent.

`openai.yaml` is portable Codex UI metadata for the skill and is not a worker
role, authority grant, provider configuration, or runtime policy.

| Role | Purpose | May edit? | Typical capability |
|---|---|---:|---|
| [Thin Orchestrator](thin-orchestrator.md) | Dispatch, collect, validate, replan, and continue waves | No task implementation | Strong coordinator |
| [Explorer](explorer.md) | Bounded read-only evidence collection | No | Fast or balanced |
| [Implementer](implementer.md) | Bounded changes in an explicit write set | Yes | Balanced or frontier |
| [Receipt-bound Validator](independent-validator.md) | Check exact claims, evidence, or diffs in a separate process | No | Same tier or one tier stronger |
| [Weak Local Worker](weak-local-worker.md) | Cheap extraction, classification, and summaries | Normally no | Verified local fast profile |
| [Strong Synthesis Worker](strong-synthesis-worker.md) | Draft a compact synthesis from result packets | No integration authority | Frontier when warranted |

Every role must receive:

- one bounded `TaskUnitV1`
- explicit allowed paths and write set
- host model, usage, and assigned-handle attestation requirements
- prompt, result, and cost budgets
- a `WorkerResultV1` output requirement

The Thin Orchestrator is the only role that manages `RunStateV2`. It may not
silently take over eligible worker work. Custom or personality-driven agents are
composed by the host runtime only when the user asks for them.
