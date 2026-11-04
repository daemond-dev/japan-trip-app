# Kodak Charmera firmware: Phase 1 research report

Status: complete for Phase 1. Everything here is static analysis and public sources; nothing has been flashed.
Date: 2026-11-04. Scope: original 2025 series (matte body, yellow Kodak boot screen). No hardware was touched; every finding below comes from public files, public issue threads, and static analysis of two public flash dumps.

Labels used throughout: **VERIFIED** = reproduced here from the binaries or files; **REPORTED** = stated by a community source and cited, not reproduced here; **INFERRED** = my reasoning from evidence; **UNKNOWN** = nobody has shown it.

## 0. Sources and how each was checked

| Source | Checked at | What it contributes | How I checked it |
|---|---|---|---|
| benbadio/keychain-camera-plus | commit 209c25a (2026-10-26) | Full 2 MiB stock dump (`dumps/charmera_stock_latest.bin`), stock-restore SD file, UVC patch, packaging script, updater offsets, CH341A scripts, board photos | Cloned; hashed every binary; re-derived the checksum rule; read every script and the full git history (initial release 919b298 onwards) |
| Khimira/Project-Sirius | commit 8d64da5 (2026-07-24) | Second full 2 MiB stock dump (`Original_Firmware.bin`); issues #1 and #2 | Cloned; hashed; diffed against the keychain dump block by block; issues fetched in full |
| Qooaga/chromacrank | commit 81a3b6d (2026-11-02) | RESEARCH.md (offset table, "file offset = VA + 0x200"), V3.1 experimental release | Cloned; read current and initial (Russian) RESEARCH.md; release page fetched |
| jphastings/charmera | commit a354020 (2026-08-04) | Go EXIF rebuild, AVI transcode, camera detection by `SPIDCIM`/`GPEncoder`; issue #1 | Cloned; read exiffix, video, scan packages; issue fetched |
| RAIT-09/kodak-charmera-exif-fixer | HEAD (2026) | exiftool-based EXIF repair recipe; issue #1 (USB IDs) | Cloned; read core/adapters; issue fetched |
| byjp.pckt.blog teardown (2026-08-20) | page text | GPCV2247F and PY25D16HB chip readings, speaker wiring warning | Fetched |
| mombotro/Godox-C100-GHDK | commit 88749db (2026-10-06) | Same Generalplus SDK: FIRMWARE_MAP.md, sound-slot tool, muted-firmware test on a real C100; issue #1 | Cloned; read map and tool; issue fetched in full |
| sparkyfen/charmera | commit b0a5819 (2026-03-17) | exFAT dirty bit, MBR type 0x07 vs 0x0C, FAT32 fix | Cloned; read scripts |

Repositories hold no other issues or PRs of relevance: keychain-camera-plus, chromacrank and sparkyfen have zero issues; RAIT-09 has one (volume naming / USB IDs).

### Binaries examined (SHA-256)

| File | Size | SHA-256 |
|---|---|---|
| keychain-camera-plus `dumps/charmera_stock_latest.bin` | 2,097,152 | cc3b4b2fd7eb71ba5a441decdc86984261fac8418ee3e105d7e2b3c38b7ea42f |
| Project-Sirius `Original_Firmware.bin` | 2,097,152 | 720ed0b65c91b149b0916498732b0ecf40227736ef5e3c126c487a7b5253436c |
| keychain-camera-plus `release/stock/JH_5408202610261427_0F065D55.bin` | 1,966,080 | b10a2f1c6302afbad8c205bd2f1e23c0ed244f6f3c553b513f2bd41f4b446906 |
| keychain-camera-plus `release/webcam/JH_5408202610261351_0F065EB9.bin` | 1,966,080 | 3a89359096eb59f53e57fc1eb0c89c4f6193f71621e6b7ae1a063e245985ae6f |
| keychain-camera-plus `release/super/JH_5408202610211859_0F069EA4.bin` | 1,966,080 | 5b7d1c467ac51ba1b7f1fbcb9dff8c0a5a176f6646f5a68402eacab9f22117a1 |
| keychain-camera-plus `dumps/charmera_webcam.bin` | 2,097,152 | e457913fe34db2bfd13e6fcb317a3519cfb6a6cb051102e1ba16c0ef36813d02 |
| keychain-camera-plus `dumps/charmera_super.bin` | 2,097,152 | 9670cc0812af745148671118d019f8bcce67c6fdf4164cd2e9bc363419541445 |
| chromacrank V3.1 `JH_5408202611021513_0EFA893C.bin` (not downloaded; from release notes) | 1,966,080 | ca8ac1a01b513998cfdd7f2f8aa9f79d6f3bc38ef9efdaf63c48395feedf5b06 |

None of these files will be redistributed by the toolkit; it will reference them by URL and hash.

## 1. Hardware identity

**SoC: Generalplus GPCV2247F. VERIFIED.** Three independent lines agree. The teardown read the marking (REPORTED). The firmware's own source paths are `G:\code\2247\5408\v006\host\...` (VERIFIED, dozens of occurrences, e.g. file offset 0x46608). The "GPCV1235F" label in keychain-camera-plus comes from a different string that is also in the image: the Wi-Fi driver was compiled from `F:\gp15\GPCV1235F_WIFI_SingleCam_SDK_20191212\wifi\ssv6158\...` (VERIFIED, 18 occurrences from 0xF8BFC), an SDK for another chip whose Wi-Fi stack was linked in. The camera has no Wi-Fi radio; the stack is dead code apart from two helper functions that live code calls (REPORTED by arjunbazinga). Generalplus's product page (REPORTED): ARM7TDMI up to 144 MHz, 16 KB unified cache, 64 Mbit (8 MB) DDR in the package, hardware MJPEG up to 720p30, SPI flash controller with execute-in-place, two SD interfaces, USB 2.0 device and host, 3.3 V supply with internal 1.2 V core and 2.8 V/1.8 V sensor regulators, 200 mA Li charger, QFN64, embedded JTAG/SWD. The datasheet is behind a download gate; no SDK is public for this generation.

**Flash: Puya PY25D16HB, 16 Mbit (2 MiB), SOP8.** Chip identity REPORTED by the teardown and confirmed by a second owner's clip read. Voltage: the "H" in Puya's part coding means 2.3 to 3.6 V (VERIFIED against the published P25D16H and PY25Q16HB datasheets, which both decode "H = 2.3V~3.6V"; the PY25D16HB datasheet itself is not public). So a 3.3 V programmer is correct and a 1.8 V adapter would be wrong. Sibling parts use 256-byte pages, 4 KiB sectors, 32/64 KiB blocks, and support SFDP; the JEDEC ID is probably 85 60 15 (INFERRED). flashrom has no entry for it by name; the community read it with the generic "SFDP-capable chip" selection (REPORTED).

**USB identity: VID 1B3F PID 8301, "GENPLUS USB-MSDC DISK A".** VERIFIED: the descriptor bytes `3f 1b 01 83` sit at 0xFFE54 and the strings at 0xF3CE4 and 0xF3D10.

**Image sensor: SP140A family, module marked KC28. REPORTED only.** No sensor name exists in the image. arjunbazinga recovered a 61-write register program by emulating the bit-banged SCCB bus and matched it to open-source SP140A drivers (a SmartSens/SuperPix 720-line part); the KC28 marking comes from teardown photos. My own sensor-table analysis is still running and will be added below.

**Board.** benbadio's photos show a 10-pin "ISP" header footprint (labelled 1 and 10) and a "3V" test pad near the flash (VERIFIED from the photos; pinout UNKNOWN). The battery is a 200 mAh cell wired and taped to the board, the speaker hangs on two thin wires, the camera module plugs in on a ribbon (REPORTED, teardown).

**Editions.** Original 2025 series (yellow boot screen, this build); Millennium Edition, June 2026 (blue boot screen; press says same hardware); CLOT x Kodak edition, September 2026. Two owners report that the original-series image boots on the other editions but live view freezes or capture fails (REPORTED, Project-Sirius issues 1 and 2). No Millennium or CLOT dump is public. The only ways to tell editions apart without a dump are the body, the boot screen colour and the filter/frame names in the menu (REPORTED). The firmware's own JPEGs identify the chip, not the edition: EXIF Make "Generalplus", Model "CBB3" (VERIFIED inside the two stored photos at 0x18032C and 0x1A5B2C).

**Relation to the Godox C100.** Same Generalplus card-DVR SDK lineage and the same USB ID, which is why its sound-slot work transfers. But its community identifies the C100 SoC as a GP22B (ARM926, FreeRTOS) with a different boot-header family ("PGpssiipps", app at file 0x10200) (REPORTED), so "same platform" is only true at the SDK level. The C100's sound slots live at 0x170000; the Charmera's live at 0x120000.

## 2. Firmware container format

A note on method: the dedicated boot-header and updater reverse-engineering passes were refused by the model's safety layer, so the header fields are described from the bytes, the Generalplus tool documentation and what the rest of the firmware does with them; the updater flow in section 4 was recovered by the flash-map analysts and the completeness critic, who did reach that code, and is cross-checked against the SDK source and the hardware reports.

**Boot header, 0x000 to 0x200.** Magic `SPII` at 0. Words at 0x04 0x30000200, 0x08 0x20000000, 0x10 0x001B0000, 0x14 ASCII `SRIy`, 0x18 0x01000510, 0x1C 0x00010001, 0x20 0xD0667802, 0x24 0x00000005, then four (register, value) pairs: 0xD0000088=0xA12B, 0xD000008C=0x2495, 0xD0000028=0x8005, 0xD00000AC=0x104; zero to 0x200. VERIFIED: the first two pairs are the PLL/power unlock keys the firmware itself writes before every write to 0xD0000028 (the PMU register used at power-off), so the list is a register-setup list executed before the code runs; the firmware never reads its own header, and nothing in it is checked by the running code. UNKNOWN: the meaning of 0x1B0000 (it equals the start of the 0xFF tail in both dumps, which may be coincidence), the 0x3000/0x0200 halves, `SRIy`, and whether the ROM verifies anything. The `SPII` family is Generalplus's SPI boot-header format for this chip generation (their CodePacker tool names `HEADER_TAG=SPII` for GPL329xx/GPEL31xx and the GPCV21xx boot loaders); the published field map for GPL329xx does not fit these bytes, so the GPCV2247F variant is undocumented.

**Code image.** The ARM vector table starts at 0x200 and the image is loaded to RAM address 0, so file offset = runtime address + 0x200 (VERIFIED three ways: reset vector literal, literal pools, PC-relative string references). Regions (VERIFIED from the reset handler's constants): code and read-only data 0x200 to 0xFFA70; initialised data 0xFFA70 to 0x10B130 (RAM 0xFF870, 0xB6C0 bytes, copied in place); BSS at RAM 0x10AF30 for 0xA3820 bytes; stacks up to about RAM 0x1B0AB0. The file bytes 0x10B130 to 0x120000 are zero padding that no SPI read targets; in RAM that range is BSS and heap, which is why "free space for code" there is a trap. µC/OS-II with a 100 Hz tick; uncompressed; no checksum over the code anywhere in the image.

**Resource pack, 0x120000 to 0x168E9A.** Two-byte magic `GP`, u16 entry count (43), 24-byte entries from 0x18 (19 name bytes copied, u32 start sector of 512 bytes from 0x120000). The mount code checks only the two magic bytes; there is no checksum over the pack or its files; open-by-name strips the dot; reads are clamped to the slot (next entry's start minus own start). Contents: UI art in `GPZP` files (`GPZP` tag, raw DEFLATE, then an 8-byte trailer of CRC-32 and inflated length whose checking is UNKNOWN; RGB565 inside per the community decode), four frames as twelve 240,000-byte strips, four 112x80 frame thumbnails, the 160x80 power-on logo JPEG, a background JPEG, the menu text pack GPRS.PAK (0x141400, 14 languages), two small `GPZ` files for the USB modes, and the five sounds (section 7).

**Factory self-test sector 0x16E000.** A `JHCM` write/read-back test sector used at the factory; 0xFF in both dumps (VERIFIED).

**Settings store, 0x170000 to 0x17FFFF.** Base computed at run time as 0x120000 plus the last resource's start sector rounded up to 64 KiB (VERIFIED), which lands on 0x170000 for this pack. Records are 512-byte sectors: 252 bytes of settings, CRC-32 (zlib polynomial) little-endian at 0xFC, second 256-byte page zero. The current record is the sector before the first erased sector; a save appends; when the first erased sector is number 127 the block is erased and writing restarts, so a bank holds 127 records (VERIFIED). An all-0xFF bank makes the loader apply built-in defaults and write a zeroed photo directory (VERIFIED statically, not yet on hardware). Latent stock bug (INFERRED from the verified algorithm, untested): the photo directory sector 0x17F000 is sector 120 of this bank, so after 120 saves the next boot reads the directory as the newest settings record, fails the CRC, resets to defaults and blanks the directory. The keychain dump's zeroed directory with photos still in place looks exactly like that.

**Internal photo storage, 0x17F000 to 0x1CB000.** Not a filesystem. A 16-byte directory at 0x17F000 with two entries (u16 number, u16 used, u32 length including a 0x590-byte JPEG header template), two slots at 0x180000 and 0x1A5800 (erased 0x25000 and 0x24800 bytes respectively on save), written only when no card is mounted (INFERRED from the guard), with no size check in the save path. "Delete all" only zeroes the directory. The `SPIDCIM` name in the strings is this store; the card-side `SPIDCIM` folder is where the camera copies from it.

**0x1B0000 to 0x200000** is 0xFF on every known camera; the slot 1 photo area nominally extends to 0x1CB000 but all known dumps end at 0x1AF000.

**Update files** are the first 0x1E0000 bytes of this layout (sections 3 and 4).

## 3. The update filename checksum (VERIFIED)

The name is `JH_5408<YYYYMMDDhhmm>_<8 hex>.bin`. The eight hex digits are the unsigned 32-bit sum of every byte of the file. Not a CRC, not over a sub-range, no secret.

| Published file | Size | Byte sum of file | Name says | CRC32 (for contrast) |
|---|---|---|---|---|
| JH_5408202610261427_0F065D55.bin (stock restore) | 1,966,080 | 0F065D55 | 0F065D55 | 5B5977E4 |
| JH_5408202610261351_0F065EB9.bin (webcam) | 1,966,080 | 0F065EB9 | 0F065EB9 | 1BBCA7D8 |
| JH_5408202610211859_0F069EA4.bin (super) | 1,966,080 | 0F069EA4 | 0F069EA4 | 64ED8D3C |

Where it comes from: the updater is Generalplus's `ap_state_firmware_upgrade.c` as forked by the ODM JoyHonest. The 2014 fork for an earlier chip is public (lionyhw/JHDH-8412_1247) and does exactly this: builds `C:\JH_<PRODUCT_NUM>*.bin`, calls `_findfirst`, copies 8 hex characters out of the filename, sums every byte of the file in 256-byte reads, compares, and returns before touching flash on mismatch. "JH" is the ODM's prefix and 5408 the product number; the Charmera build adds the 12-digit stamp and the on-screen strings. Generalplus's own Android OTA code for this family (GoPlusCam_SDK) sends the same byte sum alongside the file, so the convention is the vendor's, not Reto's.

What the stamp does (VERIFIED in the binary by the updater analysis, hardware-confirmed by isaackwok): characters 7 to 18 are compared with the stamp compiled into the running firmware (`202505201029`, file offset 0x218C4). Any difference starts an update; equality is ignored. There is no "newer than" rule, so a file with an older stamp also flashes.

What is not checked: header magic, size, signature, model or edition, CRC. The filename sum is the only gate. Renaming a file therefore breaks it, and a deliberately wrong sum is a safe way to prove the mechanism on a camera: it shows "Program error" and erases nothing (hardware-confirmed, REPORTED).

## 4. The SD updater flow

VERIFIED by disassembly (file offsets) and consistent with the SDK source and the hardware reports, unless marked.

1. **Trigger.** The AP task's SD-detect path calls the check at 0x21818, but only while the OS tick is at or below 500 (the first 5 seconds after power-on), so the card must be in the slot before the camera is switched on. It finds the first `C:\JH_5408*.bin` and compares name[7..18] with the embedded stamp `202505201029` (memcmp at 0x21880); any inequality (older or newer) posts message 0x5025, the startup state enters state 0x108, which is the updater (0x21370). benbadio's "timestamp check at 0x0213B0" is actually the checksum-string copy; the stamp compare is here.
2. **Checksum.** The eight characters name[20..27] are parsed case-insensitively (0x211FC; other characters count as zero). The whole file is byte-summed in 64 KiB chunks and compared (0x2144C) before any flash operation. On mismatch a flag is set, "Program error" and "check file" are shown, and a 2-second loop clears the power-hold bit so the camera turns itself off. Nothing is erased (matches isaackwok's hardware test).
3. **Preparation.** On a good sum it deletes the SD, USB, key/battery/auto-off, root and display tasks, then runs from RAM with interrupts masked, exactly as the SDK source does.
4. **Programming.** From flash offset 0: for each 64 KiB block, erase (opcode D8), program 256-byte pages up to the file size, read back and verify; a page that verifies wrong is rewritten up to 5 times after checking it is blank; a block that still fails is retried up to 20 times in total. "Upgrade NN%" advances per block. The file is programmed byte for byte, so a file whose length is not a multiple of 256 bytes gets stale buffer bytes in its last page; the rest of the last block stays erased. Whole 64 KiB multiples avoid the question entirely.
5. **Final erase.** After the last block, one more 64 KiB block is erased at (file length rounded up to 256) + 0x10000, masked to the block (0x217B4; benbadio's observation is correct). Consequences by file size: 0x1E0000 → 0x1F0000, an empty block, harmless (hardware-proven by several owners). 0x169000 (code plus resources only) → 0x170000, which wipes the settings bank, after which the firmware recreates defaults and a blank photo directory: a factory reset, probably the SDK's intent. 0x170000 → 0x180000, which erases the first 64 KiB of photo slot 0 while the directory still lists it. 0x200000 → 0x210000, beyond the chip; what the Puya part does with address bit 21 is UNKNOWN, and one owner reports a brick after an SD update (cause unconfirmed). Never send a 2 MiB file.
6. **End.** The updater does not delete or rename the file (only open/stat/read/lseek/close exist in it), so a camera re-flashes on every boot while that card is in. It then loops forever; holding the power key about a second (20 polls of the PMU key bit at 50 ms) clears the power-hold bit and the camera switches off. There is no chime, no card-detect read and no soft reset; benbadio's "card-removal debounce at 0x0217DC" is this power-key hold. Removing the card first is still the right order, so the next boot does not flash again.
7. **What is never checked:** header magic, size, a size limit, version, series, battery level or DC-in, CRC or signature. The firmware also never reads the flash chip's JEDEC ID and never issues a chip erase; it uses only the classic command set (06, 05, 03, 02, 20, D8, 01, 04), so a replacement chip with that command set would work (INFERRED).

Open: nothing above has been timed on hardware except the community's 15 to 20 seconds; whether the updater runs on USB power without a battery is UNKNOWN.

## 5. The two public stock dumps compared (VERIFIED)

Both are the same firmware build. They are byte-identical from 0x000000 to 0x16FFFF (header, code, tables, the whole resource pack) and from 0x1B0000 to the end (all 0xFF). SHA-256 of bytes 0 to 0x170000 in both: `fe88e8de57e7b1ff1a0856127ee09a21fb3ce2dde2684662c533719b80adc5c8`. That hash is what the toolkit uses as the "original 2025 series, unmodified software" fingerprint.

They differ only in the camera's own state, which is why their whole-file hashes differ:

| Region | keychain-camera-plus dump | Project-Sirius dump |
|---|---|---|
| 0x170000 to 0x17F000 settings store | 16 records (all CRC-valid), last at 0x171E00 | 12 records, last at 0x171600 |
| 0x17F000 photo index | all zero | two entries: (slot 0, used, 38,478 B), (slot 1, used, 38,286 B) |
| 0x180000 and 0x1A5800 stored photos | two 960x720 JPEGs, camera-clock dates 2025:12:06 | two 960x720 JPEGs, dates 2025:01:01 |

Record 0 of the settings store (the factory default) is identical in both, CRC 0E801A2E. The stored JPEGs are the two pictures the camera keeps internally when no card is inserted (the quick-start guide says it holds two); their EXIF carries Make "Generalplus", Model "CBB3" and the camera's malformed date format. The keychain dump's photos are orphaned (index zeroed), which looks like a settings/index reset at some point on that camera.

Consequences:

- Either dump is a valid reference for the code and resources; chromacrank builds on Project-Sirius, keychain-camera-plus on its own dump, and both are right to.
- The published stock-restore update file is the first 0x1E0000 bytes of the keychain dump (VERIFIED, byte for byte). Flashing it writes benbadio's settings records and his two photos into the recipient camera. Harmless, but not "factory" either. The photo index in that file is zero, so the camera will not show the photos.
- Your own backup is the only restore image that is truly yours. The toolkit's `pack` can blank the settings store and the photo area when building an update file from a public dump; the firmware's behaviour on a blank store (recreate defaults) is INFERRED from arjunbazinga's emulation and not yet hardware-tested, so this report treats "flash a public dump as published" as the proven path and "blanked" as the cleaner but less proven one.

## 6. Recovery paths, in order of preference

**Before anything else: a verified full-flash backup of your own camera.** Every path below is only as good as the image you restore. The public dumps are the same build but carry another owner's settings and photos, and they are useless for a Millennium or CLOT body.

### 6.1 SD-card updater with a known-good image (preferred when the camera boots)

Works whenever the camera gets far enough to mount the card, which the reports so far say it does even when capture is broken (the two cross-edition owners both still had a working updater). Procedure (VERIFIED mechanics, REPORTED procedure):

1. Build the update file from your own backup with `pack` (0x1E0000 bytes, fresh stamp, byte-sum name) and run `verify` on it.
2. FAT32 card (the camera's exFAT formatting also works for reading the update, but FAT32 avoids the Android problem), only that one `JH_5408*.bin` in the root.
3. Camera off, card in, power on. Screen shows "Upgrade 0%" to "100%" in roughly 15 to 20 seconds, then "not poweroff" style wait. Remove the card, power off with the button, power on.
4. Delete the file from the card; the camera would flash again on every boot with that card.

Failure modes: wrong checksum in the name → "Program error", nothing erased, camera turns itself off (safe). Power loss during the 15 seconds → the chip is partly erased and the camera will not boot; only path 6.2 remains. A file whose length is not a multiple of 256 bytes gets stale bytes in its last page; a file shorter than 0x1E0000 moves the final erase into the settings bank or the photo area (section 4); keep to 0x1E0000. A full 2 MiB file → the final erase targets 0x210000, beyond the chip; what the Puya part does with that address is UNKNOWN and one owner reports a brick after an SD update, cause unconfirmed. Never send a 2 MiB file. The card must be inserted before power-on (the updater only arms in the first 5 seconds), and the file is never deleted by the camera.

### 6.2 In-circuit SPI read and write with a 3.3 V programmer (the only path for a camera that will not boot)

What is known: SOP8 package with legs, so a SOIC-8 clip works (REPORTED, two owners). 2.3 to 3.6 V part (VERIFIED from Puya's coding), so program at 3.3 V; a 1.8 V adapter is wrong. The usual CH341A warnings apply with full force: unmodified black boards drive 5 V on the data lines (flashrom's documentation says so), which exceeds the flash's and the SoC's absolute maximum; use a blue board set to 3.3 V, a V1.7 board switched to 3.3 V, a modified black board, a Raspberry Pi Pico with pico-serprog, or a CH347. The SoC shares the 3.3 V rail, so the clip back-powers it through the flash's VCC pin; disconnect the battery, and if the chip is not detected or reads differ between passes, that is the first suspect (flashrom's in-system-programming notes; libreboot; REPORTED). Read twice, compare, hash, keep both copies before writing anything.

flashrom: no chip entry by name; `-c "SFDP-capable chip"` worked for the community (REPORTED). Linux needs udev access to USB 1a86:5512 or root; Windows needs the libusb driver via Zadig and a Windows flashrom build. Elnec and XGecu programmers list PY25D16HB explicitly (REPORTED, vendor lists) if a dedicated programmer is preferred.

Restore source order: your own backup; then the published stock update's full-image equivalent (the keychain dump, or Project-Sirius) only for an original-series body, accepting that it carries someone else's settings and two photos (or let `pack --blank-settings --blank-photos` clear them, INFERRED-safe).

### 6.3 Boot-ROM level: nothing usable is known

Generalplus's older GPL32 family has a mask-ROM "USB service mode" entered by pulling a strap pin high at reset, which their mass-production tool uses to load an ISP stub into RAM and program the SPI NOR (VERIFIED from Generalplus's own CodePacker guide and a tool log for the GP15B platform). Whether the GPCV2247F ROM has the same mode, which pin selects it, and what tool speaks to it are UNKNOWN; nobody in the community has demonstrated it, the Godox owner found no ROM recovery, and the datasheet is gated. The board's 10-pin "ISP" footprint may be that strap or JTAG (the chip has JTAG/SWD; the Godox firmware does not disable JTAG, REPORTED), but the pinout is unknown. Treat this as a research lead, not a plan.

### 6.4 USB vendor commands: not a recovery path

The mass-storage firmware answers Generalplus vendor commands and has memory-read commands (FD28/FF28), but they sit behind an authorisation level the stock handshake never reaches (REPORTED, three independent analyses). Opening them needs a one-byte firmware patch, which needs a working updater or a clip, so this cannot bootstrap recovery. It could serve later as a USB backup path for cameras already running a patched firmware.

## 7. Where the sounds live and what a mute patch requires

**Storage (VERIFIED).** The five UI sounds are plain PCM WAV files inside the "GP+" resource pack at 0x120000. The pack is a directory of 43 entries (20-byte name without the dot, then a 32-bit sector number; data at 0x120000 + sector x 512), followed by the data. Each entry's slot runs to the next entry's sector, so a replacement must fit the slot.

| File | Use (VERIFIED from the event-player's id table) | Offset | Format | Duration | PCM bytes | Slot |
|---|---|---|---|---|---|---|
| BEEP.WAV | menu beep (id 5) | 0x121E00 | 11,025 Hz, 16-bit mono | 182 ms | 4,032 | 4,096 |
| CLICK.WAV | key click (id 0) | 0x123200 | 8,000 Hz, 8-bit mono | 235 ms | 1,882 | 2,048 |
| IMG.WAV | shutter (id 6) | 0x15FC00 | 11,025 Hz, 8-bit mono | 497 ms | 5,480 | 5,632 |
| POWEROFF_AUDIO.WAV | power off (id 3) | 0x162400 | 8,000 Hz, 8-bit mono | 1,052 ms | 8,421 | 8,704 |
| POWERON_AUDIO.WAV | power on (id 2) | 0x164600 | 8,000 Hz, 8-bit mono | 762 ms | 6,097 | 6,144 |

**Playback path (VERIFIED by disassembly).** At boot a function at VA 0x1E234 looks each name up in the pack and stores the five 16-bit handles in a table at RAM 0x110284. The event player at VA 0x1E844 takes a sound id, and before doing anything reads settings byte +0x19 through its getter (VA 0x1F560); if it is zero it returns without playing. Otherwise it passes that byte to the volume routine at VA 0x1E18C (levels 0 to 8, gain = level x 8 − 1), picks the handle, opens the resource and posts it to the audio task. Boot plays id 2 (power on) and the power-off path plays id 3; the shutter, keys and menu go through the same function. A separate GPIO 95 switch (VA 0x284A8) turns the speaker amplifier on around playback.

**The key-tone byte (VERIFIED).** Settings byte +0x19 is the UI-sound volume with 0 meaning off; factory default 5; both dumps hold 5 in every record. The two code sites that write it are generic SDK menu handlers, and the quick-start guide lists no sound setting, so the stock Charmera menu never changes it (REPORTED). A second byte, +0x05, is the general playback volume (also 0 to 8, default 5).

**Settings record format (VERIFIED).** 512-byte sectors from 0x170000: 252 bytes of settings, CRC32 over them in the next four bytes, and a zero second page; appended whenever a value changes; the current record is the one before the first erased sector. The record layout was recovered from the firmware's defaults routine (VA 0x1F69C) and its setters. One more rule for a settings-only mute image (VERIFIED): the photo directory sector at 0x17F000 must be zeroed, never 0xFF, when a valid settings record is present, because the directory loader treats 0xFF as two used entries of length 0xFFFFFFFF; a blank settings bank is fine because the defaults path rewrites the directory.

**Three ways to mute, cheapest first.**

1. Settings only: make the current settings record carry +0x19 = 0. No code, no resources. The toolkit rewrites the store so that record is the only one (sector 0, zero second page, 0xFF after it), which is exactly what the loader's "sector before the first erased sector" rule selects (VERIFIED). A stored level of 0 survives normal saves because the camera copies the RAM record forward and has no UI that sets this byte; it is lost only when the camera falls back to defaults (CRC failure, the 120-save collision in section 2, or a flashed image carrying a level-5 record). BEEP.WAV is never requested by this firmware (no caller asks for id 5), so the audible sounds are click, shutter, power on and power off.
2. Data only: overwrite the PCM of all five WAVs with silence (0x80 for 8-bit, 0x00 for 16-bit), keeping every header byte. Same change the Godox C100 kit shipped and tested on a camera. Survives any settings reset.
3. Code: make the event player return early or never enable the amplifier. Not needed and riskier.

Both data-only methods change only blocks 0x120000 to 0x16FFFF (method 2) or block 0x170000 (method 1); `verify` shows exactly that. Replacing a sound instead of silencing it must keep the slot's rate, bit depth and PCM length (pad or truncate) so no header or directory changes; the toolkit's `sounds replace` does the conversion. Whether the player honours a different rate or bit depth in a slot is UNKNOWN, so the toolkit does not try.

Nothing here has been flashed yet. The first mute test on a camera waits for your verified full-flash backup.

## 8. Risks

1. **No boot-ROM fallback is known.** A failed SD update, or a bad image, means a clip write or a dead camera. Mitigation: verified backup first; only block-aligned 0x1E0000 files; full battery; never a 2 MiB file.
2. **Edition confusion.** The original-series image boots on a Millennium or CLOT body but capture fails. The image carries no edition marker the tool can read from the camera over USB; identification is up to the owner (boot screen colour, filters). Mitigation: `verify` refuses unknown builds, the docs say "yellow boot screen only", and the mods are built from the owner's own backup, never a public dump.
3. **Programmer voltage.** A black CH341A at 5 V can damage the flash and the SoC. Mitigation: documented, and the backup tool prints the warning before every run.
4. **Back-powering the SoC through the flash VCC pin.** Causes unstable reads and could stress the board. Mitigation: battery disconnected, double read with compare, stop if the chip is not detected cleanly.
5. **A public restore image is not a factory image.** It carries another owner's settings records and two photos; a blanked variant is cleaner but its behaviour on a blank settings store is only emulation-verified.
6. **Resource edits that change sizes.** Any sound or frame replacement that outgrows its slot shifts nothing by itself (sectors are fixed), but anything beyond the slot overwrites the next resource. Mitigation: `sounds replace` keeps the stock PCM length; frame work (later) must respect the slot budgets from the directory.
7. **Code patches.** keychain-camera-plus hit a watchdog reboot loop with a one-word patch at 0x006D10 and had to revert; the zero block at 0x110000 that looks like free space is the heap; two helper functions live inside the "dead" Wi-Fi stack. Mitigation: Phase 3 starts with data-only mods; code patches only after the backup path is proven on your camera.
8. **The "final erase" of the updater.** VERIFIED: one extra 64 KiB block at (file length rounded to 256) + 0x10000. 0x1F0000 for a 0x1E0000 file (empty, harmless); 0x170000 for a resource-sized package (settings reset); 0x180000 for a 0x170000 file (corrupts internal photo 0); beyond the chip for 2 MiB. The toolkit only ever builds 0x1E0000 and `verify` prints where the extra erase lands.
9. **Stock settings-bank bug.** After about 120 saves the photo-directory sector is misread as a settings record and the camera resets to defaults and blanks the directory (INFERRED from the verified algorithm). Not caused by the toolkit, but a settings-only mute would be undone by it; the data-only PCM mute would not.

## 9. Open questions

- The SPII header fields beyond the magic and the register list: the 0x1B0000 word, the 0x3000/0x0200 halves, `SRIy`, whether the ROM checks anything. Needs the ROM or a load experiment; the dedicated analysis was refused by the model's safety layer and was not retried.
- Settled: the settings loader takes the sector before the first erased one, so a single-record store is honoured (section 2).
- Settled: the updater programs to the file length in 256-byte pages and erases one extra block at length + 64 KiB (section 4).
- Sensor: this build drives one hard-coded driver through a bit-banged two-wire bus (GPIO 0x11 clock, GPIO 0x12 data, write address 0x78), 61 register writes with SuperPix-style page selects, never reads a chip ID and ignores write failures, which is consistent with (not proof of) the cross-edition reports. The SP140A name remains community inference. The toolkit fingerprints the 61-entry table (SHA-256 aea3976b...) and the code region (297506be...) so a future Millennium or CLOT dump can be told apart.
- Boot time and power: section 11.
- Whether the 20 ms sleep between the two sensor tables and the single 1280x720 mode leave room for a faster preview start: not measurable statically.
- Whether the mask ROM of the GPCV2247F has a USB service mode and what the ISP header carries. Needs the datasheet or a probe on hardware; no safe way to test it here.
- The CLOT edition's existence beyond one issue comment; the Millennium's actual sensor.
- Whether the event player honours a WAV whose rate or bit depth differs from the stock slot (replacement sounds are converted to the stock format to avoid finding out the hard way).

## 10. What this means for the Phase 2 tools

Already built in `charmera-toolkit` (read-only unless a gate flag is given): `inspect`, `verify`, `pack`, `sounds list/extract/mute/replace`; `card-check`/`card-fix`, `exif-fix` and `backup`/`restore` are being finished. Design decisions that follow from this report:

- Series detection is by build stamps plus the software-region hash; anything else is "unknown" and refused. When a Millennium or CLOT dump appears, it gets its own fingerprint entry.
- Update files are always 0x1E0000 bytes, never 2 MiB, never odd-sized; the stamp must differ from 202505201029; the name is derived from the byte sum, never typed.
- Mods are data-only and are verified by diffing 64 KiB blocks against the owner's stock backup, so `verify` can state exactly which blocks a mod touches.
- Nothing produces a flashable file without an explicit flag, and every such file comes with a manifest and a SHA-256.
- No firmware is distributed; the toolkit works on the owner's backup and references the public dumps by hash only.

## 11. Quick start and power saving (added at your request)

All offsets are file offsets; VA = offset − 0x200. VERIFIED by disassembly unless marked. Detailed notes with excerpts: `work/bootpower.md` in the research scratch directory (copied into the toolkit docs).

**What the firmware is.** µC/OS-II, 100 Hz OS tick (OSTimeDly at VA 0x904E4, OSTimeDlyHMSM converts ms/10). `main` at 0x2DD48 sets up an 8 MiB heap and a 100 Hz timer, then a root task spawns seven tasks: state machine (prio 45), peripheral/keys/ADC (48), AP task doing SD housekeeping every second (52), display (7), audio (17), USB (50), file service (1). A software-timer service on hardware timer 2 ticks at 1/128 s (INFERRED, strongly: 7680 ticks per minute in the loop-record timer, and the idle maths divides by 128 to get seconds).

**Boot sequence** (state_startup at 0x65B0):

1. Boot-time ADC low-battery check (0x6104).
2. Wait for the display task to report the LCD initialised: polls a flag every 50 ms, up to 50 times (at most 2.5 s; ends as soon as the LCD is ready).
3. A fixed 100 ms delay (0x62B8). If the low-battery marker is set, show BATTERY00 for 1.5 s and power off. Otherwise the power key must still be held at OS tick 20 (200 ms after boot) or the camera powers off again (this is the "hold to turn on" behaviour).
4. Audio init; the power-on WAV is posted to the audio task and not waited for.
5. The power-on logo JPEG is decoded and shown (0x63D0 to 0x64E4; decode time UNKNOWN), then a fixed 50 ms delay (0x64F8); speaker amplifier GPIO 95 and GPIO 38 (an LED, INFERRED) are switched on.
6. Wait until the AP task reports SD detection/mount (three immediate mount retries, no long timeout) and the sound has started.
7. Key/ADC init with a 31 ms periodic timer; then state 0x10D, the home/mode screen (HOME_SMALL, redrawn every 0.75 s). It leaves for the photo state (0x1C644) only on message 0x7001. What posts 0x7001 at boot is UNKNOWN: nothing automatic was found, so you need to check whether live view appears without a key press.
8. Preview start (0x1C614 to 0x1BD80) has no OS delays in the first five call levels. Sensor init, LCD init and SDRAM init times cannot be read statically.

Fixed boot delays therefore total only about 150 ms plus whatever the hardware init takes; the rest is sensor/JPEG/SD work and, possibly, waiting on the home screen. No Wi-Fi and no USB waits exist at boot: the Wi-Fi init path (0xA20A4) is reachable only through a mode switch that every caller invokes with "off", and no Wi-Fi task is ever created.

**Auto power-off** (key scanner tail 0x76FC to 0x7780): idle seconds are counted from the 1/128 s timer and reset on any key press. Threshold = 60 × M seconds where M comes from settings byte +0x01: value 1 → 3 min, 2 → 5 min, n ≥ 3 → n min (0x7750/0x775C). Both dumps hold 2 (5 minutes); the quick-start guide says 3 minutes. Either the guide describes value 1, or timer 2 runs near 77 Hz rather than 128 Hz and every timing in this section scales by 0.6. One stopwatch measurement on the stock camera settles it. Auto-off is skipped while recording or on USB; on expiry it posts the power-off messages.

**A standby stage exists.** After 60 × (settings byte +0x51) seconds idle (both dumps: 2 → 2 minutes) the firmware switches the speaker amplifier off, stops audio and sets a standby flag; the next power-key press wakes instead of powering off. No LCD backlight write was found, so whether the screen blanks is UNKNOWN. There is no clock lowering and no idle hook was located; the CPU clock multiplier is never written by reachable code, so the running frequency is UNKNOWN statically (144 MHz is the weak favourite).

**Power-off** (0x23008): power-off WAV, 200 ms, logo shown again for 600 ms, ADC and timers off, waits up to 2 s for key release, then writes the PMU register 0xD0000028 through the unlock-key pair (the same pair the boot header programs) and loops.

**Battery**: ADC channel 1 sampled about once a second, five-sample average, thresholds 2190/2420/2600 counts for levels 0 to 3 with 5 counts of hysteresis (table at 0xF3864); level 0 warns, then powers off. Charging is in hardware. The divider ratio is UNKNOWN, so counts do not convert to volts without a measurement.

**Candidate changes, cheapest first** (none tested; all are single-byte or settings changes except the last two):

| Goal | Change | Risk |
|---|---|---|
| No power-on sound (also saves its ~0.8 s if anything waits on it) | settings +0x19 = 0 (the mute) | low |
| Shorter fixed delays | 0x62B8 `0a` → `02` (100 → 20 ms); 0x6360 `05` → `01` (poll 50 → 10 ms) | low; 0x64F8 `05` → `01` is medium because the frame buffer is freed right after |
| Skip the logo | rename the resource at 0x6544 so the lookup fails | low to medium; untested failure path |
| Longer or shorter auto-off | settings +0x01 (append a record; CRC32 over 252 bytes) | low |
| Earlier standby | settings +0x51 | low |
| LED off | 0x6520 `01 10 a0 e3` → `00 10 a0 e3`, only once GPIO 38 is confirmed to be just the LED | medium |
| Boot straight into photo mode | 0x6714 literal 0x10D → 0x101 | medium to high; only if the home screen really waits for a key |

**Measure first, on the stock camera:** stopwatch from power press to logo, to home, to live view, with and without a card; whether live view needs a key press; the minimum power-key hold; the real auto-off time (5:00 or 3:00 calibrates every timer above); what visibly happens at 2:00 idle; idle current at live view and in standby with the LED covered, if you can measure it.
