#!/usr/bin/env bash
# The release gate. Everything that must be green before a change ships or a
# report is generated for a customer.
#
# The end-to-end tests are part of this, not an optional extra. They are the
# most product-realistic thing in the repo — a live target over a real socket
# across the whole operator journey — so if they are allowed to be skipped they
# will drift, and the first place that shows up is a customer engagement.
#
#   ./scripts/verify.sh

set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
failed=0

stage() {
  local label="$1"; shift
  printf '\n\033[1m==> %s\033[0m\n' "$label"
  if "$@"; then
    printf '\033[32m    ok\033[0m\n'
  else
    printf '\033[31m    FAILED\033[0m\n'
    failed=$((failed + 1))
  fi
}

# Fixtures must exist for the devtarget to serve and for e2e to seed from.
[ -f fixtures/fixtures.json ] || "${PY%/python}/aitenant" init >/dev/null 2>&1 || true

stage "Unit tests"              "$PY" -m pytest -q -m "not e2e"
stage "End-to-end lifecycle"    "$PY" -m pytest -q -m e2e
stage "Detector calibration"    env -u CI ./scripts/calibrate.sh
stage "Packaging"               ./scripts/smoke.sh

printf '\n'
if [ "$failed" -eq 0 ]; then
  printf '\033[32mRelease gate passed.\033[0m\n'
  exit 0
fi
printf '\033[31mRelease gate failed — %d stage(s) red.\033[0m\n' "$failed"
exit 1
