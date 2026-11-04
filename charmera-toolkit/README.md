# Charmera toolkit

Tools for backing up, inspecting, verifying, recovering and modding the Kodak Charmera keychain camera
(original 2025 series, Generalplus GPCV2247F, 2 MiB Puya PY25D16HB SPI flash). Windows and Linux, Python 3.9+,
standard library only.

Status: Phase 2 tools built and unit-tested on two public dumps; nothing has been flashed to a camera yet.

Documentation:

- `docs/PHASE1-REPORT.md`: the research this is built on, with every claim labelled verified, reported or inferred.
- `docs/USAGE.md`: the commands.
- `docs/RECOVERY.md`: the recovery decision tree (SD updater, CH341A, what is unknown).
- `docs/CARDS.md`: the exFAT dirty-bit problem and the FAT32 fix.
- `docs/EXIF.md`: what is wrong with the camera's EXIF and AVI dates and what `exif-fix` does.
- `docs/NOTES-boot-and-power.md`: boot sequence, auto-off and standby analysis for the quick-start and battery work.

Ground rules baked into the commands:

- Read-only by default. `inspect`, `verify`, `sounds list`, `card-check` and `exif-fix` never write to a card or camera.
- Anything destructive (`card-fix`, `restore`) prints exactly what it is about to do and waits for a typed confirmation.
- `pack` refuses to produce an update file unless the input matches a known series, and it labels the output as
  something that must be reviewed before it goes on a card.
- No Kodak or Reto firmware is distributed here. The tools work on your own backup.

```
python -m charmera_toolkit --help
```
