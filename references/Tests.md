# Test scenarios

Public examples must use concrete inputs supported by the selected adapter.

| Request | Expected result | Why |
| --- | --- | --- |
| `review README.md and SKILL.md` through an admitted atomic hosted provider | Two bounded workers, validators, and terminal authorization | Both inputs are concrete and the Grok/attested-Local adapters support bounded UTF-8 files. Native Codex remains blocked pending real-provider attestation. |
| `review four independent modules` with no module names | `binding_required:adapter_input_binding_required` before RunState or ledger creation | Phantom `request.items[]` are not executable. |
| Explicit target root while invoked from an unrelated repository | Target root is bound; cwd relation is `outside` | `$PWD` is not ambient authority. |
| Explicit root conflicts with `AGENT_FANOUT_PROJECT_ROOT` | Project-binding failure before state creation | Two authorities cannot silently disagree. |
| Repository path contains ordinary `config` or source file `src/auth.py` | Allowed | Common source names are not credentials. |
| Exact root appears in `AGENT_FANOUT_SENSITIVE_ROOTS` | Blocked | Sensitive topology is host policy, not a hardcoded private path. |
| Native `spawn_agent` attempt in an independently instrumented enforced host | Host denies it | Fixture traces test checker semantics; they do not authenticate a real harness. |
| Detect-only host claims `runner_enforced` or `harness_enforced` | Manifest validation fails | Detection is not prevention. |
| Local or Grok is unavailable | Explicit provider/adapter block; no parent fallback | A missing worker is not permission for controller takeover. |
| Worker returns same-input bounded residual work | Follow-up wave dispatches again through a fresh admitted provider call | Context is reused only by an adapter with separate retained-session proof; none of the currently admitted bundled adapters claims it. |
| Worker redirects residual work to another input | Fresh worker and no provider-context reuse | Context authority never crosses bounded inputs. |

Run the full checks:

```bash
python3 tests/run_tests.py
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check-harness.py \
  --manifest integrations/codex/standalone-runner-manifest.json \
  --events /path/to/normalized-events.json \
  --require-level runner_enforced
```

The test suite includes a clean temporary Git project, admitted fake Local/Grok
boundaries, and a fake Codex diagnostic. Unit-only Codex injections exercise
compatibility plumbing from an unrelated working directory but do not claim a
real Codex worker is admitted.
Passing a manifest/trace fixture does not authenticate a real harness or certify
anti-bypass behavior.
