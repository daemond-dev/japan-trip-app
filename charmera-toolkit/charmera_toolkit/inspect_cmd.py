"""`inspect`: print what is in a dump or update file. Read-only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import settings as settings_mod
from .checksum import parse_update_name
from .image import SOUND_SLOTS, summarize


def _fmt_bool(v) -> str:
    return "yes" if v else ("no" if v is False else "n/a")


def print_report(path: Path, image: bytes, show_resources: bool = False, show_settings: bool = False) -> int:
    info = summarize(image)
    print(f"file:            {path}")
    print(f"size:            {info['size']} bytes ({info['size'] / 1024:.0f} KiB), kind: {info['kind']}")
    print(f"sha256:          {info['sha256']}")
    print(f"byte sum:        {info['byte_sum']:08X}")
    if info["public_dump"]:
        print(f"identity:        matches a public dump: {info['public_dump']}")
    print(f"magic 'SPII':    {_fmt_bool(info['magic_ok'])}")
    print(f"header words:    {' '.join(f'{w:08x}' for w in info['header_words'][:18])}")
    if info["reg_init"]:
        print("reg init list:   " + ", ".join(f"[{r:#010x}]={v:#x}" for r, v in info["reg_init"]) + "  (INFERRED meaning)")
    print(f"series:          {info['series']}  ({info['series_label']})")
    for k, v in info["series_checks"].items():
        print(f"  check {k:16s} {_fmt_bool(v)}")
    print(f"embedded stamp:  {info['embedded_stamp']}")
    if info["software_sha256"]:
        print(f"software sha256: {info['software_sha256']}  (bytes 0..0x170000)")

    # Filename check when it looks like an update file.
    try:
        name = parse_update_name(path.name)
        ok = name.checksum == info["byte_sum"]
        print(f"update name:     stamp {name.stamp}, checksum {name.checksum:08X} -> {'matches file' if ok else 'DOES NOT MATCH file'}")
    except ValueError:
        pass

    res = info["resources"]
    print(f"resources:       {len(res)} entries in the GP+ pack" + (f", data ends at {info['resources_end']:#x}" if info["resources_end"] else ""))
    if show_resources:
        for e in res:
            print(f"  {e.index:2d} {e.display_name:20s} at {e.offset:#8x}  budget {e.budget}")
    if info["sounds"]:
        print("sounds:")
        for s in info["sounds"]:
            what = SOUND_SLOTS[s.entry.name][0]
            print(f"  {s.entry.display_name:20s} {what:10s} at {s.entry.offset:#8x}  {s.rate} Hz {s.bits}-bit "
                  f"{s.channels}ch  {s.duration_ms} ms  data {s.data_len} B  slot budget {s.entry.budget} B")

    recs = info["settings_records"]
    if recs:
        cur = info["current_settings"]
        print(f"settings:        {len(recs)} records, {info['settings_valid']} CRC-valid; "
              f"current at {cur.offset:#x}: UI sound {cur.ui_sound} ({'OFF' if cur.ui_sound == 0 else 'on'}), "
              f"playback volume {cur.playback_volume}" if cur else f"settings:        {len(recs)} records, none valid")
        if show_settings and cur:
            for off, name in settings_mod.FIELD_NAMES.items():
                print(f"  +{off:#04x} = {cur.raw[off]:3d}  {name}")
    elif info["size"] > settings_mod.SETTINGS_BASE:
        print("settings:        store is blank (0xFF); the camera will create defaults")

    if info["photo_index"] or info["stored_photos"]:
        print(f"stored photos:   index {[(p.slot, p.used, p.size) for p in info['photo_index']]}, "
              f"JPEG data {[(hex(o), n) for o, n in info['stored_photos']]}")
        print("                 (these belong to whoever owned the camera that was dumped)")
    if info["tail_blank"] is not None:
        print(f"tail 0x1E0000..: {'all 0xFF' if info['tail_blank'] else 'NOT blank'}")
    return 0


def handler(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: {path} is not a file")
        return 2
    image = path.read_bytes()
    if args.json:
        info = summarize(image)
        out = {k: v for k, v in info.items() if k not in ("resources", "sounds", "settings_records", "current_settings", "photo_index")}
        out["resources"] = [{"name": e.display_name, "offset": e.offset, "budget": e.budget} for e in info["resources"]]
        out["sounds"] = [{"name": s.entry.display_name, "offset": s.entry.offset, "rate": s.rate, "bits": s.bits,
                          "channels": s.channels, "data_len": s.data_len, "duration_ms": s.duration_ms} for s in info["sounds"]]
        cur = info["current_settings"]
        out["current_settings"] = {"offset": cur.offset, "ui_sound": cur.ui_sound, "playback_volume": cur.playback_volume} if cur else None
        out["photo_index"] = [vars(p) for p in info["photo_index"]]
        print(json.dumps(out, indent=2, default=str))
        return 0
    return print_report(path, image, args.resources, args.settings)


def register(subparsers) -> None:
    p = subparsers.add_parser("inspect", help="parse a dump or update file and print what it contains (read-only)")
    p.add_argument("file")
    p.add_argument("--resources", action="store_true", help="list every resource-pack entry")
    p.add_argument("--settings", action="store_true", help="print the known settings fields of the current record")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=handler)


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(handler(type("A", (), {"file": sys.argv[1], "resources": True, "settings": True, "json": False})))
