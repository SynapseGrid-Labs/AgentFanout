# Host enforcement

Use `scripts/agentfanout.py run` for the supported atomic path. Use
`agentfanout.host_runtime.HostDispatcher` when building another enforced host
adapter. Direct `route.py --results-json` remains a compatibility controller
path and must not be described as host enforcement.

The supported executable path is `scripts/run-hosted-fanout.py`, backed by
`agentfanout.runner.HostedFanoutRunner`. It owns launch, concurrent collection,
compact accepted-result persistence, separate receipt-bound validation, repair/residual
replanning, conservative accounting, and terminal authorization. The lower
level `scripts/host-dispatch.py` remains available for a host implementing its
own process adapter. It validates evidence contracts but cannot authenticate a
caller-supplied receipt's origin; the embedding host remains the trust boundary.

The hosted-runner core provides a compatibility-gated Codex implementation and
admitted bounded-file Grok fixed-endpoint HTTPS and Local implementations; each
supported entrypoint must still manifest-pin and preflight the provider it
admits. Tool-capable Local requires signed launch
attestation and sandbox proof. The credential-free Local no-tool lane instead
requires fresh exact capability/bridge evidence plus a
`LocalNoToolBoundaryV1`; it cannot execute model-requested tools or inherit a
provider credential. Routing records, capability stubs, and direct
lifecycle/result ingestion are policy or compatibility surfaces, not evidence
that those providers executed.

The supported wrapper requires:

- a `LaunchReceiptV2` with the actual provider, model, effort, worker handle,
  launch correlation, and host-issued termination token;
- provider-bound verified token usage plus verified USD when available, or an
  explicitly labeled conservative reservation-unit charge, before ingestion;
- a task/result/policy-bound `ValidatorLaunchReceiptV1` before accepting a
  validator verdict;
- a passing `ValidatorResultV1` for every task marked `validator_required`;
- append-only `EffectRecordV1` transitions, with no automatic replay of
  `unknown_after_crash`;
- a signed `LocalLaunchAttestationV2` and `SandboxReceiptV1` for tool-capable
  Local work, or a manifest-pinned `LocalNoToolBoundaryV1` plus exact fresh
  capability/bridge evidence for the credential-free bounded-file lane; and
- terminal authorization after controller completion; and
- launch leases, heartbeats, and explicit cancellation through the same wrapper.

Once a process launch receipt exists, cancellation must be confirmed through a
host termination callback using that token. For the bundled POSIX adapter this
means exit of the launched process group only. Detached descendants, remote work,
and external effects require a separate sandbox/supervisor; a state-only change
is not proof that provider work stopped.

After confirmed cancellation, a host operator may return the task to the next
bounded plan with:

```bash
python3 scripts/host-dispatch.py --state-file RUN.json \
  --requeue-json '{"task_id":"task_...","reason":"bounded retry after confirmed termination"}'
```

Requeue is denied unless the ledger proves receipt, termination confirmation,
and cancellation in that order. It is also denied when an effect is pending,
unknown after a crash, or already applied, when a result was accepted, or when
any attempt/wave/worker/cost/no-progress budget is exhausted. Requeue does not
reuse the cancelled launch identity; the next host plan must mint a fresh one.
The command accepts one bounded, exact-shape object per invocation and cannot
be combined with another host-dispatch operation. It holds a run-specific
crash-releasing advisory ledger lock across state load, evidence checks, ledger
commit, and state save; every effect append observes the same lock, and the
ledger also has a unique prior-launch requeue key. Effects are refused after a
requeue until a fresh launch receipt exists. If the ledger
commit succeeded but JSON state persistence was interrupted, repeating the same
task and reason reconciles the cancelled state to the existing event; it does
not append a second event. Reconciliation rechecks current effects and budgets,
and a different reason fails closed.

Pass the Local attestation verifier key with `--local-public-key-file` or the
`AGENT_FANOUT_LOCAL_PUBLIC_KEY_FILE` environment variable. The file contains
only the base64 Ed25519 public key; keep the corresponding private key inside
the Local launch adapter or signing boundary.

`host-dispatch.py` treats its host manifest, Local verifier keys, sandbox
verifier key, and telemetry environment as trust roots. Reads are size-bounded,
descriptor-based, no-follow, owner-checked, and reject group/other-writable
files; the telemetry environment additionally requires mode `0600`. Each
operation records content and inode identities. Do not place these files in a
shared-writable directory or substitute a symlink.

The bundled `docker_policy_receipt`, `build_docker_tool_command`, and
`scripts/docker-tool-exec.py` surfaces are **experimental, non-enforced
helpers**. They are not the Local sandbox adapter. The receipt helper validates
and signs a canonical declared/observed policy projection, but it does not
collect Docker inspection data through a separately authenticated channel and
therefore emits `boundary_status: experimental_observation_only`.
`HostDispatcher` refuses that status for an enforced Local launch.

An external Local sandbox adapter must independently observe and sign
`boundary_status: host_enforced`. The signed object binds the complete canonical
tool policy, exact image digest, exact source/destination/read-only mount set,
network mode, read-only root, dropped capabilities, no-new-privileges flag,
non-privileged/no-device state, memory/CPU/PID limits, numeric user, fixed tmpfs policy, and Docker daemon
identity. The verifier recomputes the tool-policy, declared-policy, and
observation hashes and requires the declared and observed boundary objects to
match exactly. Extra or broad mounts fail closed.

The experimental command helper accepts regular-file mounts only and rejects
commas and control characters before constructing Docker's `--mount`
mini-language. A helper timeout confirms at most the local Docker CLI process
group. It does not prove that the daemon-side container stopped, so its result
uses `timeout_container_state_unknown`, keeps `container_exit_confirmed` and
`cancellation_confirmed` false, and is never eligible to satisfy an enforced
Local receipt. A production adapter must identify the container independently,
stop/kill it through the daemon, inspect the final state, and persist that
separate cancellation evidence.

The standalone host ledger defaults outside the immutable release under an
explicit safe `$XDG_STATE_HOME/agentfanout`, or the safe implicit state root
selected by the atomic CLI (`~/.local/state/agentfanout`, with
`~/.agentfanout/state` as its writable-ancestor fallback). PAI integrations may
override that location. It stores hashes and compact accepted result/effect evidence, not raw
provider transcripts. Every free-text worker and validator field is recursively
screened for configured credential patterns before acceptance and persistence,
including the SQLite WAL path. This is defense in depth, not general DLP: an
unknown or opaque secret format can evade pattern matching, so sensitive inputs
remain out of scope for the bundled hosted lanes.

Admitted-provider tokens are accepted only from the provider-bound response
evidence. Total, cached, uncached, visible-output, and reasoning usage is
retained where available. When an adapter does not report verified USD, the
task's conservative reservation units are charged and
`measured_savings_claimed` remains false. Validator calls must also provide
verified token usage.

The Grok adapter additionally binds the task's output-token cap into the HTTPS
request for both workers and validators and applies a conservative pre-receipt
prompt byte/token ceiling. Measured input/output totals remain mandatory because
providers can add hidden framing that a client cannot cap directly.

Validator process isolation means a separate receipt-bound process from the worker.
An admitted validator may use the same provider, selector, and reasoning effort;
the runtime does not claim cross-provider or model-diverse validation by default.
Same-provider/model failures may be correlated, so this is not independent
semantic evidence of correctness.

A capability record proves launch binding/readiness only. It is not calibrated
evidence that a model will meet a task's quality bar. Registry-relative cost and
capability ordering is provisional until runtime usage and outcome calibration
support a stronger claim.

Example terminal authorization:

```bash
python3 scripts/host-dispatch.py --state-file RUN.json --authorize-terminal
```

This enforcement covers operations mediated by the wrapper. It cannot prevent
a human or unrelated process from bypassing AgentFanout entirely. Use these
claim levels:

- `advisory`: prompt/router policy; no anti-bypass claim;
- `runner_enforced`: the standalone controller has no native worker surface and
  mediates all work inside its process; and
- `harness_enforced`: an independently controlled outer host hard-denies or
  mediates native worker actions and captures trustworthy evidence.
  `scripts/check-harness.py` validates shape, ordering, bindings, and operator
  pins, but it does not authenticate event origin or certify anti-bypass.

Codex hooks are supplemental detection, not a complete boundary: current
[hook documentation](https://learn.chatgpt.com/docs/hooks) describes incomplete
tool coverage and a `SubagentStart` decision that does not itself stop a
subagent. A dedicated Codex profile may disable `multi_agent` using the
[configuration reference](https://developers.openai.com/codex/config-reference),
but AgentFanout never edits global configuration. See `integrations/codex/`.
