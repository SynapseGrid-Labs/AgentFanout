# Changelog

All notable changes to AgentFanout will be documented in this file.

## v0.1.0 - 2026-04-29

### Added

- Initial public-facing AgentFanout skill package.
- Deterministic advisory router and prompt sanitizer in `scripts/route.py`.
- Provider adapter contracts for Codex, Claude, MiniMax, and local LLM workers.
- Safety references for main-session-only tools, secrets, git state, destructive actions, and final synthesis.
- GitHub Actions CI for Python 3.11 and 3.12.
- README diagrams, FAQ, quickstart, execution model, and maintainer attribution.

### Security

- Worker prompts are designed to exclude private tools, secrets, git state changes, destructive actions, and voice/callback behavior.
- `scripts/github-preflight.sh` checks for common secret patterns, local infrastructure paths, whitespace issues, and large tracked files before public-facing pushes.

### Known Limitations

- AgentFanout is not a standalone dispatcher.
- `scripts/route.py` returns advisory routing JSON and does not launch workers.
- Provider readiness flags are routing hints and do not perform live endpoint or key validation.
- Social preview upload is a manual post-public GitHub setting.

### Not Included

- Worker subprocess orchestration.
- Concurrency limits, worker cancellation, and timeout management.
- Endpoint probes or credential validation.
- Result collection from external runtimes.
