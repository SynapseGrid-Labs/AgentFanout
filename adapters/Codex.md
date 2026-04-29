# Codex Adapter

Codex is a provider-specific adapter, not the orchestration brain.

## Best use

- Bounded file edits with explicit write ownership
- Read-only codebase exploration
- Validation of patches, test coverage, and claims
- Main-session integration when Codex is the active runtime

## Interface behavior

```yaml
provider: codex
supportsParallel: true
supportsTools: runtime-dependent
supportsFileEditing: true
supportsReasoningEffort: true
model_selectors:
  cheap: current-fast-mini
  balanced: current-balanced
  frontier: current-frontier
```

## Launch patterns

- Explorer: read-only prompt, no writes, return evidence.
- Worker: explicit files, allowed edits, expected patch summary.
- Validator: inspect worker summary and exact files/diffs only.

## Hard restrictions

Codex workers must not:

- use MCP/private tools
- access secrets
- change git state
- perform destructive actions
- overlap write sets with another worker
- produce final user-facing synthesis

## Prompt packet

```yaml
role: explorer|worker|validator
scope: <files/evidence>
allowed_writes: []
forbidden: [mcp, secrets, git-state-change, destructive-action, final-synthesis]
model_selector: <selector>
reasoning_effort: low|medium|high|xhigh
output_contract: <summary/evidence/patch/validator verdict>
```

## Notes

Use provider-native non-interactive execution when available. Keep sandbox/approval settings explicit for automation. The adapter resolves selectors to current Codex-supported models; the core never hardcodes names.
