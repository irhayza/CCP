#!/usr/bin/env sh
# Peluncur macOS/Linux: menyiapkan .venv dan dependensi sekali saja, lalu menjalankan run.py.
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then echo "Python 3.10+ belum terpasang (https://www.python.org/downloads/)."; exit 1; fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Versi Python terlalu lama. Dibutuhkan 3.10 atau lebih baru."; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Menyiapkan lingkungan Python (sekali saja)..."
  "$PY" -m venv .venv
fi
if [ ! -f .venv/.deps_ok ] || [ requirements.txt -nt .venv/.deps_ok ]; then
  echo "Memasang dependensi (butuh internet)..."
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.deps_ok
fi
exec .venv/bin/python run.py "$@"
