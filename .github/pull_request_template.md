## Summary

-

## Verification

- [ ] `python3 tests/run_tests.py`
- [ ] `python3 -m py_compile scripts/route.py tests/run_tests.py`
- [ ] `scripts/github-preflight.sh`

## Safety Checklist

- [ ] Does not delegate MCP/private tools to workers.
- [ ] Does not expose secrets or credentials to workers.
- [ ] Does not give workers git-state, destructive-action, or final-synthesis authority.
- [ ] Does not add voice, callback, webhook, or notification behavior to delegated prompts.

## Notes

-
