# Release Process

AgentFanout is public-facing. Maintainer-originated GitHub-facing changes are release work and must pass the local source review gate before any GitHub branch, pull request, tag, release, or public export is created or updated.

## Public Release Gate

Required order:

1. Prepare the change in the canonical local source repository.
2. Review the change through the local source PR/review process.
3. Complete the full security audit on the local PR/diff.
4. Document the audit result with reviewer, date, commit/PR reference, findings, remediation, and pass/fail status.
5. Create or update GitHub-facing artifacts only after the gate passes.

Agents and automation must stop before GitHub-facing activity if they cannot verify the local review and audit reference.

## Full Security Audit

The audit must include:

- `python3 tests/run_tests.py`
- `python3 -m py_compile scripts/route.py tests/run_tests.py`
- `scripts/github-preflight.sh`
- secrets, credentials, and credential-shaped examples
- private infrastructure references
- unsafe worker delegation or privilege expansion
- callback, webhook, voice, TTS, and notification leakage
- public-claim accuracy in README, docs, examples, release notes, and security policy
- dependency and supply-chain review when dependencies, CI actions, scripts, or packaging change
- prompt/data-exfiltration review for routing, worker, validator, or provider changes

Minimum audit record:

```yaml
date: YYYY-MM-DD
reviewer: maintainer-or-reviewer
local_review_ref: local-source-pr-or-commit
github_target: branch-pr-release-or-export
commands:
  - python3 tests/run_tests.py
  - python3 -m py_compile scripts/route.py tests/run_tests.py
  - scripts/github-preflight.sh
findings: []
remediation: []
result: pass|fail
```

## Exceptions

Emergency bypass is allowed only for urgent public security fixes approved by the maintainer.

Before GitHub action, document:

- why the normal local gate cannot run first
- who approved the bypass
- what public exposure the fix addresses
- when the follow-up local audit will run

The local audit must be completed within 24 hours after the emergency GitHub action.

## Violations

Premature GitHub publication is a high-severity release incident.

Response:

1. Stop further GitHub activity.
2. Assess what was exposed.
3. Revert, close, or yank the public artifact if needed.
4. Run the full security audit.
5. Document findings, remediation, and prevention.
6. Resume public work only after the incident is recorded and the gate passes.
