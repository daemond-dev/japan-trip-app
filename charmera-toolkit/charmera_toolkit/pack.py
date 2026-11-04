"""`pack`: turn an image into an update file the camera will accept, with the owner's eyes on it first.

What the updater does with the file (VERIFIED from the SDK source and hardware reports): reads it fully
into RAM, sums every byte and compares with the filename, then erases 64 KiB blocks from offset 0 and
programs the file byte for byte. The published update size is 0x1E0000 bytes (30 blocks); the last two
blocks of flash are blank on every known camera, so trimming a 2 MiB dump to 0x1E0000 loses nothing.
Never feed the camera a full 2 MiB file (see verify).

Without the gate flag this command only prints what it would write.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from . import settings as settings_mod
from .checksum import FLASH_SIZE, UPDATE_FILE_SIZE, build_update_name
from .image import PHOTO_INDEX, SETTINGS_BASE, USER_AREA_END, detect_build, image_kind, stored_photos

GATE_FLAG = "--i-understand-this-file-flashes-a-camera"


def prepare_payload(image: bytes, blank_settings: bool = False, blank_photos: bool = False,
                    factory_settings: bool = False) -> bytes:
    """Trim to 0x1E0000 and optionally reset the camera-specific areas.

    factory_settings keeps only the first CRC-valid settings record (the factory record on every known
    dump) and zeroes the photo directory, which uses the firmware's normal load path rather than its
    defaults fallback. blank_settings erases the whole bank instead (defaults path, verified statically).
    """
    kind = image_kind(image)
    if kind == "dump":
        tail = image[USER_AREA_END:]
        if tail != b"\xff" * len(tail):
            raise ValueError("bytes 0x1E0000..0x200000 are not blank in this dump; refusing to trim them away")
        payload = bytearray(image[:UPDATE_FILE_SIZE])
    elif kind == "update":
        payload = bytearray(image)
    elif kind == "software-only":
        payload = bytearray(image) + b"\xff" * (UPDATE_FILE_SIZE - len(image))
        blank_settings = True
        blank_photos = True
    else:
        raise ValueError(f"unsupported input size {len(image)}; expected a 2 MiB dump or a 0x1E0000 update file")
    if factory_settings:
        recs = [r for r in settings_mod.parse_settings(bytes(payload)) if r.crc_ok]
        if not recs:
            raise ValueError("no CRC-valid settings record to keep as the factory record")
        payload = bytearray(settings_mod.rewrite_store(bytes(payload), recs[0]))
    if blank_settings:
        payload[SETTINGS_BASE: settings_mod.SETTINGS_END] = b"\xff" * (settings_mod.SETTINGS_END - SETTINGS_BASE)
    if blank_photos:
        # The firmware's own "delete all" leaves a zeroed directory sector; an 0xFF directory would decode
        # as two used entries of length 0xFFFFFFFF, so write zeros there and erase only the photo data.
        payload[PHOTO_INDEX: PHOTO_INDEX + settings_mod.PHOTO_DIRECTORY_LEN] = b"\x00" * settings_mod.PHOTO_DIRECTORY_LEN
        payload[PHOTO_INDEX + settings_mod.PHOTO_DIRECTORY_LEN: UPDATE_FILE_SIZE] = b"\xff" * (UPDATE_FILE_SIZE - PHOTO_INDEX - settings_mod.PHOTO_DIRECTORY_LEN)
    if not settings_mod.photo_directory_consistent(bytes(payload)):
        payload[PHOTO_INDEX: PHOTO_INDEX + settings_mod.PHOTO_DIRECTORY_LEN] = b"\x00" * settings_mod.PHOTO_DIRECTORY_LEN
    return bytes(payload)


def describe(payload: bytes, stamp: Optional[str]) -> str:
    name = build_update_name(payload, stamp)
    build = detect_build(payload)
    lines = [
        f"filename:        {name.filename}",
        f"size:            {len(payload)} bytes ({len(payload) // 0x10000} blocks of 64 KiB)",
        f"byte sum:        {name.checksum:08X}",
        f"sha256:          {hashlib.sha256(payload).hexdigest()}",
        f"series:          {build.series} ({build.build.label if build.build else 'unknown build'})",
        f"software region: {'identical to stock' if build.checks.get('software_sha256') else 'MODIFIED or unknown'}",
    ]
    recs = settings_mod.parse_settings(payload)
    valid = [r for r in recs if r.crc_ok]
    if valid:
        lines.append(f"settings:        {len(recs)} records; current UI sound {valid[-1].ui_sound}, playback volume {valid[-1].playback_volume}")
    else:
        lines.append("settings:        blank (camera will create defaults)")
    photos = stored_photos(payload)
    lines.append(f"stored photos:   {len(photos)}")
    return "\n".join(lines)


def handler(args) -> int:
    src = Path(args.input)
    if not src.is_file():
        print(f"error: {src} is not a file")
        return 2
    image = src.read_bytes()
    try:
        payload = prepare_payload(image, args.blank_settings, args.blank_photos, args.factory_settings)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    build = detect_build(payload)
    if build.build is None and not args.allow_unknown_build:
        print("error: this image carries no known build stamps, so the toolkit cannot tell which camera series it is for.")
        print("       Refusing to pack it. Use --allow-unknown-build only for a dump taken from the camera it will go back into.")
        return 2
    try:
        name = build_update_name(payload, args.stamp)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print(describe(payload, args.stamp))
    print()
    if not args.gate:
        print(f"dry run: nothing written. Re-run with {GATE_FLAG} to write the file into --out.")
        return 0
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / name.filename
    out.write_bytes(payload)
    (out_dir / (name.filename + ".sha256")).write_text(f"{hashlib.sha256(payload).hexdigest()}  {name.filename}\n")
    (out_dir / (name.filename + ".MANIFEST.txt")).write_text(describe(payload, args.stamp) + "\n")
    print(f"wrote {out}")
    print("Before it touches a card: run `charmera verify` on it, and make sure you have a verified full-flash backup.")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("pack", help="build an update file from a dump or image (prints a plan unless gated)")
    p.add_argument("--input", required=True, help="2 MiB dump, 0x1E0000 update file, or software-only image")
    p.add_argument("--out", default="out", help="output directory")
    p.add_argument("--stamp", help="12-digit build stamp YYYYMMDDhhmm (default: now, UTC); must differ from the stock stamp")
    p.add_argument("--factory-settings", action="store_true", help="keep only the first (factory) settings record and zero the photo directory")
    p.add_argument("--blank-settings", action="store_true", help="erase the settings store so the camera recreates defaults (verified in code, not on hardware)")
    p.add_argument("--blank-photos", action="store_true", help="erase the internally stored photos and their index")
    p.add_argument("--allow-unknown-build", action="store_true")
    p.add_argument(GATE_FLAG, dest="gate", action="store_true", help="actually write the update file")
    p.set_defaults(func=handler)
