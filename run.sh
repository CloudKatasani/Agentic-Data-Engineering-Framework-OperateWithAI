#!/usr/bin/env bash
# One-command start for the Operate-With-AI console and API (mirrors Build-With-AI's run.sh).
#
#   ./run.sh --demo                                   # golden products on the Demo Simulator, localhost only
#   ./run.sh --demo --profile example-b               # the same, bound to a Build-With-AI Stack Profile
#   ADEF_API_TOKEN=$(openssl rand -hex 24) ./run.sh --host 0.0.0.0              # shared token, reachable remotely
#   ADEF_IDENTITY_FILE=people.yaml ./run.sh --host 0.0.0.0                      # one token per person
#
# Tokens are read from the environment at runtime and never written to disk by Operate.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
fi
if [ ! -f .venv/.deps-installed ] || [ pyproject.toml -nt .venv/.deps-installed ]; then
  echo "Installing dependencies..."
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e .
  touch .venv/.deps-installed
fi
exec .venv/bin/python -m adef_operate serve "$@"
