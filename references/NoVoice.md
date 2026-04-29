# No-Voice / No-Callback Enforcement

This reference is for the main orchestrator. Never paste the forbidden-token list into a worker prompt. Delegated prompts must use the safe wording below after sanitation.

## Policy

Workers, explorers, validators, external providers, and local workers must return text/results only to the main orchestrator. They must not start audio/readback systems, external status pings, post-run hooks, or out-of-band contact mechanisms.

Only the main orchestrator session may use any existing voice/readback system.

## Forbidden legacy tokens and patterns

The main orchestrator must remove inherited lines containing these before a prompt reaches any delegated executor:

- voice server calls
- TTS
- `localhost:8888`
- `/notify`
- `voice_id`
- `voice_enabled`
- `speak`
- `curl`
- `webhook`
- `callback`
- notify/notification instructions
- spoken-summary hook lines

## Subagent-safe wording

Use this wording instead of the forbidden-token list:

```text
Return results only to the main orchestrator. Do not contact external services, start audio/readback behavior, run post-run status hooks, or send out-of-band status messages. Produce the requested result text only.
```

## Sanitizer behavior

Before delegation:

1. Split inherited prompt/context into lines.
2. Remove any line containing a forbidden token or legacy post-run status instruction.
3. Remove shell snippets whose purpose is external status delivery.
4. Add the subagent-safe wording.
5. Verify the delegated prompt no longer contains forbidden raw tokens.

After receiving output:

1. Check output for forbidden tokens/patterns.
2. Treat any match as a validation concern.
3. Do not execute or forward matched instructions.
4. Summarize the issue in the main session if it changes the final result.
