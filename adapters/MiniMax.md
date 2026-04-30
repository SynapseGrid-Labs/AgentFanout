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
  default: minimax-latest
  balanced: minimax-latest
```

Do not use `minimax-fast` or pass `MiniMaxExec --fast` unless the runtime has explicitly verified that the current MiniMax token plan supports `MiniMax-M2.7-highspeed`. In this environment the token plan does not support highspeed, so AgentFanout routes MiniMax workers to the default supported model selector.

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
model_selector: minimax-latest
reasoning_effort: low|medium|high
output_contract: <summary/evidence/patch instructions>
validator_required: true
```
