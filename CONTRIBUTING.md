# Contributing

Thanks for considering a contribution to AgentFanout.

## Local Checks

Run these before opening a pull request:

```bash
python3 tests/run_tests.py
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'
python3 -m py_compile scripts/route.py scripts/agentfanout.py \
  scripts/run-hosted-fanout.py agentfanout/*.py tests/run_tests.py
scripts/github-preflight.sh
```

## Contribution Scope

Good contributions include:

- clearer skill documentation
- safer worker-prompt shaping
- better fixture coverage for `scripts/route.py`
- provider adapter documentation
- tests for routing, validation, and sanitizer behavior

Avoid changes that make AgentFanout depend on one specific runtime unless the integration is clearly isolated as an adapter.

## Safety Boundaries

AgentFanout's core safety model is intentional:

- The main session owns MCP/private tools.
- The main session owns secrets and credentials.
- The main session owns git state changes.
- The main session owns destructive actions.
- The main session owns final synthesis.

Worker prompts must stay small, scoped, and free of private-tool context, automatic memory, secrets, callback hooks, and voice/notification instructions.

## Public Release Gate

AgentFanout is public-facing. Maintainer-originated GitHub-facing changes must pass the local source review gate before any GitHub branch, pull request, tag, release, or public export is created or updated.

Before GitHub-facing activity, maintainers must:

1. Prepare the change in the canonical local source repository.
2. Review the change through the local source PR/review process.
3. Complete the full security audit on that local PR/diff.
4. Document the audit result with reviewer, date, commit/PR reference, findings, remediation, and pass/fail status.
5. Confirm the gate passed before creating or updating any GitHub-facing artifact.

The full security audit must include the local checks above, secret and credential review, private infrastructure reference review, unsafe worker delegation review, public-claim accuracy review, dependency and supply-chain review when dependencies or CI actions change, and prompt/data-exfiltration review for routing, worker, validator, or provider changes.

Emergency bypass is allowed only for urgent public security fixes approved by the maintainer. The bypass reason must be documented before GitHub action, and the local audit must be completed within 24 hours.

Premature GitHub publication is treated as a release incident: stop further GitHub activity, assess exposure, revert or yank if needed, complete the audit, document findings, and add a prevention note before resuming public work.

External contributors do not need to complete the local source audit infrastructure themselves. Maintainers complete this gate before merge or release. See the checklist in [.github/pull_request_template.md](.github/pull_request_template.md) for the required verification and release-gate attestations.

## Pull Requests

Open a pull request with:

- a clear summary of the behavior or docs changed
- the verification commands you ran
- any compatibility impact
- any security or prompt-safety considerations

Keep changes focused. Public-facing claims must name their enforcement level.
Do not describe an advisory skill, detect-only hook, or self-declared manifest
as anti-bypass enforcement. New providers must declare exact input modes and
pass root, unavailable-provider, cancellation, usage, residual-wave,
no-parent-fallback, and terminal-denial tests before they are called supported.
