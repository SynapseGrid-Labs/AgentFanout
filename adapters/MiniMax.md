# MiniMax Adapter

MiniMax is treated as an external worker provider. The main orchestrator remains in charge.

## Best use

- Non-MCP coding subtasks
- Summaries and log analysis
- Bulk classification/extraction
- Bounded implementation when no secrets/private tools are needed
- Cost-sensitive worker execution with stronger validators

## Interface behavior

```yaml
provider: minimax
supportsParallel: true
supportsTools: API-dependent
supportsFileEditing: false unless wrapped by an edit harness
supportsReasoningEffort: provider-dependent
model_selectors:
  cheap: minimax-fast
  balanced: minimax-latest
```

## Routing rule

When MiniMax workers are used, validators are required unless the output is purely disposable ideation.

Preferred validator order:

1. Codex/current-balanced validator
2. Claude/current-balanced validator
3. Independent MiniMax validator with stricter checklist
4. Main-session manual validation

## Hard restrictions

MiniMax workers must not receive:

- MCP/private-tool tasks
- secrets
- git-state mutation tasks
- destructive tasks
- final synthesis responsibility

## Prompt packet

```yaml
role: external-worker
scope: <bounded data/files/claim>
model_selector: minimax-fast|minimax-latest
reasoning_effort: low|medium|high
output_contract: <summary/evidence/patch instructions>
validator_required: true
```
