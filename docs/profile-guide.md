# Profile guide

[Back to the README](../README.md)

## Lightweight native skill — default

`public-skill` is portable Markdown. It needs host skill discovery and native
agent spawning/results. It supplies advisory task policy, compact packets and
repeated-wave guidance. It does not supply universal model/effort control or
enforcement. See [Get started](../README.md#get-started).

## Optional profiles

The selected [release assessment](release-scope.md) covers the default skill and
reviewed Codex native integration. Standalone runtime and provider instructions
below are preserved for reference, outside this release's accepted profiles.

### Codex plugin requirements

`codex-plugin` requires Python 3.9+, Codex plugin discovery, hooks and native
multi-agent support. Metadata records testing against CLI 0.144.4; actual loading
and profile UAT must pass on the chosen host.

Active cancellation also requires exact worker terminal events. Hosts that
omit interrupted-child hooks need the explicit
[host-owned app-server bridge](codex-cancellation.md). Direct CLI hooks alone
remain blocked for that case; interruption acknowledgements do not release a
pending lease. The bridge preserves the existing plugin and sandbox settings.

### Strict runtime requirements

`public-runtime` requires Linux/POSIX, Python 3.11+, working `venv`/`ensurepip`,
hash-locked dependencies, exact project binding and independently verified
provider evidence. It owns its hosted launches and authorization; unrelated
harnesses and processes remain outside that boundary.

### Optional Codex plugin

From a reviewed checkout, register the local marketplace:

```bash
codex plugin marketplace add /absolute/path/to/AgentFanout
codex plugin add agentfanout@agentfanout-local
```

Review and trust the hook definition in `hooks/`, then start a fresh session.
Use `python3` on Linux/macOS or `py -3` on Windows for the Python hook dependency.
Platform and CLI compatibility require the [public UAT](public-uat.md).

The plugin requires a distinct decision token after an observed worker batch.
A new worker invalidates the previous token. Completion markers are assertions
from the orchestrator, not independent proof that the work is correct or complete.
Supported `PreToolUse` calls are guarded before dispatch and between batches.
Neither `Stop` nor passive observation prevents every tool path, parent action,
or unsupported success statement. Grok's frontend does not inherit Codex hooks.

### Optional standalone runtime

Installing this profile explicitly creates an isolated environment and immutable
release, activates its managed launcher, and preserves prior releases. Read
[installation and drift](runtime-install-and-drift.md) first:

```bash
python3 scripts/install.py --dry-run --pretty
python3 scripts/install.py --pretty
```

The default launcher is `~/.local/bin/agentfanout`; data defaults to
`~/.local/share/agentfanout` (or the explicit XDG data location). Runtime state is
host-local. The installer uses hash-locked binary distributions from the fixed
PyPI HTTPS index. A clean Git checkout is required; source archives are not a
supported installation input. Installing does not configure provider credentials.

The legacy `Grok` runtime lane uses official CLI model discovery followed by a
fixed-endpoint direct-HTTPS no-model-tools helper. It is distinct from the
disabled `GrokCLI` subscription lane. It requires a dedicated owner-only API
credential root, exact model support and provider evidence. Authentication establishes
neither subscription entitlement nor included allowance, zero cost, or permission
to spend. The separate GrokCLI candidate requires official CLI authentication;
it does not extract subscription credentials or replay them over undocumented HTTP.

The legacy credential-free Local endpoint lane requires fresh host-issued exact
capability and bridge evidence. It is distinct from the disabled coordinator
integration. Hosted Codex remains blocked: a synthetic transport diagnostic
cannot attest the real provider's no-tool boundary. MiniMax and Claude routing
metadata are disabled compatibility/retired surfaces, not available adapters.
See [provider contracts](../references/Providers.md).

Preview a bounded synthetic task from the candidate checkout:

```bash
python3 scripts/agentfanout.py run \
  --project-root /absolute/path/to/fixture-project --dry-run -- \
  "review fixture/api.py and fixture/ui.py"
```

This dry plan creates no provider process, run state, guard or ledger. Real runs
require one exact eligible project root and bounded UTF-8 `file:` inputs. A
natural-language provider request does not authorize filesystem access. Broad,
sensitive, out-of-project and unresolved roots are rejected. Do not send
sensitive repositories or files to workers; known-pattern screening is not DLP.

## Experimental and disabled contracts

### Native Grok frontend — experimental

Requires the official Grok CLI and native-agent host support. Manifest validation
is historical evidence; loading and human UAT remain pending. This is advisory
task handoff. No hooks or MCP servers are bundled in this frontend; it does not
inherit Codex hooks. `native_grok_profile` remains off.

### GrokCLI subscription — disabled candidate

`grok-subscription` / `GrokCLI` is a separate ACP subscription contract, requiring
the official Grok CLI, Python 3.11+, exact installed-binary authentication, tool,
model and usage conformance, and explicit spending authority. Installed
authenticated conformance is missing. No API or native fallback is admitted.

### Coordinator Local and mixed hosted runs — disabled candidates

These are offline-tested integration primitives. They require an external
host-owned coordinator/client, exact artifact/backend/profile/epoch, role
reservations, egress policy and live conformance. The deployment-specific
coordinator implementation and credentials are not bundled.

All six upgrade controls default off: `grok_cli_subscription`,
`coordinator_local`, `mixed_provider_runs`, `native_grok_profile`,
`retained_sessions`, and `tool_capable_workers`. A flag or caller-authored receipt
cannot establish capability. Retained subscription sessions and tool-capable
upgrade workers remain deferred. See [candidate status](candidate-status.md).

## Validation, failure and recovery

The parent retains private account tools, credentials, Git mutation, destructive
actions and final synthesis. A worker receives bounded inputs and returns compact
results; its text cannot grant model, usage, launch or authorization authority.
Host receipts must precede effects. The strict runner validates identity, packet
structure, accounting and required validator evidence before accepting a result.
Semantic checks and different models can help review; they do not prove truth,
independence or absence of sensitive content.

Residual work is replanned under worker/wave/retry/time/no-progress limits.
Unavailability, failed required validation, exhausted limits, missing usage or
unreconciled effects produce an explicit blocked outcome. Strict requests do not
silently fall back to the parent or another provider. Numeric reservations remain
in force when provider dollars are unknown. Token/cost acceptance checks cannot
undo charges already incurred; pre-call caps require actual provider support.

POSIX cancellation confirms only the owned process group, not detached descendants
or remote provider/coordinator jobs. Unknown effects retain reservations and block
replay or terminal success. Recovery stays pinned to the compatible runtime and
contracts; old readers refuse unsupported schemas. Rollback must reconcile exact
effects and preserve immutable ledger history before selecting a verified prior
release. Never restore an old database over newly recorded work.

The managed runtime supports `agentfanout rollback-runtime --pretty` and
`agentfanout uninstall-runtime --pretty`. Uninstall safely deactivates the launcher
and active link while preserving releases, environment and state for review.
These commands are operator actions, not worker permissions.

The no-tool data helpers and pinned executable identities are not a complete OS
sandbox against malicious processes running as the same user. An empty MCP list,
read-only instruction or sandbox label does not prove tool isolation. External
hosts need their own independently verified mediation. See [Security](../SECURITY.md),
[host enforcement](../references/HostEnforcement.md), and [architecture](architecture.md).
