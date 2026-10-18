# Confirming Codex worker cancellation

[Back to profile guide](profile-guide.md)

`AgentFanout Cancel` requests cancellation. A request or interruption
acknowledgement does not establish that a worker turn ended. The plugin keeps
the task lease and denies new assignments and worker effects at intercepted
hooks until each registered worker has a trusted terminal event.

Some Codex hosts, including the inspected 0.163 app-server implementation, do
not invoke `SubagentStop` when a child is interrupted. Direct CLI hooks alone
cannot reconcile that case. Do not clear the lease, assume an empty wait means
success, or advertise direct CLI cancellation compatibility on those hosts.

## Host-owned app-server bridge

For clients that launch a Codex app-server over stdio, the plugin includes
`plugins/agentfanout/hooks/codex_event_bridge.py`. Configure that client to
launch the bridge in place of its normal app-server command:

```bash
python3 /absolute/path/to/reviewed-plugin/hooks/codex_event_bridge.py \
  --plugin-data /absolute/path/to/private-plugin-data \
  --codex /absolute/path/to/reviewed-codex
```

This is an app-server transport command, not an interactive chat command. It
starts the specified executable with the supplied process options followed by
`app-server --listen stdio://`. It does not install a plugin, trust hooks, sign
in, change persistent configuration, enable providers, or change a running
client. Pass the client's existing reviewed process options after `--` when
needed. Preserve its authentication, approval, sandbox and plugin settings.

`--plugin-data` must name this exact plugin installation's existing private
data directory, outside worker-writable roots. It must not name a project,
another plugin namespace or a copied database. The bridge validates ownership,
permissions and non-symlink storage. A missing database is left absent. The
client still needs the reviewed plugin and its trusted hooks enabled; the
bridge cannot replace them or their authorization policy.

The hook records the host-issued child UUID and initial child turn at
`SubagentStart`, before any worker tool. An admitted follow-up records its new
turn before effects. The bridge reads only its own server's stdout. For a
`turn/completed` notification with status `interrupted`, `completed` or `failed`,
it requires exactly one armed, cancellation-pending lease with that active
worker and the same pinned turn. It verifies the lease identity and applies
normal terminal reconciliation inside one SQLite transaction. A digest-only
terminal receipt is committed atomically with the transition; receipt updates
and deletes are rejected. No prompt, model output or error text is retained.

Client stdin, agent messages, tool results, interruption acknowledgements and
files are never imported as event authority. The bridge has no network
listener. It does not infer parent identity from thread ordering or confuse a
parent session ID with a child's thread ID. Ordinary completion remains owned
by the existing hooks; the bridge only fills the pending-cancellation gap.

## Failure and compatibility limits

- Missing notifications, stale turns, missing legacy pins, ambiguous ownership,
  malformed state and unrecognized statuses leave cancellation pending.
- Event delivery is best effort. A disconnected client, lost subscription or
  host event gap can still require recovery; this bridge promises no lossless
  delivery. It never scans private transcripts to guess the missing event.
- Reconciliation errors roll back both state and receipt writes. The proxy
  reports a fixed diagnostic and disables reconciliation for that process while
  forwarding the remaining host stream. Existing hooks retain ownership.
- Oversized or incomplete protocol frames stop the owned proxy/server path.
  EOF, server exit and local process termination are not terminal proof.
- A terminal event confirms the exact host turn ended. It does not establish
  permanent worker shutdown or cancellation of remote jobs, detached processes,
  provider charges or other unresolved effects.
- The local host account, reviewed server executable, bridge and plugin storage
  are trusted. This adds no isolation against malicious processes with the same
  account's unrestricted filesystem access. Host sandbox enforcement remains
  necessary; an empty tool list or configuration label is insufficient.

This path is explicit and optional; direct CLI cancellation on a host missing
the terminal callback remains blocked. Synthetic protocol/rollback tests are
offline engineering evidence, not a live host/profile acceptance pass. Test
the exact executable, plugin and bridge before accepting that profile.

Protocol reference: [official Codex app-server turns](https://learn.chatgpt.com/docs/app-server#turns).
