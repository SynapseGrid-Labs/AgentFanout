#!/usr/bin/env bash
# Regenerate host discovery indexes after a verified AgentFanout symlink switch.
# This is deliberately separate from installation so operators can inspect the
# immutable release before mutating host-local indexes.
set -euo pipefail

PAI_DIR="${PAI_DIR:-${PAI_HOME:-$HOME/.pai}}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SKILLS_DIR="$PAI_DIR/skills"
BACKUP_DIR="${1:-$(mktemp -d "$SKILLS_DIR/.agentfanout-index-backup.XXXXXX")}"

files=(
  "$SKILLS_DIR/skill-index.json"
  "$SKILLS_DIR/skill-routing-index.json"
  "$SKILLS_DIR/skill-routing-index.md"
  "$CODEX_HOME/AGENTS.md"
)

restore() {
  local file name
  for file in "${files[@]}"; do
    name="$(printf '%s' "$file" | sha256sum | cut -d' ' -f1)"
    if [[ -e "$BACKUP_DIR/$name.missing" ]]; then
      rm -f -- "$file"
    elif [[ -f "$BACKUP_DIR/$name" ]]; then
      cp -p -- "$BACKUP_DIR/$name" "$file"
    fi
  done
}

on_error() {
  status=$?
  echo "Index update failed; restoring backups from $BACKUP_DIR" >&2
  restore
  exit "$status"
}
trap on_error ERR

mkdir -p "$BACKUP_DIR"
for file in "${files[@]}"; do
  name="$(printf '%s' "$file" | sha256sum | cut -d' ' -f1)"
  if [[ -e "$file" ]]; then
    cp -p -- "$file" "$BACKUP_DIR/$name"
  else
    : > "$BACKUP_DIR/$name.missing"
  fi
done

bun "$PAI_DIR/tools/GenerateSkillIndex.ts"
bun "$PAI_DIR/tools/InjectSkillRoutingContext.ts"

for file in "$SKILLS_DIR/skill-routing-index.json" "$SKILLS_DIR/skill-routing-index.md"; do
  size=$(stat -c '%s' "$file")
  if (( size > 8192 )); then
    echo "Compact routing index exceeds 8192 bytes: $file ($size)" >&2
    exit 1
  fi
done

bun "$PAI_DIR/tools/SkillSearch.ts" AgentFanout | tee "$BACKUP_DIR/skillsearch.txt"
grep -q "AgentFanout" "$BACKUP_DIR/skillsearch.txt"
codex debug prompt-input "AgentFanout Status" > "$BACKUP_DIR/codex-prompt-input.json"
grep -q "AgentFanout" "$BACKUP_DIR/codex-prompt-input.json"

trap - ERR
echo "Index update verified. Backups retained at $BACKUP_DIR"
