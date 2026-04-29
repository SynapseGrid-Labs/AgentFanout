# Validator Design

Validators are bounded reviewers, not second workers.

## Universal validator checklist

- Missing requirements
- Contradictions with the original request
- Hallucinated facts or unsupported claims
- Unsafe edits or scope creep
- Secret exposure
- MCP/private-tool misuse
- Verification gaps
- Overconfident conclusions
- File ownership violations
- Unclear evidence or untested assumptions

## Routing

| Worker/result type | Validator route |
|---|---|
| MiniMax worker | Codex/current-balanced validator when available; otherwise independent MiniMax validator with strict checklist |
| Claude worker | Current/Codex validator if cross-provider validation is available; otherwise separate Claude validator |
| Codex worker edits | Explorer validator or main-session test/check pass |
| Local model output | Current-balanced or provider-native validator before final use |
| High-risk plan | Frontier/balanced critique plus main-session decision |
| Security-sensitive review | High-capability read-only security validator; main session final risk call |
| Multi-file refactor | Diff validator plus tests in main session if available |
| Bulk classification | Sample validator plus spot checks of edge cases |

## Prompt template

Use this template after sanitizing inherited instructions:

```text
You are validating a bounded worker result for the main orchestrator.

Original request:
<request>

Worker result summary:
<summary>

Inspection scope:
<exact files, claims, evidence, or data samples>

Check only for material issues that should change the orchestrator's final action:
- missed requirements
- contradictions
- unsupported claims
- unsafe edits or scope creep
- private data exposure
- private-tool misuse
- verification gaps
- overconfident conclusions

Do not redo the task. Do not edit files. Return:
verdict: pass | concerns | fail
material_findings: [...]
recommended_action: accept | revise | re-run-worker | main-session-review
verification_gaps: [...]
```

## Escalation

Escalate to main session when:

- validator finds a material defect
- worker and validator disagree materially
- validator cannot inspect required evidence
- output involves security, secrets, private tools, destructive actions, or git state
