---
name: agent-fanout
description: "Task-scoped parallel worker waves for explicitly requested fanout or clearly separable multi-part work. Do not use for simple answers, single small edits, private tools, secrets, git mutation, or destructive actions."
---

# AgentFanout

Activate only for an explicit fanout request or clearly separable parallel work.
If it is not useful, continue normally and silently; do not mention AgentFanout.
Use task-scoped `AgentFanout <task>`, `AgentFanout Codex|Grok|Local <task>`, Help, Status, or Cancel.
Treat GPT-6 Astra as an explicit cost escalation: never select it automatically, never launch more than one Astra worker in a wave, and require an exact-model launch acknowledgement plus an authorization reason.
Prefer native harness agents; use the optional hardened runtime only when explicitly requested.
For an enabled V2 candidate, load `workflows/V2.md`; dispatch only its acknowledged assignment plan, with Codex-only automatic routing.
Use a verified host lease when available; otherwise label orchestration advisory and never claim enforcement.
Keep private tools, secrets, git mutation, destructive actions, and final synthesis in the parent.
Split work into the smallest bounded, non-overlapping task packets and launch independent work concurrently.
Ingest only compact findings with evidence, uncertainty, and residual work; never paste raw transcripts or route JSON.
After every result batch, rebuild the residual ledger and launch or follow up the next wave while delegable work remains.
Follow up with the same worker only for unchanged scope; use a fresh worker for changed scope or independent validation.
Use validators only for risk, contradiction, or an explicit request; do not add default low-risk validation.
Handshake covers spawn, wait, follow-up, cancel, callback, receiver ID, model/effort, and enforcement. Require host IDs.
Do not claim model control, enforcement, portability, or savings without host evidence; benchmarks govern savings claims.
Load `workflows/Activate.md` for selection and `workflows/Wave.md` for active multi-wave execution.
Load `references/ParentBoundary.md` and `references/WhenNotToFanout.md` only when needed.
