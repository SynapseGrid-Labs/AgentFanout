# GrokCLI subscription candidate

The native skill remains the default. `plugins/grok/skills/agent-fanout/SKILL.md`
is a task-scoped native profile with no hooks, MCP servers or startup injection.
Its manifest validates against installed Grok 1.0.0; installed plugin loading and
human UAT have not conformed.
`python3 scripts/grok-agentfanout.py task 'review bounded findings'` produces a
native host handoff without launching inference. It requires the native host
capability handshake and labels coordination advisory. It runs without Codex.

The explicit `grok_cli_subscription_acp` profile uses canonical provider
`GrokCLI`, independently of legacy direct-HTTPS `Grok`. Status reports
`installed_cli_not_conformed`; task admission exits 2 without spawning Grok.
There is no flag or authentication shortcut that enables this shipped candidate.

`agentfanout.grok_cli` contains bounded NDJSON transport, request/session
correlation, denied client callbacks, text-only updates, fresh-session worker and
validator primitives, host receipt verification, durable start-intent ordering,
unknown-effect quarantine and usage normalization. The host verifies evidence;
prepared session objects are never authorization receipts. Every execution
requires its own numeric reservation, including validators and residual waves.
Missing provider dollars remain unknown. No credential store is copied and no
operator config is changed. Child environment is allowlisted; actual config and
credential isolation still require binary-specific proof.

The ACP fixture's `acceptedMethod`, `effectiveModel` and `effectiveEffort` fields
are an offline contract, **not observations or claims about installed Grok**.
The optional reference wire mapping described below uses separate V2 preparation
and versioned accounting contracts. Its source-derived shapes are not installed
conformance. The shipped capability status remains unavailable until installed
wire and all independent admission proofs are accepted. Empty MCP or a sandbox label cannot
satisfy required proofs. Stdio transport alone does not admit a worker.

Usage normalization accepts only verified whole-turn cache/reasoning semantics.
Cumulative baselines are keyed by process and session epochs; request identities
deduplicate retries. Same-epoch resets and altered duplicate observations fail.
Serialize `UsageReconciler.state` with the host journal transaction; in-memory
reconciliation alone is not durable budget accounting. Old ledger hashes and
numeric host reservation charges remain intact. Cancellation closes the local
process but reports unknown remote effects and prohibits replay.

Reader/writer compatibility: `GrokCLISessionV1` and `GrokCLIUsageV1` are new
lane-specific contracts. They never reinterpret old Grok receipts or native/V2
result packets. Unsupported readers refuse these schemas. Existing hosted result
validation remains mandatory; transport text does not authorize completion.
Active work stays pinned to its runtime and conformed contract. Rollback disables
new admission, reconciles or quarantines exact effects, preserves state and ledger
history, then uses the existing verified immutable release transaction. Do not
restore an old database or remap GrokCLI into Grok.

Live dependencies are installed-binary tool/background/bootstrap isolation,
session-auth preference and effective config proof, exact model/effort readback,
usage/billing extension shapes, account-side spending enforcement, cancellation
conformance and authorized worker/validator/two-wave inference. No account,
subscription, chargeable inference, promotion or public publication was performed
by the offline fixtures.

The host verification and durable receipt callbacks must return exactly `True`.
Durable journal callbacks must commit the event and return exactly `True`; an
in-memory object, truthy mapping, `1` or `None` is insufficient. A callback failure
before start intent prevents prompting. Journal failure after possible inference
quarantines the effect and cannot enable retry. Transport handles close even when
verification or cancellation journaling fails.

Prepared-session schema binds configuration digest and accepted auth method as
well as task/input/model/effort/binary/session/process identity. Full executable
identity composes the existing provider identity authority and sealed descriptor
launch mechanics, including interpreters and script files. Supervisor cleanup
uses the existing owned process-group implementation; process exit proves only
local cleanup. Live no-tool and remote cancellation remain separate proofs.

Usage recovery state has complete bounded `seen`, `baselines` and `modes`
dictionaries. Unknown schemas, incomplete state, altered fingerprints, invalid
correlation, cache/reasoning delta inconsistencies and same-epoch mode changes
refuse. The host's `usage_reconciled` journal event persists observations and
recovery state before result acceptance. Price verification additionally requires
attested price provenance; a numeric observation alone cannot become a verified
provider charge. Host reservation accounting remains numeric.

Installed help-only observation identified Grok 1.0.0 (`3cd0d0cbce`). Its top-level
help lists `--tools`, `--disallowed-tools`, `--no-subagents`, `--no-memory`,
`--disable-web-search`, model/effort and sandbox controls. Stdio help does not
advertise auth/no-tool controls. Plugin help advertises `validate [PATH]`, but
neither help prints a manifest schema. An isolated, credential-free
`grok --no-auto-update plugin validate plugins/grok` check subsequently accepted
the manifest (exit 0) against installed Grok 1.0.0. No plugin installation,
authentication or inference occurred. Plugin loading and human UAT remain
pending. Manifest validation and help observations do not establish live worker
boundary conformance.


## Reference wire mapping and installed binding

`grok_abi.ReferenceWireMapping` maps the reviewed public source revision
`2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`. No mapping is selected automatically.
A host-verifiable `GrokACPWireBindingV1` must pin its mapping digest to the actual
binary digest and supported method set before requests or process preparation.
Binding method identifiers are logical SDK identifiers; ACP extensions serialize
with the required leading underscore (for example `_x.ai/session/usage`).
The reference revision is distinct from the installed version/revision. Static
binary strings, a version label, method names and source availability do not prove
that the installed process implements the mapping, a harmless bootstrap or a
strict tool boundary. Typed unsupported/unverified responses keep the lane closed.

The reference auth profile is `[auth] preferred_method = "oidc"` with
`disable_api_key_auth = true`. Profile validation rejects overrides and credential
provider/per-model tables; environment remains allowlisted. This module never
writes operator config, copies auth stores or calls credential-export extensions.
It requests `cached_token`, then checks `_meta.auth_mode` plus `x.ai/auth/info`
`methodId`. Advertised API auth, an expired/missing cached session, an API or external
backend response, or an access gate refuses without fallback. That observation
still does not establish account entitlement, included allowance or spending.

Model/effort use `models.currentModelId` and select `configOptions/currentValue`.
Explicit setters require returned options, rather than an empty successful ACK.
Unlisted values, ambiguous/missing controls, drift in control notifications or
prompt metadata model/session/request identities refuse. Thought chunks are
bounded separately from result text and never written to the new usage journal.
Callbacks and tools remain denied. Host-owned durable inference receipts and
numeric reservations remain mandatory after preparation and before prompting.

Whole-turn usage comes from `_meta.usage`; its sibling token counters describe the
last call and are not used for turn accounting. A separately attested
`x.ai/session/usage` observation must agree after the one fresh-session prompt. A host-attested
`session_totals_only` surface can use complete cumulative totals after a proved
empty fresh baseline; it never silently replaces required turn metadata. The
surface is pinned in the wire binding and accounting semantics.
Before prompting, a fresh baseline containing inference counters blocks bootstrap
admission. Missing cache/reasoning/creation or model-row evidence refuses; partial
usage journals known numeric counters without claiming completeness. Full input
includes cache reads and creation. Both buckets, reasoning, model calls, main-loop
turns and provider duration remain explicit. Foreign model rows or inconsistent
sums refuse. Source semantics still require installed host verification.

`GrokCLIWireUsageV1` preserves integer `costUsdTicks` (reference unit: 1e10 per USD),
provider prompt correlation, and complete token detail. Missing or partial cost
stays unknown. Metered cost is not automatically a verified marginal account
charge; host numeric reservation accounting remains authoritative. Billing is an
explicit host-authorized observation, never an automatic worker step or a billing
mutation. New/legacy credit fields, prepaid balance and unified billing can remain
unknown; an explicitly present Cent object can represent zero, while absence cannot.
No observed cap, percentage, authentication or tier grants spending authority.

`GrokCLISessionV2` adds mapping/binding digests without reinterpreting V1 sessions.
`GrokCLIWireAccountingV1` wraps the unchanged V1 accounting data with exact wire
fingerprints and provider-request deduplication. Exact integer tick changes cannot
hide behind floating-point rounding. Recovery validates all indexes, refuses reset
or duplicate conflict, and does not mutate state on failed reconciliation. V1
readers refuse the new envelope; active runs are not migrated. Persist it in the
host journal transaction before acceptance and retain compatible runtimes for
unknown effects. No old ledger events or hashes are rewritten.

The source-shaped worker/validator/residual fixtures prove engineering only.
Installed ABI discovery, auth/config/credential isolation, all tool/background/
bootstrap and cancellation proofs, account-side spending, and authorized inference
remain separate live gates. New controls remain off and runtime stays staged.
