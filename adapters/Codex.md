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

## Native Codex selector mapping

When `scripts/route.py` returns `current-fast-mini`, the host must pass an explicit mini/fast model override to native Codex worker launch. Do not omit the model override and let the runtime default to the main-session/frontier model. Resolve the selector to the lowest-cost currently available Codex mini or fast model, use the routed `reasoning_effort`, and record the resolved model in the worker launch summary.

Before spawning a Codex worker, validate that the native launch parameters satisfy `runtime_launch.codex_native.cost_tier` and `must_pass_model_override`. If the host cannot satisfy a required cheap/mini override, report `fanout_blocked` or route to another provider instead of silently launching the default stronger model.

`current-balanced` may use the runtime's balanced default. `current-frontier` should use the strongest current Codex model only when high-effort routing requested it.

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
runtime_launch:
  codex_native:
    cost_tier: cheap
    required_model_capability: mini_or_fast
    must_pass_model_override: true
output_contract: <summary/evidence/patch/validator verdict>
```

## Notes

Use provider-native non-interactive execution when available. Keep sandbox/approval settings explicit for automation. The adapter resolves selectors to current Codex-supported models; the core exposes selector capabilities and does not publish model names as policy.
