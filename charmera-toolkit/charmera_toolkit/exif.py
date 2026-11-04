"""exif-fix: repair the EXIF the Kodak Charmera writes, and the date inside its videos, on copies only.

What the camera gets wrong, as verified by two community projects whose logic this module ports
(jphastings/charmera, Go, and RickCaleg/charmera-importer, C#):

- JPEG dates are "YYYY:MM:DD:HH:MM:SS", a colon where the space belongs. Readers reject the value and the
  capture time is lost, so photos sort by import date.
- PixelXDimension/PixelYDimension say 640x480 while the frame is 1440x1080.
- The MakerNote offset points past the end of the EXIF block, so editors refuse to write the file.
- Make/Model are "Generalplus"/"CBB3": the chip, not the camera.
- Every AVI carries the same recording date, 2010-06-29, in its text date chunks (IDIT in the header list,
  ICRD in the INFO list). The file's modification time on the card is the real recording time.

The repair never edits the camera's EXIF block in place because its offsets cannot be trusted. It reads the
true dimensions from the JPEG frame header, salvages orientation, make, model and the dates from the old
block with a bounds-checked reader that ignores the MakerNote, then writes a fresh little-endian EXIF APP1
and splices it where the old one was (or right after the JFIF APP0 when there was none). Every other byte
of the JPEG, including the compressed image data, is copied unchanged. For videos the date chunks are
rewritten in the copy in place, same text form and same byte length, so nothing else in the file moves.

Standard library only. ffmpeg is optional and only used for --video transcode.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO, List, Optional, Set, Tuple

# ---- Known facts stamped into the rebuilt EXIF ---------------------------------------------------------
# A 1/4" CMOS sensor behind a fixed 35 mm-equivalent f/2.4 lens (manufacturer spec sheet). The real focal
# length is not published, so only the 35 mm equivalent is written.

CAMERA_MAKE = "Kodak"
CAMERA_MODEL = "Charmera"
LENS_MAKE = "Kodak"
LENS_MODEL = "Charmera fixed lens (35mm-equiv f/2.4)"
FNUMBER = (24, 10)  # f/2.4 as a RATIONAL
FOCAL_LENGTH_35MM = 35

# What the camera itself writes, kept with --keep-make.
CHIP_MAKE = "Generalplus"
CHIP_MODEL = "CBB3"

# The Generalplus encoder leaves this comment in every JPEG; it identifies a Charmera card regardless of
# what the volume or the files were renamed to.
GPENCODER_SIGNATURE = b"GPEncoder"
SIGNATURE_SAMPLE_SIZE = 4096
MAX_SIGNATURE_FILES = 5
SPI_DIR_NAME = "SPIDCIM"

# The hard-coded recording date the camera writes into every AVI.
BOGUS_AVI_DATE = date(2010, 6, 29)
MAX_AVI_DATE_FIELD = 64

EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
FFMPEG_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
HASH_TOKEN_LEN = 16

_VALID_DATE_RE = re.compile(r"^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$")
_MALFORMED_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}):(\d{2}):(\d{2}):(\d{2})$")
_HASH_TOKEN_RE = re.compile(r"_([0-9a-fA-F]{16})$")

EXIF_HEADER = b"Exif\x00\x00"

# TIFF field types.
TYPE_ASCII = 2
TYPE_SHORT = 3
TYPE_LONG = 4
TYPE_RATIONAL = 5
TYPE_UNDEFINED = 7

# IFD0 and Exif IFD tags.
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_ORIENTATION = 0x0112
TAG_DATETIME = 0x0132
TAG_YCBCR_POSITIONING = 0x0213
TAG_EXIF_IFD = 0x8769
TAG_FNUMBER = 0x829D
TAG_EXIF_VERSION = 0x9000
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_COMPONENTS_CONFIG = 0x9101
TAG_FLASHPIX_VERSION = 0xA000
TAG_COLOR_SPACE = 0xA001
TAG_PIXEL_X = 0xA002
TAG_PIXEL_Y = 0xA003
TAG_FOCAL_LENGTH_35MM = 0xA405
TAG_LENS_MAKE = 0xA433
TAG_LENS_MODEL = 0xA434

PHOTO_EXTS = (".jpg", ".jpeg")
VIDEO_EXTS = (".avi",)


# ---- JPEG segment scan --------------------------------------------------------------------------------


@dataclass
class JpegLayout:
    """Where the Exif APP1 segment is (or should go) and the frame's true pixel size.

    exif_start..exif_end delimit an existing APP1 Exif segment, marker included. When they are equal there is
    no such segment and that offset is the insertion point: right after SOI, or after a leading JFIF APP0.
    """

    width: int = 0
    height: int = 0
    exif_start: int = 2
    exif_end: int = 2
    exif_found: bool = False


def _is_sof(marker: int) -> bool:
    """Start-Of-Frame markers carry the dimensions. DHT (C4), JPG (C8) and DAC (CC) do not."""
    return 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)


def scan_jpeg(data: bytes) -> JpegLayout:
    """Walk the marker segments up to the start of scan data. Raises ValueError when the data is not a JPEG."""
    if len(data) < 2 or data[0] != 0xFF or data[1] != 0xD8:
        raise ValueError("not a JPEG (missing SOI marker)")
    layout = JpegLayout()
    n = len(data)
    pos = 2
    while pos + 1 < n:
        if data[pos] != 0xFF:
            break
        while pos + 1 < n and data[pos + 1] == 0xFF:
            pos += 1  # fill bytes before a marker
        if pos + 1 >= n:
            break
        marker = data[pos + 1]
        if marker == 0xD9 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2  # standalone markers, no length field
            continue
        if marker == 0xDA:
            break  # start of scan: entropy-coded data follows
        if pos + 4 > n:
            break
        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if seg_len < 2:
            break
        seg_start = pos
        seg_end = pos + 2 + seg_len
        if seg_end > n:
            break
        payload = data[pos + 4 : seg_end]
        if marker == 0xE1 and payload[:6] == EXIF_HEADER:
            if not layout.exif_found:
                layout.exif_start, layout.exif_end = seg_start, seg_end
                layout.exif_found = True
        elif marker == 0xE0 and not layout.exif_found:
            layout.exif_start = layout.exif_end = seg_end
        elif _is_sof(marker) and len(payload) >= 5:
            layout.height, layout.width = struct.unpack(">HH", payload[1:5])
        pos = seg_end
    return layout


def replace_exif(data: bytes, layout: JpegLayout, payload: bytes) -> bytes:
    """A copy of data with its Exif APP1 replaced by (or, if absent, inserted with) one wrapping payload."""
    seg_len = len(payload) + 2
    if seg_len > 0xFFFF:
        raise ValueError(f"rebuilt EXIF segment too large ({len(payload)} bytes)")
    segment = b"\xff\xe1" + struct.pack(">H", seg_len) + payload
    return data[: layout.exif_start] + segment + data[layout.exif_end :]


# ---- Tolerant TIFF/EXIF reader -----------------------------------------------------------------------


@dataclass
class ExifInfo:
    """The subset of EXIF read from a source file. Every field is best-effort: anything out of range just stays
    empty, because the camera's IFD is never trusted. The MakerNote is not looked at."""

    present: bool = False
    orientation: int = 0
    make: str = ""
    model: str = ""
    date_time: str = ""
    date_time_original: str = ""
    date_time_digitized: str = ""


def read_exif(tiff: bytes) -> ExifInfo:
    """Parse the TIFF block that follows "Exif\\0\\0", little or big endian, for the handful of tags needed."""
    info = ExifInfo()
    if len(tiff) < 8:
        return info
    if tiff[:2] == b"II":
        order = "<"
    elif tiff[:2] == b"MM":
        order = ">"
    else:
        return info
    if struct.unpack(order + "H", tiff[2:4])[0] != 0x2A:
        return info
    info.present = True
    ifd0 = struct.unpack(order + "I", tiff[4:8])[0]
    _read_dir(tiff, order, ifd0, info, in_exif=False)
    return info


def _read_dir(tiff: bytes, order: str, off: int, info: ExifInfo, in_exif: bool) -> None:
    if off < 0 or off + 2 > len(tiff):
        return
    count = struct.unpack(order + "H", tiff[off : off + 2])[0]
    pos = off + 2
    for _ in range(count):
        if pos + 12 > len(tiff):
            return
        tag, typ = struct.unpack(order + "HH", tiff[pos : pos + 4])
        cnt = struct.unpack(order + "I", tiff[pos + 4 : pos + 8])[0]
        value = tiff[pos + 8 : pos + 12]
        pos += 12
        if not in_exif:
            if tag == TAG_ORIENTATION and typ == TYPE_SHORT:
                info.orientation = struct.unpack(order + "H", value[:2])[0]
            elif tag == TAG_MAKE:
                info.make = _read_ascii(tiff, order, value, typ, cnt)
            elif tag == TAG_MODEL:
                info.model = _read_ascii(tiff, order, value, typ, cnt)
            elif tag == TAG_DATETIME:
                info.date_time = _read_ascii(tiff, order, value, typ, cnt)
            elif tag == TAG_EXIF_IFD and typ == TYPE_LONG:
                _read_dir(tiff, order, struct.unpack(order + "I", value)[0], info, in_exif=True)
        else:
            if tag == TAG_DATETIME_ORIGINAL:
                info.date_time_original = _read_ascii(tiff, order, value, typ, cnt)
            elif tag == TAG_DATETIME_DIGITIZED:
                info.date_time_digitized = _read_ascii(tiff, order, value, typ, cnt)


def _read_ascii(tiff: bytes, order: str, value: bytes, typ: int, cnt: int) -> str:
    if typ != TYPE_ASCII or cnt <= 0:
        return ""
    if cnt <= 4:
        raw = value[:cnt]
    else:
        off = struct.unpack(order + "I", value)[0]
        if off + cnt > len(tiff):
            return ""
        raw = tiff[off : off + cnt]
    return raw.decode("ascii", errors="replace").strip("\x00 ")


# ---- Minimal little-endian TIFF writer ---------------------------------------------------------------


@dataclass
class ExifData:
    """The corrected metadata to embed. Empty optional fields are left out of the output."""

    width: int
    height: int
    date_time: str  # standard EXIF form "YYYY:MM:DD HH:MM:SS"
    orientation: int = 1
    make: str = ""
    model: str = ""
    lens_make: str = ""
    lens_model: str = ""
    fnumber: Tuple[int, int] = (0, 0)  # (numerator, denominator); a zero denominator omits the tag
    focal_length_35mm: int = 0  # millimetres; 0 omits the tag


@dataclass
class _Entry:
    tag: int
    typ: int
    count: int
    inline: bytes = b"\x00\x00\x00\x00"
    external: Optional[bytes] = None


def _ascii_entry(tag: int, text: str) -> _Entry:
    raw = text.encode("ascii", errors="replace") + b"\x00"
    if len(raw) <= 4:
        return _Entry(tag, TYPE_ASCII, len(raw), raw.ljust(4, b"\x00"))
    count = len(raw)
    if len(raw) % 2:
        raw += b"\x00"  # word-align external values
    return _Entry(tag, TYPE_ASCII, count, external=raw)


def _short_entry(tag: int, value: int) -> _Entry:
    return _Entry(tag, TYPE_SHORT, 1, struct.pack("<H", value) + b"\x00\x00")


def _long_entry(tag: int, value: int) -> _Entry:
    return _Entry(tag, TYPE_LONG, 1, struct.pack("<I", value))


def _undefined_entry(tag: int, raw: bytes) -> _Entry:
    if len(raw) > 4:
        raise ValueError("inline UNDEFINED values must be at most 4 bytes")
    return _Entry(tag, TYPE_UNDEFINED, len(raw), raw.ljust(4, b"\x00"))


def _rational_entry(tag: int, num: int, den: int) -> _Entry:
    return _Entry(tag, TYPE_RATIONAL, 1, external=struct.pack("<II", num, den))


def _ifd_size(n: int) -> int:
    return 2 + 12 * n + 4


def encode_exif(d: ExifData) -> bytes:
    """A complete APP1 payload: "Exif\\0\\0" followed by a fresh little-endian TIFF holding only the corrected
    tags. Building from scratch is what drops the camera's corrupt MakerNote. Entries are in ascending tag order
    and the mandatory Exif IFD tags are present so strict validators stay quiet."""
    ifd0: List[_Entry] = []
    if d.make:
        ifd0.append(_ascii_entry(TAG_MAKE, d.make))
    if d.model:
        ifd0.append(_ascii_entry(TAG_MODEL, d.model))
    orientation = d.orientation if d.orientation >= 1 else 1
    ifd0.extend(
        [
            _short_entry(TAG_ORIENTATION, orientation),
            _ascii_entry(TAG_DATETIME, d.date_time),
            _short_entry(TAG_YCBCR_POSITIONING, 1),  # centered
            _long_entry(TAG_EXIF_IFD, 0),  # patched below
        ]
    )

    exif_ifd: List[_Entry] = []
    if d.fnumber[1] != 0:
        exif_ifd.append(_rational_entry(TAG_FNUMBER, d.fnumber[0], d.fnumber[1]))
    exif_ifd.extend(
        [
            _undefined_entry(TAG_EXIF_VERSION, b"0230"),
            _ascii_entry(TAG_DATETIME_ORIGINAL, d.date_time),
            _ascii_entry(TAG_DATETIME_DIGITIZED, d.date_time),
            _undefined_entry(TAG_COMPONENTS_CONFIG, bytes([1, 2, 3, 0])),  # Y, Cb, Cr, -
            _undefined_entry(TAG_FLASHPIX_VERSION, b"0100"),
            _short_entry(TAG_COLOR_SPACE, 1),  # sRGB
            _long_entry(TAG_PIXEL_X, d.width),
            _long_entry(TAG_PIXEL_Y, d.height),
        ]
    )
    if d.focal_length_35mm:
        exif_ifd.append(_short_entry(TAG_FOCAL_LENGTH_35MM, d.focal_length_35mm))
    if d.lens_make:
        exif_ifd.append(_ascii_entry(TAG_LENS_MAKE, d.lens_make))
    if d.lens_model:
        exif_ifd.append(_ascii_entry(TAG_LENS_MODEL, d.lens_model))

    header_size = 8
    exif_ifd_offset = header_size + _ifd_size(len(ifd0))
    ext_base = exif_ifd_offset + _ifd_size(len(exif_ifd))
    ifd0[-1].inline = struct.pack("<I", exif_ifd_offset)

    ext = bytearray()
    for entry in ifd0 + exif_ifd:
        if entry.external is None:
            continue
        entry.inline = struct.pack("<I", ext_base + len(ext))
        ext += entry.external

    out = bytearray(EXIF_HEADER)
    out += b"II" + struct.pack("<HI", 0x2A, header_size)
    for entries in (ifd0, exif_ifd):
        out += struct.pack("<H", len(entries))
        for entry in entries:
            out += struct.pack("<HHI", entry.tag, entry.typ, entry.count) + entry.inline
        out += struct.pack("<I", 0)  # no next IFD
    out += ext
    return bytes(out)


# ---- Dates -------------------------------------------------------------------------------------------


def normalize_exif_date(raw: str) -> Optional[str]:
    """A standard "YYYY:MM:DD HH:MM:SS", repairing the Charmera's all-colons form. None for empty, zeroed or
    unrecognisable values."""
    raw = raw.strip("\x00 ")
    if not raw or raw.startswith("0000"):
        return None
    if _VALID_DATE_RE.match(raw):
        return raw
    m = _MALFORMED_DATE_RE.match(raw)
    if m:
        return "{}:{}:{} {}:{}:{}".format(*m.groups())
    return None


def pick_date(info: ExifInfo, fallback: datetime) -> Tuple[str, str, bool]:
    """The best capture time: (date text, "exif" or "mtime", whether the extra colon was repaired).

    DateTimeOriginal is preferred, then DateTimeDigitized, then DateTime. With no usable EXIF date the fallback
    (the file's modification time) is used.
    """
    for raw in (info.date_time_original, info.date_time_digitized, info.date_time):
        fixed = normalize_exif_date(raw)
        if fixed is not None:
            return fixed, "exif", fixed != raw.strip("\x00 ")
    return fallback.strftime(EXIF_DATE_FORMAT), "mtime", False


# ---- The JPEG fix ------------------------------------------------------------------------------------


@dataclass
class FixResult:
    width: int
    height: int
    orientation: int
    date_time: str
    date_source: str  # "exif" or "mtime"
    date_repaired: bool  # the EXIF date had the extra colon
    exif_was_present: bool
    make: str
    model: str
    old_make: str = ""
    old_model: str = ""


def fix_jpeg(data: bytes, fallback: datetime, keep_make: bool = False, orientation_override: int = 0) -> Tuple[bytes, FixResult]:
    """The repaired JPEG and what was written. Raises ValueError when data is not a usable JPEG.

    keep_make keeps the Make/Model the camera wrote ("Generalplus"/"CBB3") instead of "Kodak"/"Charmera".
    orientation_override, when 1..8, replaces the source's Orientation (for an external detector); 0 keeps it.
    """
    layout = scan_jpeg(data)
    if layout.width == 0 or layout.height == 0:
        raise ValueError("could not read image dimensions from the JPEG frame header")

    info = ExifInfo()
    if layout.exif_found:
        payload = data[layout.exif_start + 4 : layout.exif_end]
        if len(payload) >= 6:
            info = read_exif(payload[6:])

    date_text, source, repaired = pick_date(info, fallback)
    orientation = orientation_override if 1 <= orientation_override <= 8 else info.orientation
    if orientation < 1:
        orientation = 1

    if keep_make:
        make = info.make or CAMERA_MAKE
        model = info.model or CAMERA_MODEL
    else:
        make, model = CAMERA_MAKE, CAMERA_MODEL

    payload = encode_exif(
        ExifData(
            width=layout.width,
            height=layout.height,
            date_time=date_text,
            orientation=orientation,
            make=make,
            model=model,
            lens_make=LENS_MAKE,
            lens_model=LENS_MODEL,
            fnumber=FNUMBER,
            focal_length_35mm=FOCAL_LENGTH_35MM,
        )
    )
    out = replace_exif(data, layout, payload)
    return out, FixResult(
        width=layout.width,
        height=layout.height,
        orientation=orientation,
        date_time=date_text,
        date_source=source,
        date_repaired=repaired,
        exif_was_present=info.present,
        make=make,
        model=model,
        old_make=info.make,
        old_model=info.model,
    )


# ---- AVI: the hard-coded 2010-06-29 ------------------------------------------------------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_ASCTIME_RE = re.compile(r"^([A-Za-z]{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})$")


@dataclass
class AviDateField:
    """A date stored as text inside the AVI: where it is and how much room it has, so it can be rewritten in
    place without moving anything else."""

    chunk_id: str
    offset: int
    size: int
    value: str


@dataclass
class AviInfo:
    width: int = 0
    height: int = 0
    usec_per_frame: int = 0
    total_frames: int = 0
    date_fields: List[AviDateField] = field(default_factory=list)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.usec_per_frame > 0 and self.total_frames > 0:
            return self.usec_per_frame * self.total_frames / 1_000_000
        return None


def parse_avi_date(raw: str) -> Optional[datetime]:
    """The forms AVI writers use for IDIT/ICRD: C asctime ("Tue Jun 29 12:00:00 2010"), EXIF-style
    "2010:06:29 12:00:00", and ISO dates. The weekday is redundant and is ignored, so a writer that gets it
    wrong does not make the whole date unreadable. Month names are matched in English, independent of locale."""
    text = re.sub(r"\s+", " ", raw.strip("\x00 \r\n"))
    text = re.sub(r"^[A-Za-z]{3} (?=[A-Za-z]{3} )", "", text)
    m = _ASCTIME_RE.match(text)
    if m:
        month = [name.lower() for name in _MONTHS]
        if m.group(1).lower() in month:
            try:
                return datetime(
                    int(m.group(6)), month.index(m.group(1).lower()) + 1, int(m.group(2)),
                    int(m.group(3)), int(m.group(4)), int(m.group(5)),
                )
            except ValueError:
                return None
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_avi_date_like(original: str, when: datetime) -> str:
    """when, in the same style as original (asctime, EXIF or ISO; trailing newline kept)."""
    text = original.rstrip("\x00")
    newline = "\n" if text.endswith("\n") else ""
    core = text.strip()
    if re.match(r"^[A-Za-z]{3} ", core):
        # asctime pads a single-digit day with a space ("Jun  9").
        formatted = "{} {} {:>2} {:02d}:{:02d}:{:02d} {:04d}".format(
            _DAYS[when.weekday()], _MONTHS[when.month - 1], when.day, when.hour, when.minute, when.second, when.year
        )
    elif re.match(r"^\d{4}:\d{2}:\d{2}", core):
        formatted = when.strftime("%Y:%m:%d %H:%M:%S")
    elif len(core) <= 10:
        formatted = when.strftime("%Y-%m-%d")
    else:
        formatted = when.strftime("%Y-%m-%dT%H:%M:%S" if "T" in core else "%Y-%m-%d %H:%M:%S")
    return formatted + newline


def is_avi(header: bytes) -> bool:
    return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"AVI "


def read_avi(fh: BinaryIO) -> AviInfo:
    """Headers only, with seeks; the frames are never read. Raises ValueError when the stream is not an AVI."""
    fh.seek(0, os.SEEK_END)
    length = fh.tell()
    fh.seek(0)
    header = fh.read(12)
    if not is_avi(header):
        raise ValueError("not an AVI file")
    info = AviInfo()
    riff_end = min(length, 8 + struct.unpack("<I", header[4:8])[0])
    _walk_riff(fh, 12, riff_end, info, depth=0)
    return info


def _walk_riff(fh: BinaryIO, start: int, end: int, info: AviInfo, depth: int) -> None:
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        chunk_id = header[:4]
        size = struct.unpack("<I", header[4:8])[0]
        data_start = pos + 8
        data_end = min(end, data_start + size)
        if chunk_id in (b"LIST", b"RIFF") and size >= 4 and depth < 4:
            list_type = fh.read(4)
            if list_type != b"movi":  # frames: nothing to read there
                _walk_riff(fh, data_start + 4, data_end, info, depth + 1)
        else:
            _read_leaf(fh, chunk_id, data_start, min(size, 0x7FFFFFFF), info)
        pos = data_start + size + (size & 1)  # chunks are word-aligned


def _read_leaf(fh: BinaryIO, chunk_id: bytes, data_start: int, size: int, info: AviInfo) -> None:
    if chunk_id == b"avih" and size >= 40:
        fh.seek(data_start)
        avih = fh.read(40)
        if len(avih) == 40:
            info.usec_per_frame = struct.unpack("<I", avih[0:4])[0]
            info.total_frames = struct.unpack("<I", avih[16:20])[0]
            info.width = struct.unpack("<I", avih[32:36])[0]
            info.height = struct.unpack("<I", avih[36:40])[0]
    elif chunk_id in (b"IDIT", b"ICRD") and 0 < size <= MAX_AVI_DATE_FIELD:
        fh.seek(data_start)
        raw = fh.read(size)
        if len(raw) == size:
            value = raw.decode("ascii", errors="replace")
            info.date_fields.append(AviDateField(chunk_id.decode("ascii"), data_start, size, value))


def has_bogus_avi_date(info: AviInfo) -> bool:
    """True when the file carries the Charmera's hard-coded fake date."""
    for f in info.date_fields:
        parsed = parse_avi_date(f.value)
        if parsed is not None and parsed.date() == BOGUS_AVI_DATE:
            return True
    return False


def patch_avi_dates(fh: BinaryIO, info: AviInfo, recorded: datetime) -> Tuple[List[str], List[str]]:
    """Rewrite the bogus date fields of an open AVI (a copy, never the camera's file) in place: same style,
    same byte length, NUL-padded, so no other byte moves. Only a field holding exactly the known fake date is
    touched. A field whose replacement would not fit is left alone and reported. Returns (chunk ids rewritten,
    chunk ids left because the text did not fit)."""
    patched: List[str] = []
    too_small: List[str] = []
    for f in info.date_fields:
        current = parse_avi_date(f.value)
        if current is None or current.date() != BOGUS_AVI_DATE:
            continue
        replacement = format_avi_date_like(f.value, recorded).encode("ascii")
        if len(replacement) > f.size:
            too_small.append(f.chunk_id)
            continue
        fh.seek(f.offset)
        fh.write(replacement.ljust(f.size, b"\x00"))
        patched.append(f.chunk_id)
    return patched, too_small


# ---- Finding the camera ------------------------------------------------------------------------------


def is_jpeg_name(name: str) -> bool:
    return name.lower().endswith(PHOTO_EXTS)


def head_contains(path: Path, needle: bytes, size: int = SIGNATURE_SAMPLE_SIZE) -> bool:
    try:
        with open(path, "rb") as fh:
            return needle in fh.read(size)
    except OSError:
        return False


def is_charmera_volume(root: Path) -> bool:
    """A Charmera card: DCIM plus either the SPIDCIM folder the camera creates next to it, or a photo carrying
    the Generalplus "GPEncoder" comment in its first 4 KiB. Judged by content, never by volume label."""
    dcim = root / "DCIM"
    try:
        if not dcim.is_dir():
            return False
        if (root / SPI_DIR_NAME).is_dir():
            return True
        checked = 0
        for path in sorted(dcim.rglob("*")):
            if not path.is_file() or path.name.startswith("._") or not is_jpeg_name(path.name):
                continue
            if head_contains(path, GPENCODER_SIGNATURE):
                return True
            checked += 1
            if checked >= MAX_SIGNATURE_FILES:
                break
    except OSError:
        pass
    return False


def candidate_volumes() -> List[Path]:
    """Mount points worth checking: /media/*/*, /run/media/*/* and /mnt/* on Linux, D: to Z: on Windows,
    /Volumes/* on macOS."""
    found: List[Path] = []
    if sys.platform.startswith("win"):
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(letter + ":\\")
            if root.is_dir():
                found.append(root)
        return found
    patterns = ["/Volumes/*"] if sys.platform == "darwin" else ["/media/*/*", "/run/media/*/*", "/mnt/*"]
    for pattern in patterns:
        base, _, glob = pattern.lstrip("/").partition("/")
        try:
            found.extend(p for p in sorted(Path("/" + base).glob(glob)) if p.is_dir())
        except OSError:
            continue
    return found


def find_cameras() -> List[Path]:
    return [p for p in candidate_volumes() if is_charmera_volume(p)]


def media_root(source: Path) -> Path:
    """The folder to read: DCIM under a card root, or the folder itself."""
    dcim = source / "DCIM"
    return dcim if dcim.is_dir() else source


def list_media(root: Path) -> List[Path]:
    """Every .jpg/.jpeg/.avi under root, recursively, sorted, skipping macOS "._" metadata files."""
    out: List[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("._"):
            continue
        low = path.name.lower()
        if low.endswith(PHOTO_EXTS) or low.endswith(VIDEO_EXTS):
            out.append(path)
    return out


# ---- Naming and dedup --------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_token(digest: str) -> str:
    return digest[:HASH_TOKEN_LEN].lower()


def token_from_name(name: str) -> Optional[str]:
    """The hash token embedded in a name this command produced; None for any other name."""
    m = _HASH_TOKEN_RE.search(Path(name).stem)
    return m.group(1).lower() if m else None


def existing_tokens(out_dir: Path) -> Set[str]:
    tokens: Set[str] = set()
    if not out_dir.is_dir():
        return tokens
    for entry in out_dir.iterdir():
        token = token_from_name(entry.name)
        if token:
            tokens.add(token)
    return tokens


def output_name(kind: str, when: datetime, token: str, ext: str) -> str:
    prefix = "IMG" if kind == "photo" else "VID"
    return "{}_{}_{}{}".format(prefix, when.strftime("%Y%m%d_%H%M%S"), token, ext)


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        candidate = directory / "{}_{}{}".format(stem, i, ext)
        if not candidate.exists():
            return candidate
        i += 1


# ---- ffmpeg ------------------------------------------------------------------------------------------


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def ffmpeg_args(ffmpeg: str, src: Path, dst: Path, recorded: datetime) -> List[str]:
    """H.264 (CRF 26, medium) + AAC 128k at 44.1 kHz with a correct creation_time, the same settings the Go tool
    uses. The muxer is named explicitly because the output is written under a temporary name."""
    return [
        ffmpeg, "-y", "-nostdin", "-loglevel", "error",
        "-i", str(src),
        "-c:v", "libx264", "-crf", "26", "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-metadata", "creation_time=" + recorded.strftime(FFMPEG_TIME_FORMAT),
        "-movflags", "+faststart",
        "-f", "mp4", str(dst),
    ]


# ---- The command -------------------------------------------------------------------------------------


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "exif-fix",
        help="repair EXIF dates and AVI dates on copies of your photos (never touches the card)",
        description=(
            "Copy every JPEG and AVI from a Charmera card (or any folder) to --out and repair the copies: a fresh "
            "EXIF with the real dimensions, a parseable date (the camera's own, else the file time), Kodak/Charmera "
            "as the camera and the lens specs, with the corrupt MakerNote dropped; and the camera's fake 2010-06-29 "
            "date inside videos replaced by the file time. The source is only ever read. Outputs keep the source "
            "modification time and are named IMG_/VID_<date>_<hash>, so a second run skips what is already there."
        ),
    )
    parser.add_argument("--source", type=Path, help="card root or any folder; omit to look for a mounted Charmera")
    parser.add_argument("--out", type=Path, required=True, help="where the repaired copies go")
    parser.add_argument(
        "--video", choices=("fix-date", "transcode", "copy"), default="fix-date",
        help="fix-date (default): rewrite the fake date in the AVI copy; transcode: H.264/AAC MP4 through ffmpeg "
        "with a correct creation_time (needs ffmpeg on PATH, else falls back to fix-date); copy: copy unchanged",
    )
    parser.add_argument("--keep-make", action="store_true", help="keep the camera's Make/Model (Generalplus/CBB3)")
    parser.add_argument("--keep-names", action="store_true", help="keep the source file names instead of IMG_/VID_ names")
    parser.add_argument("--dry-run", action="store_true", help="report what would be done; write nothing")
    parser.add_argument("--verbose", action="store_true", help="also show what the camera wrote")
    parser.set_defaults(func=handler)


def _resolve_source(arg: Optional[Path]) -> Optional[Path]:
    if arg is not None:
        if not arg.is_dir():
            print(f"error: --source {arg} is not a directory", file=sys.stderr)
            return None
        return arg
    cameras = find_cameras()
    if not cameras:
        print(
            "error: no Charmera found among the mounted volumes (a Charmera has DCIM plus SPIDCIM, or JPEGs with "
            "the GPEncoder signature). Pass --source with the card root or a folder of its files.",
            file=sys.stderr,
        )
        return None
    if len(cameras) > 1:
        print("error: more than one Charmera-like volume; pass --source:", file=sys.stderr)
        for cam in cameras:
            print(f"  {cam}", file=sys.stderr)
        return None
    print(f"Charmera found at {cameras[0]}")
    return cameras[0]


def _write_atomically(dst: Path, data: bytes) -> None:
    tmp = dst.with_name(dst.name + ".charmera-tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def _copy_times(src_stat: os.stat_result, dst: Path) -> None:
    os.utime(dst, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))


def _describe_photo(res: FixResult, keep_make: bool) -> str:
    date_note = res.date_source
    if res.date_repaired:
        date_note += ", extra colon repaired"
    parts = [
        f"{res.width}x{res.height}",
        f"orientation {res.orientation}",
        f"date {res.date_time} from {date_note}",
    ]
    if not res.exif_was_present:
        parts.append("no EXIF in source")
    if keep_make:
        parts.append(f"make/model kept {res.make}/{res.model}")
    elif res.old_make or res.old_model:
        parts.append(f"make/model {res.make}/{res.model} (was {res.old_make}/{res.old_model})")
    else:
        parts.append(f"make/model {res.make}/{res.model}")
    return ", ".join(parts)


class _Run:
    """One invocation's state and counters."""

    def __init__(self, args: argparse.Namespace, video_mode: str, ffmpeg: Optional[str]) -> None:
        self.args = args
        self.video_mode = video_mode
        self.ffmpeg = ffmpeg
        self.out_dir: Path = args.out
        self.tokens: Set[str] = set() if args.keep_names else existing_tokens(args.out)
        self.photos = 0
        self.videos = 0
        self.skipped = 0
        self.failed = 0

    def say(self, src: Path, dst: Optional[Path], text: str) -> None:
        verb = "would write" if self.args.dry_run else "->"
        target = dst.name if dst is not None else "-"
        print(f"{src.name} {verb} {target}: {text}")

    def destination(self, src: Path, kind: str, when: datetime, token: str, out_ext: str) -> Optional[Path]:
        """The output path, or None when this file is already in the output directory."""
        if self.args.keep_names:
            name = src.stem + (out_ext if kind == "video" and self.video_mode == "transcode" else src.suffix)
            dst = self.out_dir / name
            if dst.exists():
                self.skipped += 1
                print(f"{src.name} skipped: {dst.name} already exists in {self.out_dir}")
                return None
            return dst
        if token in self.tokens:
            self.skipped += 1
            print(f"{src.name} skipped: already in {self.out_dir} (hash {token})")
            return None
        return unique_path(self.out_dir, output_name(kind, when, token, out_ext))

    def photo(self, src: Path) -> None:
        st = src.stat()
        when = datetime.fromtimestamp(st.st_mtime)
        token = hash_token(sha256_file(src))
        dst = self.destination(src, "photo", when, token, ".jpg")
        if dst is None:
            return
        data = src.read_bytes()
        fixed, res = fix_jpeg(data, when, keep_make=self.args.keep_make)
        if self.args.verbose and res.exif_was_present:
            layout = scan_jpeg(data)
            info = read_exif(data[layout.exif_start + 10 : layout.exif_end])
            print(
                f"  source EXIF: make {info.make!r} model {info.model!r} orientation {info.orientation} "
                f"DateTime {info.date_time!r} DateTimeOriginal {info.date_time_original!r} "
                f"DateTimeDigitized {info.date_time_digitized!r}; APP1 {layout.exif_end - layout.exif_start} bytes"
            )
        if not self.args.dry_run:
            _write_atomically(dst, fixed)
            _copy_times(st, dst)
        self.tokens.add(token)
        self.photos += 1
        self.say(src, dst, _describe_photo(res, self.args.keep_make))

    def video(self, src: Path) -> None:
        st = src.stat()
        when = datetime.fromtimestamp(st.st_mtime)
        token = hash_token(sha256_file(src))
        out_ext = ".mp4" if self.video_mode == "transcode" else ".avi"
        dst = self.destination(src, "video", when, token, out_ext)
        if dst is None:
            return
        with open(src, "rb") as fh:
            info = read_avi(fh)
        dims = f"{info.width}x{info.height}" if info.width and info.height else "unknown size"
        if self.args.verbose:
            for f in info.date_fields:
                print(f"  source {f.chunk_id} at offset {f.offset}, {f.size} bytes: {f.value.rstrip(chr(0))!r}")
            if not info.date_fields:
                print("  source has no IDIT/ICRD date chunk")

        if self.video_mode == "transcode":
            text = (
                f"{dims}, transcoded to H.264/AAC MP4, creation_time {when.strftime(FFMPEG_TIME_FORMAT)} from mtime"
            )
            if not self.args.dry_run:
                tmp = dst.with_name(dst.name + ".charmera-tmp")
                try:
                    proc = subprocess.run(
                        ffmpeg_args(self.ffmpeg or "ffmpeg", src, tmp, when),
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False,
                    )
                    if proc.returncode != 0:
                        raise RuntimeError("ffmpeg failed: " + proc.stderr.strip())
                    os.replace(tmp, dst)
                finally:
                    if tmp.exists():
                        tmp.unlink()
                _copy_times(st, dst)
        elif self.video_mode == "copy":
            text = f"{dims}, copied unchanged"
            if not self.args.dry_run:
                tmp = dst.with_name(dst.name + ".charmera-tmp")
                try:
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, dst)
                finally:
                    if tmp.exists():
                        tmp.unlink()
                _copy_times(st, dst)
        else:
            text = self._fix_date(src, dst, info, when, dims, st)
        self.tokens.add(token)
        self.videos += 1
        self.say(src, dst, text)

    def _fix_date(self, src: Path, dst: Path, info: AviInfo, when: datetime, dims: str, st: os.stat_result) -> str:
        bogus = [f for f in info.date_fields if (parse_avi_date(f.value) or datetime.min).date() == BOGUS_AVI_DATE]
        genuine = [f for f in info.date_fields if f not in bogus]
        if not info.date_fields:
            note = "no IDIT/ICRD date chunk, copied unchanged"
        elif not bogus:
            held = ", ".join(f"{f.chunk_id} {f.value.strip(chr(0)).strip()!r}" for f in genuine)
            note = f"left as is, not the camera's fake date: {held}"
        else:
            note = (
                f"date {when.strftime(EXIF_DATE_FORMAT)} from mtime replaces 2010-06-29 in "
                + ", ".join(f.chunk_id for f in bogus)
            )
        if not self.args.dry_run:
            tmp = dst.with_name(dst.name + ".charmera-tmp")
            try:
                shutil.copyfile(src, tmp)
                if bogus:
                    with open(tmp, "r+b") as fh:
                        copy_info = read_avi(fh)
                        patched, too_small = patch_avi_dates(fh, copy_info, when)
                    if too_small:
                        note += "; left unchanged because the new text would not fit: " + ", ".join(too_small)
                    if not patched and not too_small:
                        note = "copied unchanged: the copy did not show the fake date"
                os.replace(tmp, dst)
            finally:
                if tmp.exists():
                    tmp.unlink()
            _copy_times(st, dst)
        return f"{dims}, {note}"


def handler(args: argparse.Namespace) -> int:
    source = _resolve_source(args.source)
    if source is None:
        return 1
    root = media_root(source)
    out_dir: Path = args.out
    files = [p for p in list_media(root) if not _is_under(p, out_dir)]
    if not files:
        print(f"No .jpg/.jpeg/.avi files under {root}")
        return 0

    video_mode = args.video
    ffmpeg = find_ffmpeg()
    has_videos = any(p.name.lower().endswith(VIDEO_EXTS) for p in files)
    if video_mode == "transcode" and has_videos and ffmpeg is None:
        print(
            "ffmpeg is not on PATH, so videos cannot be transcoded to MP4. Falling back to --video fix-date: the "
            "AVI is copied and only its fake 2010-06-29 date is rewritten. Install ffmpeg (ffmpeg.org, or your "
            "package manager) to get H.264/AAC MP4 output.",
        )
        video_mode = "fix-date"

    if args.dry_run:
        print(f"Dry run: reading {root}, nothing will be written to {out_dir}")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)

    run = _Run(args, video_mode, ffmpeg)
    for path in files:
        try:
            if path.name.lower().endswith(PHOTO_EXTS):
                run.photo(path)
            else:
                run.video(path)
        except Exception as exc:  # one bad file must not stop the rest
            run.failed += 1
            print(f"{path.name} FAILED: {exc}")

    video_word = {"fix-date": "videos date-fixed", "transcode": "videos transcoded", "copy": "videos copied"}[video_mode]
    summary = (
        f"{run.photos} photos fixed, {run.videos} {video_word}, {run.skipped} skipped (already in {out_dir}), "
        f"{run.failed} failed"
    )
    if args.dry_run:
        print("Dry run, nothing written: " + summary)
    else:
        print("Done: " + summary)
    return 1 if run.failed else 0


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except (ValueError, OSError):
        return False
