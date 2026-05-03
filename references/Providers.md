# Provider Adapter Interface

Each provider adapter should expose the same conceptual interface. Concrete implementations may be CLI, SDK, API, or provider-native subagent files.

Provider adapters are contracts/templates unless a host runtime implements them. Methods such as `launchWorker()` and `launchValidator()` are interface concepts; they are not implemented by `scripts/route.py`.

```ts
interface ProviderAdapter {
  canHandle(task): boolean
  estimateCost(task): CostEstimate
  estimateLatency(task): LatencyEstimate
  contextWindow(selector): number | null
  supportsParallel: boolean
  supportsTools: boolean
  supportsFileEditing: boolean
  supportsReasoningEffort: boolean
  launchWorker(task): WorkerResult
  launchValidator(task): ValidatorResult
  sanitizePrompt(prompt): string
  formatResult(result): NormalizedResult
}
```

Provider-primary routing requires a capability snapshot before launch:

```yaml
provider: <provider>
model_selector: <selector>
available_tools: []
supports_file_editing: true|false
validation_eligible: true|false
snapshot_source: <live_check|adapter_env|runtime>
fallback_reason: <reason|null>
```

If a requested provider cannot produce the required capability snapshot, route
output must report `fanout_blocked` instead of silently shifting the work back to
the main session.

## Normalized task object

```yaml
id: <stable id>
class: <task class>
role: explorer|worker|validator|external-worker|local-worker
scope: <files/data/claims>
write_set: []
requires_mcp: false
requires_secrets: false
requires_git_state_change: false
risk: low|medium|high|critical
model_selector: <selector>
reasoning_effort: low|medium|high|xhigh
output_contract: <schema or checklist>
```

## Normalized result object

```yaml
task_id: <id>
provider: <provider>
model_selector: <selector>
status: success|partial|failed
summary: <bounded summary>
evidence: []
files_touched: []
claims: []
verification: []
errors: []
raw_output_ref: <optional local reference>
```

Provider-primary results must additionally provide `CompactResultPacketV1` so
the orchestrator can review compact evidence instead of reopening the full
worker context.

## Adapter files

- `adapters/Codex.md`
- `adapters/Claude.md`
- `adapters/LocalLLM.md`
- `adapters/MiniMax.md`

Future adapters should implement the same interface and should not change the orchestration core.
