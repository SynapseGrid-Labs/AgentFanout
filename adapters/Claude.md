# Claude Adapter

Claude is a retired compatibility adapter and is disabled in the default
registry. The orchestration core remains provider-agnostic.

## Best use

- Design critique
- Code review
- Read-only exploration
- Bounded implementation when tool permissions and file ownership are explicit
- Validator passes for plans, diffs, and evidence claims

## Interface behavior

```yaml
provider: claude
supportsParallel: true
supportsTools: runtime-dependent
supportsFileEditing: true
supportsReasoningEffort: provider-dependent
model_selectors:
  cheap: claude-fast
  balanced: claude-balanced
  frontier: claude-frontier
```

## Launch patterns

- Use Claude agents/subagents only with minimal scoped context.
- Configure allowed tools narrowly.
- Use separate validator instances when cross-provider validation is unavailable.
- Keep MCP/private-tool tasks in the main session unless the current main session itself is Claude and tool policy allows it.

## Hard restrictions

Claude workers must not:

- use private account tools delegated from the main session
- access secrets
- change git state
- perform destructive actions
- bypass the no-out-of-band-status policy
- own final synthesis

## Prompt packet

```yaml
role: explorer|worker|validator
scope: <bounded scope>
allowed_tools: <read/edit/bash subset as appropriate>
allowed_writes: <explicit list>
model_selector: claude-fast|claude-balanced|claude-frontier
output_contract: <schema/checklist>
```
