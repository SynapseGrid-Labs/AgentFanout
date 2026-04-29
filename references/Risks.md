# Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Over-delegating tiny tasks | Tiny-task gate; main-session-only for one-line/simple work |
| Leaking secrets to external providers | Secrets gate; redact before delegation; main session only when secrets are required |
| False MCP detection | Maintain cue list; allow main session to override only with explicit evidence no private/tool use is needed |
| Bad local-model routing | Local readiness gates; fallback reasons; validate local output |
| Provider adapter drift | Keep adapters thin; selectors instead of hardcoded model names; periodic adapter review |
| Hardcoded model names become stale | Use selectors resolved by adapters or environment config |
| Context bloat | Scope worker packets; chunk long context; include only needed evidence |
| Validators duplicating worker tasks | Validator checklist forbids redo; bounded inspection scope |
| Spoken-summary hook leaking into subagent output | Prompt sanitizer and output scan in `references/NoVoice.md` |
| Orchestration overhead exceeds savings | Delegation-worthiness heuristic and tiny-task gate |
| Provider-specific capability incorrectly generalized | Adapter capability flags; `canHandle(task)` checks; validator fallback |
| Overlapping worker edits | Explicit write sets; no overlap; main session integration |
| External provider unsupported tools | Adapter exposes `supportsTools`; hard gate routes MCP/tool work to main |
| User requests a provider that is unsafe for the task | Constraint gates override provider preference and explain why |
