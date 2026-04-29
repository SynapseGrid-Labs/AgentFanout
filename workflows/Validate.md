# Validate Workflow

Validators run after a worker returns output. A validator must inspect a bounded target, not redo the whole task.

## Validator input package

Provide:

```yaml
original_request: <user request>
worker_summary: <worker claims or patch summary>
inspection_scope: <exact files, diffs, logs, evidence, or claims to inspect>
checklist:
  - missing requirements
  - contradictions
  - hallucinated facts
  - unsafe edits
  - secret exposure
  - MCP misuse
  - verification gaps
  - overconfident conclusions
expected_output:
  - pass/fail/concerns
  - material findings only
  - recommended orchestrator action
```

## Validator output format

```yaml
verdict: pass|concerns|fail
material_findings:
  - finding: <specific issue>
    evidence: <file/claim/check>
    impact: <why it changes integration>
recommended_action: accept|revise|re-run-worker|main-session-review
verification_gaps:
  - <gap>
```

## Validator limits

Validators must not:

- redo the full worker task
- perform unbounded exploration
- edit files unless explicitly assigned as a worker
- use MCP tools
- use secrets
- change git state
- produce final synthesis for the user
