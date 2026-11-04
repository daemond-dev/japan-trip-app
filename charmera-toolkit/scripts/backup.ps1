# Thin wrapper: python -m charmera_toolkit backup, with every argument passed through.
#
#   .\scripts\backup.ps1 --out C:\charmera-backups --label mine
#
# Runs from any directory: the repository root is put on PYTHONPATH, so relative paths in the
# arguments stay relative to where you run it. Set $env:PYTHON to use a particular interpreter.
# Read-only: this never writes to the chip. Windows needs the WinUSB (or libusb-win32) driver on the
# CH341A, installed with Zadig, and a Windows build of flashrom (see docs/RECOVERY.md).
$root = Split-Path -Parent $PSScriptRoot
if ($env:PYTHONPATH) { $env:PYTHONPATH = "$root;$env:PYTHONPATH" } else { $env:PYTHONPATH = $root }
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $python -m charmera_toolkit backup @args
exit $LASTEXITCODE
