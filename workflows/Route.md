# Optional runtime route

The public skill does not route every message. Native host selection happens
only after the positive activation decision in `Activate.md`.

Use the hardened `scripts/agentfanout.py` path only for an explicit advanced
runtime request. It owns exact project binding, provider admission, receipts,
enforcement, cancellation, and crash recovery. It may fail closed.

Never use the optional runtime to create a conversation-wide provider mode,
and never show a no-op route result for ordinary parent work.
