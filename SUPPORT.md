# Support

Use GitHub Issues for:

- usage questions
- documentation gaps
- routing or sanitizer bugs
- provider adapter suggestions
- examples that should be added to the README or references

For security-sensitive issues, use GitHub private security advisories when available. Do not post secrets, credentials, private infrastructure details, or harmful exploit payloads in public issues.

The current source contains tested standalone `runner_enforced` Grok
direct-HTTPS and attested Local bounded-file paths, a compatibility-gated Codex
path, and an advisory cross-harness skill/router. It is not a universal process
supervisor. The standalone adapters support bounded UTF-8 file analysis on
Linux/POSIX. Generic repository tools and edits remain blocked until an adapter
satisfies the published manifest, sandbox, execution, and evidence contracts.
The conformance checker is a structural linter, not an attestation authority.

When reporting a failure, include the compact `doctor` or `run` outcome, the
enforcement level, provider, exact block reason, and whether the root was
explicitly supplied or explicitly discovered. Never attach credentials, raw
worker transcripts, private paths, or host ledgers.
