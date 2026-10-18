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
supportsTools: true only through VLLMAgent when AGENT_FANOUT_LOCAL_TOOL_AGENT=1 and host attestation passes
supportsFileEditing: true only within scoped ToolEnvelopeV1 write_files through VLLMAgent
supportsReasoningEffort: selector-dependent
model_selectors:
  worker: local-worker
  coder: local-coder
  thinker: local-thinker
  chat: local-chat
```

## Configured local runtime

AgentFanout has no public default host, endpoint, model, mode, or runtime path.
The host supplies them through `AGENT_FANOUT_LOCAL_*` configuration. Local mode
requires endpoint reachability plus `AGENT_FANOUT_LOCAL_ATTESTATION_JSON`
containing a fresh `LocalCapabilityAttestationV1`. Missing, worker-authored,
unknown, stale, expired, or mismatched evidence returns `fanout_blocked`.

Local has two intentionally separate execution lanes:

1. `AGENT_FANOUT_LOCAL_HOSTED_NO_TOOL=1` selects the bundled bounded-file
   data-client adapter. It accepts one host-bound UTF-8 `file:` input, runs in
   an empty ephemeral directory, sends no credentials, exposes no tools, and
   never mounts the project root in the helper process.
2. `AGENT_FANOUT_LOCAL_TOOL_AGENT=1` selects the tool-capable VLLMAgent bridge.
   That lane still requires the separately signed launch and sandbox boundary;
   it cannot downgrade into the no-tool lane after a tool request.

For the hosted no-tool lane, the endpoint must be HTTPS or an explicitly
enabled numeric loopback HTTP address
(`AGENT_FANOUT_LOCAL_ALLOW_LOOPBACK_HTTP=1`). `localhost`, URL credentials,
redirects, query/fragment authority, model aliases in place of exact model IDs,
missing token usage, tool-call responses, and unverified response-model changes
all fail closed. The endpoint must expose OpenAI-compatible `/models` and
`/chat/completions` responses. AgentFanout sends no `Authorization` header and
rejects configured Local API-key/token/password variables for this lane.

The helper still transmits the assigned file text to the configured endpoint.
HTTPS is transport protection, not a confidentiality classification: do not
route sensitive content to a remote endpoint merely because it uses TLS.

The portable atomic Local lane above is no-tool. The following broker command
is an optional PAI-specific lower-level integration and is not evidence of an
AgentFanout hosted launch by itself. A trusted custom host must own project
binding, launch receipts, sandbox policy, usage, cancellation, validation, and
terminal authorization before using it. For tool-capable worker-agent use in
that integration, Local launches:

```bash
bun "$HOME/.pai/skills/VLLMWorker/Tools/VLLMAgent.ts" --request-json - --json
```

The request must include `LocalAgentRequestV1` with `model_alias`, `endpoint`,
and a validated `ToolEnvelopeV1`. The broker exposes scoped project reads,
public fetches, read-only shell profiles, verification commands,
and assigned-file patches. It must block secrets, private account side effects,
git state mutation, destructive commands, deploys, broad shell, unscoped writes,
and final user synthesis.

## VLLMWorker host bridge

AgentFanout does not construct a trusted Local request from worker-provided
data. After the host measures the configured endpoint, it calls the documented
VLLMWorker host bridge through `scripts/local-bridge-issue.mjs`. The issuer
uses only `issueHostCapabilityAttestation` and
`createHostUsageCorrelation` from
`VLLMWorker/Tools/broker/host_bridge.ts`.

The bridge-issued request contains the exact evaluated envelope and capability
attestation. Its opaque usage correlation stays host-side; it is never placed
in a delegated prompt or `LocalAgentRequestV1`. A missing observation, bridge
module, issuer failure, or contract/profile mismatch blocks Local fan-out
without falling back to main-session implementation.

The tool envelope also requires an explicit approved project root. Configured
additional roots must remain inside it; broad and sensitive roots fail closed.

## Host capability attestation

Local provider-primary routing requires host-issued evidence at task time.
Static registry entries and independent environment booleans are hints only;
they do not prove the currently loaded model is safe for the requested task.

```yaml
schema: LocalCapabilityAttestationV1
attestation_id: localcap_<host-issued-id>
source: host
issuer: host-runtime
selector_alias: local-worker|local-coder|local-thinker
exact_model_id: <exact loaded model, never "unknown">
observed_at: <fresh RFC3339 timestamp>
expires_at: <future RFC3339 timestamp>
active_mode: <host-verified mode>
context_window: <positive tokens>
evaluated_task_classes: []
supported_reasoning_efforts: [low|medium|high|xhigh|ultra]
max_risk: low|medium|high|critical
tools: []
supports_file_editing: false  # required by the hosted no-tool lane
```

`selector_alias` is a routing alias, not model identity, and must match the
selected registry profile. `exact_model_id` must separately match the host's
loaded-model observation. Task class, reasoning effort, risk, tools, mode,
context, freshness, and editing capability all fail closed on mismatch.

Snapshot the attestation in the route result. Downgrade or block Local routing
on selector/model mismatch, mode switch, health failure, stale evidence, missing
tool capability, or repeated validator misses.

Do not use Local as a fallback inside explicit `AgentFanout MiniMax` mode. Local
fallback belongs only to `AgentFanout Auto` or explicit `AgentFanout Local`.

## Result handling

The hosted adapter requires exact response-model acknowledgement and
non-negative, internally consistent prompt/completion token counts before a
result can be ingested. Reported completion usage above the requested output
limit is denied. These counts are host-observed endpoint evidence, not an
independent tokenizer or cryptographic meter.
USD cost remains unverified unless a separate trusted price source exists, so
the runner charges the reservation upper bound and does not claim measured
savings. Worker and validator calls are separate supervised process groups.
Timeout, cancellation, model mismatch, malformed output, stale evidence, or
missing usage blocks the task without parent fallback.

## Docker helper status

The bundled Docker command and receipt helpers are experimental and
non-enforced. They reject broad or mini-language-injectable mounts and produce a
canonical policy observation, but they do not independently collect Docker
inspection state or confirm daemon-side cancellation. They cannot satisfy the
`host_enforced` sandbox receipt required for **tool-capable** Local execution; a
deployment must provide a separately trusted Local sandbox adapter and signer.
The bounded-file no-tool data-client lane does not execute model-requested tools
and therefore uses `LocalNoToolBoundaryV1`, not a fabricated sandbox receipt.
