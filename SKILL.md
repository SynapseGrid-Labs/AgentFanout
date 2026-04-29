---
name: AgentFanout
description: AgentFanout is the lazy provider-agnostic execution layer for useful agent fanout. USE WHEN launch parallel agents, use AgentFanout, AgentOrchestrator, OrchestratorMode, delegate this, delegate work, spin up workers, worker agents, run validators, cheapest capable model, local-first execution, MiniMax workers, Codex subagents, Claude agents, local LLM workers, multi-agent review, fan out subtasks, specialist agents, coding agents, custom agents, provider-agnostic orchestration, cost-aware runtime routing. This skill decomposes work, shapes minimal specialist worker capsules, routes to capable providers, keeps MCP/secrets/git/destructive actions/final integration in the main session, validates worker outputs, and synthesizes the final result.
---

# AgentFanout

AgentFanout is the lazy execution layer for parallel agents, delegated workers, validators, specialist worker capsules, local-first execution, and cost-aware provider routing.

AgentFanout is not injected into session startup. It loads through compact skill routing only when orchestration is useful.

## Core rule

The main session remains the orchestrator, security boundary, and final integrator.

It may launch bounded helpers, but it must keep these responsibilities in the main session:

- MCP/tool work: memory, calendar, email, chat, browser automation, workspace tools, private search, or any other private/tool-backed system.
- Secrets, credentials, private keys, tokens, customer-private data, and provider-sensitive material.
- Git state changes: commit, rebase, cherry-pick, reset, blame-driven decisions, branch changes, release tagging.
- Destructive actions: delete, wipe, deploy, migrate, irreversible writes, force-push, schema-destructive operations.
- Final answer, final synthesis, final patch integration, and user-facing conclusions.

## Activation workflow

1. Classify and route with `scripts/route.py`, `workflows/Route.md`, and `references/Routing.md`.
2. Apply lazy-loading rules from `references/LazyExecution.md`.
3. Shape minimal worker identities with `references/AgentCapsules.md`.
4. Apply cost and local-first policy from `references/CostPolicy.md` and `references/LocalFirst.md`.
5. Select provider behavior from `references/Providers.md` and only the matching `adapters/*.md` file.
6. Apply subagent constraints from `references/Subagents.md`.
7. Sanitize all delegated prompts with `references/NoVoice.md`.
8. Validate delegated results using `workflows/Validate.md` and `references/Validators.md`.
9. Integrate results in the main session.

## Session modes

Only exact full-line `AgentFanout ...` commands may mutate conversation state. Legacy `AgentOrchestrator`, `OrchestratorMode`, and `CodexAgent` trigger phrases are compatibility/discovery hints only.

Valid commands:

- `AgentFanout Status`
- `AgentFanout Help`
- `AgentFanout MiniMax`
- `AgentFanout Local`
- `AgentFanout Codex`
- `AgentFanout Claude`
- `AgentFanout HIGH`
- `AgentFanout Auto`
- `AgentFanout Clear`
- `AgentFanout Reset`

Fanout is automatic and guarded. AgentFanout chooses the useful number of workers from task decomposition, runtime/provider capacity, validation overhead, write-scope conflicts, and safety gates.

## Worker capsules

Every delegated worker receives a minimal specialist capsule by default. Do not load automatic memory, relationship history, full Agents context, full CodexAgent context, voice metadata, or private MCP context into worker prompts.

Load full Agents tooling only for named, custom, personality-heavy, trait-heavy, or user-requested specialist composition beyond the compact capsule.

## Approval-to-execution

If AgentFanout produced a plan and the next user message clearly approves it, execute immediately unless a safety exception applies. See `references/ApprovalExecution.md`.

## Minimal execution contract

Before delegation, produce a concise internal execution plan:

```yaml
task_class: <class>
constraints: [mcp|secrets|git|destructive|none]
worth_delegating: true|false
parallelism: none|serial-delegated|parallel
fanout:
  policy: automatic_guarded
  planned_workers: <number>
  rationale: <why this count is useful>
executor:
  role: main|explorer|worker|external-worker|local-worker
  provider: current|codex|claude|local-llm|minimax|future
  model_selector: <selector>
  reasoning_effort: low|medium|high|xhigh
validator:
  required: true|false
  provider: <selector/provider>
  scope: <bounded checklist>
integration: main-session-only
references_to_load: []
```

## When not to delegate

Do not delegate merely because this skill is active. Stay main-session-only when:

- The task is a one-line edit, trivial copy change, or simple answer.
- The task requires MCP or private account tools.
- The task touches secrets or private credentials.
- The task changes git state.
- The task is destructive or irreversible.
- The expected orchestration overhead exceeds savings.

## Built-in deterministic helper

Use `scripts/route.py` for dry-run routing checks, prompt sanitization checks, and fixture tests. The script is advisory; the main session still owns final judgment.
