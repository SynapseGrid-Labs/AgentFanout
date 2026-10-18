# Public Candidate UAT

This checklist validates the supported product boundary before any public
release. It does not authorize publishing.

## Supported candidate profiles

- `public-skill`: portable Markdown skill plus native host agents; no Python,
  Bun, provider credential, project-root, or Linux/POSIX requirement.
- `codex-plugin`: Codex native agents plus task-scoped lifecycle hooks; Python
  3.9+, tested Codex CLI 0.144.4 or newer, Codex hooks, and one hook trust
  review are required. Its post-batch decision checks and supported-tool denial
  apply only to intercepted events. They do not prove universal anti-bypass,
  semantic completion or remote cancellation.
- `public-runtime`: optional Linux/POSIX runner with Python 3.11+, exact project
  binding, provider evidence, receipts, and fail-closed enforced execution.

The current [release assessment scope](release-scope.md) selects `public-skill`
and the reviewed Codex native integration. `public-runtime` and provider-specific
lanes are preserved outside its accepted profiles. Native steps below still
apply; an unavailable or unobserved native case is not silently marked passed.

The previous runtime-first `0.2.0-rc1` UAT is superseded. Do not use it as
public acceptance evidence.

## Standalone runtime clean-room test

```bash
python3 scripts/cleanroom-test.py --pretty > /tmp/agentfanout-cleanroom-report.json
python3 scripts/public-readiness.py \
  --cleanroom-report /tmp/agentfanout-cleanroom-report.json --pretty
```

This test applies when accepting the standalone runtime profile. It is not a
native-skill installation test. The test clones committed source into a temporary directory,
creates an isolated dependency environment, performs the immutable install,
verifies the managed launcher and exact worktree handoff, safely deactivates the
install, and confirms releases were preserved. It does not call a provider.

## User acceptance

Run these steps from a deliberately non-sensitive test repository. Do not use a
home directory, private vault, credentials tree, runtime tree, or multi-project
parent as the project root.

1. Install the local candidate plugin, review/trust its hooks, and start a fresh
   Codex session.
2. Verify an ordinary answer, one small edit, and an
   unrelated complex request produce zero AgentFanout work or visible output.
3. Request `AgentFanout Codex review four independent fixture modules` and confirm
   native Codex workers launch the smallest useful first wave.
4. Return an incomplete result with residual work and confirm wave two starts
   automatically without another user prompt.
5. Confirm the parent cannot edit or run supported work tools between the first
   result batch and its post-batch replan.
6. Return a contradiction and confirm targeted follow-up or one justified
   validator; confirm compact packets only reach the parent.
7. Confirm completion occurs only after a distinct empty-residual decision and
   that a new worker invalidates the prior decision token.
8. Confirm `AgentFanout Codex <task>` is task-scoped and that later turns do not
   inherit its provider preference. Grok/Local selector acceptance is outside
   this native assessment and retains its independent provider/profile gates.
9. Confirm `AgentFanout Cancel` releases the lease and provider unavailability
   produces an explicit blocked result without parent takeover.
   For active workers, release requires exact terminal events for every worker.
   If the host omits interrupted-child hooks, use the reviewed
   [app-server bridge](codex-cancellation.md) for this acceptance path. Confirm
   the worker UUID and pinned child turn match the terminal receipt, the lease
   remains pending on a missing/stale event, and the later ordinary turn is
   allowed only after reconciliation. A timeout or interruption acknowledgement
   is not a pass. Direct CLI compatibility remains blocked on affected hosts.
10. If testing `public-runtime`, run the separate runtime UAT below from a
   non-sensitive fixture repository; do not treat it as skill UAT.

### Optional runtime UAT

1. Install with `python3 scripts/install.py --pretty`.
2. Run a dry plan against two named UTF-8 files and confirm no provider process,
   guard, run state, or ledger is created.
3. Run one explicitly requested bounded-file job and confirm receipts, usage,
   exact provenance, residual-wave handling, cancellation, and rollback.
4. Repeat from a sensitive or broad directory and confirm fail-closed binding.

Record the candidate commit, provider, exact model, operating system, Python
version, commands, compact JSON outcomes, and pass/fail decision. Remove or
redact paths, credentials, hostnames, endpoint details, and private content
before sharing the UAT record.

## Release decision

Public release requires the engineering, security/sanitization, claim review,
private staging and user UAT applicable to the explicitly selected profiles.
The accepted source/export and supporting input hashes must be recorded. Reused
results keep their original candidate identity and documented applicability.
Standalone clean-room install, runtime parity and real strict-provider UAT apply
when accepting `public-runtime`; they do not certify or gate native-only use.
Native UAT and final human profile acceptance remain required for the selected
native release. An unresolved required native case still blocks that acceptance.

Disabled/experimental profiles do not become supported through this baseline UAT.
They require the independent live gates in [candidate status](candidate-status.md).
Exact README/image approval and separate branch-upload/PR authorization remain
required after engineering checks. Automated readiness is not human acceptance.
