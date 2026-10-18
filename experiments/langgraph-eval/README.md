# LangGraph evaluation fixture

This directory is an isolated, deterministic evaluation of whether LangGraph
can host AgentFanout's existing lifecycle and enforcement contracts. It is not
part of the shipped AgentFanout runtime and it does not add LangGraph, LangChain,
provider SDKs, LiteLLM, credentials, network access, shell access, or real tools
to the product.

The fixture keeps `RunStateV2`, `TaskUnitV1`, `WorkerResultV1`,
`NextWavePlanV1`, `HostDispatcher`, and `HostLedger` authoritative. LangGraph
only provides conditional graph routing and its in-memory checkpointer. All
worker outputs are hard-coded test packets and all provider usage is host-owned
fixture data. The fixture runs `HostDispatcher(require_capability=True)` and
therefore records frozen providers, capability-record digests, provider/profile/
selector-bound launch receipts, a five-minute lease, a heartbeat, and
validator-result provenance for every synthetic worker result. It also records
the current `ValidatorLaunchReceiptV1` before every verdict, binding the
accepted-result SHA-256, host validator policy SHA-256, lease, capability digest,
and a tagged conservative `reservation_upper_bound` usage record.

The fixture exercises Codex-only synthetic workers. It does not claim Local
coverage or fabricate a Local sandbox receipt: the signed Ed25519
sandbox-attestation contract remains fail-closed for Local adapters.

## Run on the isolated application host

From the canonical private checkout:

```bash
cd /exact/AgentFanout/worktree
python3 -m venv /tmp/agentfanout-langgraph-eval-venv
/tmp/agentfanout-langgraph-eval-venv/bin/pip install --requirement experiments/langgraph-eval/requirements.txt
PYTHONDONTWRITEBYTECODE=1 \
  /tmp/agentfanout-langgraph-eval-venv/bin/python \
  experiments/langgraph-eval/test_fixture.py
```

The virtual environment is deliberately outside the repository and may be
removed after the evaluation. Do not install the requirement into the system
Python, the AgentFanout runtime release, or the shared PAI runtime.

If `langgraph` is absent, importing `fixture_runtime.py` raises an explicit
error naming this document. The normal AgentFanout test suite never imports this
directory and gains no LangGraph dependency.

## What the six tests prove

1. A partial first batch produces a residual-only second wave and reuses the
   previous worker handle as a follow-up recommendation.
2. Contradictory worker evidence is sent to a deterministic independent
   validator and reaches an explicit validation block, not parent synthesis.
3. A parent worker-class action is denied before the fixture's parent-tool spy
   can run.
4. A crash after recording an effect turns the unacknowledged effect into
   `unknown_after_crash` on a fresh graph/ledger process; the effect executor is
   not re-run and terminal authorization is denied.
5. A blocked worker produces a blocked lifecycle action while delegable work
   remains; the parent cannot take it over.
6. The adapter's first dispatch has the same action, assignment count, and
   parent policy as a direct AgentFanout lifecycle call.

## Limits of this fixture adapter

These limits describe this experimental wrapper, not the separate hosted runner.
Synthetic capabilities use `launch_readiness` with zero quality samples and an
explicit matching launch admission basis; they do not claim measured model quality.

- `WorkerResultV1` has no durable structured unavailable/capability failure
  code. The blocked-worker test can prove no parent fallback, but the current
  lifecycle reports the generic `delegable_residual_not_dispatchable` reason.
- Validator receipts are external to `RunStateV2`. The fixture validates before
  calling the lifecycle completion branch. A production integration still needs
  a versioned lifecycle change so failed validation can create repair work rather
  than leave a completed controller that terminal authorization later rejects.
- A fixture validator is a separate graph node, but current contracts do not
  cryptographically bind validator identity to a distinct execution receipt.

Do not adopt LangGraph as production infrastructure unless every test passes,
the normal AgentFanout suite remains green, no lifecycle authority is duplicated
in the graph adapter, and the listed core contract gaps are resolved.
