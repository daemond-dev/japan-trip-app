"""card-check and card-fix: the Charmera's exFAT dirty bit and the MBR partition type.

The camera formats its microSD card as exFAT behind an MBR whose partition type is 0x07, and it never
clears the exFAT VolumeDirty flag (VolumeFlags bit 1 at bytes 106..107 of the boot sector). Android
refuses to mount a dirty exFAT volume. A FAT32 volume with the partition type set to 0x0C (FAT32 LBA)
mounts on Android, iPhone, Mac, Linux and Windows; mkfs.fat alone leaves the type byte at 0x07, which
iPhones reject.

Every parser and writer in this file takes a file-like object plus offsets, so the same code runs on an
image file in the tests and on a block device for real. The command layer adds the device lookup, the
mount and size checks, the typed confirmation and the synchronous writes.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import stat
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import IO, Callable, Dict, List, Optional, Tuple

try:  # Linux only
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

SECTOR = 512
MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_CARD_BYTES = 512 * GIB

MBR_TABLE = 446
MBR_ENTRY = 16
MBR_SIGNATURE = b"\x55\xAA"

PARTITION_TYPES = {
    0x00: "empty",
    0x01: "FAT12",
    0x04: "FAT16 under 32 MiB",
    0x05: "extended",
    0x06: "FAT16",
    0x07: "NTFS/exFAT",
    0x0B: "FAT32 CHS",
    0x0C: "FAT32 LBA",
    0x0E: "FAT16 LBA",
    0x0F: "extended LBA",
    0x82: "Linux swap",
    0x83: "Linux",
    0xEE: "GPT protective",
    0xEF: "EFI system",
}

GPT_TYPE_NAMES = {
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI system",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
}

# exFAT VolumeFlags bits (bytes 106..107 of the boot sector).
EXFAT_FLAG_ACTIVE_FAT = 0x0001
EXFAT_FLAG_DIRTY = 0x0002
EXFAT_FLAG_MEDIA_FAILURE = 0x0004
EXFAT_VOLUME_FLAGS_OFFSET = 106
EXFAT_BACKUP_BOOT_SECTOR = 12  # the backup boot region starts at sector 12 of the volume
EXFAT_CHECKSUM_SECTOR = 11

# FAT32 layout written by format_fat32 (all the usual values).
FAT32_START_LBA = 2048
FAT32_RESERVED = 32
FAT32_NUM_FATS = 2
FAT32_FSINFO_SECTOR = 1
FAT32_BACKUP_SECTOR = 6
FAT32_ROOT_CLUSTER = 2
FAT32_MIN_CLUSTERS = 65525
FAT32_MAX_CLUSTERS = 0x0FFFFFF5
FAT32_PARTITION_TYPE = 0x0C
FSINFO_LEAD = 0x41615252
FSINFO_STRUCT = 0x61417272
FSINFO_TRAIL = 0xAA550000
ZERO_HEAD_BYTES = 4 * MIB

LABEL_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !#$%&'()-@^_`{}~")


class CardError(Exception):
    """A refusal or a bad request. The message is meant for the user."""


# ----------------------------------------------------------------------------------------------
# Low-level reads and writes. Offsets and lengths are kept sector-aligned because raw devices on
# Windows reject anything else.


def read_at(fh: IO[bytes], offset: int, length: int) -> bytes:
    """Read exactly `length` bytes at `offset`, aligning the physical read to 512-byte sectors."""
    start = offset - (offset % SECTOR)
    end = offset + length
    end += (-end) % SECTOR
    fh.seek(start)
    data = fh.read(end - start)
    if len(data) < (offset - start) + length:
        raise EOFError(f"short read at offset {offset}: wanted {length} bytes, got {max(0, len(data) - (offset - start))}")
    return data[offset - start : offset - start + length]


def write_at(fh: IO[bytes], offset: int, data: bytes) -> None:
    """Write `data` at `offset`. Callers keep this sector-aligned."""
    if offset % SECTOR or len(data) % SECTOR:
        raise ValueError("writes must be whole sectors")
    fh.seek(offset)
    written = fh.write(data)
    if written is not None and written != len(data):
        raise OSError(f"short write at offset {offset}: {written} of {len(data)} bytes")


def zero_range(fh: IO[bytes], offset: int, length: int, chunk: int = MIB) -> None:
    """Write zeros over [offset, offset + length)."""
    buf = bytes(chunk)
    pos = offset
    end = offset + length
    while pos < end:
        n = min(chunk, end - pos)
        write_at(fh, pos, buf if n == chunk else bytes(n))
        pos += n


def sync(fh: IO[bytes]) -> None:
    """Flush Python buffers and ask the OS to commit the writes to the medium."""
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "unknown size"
    for unit, div in (("TiB", GIB * 1024), ("GiB", GIB), ("MiB", MIB), ("KiB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


# ----------------------------------------------------------------------------------------------
# MBR and GPT


@dataclass
class MbrPartition:
    index: int  # 1-based
    status: int
    type: int
    start_lba: int
    sectors: int
    chs_start: Tuple[int, int, int]
    chs_end: Tuple[int, int, int]

    @property
    def empty(self) -> bool:
        return self.type == 0 and self.sectors == 0

    @property
    def type_name(self) -> str:
        return PARTITION_TYPES.get(self.type, "unknown")


@dataclass
class Mbr:
    signature_ok: bool
    disk_signature: int
    partitions: List[MbrPartition]

    @property
    def gpt_protective(self) -> bool:
        return any(p.type == 0xEE for p in self.partitions)

    @property
    def first(self) -> Optional[MbrPartition]:
        for p in self.partitions:
            if not p.empty:
                return p
        return None


def parse_mbr(data: bytes) -> Mbr:
    """Parse a 512-byte master boot record."""
    if len(data) < SECTOR:
        raise ValueError("MBR needs 512 bytes")
    parts = []
    for i in range(4):
        e = MBR_TABLE + i * MBR_ENTRY
        status, h0, s0, c0, ptype, h1, s1, c1, start, sectors = struct.unpack_from("<BBBBBBBBII", data, e)
        parts.append(MbrPartition(i + 1, status, ptype, start, sectors, (h0, s0, c0), (h1, s1, c1)))
    return Mbr(
        signature_ok=data[510:512] == MBR_SIGNATURE,
        disk_signature=struct.unpack_from("<I", data, 440)[0],
        partitions=parts,
    )


def lba_to_chs(lba: int, heads: int = 255, sectors_per_track: int = 63) -> Tuple[int, int, int]:
    """The three CHS bytes (head, sector+cylinder high bits, cylinder low) for an LBA, or the usual
    0xFE/0xFF/0xFF once the address is past what CHS can express."""
    if lba >= 1024 * heads * sectors_per_track:
        return (0xFE, 0xFF, 0xFF)
    cyl = lba // (heads * sectors_per_track)
    head = (lba // sectors_per_track) % heads
    sec = lba % sectors_per_track + 1
    return (head, ((cyl >> 8) & 0x03) << 6 | sec, cyl & 0xFF)


def build_mbr(start_lba: int, sectors: int, ptype: int = FAT32_PARTITION_TYPE, disk_signature: Optional[int] = None) -> bytes:
    """An MBR with one partition and no boot code."""
    mbr = bytearray(SECTOR)
    if disk_signature is None:
        disk_signature = struct.unpack("<I", os.urandom(4))[0] or 0x12345678
    struct.pack_into("<I", mbr, 440, disk_signature)
    h0, s0, c0 = lba_to_chs(start_lba)
    h1, s1, c1 = lba_to_chs(start_lba + sectors - 1)
    struct.pack_into("<BBBBBBBBII", mbr, MBR_TABLE, 0x00, h0, s0, c0, ptype, h1, s1, c1, start_lba, sectors)
    mbr[510:512] = MBR_SIGNATURE
    return bytes(mbr)


@dataclass
class GptEntry:
    index: int
    type_guid: str
    first_lba: int
    last_lba: int
    name: str

    @property
    def type_name(self) -> str:
        return GPT_TYPE_NAMES.get(self.type_guid, "unknown")


def parse_gpt(fh: IO[bytes]) -> Tuple[bool, List[GptEntry]]:
    """Read the primary GPT header at LBA 1 and its used entries. Returns (header found, entries)."""
    header = read_at(fh, SECTOR, SECTOR)
    if header[:8] != b"EFI PART":
        return False, []
    entries_lba, count, entry_size = struct.unpack_from("<QII", header, 72)
    if entry_size < 128 or count == 0 or count > 1024:
        return True, []
    raw = read_at(fh, entries_lba * SECTOR, count * entry_size)
    out = []
    for i in range(count):
        e = raw[i * entry_size : (i + 1) * entry_size]
        if e[:16] == bytes(16):
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16-le", errors="replace").split("\x00", 1)[0]
        out.append(GptEntry(i + 1, str(uuid.UUID(bytes_le=e[:16])), first, last, name))
    return True, out


# ----------------------------------------------------------------------------------------------
# Boot sectors


@dataclass
class ExfatInfo:
    volume_flags: int
    bytes_per_sector: int
    sectors_per_cluster: int
    volume_length: int
    fat_offset: int
    fat_length: int
    cluster_heap_offset: int
    cluster_count: int
    root_cluster: int
    serial: int
    revision: Tuple[int, int]
    percent_in_use: int

    @property
    def dirty(self) -> bool:
        return bool(self.volume_flags & EXFAT_FLAG_DIRTY)

    @property
    def media_failure(self) -> bool:
        return bool(self.volume_flags & EXFAT_FLAG_MEDIA_FAILURE)

    @property
    def active_fat(self) -> int:
        return self.volume_flags & EXFAT_FLAG_ACTIVE_FAT


@dataclass
class FatInfo:
    kind: str  # fat12, fat16 or fat32
    oem_name: str
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    num_fats: int
    root_entries: int
    total_sectors: int
    media: int
    fat_size: int
    sectors_per_track: int
    heads: int
    hidden_sectors: int
    root_cluster: int
    fsinfo_sector: int
    backup_boot_sector: int
    drive_number: int
    ext_boot_sig: int
    serial: int
    label: str
    fs_type: str
    cluster_count: int
    data_start_sector: int


@dataclass
class BootSector:
    kind: str  # exfat, fat32, fat16, fat12 or unknown
    raw: bytes
    exfat: Optional[ExfatInfo] = None
    fat: Optional[FatInfo] = None


def identify_boot_sector(data: bytes) -> str:
    """exfat, fat32, fat16, fat12 or unknown, from the first 512 bytes of a volume."""
    if len(data) < SECTOR:
        return "unknown"
    if data[3:11] == b"EXFAT   ":
        return "exfat"
    if data[510:512] != MBR_SIGNATURE or data[0] not in (0xEB, 0xE9):
        return "unknown"
    bps = struct.unpack_from("<H", data, 11)[0]
    spc = data[13]
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1):
        return "unknown"
    try:
        return parse_fat_boot_sector(data).kind
    except ValueError:
        return "unknown"


def parse_exfat_boot_sector(data: bytes) -> ExfatInfo:
    if data[3:11] != b"EXFAT   ":
        raise ValueError("not an exFAT boot sector (no 'EXFAT   ' at offset 3)")
    (
        _partition_offset,
        volume_length,
        fat_offset,
        fat_length,
        heap_offset,
        cluster_count,
        root_cluster,
        serial,
        rev_minor,
        rev_major,
        flags,
        bps_shift,
        spc_shift,
        _num_fats,
        _drive_select,
        percent,
    ) = struct.unpack_from("<QQIIIIIIBBHBBBBB", data, 64)
    if not 9 <= bps_shift <= 12:
        raise ValueError(f"exFAT BytesPerSectorShift {bps_shift} is out of range 9..12")
    return ExfatInfo(
        volume_flags=flags,
        bytes_per_sector=1 << bps_shift,
        sectors_per_cluster=1 << spc_shift,
        volume_length=volume_length,
        fat_offset=fat_offset,
        fat_length=fat_length,
        cluster_heap_offset=heap_offset,
        cluster_count=cluster_count,
        root_cluster=root_cluster,
        serial=serial,
        revision=(rev_major, rev_minor),
        percent_in_use=percent,
    )


def exfat_boot_checksum(region: bytes) -> int:
    """The exFAT boot region checksum over sectors 0..10. Bytes 106, 107 (VolumeFlags) and 112
    (PercentInUse) are excluded by the specification, which is why clearing the dirty bit leaves the
    checksum sector untouched."""
    cs = 0
    for i, b in enumerate(region):
        if i == 106 or i == 107 or i == 112:
            continue
        cs = ((0x80000000 if cs & 1 else 0) + (cs >> 1) + b) & 0xFFFFFFFF
    return cs


def parse_fat_boot_sector(data: bytes) -> FatInfo:
    """Parse a FAT12/16/32 boot sector. The kind follows Microsoft's cluster-count rule, except that a
    FAT32-shaped BPB (FATSz16 == 0, 'FAT32' at 82) is reported as fat32 even when its count is small."""
    bps, spc, reserved, nfats, root_entries, tot16, media, fatsz16, spt, heads, hidden, tot32 = struct.unpack_from(
        "<HBHBHHBHHHII", data, 11
    )
    if bps == 0 or spc == 0:
        raise ValueError("bytes per sector or sectors per cluster is zero")
    total = tot16 or tot32
    fat32_layout = fatsz16 == 0 and (data[82:87] == b"FAT32" or struct.unpack_from("<I", data, 36)[0] != 0)
    if fat32_layout:
        fatsz32, _ext_flags, _fsver, root_cluster, fsinfo, backup = struct.unpack_from("<IHHIHH", data, 36)
        fat_size = fatsz32
        drive, _res, bootsig, serial = struct.unpack_from("<BBBI", data, 64)
        label = data[71:82].decode("ascii", errors="replace")
        fs_type = data[82:90].decode("ascii", errors="replace")
    else:
        fat_size = fatsz16
        root_cluster, fsinfo, backup = 0, 0, 0
        drive, _res, bootsig, serial = struct.unpack_from("<BBBI", data, 36)
        label = data[43:54].decode("ascii", errors="replace")
        fs_type = data[54:62].decode("ascii", errors="replace")
    root_dir_sectors = (root_entries * 32 + bps - 1) // bps
    data_start = reserved + nfats * fat_size + root_dir_sectors
    data_sectors = max(0, total - data_start)
    clusters = data_sectors // spc
    if fat32_layout:
        kind = "fat32"
    elif clusters < 4085:
        kind = "fat12"
    elif clusters < 65525:
        kind = "fat16"
    else:
        kind = "fat32"
    return FatInfo(
        kind=kind,
        oem_name=data[3:11].decode("ascii", errors="replace"),
        bytes_per_sector=bps,
        sectors_per_cluster=spc,
        reserved_sectors=reserved,
        num_fats=nfats,
        root_entries=root_entries,
        total_sectors=total,
        media=media,
        fat_size=fat_size,
        sectors_per_track=spt,
        heads=heads,
        hidden_sectors=hidden,
        root_cluster=root_cluster,
        fsinfo_sector=fsinfo,
        backup_boot_sector=backup,
        drive_number=drive,
        ext_boot_sig=bootsig,
        serial=serial,
        label=label,
        fs_type=fs_type,
        cluster_count=clusters,
        data_start_sector=data_start,
    )


def parse_boot_sector(data: bytes) -> BootSector:
    kind = identify_boot_sector(data)
    bs = BootSector(kind=kind, raw=bytes(data[:SECTOR]))
    if kind == "exfat":
        bs.exfat = parse_exfat_boot_sector(data)
    elif kind in ("fat12", "fat16", "fat32"):
        bs.fat = parse_fat_boot_sector(data)
    return bs


def read_boot_sector(fh: IO[bytes], part_offset: int) -> BootSector:
    return parse_boot_sector(read_at(fh, part_offset, SECTOR))


@dataclass
class FsInfo:
    lead: int
    struct_sig: int
    free_count: int
    next_free: int
    trail: int

    @property
    def signatures_ok(self) -> bool:
        return self.lead == FSINFO_LEAD and self.struct_sig == FSINFO_STRUCT and self.trail == FSINFO_TRAIL


def parse_fsinfo(data: bytes) -> FsInfo:
    lead = struct.unpack_from("<I", data, 0)[0]
    struct_sig, free_count, next_free = struct.unpack_from("<III", data, 484)
    trail = struct.unpack_from("<I", data, 508)[0]
    return FsInfo(lead, struct_sig, free_count, next_free, trail)


def read_fat_entries(fh: IO[bytes], part_offset: int, info: FatInfo, count: int = 3, fat_index: int = 0) -> List[int]:
    """The first `count` 32-bit entries of one FAT (high 4 bits masked off as the spec requires)."""
    off = part_offset + (info.reserved_sectors + fat_index * info.fat_size) * info.bytes_per_sector
    raw = read_at(fh, off, SECTOR)
    return [struct.unpack_from("<I", raw, i * 4)[0] & 0x0FFFFFFF for i in range(count)]


# ----------------------------------------------------------------------------------------------
# Fixes that patch a few bytes


@dataclass
class DirtyClearResult:
    part_offset: int
    old_flags: int
    new_flags: int
    sectors_written: List[int]  # volume-relative sector numbers


def clear_exfat_dirty_bit(fh: IO[bytes], part_offset: int) -> DirtyClearResult:
    """Clear VolumeDirty (bit 1 of VolumeFlags) in the main boot sector and in the backup boot sector
    at sector 12 of the volume.

    The boot region checksum in sector 11 (and its copy in sector 23) covers sectors 0..10 but skips
    bytes 106, 107 and 112 of sector 0, so changing VolumeFlags does not change the checksum. Sector 11
    is therefore left alone on purpose.
    """
    main = bytearray(read_at(fh, part_offset, SECTOR))
    info = parse_exfat_boot_sector(main)
    bps = info.bytes_per_sector
    if bps != SECTOR:
        main = bytearray(read_at(fh, part_offset, bps))
    old = info.volume_flags
    new = old & ~EXFAT_FLAG_DIRTY
    written = []
    if new != old:
        struct.pack_into("<H", main, EXFAT_VOLUME_FLAGS_OFFSET, new)
        write_at(fh, part_offset, bytes(main))
        written.append(0)
    backup_off = part_offset + EXFAT_BACKUP_BOOT_SECTOR * bps
    backup = bytearray(read_at(fh, backup_off, bps))
    if backup[3:11] == b"EXFAT   ":
        bflags = struct.unpack_from("<H", backup, EXFAT_VOLUME_FLAGS_OFFSET)[0]
        if bflags & EXFAT_FLAG_DIRTY:
            struct.pack_into("<H", backup, EXFAT_VOLUME_FLAGS_OFFSET, bflags & ~EXFAT_FLAG_DIRTY)
            write_at(fh, backup_off, bytes(backup))
            written.append(EXFAT_BACKUP_BOOT_SECTOR)
    sync(fh)
    return DirtyClearResult(part_offset, old, new, written)


def set_partition_type(fh: IO[bytes], index: int, new_type: int) -> Tuple[int, int]:
    """Set the type byte of MBR partition `index` (1-based). Returns (old, new)."""
    if not 1 <= index <= 4:
        raise ValueError("partition index must be 1..4")
    mbr = bytearray(read_at(fh, 0, SECTOR))
    if mbr[510:512] != MBR_SIGNATURE:
        raise CardError("sector 0 has no MBR signature; refusing to edit a partition table that is not there")
    pos = MBR_TABLE + (index - 1) * MBR_ENTRY + 4
    old = mbr[pos]
    mbr[pos] = new_type & 0xFF
    write_at(fh, 0, bytes(mbr))
    sync(fh)
    return old, new_type & 0xFF


def fix_mbr_type_for_fat32(fh: IO[bytes]) -> Tuple[int, int]:
    """Set the first partition's type to 0x0C, but only when that partition holds FAT32."""
    mbr = parse_mbr(read_at(fh, 0, SECTOR))
    part = mbr.first
    if mbr.gpt_protective:
        raise CardError("the disk has a GPT, not a plain MBR; use --format-fat32 instead")
    if part is None:
        raise CardError("the MBR has no partitions; use --format-fat32 instead")
    bs = read_boot_sector(fh, part.start_lba * SECTOR)
    if bs.kind != "fat32":
        raise CardError(
            f"partition {part.index} holds {describe_kind(bs.kind)}, not FAT32; the type byte 0x0C is only right for "
            "FAT32 (for exFAT, 0x07 is correct)"
        )
    return set_partition_type(fh, part.index, FAT32_PARTITION_TYPE)


def describe_kind(kind: str) -> str:
    return {"exfat": "exFAT", "fat32": "FAT32", "fat16": "FAT16", "fat12": "FAT12"}.get(kind, "an unknown filesystem")


# ----------------------------------------------------------------------------------------------
# FAT32 formatting


@dataclass
class Fat32Plan:
    disk_bytes: int
    disk_sectors: int
    start_lba: int
    partition_sectors: int
    sectors_per_cluster: int
    fat_sectors: int
    cluster_count: int
    label: str
    serial: int
    reserved_sectors: int = FAT32_RESERVED
    num_fats: int = FAT32_NUM_FATS

    @property
    def cluster_bytes(self) -> int:
        return self.sectors_per_cluster * SECTOR

    @property
    def data_start_sector(self) -> int:
        return self.reserved_sectors + self.num_fats * self.fat_sectors

    @property
    def partition_bytes(self) -> int:
        return self.partition_sectors * SECTOR


def default_cluster_size(partition_bytes: int) -> int:
    """The usual FAT32 cluster size for a volume of this size."""
    if partition_bytes <= 8 * GIB:
        return 4096
    if partition_bytes <= 16 * GIB:
        return 8192
    if partition_bytes <= 32 * GIB:
        return 16384
    return 32768


def fat32_geometry(partition_sectors: int, sectors_per_cluster: int, reserved: int = FAT32_RESERVED, num_fats: int = FAT32_NUM_FATS) -> Tuple[int, int]:
    """(FAT size in sectors, cluster count) with Microsoft's FATGEN rounding, then verified so that the
    FAT really holds every cluster (the estimate can only err on the large side, but check anyway)."""
    tmp1 = partition_sectors - reserved
    tmp2 = (256 * sectors_per_cluster + num_fats) // 2
    fat_sectors = (tmp1 + tmp2 - 1) // tmp2
    while True:
        data_sectors = partition_sectors - reserved - num_fats * fat_sectors
        clusters = max(0, data_sectors // sectors_per_cluster)
        needed = ((clusters + 2) * 4 + SECTOR - 1) // SECTOR
        if needed <= fat_sectors:
            return fat_sectors, clusters
        fat_sectors += 1


def parse_cluster_size(text: str) -> int:
    """'4096', '4K', '32KiB' and the like, in bytes. Must be a power of two from 512 to 32768."""
    t = text.strip().lower().replace("ib", "").replace("b", "")
    mult = 1
    if t.endswith("k"):
        mult = 1024
        t = t[:-1]
    if not t.isdigit():
        raise CardError(f"cluster size {text!r} is not a number of bytes (examples: 512, 4096, 32K)")
    n = int(t) * mult
    if n < 512 or n > 32768 or n & (n - 1):
        raise CardError(f"cluster size {text!r}: use a power of two between 512 and 32K")
    return n


def normalize_label(label: str) -> str:
    """Upper-case, 11 characters at most, FAT's character set."""
    lab = label.strip().upper()
    if len(lab) > 11:
        raise CardError(f"label {label!r} is longer than 11 characters")
    bad = sorted(set(lab) - LABEL_ALLOWED)
    if bad:
        raise CardError(f"label {label!r} contains characters FAT does not allow: {''.join(bad)!r}")
    return lab


def plan_fat32(disk_bytes: int, cluster_size: Optional[int] = None, label: str = "CHARMERA", serial: Optional[int] = None) -> Fat32Plan:
    """Work out the FAT32 layout for a disk of `disk_bytes` without writing anything.

    With cluster_size None the usual size for the volume is used, halved as needed so that small images
    still get the 65525 clusters FAT32 requires. An explicit cluster size that cannot work is an error.
    """
    if disk_bytes > MAX_CARD_BYTES:
        raise CardError(f"{human_size(disk_bytes)} is larger than 512 GiB; that is not a camera card, refusing")
    disk_sectors = disk_bytes // SECTOR
    partition_sectors = disk_sectors - FAT32_START_LBA
    if partition_sectors <= 0:
        raise CardError(f"{human_size(disk_bytes)} is too small to hold a partition that starts at 1 MiB")
    partition_bytes = partition_sectors * SECTOR
    lab = normalize_label(label)
    if serial is None:
        serial = struct.unpack("<I", os.urandom(4))[0]

    def attempt(cbytes: int) -> Tuple[int, int, int]:
        spc = cbytes // SECTOR
        fat_sectors, clusters = fat32_geometry(partition_sectors, spc)
        return spc, fat_sectors, clusters

    if cluster_size is not None:
        if cluster_size % SECTOR or cluster_size < 512 or cluster_size > 32768 or cluster_size & (cluster_size - 1):
            raise CardError("cluster size must be a power of two between 512 and 32768 bytes")
        spc, fat_sectors, clusters = attempt(cluster_size)
        if clusters < FAT32_MIN_CLUSTERS:
            raise CardError(
                f"{cluster_size}-byte clusters give {clusters} clusters on a {human_size(partition_bytes)} volume; FAT32 "
                f"needs at least {FAT32_MIN_CLUSTERS}. Use a smaller cluster size or leave --cluster-size out."
            )
    else:
        cbytes = default_cluster_size(partition_bytes)
        while True:
            spc, fat_sectors, clusters = attempt(cbytes)
            if clusters >= FAT32_MIN_CLUSTERS or cbytes == SECTOR:
                break
            cbytes //= 2
        if clusters < FAT32_MIN_CLUSTERS:
            raise CardError(
                f"{human_size(disk_bytes)} is too small for FAT32 (needs about 34 MiB for {FAT32_MIN_CLUSTERS} clusters)"
            )
    if clusters >= FAT32_MAX_CLUSTERS:
        raise CardError("too many clusters for FAT32; use a larger cluster size")
    return Fat32Plan(
        disk_bytes=disk_bytes,
        disk_sectors=disk_sectors,
        start_lba=FAT32_START_LBA,
        partition_sectors=partition_sectors,
        sectors_per_cluster=spc,
        fat_sectors=fat_sectors,
        cluster_count=clusters,
        label=lab,
        serial=serial,
    )


def build_fat32_boot_sector(plan: Fat32Plan) -> bytes:
    bs = bytearray(SECTOR)
    bs[0:3] = b"\xEB\x58\x90"
    bs[3:11] = b"MSWIN4.1"
    struct.pack_into(
        "<HBHBHHBHHHII",
        bs,
        11,
        SECTOR,  # BPB_BytsPerSec
        plan.sectors_per_cluster,  # BPB_SecPerClus
        plan.reserved_sectors,  # BPB_RsvdSecCnt
        plan.num_fats,  # BPB_NumFATs
        0,  # BPB_RootEntCnt
        0,  # BPB_TotSec16
        0xF8,  # BPB_Media
        0,  # BPB_FATSz16
        63,  # BPB_SecPerTrk
        255,  # BPB_NumHeads
        plan.start_lba,  # BPB_HiddSec
        plan.partition_sectors,  # BPB_TotSec32
    )
    struct.pack_into(
        "<IHHIHH",
        bs,
        36,
        plan.fat_sectors,  # BPB_FATSz32
        0,  # BPB_ExtFlags: FATs mirrored
        0,  # BPB_FSVer
        FAT32_ROOT_CLUSTER,  # BPB_RootClus
        FAT32_FSINFO_SECTOR,  # BPB_FSInfo
        FAT32_BACKUP_SECTOR,  # BPB_BkBootSec
    )
    struct.pack_into("<BBBI", bs, 64, 0x80, 0x00, 0x29, plan.serial)
    bs[71:82] = (plan.label or "NO NAME").ljust(11).encode("ascii")
    bs[82:90] = b"FAT32   "
    bs[510:512] = MBR_SIGNATURE
    return bytes(bs)


def build_fsinfo(free_count: int, next_free: int) -> bytes:
    fs = bytearray(SECTOR)
    struct.pack_into("<I", fs, 0, FSINFO_LEAD)
    struct.pack_into("<III", fs, 484, FSINFO_STRUCT, free_count, next_free)
    struct.pack_into("<I", fs, 508, FSINFO_TRAIL)
    return bytes(fs)


def dos_datetime(t: Optional[time.struct_time] = None) -> Tuple[int, int]:
    """(time, date) in FAT directory-entry encoding for a local time, default now."""
    if t is None:
        t = time.localtime()
    year = min(max(t.tm_year, 1980), 2107)
    date = ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return tm, date


def build_label_entry(label: str) -> bytes:
    """A 32-byte volume-label directory entry (ATTR_VOLUME_ID)."""
    e = bytearray(32)
    e[0:11] = label.ljust(11).encode("ascii")
    e[11] = 0x08
    tm, date = dos_datetime()
    struct.pack_into("<HH", e, 22, tm, date)
    struct.pack_into("<H", e, 18, date)
    return bytes(e)


def format_fat32(
    fh: IO[bytes],
    disk_bytes: int,
    label: str = "CHARMERA",
    cluster_size: Optional[int] = None,
    serial: Optional[int] = None,
    zero_gpt_tail: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> Fat32Plan:
    """Write a fresh MBR (one partition, type 0x0C, at 1 MiB) and a FAT32 volume over the whole disk.

    Destructive. The first 4 MiB are zeroed first so no exFAT boot region, bitmap or up-case table
    survives at the old offsets. With zero_gpt_tail the last 33 sectors (a backup GPT) go too.
    """
    plan = plan_fat32(disk_bytes, cluster_size=cluster_size, label=label, serial=serial)
    say = log or (lambda _msg: None)
    say(f"zeroing the first {human_size(min(ZERO_HEAD_BYTES, disk_bytes))}")
    zero_range(fh, 0, min(ZERO_HEAD_BYTES, disk_bytes))
    if zero_gpt_tail and disk_bytes > 34 * SECTOR:
        say("zeroing the backup GPT at the end of the disk")
        zero_range(fh, (plan.disk_sectors - 33) * SECTOR, 33 * SECTOR)
    say("writing the MBR")
    write_at(fh, 0, build_mbr(plan.start_lba, plan.partition_sectors))
    base = plan.start_lba * SECTOR
    boot = build_fat32_boot_sector(plan)
    fsinfo = build_fsinfo(plan.cluster_count - 1, FAT32_ROOT_CLUSTER + 1)
    third = bytes(510) + MBR_SIGNATURE
    say("writing the boot sector, FSInfo and their backups")
    for sec in (0, FAT32_BACKUP_SECTOR):
        write_at(fh, base + sec * SECTOR, boot)
    for sec in (FAT32_FSINFO_SECTOR, FAT32_BACKUP_SECTOR + 1):
        write_at(fh, base + sec * SECTOR, fsinfo)
    for sec in (2, FAT32_BACKUP_SECTOR + 2):
        write_at(fh, base + sec * SECTOR, third)
    first_fat_sector = bytearray(SECTOR)
    struct.pack_into("<III", first_fat_sector, 0, 0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF)
    for i in range(plan.num_fats):
        say(f"writing FAT {i + 1} of {plan.num_fats} ({human_size(plan.fat_sectors * SECTOR)})")
        fat_off = base + (plan.reserved_sectors + i * plan.fat_sectors) * SECTOR
        write_at(fh, fat_off, bytes(first_fat_sector))
        zero_range(fh, fat_off + SECTOR, (plan.fat_sectors - 1) * SECTOR)
    say("writing the root directory")
    root_off = base + plan.data_start_sector * SECTOR
    zero_range(fh, root_off, plan.cluster_bytes)
    if plan.label:
        sector0 = bytearray(SECTOR)
        sector0[0:32] = build_label_entry(plan.label)
        write_at(fh, root_off, bytes(sector0))
    sync(fh)
    return plan


# ----------------------------------------------------------------------------------------------
# The check


@dataclass
class CheckResult:
    size: int
    mbr: Optional[Mbr] = None
    gpt_entries: List[GptEntry] = field(default_factory=list)
    superfloppy: bool = False
    partition: Optional[MbrPartition] = None
    part_offset: Optional[int] = None
    boot: Optional[BootSector] = None
    fsinfo: Optional[FsInfo] = None
    backup_matches: Optional[bool] = None
    fat_entries: List[int] = field(default_factory=list)
    exfat_checksum_ok: Optional[bool] = None
    lines: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    platforms: Dict[str, str] = field(default_factory=dict)
    fixes: List[str] = field(default_factory=list)  # option strings for card-fix, in recommended order

    @property
    def exit_code(self) -> int:
        return 1 if self.problems else 0


PLATFORMS = ("Android", "iPhone", "Mac", "Linux", "Windows")


def check_disk(fh: IO[bytes], size: int) -> CheckResult:
    """Read-only inspection of a whole disk (device or image). Nothing is written."""
    r = CheckResult(size=size)
    out = r.lines.append
    if size < 2 * SECTOR:
        r.problems.append("the disk is smaller than two sectors; nothing to read")
        return r
    if size > MAX_CARD_BYTES:
        r.notes.append(f"{human_size(size)} is larger than any camera card; make sure this is the right disk")
    sector0 = read_at(fh, 0, SECTOR)
    kind0 = identify_boot_sector(sector0)
    mbr = parse_mbr(sector0)
    r.mbr = mbr

    if kind0 != "unknown":
        # A filesystem at sector 0: no partition table at all.
        r.superfloppy = True
        out(f"Sector 0: {describe_kind(kind0)} boot sector, no partition table (superfloppy layout)")
        r.problems.append("no partition table: the camera, iPhone and most readers expect an MBR with one partition")
        r.partition = None
        r.part_offset = 0
        _inspect_volume(fh, r, 0, size)
        for p in PLATFORMS:
            r.platforms[p] = "unknown (no partition table)"
        r.fixes.append("--format-fat32")
        return r

    out(f"MBR: signature {'ok' if mbr.signature_ok else 'missing (0x55AA not at byte 510)'}, disk id 0x{mbr.disk_signature:08X}")
    if not mbr.signature_ok:
        r.problems.append("no MBR signature; the card has no usable partition table")
    for p in mbr.partitions:
        if p.empty:
            out(f"  #{p.index}  empty")
            continue
        flag = " active" if p.status == 0x80 else ""
        out(
            f"  #{p.index}  type 0x{p.type:02X} ({p.type_name}){flag}  start LBA {p.start_lba:,}  "
            f"sectors {p.sectors:,} ({human_size(p.sectors * SECTOR)})"
        )
        if (p.start_lba + p.sectors) * SECTOR > size and p.type != 0xEE:
            r.problems.append(f"partition {p.index} runs past the end of the disk ({human_size(size)})")

    if mbr.gpt_protective:
        found, entries = parse_gpt(fh)
        r.gpt_entries = entries
        if found:
            out("GPT: primary header found at LBA 1")
            for e in entries:
                out(f"  GPT #{e.index}  {e.type_name}  first LBA {e.first_lba:,}  last LBA {e.last_lba:,}  {e.name!r}")
        else:
            out("GPT: protective MBR but no GPT header at LBA 1")
        r.problems.append("the disk has a GPT partition table; the camera and phones expect a plain MBR")
        if entries:
            r.part_offset = entries[0].first_lba * SECTOR
            _inspect_volume(fh, r, r.part_offset, (entries[0].last_lba - entries[0].first_lba + 1) * SECTOR)
        for p in PLATFORMS:
            r.platforms[p] = "unknown (GPT)"
        r.fixes.append("--format-fat32")
        return r

    first = mbr.first
    if first is None:
        out("No partitions in the MBR")
        r.problems.append("the MBR has no partitions; the card is blank or wiped")
        for p in PLATFORMS:
            r.platforms[p] = "nothing to mount"
        r.fixes.append("--format-fat32")
        return r

    r.partition = first
    r.part_offset = first.start_lba * SECTOR
    if r.part_offset + SECTOR > size:
        r.problems.append(f"partition {first.index} starts past the end of the disk")
        return r
    _inspect_volume(fh, r, r.part_offset, first.sectors * SECTOR)
    _verdict(r)
    return r


def _inspect_volume(fh: IO[bytes], r: CheckResult, part_offset: int, part_bytes: int) -> None:
    out = r.lines.append
    try:
        boot = read_boot_sector(fh, part_offset)
    except EOFError:
        r.problems.append("could not read the boot sector of the first partition")
        return
    r.boot = boot
    where = f"LBA {part_offset // SECTOR:,}"
    if boot.kind == "exfat" and boot.exfat is not None:
        x = boot.exfat
        out(f"Boot sector at {where}: exFAT {x.revision[0]}.{x.revision[1]:02d}")
        out(
            f"  VolumeFlags 0x{x.volume_flags:04X}: ActiveFat {x.active_fat}, VolumeDirty {int(x.dirty)}, "
            f"MediaFailure {int(x.media_failure)}"
        )
        out(
            f"  bytes/sector {x.bytes_per_sector}, sectors/cluster {x.sectors_per_cluster} "
            f"({human_size(x.bytes_per_sector * x.sectors_per_cluster)}), clusters {x.cluster_count:,}, "
            f"volume length {x.volume_length:,} sectors, serial 0x{x.serial:08X}, {x.percent_in_use}% in use"
        )
        try:
            region = read_at(fh, part_offset, 11 * x.bytes_per_sector)
            stored = struct.unpack_from("<I", read_at(fh, part_offset + EXFAT_CHECKSUM_SECTOR * x.bytes_per_sector, SECTOR), 0)[0]
            r.exfat_checksum_ok = exfat_boot_checksum(region) == stored
            out(f"  boot region checksum: {'ok' if r.exfat_checksum_ok else 'MISMATCH'}")
            if not r.exfat_checksum_ok:
                r.notes.append("the exFAT boot region checksum does not match; the boot region was altered or is damaged")
        except EOFError:
            pass
        if x.dirty:
            r.problems.append("exFAT VolumeDirty is set (the camera never clears it); Android refuses to mount the card")
        if x.media_failure:
            r.problems.append("exFAT MediaFailure is set: the card has reported read or write errors; consider replacing it")
        return
    if boot.kind in ("fat12", "fat16", "fat32") and boot.fat is not None:
        f = boot.fat
        out(f"Boot sector at {where}: {describe_kind(boot.kind)} (type string {f.fs_type.strip()!r}, OEM {f.oem_name.strip()!r})")
        out(
            f"  bytes/sector {f.bytes_per_sector}, sectors/cluster {f.sectors_per_cluster} "
            f"({human_size(f.bytes_per_sector * f.sectors_per_cluster)}), reserved {f.reserved_sectors}, "
            f"FATs {f.num_fats}, total sectors {f.total_sectors:,} ({human_size(f.total_sectors * f.bytes_per_sector)})"
        )
        out(
            f"  FAT size {f.fat_size:,} sectors, root cluster {f.root_cluster}, FSInfo sector {f.fsinfo_sector}, "
            f"backup boot sector {f.backup_boot_sector}, clusters {f.cluster_count:,}, hidden sectors {f.hidden_sectors:,}"
        )
        out(
            f"  geometry: sectors/track {f.sectors_per_track}, heads {f.heads}, drive number 0x{f.drive_number:02X}, "
            f"extended boot signature 0x{f.ext_boot_sig:02X}, serial 0x{f.serial:08X}, label {f.label.strip()!r}"
        )
        if r.partition is not None and f.total_sectors * f.bytes_per_sector > r.partition.sectors * SECTOR:
            r.problems.append("the FAT volume says it is larger than its partition")
        if boot.kind == "fat32":
            if f.cluster_count < FAT32_MIN_CLUSTERS:
                r.problems.append(
                    f"FAT32 with only {f.cluster_count:,} clusters (minimum {FAT32_MIN_CLUSTERS:,}); some drivers will read it as FAT16"
                )
            if f.sectors_per_track == 0 or f.heads == 0:
                r.notes.append("geometry fields are zero (sectors/track or heads); picky firmware may dislike that")
            if f.drive_number != 0x80:
                r.notes.append(f"drive number is 0x{f.drive_number:02X}; 0x80 is the usual value for cards")
            if f.ext_boot_sig != 0x29:
                r.notes.append(f"extended boot signature is 0x{f.ext_boot_sig:02X}, expected 0x29")
            try:
                if f.backup_boot_sector:
                    backup = read_at(fh, part_offset + f.backup_boot_sector * f.bytes_per_sector, SECTOR)
                    r.backup_matches = backup == boot.raw
                    out(f"  backup boot sector (sector {f.backup_boot_sector}): {'identical' if r.backup_matches else 'DIFFERS'}")
                    if not r.backup_matches:
                        r.notes.append("the backup boot sector differs from the main one")
                if f.fsinfo_sector:
                    r.fsinfo = parse_fsinfo(read_at(fh, part_offset + f.fsinfo_sector * f.bytes_per_sector, SECTOR))
                    out(
                        f"  FSInfo: signatures {'ok' if r.fsinfo.signatures_ok else 'BAD'}, free clusters "
                        f"{r.fsinfo.free_count:,}, next free {r.fsinfo.next_free}"
                    )
                    if not r.fsinfo.signatures_ok:
                        r.notes.append("FSInfo signatures are wrong")
                r.fat_entries = read_fat_entries(fh, part_offset, f)
                out("  FAT[0..2]: " + ", ".join(f"0x{e:08X}" for e in r.fat_entries))
                if len(r.fat_entries) > 1 and not r.fat_entries[1] & 0x08000000:
                    r.notes.append("FAT[1] clean-shutdown bit is clear (Windows marks the volume dirty this way); run a filesystem check")
            except EOFError:
                r.notes.append("could not read the FAT32 reserved area; the partition is truncated")
        return
    out(f"Boot sector at {where}: not exFAT or FAT (first bytes {boot.raw[:8].hex()})")
    r.problems.append("the first partition holds no recognisable FAT or exFAT filesystem")


def _verdict(r: CheckResult) -> None:
    p = r.partition
    boot = r.boot
    if p is None or boot is None:
        return
    if boot.kind == "exfat" and boot.exfat is not None:
        if boot.exfat.dirty:
            r.platforms["Android"] = "refuses (dirty exFAT: offers to format instead)"
            for name in ("iPhone", "Mac", "Linux", "Windows"):
                r.platforms[name] = "mounts (they tolerate the dirty flag)"
            r.fixes.append("--clear-dirty")
            r.fixes.append("--format-fat32")
        else:
            r.platforms["Android"] = "mounts (until the camera sets the dirty flag again)"
            for name in ("iPhone", "Mac", "Linux", "Windows"):
                r.platforms[name] = "mounts"
            r.notes.append("exFAT is clean right now; the camera sets VolumeDirty again every time it writes. FAT32 is the durable fix")
            r.fixes.append("--format-fat32")
        if p.type != 0x07:
            r.notes.append(f"MBR type is 0x{p.type:02X}; 0x07 is the usual value for exFAT")
        return
    if boot.kind == "fat32":
        if p.type == 0x0C:
            for name in PLATFORMS:
                r.platforms[name] = "mounts"
        elif p.type == 0x0B:
            for name in PLATFORMS:
                r.platforms[name] = "mounts (probably)"
            r.notes.append("MBR type is 0x0B (FAT32 CHS); 0x0C (FAT32 LBA) is the value verified on iPhone. card-fix --fix-mbr-type sets it")
        else:
            r.problems.append(
                f"MBR partition type is 0x{p.type:02X} ({p.type_name}) but the volume is FAT32; iPhone needs 0x0C (FAT32 LBA)"
            )
            r.platforms["iPhone"] = f"refuses (MBR type 0x{p.type:02X} on a FAT32 volume)"
            for name in ("Android", "Mac", "Linux", "Windows"):
                r.platforms[name] = "mounts"
            r.fixes.append("--fix-mbr-type")
        return
    if boot.kind in ("fat12", "fat16"):
        for name in PLATFORMS:
            r.platforms[name] = "mounts"
        r.notes.append(f"{describe_kind(boot.kind)} works but is unusual for a card this size; the camera may reformat it")
        return
    for name in PLATFORMS:
        r.platforms[name] = "unknown"
    r.fixes.append("--format-fat32")


def format_report(r: CheckResult, target_text: str) -> str:
    """The printed report: what was found, the platform verdict and the fix commands."""
    lines = [f"Target: {target_text}, {r.size:,} bytes ({human_size(r.size)})"]
    lines.extend(r.lines)
    if r.platforms:
        lines.append("Platforms:")
        for name in PLATFORMS:
            lines.append(f"  {name:<8} {r.platforms.get(name, 'unknown')}")
    if r.notes:
        lines.append("Notes:")
        lines.extend(f"  - {n}" for n in r.notes)
    if r.problems:
        lines.append("Problems:")
        lines.extend(f"  - {p}" for p in r.problems)
    if r.fixes:
        lines.append("Fix:")
        for opt in r.fixes:
            hint = {
                "--clear-dirty": "quick; the camera will set the flag again on its next write",
                "--fix-mbr-type": "one byte in the MBR; keeps the files",
                "--format-fat32": "erases the card; back up the photos first; durable",
            }.get(opt, "")
            lines.append(f"  charmera card-fix {target_text} {opt}" + (f"   ({hint})" if hint else ""))
    if not r.problems:
        lines.append("Verdict: no problem found.")
    else:
        lines.append(f"Verdict: {len(r.problems)} problem(s) found.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# Finding and opening targets


@dataclass
class Target:
    given: str  # as typed, used in messages and in the suggested commands
    path: str  # what to open
    size: int
    is_image: bool
    description: str
    disk_number: Optional[int] = None  # Windows
    sys_name: Optional[str] = None  # Linux

    @property
    def command_text(self) -> str:
        return f"--image {self.given}" if self.is_image else self.given


_WIN_PHYSICAL = re.compile(r"^(?:\\\\[.?]\\)?physicaldrive(\d+)$", re.IGNORECASE)
_WIN_LETTER = re.compile(r"^([A-Za-z]):?\\?$")


def resolve_target(target: Optional[str], image: Optional[str]) -> Target:
    """Turn the command-line target into something with a path and a size, or raise CardError."""
    if image and target:
        raise CardError("give either a device or --image, not both")
    if image:
        if not os.path.isfile(image):
            raise CardError(f"image file not found: {image}")
        return Target(given=image, path=image, size=os.path.getsize(image), is_image=True, description="image file")
    if not target:
        raise CardError("no target: give a whole-disk device (/dev/sdX, /dev/mmcblkN, \\\\.\\PhysicalDriveN, E:) or --image FILE")
    if sys.platform.startswith("win"):
        return _resolve_windows(target)
    return _resolve_posix(target)


def _resolve_posix(target: str) -> Target:
    if not os.path.exists(target):
        raise CardError(f"device not found: {target}")
    st = os.stat(target)
    if stat.S_ISREG(st.st_mode):
        raise CardError(f"{target} is a regular file; use --image {target} for image files")
    if not stat.S_ISBLK(st.st_mode):
        raise CardError(f"{target} is not a block device")
    name = os.path.basename(os.path.realpath(target))
    if os.path.exists(f"/sys/class/block/{name}/partition"):
        parent = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{name}")))
        raise CardError(f"{target} is a partition; give the whole disk instead (/dev/{parent})")
    try:
        fd = os.open(target, os.O_RDONLY)
    except PermissionError:
        raise CardError(f"permission denied opening {target}; run with sudo")
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)
    if size <= 0:
        try:
            with open(f"/sys/class/block/{name}/size") as f:
                size = int(f.read().strip()) * SECTOR
        except (OSError, ValueError):
            size = 0
    if size <= 0:
        raise CardError(f"could not determine the size of {target}")
    model = _read_sys(f"/sys/class/block/{name}/device/model") or _read_sys(f"/sys/class/block/{name}/device/name") or ""
    desc = f"block device {name}" + (f" ({model})" if model else "")
    return Target(given=target, path=target, size=size, is_image=False, description=desc, sys_name=name)


def _read_sys(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _resolve_windows(target: str) -> Target:  # pragma: no cover - Windows only
    m = _WIN_PHYSICAL.match(target.strip())
    number: Optional[int] = None
    if m:
        number = int(m.group(1))
    else:
        lm = _WIN_LETTER.match(target.strip())
        if not lm:
            raise CardError(
                f"{target!r} is not a disk: use \\\\.\\PhysicalDriveN (the number from Disk Management or Get-Disk) or a drive letter like E:"
            )
        letter = lm.group(1).upper()
        try:
            number = windows_device_number(letter)
        except OSError as exc:
            raise CardError(
                f"could not map {letter}: to a physical drive ({exc}). Find the disk number in Disk Management "
                "(Win+X, Disk Management: 'Disk N' next to the card) or in PowerShell with Get-Disk, then use \\\\.\\PhysicalDriveN"
            )
        if number is None:
            raise CardError(f"{letter}: is not on a disk this tool can address (network drive or CD?)")
    path = f"\\\\.\\PhysicalDrive{number}"
    try:
        size = windows_disk_length(path)
    except OSError as exc:
        raise CardError(f"cannot open {path}: {exc}. Run from an administrator prompt and check the number in Disk Management")
    return Target(given=target, path=path, size=size, is_image=False, description=f"PhysicalDrive{number}", disk_number=number)


# --- Windows ctypes helpers (only ever called on Windows) ---

IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x70140
FSCTL_LOCK_VOLUME = 0x90018
FSCTL_UNLOCK_VOLUME = 0x9001C
FSCTL_DISMOUNT_VOLUME = 0x90020
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ_WRITE = 0x3
OPEN_EXISTING = 3
FILE_DEVICE_DISK = 7


def _kernel32():  # pragma: no cover - Windows only
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    k32.DeviceIoControl.restype = ctypes.c_int
    k32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return ctypes, k32


def _win_open(path: str, access: int):  # pragma: no cover - Windows only
    ctypes, k32 = _kernel32()
    h = k32.CreateFileW(path, access, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
    invalid = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
    if h is None or h == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _win_ioctl(handle, code: int, out_size: int = 0) -> bytes:  # pragma: no cover - Windows only
    ctypes, k32 = _kernel32()
    out = ctypes.create_string_buffer(out_size) if out_size else None
    returned = ctypes.c_uint32(0)
    ok = k32.DeviceIoControl(handle, code, None, 0, out, out_size, ctypes.byref(returned), None)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return out.raw[: returned.value] if out is not None else b""


def windows_device_number(letter: str) -> Optional[int]:  # pragma: no cover - Windows only
    """The PhysicalDrive number behind a drive letter, or None when it is not a disk."""
    _ctypes, k32 = _kernel32()
    h = _win_open(f"\\\\.\\{letter}:", 0)
    try:
        raw = _win_ioctl(h, IOCTL_STORAGE_GET_DEVICE_NUMBER, 12)
    finally:
        k32.CloseHandle(h)
    dev_type, number, _part = struct.unpack("<III", raw[:12])
    return number if dev_type == FILE_DEVICE_DISK else None


def windows_disk_length(path: str) -> int:  # pragma: no cover - Windows only
    _ctypes, k32 = _kernel32()
    h = _win_open(path, GENERIC_READ)
    try:
        raw = _win_ioctl(h, IOCTL_DISK_GET_LENGTH_INFO, 8)
    finally:
        k32.CloseHandle(h)
    return struct.unpack("<q", raw[:8])[0]


def windows_letters_on_disk(number: int) -> List[str]:  # pragma: no cover - Windows only
    letters = []
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        try:
            if windows_device_number(c) == number:
                letters.append(c)
        except OSError:
            continue
    return letters


class WindowsVolumeLocks:  # pragma: no cover - Windows only
    """Lock and dismount every lettered volume on a disk for the duration of a write."""

    def __init__(self, number: int):
        self.number = number
        self.handles: List[Tuple[str, object]] = []

    def __enter__(self):
        _ctypes, k32 = _kernel32()
        for letter in windows_letters_on_disk(self.number):
            h = _win_open(f"\\\\.\\{letter}:", GENERIC_READ | GENERIC_WRITE)
            try:
                _win_ioctl(h, FSCTL_LOCK_VOLUME)
                _win_ioctl(h, FSCTL_DISMOUNT_VOLUME)
            except OSError as exc:
                k32.CloseHandle(h)
                self.__exit__(None, None, None)
                raise CardError(
                    f"could not lock and dismount {letter}: ({exc}); close Explorer windows and programs using the card, then retry"
                )
            self.handles.append((letter, h))
        return self

    def __exit__(self, *_exc):
        _ctypes, k32 = _kernel32()
        for _letter, h in self.handles:
            try:
                _win_ioctl(h, FSCTL_UNLOCK_VOLUME)
            except OSError:
                pass
            k32.CloseHandle(h)
        self.handles = []


def windows_rescan(path: str) -> None:  # pragma: no cover - Windows only
    _ctypes, k32 = _kernel32()
    try:
        h = _win_open(path, GENERIC_READ | GENERIC_WRITE)
    except OSError:
        return
    try:
        _win_ioctl(h, IOCTL_DISK_UPDATE_PROPERTIES)
    except OSError:
        pass
    finally:
        k32.CloseHandle(h)


# --- Linux mount checks ---


def linux_partition_names(name: str, sys_root: str = "/sys") -> List[str]:
    base = os.path.join(sys_root, "block", name)
    try:
        return sorted(e for e in os.listdir(base) if e.startswith(name) and os.path.isdir(os.path.join(base, e)))
    except OSError:
        return []


def linux_mounts_for(device: str, sys_name: Optional[str] = None, mounts_path: str = "/proc/mounts", sys_root: str = "/sys") -> List[Tuple[str, str]]:
    """(source, mountpoint) pairs in /proc/mounts for the disk or any of its partitions."""
    real = os.path.realpath(device)
    name = sys_name or os.path.basename(real)
    dev_dir = os.path.dirname(real) or "/dev"
    targets = {real}
    for p in linux_partition_names(name, sys_root):
        targets.add(os.path.join(dev_dir, p))
    out = []
    try:
        with open(mounts_path) as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith("/"):
            continue
        src = fields[0]
        if os.path.realpath(src) in targets or src in targets:
            out.append((src, fields[1].replace("\\040", " ")))
    return out


def open_for_write(path: str) -> IO[bytes]:
    """Open a device or image unbuffered with O_SYNC where the platform has it; O_EXCL on Linux makes
    the kernel refuse a block device that is mounted or otherwise in use."""
    flags = os.O_RDWR | getattr(os, "O_SYNC", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    is_block = False
    try:
        is_block = stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        pass
    if is_block and not sys.platform.startswith("win"):
        flags |= os.O_EXCL
    fd = os.open(path, flags)
    return os.fdopen(fd, "r+b", buffering=0)


def reread_partition_table(fh: IO[bytes], target: Target) -> bool:
    if target.is_image:
        return True
    if fcntl is not None and target.sys_name:
        try:
            fcntl.ioctl(fh.fileno(), 0x125F)  # BLKRRPART
            return True
        except OSError:
            return False
    if sys.platform.startswith("win"):  # pragma: no cover
        windows_rescan(target.path)
        return True
    return False


# ----------------------------------------------------------------------------------------------
# Listing disks


@dataclass
class DiskInfo:
    path: str
    size: Optional[int]
    removable: Optional[bool]
    model: str
    mounted: List[str]
    bus: str = ""

    @property
    def card_candidate(self) -> Tuple[bool, str]:
        if self.removable is False:
            return False, "not removable"
        if self.removable is None:
            return False, "removable flag unknown"
        if self.size is not None and self.size > MAX_CARD_BYTES:
            return False, "larger than 512 GiB"
        return True, ""


_SKIP_LINUX = re.compile(r"^(loop|ram|zram|dm-|md|nbd|fd)\d*")


def list_disks_linux(sys_root: str = "/sys", mounts_path: str = "/proc/mounts", dev_root: str = "/dev") -> List[DiskInfo]:
    """Whole disks from /sys/block. An SD card in a built-in slot (mmcblk) reports removable=0, so the
    mmc card type is consulted instead for those."""
    out = []
    block = os.path.join(sys_root, "block")
    try:
        names = sorted(os.listdir(block))
    except OSError:
        return out
    for name in names:
        if _SKIP_LINUX.match(name):
            continue
        base = os.path.join(block, name)
        removable_txt = _read_sys(os.path.join(base, "removable"))
        removable: Optional[bool] = None if removable_txt is None else removable_txt == "1"
        size_txt = _read_sys(os.path.join(base, "size"))
        size = int(size_txt) * SECTOR if size_txt and size_txt.isdigit() else None
        vendor = _read_sys(os.path.join(base, "device", "vendor")) or ""
        model = _read_sys(os.path.join(base, "device", "model")) or _read_sys(os.path.join(base, "device", "name")) or ""
        model = " ".join(x for x in (vendor, model) if x)
        bus = ""
        if name.startswith("mmcblk"):
            mmc_type = _read_sys(os.path.join(base, "device", "type")) or ""
            bus = {"SD": "SD card", "MMC": "eMMC", "SDIO": "SDIO"}.get(mmc_type, "mmc")
            if mmc_type == "SD":
                removable = True
            elif mmc_type == "MMC":
                removable = False
        elif name.startswith("sr"):
            bus = "optical"
            removable = False
        elif name.startswith("nvme"):
            bus = "NVMe"
        dev_path = os.path.join(dev_root, name)
        mounted = [f"{src} on {mnt}" for src, mnt in linux_mounts_for(dev_path, name, mounts_path, sys_root)]
        out.append(DiskInfo(dev_path, size, removable, model, mounted, bus))
    return out


def _run(cmd: List[str], timeout: float = 20.0) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_wmic_csv(text: str) -> List[DiskInfo]:
    """Rows of `wmic diskdrive get Index,Model,Size,MediaType,InterfaceType /format:csv`."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    out = []
    if not lines:
        return out
    for row in csv.DictReader(lines):
        idx = (row.get("Index") or "").strip()
        if not idx.isdigit():
            continue
        size_txt = (row.get("Size") or "").strip()
        size = int(size_txt) if size_txt.isdigit() else None
        media = (row.get("MediaType") or "").strip()
        iface = (row.get("InterfaceType") or "").strip()
        removable: Optional[bool]
        if "removable" in media.lower() or iface.upper() == "USB":
            removable = True
        elif media:
            removable = False
        else:
            removable = None
        out.append(DiskInfo(f"\\\\.\\PhysicalDrive{idx}", size, removable, (row.get("Model") or "").strip(), [], iface or media))
    return out


def parse_get_disk_csv(text: str) -> List[DiskInfo]:
    """Rows of `Get-Disk | Select Number,FriendlyName,Size,BusType | ConvertTo-Csv -NoTypeInformation`."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    out = []
    if not lines:
        return out
    for row in csv.DictReader(lines):
        num = (row.get("Number") or "").strip()
        if not num.isdigit():
            continue
        size_txt = (row.get("Size") or "").strip()
        size = int(size_txt) if size_txt.isdigit() else None
        bus = (row.get("BusType") or "").strip()
        removable: Optional[bool]
        if bus.upper() in ("USB", "SD", "MMC"):
            removable = True
        elif bus:
            removable = False
        else:
            removable = None
        out.append(DiskInfo(f"\\\\.\\PhysicalDrive{num}", size, removable, (row.get("FriendlyName") or "").strip(), [], bus))
    return out


def list_disks_windows() -> List[DiskInfo]:  # pragma: no cover - Windows only
    disks: List[DiskInfo] = []
    text = _run(["wmic", "diskdrive", "get", "Index,Model,Size,MediaType,InterfaceType", "/format:csv"])
    if text:
        disks = parse_wmic_csv(text)
    if not disks:
        text = _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-Disk | Select-Object Number,FriendlyName,Size,BusType | ConvertTo-Csv -NoTypeInformation",
            ],
            timeout=60.0,
        )
        if text:
            disks = parse_get_disk_csv(text)
    for d in disks:
        m = _WIN_PHYSICAL.match(d.path)
        if m:
            try:
                d.mounted = [f"{letter}:" for letter in windows_letters_on_disk(int(m.group(1)))]
            except OSError:
                pass
    return disks


def format_disk_list(disks: List[DiskInfo]) -> str:
    if not disks:
        return "No disks found."
    lines = ["Whole disks (give one of these paths to card-check or card-fix):"]
    for d in disks:
        ok, why = d.card_candidate
        flag = "card?" if ok else f"not a card: {why}"
        rem = {True: "removable", False: "fixed", None: "removable?"}[d.removable]
        kind = d.bus or rem
        mounted = ", ".join(d.mounted) if d.mounted else "not mounted"
        lines.append(f"  {d.path:<24} {human_size(d.size):>10}  {kind:<10} {d.model or '-':<28} {mounted:<32} [{flag}]")
    lines.append("Only removable disks up to 512 GiB are treated as candidates. Mounted ones must be unmounted before card-fix.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# Commands


def _print(msg: str = "") -> None:
    print(msg)


def cmd_card_check(args: argparse.Namespace) -> int:
    if args.list:
        if sys.platform.startswith("win"):  # pragma: no cover
            disks = list_disks_windows()
        elif sys.platform.startswith("linux"):
            disks = list_disks_linux()
        else:
            _print(f"--list is not implemented for {sys.platform}; use `diskutil list` or the OS's disk tool")
            return 2
        _print(format_disk_list(disks))
        return 0
    try:
        target = resolve_target(args.target, args.image)
    except CardError as exc:
        _print(f"error: {exc}")
        return 2
    try:
        with open(target.path, "rb", buffering=0) as fh:
            result = check_disk(fh, target.size)
    except PermissionError:
        _print(f"error: permission denied reading {target.path}; run with sudo (Linux) or from an administrator prompt (Windows)")
        return 2
    except OSError as exc:
        _print(f"error: cannot read {target.path}: {exc}")
        return 2
    text = format_report(result, target.command_text)
    if not target.is_image and target.sys_name:
        mounts = linux_mounts_for(target.path, target.sys_name)
        if mounts:
            text += "\nMounted: " + "; ".join(f"{s} on {m}" for s, m in mounts) + " (unmount before card-fix)"
    if not target.is_image:
        _print(f"{target.description}:")
    _print(text)
    return result.exit_code


def _confirm(args: argparse.Namespace, target: Target, expected: str) -> bool:
    if args.yes and (args.i_know_what_i_am_doing or target.is_image):
        return True
    if args.yes and not target.is_image:
        _print("--yes on a real device also needs --i-know-what-i-am-doing; asking instead.")
    if not sys.stdin or not sys.stdin.isatty():
        _print("error: no terminal to confirm on. Re-run interactively, or pass --yes --i-know-what-i-am-doing.")
        return False
    try:
        typed = input(f"Type {expected} to continue, anything else to stop: ").strip()
    except EOFError:
        return False
    if typed != expected:
        _print("Not confirmed; nothing was written.")
        return False
    return True


def cmd_card_fix(args: argparse.Namespace) -> int:
    ops = [name for name, on in (("--clear-dirty", args.clear_dirty), ("--fix-mbr-type", args.fix_mbr_type), ("--format-fat32", args.format_fat32)) if on]
    if not ops:
        _print("error: nothing to do; give --clear-dirty, --fix-mbr-type or --format-fat32")
        return 2
    if args.format_fat32 and len(ops) > 1:
        _print("error: --format-fat32 replaces everything on the card; do not combine it with the other fixes")
        return 2
    try:
        target = resolve_target(args.target, args.image)
    except CardError as exc:
        _print(f"error: {exc}")
        return 2
    if target.size > MAX_CARD_BYTES:
        _print(f"error: {target.description} is {human_size(target.size)}, larger than 512 GiB; that is not a camera card. Refusing.")
        return 2

    # Mount check (Linux). Windows locks and dismounts below, at write time.
    if not target.is_image and target.sys_name:
        mounts = linux_mounts_for(target.path, target.sys_name)
        if mounts:
            _print("error: the card is mounted; unmount it first:")
            for src, mnt in mounts:
                _print(f"  {src} on {mnt}    ->  sudo umount {mnt}")
            return 2

    # Read-only look first, so the plan can be shown.
    try:
        with open(target.path, "rb", buffering=0) as fh:
            before = check_disk(fh, target.size)
    except OSError as exc:
        _print(f"error: cannot read {target.path}: {exc}")
        return 2

    planned: List[str] = []
    cluster_size: Optional[int] = None
    plan: Optional[Fat32Plan] = None
    if args.format_fat32:
        try:
            if args.cluster_size:
                cluster_size = parse_cluster_size(args.cluster_size)
            plan = plan_fat32(target.size, cluster_size=cluster_size, label=args.label)
        except CardError as exc:
            _print(f"error: {exc}")
            return 2
        planned.append(f"zero the first {human_size(min(ZERO_HEAD_BYTES, target.size))}" + (" and the backup GPT at the end" if before.mbr and before.mbr.gpt_protective else ""))
        planned.append(f"write an MBR with one partition: type 0x0C (FAT32 LBA), start LBA {plan.start_lba}, {plan.partition_sectors:,} sectors ({human_size(plan.partition_bytes)})")
        planned.append(
            f"write FAT32: label {plan.label!r}, {plan.cluster_bytes}-byte clusters, {plan.cluster_count:,} clusters, "
            f"{plan.num_fats} FATs of {plan.fat_sectors:,} sectors, reserved {plan.reserved_sectors}, FSInfo at 1, backup boot at 6, root at cluster 2"
        )
        planned.append("everything on the card is erased")
    else:
        if args.clear_dirty:
            if before.boot is not None and before.boot.kind == "exfat" and before.boot.exfat is not None:
                if before.boot.exfat.dirty:
                    planned.append(
                        f"clear VolumeDirty in the exFAT boot sector at LBA {before.part_offset // SECTOR:,} and in its backup (sector 12 of the volume); the checksum sector is not touched"
                    )
                else:
                    _print("--clear-dirty: the exFAT VolumeDirty flag is already clear; nothing to do for it")
            else:
                _print("--clear-dirty: the first partition is not exFAT; nothing to do for it")
        if args.fix_mbr_type:
            if before.boot is not None and before.boot.kind == "fat32" and before.partition is not None:
                if before.partition.type == FAT32_PARTITION_TYPE:
                    _print("--fix-mbr-type: the partition type is already 0x0C; nothing to do for it")
                else:
                    planned.append(f"set MBR partition {before.partition.index} type from 0x{before.partition.type:02X} to 0x0C (FAT32 LBA)")
            else:
                kind = before.boot.kind if before.boot else "nothing readable"
                _print(f"--fix-mbr-type: the first partition holds {describe_kind(kind)}, not FAT32; 0x0C would be wrong, skipping")
        if not planned:
            _print("Nothing to write.")
            return 0 if not before.problems else 1

    _print(f"Device:  {target.given}  ({target.description}, {target.size:,} bytes, {human_size(target.size)})")
    _print("Will write:")
    for p in planned:
        _print(f"  - {p}")
    expected = "FORMAT" if args.format_fat32 else target.given
    if not _confirm(args, target, expected):
        return 2

    try:
        if target.disk_number is not None:  # pragma: no cover - Windows only
            lock_ctx = WindowsVolumeLocks(target.disk_number)
        else:
            lock_ctx = _NoLock()
        with lock_ctx:
            with open_for_write(target.path) as fh:
                if args.format_fat32:
                    format_fat32(
                        fh,
                        target.size,
                        label=args.label,
                        cluster_size=cluster_size,
                        zero_gpt_tail=bool(before.mbr and before.mbr.gpt_protective),
                        log=lambda m: _print(f"  {m}"),
                    )
                else:
                    if args.clear_dirty and before.boot is not None and before.boot.kind == "exfat" and before.boot.exfat is not None and before.boot.exfat.dirty:
                        res = clear_exfat_dirty_bit(fh, before.part_offset or 0)
                        _print(f"  VolumeFlags 0x{res.old_flags:04X} -> 0x{res.new_flags:04X}, sectors written: {res.sectors_written}")
                    if args.fix_mbr_type and before.boot is not None and before.boot.kind == "fat32" and before.partition is not None and before.partition.type != FAT32_PARTITION_TYPE:
                        old, new = fix_mbr_type_for_fat32(fh)
                        _print(f"  MBR partition type 0x{old:02X} -> 0x{new:02X}")
                sync(fh)
                if not reread_partition_table(fh, target):
                    _print("  (the kernel kept the old partition table; unplug and replug the card)")
    except CardError as exc:
        _print(f"error: {exc}")
        return 2
    except PermissionError as exc:
        _print(f"error: permission denied: {exc}; run with sudo (Linux) or from an administrator prompt (Windows)")
        return 2
    except OSError as exc:
        if getattr(exc, "errno", None) == 16:
            _print(f"error: {target.path} is busy (mounted or in use); unmount it and retry")
            return 2
        _print(f"error: write failed: {exc}")
        return 1

    _print("Done. Checking the result:")
    try:
        with open(target.path, "rb", buffering=0) as fh:
            after = check_disk(fh, target.size)
    except OSError as exc:
        _print(f"error: cannot re-read {target.path}: {exc}")
        return 1
    _print(format_report(after, target.command_text))
    return after.exit_code


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def register(subparsers) -> None:
    check = subparsers.add_parser(
        "card-check",
        help="inspect a card's MBR and boot sector; never writes",
        description="Read the partition table and the first partition's boot sector and say which phones and "
        "computers will mount the card. Nothing is written.",
    )
    check.add_argument("target", nargs="?", help="whole disk: /dev/sdX, /dev/mmcblkN, \\\\.\\PhysicalDriveN or a drive letter like E:")
    check.add_argument("--image", metavar="FILE", help="inspect a disk image file instead of a device")
    check.add_argument("--list", action="store_true", help="list removable disks that could be the card")
    check.set_defaults(func=cmd_card_check)

    fix = subparsers.add_parser(
        "card-fix",
        help="clear the exFAT dirty bit, set the MBR type to 0x0C, or reformat as FAT32 (asks first)",
        description="Writes to the card. Prints exactly what it will do and waits for a typed confirmation.",
    )
    fix.add_argument("target", nargs="?", help="whole disk: /dev/sdX, /dev/mmcblkN, \\\\.\\PhysicalDriveN or a drive letter like E:")
    fix.add_argument("--image", metavar="FILE", help="operate on a disk image file instead of a device")
    fix.add_argument("--clear-dirty", action="store_true", help="exFAT: clear VolumeDirty in the main and backup boot sectors")
    fix.add_argument("--fix-mbr-type", action="store_true", help="FAT32: set the first partition's type byte to 0x0C")
    fix.add_argument("--format-fat32", action="store_true", help="erase the card: new MBR (type 0x0C) and a fresh FAT32 volume")
    fix.add_argument("--label", default="CHARMERA", help="volume label for --format-fat32 (default CHARMERA)")
    fix.add_argument("--cluster-size", metavar="N", help="cluster size for --format-fat32, e.g. 4096 or 32K (default by volume size)")
    fix.add_argument("--yes", action="store_true", help="skip the typed confirmation (images); devices also need --i-know-what-i-am-doing")
    fix.add_argument("--i-know-what-i-am-doing", action="store_true", help="with --yes: write to a real device without asking")
    fix.set_defaults(func=cmd_card_fix)
