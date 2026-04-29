# Test Scenarios

| Prompt | Expected routing | Why |
|---|---|---|
| `launch parallel agents to review these three modules` | Parallel explorers + validator | Explicit parallel agents; read-only module review is independent |
| `orchestrator mode local, summarize these logs` | Local-first bulk-read workers; fallback if local gates fail | Local requested; log chunks are independent |
| `use cheapest capable agents to classify these files` | Local/fast-mini bulk-classify workers + sample validator | Classification is independent and cheap-worker friendly |
| `use MiniMax workers and Codex validators` | MiniMax workers + Codex/current validator | User requested provider pairing; validator required |
| `use Claude agents to review this design` | Claude explorer(s), high reasoning, validator/critique | User requested Claude; design review benefits from critique |
| `use local LLM workers to summarize these logs` | Local-first workers; fallback recorded | Local requested; summary chunks are safe if no secrets/MCP |
| `debug this failing test` | Explorer diagnosis; bounded worker only if fix is requested | Debugging benefits from independent diagnosis; edits need scope |
| `plan an architecture migration` | Main session plan + explorer critique | Architecture decisions stay integrated in main; critique useful |
| `send an email` | Main session only | MCP/private account action |
| `commit these changes` | Main session only | Git state change |
| `review security` | Read-only security explorer + main final decision | Security review should not edit unless requested |
| `make a one-line copy edit` | Main session only | Orchestration overhead is not worth it |

Run fixture tests:

```bash
python tests/run_tests.py
```
