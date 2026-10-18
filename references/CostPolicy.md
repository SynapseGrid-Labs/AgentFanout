# Cost Policy

Use model profiles and selectors from `providers/models.json`, not hardcoded live
model claims. Provider adapters resolve selectors and workers acknowledge the
actual model used. Requested SOL, Terra, and Luna labels are configurable aliases.

## Selectors

| Selector | Intended use |
|---|---|
| `current-frontier` | hardest synthesis, security, correctness, ambiguous architecture |
| `astra-explicit` | exceptional work explicitly assigned to GPT-6 Astra or a host-authorized post-Sol escalation |
| `current-balanced` | default robust cloud model for normal engineering and integration |
| `current-fast-mini` | cheap simple classification, extraction, summaries, low-risk rewrites |
| `local-coder` | local code understanding or bounded code generation |
| `local-thinker` | local higher-reasoning model if available and proven capable |
| `local-worker` | local cheap worker for summaries, classifications, extraction |
| `local-chat` | local low-risk prose or conversational transformation |
| `claude-frontier` | Claude high-capability reasoning/code review |
| `claude-balanced` | Claude normal engineering/review worker |
| `claude-fast` | Claude cheap/fast summary/classification worker |
| `minimax-latest` | Retired MiniMax compatibility selector; no admitted adapter |
| `minimax-fast` | Retired highspeed compatibility selector; no admitted adapter |

## Selection policy

1. **Hard gates first**: MCP, secrets, git state, destructive actions, and final synthesis stay in the main session.
2. **Local first when safe**: use local selectors only when reachability, loaded model, context, capability, and latency gates pass.
3. **Lowest registry-relative eligible path**: for simple independent tasks,
   compare only adapters with current capability evidence and supported input
   modes. Registry ordering is provisional; it is not measured quality or a
   realized-savings claim.
4. **Balanced path**: for ordinary debugging, test generation, and data transformations, select only currently admitted capabilities for the host. Selector metadata for `current-balanced`, `local-coder`, `claude-balanced`, or `minimax-latest` is not adapter availability; retired/disabled providers cannot be selected.
5. **Frontier path**: reserve `current-frontier`, `claude-frontier`, or high-effort current runtime for high-risk design, correctness, or security.
6. **Validator pairing**: in provider-primary mode, prefer deterministic structural validation plus same-provider adversarial validation before spending main-session or cross-provider tokens. Use cross-provider validation only on escalation triggers.
7. **Reasoning effort is separate**: choose model selector and reasoning effort independently. A balanced model with high effort may have a lower registry-relative estimate than a frontier model for bounded reasoning; this is not a verified price claim.
8. **Measure the run**: apply `references/CostAccounting.md`; account for worker
   and parent-result tokens, stop later waves on threshold violations, and
   require runtime usage before claiming savings. Do not call these provider-side
   hard caps without native pre-call enforcement.
9. **Astra is explicit-only**: exclude the Astra profile from automatic model
   ranking. Permit it only when the task explicitly requests Astra or when the
   host sets `AGENT_FANOUT_ALLOW_ASTRA_ESCALATION=1`, reports exactly two prior
   worker failures, and binds `AGENT_FANOUT_ASTRA_ESCALATION_REASON` to
   `sol_validation_failed_twice`.
10. **Astra is serial and audited**: cap each Astra wave at one worker, require
    exact `gpt-6-astra` acknowledgement, and record `selection_reason` with the
    resolved model. A high or maximum reasoning request alone does not authorize
    Astra.

MiniMax note: both selectors are retained compatibility metadata. This repository
does not admit a MiniMax adapter. Historical highspeed model mappings do not
establish availability or subscription support; do not choose either selector
for an actual launch. Claude selectors likewise require an admitted host adapter
and do not enable the retired standalone runtime lane.

Codex note: `current-fast-mini` is an advisory routing contract while native
Codex worker admission remains blocked. Any future admitted adapter must resolve
and attest an explicit mini/fast model; it must not default a cheap explorer
route to the main-session/frontier model. If that contract cannot be satisfied,
block or replan among already approved workers rather than silently overspending.
`current-balanced` targets Terra, and `current-frontier` targets Sol while
forbidding `gpt-6-astra`; all three require explicit model overrides once a
Codex worker lane is admitted.

## Serial vs parallel

Use parallelism when subtasks are independent and outputs can be merged. Use serial delegation when one worker’s result defines the next scope. Use main-session-only for tiny, gated, or integration-heavy tasks.

## Launch-overhead anti-waste gate

Hosted worker and validator launches have non-zero context overhead. Compare
the predicted first wave against the direct main-session baseline before
launching, including when a provider was selected explicitly. Provider choice
answers *which eligible worker* to use; it is not economic consent. When the
candidate is more expensive, route to the main session unless the operator uses
an explicit overhead-acceptance control, force-dispatch mode, or requires
separate receipt-bound validation. Low/medium classification, extraction,
transformation, and creative packets use deterministic contract/evidence checks
without a second model call; higher-risk or explicitly independent work retains
semantic validators.

## Provider-native subagent vs subprocess worker

Prefer provider-native subagents when the runtime supports scoped tools, clean permissions, and result capture. Use subprocess/API workers for providers that expose only API/CLI access. In both cases, the main session owns prompt sanitation, scope, compact final review, and integration. Deep main-session validation is reserved for escalation triggers.
