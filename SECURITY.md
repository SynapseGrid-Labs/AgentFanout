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

## Design Commitments

AgentFanout is designed so the main session remains the security boundary. Workers and validators should receive only bounded task packets, no private account tools, no secrets, no automatic memory, no git-state authority, and no final synthesis authority.
