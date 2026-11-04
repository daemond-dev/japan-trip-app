#!/bin/sh
# Thin wrapper: python -m charmera_toolkit backup, with every argument passed through.
#
#   scripts/backup.sh --out ~/charmera-backups --label mine
#
# Runs from any directory: the repository root is put on PYTHONPATH, so relative paths in the
# arguments stay relative to where you run it. Set PYTHON to use a particular interpreter.
# Read-only: this never writes to the chip. Linux needs udev access to the CH341A
# (scripts/99-ch341a.rules) or sudo.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec "${PYTHON:-python3}" -m charmera_toolkit backup "$@"
