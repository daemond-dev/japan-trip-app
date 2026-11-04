"""`verify`: decide whether an update file is safe to put on a card. Read-only.

A file passes only if every check below passes. The checks encode what is known about the camera's
updater (VERIFIED: filename-driven byte sum, no other validation; erases and programs whole 64 KiB blocks
from offset 0) and about the build (the original 2025 series). Anything unknown fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import settings as settings_mod
from .checksum import BLOCK_SIZE, STOCK_TIMESTAMP, UPDATE_FILE_SIZE, byte_sum, parse_update_name
from .image import (ORIGINAL_2025, SETTINGS_BASE, SOFTWARE_END, SPII_MAGIC, describe_erase_target, detect_build,
                    resource_directory, sound_slots, stored_photos, updater_extra_erase)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "fail"   # fail | warn | info


def verify_update(path: Path, image: bytes, stock: Optional[bytes] = None, allow_modified: bool = False,
                  allow_unknown_build: bool = False, allow_odd_size: bool = False) -> List[Check]:
    checks: List[Check] = []

    # 1. Filename.
    try:
        name = parse_update_name(path.name)
        checks.append(Check("filename", True, f"JH_{name.product} stamp {name.stamp} checksum {name.checksum:08X}"))
        s = byte_sum(image)
        checks.append(Check("checksum", s == name.checksum,
                            f"file byte sum {s:08X} {'==' if s == name.checksum else '!='} filename {name.checksum:08X}"
                            + ("" if s == name.checksum else " (the camera would show 'Program error' and not erase)")))
        checks.append(Check("stamp", name.stamp != STOCK_TIMESTAMP,
                            f"stamp {name.stamp} {'differs from' if name.stamp != STOCK_TIMESTAMP else 'EQUALS'} the stock stamp {STOCK_TIMESTAMP}"
                            + ("" if name.stamp != STOCK_TIMESTAMP else " (the updater would ignore the file)")))
        checks.append(Check("stamp-date", name.stamp_datetime() is not None, "stamp is a real date" if name.stamp_datetime() else "stamp is not a valid YYYYMMDDhhmm", "warn"))
    except ValueError as exc:
        checks.append(Check("filename", False, str(exc)))

    # 2. Size and structure.
    n = len(image)
    if n == UPDATE_FILE_SIZE:
        checks.append(Check("size", True, f"{n} bytes = 30 blocks of 64 KiB (the proven update size)"))
    elif n % 0x100 == 0 and SOFTWARE_END <= n <= 0x200000:
        checks.append(Check("size", True, f"{n} bytes, whole 256-byte pages" + ("" if n % BLOCK_SIZE == 0 else "; not a whole number of 64 KiB blocks (the remainder of the last block stays erased)"), "info"))
    else:
        checks.append(Check("size", allow_odd_size, f"{n} bytes is not a multiple of 256; the updater programs whole pages from its RAM buffer, "
                            "so the last page would receive stale bytes after the file's end"))
    extra = updater_extra_erase(n)
    what = describe_erase_target(extra)
    harmless = extra >= 0x1E0000 and extra < 0x200000
    checks.append(Check("final-erase", harmless or not (extra >= 0x200000), f"after programming, the updater erases the 64 KiB block at {extra:#x}: {what}",
                        "info" if harmless else ("fail" if extra >= 0x200000 else "warn")))
    if n == 0x200000:
        checks.append(Check("size-2mib", False, "a full 2 MiB file: never put one on a card. Trim to 0x1E0000 with pack."))
    checks.append(Check("magic", image[:4] == SPII_MAGIC, "starts with 'SPII'" if image[:4] == SPII_MAGIC else f"first bytes {image[:4]!r}, not 'SPII' (the camera does not check this, but it would not boot)"))

    # 3. Build identity.
    build = detect_build(image)
    if build.build is None:
        checks.append(Check("series", allow_unknown_build, "no known build stamps found: cannot tell which camera series this is for"
                            + ("" if allow_unknown_build else " (refusing; use --allow-unknown-build only with your own dump of that camera)")))
    else:
        checks.append(Check("series", True, f"{build.build.label}"))
        exact = build.checks.get("software_sha256", False)
        if exact:
            checks.append(Check("software-region", True, "bytes 0..0x170000 are byte-identical to the stock original-series software"))
        else:
            detail = "bytes 0..0x170000 differ from stock"
            if stock is not None and len(stock) >= SOFTWARE_END:
                blocks = [i for i in range(0, SOFTWARE_END, BLOCK_SIZE) if image[i:i + BLOCK_SIZE] != stock[i:i + BLOCK_SIZE]]
                detail += f" in 64 KiB blocks {[hex(b) for b in blocks]}"
                in_res = [b for b in blocks if b >= 0x120000]
                in_code = [b for b in blocks if b < 0x120000]
                detail += f" (code/table blocks: {len(in_code)}, resource blocks: {len(in_res)})"
            checks.append(Check("software-region", allow_modified, detail + ("" if allow_modified else "; pass --allow-modified if this is your intended mod")))

    # 4. What the file carries into the camera's own area.
    if n > SETTINGS_BASE:
        recs = settings_mod.parse_settings(image)
        valid = [r for r in recs if r.crc_ok]
        if not recs:
            checks.append(Check("settings", True, "settings store blank: the camera will create defaults (INFERRED from emulation, not hardware-tested)", "info"))
        else:
            try:
                cur = settings_mod.current_record(image)
                checks.append(Check("settings", True, f"{len(recs)} settings records ({len(valid)} valid); the camera will load the one at {cur.offset:#x}: UI sound {cur.ui_sound}, playback volume {cur.playback_volume}, auto-off code {cur.raw[settings_mod.OFF_AUTO_OFF]}", "info"))
            except ValueError as exc:
                checks.append(Check("settings", True, f"{len(recs)} settings records but {exc}", "warn"))
        checks.append(Check("photo-directory", settings_mod.photo_directory_consistent(image),
                            "photo directory sector is consistent with the settings state" if settings_mod.photo_directory_consistent(image)
                            else "a valid settings record exists but the photo directory sector at 0x17F000 is 0xFF; the camera would read two bogus photo entries. Zero that sector (pack does this)."))
        photos = stored_photos(image)
        checks.append(Check("photos", True, f"{len(photos)} photo(s) stored in the image" + (" (someone else's if this is not your own dump)" if photos else ""), "info" if not photos else "warn"))

    # 5. Resource pack sanity (only when present).
    if n > 0x120000:
        res = resource_directory(image)
        checks.append(Check("resources", len(res) == 43, f"{len(res)} resource entries" + ("" if len(res) == 43 else " (stock has 43)"), "fail" if len(res) == 0 else ("warn" if len(res) != 43 else "info")))
        try:
            slots = sound_slots(image)
            bad = [s.entry.display_name for s in slots if s.total_len > (s.entry.budget or 0)]
            checks.append(Check("sounds", not bad, f"{len(slots)} WAV slots parse" + (f"; oversized: {bad}" if bad else "")))
        except ValueError as exc:
            checks.append(Check("sounds", False, str(exc)))
    return checks


def handler(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: {path} is not a file")
        return 2
    image = path.read_bytes()
    stock = Path(args.stock).read_bytes() if args.stock else None
    checks = verify_update(path, image, stock, args.allow_modified, args.allow_unknown_build, args.allow_odd_size)
    failed = False
    for c in checks:
        tag = "PASS" if c.ok else ("WARN" if c.severity == "warn" else "FAIL")
        if c.severity == "info" and c.ok:
            tag = "INFO"
        if not c.ok and c.severity == "fail":
            failed = True
        print(f"{tag:4s} {c.name:16s} {c.detail}")
    print()
    if failed:
        print("RESULT: do not put this file on a card.")
        return 1
    print("RESULT: no blocking problem found. This is a static check; it cannot see which camera you will put it in.")
    print("        Only use it on the series it was built for, with a verified backup of your own flash.")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("verify", help="check an update file's name, checksum, size and build before it goes near a card")
    p.add_argument("file")
    p.add_argument("--stock", help="your own stock dump, to show which blocks a modified file changes")
    p.add_argument("--allow-modified", action="store_true", help="accept a file whose software region differs from stock")
    p.add_argument("--allow-unknown-build", action="store_true", help="accept a file without known build stamps")
    p.add_argument("--allow-odd-size", action="store_true", help="accept a size that is not a multiple of 64 KiB")
    p.set_defaults(func=handler)
