# Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Over-delegating tiny tasks | Tiny-task gate; main-session-only for one-line/simple work |
| Leaking secrets to external providers | Prohibit sensitive inputs; enforce configured path policy and best-effort credential screening; treat detection as defense in depth, not DLP |
| False MCP detection | Maintain cue list; allow main session to override only with explicit evidence no private/tool use is needed |
| Bad local-model routing | Require launch-bound capability and sandbox proofs; block explicitly with `fanout_blocked`; never fall back to parent execution |
| Provider adapter drift | Keep adapters thin; selectors instead of hardcoded model names; periodic adapter review |
| Hardcoded model names become stale | Use selectors resolved by adapters or environment config |
| Context bloat | Scope worker packets; chunk long context; include only needed evidence |
| Validators duplicating worker tasks | Validator checklist forbids redo; bounded inspection scope |
| Spoken-summary hook leaking into subagent output | Prompt sanitizer and output scan in `references/NoVoice.md` |
| Orchestration overhead exceeds expected benefit | Delegation-worthiness heuristic and tiny-task gate; treat registry costs as provisional and claim no measured savings without runtime receipts |
| Provider-specific capability incorrectly generalized | Adapter capability flags and `canHandle(task)` checks; treat capability stubs as launch proof, not calibrated quality; require a separate receipt-bound validator process |
| Overlapping worker edits | Explicit write sets; no overlap; main session integration |
| External provider unsupported tools | Adapter exposes `supportsTools`; hard gate routes MCP/tool work to main |
| User requests a provider that is unsafe for the task | Constraint gates override provider preference and explain why |
