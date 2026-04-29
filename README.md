# AgentFanout

AgentFanout is a provider-agnostic orchestration skill for useful parallel agent work. It keeps the main session in charge of safety, private tools, git state, destructive actions, and final synthesis while allowing bounded workers and validators when fanout is worth the overhead.

## What It Does

- Routes work between the main session, Codex, Claude, MiniMax, local LLMs, or future providers.
- Uses automatic guarded fanout instead of a fixed worker-count command.
- Gives each worker a compact specialist capsule by default.
- Loads richer agent/persona context only when the request actually needs it.
- Sanitizes delegated prompts so voice, callback, webhook, and notification behavior cannot leak into workers.
- Treats legacy `AgentOrchestrator` and `OrchestratorMode` phrases as compatibility hints, not global policy.

## Safety Model

The main session always owns:

- MCP/private-account tools
- secrets and credentials
- git state changes
- destructive or irreversible actions
- final synthesis and user-facing conclusions

Workers receive only bounded task packets. They must return results to the main session and must not contact notification hooks, voice systems, webhooks, or callback URLs.

## Commands

Only exact full-line `AgentFanout ...` commands mutate session state:

- `AgentFanout Status`
- `AgentFanout Help`
- `AgentFanout MiniMax`
- `AgentFanout Local`
- `AgentFanout Codex`
- `AgentFanout Claude`
- `AgentFanout HIGH`
- `AgentFanout Auto`
- `AgentFanout Clear`
- `AgentFanout Reset`

Fanout size is automatic. AgentFanout estimates independent work units, provider readiness, validation overhead, write-scope conflicts, and hard gates.

## Install

Copy this repository into your runtime skill root:

```bash
mkdir -p "${PAI_DIR:-$HOME/.pai}/skills"
cp -R AgentFanout "${PAI_DIR:-$HOME/.pai}/skills/AgentFanout"
```

If your runtime supports a skill registry, regenerate it after copying the skill.

## Deterministic Router

Run the advisory router directly:

```bash
python3 scripts/route.py "review twenty independent modules"
python3 scripts/route.py "AgentFanout Status"
python3 scripts/route.py --sanitize "prompt text"
```

Provider readiness is environment-driven:

```bash
AGENT_FANOUT_MINIMAX_AVAILABLE=1
AGENT_FANOUT_LOCAL_AVAILABLE=1
AGENT_FANOUT_CODEX_SUBAGENTS=1
AGENT_FANOUT_CLAUDE_AVAILABLE=1
```

The router also checks `$PAI_DIR/skills/MiniMax/Tools/MiniMaxExec.ts` or `$PAI_HOME/skills/MiniMax/Tools/MiniMaxExec.ts` for MiniMax readiness, falling back to `~/.pai/skills/...`.

## Tests

```bash
python3 tests/run_tests.py
python3 -m py_compile scripts/route.py tests/run_tests.py
scripts/github-preflight.sh
```

## Repository Layout

- `SKILL.md`: primary skill instructions
- `scripts/route.py`: deterministic advisory router and sanitizer
- `references/`: routing, safety, provider, validator, and token policy references
- `adapters/`: provider-specific adapter contracts
- `workflows/`: activation, routing, and validation workflows
- `tests/`: fixture-based router tests
