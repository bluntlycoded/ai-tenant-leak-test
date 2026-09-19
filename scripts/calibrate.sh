#!/usr/bin/env bash
# Calibrate the suite against the deliberately vulnerable target.
#
# Proves two things that no clean customer report can prove on its own:
#   - with every leak flag off, the suite reports PASS (no false positives);
#   - with each leak flag on, the suite reports FAIL (it detects what it claims to).
#
# Run this after changing fixtures, the suite, or the detector.

set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
AITENANT="${AITENANT:-.venv/bin/aitenant}"
PORT="${PORT:-8000}"

export TENANT_A_TOKEN=tenant-a-token
export TENANT_B_TOKEN=tenant-b-token

pass=0
fail=0

run_scenario() {
  local label="$1"; shift
  local expect="$1"; shift   # PASS or FAIL
  local flags=("$@")

  env -u LEAK_POST_FILTER -u LEAK_CITATION_RESOLVER -u LEAK_METADATA \
      -u LEAK_SEMANTIC_CACHE -u LEAK_INDIRECT_INJECTION \
      ${flags[@]+"${flags[@]}"} \
      "$PY" -m uvicorn devtarget.app:app --port "$PORT" --log-level error \
      > "/tmp/aitlt-target-${PORT}.log" 2>&1 &
  local pid=$!

  for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
    sleep 0.25
  done

  local out exit_code
  out="$("$AITENANT" test --format json 2>&1)"
  exit_code=$?

  kill "$pid" >/dev/null 2>&1
  wait "$pid" 2>/dev/null

  local actual="PASS"
  [ "$exit_code" -eq 2 ] && actual="FAIL"
  [ "$exit_code" -eq 1 ] && actual="ERROR"

  local failed_ids
  failed_ids="$(echo "$out" | grep -oE '  FAIL [a-z]{2}-[0-9]{2}' | awk '{print $2}' | tr '\n' ' ')"

  if [ "$actual" = "$expect" ]; then
    printf '  \033[32mok\033[0m   %-46s expected %s, got %s  %s\n' "$label" "$expect" "$actual" "$failed_ids"
    pass=$((pass + 1))
  else
    printf '  \033[31mBAD\033[0m  %-46s expected %s, got %s (exit %d)\n' "$label" "$expect" "$actual" "$exit_code"
    echo "$out" | tail -25
    fail=$((fail + 1))
  fi
}

echo "Calibrating detector against devtarget..."
echo

run_scenario "secure (all boundaries enforced)"      PASS
run_scenario "post-filter retrieval"                 FAIL LEAK_POST_FILTER=1
run_scenario "unscoped citation resolver"            FAIL LEAK_CITATION_RESOLVER=1
run_scenario "unscoped metadata"                     FAIL LEAK_METADATA=1
run_scenario "tenant-blind response cache"           FAIL LEAK_SEMANTIC_CACHE=1
# Retrieval scoping is fully intact here. The leak comes from a tenant's OWN
# poisoned document telling the model to enumerate the whole index — so the
# caller sees other tenants' titles without the retrieval boundary ever failing.
# Found by the suite; the original expectation for this scenario was wrong.
run_scenario "self-owned poisoned doc dumps index"   FAIL LEAK_INDIRECT_INJECTION=1
run_scenario "indirect injection via leaked doc"     FAIL LEAK_POST_FILTER=1 LEAK_INDIRECT_INJECTION=1
run_scenario "every leak enabled"                    FAIL LEAK_POST_FILTER=1 LEAK_CITATION_RESOLVER=1 \
                                                          LEAK_METADATA=1 LEAK_SEMANTIC_CACHE=1 \
                                                          LEAK_INDIRECT_INJECTION=1

echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32mCalibration passed\033[0m — %d/%d scenarios behaved as expected.\n' "$pass" "$((pass + fail))"
  exit 0
fi
printf '\033[31mCalibration failed\033[0m — %d of %d scenarios were wrong.\n' "$fail" "$((pass + fail))"
exit 1
