# Security Policy

## Supported Scope

Security reports should focus on:

- prompt leakage into workers
- secret or private-tool delegation
- git/destructive action delegation
- voice, callback, webhook, or notification leakage into worker prompts
- provider-routing behavior that bypasses hard gates

## Reporting

Open a private security advisory on GitHub if available. If advisories are unavailable, open a minimal issue that describes the impact without including secrets, credentials, private infrastructure details, or exploit payloads that would cause harm if copied.

## Public Release Gate

AgentFanout is public-facing. Maintainer-originated GitHub-facing changes must pass a local source review and full security audit before any GitHub branch, pull request, tag, release, or public export is created or updated.

The audit must cover:

- tests, compile checks, and `scripts/github-preflight.sh`
- secrets, credentials, and credential-shaped examples
- private infrastructure references
- unsafe worker delegation or privilege expansion
- callback, webhook, voice, TTS, and notification leakage
- public-claim accuracy in documentation
- dependency and supply-chain changes when dependencies or CI actions change
- prompt/data-exfiltration paths for routing, worker, validator, or provider changes

Any emergency bypass for urgent public security fixes must be explicitly approved by the maintainer, documented before GitHub action, and followed by the local audit within 24 hours.

## Design Commitments

Native skill instructions are advisory. Plugin checks apply only to intercepted
events and supported tools; they do not independently verify residual completeness
or prevent arbitrary parent statements. Hosted controls authorize only executions
owned by the runner. Credential/path screening is defense in depth, not DLP.
No-tool labels, empty MCP lists and synthetic provider tests cannot establish a
live isolation boundary. See [candidate status](docs/candidate-status.md) for
disabled profiles and unproved live capabilities.

AgentFanout is designed so the main session remains the security boundary. Workers and validators should receive only bounded task packets, no private account tools, no secrets, no automatic memory, no git-state authority, and no final synthesis authority. Secret hard gates include explicit secret wording and common credential-shaped syntax such as bearer headers, API-key prefixes, access-key assignments, JWT-like values, private key blocks, and password-bearing DSNs.
