# Subagent Policy

## Main-session only

Use no subagent when:

- MCP/private tools are needed.
- Secrets are present.
- Git state must change.
- The task is destructive or irreversible.
- The task is tiny.
- The main session must perform final synthesis or make a high-stakes decision.

Examples:

- "send an email" → main session only.
- "commit these changes" → main session only.
- "make a one-line copy edit" → main session only.

## Explorer

Use an explorer for read-only work:

- codebase questions
- evidence gathering
- module review
- design critique
- security review without edits
- validation of claims or diffs

Explorer constraints:

- no writes
- no MCP
- no secrets
- no git state changes
- bounded scope
- return evidence and uncertainties

## Worker

Use a worker for bounded implementation or transformation:

- write tests for a specified file or module
- transform CSV/JSON/log data
- implement a scoped patch
- classify independent files/items
- produce variants of copy or summaries

Worker constraints:

- explicit file/data ownership
- no overlapping write sets
- no MCP
- no secrets
- no git state changes
- no destructive actions
- output patch/summary/evidence only

## Validator

Use a validator after any worker result that can materially affect final output. Validators inspect a bounded scope using the checklist in `references/Validators.md`.

## External model worker

Use when the user requests a provider or when cost/capability policy chooses an API/CLI provider. External workers must receive the minimum necessary context and no private-tool/secrets material.

## Local model worker

Use when local-first gates pass. Best for summaries, classifications, extraction, log chunks, and bounded code review. Validate before relying on local output for final claims.
