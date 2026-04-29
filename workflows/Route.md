# Route Workflow

## Step 1: classify

Assign one primary task class from `references/Routing.md`.

## Step 2: hard gates

Route to main session only if any gate is true:

- MCP/tool-backed private context is required.
- Secrets or credentials must be inspected or used.
- Git state will be changed.
- The action is destructive or irreversible.
- The request is too small to justify orchestration.

## Step 3: split

Split only along independent boundaries:

- files/modules/components
- datasets/log shards
- test suites
- candidate designs
- independent review dimensions
- read-only evidence checks

Do not split tasks with overlapping write sets unless the main session stages integration explicitly.

## Step 4: choose roles

- `explorer`: read-only investigation, validation, research, evidence checks, codebase questions.
- `worker`: bounded implementation or transformation with explicit ownership.
- `validator`: bounded critique of a worker result; never redo the whole task.
- `external-worker`: non-MCP work delegated to a provider endpoint or subprocess.
- `local-worker`: local LLM work after local-first gates pass.

## Step 5: choose provider and selector

Use `references/CostPolicy.md`:

1. Prefer local if gates pass and capability fits.
2. Prefer the cheapest capable cloud selector for simple work.
3. Use balanced selectors for normal engineering, debugging, and synthesis.
4. Reserve frontier/high-reasoning selectors for high-risk architecture, correctness, security, and ambiguous failures.
5. Use the provider requested by the user unless a hard gate forbids it.

## Step 6: validation

Require validators for:

- external-provider worker output
- MiniMax worker output
- local model output used for final claims or edits
- Claude/Codex worker edits
- multi-file edits or refactors
- high-risk plans
- security-sensitive reviews
- factual research with citations
- any worker result that materially affects final output

## Step 7: integration

Main session integrates all results, resolves conflicts, runs available checks, and produces the final answer.
