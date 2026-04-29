# Agent Capsules

AgentFanout automatically gives delegated workers a compact specialist identity. This is not the full Agents skill context.

## Capsule Shape

```yaml
role_name: <specialist role>
expertise:
  - <2-4 task-specific bullets>
task_scope: <bounded assignment>
allowed_actions: [read|edit|test|analyze]
forbidden_actions: [mcp_private_tools, secrets, git_state_changes, destructive_actions, callbacks_voice_notifications, final_synthesis]
output_contract: <summary/evidence/patch/verdict schema>
return_only_to_main_session: true
```

## Defaults

- Coding work: coding specialist.
- Tests: test engineer.
- Security: security reviewer.
- Architecture/plans: architecture reviewer.
- Data extraction/classification: data extraction specialist.
- General read/review: research analyst.

Load full Agents tooling only when the user asks for named agents, custom agents, personality/trait-driven agents, or unusually rich specialist composition.
