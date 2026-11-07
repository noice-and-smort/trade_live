#!/bin/bash
# TradeLive for macOS (and Linux): double-click in Finder, or run in Terminal.
# First run creates a private Python environment in .venv (needs internet,
# ~1 minute). Extra arguments are passed through, e.g. ./TradeLive.command --debug
set -e
cd "$(dirname "$0")"
export PATH="$PWD/.python/bin:$HOME/.local/bin:$PATH"

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 is not installed. On macOS run:  xcode-select --install"
  echo "(or install it from https://www.python.org/downloads/)"
  read -r -p "Press Enter to close." _
  exit 1
fi

if [ ! -x .venv/bin/python ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Setting up TradeLive (first run only)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  touch .venv/.installed
fi

exec .venv/bin/python run.py "$@"
