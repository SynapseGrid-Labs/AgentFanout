# Roadmap

AgentFanout is intentionally small in `v0.1.0`: a portable skill/policy layer plus deterministic advisory router.

## Near Term

- Expand routing fixtures for more task classes and provider combinations.
- Add more examples for Codex, Claude, MiniMax, and local LLM host runtimes.
- Improve documentation around worker packet construction and validator pairing.

## Future Optional Harness

A future harness may add:

- provider probes
- endpoint and key validation
- concurrency caps
- worker timeouts and cancellation
- retry policy
- normalized result capture
- local result artifacts

The harness should remain optional so AgentFanout stays portable across runtimes.

## Not Planned For Core Skill

- Built-in access to private account tools.
- Worker ownership of secrets or credentials.
- Worker ownership of git state or destructive actions.
- Final synthesis outside the main session.
