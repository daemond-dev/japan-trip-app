"""`sounds`: list, extract, mute or replace the five UI sounds.

Two independent ways to mute (both VERIFIED in the firmware, neither yet tested on hardware):

- settings: the event-sound player returns before playing when settings byte +0x19 is 0. The toolkit
  rewrites the settings store so that its only valid record is the current one with +0x19 = 0. No code or
  resource changes.
- pcm: silence the PCM data of all five WAV resources in place (0x80 for 8-bit, 0x00 for 16-bit), keeping
  every header byte. The Godox C100 kit, built on the same SDK, shipped exactly this and reported it working.

`replace` writes a new clip into a slot, converted to the slot's own sample rate, bit depth and mono
layout, truncated or padded with silence to the stock data length so no header changes.

Writing a modified image is a mod step: the output is a full image, not an update file. Run `pack` on
it afterwards and read what `verify` says.
"""
from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import Dict, List, Optional

from . import settings as settings_mod
from .image import SOUND_SLOTS, WavSlot, sound_slots

GATE_FLAG = "--i-understand-this-is-a-mod"


# --- Read side ------------------------------------------------------------------------------------------


def list_sounds(image: bytes) -> List[WavSlot]:
    return sound_slots(image)


def extract(image: bytes, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for s in sound_slots(image):
        p = out_dir / (s.entry.display_name.lower())
        with wave.open(str(p), "wb") as w:
            w.setnchannels(s.channels)
            w.setsampwidth(s.bits // 8)
            w.setframerate(s.rate)
            w.writeframes(bytes(image[s.data_offset: s.data_offset + s.data_len]))
        written.append(p)
    return written


# --- Mute -----------------------------------------------------------------------------------------------


def mute_pcm(image: bytes, names: Optional[List[str]] = None) -> bytes:
    """Silence the PCM of the named slots (directory names, default all five). Headers untouched."""
    buf = bytearray(image)
    for s in sound_slots(image):
        if names and s.entry.name not in names:
            continue
        buf[s.data_offset: s.data_offset + s.data_len] = bytes([s.silence_byte]) * s.data_len
    return bytes(buf)


def set_ui_sound(image: bytes, level: int, base: str = "current") -> bytes:
    """Rewrite the settings store so one record with UI sound = level is the only record.

    base = "current": start from the record the camera would load (keeps that camera's choices).
    base = "first": start from the first record in the bank, which on every known dump is the factory
    default record (identical in both public dumps). Use this when the dump is not your own camera's.
    The rest of the bank is erased so the firmware cannot pick an older record, and the photo directory
    is zeroed if it was erased.
    """
    if not 0 <= level <= 8:
        raise ValueError("level must be 0..8")
    if len(image) < settings_mod.BANK_END:
        raise ValueError("image does not contain the settings store")
    if base == "first":
        recs = [r for r in settings_mod.parse_settings(image) if r.crc_ok]
        if not recs:
            raise ValueError("no CRC-valid settings record in the image")
        src = recs[0]
    elif base == "current":
        src = settings_mod.current_record(image)
    else:
        raise ValueError("base must be 'current' or 'first'")
    return settings_mod.rewrite_store(image, src.with_byte(settings_mod.OFF_UI_SOUND, level))


# --- Replace --------------------------------------------------------------------------------------------


def _read_pcm(path: Path):
    """Return (channels, sampwidth, rate, frames) from a PCM WAV. Raises ValueError for anything else."""
    with wave.open(str(path), "rb") as w:
        if w.getcomptype() != "NONE":
            raise ValueError(f"{path}: only uncompressed PCM WAV is supported")
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())


def _to_s16_mono(frames: bytes, channels: int, width: int) -> List[int]:
    n = len(frames) // (channels * width)
    out = []
    for i in range(n):
        acc = 0
        for c in range(channels):
            o = (i * channels + c) * width
            if width == 1:
                v = (frames[o] - 128) << 8
            elif width == 2:
                v = struct.unpack_from("<h", frames, o)[0]
            elif width == 3:
                v = int.from_bytes(frames[o: o + 3], "little", signed=True) >> 8
            elif width == 4:
                v = struct.unpack_from("<i", frames, o)[0] >> 16
            else:
                raise ValueError("unsupported sample width")
            acc += v
        out.append(acc // channels)
    return out


def _resample(samples: List[int], src: int, dst: int) -> List[int]:
    if src == dst or not samples:
        return samples
    n = max(1, int(len(samples) * dst / src))
    out = []
    for i in range(n):
        x = i * src / dst
        j = int(x)
        f = x - j
        a = samples[min(j, len(samples) - 1)]
        b = samples[min(j + 1, len(samples) - 1)]
        out.append(int(a + (b - a) * f))
    return out


def convert_for_slot(path: Path, slot: WavSlot, gain: float = 1.0) -> bytes:
    """PCM bytes in the slot's own format, exactly slot.data_len long."""
    ch, width, rate, frames = _read_pcm(path)
    s16 = _resample(_to_s16_mono(frames, ch, width), rate, slot.rate)
    if gain != 1.0:
        s16 = [max(-32768, min(32767, int(v * gain))) for v in s16]
    if slot.bits == 16:
        pcm = b"".join(struct.pack("<h", v) for v in s16)
    elif slot.bits == 8:
        pcm = bytes(((v + 32768) >> 8) & 0xFF for v in s16)
    else:
        raise ValueError("slot has an unsupported bit depth")
    if len(pcm) >= slot.data_len:
        return pcm[: slot.data_len]
    return pcm + bytes([slot.silence_byte]) * (slot.data_len - len(pcm))


def replace_sound(image: bytes, slot_name: str, wav_path: Path, gain: float = 1.0) -> bytes:
    name = slot_name.upper().replace(".", "")
    for s in sound_slots(image):
        if s.entry.name == name:
            pcm = convert_for_slot(wav_path, s, gain)
            buf = bytearray(image)
            buf[s.data_offset: s.data_offset + s.data_len] = pcm
            return bytes(buf)
    raise ValueError(f"no sound slot named {slot_name}; slots are {', '.join(n for n in SOUND_SLOTS)}")


# --- CLI ------------------------------------------------------------------------------------------------


def _print_slots(image: bytes) -> None:
    for s in sound_slots(image):
        print(f"{s.entry.display_name:20s} {SOUND_SLOTS[s.entry.name][0]:10s} at {s.entry.offset:#8x}  {s.rate} Hz "
              f"{s.bits}-bit {s.channels}ch  {s.duration_ms} ms  data {s.data_len} B  budget {s.entry.budget} B")


def _write_mod(args, image: bytes, out: Path, what: str) -> int:
    if not getattr(args, "gate", False):
        print(f"dry run: would write {out} ({len(image)} bytes) with {what}.")
        print(f"Re-run with {GATE_FLAG} to write it. The result is an image, not an update file; pack and verify it next.")
        return 0
    out.write_bytes(image)
    print(f"wrote {out} ({len(image)} bytes): {what}")
    print("next: charmera pack --input <this file> ... then charmera verify <the update file>")
    return 0


def handler(args) -> int:
    src = Path(args.file)
    if not src.is_file():
        print(f"error: {src} is not a file")
        return 2
    image = src.read_bytes()
    if args.action == "list":
        _print_slots(image)
        return 0
    if args.action == "extract":
        for p in extract(image, Path(args.out)):
            print(f"wrote {p}")
        return 0
    out = Path(args.out)
    if args.action == "mute":
        what = []
        if args.method in ("settings", "both"):
            image = set_ui_sound(image, 0, args.base_record)
            what.append(f"settings byte +0x19 = 0 (UI sounds off), from the {args.base_record} record")
        if args.method in ("pcm", "both"):
            image = mute_pcm(image)
            what.append("all five WAV slots silenced")
        return _write_mod(args, image, out, "; ".join(what))
    if args.action == "replace":
        image = replace_sound(image, args.slot, Path(args.wav), args.gain)
        return _write_mod(args, image, out, f"{args.slot} replaced by {args.wav}")
    return 2


def register(subparsers) -> None:
    p = subparsers.add_parser("sounds", help="list, extract, mute or replace the UI sounds")
    p.add_argument("action", choices=["list", "extract", "mute", "replace"])
    p.add_argument("file", help="a 2 MiB dump or a 0x1E0000 update file")
    p.add_argument("--out", help="output directory (extract) or output image path (mute/replace)")
    p.add_argument("--method", choices=["settings", "pcm", "both"], default="settings",
                   help="mute: settings byte only (default), silence the PCM, or both")
    p.add_argument("--base-record", choices=["current", "first"], default="current",
                   help="mute: which settings record to derive the muted record from (first = factory record)")
    p.add_argument("--slot", help="replace: CLICK.WAV, POWERON_AUDIO.WAV, POWEROFF_AUDIO.WAV, BEEP.WAV or IMG.WAV")
    p.add_argument("--wav", help="replace: a PCM WAV file")
    p.add_argument("--gain", type=float, default=1.0, help="replace: linear gain applied before conversion")
    p.add_argument(GATE_FLAG, dest="gate", action="store_true", help="actually write the modified image")
    p.set_defaults(func=handler)
