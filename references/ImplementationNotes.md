# Implementation Notes

## Architecture Decision

Use one lazy `AgentFanout` skill as the execution policy for useful delegation and fanout.

AgentFanout combines:

- Orchestration routing concepts: provider preferences, local-first routing, hard gates, and validators.
- Agent composition concepts: compact specialist worker identity, with richer named/custom/personality composition only when needed.
- Codex adapter concepts: explorer, worker, and validator packet shaping.

AgentFanout is not injected into startup context. A host runtime should expose only compact routing metadata at startup and load this skill only when the request benefits from orchestration.

## Token Policy

- Keep `SKILL.md` lean and navigational.
- Route first; load details second.
- Generic workers receive compact task-specific capsules from `references/AgentCapsules.md`.
- Full agent/persona context loads only for named, custom, personality-heavy, or trait-heavy agents.
- Full Codex-specific context loads only for deeper Codex persona/profile behavior.
- Worker prompts must not include automatic memory, relationship history, full skill bodies, or voice metadata.

## Provider Fallback

Auto mode tries the cheapest capable provider chain for the task instead of stopping at the first unavailable provider. Low-risk bulk work prefers local, then MiniMax, then Codex. Coding/editing prefers Codex first. High-risk review/planning prefers Codex, then MiniMax, then Claude, with local only after stronger providers are unavailable.

Runtime readiness flags use the `AGENT_FANOUT_*` prefix. Legacy `AGENT_ORCH_*` flags are tolerated only for compatibility.

## Compatibility Decision

- `AgentOrchestrator` is a compatibility trigger and redirects to AgentFanout where a host runtime provides that wrapper.
- `OrchestratorMode` is a compatibility trigger and redirects to AgentFanout where a host runtime provides that wrapper.
- `CodexAgent` remains Codex adapter/reference material and cannot own global routing policy.
- Legacy triggers may provide one-turn provider hints, but only `AgentFanout ...` commands mutate conversation state.

## Approval-to-Execution

If AgentFanout created a plan and the next user message clearly approves it, execute immediately unless a safety exception applies. Runtime Plan Mode, stale plans, ambiguous approvals, and unapproved destructive/production/secret/git-state operations pause execution.

## Portable Runtime Assumptions

- Skill registry root: `$PAI_DIR/skills` or `$PAI_HOME/skills`, falling back to `~/.pai/skills`.
- MiniMax readiness: `AGENT_FANOUT_MINIMAX_AVAILABLE=1` or a MiniMax wrapper at `~/.pai/skills/MiniMax/Tools/MiniMaxExec.ts`.
- Local readiness: `AGENT_FANOUT_LOCAL_AVAILABLE=1`.
- Codex readiness: Codex runtime environment variables or `AGENT_FANOUT_CODEX_SUBAGENTS=1`.
- Claude readiness: `AGENT_FANOUT_CLAUDE_AVAILABLE=1`.

## Verification

- `python3 tests/run_tests.py`
- `python3 -m py_compile scripts/route.py tests/run_tests.py`
- `scripts/github-preflight.sh`
- `python3 scripts/route.py "review twenty independent modules"` should route to automatic guarded fanout when a provider is available.
- `python3 scripts/route.py "AgentOrchestrator MiniMax review three modules"` should treat the legacy phrase as a one-turn provider hint and leave `next_state.provider_mode` as `Auto`.
