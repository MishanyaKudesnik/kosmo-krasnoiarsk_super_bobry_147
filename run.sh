#!/usr/bin/env bash
# Start CANOPY.  One command, no build step, no package installation.
#
#   ./run.sh              -> http://localhost:8000
#   ./run.sh 8080         -> http://localhost:8080
#   HOST=0.0.0.0 ./run.sh -> reachable from other machines on the network
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-${PORT:-8000}}"
HOST="${HOST:-127.0.0.1}"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Нужен python3 (3.9 или новее)." >&2
  exit 1
fi

if ! "$PY" -c "import numpy" >/dev/null 2>&1; then
  echo "Нужен numpy — единственная зависимость проекта:" >&2
  echo "    pip install numpy        (или: apt install python3-numpy)" >&2
  exit 1
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"

echo "FOREST BEAVER → http://${HOST}:${PORT}"
exec "$PY" -m canopy.cli serve --host "$HOST" --port "$PORT"
