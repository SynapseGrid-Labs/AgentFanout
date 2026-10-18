# Changelog

## Prepared development candidate — not a release

- Lead with dependency-free native skill installation and task-scoped usage.
- Document the optional plugin/runtime boundaries and disabled GrokCLI,
  coordinator Local and mixed contracts; keep historical provider evidence
  distinct from this candidate's offline tests.
- Replace outdated provider/performance JPEGs with an original synthetic flow
  diagram and remove external image badges.
- Clarify that post-batch hook checks apply only to intercepted events. A
  completion marker is an orchestrator assertion, not independent proof.
- Add explicit exact-content approval and separate upload authorization gates.
- Replace deployment-specific cue names with existing generic routing cues;
  screen localhost notification ports generally and use synthetic examples.
- Pin CI checkout and Python setup actions to immutable upstream commits.
- Align the optional LangGraph fixture with current readiness/admission records,
  without fabricated quality samples or relaxed host validation.

Human UAT and publication remain pending. Version metadata stays `0.4.0-rc.1`.

## 0.4.0-rc.1 - 2026-07-14

- Add a Codex plugin with task-scoped lifecycle hooks that require a distinct
  residual-work decision after intercepted completed native worker batches.
- Add one-time decision tokens so a new wave invalidates prior completion
  authority and supported parent tools remain blocked between batches.
- Keep explicit activation lazy, preserve zero prompt/transcript persistence,
  and retain the compact standalone skill as an advisory cross-host surface.
- State the Codex hook boundary honestly: continuation and supported-tool
  guards apply to supported intercepted events; universal anti-bypass is not claimed.
- Harden activation to exact command-position syntax, exact orchestration-tool
  allowlisting, active-task replacement denial, cancellation reconciliation,
  same-worker follow-up callbacks, workspace binding, private state storage,
  and terminal-row cleanup.

All notable changes to AgentFanout will be documented in this file.

## Unreleased

### Added

- Public-candidate `0.2.0-rc1` version metadata, declared and hash-locked
  cryptography dependency set, isolated user installer, managed launcher, safe deactivation,
  clean-room install test, and machine-readable readiness report.
- Structured remediation for missing, broad, sensitive, and non-repository
  project roots so outer harnesses can retry with one safe exact binding instead
  of misclassifying the guard as provider failure.
- Public UAT checklist and community code of conduct.
- Actionable public-install failure when the selected Python interpreter lacks
  its host-provided `venv`/`ensurepip` component.
- Atomic `doctor`, `run`, `resume`, `abandon`, and `status` CLI with explicit Git-root binding,
  secure XDG state defaults, prelaunch input compatibility checks, and compact
  end-to-end outcomes.
- `ProjectBindingV1`, `HostCapabilityManifestV1`, and `SessionGuardStateV1`
  contracts plus cross-harness conformance checks.
- Clean-room fake-provider acceptance tests that run the complete hosted path
  from an unrelated worktree.
- A compatibility-gated Codex bounded-file contract that blocks before launch
  when the installed Codex runtime cannot prove a strict no-tool transport.
- Admitted Grok fixed-endpoint HTTPS and credential-free OpenAI-compatible
  Local adapters with exact provider-boundary binding, process-group
  cancellation, verified usage evidence, and no parent fallback.
- Portable Codex skill metadata under `agents/openai.yaml`.

- Persistent `RunStateV2` multi-wave lifecycle with result ingestion, bounded
  next waves, residual-task replanning, and worker-handle reuse recommendations.
- Compact built-in orchestrator, explorer, implementer, validator, weak Local,
  and synthesis roles.
- JSON schemas for tasks, results, run state, and next-wave plans.
- Data-driven active Grok/Local, explicit-diagnostic Codex, and
  compatibility-disabled MiniMax/Claude model profiles.
- Host-issued Local capability, model, usage, and worker-handle attestation
  contracts.
- Token, context, worker, wave, retry, cost, and no-progress budgets with routing
  decision telemetry.
- Host-enforced launch, measured-usage, validation, effect, Local attestation,
  Docker policy, and terminal-authorization contracts.
- Exact host-side worker-call, wave, retry, token, cost, and wall-clock controls;
  guarded interrupted runs resume only with the original provider boundary.
- Client-side redacted JSONL/Langfuse telemetry and scoped retention tooling.

### Security

- The Codex diagnostic now captures the exact outbound Responses request through a
  high-entropy bearer-authenticated loopback-only transport. The observed model
  must match, `tools` must be empty, and tool choice must be disabled. Codex CLI
  0.144.1 fails closed; no run state, guard, ledger, or worker is created.
- Codex synthetic transport diagnostics are bound to executable, exact model,
  feature inventory, and ephemeral session mode. They do not admit a real
  worker. Persistent/resumed execution and all native Codex worker/validator
  waves remain disabled until separately trusted real-provider proof exists.
- Auto removes a compatibility-rejected Codex runtime and replans only among
  configured Grok/Local workers, preserving the rejection in output and never
  falling back to the parent.
- Grok discovery now executes from descriptor-pinned code with forced
  disposable `HOME` and `GROK_HOME`, preventing ambient configuration reads.
- Grok inference no longer uses the CLI agent harness after larger prompts were
  observed invoking hidden read/search/tool surfaces despite attempted tool
  denial. A descriptor-pinned isolated helper now calls only the fixed xAI
  chat-completions endpoint, sends no tool/function fields, disables ambient
  proxies and redirects, and validates exact model, finish reason, usage, and
  absence of tool/function/source output.

- Generic or symbolic inputs are denied before RunState/ledger creation by the
  atomic CLI; a wave-wide adapter preflight runs before provider receipts.
- Enforcement claims are split into advisory, runner-enforced, and trusted
  harness-enforced levels. Detect-only hooks cannot claim anti-bypass.
- Sensitive roots are host-configurable exact paths, avoiding false positives
  for ordinary repository/source names while retaining credential-path gates.
- Session guards are armed before provider launch and bind the run, project,
  state/ledger identities, manifest, provider, exact model, executable, worker
  limit, and Grok/Local boundary evidence. Ambiguous effects cannot be replayed.
- Private state paths reject group/other-writable ancestors. The implicit XDG
  default falls back to an owner-only home directory when `~/.local` is unsafe;
  explicitly configured unsafe XDG paths still fail closed.
- Grok inference runs from an empty workspace under isolated/no-site Python
  with an auth-only ephemeral home and no CLI filesystem, terminal, MCP, web,
  memory, skill, or subagent surface. Its dedicated owner-only `auth.json`
  identity is guard-bound; ambient auth sockets, proxies, redirects, and
  credential-home drift fail closed.
- The Local no-tool lane rejects credentials, redirects, proxy inheritance,
  endpoint/model drift, missing usage, tool calls, and stale host evidence. Its
  helper runs under isolated/no-site Python (`-I -S`).
- Codex synthetic diagnostics use one approval-never, read-only, feature-denial
  configuration. Every known
  native tool surface, including `unified_exec`, is preflighted off, but the
  synthetic provider cannot prove parity with the real provider. Native Codex
  therefore remains blocked pending separately trusted real-provider proof;
  persistent/resume launches remain blocked as well.
- Provider identity now covers interpreter/wrapper/package/native launcher
  dependencies, and supported executable targets are descriptor-pinned across
  verification and launch to close ordinary replacement races.
- Worker and validator free text is recursively screened before acceptance and
  raw ledger persistence; rejected credential-shaped content is not echoed.
- Non-finite numeric input and JSON constants are rejected across budgets,
  usage, pricing, capabilities, timeouts, telemetry, and persisted records.
- Public-profile release scanning is binary-aware instead of allowing binary
  files to bypass secret and private-topology checks.

- Conceptual token-usage language no longer triggers the credential gate.
- External envelopes receive sanitized task text and no longer infer home,
  runtime-state, or private knowledge-base roots.
- Credential-bearing delegated text is omitted and never marked safe.
- Local dispatch requires a fresh host-issued attestation for selector, exact
  model, mode, context, task/risk evaluation, reasoning effort, and tools.
- Narrow repository/allowlisted roots, stable path/write binding gates, bounded
  result structures, host-measured parent context, and run-level accounting
  reservations fail closed.
- Persisted state validates objective/task/control-plane integrity and uses an
  exclusive lock plus revision compare-and-swap.
- External request IDs are unique.

### Changed

- Live model discovery now honors an exact configured model only when the
  provider lists it, then falls back to profile alias discovery.
- Codex live discovery records model-catalog and profile-resolution evidence
  with zero quality samples; this evidence does not establish worker launch
  readiness or bypass the strict no-tool compatibility gate.

- Large requested fanouts are emitted as bounded first waves with deferred task
  packets instead of unbounded simultaneous worker counts.
- Circuit breaker behavior now matches the documented three-failure threshold.
- Auto mode selects the lowest registry-relative active capable profile;
  explicit light/medium/high/xhigh/ultra requests remain deterministic.
- The anti-waste gate now applies to every hosted provider, includes bounded
  input size and conservative launch overhead, and treats provider selection
  separately from explicit economic consent.
- Residual work is always replanned into another bounded wave. The currently
  admitted Grok and Local lanes use fresh calls; Codex compatibility diagnostics
  are ephemeral and native Codex worker admission remains blocked.

## v0.1.0 - 2026-04-29

### Added

- Initial public-facing AgentFanout skill package.
- Deterministic advisory router and prompt sanitizer in `scripts/route.py`.
- Provider adapter contracts for Codex, Claude, MiniMax, and local LLM workers.
- Safety references for main-session-only tools, secrets, git state, destructive actions, and final synthesis.
- GitHub Actions CI for Python 3.11 and 3.12.
- README diagrams, FAQ, quickstart, execution model, and maintainer attribution.

### Security

- Worker prompts are designed to exclude private tools, secrets, git state changes, destructive actions, and voice/callback behavior.
- `scripts/github-preflight.sh` checks for common secret patterns, local infrastructure paths, whitespace issues, and large tracked files before public-facing pushes.

### Known Limitations

- AgentFanout is not a standalone dispatcher.
- `scripts/route.py` returns advisory routing JSON and does not launch workers.
- Provider readiness flags are routing hints and do not perform live endpoint or key validation.
- Social preview upload is a manual post-public GitHub setting.

### Not Included

- Worker subprocess orchestration.
- Concurrency limits, worker cancellation, and timeout management.
- Endpoint probes or credential validation.
- Result collection from external runtimes.
