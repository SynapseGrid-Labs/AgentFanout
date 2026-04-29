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

A local adapter should check:

```yaml
service_reachable: true|false
loaded_models: []
selected_model_loaded: true|false
context_window: <tokens|null>
context_fits_with_headroom: true|false
capability_match: true|false
latency_ok: true|false
fallback_reason: <reason|null>
```

## Result handling

Local outputs should be normalized and validated before final use when they affect user-facing conclusions or edits.
