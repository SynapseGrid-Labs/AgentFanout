# Cost Policy

Use model selectors, not hardcoded model names. Provider adapters resolve selectors to current model names.

## Selectors

| Selector | Intended use |
|---|---|
| `current-frontier` | hardest synthesis, security, correctness, ambiguous architecture |
| `current-balanced` | default robust cloud model for normal engineering and integration |
| `current-fast-mini` | cheap simple classification, extraction, summaries, low-risk rewrites |
| `local-coder` | local code understanding or bounded code generation |
| `local-thinker` | local higher-reasoning model if available and proven capable |
| `local-worker` | local cheap worker for summaries, classifications, extraction |
| `local-chat` | local low-risk prose or conversational transformation |
| `claude-frontier` | Claude high-capability reasoning/code review |
| `claude-balanced` | Claude normal engineering/review worker |
| `claude-fast` | Claude cheap/fast summary/classification worker |
| `minimax-latest` | MiniMax default supported worker selector |
| `minimax-fast` | Optional only when the runtime has verified highspeed support for the active token plan |

## Selection policy

1. **Hard gates first**: MCP, secrets, git state, destructive actions, and final synthesis stay in the main session.
2. **Local first when safe**: use local selectors only when reachability, loaded model, context, capability, and latency gates pass.
3. **Cheapest capable path**: for simple independent tasks, use `local-worker`, then `current-fast-mini`, then low-cost external worker.
4. **Balanced path**: for ordinary debugging, test generation, and data transformations, use `current-balanced`, `local-coder`, `claude-balanced`, or `minimax-latest` depending on preference and gates.
5. **Frontier path**: reserve `current-frontier`, `claude-frontier`, or high-effort current runtime for high-risk design, correctness, or security.
6. **Validator pairing**: in provider-primary mode, prefer deterministic structural validation plus same-provider adversarial validation before spending main-session or cross-provider tokens. Use cross-provider validation only on escalation triggers.
7. **Reasoning effort is separate**: choose model selector and reasoning effort independently. A balanced model with high effort may be cheaper than a frontier model for bounded reasoning.

MiniMax note: do not choose `minimax-fast` by default. The local MiniMax wrapper maps fast mode to `MiniMax-M2.7-highspeed`, which may be unsupported by the current token plan. Use `minimax-latest` unless highspeed support has been explicitly verified.

Codex note: `current-fast-mini` requires an explicit native Codex mini/fast model override resolved by the host adapter. Do not launch cheap Codex explorer workers with the host's default main-session/frontier model. If the host cannot satisfy `runtime_launch.codex_native.cost_tier=cheap` and `required_model_capability=mini_or_fast`, block or reroute rather than silently overspending.

## Serial vs parallel

Use parallelism when subtasks are independent and outputs can be merged. Use serial delegation when one worker’s result defines the next scope. Use main-session-only for tiny, gated, or integration-heavy tasks.

## Provider-native subagent vs subprocess worker

Prefer provider-native subagents when the runtime supports scoped tools, clean permissions, and result capture. Use subprocess/API workers for providers that expose only API/CLI access. In both cases, the main session owns prompt sanitation, scope, compact final review, and integration. Deep main-session validation is reserved for escalation triggers.
