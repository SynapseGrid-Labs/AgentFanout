# Activate

Use AgentFanout only when the user explicitly requests it or the task contains
independent work that materially benefits from concurrency. Keep simple work in
the parent and say nothing about fanout.

Prefer native agents. When the AgentFanout Codex plugin reports an armed lease,
follow its task-scoped continuation decisions. Without a verified lease, label
the behavior advisory and never claim enforcement.

Choose the cheapest worker class that satisfies the task's capability, risk,
tool, and evidence requirements. Use stronger workers for complex architecture,
security, ambiguous debugging, or synthesis-sensitive analysis. Model and
reasoning selection are unavailable unless the host acknowledges them.
