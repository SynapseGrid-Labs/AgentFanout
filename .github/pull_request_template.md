## Summary

-

## Verification

- [ ] `python3 tests/run_tests.py`
- [ ] `python3 -m py_compile scripts/route.py tests/run_tests.py`
- [ ] `scripts/github-preflight.sh`

## Public Release Gate

For maintainer-originated or synced GitHub-facing changes:

- [ ] Local source PR/review completed before this GitHub-facing update.
- [ ] Full security audit completed on the local PR/diff.
- [ ] Audit result documented with reviewer, date, commit/PR reference, findings, remediation, and pass/fail status.
- [ ] No GitHub branch, pull request, tag, release, or public export was created or updated before the local gate passed.

For external contributor PRs:

- [ ] Maintainer will complete the local source review gate before merge or release.

## Safety Checklist

- [ ] Does not delegate MCP/private tools to workers.
- [ ] Does not expose secrets or credentials to workers.
- [ ] Does not give workers git-state, destructive-action, or final-synthesis authority.
- [ ] Does not add voice, callback, webhook, or notification behavior to delegated prompts.

## Notes

-
