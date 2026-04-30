# Lazy Execution

AgentFanout uses routing first and execution depth second.

## No Pre-Route Sweeps

After AgentFanout is selected, do not run repo-wide, vault-wide, or home-directory searches to find AgentFanout routing details. Use the selected skill path, read `SKILL.md` and the directly referenced route/provider files only, then run `scripts/route.py`. Broad search belongs inside scoped workers after dispatch, not in the orchestrator before route.

## Default

Stay in the main session for trivial prompts, direct answers, one-line edits, private-tool work, secrets, git state changes, destructive actions, and final synthesis.

## Load Order

Load only what the current route needs:

- AgentFanout core: orchestration-worthy requests.
- `references/AgentCapsules.md`: any delegated worker.
- `references/ApprovalExecution.md`: pending plan approval.
- `adapters/<Provider>.md`: only the selected provider.
- Full Agents skill: named, custom, personality-heavy, or trait-heavy workers.
- CodexAgent references: deeper Codex persona/profile behavior, not ordinary Codex workers.

## Token Rule

Worker prompts must be small task packets. Do not include automatic memory, relationship history, full skill bodies, full profile context, unrelated repo context, or voice metadata.
