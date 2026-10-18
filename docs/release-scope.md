# Release assessment scope

The selected release assessment covers the lightweight `public-skill` and the
Codex native integration (`codex-plugin`) on its reviewed host path. The host
supplies native agents, permissions, available models and cancellation events.
The skill does not require Grok, Python, an API credential or a standalone
runtime; the optional Codex plugin has its own Python and host requirements.

This scope selection does not itself establish human acceptance or authorize
publication. Final profile acceptance must name the exact source and export.
See the [native UAT](public-uat.md) and [release process](release-process.md).

## Accepted-profile boundary

Native assessment covers ordinary conversation coexistence, bounded delegation,
compact findings, residual follow-up, decision fencing, explicit failure, and
cancellation on the supported host path. A completion marker is an assertion,
not proof of semantic completion. Hooks enforce only intercepted events.

Active Codex cancellation requires exact worker and child-turn terminal events.
Hosts omitting the required callback need the reviewed
[app-server bridge](codex-cancellation.md). Direct CLI hooks alone do not satisfy
that cancellation case. Neither a cancellation acknowledgement nor local process
exit establishes permanent remote shutdown or a billing outcome.

Evidence is specific to observed host behavior and implementation inputs; it is
not universal acceptance of every host, model, plugin version or event path.
Unobserved required native cases remain unaccepted and block final profile
acceptance until resolved. Scope selection never fabricates a passing UAT.

## Preserved outside this release's accepted profiles

The standalone `public-runtime`, its Grok HTTPS and Local provider lanes,
GrokCLI subscription, native Grok, coordinator-backed Local, mixed-provider runs,
retained sessions and tool-capable upgrade workers are outside this acceptance
scope. Their code, schemas, experiments, documentation and historical evidence
are preserved. They are not promoted or newly enabled by this assessment.

The complete repository tree remains the proposed source publication boundary.
Keeping optional source available for inspection does not certify that profile
for use. Every tracked file and introduced Git object still needs sanitization,
security and public-claim review, including these out-of-scope sources.

Standalone installation/clean-room/runtime parity and live strict-provider UAT
remain requirements for a later runtime-profile release. They are not native
profile acceptance gates. Existing runtime receipts retain their original
candidate identity; no new runtime pass or current provider support is inferred.
No account authentication, provider fallback or spending authority follows from
this release scope. See [feature status](candidate-status.md) for conditional,
disabled and deferred behavior.
