# Private observability evaluation

This experiment compares private Phoenix OTLP ingestion with the existing
AgentFanout Langfuse sanitized event sink. Ingestion requires an explicit exact
private-host binding. It emits a fixed synthetic run and tests the production
allowlist with `path` and `api_key` probes; no live AgentFanout prompt, tool
output, secret, private path, account data, provider call, or public service is
in scope.

## Dependencies and isolation

`requirements.txt` has no third-party package because the harness uses only the
standard library plus the repository's existing `agentfanout.telemetry` module.
Run with a dedicated temporary virtual environment if local policy requires
one; no package installation is needed.

## Offline test

```bash
cd /exact/AgentFanout/worktree
PYTHONDONTWRITEBYTECODE=1 python3 experiments/observability-eval/test_harness.py
```

This does not open a network connection. It builds OTLP protobuf bytes locally,
verifies redaction and endpoint policy, rejects a host mismatch, scores the
rubric, and writes a temporary synthetic report.

## Private application-host execution

Run this command directly on the private host. It is the only step
that sends data. The report contains fixed synthetic trace metadata and ingestion
status, and is written mode `0600`. It omits configured endpoint addresses, local
output paths, hostnames, disk measurements and memory measurements. The harness
does not probe host resources; `resource_observations.collection` is `not_collected`.

```bash
AGENT_FANOUT_EVAL_HOSTNAME="$(hostname -s)" \
AGENT_FANOUT_EVAL_PHOENIX_OTLP_ENDPOINT="http://telemetry.example.invalid:6006/v1/traces" \
AGENT_FANOUT_EVAL_ALLOWED_HOSTS="telemetry.example.invalid" \
PYTHONDONTWRITEBYTECODE=1 python3 experiments/observability-eval/harness.py \
  --ingest --target both \
  --report-out /tmp/agentfanout-observability-eval-report.json
```

The harness hard-fails unless its local hostname matches the configured host
and the endpoint hostname is explicitly allowlisted. Phoenix traffic uses the
configured `/v1/traces` endpoint with content type `application/x-protobuf`.
Endpoint userinfo, query strings, fragments and redirects are rejected. Expected
CLI failures emit a stable blocked reason instead of raw endpoint/path exceptions.
Langfuse credentials are read from the existing
private `$XDG_CONFIG_HOME/agentfanout/langfuse.env` file (falling back to
`~/.config/agentfanout/langfuse.env`) and are never printed,
written to the report, or accepted as command-line flags.

After ingestion, use `report-template.md` to record UI/API query evidence,
dataset/evaluation support, retention/deletion behavior, storage/resource data,
and the weighted decision. Collect any operational measurements separately in
private records; they are not synthetic report data. Do not paste device details,
endpoint addresses, credentials or raw trace content into a shared report.

## Scope limits

- Phoenix hierarchy is genuine OTLP root/child span linkage.
- The existing Langfuse sink emits sanitized events only. It preserves grouping
  fields such as `run_id`, but does not create a parent/child trace hierarchy.
- Dataset/evaluation, retention/deletion, backup/restore, and UI query results
  require private operator evidence; the harness never invents those outcomes.
