# Contributing

Thanks for considering a contribution to AgentFanout.

## Local Checks

Run these before opening a pull request:

```bash
python3 tests/run_tests.py
python3 -m py_compile scripts/route.py tests/run_tests.py
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

## Pull Requests

Open a pull request with:

- a clear summary of the behavior or docs changed
- the verification commands you ran
- any compatibility impact
- any security or prompt-safety considerations

Keep changes focused. Public-facing docs should not claim a standalone dispatcher exists unless such a harness is actually implemented and tested.
