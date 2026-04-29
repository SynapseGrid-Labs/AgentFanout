#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

fail=0

section() {
  printf '\n== %s ==\n' "$1"
}

run_check() {
  local name="$1"
  shift
  section "$name"
  if "$@"; then
    echo "PASS: $name"
  else
    echo "FAIL: $name"
    fail=1
  fi
}

grep_gate() {
  local name="$1"
  local pattern="$2"
  section "$name"
  local matches
  matches=$(git grep -n -E "$pattern" -- . ':!scripts/github-preflight.sh' 2>/dev/null || true)
  if [[ -n "$matches" ]]; then
    echo "$matches" | sed -n '1,80p'
    echo "FAIL: $name"
    fail=1
  else
    echo "PASS: $name"
  fi
}

large_file_gate() {
  section "Large tracked files"
  local hits=""
  while IFS= read -r file; do
    [[ -f "$file" ]] || continue
    local size
    size=$(stat -c '%s' "$file")
    if (( size > 26214400 )); then
      hits+="${size} ${file}"$'\n'
    fi
  done < <(git ls-files)

  if [[ -n "$hits" ]]; then
    printf "%s" "$hits" | sort -nr
    echo "FAIL: Large tracked files"
    fail=1
  else
    echo "PASS: Large tracked files"
  fi
}

run_check "Router tests" python3 tests/run_tests.py
run_check "Python compile" python3 -m py_compile scripts/route.py tests/run_tests.py
run_check "Git diff whitespace" git diff --check
run_check "Staged diff whitespace" git diff --cached --check

grep_gate "Private key material" "-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
grep_gate "Common token prefixes" "(ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,})"
grep_gate "Bearer tokens" "Bearer [A-Za-z0-9._-]{30,}"
grep_gate "Inline DSNs with passwords" "postgresql://[^:/\$[:space:]]+:[^@\$[:space:]]+@[^/\$[:space:]]+"
grep_gate "Local infrastructure paths" "(/home/minion|/srv/tools|192\\.168\\.|gitea-minion|OpsVault)"

large_file_gate

if (( fail != 0 )); then
  echo
  echo "GitHub preflight failed."
  exit 1
fi

echo
echo "GitHub preflight passed."
