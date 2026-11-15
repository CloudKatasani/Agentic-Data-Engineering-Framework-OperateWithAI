#!/usr/bin/env bash
# One-command start for the OperateWithAI console and API (same convention as Build-With-AI).
#
#   ./run.sh                                  # "typical bad night" at the demo client, localhost:8090
#   ./run.sh --scenarios S01,S06 --until 00:30
#   ./run.sh --autotick 20                    # the simulated clock moves 5 minutes every 20 seconds
#   ADEF_API_TOKEN=$(openssl rand -hex 24) ./run.sh --host 0.0.0.0
#
# Tokens are read from the environment at runtime and never written to disk.
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
exec .venv/bin/python -m operatewithai serve "$@"
