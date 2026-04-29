# Local-First Strategy

Local routing is preferred only when all gates pass.

## Local readiness gates

| Gate | Requirement | Failure explanation |
|---|---|---|
| Service reachable | Configured local endpoint responds | `local unavailable` |
| Model loaded | Target selector resolves to a loaded model | `model not loaded` |
| Context fits | Estimated input + output + 20% headroom fits model window | `context too large` |
| Capability fit | Model is appropriate for task class | `capability mismatch` |
| Latency acceptable | Expected runtime is acceptable for the task | `local too slow` |
| Privacy fit | Task has no MCP/secrets constraints that make local inappropriate | `private-tool or secret gate` |
| Estimator usable | Token/context estimator is available or conservative fallback is safe | `token estimator unavailable` |
| User preference | User did not explicitly request cloud/frontier | `user requested cloud/frontier` |

## Detection order

1. Read configured local provider endpoint and selector mapping.
2. Query available models from the local service.
3. Confirm selected model is loaded.
4. Estimate context with headroom.
5. Check task capability against model tags.
6. Estimate latency from recent telemetry or conservative defaults.
7. Route locally or emit a fallback reason.

## Fallback behavior

When local-first fails, do not silently use local anyway. Route to the cheapest capable cloud/provider selector and record the reason:

```yaml
local_preferred: true
local_selected: false
fallback_reason: context too large
fallback_selector: current-balanced
```

## Suggested local selector mapping

| Selector | Typical loaded-model capability |
|---|---|
| `local-worker` | short summaries, extraction, classification, log chunks |
| `local-chat` | prose drafts and low-risk rewrites |
| `local-coder` | bounded code reading, simple patches, test scaffolds |
| `local-thinker` | reasoning-heavy local model; use only after validation history proves capability |
