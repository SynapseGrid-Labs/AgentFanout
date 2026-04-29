# AgentFanout

AgentFanout is a provider-agnostic orchestration skill and policy layer for useful parallel agent work. It keeps the main session in charge of safety, private tools, git state, destructive actions, and final synthesis while allowing host runtimes to use bounded workers and validators when fanout is worth the overhead.

![AgentFanout orchestration architecture](assets/readme/agentfanout-orchestrator.jpg)

The diagrams show the intended orchestration flow. Worker launch is performed by the host runtime; AgentFanout provides routing policy, task packets, safety gates, and validation rules.

## Execution Model

- **Skill Mode:** The host agent uses AgentFanout instructions to decompose, delegate, validate, and integrate work.
- **Router Mode:** `scripts/route.py` returns advisory JSON and sanitization output. It does not launch workers.
- **Harness Mode:** A standalone dispatcher is possible future infrastructure. It is not currently shipped.

## What It Does

- Routes work between the main session, Codex, Claude, MiniMax, local LLMs, or future providers.
- Uses automatic guarded fanout instead of a fixed worker-count command.
- Gives each worker a compact specialist capsule by default.
- Loads richer agent/persona context only when the request actually needs it.
- Sanitizes delegated prompts so voice, callback, webhook, and notification behavior cannot leak into workers.
- Treats legacy `AgentOrchestrator` and `OrchestratorMode` phrases as compatibility hints, not global policy.

## Why It Helps

![AgentFanout before and after context comparison](assets/readme/agentfanout-before-after.jpg)

AgentFanout keeps the main session's context focused by shaping small, bounded worker packets. When the host runtime supports worker execution, it sends those packets to workers; the main session receives evidence and validated results back, then performs the final integration.

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

Readiness flags and file checks are routing hints. They do not perform live endpoint probing, key validation, or health checks.

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

## FAQ

<details>
<summary>Is AgentFanout a standalone orchestrator?</summary>

No. AgentFanout is a portable skill and policy layer. It provides advisory routing via `scripts/route.py` and integration instructions for host runtimes, but it does not launch workers or manage dispatch queues.

</details>

<details>
<summary>Is this just regex routing?</summary>

No. `scripts/route.py` uses deterministic cues and regexes as one input, then applies task classification, hard safety gates, provider selection, worker capsule shaping, and validation policy. It returns an advisory route; the host runtime executes it.

</details>

<details>
<summary>Who actually launches the workers?</summary>

The host runtime launches workers using its native mechanism, such as Codex subagents, Claude agents, a MiniMax wrapper, a local LLM runner, or a future optional harness.

</details>

<details>
<summary>What does <code>route.py</code> actually do?</summary>

`scripts/route.py` returns advisory JSON for routing, mode state, worker capsules, validator requirements, warnings, and prompt sanitization. It does not dispatch workers, enforce concurrency, apply timeouts, or collect worker results.

</details>

<details>
<summary>Why not ship a full harness now?</summary>

AgentFanout is designed to be host-runtime agnostic. Shipping a dispatcher would add implementation assumptions that reduce portability. A harness can be added later as an optional layer.

</details>

<details>
<summary>What remains main-session only?</summary>

MCP/private tools, secrets, git state changes, destructive actions, authenticated calls, and final synthesis remain in the main session.

</details>

<details>
<summary>Will there be a future harness?</summary>

Possibly. A future optional harness could add provider probes, key and endpoint checks, concurrency caps, timeouts, retries, cancellation, and normalized result capture.

</details>

## Author

Created and maintained by [Trent Doney](https://github.com/trentdoney) at [SynapseGrid Labs](https://github.com/SynapseGrid-Labs).
