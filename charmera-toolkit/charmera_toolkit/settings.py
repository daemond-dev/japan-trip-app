"""The camera's settings store.

Layout (verified in both public dumps and in the firmware's loader, writer, setter and getter code):

- The store is a 64 KiB bank at flash offset 0x170000 made of 128 sectors of 512 bytes. A record is one
  sector: 252 bytes of settings, the CRC32 (zlib polynomial, little-endian) of those bytes at 0xFC, and a
  second 256-byte page of zeros. Unused sectors are 0xFF.
- The firmware appends a record at the first erased sector whenever a setting changes, and on boot takes
  the sector just before the first erased one as current. When the first erased sector is number 127 the
  bank is erased and writing restarts, so it holds at most 127 records and has no sequence numbers.
- Sector 120 of this bank (0x17F000) is also the internal photo directory, which the firmware treats as a
  settings record after 120 saves (a stock bug: it then falls back to defaults and blanks the directory).
- Byte +0x19 is the UI-sound setting: 0 disables every event sound (power on, power off, keys, shutter),
  1..8 is their volume. Factory default 5. The stock Charmera menu never changes it.
- Byte +0x05 is the general playback volume, 0..8, default 5. Byte +0x01 selects the auto power-off time
  and byte +0x51 the standby time (see FIELD_NAMES).
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import List, Optional

SETTINGS_BASE = 0x170000
SETTINGS_END = 0x17F000      # the bank runs to 0x180000; 0x17F000 is sector 120, the photo directory
BANK_END = 0x180000
RECORD_SIZE = 0x100
RECORD_STRIDE = 0x200        # one record per 512-byte sector
SECTOR_SIZE = 0x200
CRC_LEN = 252
PHOTO_DIRECTORY = 0x17F000
PHOTO_DIRECTORY_LEN = 0x200  # the firmware writes two zeroed 256-byte pages here

OFF_PLAYBACK_VOLUME = 0x05
OFF_UI_SOUND = 0x19

FIELD_NAMES = {
    0x00: "index/counter (incremented by the setter)",
    0x01: "auto power-off: 1 = 3 min, 2 = 5 min, n >= 3 = n min (default 2)",
    0x05: "playback volume 0..8 (default 5)",
    0x12: "field 0x12 (changes in the dumps; frame/filter related)",
    0x19: "UI sound volume 0..8, 0 = all event sounds off (default 5)",
    0x29: "field 0x29 (default 4)",
    0x3e: "field 0x3e (default 15)",
    0x51: "standby after n minutes idle: speaker amp off, audio stopped (default 2)",
    0x52: "field 0x52 (changes in the dumps)",
    0x53: "field 0x53 (changes in the dumps)",
    0xdf: "field 0xdf (default 9)",
}

OFF_AUTO_OFF = 0x01
OFF_STANDBY = 0x51


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass
class SettingsRecord:
    offset: int
    raw: bytes

    @property
    def stored_crc(self) -> int:
        return int.from_bytes(self.raw[CRC_LEN:RECORD_SIZE], "little")

    @property
    def crc_ok(self) -> bool:
        return crc32(self.raw[:CRC_LEN]) == self.stored_crc

    @property
    def ui_sound(self) -> int:
        return self.raw[OFF_UI_SOUND]

    @property
    def playback_volume(self) -> int:
        return self.raw[OFF_PLAYBACK_VOLUME]

    def with_byte(self, offset: int, value: int) -> "SettingsRecord":
        """Copy with one byte changed and the CRC recomputed."""
        if not 0 <= offset < CRC_LEN:
            raise ValueError("offset must be inside the 252 CRC-covered bytes")
        body = bytearray(self.raw[:CRC_LEN])
        body[offset] = value & 0xFF
        return SettingsRecord(self.offset, bytes(body) + crc32(bytes(body)).to_bytes(4, "little"))

    def sector(self) -> bytes:
        """The 512-byte sector the firmware writes: record plus a zero second page."""
        return self.raw + b"\x00" * (SECTOR_SIZE - RECORD_SIZE)


def is_erased_sector(image: bytes, off: int) -> bool:
    """The firmware's test: a sector is erased when its first 8 bytes are 0xFF."""
    return image[off : off + 8] == b"\xff" * 8


def parse_settings(image: bytes) -> List[SettingsRecord]:
    """All non-erased records in order of appearance, up to (not including) the photo directory sector."""
    out = []
    for off in range(SETTINGS_BASE, SETTINGS_END, RECORD_STRIDE):
        raw = image[off : off + RECORD_SIZE]
        if len(raw) < RECORD_SIZE or is_erased_sector(image, off):
            continue
        out.append(SettingsRecord(off, raw))
    return out


def firmware_current_sector(image: bytes) -> Optional[int]:
    """Offset of the sector the firmware would load: the one before the first erased sector of the bank.

    Returns None when sector 0 is erased (the firmware then applies defaults). Note that the scan does not
    stop at the photo directory: that is the stock bug described in the module docstring.
    """
    for off in range(SETTINGS_BASE, BANK_END, SECTOR_SIZE):
        if is_erased_sector(image, off):
            return None if off == SETTINGS_BASE else off - SECTOR_SIZE
    return BANK_END - SECTOR_SIZE


def current_record(image: bytes) -> SettingsRecord:
    """The record the firmware loads at boot, which must also pass its CRC.

    Raises ValueError when the bank is blank, when the firmware would pick the photo directory sector
    (120 or more records), or when the selected sector fails the CRC (the firmware then uses defaults).
    """
    off = firmware_current_sector(image)
    if off is None:
        raise ValueError("settings bank is blank; the camera would apply defaults")
    if off >= PHOTO_DIRECTORY:
        raise ValueError("the firmware would read the photo directory sector as a settings record (120+ records)")
    rec = SettingsRecord(off, image[off : off + RECORD_SIZE])
    if not rec.crc_ok:
        raise ValueError(f"settings record at {off:#x} fails its CRC; the camera would apply defaults")
    return rec


def rewrite_store(image: bytes, record: SettingsRecord) -> bytes:
    """Return a copy of the image whose settings bank holds only this record (sector 0), with the photo
    directory sector zeroed if it is erased, which is what the firmware itself writes on a reset."""
    buf = bytearray(image)
    buf[SETTINGS_BASE:SETTINGS_END] = b"\xff" * (SETTINGS_END - SETTINGS_BASE)
    buf[SETTINGS_BASE : SETTINGS_BASE + SECTOR_SIZE] = record.sector()
    if buf[PHOTO_DIRECTORY : PHOTO_DIRECTORY + 8] == b"\xff" * 8:
        buf[PHOTO_DIRECTORY : PHOTO_DIRECTORY + PHOTO_DIRECTORY_LEN] = b"\x00" * PHOTO_DIRECTORY_LEN
    return bytes(buf)


def photo_directory_consistent(image: bytes) -> bool:
    """False when a valid settings record exists but the photo directory sector is 0xFF (the directory
    loader would then see two used entries of length 0xFFFFFFFF)."""
    if len(image) < PHOTO_DIRECTORY + 8:
        return True
    try:
        current_record(image)
    except ValueError:
        return True
    return image[PHOTO_DIRECTORY : PHOTO_DIRECTORY + 8] != b"\xff" * 8


def diff_records(a: SettingsRecord, b: SettingsRecord) -> List[int]:
    """Offsets whose bytes differ between two records (CRC bytes excluded)."""
    return [i for i in range(CRC_LEN) if a.raw[i] != b.raw[i]]
