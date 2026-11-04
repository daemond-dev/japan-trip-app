#!/bin/sh
# Thin wrapper: python -m charmera_toolkit restore, with every argument passed through.
#
#   scripts/restore.sh --image ~/charmera-backups/mine_20260101-120000Z.bin --backup <your backup>
#
# Runs from any directory: the repository root is put on PYTHONPATH, so relative paths in the
# arguments stay relative to where you run it. Set PYTHON to use a particular interpreter.
# This WRITES the chip after you type RESTORE. Read docs/RECOVERY.md first: 3.3 V programmer,
# battery disconnected, your own backup made and checked.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec "${PYTHON:-python3}" -m charmera_toolkit restore "$@"
