# Security Policy

## Supported Scope

Security reports should focus on:

- prompt leakage into workers
- secret or private-tool delegation
- git/destructive action delegation
- voice, callback, webhook, or notification leakage into worker prompts
- provider-routing behavior that bypasses hard gates

## Reporting

Use the repository security advisory flow when GitHub private vulnerability reporting is available. If private reporting is unavailable, open a minimal public issue that describes only the affected area and impact. Do not include secrets, credentials, private infrastructure details, proof-of-concept exploit payloads, or steps that would cause harm if copied.

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

AgentFanout is designed so the main session remains the security boundary. Workers and validators should receive only bounded task packets, no private account tools, no secrets, no automatic memory, no git-state authority, and no final synthesis authority. Secret hard gates include explicit secret wording and common credential-shaped syntax such as bearer headers, API-key prefixes, access-key assignments, JWT-like values, private key blocks, and password-bearing DSNs.
