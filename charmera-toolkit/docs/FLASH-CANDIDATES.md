# Flash candidates (original 2025 series only)

Three update files built from the public original-series dump, reviewed statically and by an independent
pass. None has been flashed yet. They are reproducible byte for byte from the recipe below, so they are not
committed: build them yourself from the upstream dump and compare the hashes.

Input: `charmera_stock_latest.bin` from https://github.com/benbadio/keychain-camera-plus (commit 209c25a),
SHA-256 `cc3b4b2fd7eb71ba5a441decdc86984261fac8418ee3e105d7e2b3c38b7ea42f`. The Project-Sirius dump gives the same
software region; its settings bank and photos differ, so the hashes below are for the keychain dump.

Prerequisite before any of these goes on a card: a verified full-flash backup of your own camera (`backup`),
and a yellow-boot-screen original-series body. A Millennium or CLOT body must not receive these.

| Candidate | What differs from stock | Update file | SHA-256 |
|---|---|---|---|
| A: settings-only mute | Settings bank holds one record: the factory record with UI sound 0 (CRC recomputed). Photo directory zeroed, photo slots erased. Code and resources byte-identical to stock. | `JH_5408202611050001_0FB74D14.bin` | `c4822cd7eed63169d80c5105811ab6492444793fafcc7a96e809869f2e5ccda7` |
| B: settings mute + silent WAVs | As A, plus the PCM of all five WAV resources replaced by silence (headers untouched). Survives a settings reset. | `JH_5408202611050002_0FB0BD3C.bin` | `32a7f54e5090050d099681c33378e3d23822a736279ea78f9f3442a45edc3979` |
| C: clean stock restore | Stock software, the factory settings record only, photo directory zeroed, photo slots erased. No other owner's settings or photos. | `JH_5408202611050003_0FB74C24.bin` | `9b8841d7b0674ca047a67b06594b92e67d17c387b6fab11f4b5191abc644c3ec` |

What the camera will do with each (VERIFIED from the firmware's loader and updater, not yet on hardware):

- The updater accepts the file (name sum matches), erases blocks 0 to 29, programs the file, erases block 31 (empty), then waits for a one-second power-key hold.
- On the next boot the settings loader stops at sector 1 (erased) and loads sector 0; its CRC passes, and the camera runs with those values: UI sound 0 (A, B) or 5 (C), playback volume 5, auto-off code 2, language and date-format bytes as in the factory record. (Sector 120, the zeroed photo directory, is not erased either, but the loader never gets that far.) Two bytes differ from what the dumped camera was last running: +0x03 is 0 instead of 1 and +0x53 is 0 instead of 2; both values occur in other live records of the two dumps, so they are known-good states, and your own choices (frame/style selector) start from the factory state.
- The photo directory reads as two unused entries, so playback without a card shows nothing.
- A and B: the event-sound player returns before playing for every event (power on, shutter, keys, power off). B also has silent audio data if a later settings reset turns the gate back on.

Known limits:

- A settings-only mute is undone if the camera ever falls back to defaults: CRC failure, the stock bug after about 120 settings saves (a fresh bank like these has 119 free sectors before it; the published stock restore has 104), or the SDK's Sys.Reset menu item and network set-by-id paths, which this camera cannot reach. B does not have that weakness.
- The speaker amplifier is still switched on around the (now silent) sounds; whether that produces an audible click is unknown until tested.
- Recovery from any of these is the stock restore C, or your own backup.

## Recipe

```
python -m charmera_toolkit sounds mute charmera_stock_latest.bin --out A.bin --method settings --base-record first --i-understand-this-is-a-mod
python -m charmera_toolkit pack --input A.bin --out out/A --blank-photos --stamp 202611050001 --i-understand-this-file-flashes-a-camera

python -m charmera_toolkit sounds mute charmera_stock_latest.bin --out B.bin --method both --base-record first --i-understand-this-is-a-mod
python -m charmera_toolkit pack --input B.bin --out out/B --blank-photos --stamp 202611050002 --i-understand-this-file-flashes-a-camera

python -m charmera_toolkit pack --input charmera_stock_latest.bin --out out/C --factory-settings --blank-photos --stamp 202611050003 --i-understand-this-file-flashes-a-camera

python -m charmera_toolkit verify out/A/JH_5408202611050001_0FB74D14.bin --stock charmera_stock_latest.bin
python -m charmera_toolkit verify out/B/JH_5408202611050002_0FB0BD3C.bin --stock charmera_stock_latest.bin --allow-modified
python -m charmera_toolkit verify out/C/JH_5408202611050003_0FB74C24.bin --stock charmera_stock_latest.bin
```

The stamps are fixed so the names and hashes reproduce. Any other stamp except `202505201029` works too; only the
checksum part of the name must then be recomputed, which `pack` does.

## Byte-level diff against the stock dump (first 0x1E0000 bytes)

- A: block 0x170000 (7,681 bytes: byte +0x19 of record 0, its CRC, and records 1 to 15 erased to 0xFF; the second page of record 0 was already zero), blocks 0x180000 and 0x1A0000 (the two stored photos erased to 0xFF). The photo directory at 0x17F000 was already zero in this dump.
- B: as A plus blocks 0x120000, 0x150000, 0x160000 (only the PCM bytes inside the five WAVs; 5,182 + 686 + 10,459 bytes).
- C: block 0x170000 (records 1 to 15 erased), blocks 0x180000 and 0x1A0000 (photos erased).

## Flashing order, when you are ready

1. `backup` your camera twice; keep the files.
2. `verify` the candidate against your backup (`--stock <backup>`); read every line.
3. Card formatted FAT32, only the candidate in the root, camera off, card in, power on, wait for 100%, hold power to switch off, remove the card, delete the file from it.
4. Power on. Expected: no power-on sound (A, B); the live view and the shutter work as before.
5. To revert: C, or `pack` your own backup.
