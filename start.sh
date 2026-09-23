#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
python_bin="${PYTHON_BIN:-python3}"
if [ ! -x .venv/bin/python ]; then
  "$python_bin" -m venv .venv
fi
dependency_hash="$(shasum -a 256 requirements.lock.txt | cut -d ' ' -f 1)"
installed_hash="$(cat .venv/.windagent-dependencies 2>/dev/null || true)"
if [ "$dependency_hash" != "$installed_hash" ]; then
  .venv/bin/python -m pip install -r requirements.lock.txt
  echo "$dependency_hash" > .venv/.windagent-dependencies
fi
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-2}"
exec .venv/bin/python -m windagent serve --port "${PORT:-8000}"
