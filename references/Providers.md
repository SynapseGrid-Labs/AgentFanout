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
selection_reason: <registry_lowest_eligible_profile|explicit_model_alias|controlled_escalation>
available_tools: []
supports_file_editing: true|false
validation_eligible: true|false
snapshot_source: <live_check|adapter_env|runtime>
fallback_reason: <reason|null>
```

If a requested provider cannot produce the required capability snapshot, route
output must report `fanout_blocked` instead of silently shifting the work back to
the main session.

Provider modes are strict:

- `AgentFanout MiniMax` is compatibility-disabled by default and blocks unless a
  host explicitly enables disabled providers.
- `AgentFanout Local` uses the local worker runtime only after a fresh host
  `LocalCapabilityAttestationV1` matches the exact launch.
- `AgentFanout Grok` uses only a configured Grok host adapter.
- `AgentFanout Auto` ranks eligible profiles by provisional registry-relative
  cost/latency metadata. This ordering is not calibrated model-quality proof or
  a measured-savings claim. MiniMax and retired Claude participate only through
  an explicit compatibility override.

Do not substitute Local for explicit MiniMax or MiniMax for explicit Local. If a
strict provider cannot complete a real worker task, the host reports
`fanout_blocked`.

The hosted-runner core includes a compatibility-gated Codex adapter plus
admitted Grok fixed-endpoint HTTPS and credential-free Local bounded-file
adapters. An outer entrypoint must still explicitly select and manifest-pin the
adapter it exposes. The Codex diagnostic inventories the installed CLI feature
table, disables all classified tool surfaces, rejects unknown active features,
captures one synthetic-provider outbound request, and binds the supported
interpreter/wrapper/package/native executable chain. That observation cannot
admit a real worker; Codex initial, persistent, and resumed execution therefore
remains unavailable. The Grok CLI is
discovery-only; inference uses the pinned isolated/no-site HTTPS helper, sends
no tool/function schema, rejects proxies and redirects, and requires a dedicated
owner-only, guard-bound `auth.json`. Local launches its data-only helper with
isolated/no-site Python.
These runner controls do not replace an OS sandbox against a malicious
same-user process.

Local no-tool execution requires a fresh
`LocalCapabilityAttestationV1`, exact bridge observation, exact endpoint/model/
effort binding, verified response token usage, and `LocalNoToolBoundaryV1`.
Tool-capable Local execution remains a separate contract requiring signed
launch attestation and sandbox proof. A capability snapshot or stub proves that
the declared launch binding is admissible; it does not establish calibrated
task quality.

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
selection_reason: <auditable routing reason>
reasoning_effort: low|medium|high|xhigh|ultra
output_contract: <schema or checklist>
```

## Normalized result object

```yaml
task_id: <id>
provider: <provider>
model_selector: <selector>
resolved_model: <host-acknowledged exact model>
selection_reason: <auditable routing reason>
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

Persistent runs normalize each completed provider response to `WorkerResultV1`
and ingest it into `RunStateV2`. The host attaches model, usage, and assigned
worker-handle attestations. Bounded `remaining_work` becomes a new `TaskUnitV1`;
the controller may recommend its stored host-issued handle, but only an adapter
that explicitly supports retained context may reuse it. The currently admitted
Grok and Local adapters do not; every follow-up starts a fresh call. Native
Codex workers are unavailable. A future retained adapter may reuse only an
unexpired private-ledger binding for the same project, bounded input, model,
effort, and executable. Cross-input residuals always rotate the handle.

For offload accounting, provider invocation is not enough. A successful offload
requires a completed bounded work unit, a success status, evidence or scoped
file changes, and structural validation. Partial reads, denied tool calls,
timeouts, and checklist-only responses are failures, not worker execution.

## Adapter files

- `adapters/Codex.md`
- `adapters/Grok.md`
- `adapters/Claude.md`
- `adapters/LocalLLM.md`
- `adapters/MiniMax.md`

Future adapters should implement the same interface and should not change the orchestration core.
