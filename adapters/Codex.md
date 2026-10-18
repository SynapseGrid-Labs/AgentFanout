# Codex Adapter

Codex is a provider-specific adapter, not the orchestration brain.

## Current compatibility status

The strict runner lane is unavailable. Its authenticated loopback diagnostic
captures an exact-model request through a synthetic custom provider, but
provider capability bounds mean that even `tools: []` there cannot prove the
real ChatGPT/OpenAI request is empty. Native admission therefore always returns
`codex_native_real_provider_attestation_required` unless an independently
trusted real-provider attestation mechanism is implemented in the future.
Codex CLI 0.144.1 additionally advertises built-ins and fails earlier with
`codex_native_no_tool_transport_unavailable:*`. Neither block permits parent
fallback.

The diagnostic observes only an ephemeral synthetic-provider request; it does
not attest or admit real-provider worker execution. Persistent and resumed
Codex workers are also blocked. Consequently no native Codex worker or validator
wave currently runs through the strict lane. This is separate from advisory use
of the skill inside an outer Codex harness.

## Intended use after admission

- Read-only analysis of host-supplied bounded file content
- Validation of patches, test coverage, and claims
- Repeated follow-up through independently attested fresh workers after every result batch

## Required future interface behavior

```yaml
provider: codex
supportsParallel: true
supportsTools: false
supportsFileEditing: false
supportsContextReuse: false
supportsReasoningEffort: true
model_selectors:
  cheap: current-fast-mini
  balanced: current-balanced
  frontier: current-frontier
  astra: astra-explicit
```

## Native Codex selector mapping

If a future admitted adapter receives `current-fast-mini`, it must pass an
explicit mini/fast model override rather than defaulting to the
main-session/frontier model. Resolve the selector through current verified host
metadata, use the routed `reasoning_effort`, and record the exact model; the
profile name is not a verified price claim.

Before any future Codex worker launch, validate that the native parameters
satisfy `runtime_launch.codex_native.cost_tier` and
`must_pass_model_override`. If the host cannot satisfy that contract, report
`fanout_blocked` or route to another already approved provider.

A future `current-balanced` route may use a verified Terra-tier model.
`current-frontier` is the Sol-tier route and must explicitly reject
`gpt-6-astra`. Astra uses only `astra-explicit`, requires the exact
`gpt-6-astra` model acknowledgement and an authorization reason, and is limited
to one worker per wave. Every Codex selector requires a concrete model override
and launch audit fields for `resolved_model` and `selection_reason`; allowing
the runtime default is not sufficient evidence.

## Future launch patterns

- Explorer/worker: host reads one approved file and supplies its bounded text;
  Codex receives no project mount and returns a structured result packet.
- Validator: receives the same bounded source packet plus the worker result in a
  new process; it never resumes worker context.

## Enforced hosted boundary

The compatibility diagnostic constructs the authority prefix a future admitted
`launch_codex` turn would require:

- strict configuration parsing, `read-only` sandbox, approval policy `never`,
  ignored user config/rules, and an empty launch directory;
- an explicit feature deny list covering `unified_exec`, `shell_tool`, patch,
  apps/MCP, browser/computer/web, plugins/hooks, goals, workspace discovery,
  image, and multi-agent surfaces;
- a preflight of the effective `codex features list` state. Every present,
  non-removed denied feature must be false; an unknown active feature blocks
  until it is classified;
- top-level `web_search="disabled"` plus lifecycle-aware flags that never pass
  removed or deprecated feature switches;
- the exact-model authenticated loopback diagnostic described above, which is
  insufficient for real-provider admission;
- fail-closed JSON event inspection. Any command, file-change, tool, MCP, web,
  browser, computer, image, or subagent item invalidates the result.

The official npm `codex.js` entrypoint is not trusted as a single file. The
identity includes its shebang interpreter, resolved Node runtime, Codex package
manifest, platform-package manifest, and referenced native executable. Dispatch
bypasses the wrapper and executes a verified native copy from a sealed Linux
memfd (or an unlinked descriptor on other POSIX systems). Persistent and resume
launches remain denied until a distinct resume payload passes the same class of
transport proof.

## Hard restrictions

Codex workers must not:

- use MCP/private tools
- access secrets
- change git state
- perform destructive actions
- overlap write sets with another worker
- produce final user-facing synthesis
- rely on prompt text as the no-tool boundary

## Prompt packet

```yaml
role: explorer|worker|validator
scope: <files/evidence>
allowed_writes: []
forbidden: [mcp, secrets, git-state-change, destructive-action, final-synthesis]
model_selector: <selector>
reasoning_effort: low|medium|high|xhigh|ultra
runtime_launch:
  codex_native:
    cost_tier: cheap
    required_model_capability: mini_or_fast
    must_pass_model_override: true
output_contract: <summary/evidence/patch/validator verdict plus host attestations>
```

Astra supports `low`, `medium`, `high`, `xhigh`, and `max`; it does not use the
Codex `ultra` effort alias. `AgentFanout HIGH` changes effort only and cannot
authorize or select Astra.

## Notes

A future independently attested hosted lane would be no-tool and read-only even
if Codex itself supports editing. No native Codex runtime is currently admitted.
A future tool-capable lane needs a separately sandbox-attested adapter. Sealed
descriptor execution prevents pathname replacement after verification; it does
not protect against a process that already controls the same operating-system
account. AgentFanout remains Linux/POSIX-first.
