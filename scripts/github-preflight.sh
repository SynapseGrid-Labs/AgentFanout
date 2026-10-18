#!/usr/bin/env bash
# Public-release preflight. The public profile must cover every tracked source
# file so a normal branch/PR cannot push an unscanned excluded blob.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
fail=0

if [[ "${AGENT_FANOUT_STAGE_CHECKS:-}" == "1" ]]; then
  echo "Refusing top-level public preflight with AGENT_FANOUT_STAGE_CHECKS=1."
  exit 2
fi

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

if ! python3 scripts/release-files.py --source . --profile public >/dev/null; then
  echo "Public-safe allowlist could not be resolved."
  exit 2
fi
mapfile -d '' -t release_files < <(python3 scripts/release-files.py --source . --profile public --null)
scan_files=("${release_files[@]}")

grep_gate() {
  local name="$1"
  local pattern="$2"
  section "$name"
  local matches
  # -a is deliberate: release-profile binary assets must not bypass the same
  # plaintext secret/private-identifier scan applied to source files.
  matches=$(LC_ALL=C grep -a -nH -E -- "$pattern" "${scan_files[@]}" 2>/dev/null || true)
  # Ignore only gate-declaration lines containing the intentional patterns;
  # the rest of this script is scanned like every other public file.
  matches=$(printf '%s\n' "$matches" | grep -v -E '^scripts/github-preflight\.sh:[0-9]+:grep_gate "' || true)
  if [[ -n "$matches" ]]; then
    echo "$matches" | sed -n '1,80p'
    echo "FAIL: $name"
    fail=1
  else
    echo "PASS: $name"
  fi
}

large_file_gate() {
  section "Large intended release files"
  local hits=""
  local file size
  for file in "${release_files[@]}"; do
    [[ -f "$file" ]] || continue
    size=$(stat -c '%s' "$file")
    if (( size > 26214400 )); then
      hits+="${size} ${file}"$'\n'
    fi
  done
  if [[ -n "$hits" ]]; then
    printf '%s' "$hits" | sort -nr
    echo "FAIL: Large intended release files"
    fail=1
  else
    echo "PASS: Large intended release files"
  fi
}

tracked_source_gate() {
  section "Every tracked source file is in the public profile"
  local missing
  if ! missing=$(LC_ALL=C comm -23 \
    <(git ls-files | LC_ALL=C sort) \
    <(printf '%s\n' "${release_files[@]}" | LC_ALL=C sort)); then
    echo "FAIL: tracked-file coverage comparison failed"
    fail=1
    return
  fi
  if [[ -n "$missing" ]]; then
    printf '%s\n' "$missing" | sed -n '1,80p'
    echo "FAIL: tracked files would bypass public scanning"
    fail=1
  else
    echo "PASS: Every tracked source file is in the public profile"
  fi
}

private_identifier_gate() {
  section "Maintainer private identifier denylist"
  local deny_file="${AGENT_FANOUT_PRIVATE_DENY_REGEX_FILE:-}"
  if [[ -z "$deny_file" ]]; then
    echo "PASS: optional maintainer denylist not configured"
    return
  fi
  if [[ ! -f "$deny_file" || -L "$deny_file" ]]; then
    echo "FAIL: maintainer denylist must be a regular, non-symlink file"
    fail=1
    return
  fi
  local matches
  matches=$(LC_ALL=C grep -a -nH -E -f "$deny_file" "${scan_files[@]}" 2>/dev/null || true)
  if [[ -n "$matches" ]]; then
    printf '%s\n' "$matches" | sed -n '1,80p'
    echo "FAIL: Maintainer private identifier denylist"
    fail=1
  else
    echo "PASS: Maintainer private identifier denylist"
  fi
}

run_check "Router core tests" python3 tests/run_tests.py
run_check "Python compile" python3 -m py_compile agentfanout/*.py scripts/*.py tests/*.py
run_check "Complete Python unit suite" python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'
run_check "Runtime parity tests" python3 -m unittest tests/test_runtime_parity.py
run_check "Release installer tests" python3 -m unittest tests/test_release_pipeline.py
run_check "VLLMWorker broker smoke" python3 -m unittest tests/test_vllmworker_broker_smoke.py
run_check "host enforcement runtime" python3 -m unittest tests/test_host_runtime.py
run_check "hosted orchestration runner" python3 -m unittest tests/test_hosted_runner.py
run_check "provider adapters" python3 -m unittest tests/test_provider_adapters.py
run_check "routing calibration" python3 -m unittest tests/test_calibration.py
run_check "telemetry redaction" python3 -m unittest tests/test_telemetry.py
run_check "Git diff whitespace" git diff --check
run_check "Staged diff whitespace" git diff --cached --check

grep_gate "Private key material" "-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
grep_gate "Common token prefixes" "(ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{12,})"
grep_gate "Bearer tokens" "Bearer [A-Za-z0-9._-]{30,}"
grep_gate "Inline DSNs with passwords" "postgresql://[^:/\$[:space:]]+:[^@\$[:space:]]+@[^/\$[:space:]]+"
grep_gate "Local infrastructure references" "(/home/[A-Za-z0-9_-]+|/srv/[A-Za-z0-9_-]+|192\\.168\\.|10\\.[0-9]+\\.|172\\.(1[6-9]|2[0-9]|3[0-1])\\.|ssh [A-Za-z0-9_.-]+|\\s\\.[0-9]{2,3}\\b)"
grep_gate "Private topology prose" "[Pp]rivate[[:space:]]+[A-Za-z0-9_-]+[[:space:]]+(volume|host|server|device)"
private_identifier_gate
large_file_gate
tracked_source_gate

if (( fail != 0 )); then
  echo
  echo "GitHub preflight failed."
  exit 1
fi

echo
echo "GitHub preflight passed."
