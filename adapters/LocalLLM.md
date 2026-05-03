# Local LLM Adapter

Local LLM routing is preferred only after local-first gates pass.

## Best use

- Log and file summaries
- Classification/extraction
- Low-risk rewrites
- Bounded code reading
- Cheap first-pass reviews

## Avoid local routing for

- MCP/private-tool tasks
- secrets/credentials
- high-stakes final decisions
- tasks exceeding context window
- tasks requiring tools the local service does not provide
- tasks where local model capability is unknown or historically weak

## Interface behavior

```yaml
provider: local-llm
supportsParallel: true
supportsTools: usually false
supportsFileEditing: false unless wrapped by a trusted local edit harness
supportsReasoningEffort: selector-dependent
model_selectors:
  worker: local-worker
  coder: local-coder
  thinker: local-thinker
  chat: local-chat
```

## Detection contract

Local provider-primary routing requires live checks at task time. Static model
registry entries are hints only; they do not prove the currently loaded model is
safe for implementation or validation.

```yaml
service_reachable: true|false
health_ok: true|false
active_lila_mode: <mode|null>
loaded_models: []
selected_model_loaded: true|false
exact_model_id: <model id|null>
context_window: <tokens|null>
context_fits_with_headroom: true|false
capability_match: true|false
recent_task_class_eval_passed: true|false
latency_ok: true|false
fallback_reason: <reason|null>
```

## Capability tiers

- 120B reasoning models may validate complex work only with recent relevant eval evidence.
- 70B/80B-class models may validate bounded research or review when live checks pass.
- 30B/32B coder models may handle scoped coding/review, but not broad final validation by default.
- A3B, small, stale, or unknown models are limited to low-risk summarization/classification unless explicitly proven for the task class.

Snapshot the model identity, mode, context window, and capability evidence in the
route result. Downgrade or block Local routing on mode switch, health failure,
stale capability evidence, or repeated validator misses.

## Result handling

Local outputs should be normalized and validated before final use when they affect user-facing conclusions or edits.
