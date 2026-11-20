#!/usr/bin/env bash
# Create the virtual environment and install dependencies.
set -euo pipefail
VENV="${1:-.venv}"
uv venv "$VENV" --python 3.12
uv pip install --python "$VENV/bin/python" -e ".[eval,dev]"
[ -f .env ] || { cp .env.example .env; echo "Created .env - add your API keys."; }
