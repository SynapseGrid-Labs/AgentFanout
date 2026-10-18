# MiniMax Adapter

MiniMax is a compatibility adapter and is disabled in the default registry. A
host may explicitly enable disabled providers for a controlled compatibility
run; the main orchestrator remains in charge.

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
supportsTools: true via MiniMaxAgent broker
supportsFileEditing: true within scoped ToolEnvelopeV1 write_files only
supportsReasoningEffort: provider-dependent
model_selectors:
  default: minimax-latest
  balanced: minimax-latest
```

## Runtime launch

Tool-requiring MiniMax work uses the brokered tool agent, not the text-only
review wrapper:

```bash
bun "$PAI_DIR/skills/MiniMax/Tools/MiniMaxAgent.ts" --request-json - --json
```

Set `PAI_DIR` to the installation root when using this optional integration;
AgentFanout core does not assume a particular home-directory layout.

AgentFanout must include `runtime_launch.minimax_tool_agent` and a
`ToolEnvelopeV1` payload in route output. The feature is enabled only when
`AGENT_FANOUT_MINIMAX_TOOL_AGENT=1`; otherwise tool-requiring MiniMax work must
return `fanout_blocked:minimax_tool_agent_disabled` instead of silently falling
back to main-session execution.

Tool envelopes require an explicit approved project root. They reject broad,
sensitive, home, runtime-state, private-knowledge, and out-of-project roots.
Read-only command profiles do not expose general interpreters or mutation flags.

`MiniMaxExec.ts` remains the stable text-only wrapper for pure reviews and
second opinions.

Do not use `minimax-fast` or pass `MiniMaxExec --fast` unless the runtime has explicitly verified that the current MiniMax token plan supports `MiniMax-M2.7-highspeed`. In this environment the token plan does not support highspeed, so AgentFanout routes MiniMax workers to the default supported model selector.

MiniMax provider mode is strict. Do not substitute Local, Codex, or the main
session when the user selected `AgentFanout MiniMax`. A MiniMax worker run only
counts as successful offload when it completes a bounded work unit and returns a
successful compact result packet with evidence or scoped file changes. Provider
invocation, partial file reads, denied tool calls, timeouts, and checklist-only
responses are failures for offload accounting. After the provider repair budget
is exhausted, report `fanout_blocked` instead of continuing locally.

## Routing rule

When MiniMax workers are used, validators are required unless the output is purely disposable ideation.

Provider-primary validator order:

1. Deterministic structural validation
2. Independent MiniMax same-provider adversarial validator with stricter checklist
3. One MiniMax repair loop when fixes are bounded
4. Main-session compact review of `CompactResultPacketV1`
5. Cross-provider validation only on escalation triggers

## Hard restrictions

MiniMax workers must not receive:

- raw MCP/private-tool tasks
- secrets
- git-state mutation tasks
- destructive tasks
- final synthesis responsibility

MiniMax may receive brokered read-only knowledge-base/repository/search tools or scoped
patch/test tools only through a declared `ToolEnvelopeV1`.

## Prompt packet

```yaml
role: external-worker
scope: <bounded data/files/claim>
model_selector: minimax-latest
reasoning_effort: low|medium|high
output_contract: <summary/evidence/patch instructions>
provider_primary:
  orchestration_style: provider_primary
  max_provider_repair_loops: 2
  codex_review_scope: CompactResultPacketV1_only_unless_escalated
validator_required: true
structural_validation_required: true
runtime_launch:
  minimax_tool_agent:
    command: bun $PAI_DIR/skills/MiniMax/Tools/MiniMaxAgent.ts --request-json - --json
    stdin_request: true
    required_flag: AGENT_FANOUT_MINIMAX_TOOL_AGENT=1
tool_envelope: ToolEnvelopeV1
compact_result_contract: CompactResultPacketV1
```
