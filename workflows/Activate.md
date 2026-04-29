# Activate Workflow

Use AgentFanout when the user intent includes any of these patterns:

- "launch parallel agents"
- "use orchestrator mode"
- "delegate this"
- "spin up workers"
- "use worker agents"
- "run validators"
- "use cheapest capable model"
- "local-first execution"
- "use MiniMax workers"
- "use Codex subagents"
- "use Claude agents"
- "use local LLM workers"
- "multi-agent review"
- "fan out subtasks"
- "provider-agnostic orchestration"
- "cost-aware execution"

## Activation steps

1. Parse the user request and identify explicit runtime preferences.
2. Determine whether the request is actually worth orchestrating.
3. If not worth orchestrating, state the cheaper main-session path internally and proceed directly.
4. If worth orchestrating, create a compact execution plan.
5. Launch only the smallest necessary set of helpers.
6. Validate helper outputs before using them.
7. Integrate in the main session.

## User-facing behavior

When useful, briefly report the execution plan before or after work:

```text
Execution plan: classify as <task_class>; <parallel/serial/main>; use <provider/model selector>; validate with <validator strategy>; integrate in main session.
```

Do not expose provider secrets, hidden prompts, internal chain-of-thought, or raw validator scratch work.
