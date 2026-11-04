# Thin wrapper: python -m charmera_toolkit restore, with every argument passed through.
#
#   .\scripts\restore.ps1 --image C:\charmera-backups\mine_20260101-120000Z.bin --backup <your backup>
#
# Runs from any directory: the repository root is put on PYTHONPATH, so relative paths in the
# arguments stay relative to where you run it. Set $env:PYTHON to use a particular interpreter.
# This WRITES the chip after you type RESTORE. Read docs/RECOVERY.md first: 3.3 V programmer,
# battery disconnected, your own backup made and checked.
$root = Split-Path -Parent $PSScriptRoot
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$root;$env:PYTHONPATH" } else { $env:PYTHONPATH = $root }
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $python -m charmera_toolkit restore @args
exit $LASTEXITCODE
