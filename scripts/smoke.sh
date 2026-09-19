#!/usr/bin/env bash
# Packaging smoke check.
#
# Builds the distribution, installs the wheel into a throwaway venv, and runs
# the CLI from a directory that is not the source tree. Catches the class of
# bug where everything works under `pip install -e .` because the repo happens
# to be on sys.path.

set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Building distribution..."
python3 -m pip install --quiet --upgrade build
rm -rf dist
python3 -m build --quiet

echo "Installing the wheel into a clean venv..."
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --upgrade pip
"$WORK/venv/bin/pip" install --quiet dist/*.whl

echo "Running the CLI outside the source tree..."
mkdir -p "$WORK/elsewhere"
cd "$WORK/elsewhere"
"$WORK/venv/bin/aitenant" --help > /dev/null
"$WORK/venv/bin/aitenant" init > /dev/null

for path in aitenant.yaml fixtures/fixtures.json fixtures/corpus/tenant_a fixtures/corpus/tenant_b; do
  if [ ! -e "$path" ]; then
    echo "FAIL: init did not produce $path"
    exit 1
  fi
done

docs=$("$WORK/venv/bin/python" -c "
import json; print(sum(len(t['documents']) for t in json.load(open('fixtures/fixtures.json'))['tenants']))
")
if [ "$docs" -ne 8 ]; then
  echo "FAIL: expected 8 fixture documents, got $docs"
  exit 1
fi

cd "$REPO"
echo "Packaging OK — wheel installs clean and the entrypoint works standalone."
