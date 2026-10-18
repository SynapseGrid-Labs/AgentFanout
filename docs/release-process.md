# Release Process

AgentFanout is public-facing. Maintainer-originated GitHub-facing changes are release work and must pass the local source review gate before any GitHub branch, pull request, tag, release, or public export is created or updated.

## Public Release Gate

Required order:

1. Prepare the change in the canonical local source repository.
2. Review the change through the local source PR/review process.
3. Complete the full security audit on the local PR/diff.
4. Document the audit result with reviewer, date, commit/PR reference, findings, remediation, and pass/fail status.
5. Complete profile-specific human UAT for the exact candidate.
6. Obtain explicit maintainer approval of the exact README and every image,
   identified by commit and SHA-256 hashes. Preserve the approved bytes.
7. Obtain separate authorization for the exact GitHub repository, base,
   branch upload and PR action. A branch push or draft PR is already an upload.
8. Create or update only the authorized GitHub-facing artifacts after all gates pass.

Audit every tracked file and all newly introduced commits/objects/metadata, not
only a packaging allowlist or latest diff. Inspect image pixels/frames and hidden
metadata, external references, LFS, submodules, symlinks and publication text.
Keep private evidence and maintainer denylists outside the exported tree.

Changed source requires affected engineering/audit checks to be renewed. Any
README or image change invalidates approval of the affected content. Content
approval does not authorize upload, merge, tags/releases, deployment, repository
settings or social-preview changes. Those are separate operator actions.

Agents and automation must stop before GitHub-facing activity if they cannot verify the local review and audit reference.

## Full Security Audit

The audit must include:

- `python3 tests/run_tests.py`
- `python3 -m py_compile agentfanout/*.py scripts/*.py tests/*.py`
- `python3 -m unittest tests/test_runtime_parity.py`
- `python3 -m unittest tests/test_release_pipeline.py`
- `python3 -m unittest tests/test_vllmworker_broker_smoke.py`
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
  - python3 -m py_compile agentfanout/*.py scripts/*.py tests/*.py
  - python3 -m unittest tests/test_runtime_parity.py
  - python3 -m unittest tests/test_release_pipeline.py
  - python3 -m unittest tests/test_vllmworker_broker_smoke.py
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
