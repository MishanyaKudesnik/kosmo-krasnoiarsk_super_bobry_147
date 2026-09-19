#!/usr/bin/env bash
# Run the whole test suite.  No pytest, no network, no fixtures to install.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export CANOPY_QUIET=1
cd "$ROOT"
exec "${PYTHON:-python3}" tests/run_tests.py "$@"
