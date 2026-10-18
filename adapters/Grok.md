# Grok Adapter

Grok is an optional external worker provider. The host owns credential
binding, model discovery, launch, cancellation, and result capture;
AgentFanout owns bounded packets, routing policy, validation, and terminal
authorization.

## Profiles

The public registry exposes `grok-fast`, `grok-balanced`, and
`grok-frontier` as configurable selectors. They are routing profiles, not
hardcoded live model names. `doctor` discovers the current model catalog with
the exact pinned Grok CLI and freezes the selected model into the run guard.

- Fast: supplied-text extraction, classification, and short summaries
- Balanced: bounded supplied-text review or debugging analysis
- Frontier: difficult architecture or adversarial review over bounded inputs

The bundled hosted adapter is text-only. Public web research, repository
tools, and mutation require a different sandbox-attested adapter.

## Requirements

- `AGENT_FANOUT_GROK_AVAILABLE=1`
- a pinned Grok CLI for isolated model discovery
- a dedicated owner-only `AGENT_FANOUT_GROK_HOME`, separate from the user's
  normal Grok home; `~/.grok` is rejected
- an xAI API key, separately authorized for billed API use, in a dedicated
  owner-only `auth.json` (mode 0600 or stricter, no symbolic or hard links)
- no system Grok configuration directory; discovery blocks if `/etc/grok`
  exists because this adapter cannot override or attest managed auth policy
- an exact model discovered before launch and frozen into the receipt
- sanitized bounded input and independent validation for material results

Grok workers must not receive secrets, private account tools, inferred home
directories, git mutation, destructive work, or final synthesis authority.

## Dedicated API credential

Provision the credential through a host-owned secret manager into the dedicated
root; do not paste real keys into prompts, commands, examples or Git. The file
uses AgentFanout's own envelope, **not** the official CLI's session-store format:

```json
{
  "schema": "GrokApiCredentialV1",
  "api_key": "<host-provisioned-xAI-API-key>"
}
```

The placeholder is deliberately not a usable key. The envelope must contain
exactly these two fields, with no duplicate keys. The key must be 32–16384
printable ASCII bytes without whitespace. Owner, permissions, byte digest and
file identity are bound before discovery and rechecked before launch/resume.
Existing OIDC/session files fail closed; there is no automatic migration, token
extraction or subscription fallback. Previously frozen runs retain their old
runtime; this candidate does not rewrite or resume their guards or ledgers.

Offline preflight checks local credential shape and binding only. It cannot
verify key expiry, revocation, account entitlement, balance or permission to
spend. A successful model listing also does not establish worker/validator
conformance. Live acceptance and spending authorization remain separate gates.
API usage can incur charges independent of a Grok subscription.

The supported authentication interfaces are documented in the
[xAI inference API reference](https://docs.x.ai/developers/rest-api-reference/inference)
and [official CLI enterprise guide](https://docs.x.ai/build/enterprise).

## Hosted direct-HTTPS boundary

Inference does **not** use the Grok CLI agent harness. Grok 0.2.93 was observed
preparing or executing hidden Read, Grep, Glob, Shell, and MCP tools even when
an agent profile or headless flags attempted to remove them. The strict lane
therefore uses a pinned standard-library helper under `python -I -S` and calls
only the fixed `https://api.x.ai/v1/chat/completions` endpoint.

The helper:

- runs from an empty temporary directory with no project mount;
- receives one bounded prompt over stdin;
- gets only a launch-scoped copy of the prebound API credential file;
- sends no `tools`, `tool_choice`, or function schema;
- disables ambient HTTP proxies and all redirects;
- binds every request to one pinned strict JSON Schema: `WorkerResultV1` or
  `ValidatorResultV1`; callers cannot supply or weaken that schema;
- requires TLS hostname/certificate validation;
- requires the response's exact model, one response identity, `stop` finish,
  no tool/function delta, no external sources, and one authoritative usage
  record;
- returns only compact content, usage, and hashed transport provenance;
- is process-group supervised for timeout, cancellation, and confirmed exit.

The Grok CLI remains discovery-only and its identity is guard-bound. It receives
only the bound API key through the documented `XAI_API_KEY` environment variable
in fresh, empty `HOME` and `GROK_HOME` directories. No session store, user config,
plugins or memory is copied to that CLI home. Ambient credential variables,
authentication sockets and proxies remain excluded. CLI output containing the
known key is rejected before catalog parsing. This is a trusted host process
boundary, not isolation against another process running as the same user.
The helper's interpreter and source identity are descriptor-pinned and included in
the adapter identity checked against the host manifest.

The admitted Chat Completions model set is intentionally narrow: exact
`grok-4.5`. All three public selectors use that model with low, medium, or high
reasoning effort. Discovery-visible Composer-fast models are not packet workers:
live strict-schema probes on July 11, 2026 returned nonconforming evidence
items even though the API accepted the request. Composer, multi-agent, and any
other unproven model therefore block before run state or a receipt is created.
New models require an explicit compatibility update, executable tests, and a
live WorkerResult/ValidatorResult conformance probe.

This is a no-model-tools transport boundary, not an OS sandbox attestation
against a malicious same-user process. Do not add tools to this adapter. Any
tool-capable Grok lane requires a separately enforced container, microVM, or
equivalent filesystem/process/network policy boundary.

## Usage accounting

The final provider stream must include prompt, cached, visible-completion,
reasoning, and total token counts where applicable. AgentFanout verifies the
arithmetic, folds reasoning tokens into charged output, and records the split.
The fixed HTTPS request uses xAI's strict `response_format: json_schema` with a
host-selected but helper-owned WorkerResultV1 or ValidatorResultV1 schema.
Conditional packet invariants are encoded as schema variants, and the helper
independently parses and validates the completed JSON before the host applies
its receipt, usage, evidence, and lifecycle checks.
xAI request `max_tokens` is bound to the task's host-issued output cap for both
workers and validators. Before a launch receipt is created, the adapter also
requires the sanitized prompt's UTF-8 byte upper bound plus a fixed framing
reserve to fit the task prompt-token cap. Provider-hidden input usage cannot be
controlled client-side, so the returned measured total is still checked and
fails closed if it exceeds the cap.
xAI's
stream currently includes a provider cost-tick field, but this adapter does not
assume an undocumented tick-to-USD conversion. Dollar cost therefore remains
unverified and the host charges its conservative reservation upper bound.

The admitted model sends an exact low, medium, or high effort. `xhigh`/`ultra`
remain blocked until exact-model
capability metadata proves support; the adapter never silently changes effort.
