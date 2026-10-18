# Runtime installation and drift control

The repository is the canonical public-safe source. A runtime is an immutable,
allowlisted release selected by a symlink; it is never an editable copy of the
working tree. Do not copy a runtime back into source and do not repair a
release in place.

## Release contract

[`release/public-safe-allowlist.json`](../release/public-safe-allowlist.json)
is the machine-readable authority for both the public preflight and the runtime
projection. The public profile must contain every Git-tracked source file; the
preflight fails if a normal branch or PR would carry an excluded, unscanned
blob. It excludes only untracked caches, generated output, and host-local
overlays. New files under an allowlisted public directory are included even
when untracked, so they cannot bypass sanitization. The runtime profile remains
intentionally smaller than the complete public source tree.

Each immutable release contains:

- `.agentfanout-manifest.json`: sorted path, SHA-256, size, and aggregate tree
  digest for every runtime file;
- `.agentfanout-provenance.json`: source commit and Git tree, source dirtiness
  and paths, source/release tree digests, manifest digest, timestamp, review
  reference, installer version, and tool version; and
- the allowlist and schemas used to verify the projection.

Runtime overlays are never merged into a release. A legacy runtime directory is
fully traversed and hashed before it is renamed into a local archive. Symlinks,
special/unreadable paths, and a pre-existing reserved archive manifest fail
before the active path moves. The moved tree is hashed again and its manifest
is installed atomically; any post-move failure restores the original active
directory. This preserves runtime artifacts for recovery while keeping them out
of the new public release.

## Stage, verify, promote

Use a clean, reviewed source commit. A live promotion requires `--review-ref`;
dirty source is refused. The installer creates its stage under the skill root,
so stage, release, archive, active symlink, and atomic rename share a
filesystem.

```bash
# Inspect the exact release identity; writes nothing.
python3 scripts/install-runtime.py --dry-run

# Build, manifest, provenance-check, test, and freeze a promotable release
# without changing the active runtime. Use the same exact review reference.
python3 scripts/install-runtime.py --stage-only \
  --review-ref "gitea-review-or-audit-reference"

# Promote a reviewed source release. The reference is recorded in provenance.
python3 scripts/install-runtime.py --review-ref "gitea-review-or-audit-reference"
```

The promotion command revalidates and reuses the exact immutable stage when
its source commit, Git tree, runtime-tree digest, manifest, provenance, and
review reference all match. A stage created without `--review-ref` is
diagnostic-only and cannot later be promoted. Mismatched, mutable, tampered, or
symlinked existing releases fail closed. Extra files, directories, special
paths, and renamed release directories also fail the exact frozen-tree proof.
The installer rechecks source identity and runtime bytes after executable
checks, immediately before activation, and after post-switch checks. A repeated
promotion of the already-active exact release is a verified no-op; checks are
never executed against live release bytes. Operators do not delete or rewrite
a verified stage between rehearsal and activation.

The stage runs core tests, compilation for all Python modules, parity tests, the
VLLMWorker broker self-check, and parity against the source before it is
renamed into an immutable release. Promotion switches only a temporary symlink
over the active skill path. If post-switch verification fails, the installer
switches back to the prior verified target. No command in this workflow edits
VLLMWorker. The core router suite is hermetic; an installed VLLMWorker request
contract runs only when `AGENT_FANOUT_RUN_INSTALLED_VLLM_INTEGRATION=1` is set
for an explicit host integration check. Each installer check has a bounded
timeout and verified process-group termination on expiry. A check leader may
not leave background members in its original process group: the installer
checks group extinction immediately after every leader exit, terminates any
survivors, and fails the release check regardless of the leader's exit code.
Python bytecode and import caches for both pre- and post-switch checks are
redirected to an external temporary cache, so verification never mutates an
immutable release. Install and rollback share one nonblocking, no-follow
transaction lock for the skill root, so concurrent activation attempts fail
explicitly instead of racing the active symlink or release state.

`--allow-dirty` is diagnostic-only and works only with `--dry-run` or
`--stage-only`; it cannot promote a runtime.

## Legacy bootstrap and rollback

If the active path is still a directory, first promotion archives it under
`.agentfanout-archives/` with a sorted SHA-256 archive manifest. The archive
preserves runtime-only artifacts, but none are copied into the release. Caches
and generated artifacts are excluded from release staging.

Roll back only to an exact, reviewed immutable release that is a direct child
of the configured release root:

```bash
python3 scripts/rollback-runtime.py --release "$PAI_DIR/skills/.agentfanout-releases/RELEASE_ID"
```

Without `--release`, rollback selects the previous target recorded by the last
successful promotion. A failed first promotion restores a legacy archive
automatically; manual rollback intentionally does not accept an unverified
legacy directory. Rollback uses the same exact inventory, digest, provenance,
immutability, symlink/special-path, and transaction-lock checks as promotion.
Diagnostic-only stages, dirty-source stages, mutable releases, and paths
outside the configured release root are never rollback targets. A switch or
state-write failure restores the prior active runtime.

## Post-switch indexes

Index data is host-local and not part of the public release. After promotion,
run this separate, reversible procedure:

```bash
scripts/post-switch-index.sh
```

It backs up `skill-index.json`, both compact routing indexes, and
`.codex/AGENTS.md`; runs `GenerateSkillIndex` and `InjectSkillRoutingContext`;
requires each compact routing index to remain at most 8192 bytes; verifies
`SkillSearch AgentFanout`; and records `codex debug prompt-input` output. Any
failure restores all four backups. The backup directory is retained for manual
inspection or recovery.

## Drift verification

```bash
# Fail closed: clean source, valid provenance, and exact runtime projection.
python3 scripts/check-runtime-parity.py --mode release --pretty

# Developer-only diagnostic inspection. It can inspect a dirty source but does
# not make it eligible for installation or release.
python3 scripts/check-runtime-parity.py --mode diagnostic --pretty
```

Missing or invalid manifest/provenance, mismatched source/release digests,
provider-set drift, router-schema drift, core drift, source-only files, and
runtime-only public files all fail parity. Host overlays alone are reported but
do not hide public drift.
