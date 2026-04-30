# Routing Reference

## Task classes

| Class | Description | Default route |
|---|---|---|
| `mcp-inline` | Needs memory, calendar, email, chat, browser automation, workspace tools, private search, or any MCP/private tool | Main session only |
| `long-ctx` | Very large context or many files beyond worker window | Main session plans chunking; workers only on scoped chunks |
| `git-ops` | commit, diff, rebase, cherry-pick, log, blame, branch, tag | Main session only for state changes; explorers may inspect read-only diffs |
| `bulk-read` | Read/grep/summarize many files or logs without edits | Parallel explorers or local workers |
| `bulk-classify` | Classify/label/extract from many independent items | Parallel workers; sample validator |
| `test-gen` | Write or modify tests | Bounded worker with explicit test file ownership; validator checks coverage |
| `data-xform` | ETL, CSV/JSON reshaping, log parsing, schema mapping | Worker/local worker; validator checks sample and schema |
| `debug` | Diagnose failures or runtime errors | Explorer for diagnosis; worker only for bounded fix |
| `plan` | Architecture, design, strategy, specs | Main session leads; explorer critique or alternative plan |
| `refactor` | Multi-file restructuring | Main session partitions write sets; workers with no overlap; validator required |
| `small-edit` | Single-file tiny edit or one-line copy/code change | Main session only |
| `reason` | Multi-step correctness or reasoning argument | Main session or high-capability explorer critique |
| `creative` | Prose, marketing copy, creative writing | Main session for tiny work; workers for variants |
| `security-review` | Security-sensitive review or threat analysis | Read-only explorer; main session final risk decision |

## Constraint gates

A constraint gate overrides provider preference.

| Gate | Detection cues | Route |
|---|---|---|
| MCP/private tools | account tools, private memory, email, calendar, chat, browser automation, workspace tools, private search | Main session only |
| Secrets | secret words plus credential-shaped syntax such as API keys, bearer headers, JWT-like values, access-key env vars, private key blocks, and password-bearing DSNs | Main session only; redact before any delegation |
| Git state | commit, rebase, cherry-pick, reset, tag, branch mutation | Main session only |
| Destructive | delete, wipe, drop, truncate, force-push, production deploy, irreversible migration | Main session only unless user explicitly authorizes and tool policy allows |
| Tiny task | one-line edit, simple answer, no parallel substructure | Main session only |

## Delegation-worthiness heuristic

Delegate when at least one condition is true and no hard gate blocks it:

- user explicitly requests agents/orchestration/delegation/validators
- there are independent files/modules/data chunks
- there are multiple review dimensions
- the task benefits from independent validation
- the task is high-risk and a second-pass critique materially reduces risk
- the task is large enough that parallelism or cheaper workers save time/cost

Do not delegate when orchestration overhead dominates.

Broad investigation is worker-first. Prompts such as "fully investigate", "find whether X exists", "trace how X talks to Y", or multi-surface host/repo/service research should produce research workers before the main session starts gathering evidence. After launch, the main session coordinates, waits, validates, and integrates; it must not duplicate the workers' SSH diagnostics, filesystem sweeps, repo-wide search, or substantive research. If no provider can launch workers, the route must expose `fanout_blocked` instead of silently converting the job into main-session research.

Research workers may receive scoped read-only tool access when assigned by the host runtime: repo/file reads, filesystem search, runtime-approved knowledge-base reads, public web/docs research, read-only SSH diagnostics, and read-only database queries where configured. These are not permission to perform private-account side effects, handle secrets, change git state, run destructive operations, or write final synthesis.

## Independence test

A subtask is independent only if it has:

- a bounded scope
- a clear input package
- no MCP requirement
- no secret requirement
- no overlapping write set with another worker
- a locally checkable output contract
- a validator strategy if its output will affect final integration

## Reasoning-effort defaults

| Class | Effort |
|---|---|
| `bulk-read`, `bulk-classify`, `creative` variants | low |
| `data-xform`, `test-gen`, simple `debug` | medium |
| `plan`, `refactor`, `security-review`, `reason`, ambiguous `debug` | high |
| critical security/correctness or irreversible decisions | xhigh in main session |
