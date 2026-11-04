"""Reading a Charmera flash image: header, flash map, build identity, resources, sounds, settings, photos.

Everything here is read-only and works on a full 2 MiB dump, a 0x1E0000-byte update file, or a
shorter software-only image (the layout of the first 0x170000 bytes is the same in all three).

Status labels in comments: VERIFIED = checked against both public dumps and the firmware code by the
research pass; INFERRED = consistent with the evidence but not proven on hardware; UNKNOWN = guess.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import settings as settings_mod
from .checksum import FLASH_SIZE, UPDATE_FILE_SIZE, byte_sum

# --- Fixed layout (file offsets) ----------------------------------------------------------------------
HEADER_SIZE = 0x200          # VERIFIED: 'SPII' header, ARM vector table starts at 0x200
VA_OFFSET = 0x200            # VERIFIED: file offset = runtime address + 0x200
CODE_END = 0x110000          # INFERRED: code and tables; the 64 KiB at 0x110000 is zero (heap placeholder)
RESOURCE_BASE = 0x120000     # VERIFIED: 'GP+' resource pack
RESOURCE_SECTOR = 0x200      # VERIFIED: directory entries address 512-byte sectors from RESOURCE_BASE
SOFTWARE_END = 0x170000      # VERIFIED: everything below is identical in both public dumps
SETTINGS_BASE = settings_mod.SETTINGS_BASE
PHOTO_INDEX = 0x17F000       # VERIFIED: 8-byte entries describing the internally stored photos
PHOTO_SLOTS = (0x180000, 0x1A5800)   # VERIFIED in both dumps (two slots, 0x25800 apart)
PHOTO_SLOT_SIZE = 0x25800
USER_AREA_END = 0x1E0000     # VERIFIED: 0x1E0000..0x200000 is 0xFF in every known dump

SPII_MAGIC = b"SPII"

# --- Known builds -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownBuild:
    key: str
    label: str
    stamp: str                 # the 12-digit stamp the updater compares against
    stamp_offset: int
    build_string: bytes
    build_offset: int
    compiled: bytes
    compiled_offset: int
    software_sha256: str       # sha256 of bytes 0..0x170000 (code, data and resources, unmodified)
    code_sha256: str           # sha256 of bytes 0x200..0x100000 (code and read-only data only)
    sensor_table: tuple        # (start, end) of the sensor init register table
    sensor_table_sha256: str   # sha256 of that table: the edition-sensitive part of the image


ORIGINAL_2025 = KnownBuild(
    key="original-2025",
    label="Original 2025 series (matte body, yellow Kodak boot screen), build JH5408 20250520",
    stamp="202505201029",
    stamp_offset=0x218C4,
    build_string=b"JH5408  20250520",
    build_offset=0x18964,
    compiled=b"Oct 14 2025",
    compiled_offset=0xF3978,
    software_sha256="fe88e8de57e7b1ff1a0856127ee09a21fb3ce2dde2684662c533719b80adc5c8",
    code_sha256="297506be352e2efb0e9231936197c0798f2633dd27a005ae64a0b3ad547d33f6",
    sensor_table=(0xF5C6C, 0xF5CE8),
    sensor_table_sha256="aea3976bdf7ba2e747d8b56141f7ef0b4e3262481540dc2869457bc814fcb755",
)

KNOWN_BUILDS = {ORIGINAL_2025.key: ORIGINAL_2025}

# Public full dumps, for reference only. Never redistributed by this toolkit.
PUBLIC_DUMPS: Dict[str, str] = {
    "cc3b4b2fd7eb71ba5a441decdc86984261fac8418ee3e105d7e2b3c38b7ea42f":
        "benbadio/keychain-camera-plus dumps/charmera_stock_latest.bin (original series)",
    "720ed0b65c91b149b0916498732b0ecf40227736ef5e3c126c487a7b5253436c":
        "Khimira/Project-Sirius Original_Firmware.bin (original series)",
}

# --- Header -------------------------------------------------------------------------------------------


@dataclass
class SpiiHeader:
    magic: bytes
    words: List[int]
    reg_init: List[tuple]      # INFERRED: (register, value) pairs at 0x28..0x48

    @property
    def magic_ok(self) -> bool:
        return self.magic == SPII_MAGIC


def parse_header(image: bytes) -> SpiiHeader:
    """Decode what is known of the 512-byte boot header. Field meanings beyond the magic are INFERRED."""
    words = [struct.unpack_from("<I", image, i)[0] for i in range(0, 0x50, 4)]
    pairs = []
    for i in range(0x28, 0x48, 8):
        reg, val = struct.unpack_from("<II", image, i)
        if reg:
            pairs.append((reg, val))
    return SpiiHeader(magic=bytes(image[:4]), words=words, reg_init=pairs)


# --- Build identity -----------------------------------------------------------------------------------


@dataclass
class BuildMatch:
    build: Optional[KnownBuild]
    checks: Dict[str, bool] = field(default_factory=dict)

    @property
    def series(self) -> str:
        return self.build.key if self.build else "unknown"

    @property
    def exact(self) -> bool:
        return self.build is not None and all(self.checks.values())


def software_sha256(image: bytes) -> str:
    return hashlib.sha256(image[:SOFTWARE_END]).hexdigest()


def detect_build(image: bytes) -> BuildMatch:
    """Identify the firmware build from its stamps and the software-region hash.

    A modified image (sounds replaced, patches applied) keeps its stamps but changes the hash; the
    caller decides whether that is acceptable. Anything whose stamps do not match is 'unknown'.
    """
    for build in KNOWN_BUILDS.values():
        checks = {
            "stamp": image[build.stamp_offset: build.stamp_offset + 12] == build.stamp.encode(),
            "build_string": image[build.build_offset: build.build_offset + len(build.build_string)] == build.build_string,
            "compiled": image[build.compiled_offset: build.compiled_offset + len(build.compiled)] == build.compiled,
        }
        if checks["stamp"] and checks["build_string"]:
            checks["code_sha256"] = len(image) >= 0x100000 and hashlib.sha256(image[0x200:0x100000]).hexdigest() == build.code_sha256
            s, e = build.sensor_table
            checks["sensor_table"] = len(image) >= e and hashlib.sha256(image[s:e]).hexdigest() == build.sensor_table_sha256
            checks["software_sha256"] = len(image) >= SOFTWARE_END and software_sha256(image) == build.software_sha256
            return BuildMatch(build, checks)
    return BuildMatch(None, {})


def updater_extra_erase(file_len: int) -> int:
    """Flash offset of the 64 KiB block the updater erases after programming a file of this length.

    VERIFIED from the updater: (length rounded up to 256) + 0x10000, masked to the block.
    """
    return (((file_len + 0xFF) & ~0xFF) + 0x10000) & ~0xFFFF


def describe_erase_target(off: int) -> str:
    if off >= FLASH_SIZE:
        return "beyond the 2 MiB chip (what the flash does with that address is unknown; a brick is reported)"
    if SETTINGS_BASE <= off < 0x180000:
        return "the settings bank: the camera will reset to defaults and blank its photo directory"
    if 0x180000 <= off < 0x1D0000:
        return "the internal photo area: a stored photo is partly erased while the directory still lists it"
    if off >= USER_AREA_END:
        return "a blank block (harmless)"
    return "inside the software or resource area"


def embedded_stamp(image: bytes, build: KnownBuild = ORIGINAL_2025) -> str:
    return image[build.stamp_offset: build.stamp_offset + 12].decode("ascii", "replace")


# --- Image kind ---------------------------------------------------------------------------------------


def image_kind(image: bytes) -> str:
    n = len(image)
    if n == FLASH_SIZE:
        return "dump"
    if n == UPDATE_FILE_SIZE:
        return "update"
    if n <= SOFTWARE_END and n >= RESOURCE_BASE:
        return "software-only"
    return "unknown"


def tail_blank(image: bytes, start: int = USER_AREA_END) -> Optional[bool]:
    if len(image) <= start:
        return None
    return image[start:] == b"\xff" * (len(image) - start)


# --- Resource pack ------------------------------------------------------------------------------------


@dataclass
class ResourceEntry:
    index: int
    name: str          # directory spelling, e.g. 'BEEPWAV' (8.3 name without the dot)
    sector: int
    offset: int
    budget: Optional[int]   # bytes until the next entry; None for the last entry

    @property
    def display_name(self) -> str:
        for ext in ("GPZP", "GPZ", "WAV", "JPG", "PAK"):
            if self.name.endswith(ext) and len(self.name) > len(ext):
                return f"{self.name[:-len(ext)]}.{ext}"
        return self.name


def find_resources_end(image: bytes) -> int:
    """Offset just past the last non-0xFF byte below the settings store (0x168E9A on the known build)."""
    i = min(len(image), SETTINGS_BASE) - 1
    while i > RESOURCE_BASE and image[i] == 0xFF:
        i -= 1
    return i + 1


def resource_directory(image: bytes) -> List[ResourceEntry]:
    if image[RESOURCE_BASE: RESOURCE_BASE + 4] != b"GP+\x00":
        return []
    entries = []
    off = RESOURCE_BASE + 0x18
    while off + 24 <= len(image):
        name = bytes(image[off: off + 20])
        sector = struct.unpack_from("<I", image, off + 20)[0]
        if name[0] in (0, 0xFF):
            break
        entries.append(ResourceEntry(len(entries), name.rstrip(b"\x00").decode("ascii", "replace"), sector,
                                     RESOURCE_BASE + sector * RESOURCE_SECTOR, None))
        off += 24
    end = find_resources_end(image)
    for i, e in enumerate(entries):
        nxt = entries[i + 1].offset if i + 1 < len(entries) else end
        e.budget = max(0, nxt - e.offset)
    return entries


# --- Sounds -------------------------------------------------------------------------------------------

SOUND_SLOTS = {
    # directory name: (what it is, sound id used by the event player)
    "CLICKWAV": ("key click", 0),
    "POWERON_AUDIOWAV": ("power on", 2),
    "POWEROFF_AUDIOWAV": ("power off", 3),
    "BEEPWAV": ("menu beep", 5),
    "IMGWAV": ("shutter", 6),
}


@dataclass
class WavSlot:
    entry: ResourceEntry
    riff_size: int
    fmt_tag: int
    channels: int
    rate: int
    bits: int
    data_offset: int     # absolute file offset of the PCM data
    data_len: int

    @property
    def total_len(self) -> int:
        return 8 + self.riff_size

    @property
    def duration_ms(self) -> int:
        frame = max(1, self.channels * (self.bits // 8))
        return int(1000 * (self.data_len // frame) / self.rate) if self.rate else 0

    @property
    def silence_byte(self) -> int:
        return 0x80 if self.bits == 8 else 0x00


def parse_wav_at(image: bytes, entry: ResourceEntry) -> WavSlot:
    off = entry.offset
    if image[off: off + 4] != b"RIFF" or image[off + 8: off + 12] != b"WAVE":
        raise ValueError(f"{entry.display_name} at {off:#x} is not a RIFF WAVE")
    riff_size = struct.unpack_from("<I", image, off + 4)[0]
    p = off + 12
    fmt = None
    data = None
    end = off + 8 + riff_size
    while p + 8 <= end:
        cid = bytes(image[p: p + 4])
        csz = struct.unpack_from("<I", image, p + 4)[0]
        if cid == b"fmt " and csz >= 16:
            fmt = struct.unpack_from("<HHIIHH", image, p + 8)
        elif cid == b"data":
            data = (p + 8, csz)
            break
        p += 8 + csz + (csz & 1)
    if fmt is None or data is None:
        raise ValueError(f"{entry.display_name}: missing fmt or data chunk")
    tag, ch, rate, _brate, _align, bits = fmt
    return WavSlot(entry, riff_size, tag, ch, rate, bits, data[0], data[1])


def sound_slots(image: bytes) -> List[WavSlot]:
    out = []
    for e in resource_directory(image):
        if e.name in SOUND_SLOTS:
            out.append(parse_wav_at(image, e))
    return out


# --- Photos stored in flash ---------------------------------------------------------------------------


@dataclass
class PhotoIndexEntry:
    slot: int
    used: int
    size: int


def photo_index(image: bytes) -> List[PhotoIndexEntry]:
    """8-byte entries (u16 slot, u16 used, u32 size); VERIFIED against the stored JPEG lengths."""
    out = []
    for i in range(0, 0x40, 8):
        if len(image) < PHOTO_INDEX + i + 8:
            break
        slot, used, size = struct.unpack_from("<HHI", image, PHOTO_INDEX + i)
        if used == 0 and size == 0:
            continue
        out.append(PhotoIndexEntry(slot, used, size))
    return out


def jpeg_length_at(image: bytes, off: int) -> Optional[int]:
    if image[off: off + 2] != b"\xff\xd8":
        return None
    end = image.find(b"\xff\xd9", off)
    return None if end < 0 else end + 2 - off


def stored_photos(image: bytes) -> List[tuple]:
    out = []
    for slot_off in PHOTO_SLOTS:
        if len(image) > slot_off + 2:
            n = jpeg_length_at(image, slot_off)
            if n:
                out.append((slot_off, n))
    return out


# --- Summary -------------------------------------------------------------------------------------------


def summarize(image: bytes) -> dict:
    """Everything inspect prints, as plain data (also used by verify and pack)."""
    kind = image_kind(image)
    hdr = parse_header(image)
    build = detect_build(image)
    recs = settings_mod.parse_settings(image) if len(image) > SETTINGS_BASE else []
    valid = [r for r in recs if r.crc_ok]
    current = valid[-1] if valid else None
    info = {
        "size": len(image),
        "kind": kind,
        "sha256": hashlib.sha256(image).hexdigest(),
        "byte_sum": byte_sum(image),
        "magic_ok": hdr.magic_ok,
        "header_words": hdr.words,
        "reg_init": hdr.reg_init,
        "series": build.series,
        "series_label": build.build.label if build.build else "not a known build",
        "series_checks": build.checks,
        "embedded_stamp": embedded_stamp(image) if len(image) > ORIGINAL_2025.stamp_offset + 12 else "",
        "software_sha256": software_sha256(image) if len(image) >= SOFTWARE_END else None,
        "public_dump": PUBLIC_DUMPS.get(hashlib.sha256(image).hexdigest()),
        "resources": resource_directory(image),
        "resources_end": find_resources_end(image) if len(image) > RESOURCE_BASE else None,
        "sounds": sound_slots(image) if len(image) > RESOURCE_BASE else [],
        "settings_records": recs,
        "settings_valid": len(valid),
        "current_settings": current,
        "photo_index": photo_index(image) if len(image) > PHOTO_INDEX + 0x40 else [],
        "stored_photos": stored_photos(image),
        "tail_blank": tail_blank(image),
    }
    return info
