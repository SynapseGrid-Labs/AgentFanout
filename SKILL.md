---
name: AgentFanout
description: AgentFanout is the lazy provider-agnostic skill and policy layer for useful agent fanout. USE WHEN launch parallel agents, use AgentFanout, AgentOrchestrator, OrchestratorMode, delegate this, delegate work, spin up workers, worker agents, run validators, cheapest capable model, local-first execution, MiniMax workers, Codex subagents, Claude agents, local LLM workers, multi-agent review, fan out subtasks, specialist agents, coding agents, custom agents, provider-agnostic orchestration, cost-aware runtime routing. This skill decomposes work, shapes minimal specialist worker capsules, routes to capable providers, keeps MCP/secrets/git/destructive actions/final integration in the main session, validates worker outputs, and synthesizes the final result through the host runtime.
---

# AgentFanout

AgentFanout is the lazy skill and policy layer for parallel agents, delegated workers, validators, specialist worker capsules, local-first execution, and cost-aware provider routing.

AgentFanout is not injected into session startup. It loads through compact skill routing only when orchestration is useful.

## Core rule

The main session remains the orchestrator, security boundary, and final integrator.

For broad investigation, repo search, infrastructure research, and multi-surface debugging, the main session must dispatch workers before doing substantive research when a worker mechanism is available. After dispatch, the main session coordinates, waits, validates, and integrates; it must not duplicate the workers' SSH diagnostics, filesystem sweeps, repo-wide search, or substantive research while workers are running. If no worker mechanism is available, report `fanout_blocked` instead of silently doing all research in the main session.

It may use host-runtime worker mechanisms for bounded helpers when available, but it must keep these responsibilities in the main session:

- MCP/tool work: memory, calendar, email, chat, browser automation, workspace tools, private search, or any other private/tool-backed system.
- Secrets, credentials, private keys, tokens, customer-private data, and provider-sensitive material.
- Git state changes: commit, rebase, cherry-pick, reset, blame-driven decisions, branch changes, release tagging.
- Destructive actions: delete, wipe, deploy, migrate, irreversible writes, force-push, schema-destructive operations.
- Final answer, final synthesis, final patch integration, and user-facing conclusions.

## Activation workflow

Do not perform repo-wide, vault-wide, or home-directory searches to rediscover AgentFanout before routing. Once this skill is selected, use this skill directory directly, read only the minimal referenced files needed, and run `scripts/route.py` before any broad search or substantive research.

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
- `AgentFanout MiniMax` for one-task MiniMax provider-primary execution
- `AgentFanout MiniMax Mode` for persistent MiniMax provider mode
- `AgentFanout Local` for one-task Local provider-primary execution after live capability checks
- `AgentFanout Local Mode` for persistent Local provider mode after live capability checks
- `AgentFanout Codex` for one-task Codex provider-primary execution
- `AgentFanout Codex Mode` for persistent Codex provider mode
- `AgentFanout Claude` for one-task Claude provider-primary execution
- `AgentFanout Claude Mode` for persistent Claude provider mode
- `AgentFanout HIGH`
- `AgentFanout Auto`
- `AgentFanout Clear`
- `AgentFanout Reset`

Fanout is automatic and guarded. AgentFanout chooses the useful number of workers from task decomposition, runtime/provider capacity, validation overhead, write-scope conflicts, and safety gates.

## Worker capsules

Every delegated worker receives a minimal specialist capsule by default. Do not load automatic memory, relationship history, full Agents context, full CodexAgent context, voice metadata, or private MCP context into worker prompts.

Research workers may use scoped read-only tools when explicitly assigned: repo/file reads, filesystem search, runtime-approved knowledge-base retrieval, public web or docs research, read-only SSH diagnostics, and read-only database queries where the environment already permits them. They must not perform private-account side effects, write state, touch secrets, mutate git state, perform destructive actions, or own final synthesis.

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
orchestration_style: automatic_guarded|provider_primary
fanout:
  policy: automatic_guarded|provider_primary
  planned_workers: <number>
  rationale: <why this count is useful>
provider_primary:
  active: true|false
  codex_review_scope: CompactResultPacketV1_only_unless_escalated
  max_provider_repair_loops: 2
dispatch:
  status: launch_required|fanout_blocked|not_required
  main_session_policy: coordinate_wait_integrate|report_block|handle_directly
  launch_before_main_research: true|false
  main_session_may_duplicate_worker_research: false
  fallback_allowed: true|false
executor:
  role: main|explorer|worker|external-worker|local-worker
  provider: current|codex|claude|local-llm|minimax|future
  model_selector: <selector>
  reasoning_effort: low|medium|high|xhigh
provider_capability_snapshot:
  provider: <provider>
  selected_model_or_runtime: <selector/model>
  available_tools: []
  supports_file_editing: true|false
  validation_eligible: true|false
structural_validation:
  schema: StructuralValidationResultV1
  required: true|false
validator:
  required: true|false
  provider: <selector/provider>
  scope: <bounded checklist>
compact_result_contract:
  schema: CompactResultPacketV1
integration: main-session-only
references_to_load: []
```

## When not to delegate

Do not delegate merely because this skill is active. Stay main-session-only when:

- The task is a one-line edit, trivial copy change, or simple answer.
- The task requires MCP or private account tools.
- The task touches secrets or private credentials.
- The task includes credential-shaped syntax such as bearer headers, API-key prefixes, access-key assignments, JWT-like values, private key blocks, or password-bearing DSNs.
- The task changes git state.
- The task is destructive or irreversible.
- The expected orchestration overhead exceeds savings.

If delegation is useful but all worker providers are unavailable, stop and surface the block. Do not treat that state as permission for the orchestrator to consume the full research context itself.

## Built-in deterministic helper

Use `scripts/route.py` for dry-run routing checks, prompt sanitization checks, and fixture tests. The script is advisory; the main session still owns final judgment.
