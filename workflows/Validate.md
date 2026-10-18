# Validate Workflow

Validators run after a worker returns output. A validator must inspect a bounded target, not redo the whole task.

After validation, return the compact packet to the lifecycle controller.
Residual or repair work becomes a new `TaskUnitV1`; it is not silently absorbed
by the parent session.

In provider-primary mode, validation stays provider-side first. The main session
does a compact final review and escalates only when structural or provider-side
validation cannot support the result.

## Structural validation first

Before LLM validation, check:

```yaml
schema: StructuralValidationResultV1
scope_compliance: pass|fail
file_allowlist_compliance: pass|fail
forbidden_tool_requests_absent: pass|fail
test_or_build_evidence_present_when_applicable: pass|fail
transcript_or_artifact_refs_present: pass|fail
claims_have_evidence_refs: pass|fail
```

If this fails twice for the same task packet, stop same-provider repair and
escalate to the main session.

## Validator input package

Provide:

```yaml
original_request: <user request>
worker_summary: <worker claims or patch summary>
inspection_scope: <exact files, diffs, logs, evidence, or claims to inspect>
checklist:
  - missing requirements
  - contradictions
  - hallucinated facts
  - unsafe edits
  - secret exposure
  - MCP misuse
  - verification gaps
  - overconfident conclusions
expected_output:
  - pass/fail/concerns
  - material findings only
  - recommended orchestrator action
```

Provider-primary validators must also preserve the compact packet:

```yaml
schema: CompactResultPacketV1
summary: <what changed or was found>
changed_files_or_evidence_refs: []
commands: []
exit_codes: []
structural_validation_result: <StructuralValidationResultV1>
provider_validator_result: <validator result>
unresolved_risks: []
escalation_recommendation: accept|repair|cross-provider-validation|main-session-review
```

## Validator output format

```yaml
verdict: pass|concerns|fail
material_findings:
  - finding: <specific issue>
    evidence: <file/claim/check>
    impact: <why it changes integration>
recommended_action: accept|revise|re-run-worker|main-session-review
verification_gaps:
  - <gap>
```

## Validator limits

Validators must not:

- redo the full worker task
- perform unbounded exploration
- edit files unless explicitly assigned as a worker
- use MCP tools
- use secrets
- change git state
- produce final synthesis for the user
