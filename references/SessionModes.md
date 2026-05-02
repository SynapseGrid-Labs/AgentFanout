# Session Modes

## Glossary

- Main session: the active user-facing conversation that owns safety, private tools, git state, destructive actions, final integration, and final synthesis.
- Runtime host: Codex, Claude, or another process running the main session.
- Provider adapter: a bounded worker or validator implementation. It never owns global routing policy.
- Automatic guarded fanout: AgentFanout chooses the useful worker count from task decomposition and guardrails.

## State Schema

```json
{
  "provider_mode": "Auto|MiniMax|Local|Codex|Claude",
  "effort_mode": "Auto|High",
  "fanout_policy": "automatic_guarded",
  "state_scope": "conversation_only"
}
```

`route.py` is stateless. Callers pass the previous `next_state` as `--state-json` on the next route call. If state is unavailable after compaction, use defaults and warn `state_unavailable_defaulted`. No state is written to disk, env, cache, registry, or skill files.

## Canonical Commands

Commands must be an entire trimmed line beginning with `AgentFanout`. Valid commands: `Status`, `Help`, `MiniMax`, `MiniMax Mode`, `Local`, `Local Mode`, `Codex`, `Codex Mode`, `Claude`, `Claude Mode`, `HIGH`, `Auto`, `Clear`, `Reset`.

Only `* Mode`, `HIGH`, `Auto`, `Clear`, and `Reset` mutate `next_state`. Legacy OrchestratorMode and CodexAgent phrases are discovery compatibility or one-turn hints only. `Status` and `Help` do not mutate state. Provider commands without `Mode` are current-task provider-primary directives and do not persist. Provider `Mode` commands mutate only `provider_mode`. `HIGH` mutates only `effort_mode`. `Auto`, `Clear`, and `Reset` restore defaults.

## Provider/Effort Matrix

- Auto: choose cheapest capable safe route; prefer local only when readiness passes.
- MiniMax: use `minimax-latest` for eligible workers; use `minimax-fast` only after the runtime verifies highspeed support for the active token plan. In provider-primary mode, validate with structural checks and a separate MiniMax adversarial validator before main-session compact review.
- Local: use `local-worker`, `local-coder`, `local-thinker`, or `local-chat` only after live readiness and task-class capability gates pass.
- Codex: use native Codex subagents only when the runtime host exposes them; otherwise fall back to main-session routing. For `current-fast-mini`, pass the explicit mini/fast model override from `runtime_launch.codex_native` instead of allowing the host default model.
- Claude: use `claude-fast`, `claude-balanced`, or `claude-frontier` only when a safe adapter is configured.
- High effort: quality-first non-premium frontier/latest selector with high reasoning; use extra-high where supported for security, release, architecture, migration, high-stakes, or ambiguous synthesis tasks.

## Readiness Checks

All provider readiness failures produce warnings and safe fallback, not hard crashes.

```json
{
  "adapter_file_exists": true,
  "executable_or_endpoint_configured": true,
  "probe_timeout_ms": 3000,
  "selector_mapped": true,
  "service_reachable": true,
  "context_fits_with_headroom": true,
  "capability_match": true,
  "latency_ok": true,
  "fallback_warning": null
}
```

## Worker Packet

```json
{
  "worker_id": "string",
  "task_scope": "string",
  "context_packet": "string",
  "allowed_paths": ["string"],
  "allowed_actions": ["read", "edit", "test", "analyze"],
  "forbidden_actions": ["mcp_private_tools", "secrets", "git_state_changes", "destructive_actions", "callbacks_voice_notifications", "final_synthesis"],
  "output_contract": {"format": "json|markdown", "required_fields": ["string"]},
  "validator_required": true,
  "return_only_to_main_session": true
}
```

## Validator Packet

```json
{
  "validator_id": "string",
  "task_scope": "string",
  "evidence_refs": ["string"],
  "checklist": ["string"],
  "pass_threshold": "all_critical_items_pass",
  "forbidden_actions": ["mcp_private_tools", "secrets", "git_state_changes", "destructive_actions", "callbacks_voice_notifications", "final_synthesis"],
  "output_contract": {"verdict": "pass|concerns|fail", "required_fields": ["checked_items", "evidence_refs", "required_fixes", "escalation_required"]},
  "escalation_target": "main_session"
}
```

Validators must be independent from worker instances when possible. Escalate to the main session on evidence gaps, forbidden text leakage, safety gate conflicts, or material worker/validator disagreement.

Provider-primary validators should be same-provider adversarial validators first.
Cross-provider validators are escalation tools, not the default route.

## Circuit Breaker

Circuit breaker scope is per route invocation and per fanout wave. Three worker failures in one wave stops new launches for that task and reports `triggered`, `failure_count`, `wave_id`, and `reason`.

## Status And Help Examples

Status reports provider mode, effort mode, `fanout_policy: automatic_guarded`, state scope, kernel status when known, full skill load status, hard gate policy, and warnings. Help returns the canonical command list and states that fanout is automatic.

## Compaction Behavior

If conversation compaction drops state, reset to defaults and warn `state_unavailable_defaulted`. Do not infer sticky mode from legacy trigger phrases after compaction.
