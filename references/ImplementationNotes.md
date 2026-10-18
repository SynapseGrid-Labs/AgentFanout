# Implementation Notes

## Architecture Decision

Use one lazy `AgentFanout` skill as the execution policy for useful delegation and fanout.

AgentFanout includes a skill/policy router, a persistent lifecycle controller,
and a host-owned executable runner. Direct `scripts/route.py` and lifecycle
result ingestion are compatibility surfaces: they do not launch provider
processes or prove enforcement. The current hosted runner admits bounded Grok
direct-HTTPS and attested Local execution. Native Codex remains unavailable
because its diagnostic cannot prove the real provider no-tool boundary.

AgentFanout combines:

- Orchestration routing concepts: provider preferences, local-first routing, hard gates, and validators.
- Agent composition concepts: compact specialist worker identity, with richer named/custom/personality composition only when needed.
- Codex adapter concepts: explorer, worker, and validator packet shaping.

AgentFanout is not injected into startup context. A host runtime should expose only compact routing metadata at startup and load this skill only when the request benefits from orchestration.

Broad investigation is worker-first. When routing returns `dispatch.status=launch_required`, the host should launch workers before doing substantive main-session research, then wait for worker results before integrating. The main session should not duplicate worker research while they run. If no configured worker mechanism is available, the host should surface `fanout_blocked`; silent main-session fallback recreates the context-growth failure AgentFanout is meant to prevent.

Host runtimes must route any user message containing `AgentFanout` before starting private knowledge-base, repository, filesystem, remote-host, or other substantive research. Inline directives such as `... AgentFanout MiniMax` are activation commands, not topic text. Provider commands are sticky for the current session until `AgentFanout Auto`, `AgentFanout Clear`, `AgentFanout Reset`, or session end.

## Token Policy

- Keep `SKILL.md` lean and navigational.
- Route first; load details second.
- Generic workers receive compact task-specific capsules from `references/AgentCapsules.md`.
- Research workers receive only scoped read-only tool permission relevant to their work unit.
- Full agent/persona context loads only for named, custom, personality-heavy, or trait-heavy agents.
- Full Codex-specific context loads only for deeper Codex persona/profile behavior.
- Worker prompts must not include automatic memory, relationship history, full skill bodies, or voice metadata.
- The parent ingests compact result packets, not full transcripts, and routes
  again after every batch while delegable work remains.

## Auto Provider Replanning

Before launch, Auto mode may replan among approved eligible profiles instead of
stopping at the first unavailable candidate. Its ordering is provisional and
registry-relative, not calibrated quality or measured savings. An explicit
provider request does not enter this chain: readiness failure reports
`fanout_blocked`, with no parent execution. Low-risk bulk work may prefer a
verified Local profile; high-risk Local work requires explicit evaluation
evidence. Claude is retired by default.

Runtime readiness flags use the `AGENT_FANOUT_*` prefix. Legacy `AGENT_ORCH_*` flags are tolerated only for compatibility.

Router readiness flags and file checks are routing hints only. The atomic
`doctor` and `run` paths add provider-specific executable, auth, helper,
endpoint, capability, and dependency preflights where implemented; they are not
general provider health or task-quality guarantees.

## Compatibility Decision

- `AgentOrchestrator` is a compatibility trigger and redirects to AgentFanout where a host runtime provides that wrapper.
- `OrchestratorMode` is a compatibility trigger and redirects to AgentFanout where a host runtime provides that wrapper.
- `CodexAgent` remains Codex adapter/reference material and cannot own global routing policy.
- Legacy triggers may provide one-turn provider hints, but only `AgentFanout ...` commands mutate conversation state.

## Approval-to-Execution

If AgentFanout created a plan and the next user message clearly approves it, execute immediately unless a safety exception applies. Runtime Plan Mode, stale plans, ambiguous approvals, and unapproved destructive/production/secret/git-state operations pause execution.

## Public Release Gate

AgentFanout is public-facing. Treat GitHub as the public distribution surface, not the first review surface.

Maintainer-originated GitHub-facing changes must follow this order:

1. Prepare the change in the canonical local source repository.
2. Review the change through the local source PR/review process.
3. Run the full security audit against that local PR/diff.
4. Document the audit result with reviewer, date, commit/PR reference, findings, remediation, and pass/fail status.
5. Only after the audit passes, create or update any GitHub-facing branch, pull request, tag, release, or public export.

Agents must stop before GitHub-facing activity if they cannot verify the local review and audit reference.

The full security audit includes tests, compile checks, `scripts/github-preflight.sh`, secret and credential review, private infrastructure reference review, unsafe worker delegation review, public-claim accuracy review, dependency/supply-chain review when relevant, and prompt/data-exfiltration review for routing, worker, validator, or provider changes.

Emergency bypass is allowed only for urgent public security fixes approved by the maintainer. Document the bypass reason before GitHub action and complete the local audit within 24 hours.

Premature GitHub publication is a high-severity release incident. Stop public work, assess exposure, revert or yank if needed, complete the audit, document findings, and add a prevention note before resuming GitHub work.

## Portable Runtime Assumptions

- Skill registry root: `$PAI_DIR/skills` or `$PAI_HOME/skills`, falling back to `~/.pai/skills`.
- MiniMax readiness: `AGENT_FANOUT_MINIMAX_AVAILABLE=1` or a MiniMax wrapper at `~/.pai/skills/MiniMax/Tools/MiniMaxExec.ts`.
- Local readiness: `AGENT_FANOUT_LOCAL_AVAILABLE=1`.
- Grok readiness: `AGENT_FANOUT_GROK_AVAILABLE=1` plus an atomic preflight of
  the dedicated auth file, pinned discovery executable, exact model, and bundled
  direct-HTTPS helper.
- Codex router readiness: Codex runtime environment variables or
  `AGENT_FANOUT_CODEX_SUBAGENTS=1`. Strict native worker admission still blocks
  until a separately trusted real-provider no-tool proof exists.
- Codex `current-fast-mini` is an advisory cheap-worker contract. A future
  admitted host runtime must pass the mini/fast model override and must not let
  native subagents default to the main-session/frontier model.
- Claude readiness: `AGENT_FANOUT_CLAUDE_AVAILABLE=1`.

## Host Enforcement Layer

`agentfanout.host_runtime` and `scripts/host-dispatch.py` add the portable host
evidence and authorization layer. Provider-native process supervision remains
adapter-owned, but an enforced run must record launches, measured usage,
validator verdicts, effects, and Local sandbox evidence before terminal
authorization. See `references/HostEnforcement.md`.

## Verification

- `python3 tests/run_tests.py`
- `python3 -m py_compile scripts/route.py tests/run_tests.py`
- `scripts/github-preflight.sh`
- `python3 scripts/route.py --project-root /exact/git/worktree "review twenty independent modules"` should produce root-bound advisory guidance when an eligible provider is configured.
- `python3 scripts/route.py --project-root /exact/git/worktree "AgentOrchestrator MiniMax review three modules"` should treat the legacy phrase as a one-turn provider hint and leave `next_state.provider_mode` as `Auto`.
