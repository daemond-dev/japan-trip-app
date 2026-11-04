"""exif-fix: the JPEG EXIF rebuild, the AVI date rewrite, naming/dedup and the command itself.

Pillow (if installed) only generates input JPEGs and double-checks the rebuilt EXIF with an independent
reader; the module under test never imports it. ffmpeg/ffprobe (if on PATH) generate a real AVI and
validate the patched copy; a hand-built RIFF covers the same logic when they are absent.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import struct
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from charmera_toolkit import cli
from charmera_toolkit import exif as ex

FALLBACK = datetime(2026, 3, 3, 12, 16, 29)
RECORDED = datetime(2026, 8, 7, 18, 45, 12)
NAME_RE = re.compile(r"^(IMG|VID)_\d{8}_\d{6}_[0-9a-f]{16}\.(jpg|avi|mp4)$")


# ---- helpers -----------------------------------------------------------------------------------------


def make_jpeg(width: int = 64, height: int = 48, comment: bytes = b"") -> bytes:
    """A baseline JPEG with a JFIF APP0 and no EXIF, like a freshly encoded frame."""
    Image = pytest.importorskip("PIL.Image")
    img = Image.new("RGB", (width, height), (200, 30, 30))
    buf = io.BytesIO()
    if comment:
        img.save(buf, "JPEG", quality=80, comment=comment)
    else:
        img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def with_exif(jpeg: bytes, payload: bytes) -> bytes:
    return ex.replace_exif(jpeg, ex.scan_jpeg(jpeg), payload)


def exif_of(jpeg: bytes) -> ex.ExifInfo:
    layout = ex.scan_jpeg(jpeg)
    assert layout.exif_found
    return ex.read_exif(jpeg[layout.exif_start + 10 : layout.exif_end])


def set_mtime(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def tiff_big_endian(ifd0, exif_ifd) -> bytes:
    """A big-endian TIFF from (tag, type, count, value bytes) entries; values over 4 bytes go out of line."""

    def ifd_size(entries):
        return 2 + 12 * len(entries) + 4

    exif_off = 8 + ifd_size(ifd0 + [(ex.TAG_EXIF_IFD, ex.TYPE_LONG, 1, b"")])
    ifd0 = sorted(ifd0 + [(ex.TAG_EXIF_IFD, ex.TYPE_LONG, 1, struct.pack(">I", exif_off))])
    ext_base = exif_off + ifd_size(exif_ifd)
    ext = bytearray()
    out = bytearray(b"MM" + struct.pack(">HI", 0x2A, 8))

    def write(entries):
        out.extend(struct.pack(">H", len(entries)))
        for tag, typ, count, value in sorted(entries):
            out.extend(struct.pack(">HHI", tag, typ, count))
            if len(value) <= 4:
                out.extend(value.ljust(4, b"\x00"))
            else:
                out.extend(struct.pack(">I", ext_base + len(ext)))
                ext.extend(value + (b"\x00" if len(value) % 2 else b""))
        out.extend(struct.pack(">I", 0))

    write(ifd0)
    write(exif_ifd)
    return bytes(out) + bytes(ext)


def riff_chunk(cid: bytes, data: bytes) -> bytes:
    return cid + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")


def riff_list(kind: bytes, *children: bytes) -> bytes:
    body = kind + b"".join(children)
    return b"LIST" + struct.pack("<I", len(body)) + body


BOGUS_IDIT = b"Tue Jun 29 12:00:00 2010\n\x00"
BOGUS_ICRD = b"2010-06-29\x00"


def minimal_avi(idit: bytes = BOGUS_IDIT, icrd: bytes = BOGUS_ICRD) -> bytes:
    """The shape of a Charmera video: MJPG 1440x1080 + PCM, IDIT in hdrl (asctime) and ICRD in INFO (ISO),
    exactly as the C# importer's sample builder lays it out."""
    avih = bytearray(56)
    struct.pack_into("<I", avih, 0, 33_333)
    struct.pack_into("<I", avih, 16, 90)
    struct.pack_into("<I", avih, 24, 2)
    struct.pack_into("<I", avih, 32, 1440)
    struct.pack_into("<I", avih, 36, 1080)
    vstrh = bytearray(56)
    vstrh[0:8] = b"vidsMJPG"
    vstrf = bytearray(40)
    struct.pack_into("<III", vstrf, 0, 40, 1440, 1080)
    astrh = bytearray(56)
    astrh[0:4] = b"auds"
    astrf = struct.pack("<HHIIHH", 1, 1, 22050, 44100, 2, 16)
    hdrl = [
        riff_chunk(b"avih", bytes(avih)),
        riff_list(b"strl", riff_chunk(b"strh", bytes(vstrh)), riff_chunk(b"strf", bytes(vstrf))),
        riff_list(b"strl", riff_chunk(b"strh", bytes(astrh)), riff_chunk(b"strf", astrf)),
    ]
    if idit:
        hdrl.append(riff_chunk(b"IDIT", idit))
    frame = b"\xff\xd8" + bytes(range(256)) * 4 + b"\xff\xd9"
    top = [riff_list(b"hdrl", *hdrl)]
    if icrd:
        top.append(riff_list(b"INFO", riff_chunk(b"ICRD", icrd)))
    top.append(riff_list(b"movi", riff_chunk(b"01wb", bytes(64)), riff_chunk(b"00dc", frame), riff_chunk(b"00dc", frame)))
    top.append(riff_chunk(b"idx1", bytes(16)))
    body = b"".join(top)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"AVI " + body


def inject_dates(avi: bytes, idit: bytes = BOGUS_IDIT, icrd: bytes = BOGUS_ICRD) -> bytes:
    """Add the camera's date chunks to an AVI that has none: IDIT at the end of LIST hdrl, a LIST INFO with
    ICRD at the end of the RIFF, with the list and RIFF sizes updated."""
    assert ex.is_avi(avi[:12])
    end = min(len(avi), 8 + struct.unpack("<I", avi[4:8])[0])
    out = bytearray(avi[:12])
    pos = 12
    while pos + 8 <= end:
        cid = avi[pos : pos + 4]
        size = struct.unpack("<I", avi[pos + 4 : pos + 8])[0]
        body = avi[pos + 8 : pos + 8 + size]
        if cid == b"LIST" and body[:4] == b"hdrl":
            body += riff_chunk(b"IDIT", idit)
            out += b"LIST" + struct.pack("<I", len(body)) + body
        else:
            out += avi[pos : pos + 8 + size + (size & 1)]
        pos += 8 + size + (size & 1)
    out += riff_list(b"INFO", riff_chunk(b"ICRD", icrd))
    out[4:8] = struct.pack("<I", len(out) - 8)
    return bytes(out)


def ffmpeg_avi(path: Path) -> None:
    """A 0.3 s MJPEG + PCM AVI, the Charmera's codecs, from ffmpeg's test sources."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
            "-t", "0.3", "-c:v", "mjpeg", "-c:a", "pcm_s16le", "-ac", "1", str(path),
        ],
        check=True,
    )


def ffprobe_tags(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=nb_streams:format_tags", "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def card(tmp_path: Path) -> Path:
    root = tmp_path / "card"
    (root / "DCIM").mkdir(parents=True)
    (root / "SPIDCIM").mkdir()
    return root


def run_cli(*args: str) -> int:
    return cli.main(["exif-fix", *args])


# ---- dates ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, want",
    [
        ("2026:03:03:12:16:29", "2026:03:03 12:16:29"),  # the camera's extra colon
        ("2026:03:03 12:16:29", "2026:03:03 12:16:29"),  # already valid
        ("2026:03:03 12:16:29\x00   ", "2026:03:03 12:16:29"),  # padding
        ("0000:00:00 00:00:00", None),
        ("", None),
        ("garbage", None),
    ],
)
def test_normalize_exif_date(raw, want):
    assert ex.normalize_exif_date(raw) == want


@pytest.mark.parametrize(
    "raw, ymd",
    [
        ("Tue Jun 29 12:00:00 2010\n", (2010, 6, 29)),
        ("Wed Jun  9 12:00:00 2010", (2010, 6, 9)),
        ("Mon Jun 29 12:00:00 2010", (2010, 6, 29)),  # wrong weekday: still read
        ("2010:06:29 12:00:00", (2010, 6, 29)),
        ("2010-06-29\x00", (2010, 6, 29)),
        ("2010-06-29T12:00:00", (2010, 6, 29)),
        ("2010/06/29", (2010, 6, 29)),
    ],
)
def test_parse_avi_date_accepts_common_forms(raw, ymd):
    parsed = ex.parse_avi_date(raw)
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == ymd


def test_parse_avi_date_rejects_nonsense():
    assert ex.parse_avi_date("") is None
    assert ex.parse_avi_date("Tue Xyz 29 12:00:00 2010") is None
    assert ex.parse_avi_date("not a date") is None


def test_format_avi_date_like_keeps_style_and_length():
    for original in ("Tue Jun 29 12:00:00 2010\n", "Tue Jun 29 12:00:00 2010", "2010:06:29 12:00:00", "2010-06-29", "2010-06-29T12:00:00", "2010-06-29 12:00:00"):
        formatted = ex.format_avi_date_like(original, RECORDED)
        assert len(formatted) == len(original), original
        assert ex.parse_avi_date(formatted) == (RECORDED if len(original.strip()) > 10 else RECORDED.replace(hour=0, minute=0, second=0))
    assert ex.format_avi_date_like("Tue Jun 29 12:00:00 2010\n", RECORDED) == "Fri Aug  7 18:45:12 2026\n"


# ---- JPEG EXIF rebuild ---------------------------------------------------------------------------------


def test_no_exif_gets_mtime_date_true_dimensions_and_camera_identity():
    src = make_jpeg(64, 48)
    out, res = ex.fix_jpeg(src, FALLBACK)

    assert (res.width, res.height, res.orientation) == (64, 48, 1)
    assert res.date_source == "mtime" and res.date_time == "2026:03:03 12:16:29"
    assert not res.exif_was_present and not res.date_repaired
    assert (res.make, res.model) == ("Kodak", "Charmera")

    info = exif_of(out)
    assert info.present
    assert (info.make, info.model, info.orientation) == ("Kodak", "Charmera", 1)
    assert info.date_time == info.date_time_original == info.date_time_digitized == "2026:03:03 12:16:29"

    layout = ex.scan_jpeg(out)
    assert layout.exif_found and layout.exif_start == ex.scan_jpeg(src).exif_end  # right after the JFIF APP0


def test_pillow_reads_the_rebuilt_exif_and_decodes_identical_pixels():
    Image = pytest.importorskip("PIL.Image")
    src = make_jpeg(64, 48)
    out, _ = ex.fix_jpeg(src, FALLBACK)

    fixed = Image.open(io.BytesIO(out))
    fixed.load()
    exif = fixed.getexif()
    assert exif[ex.TAG_MAKE] == "Kodak" and exif[ex.TAG_MODEL] == "Charmera"
    assert exif[ex.TAG_ORIENTATION] == 1 and exif[ex.TAG_DATETIME] == "2026:03:03 12:16:29"
    sub = exif.get_ifd(ex.TAG_EXIF_IFD)
    assert sub[ex.TAG_DATETIME_ORIGINAL] == "2026:03:03 12:16:29"
    assert (sub[ex.TAG_PIXEL_X], sub[ex.TAG_PIXEL_Y]) == (64, 48)
    assert float(sub[ex.TAG_FNUMBER]) == pytest.approx(2.4)
    assert sub[ex.TAG_FOCAL_LENGTH_35MM] == 35
    assert sub[ex.TAG_LENS_MODEL] == ex.LENS_MODEL
    assert sub[ex.TAG_EXIF_VERSION] == b"0230"
    assert fixed.tobytes() == Image.open(io.BytesIO(src)).tobytes()


def test_malformed_date_is_repaired_and_chip_make_replaced():
    src = make_jpeg()
    bad = with_exif(src, ex.encode_exif(ex.ExifData(64, 48, "2026:03:03:12:16:29", make="Generalplus", model="CBB3")))
    assert exif_of(bad).date_time_original == "2026:03:03:12:16:29"  # the corruption is really in the file

    out, res = ex.fix_jpeg(bad, datetime(2000, 1, 1))
    assert res.date_source == "exif" and res.date_repaired
    assert res.date_time == "2026:03:03 12:16:29"
    assert (res.old_make, res.old_model) == ("Generalplus", "CBB3")
    assert (res.make, res.model) == ("Kodak", "Charmera")
    assert exif_of(out).date_time_original == "2026:03:03 12:16:29"
    assert exif_of(out).make == "Kodak"


def test_keep_make_preserves_the_camera_values():
    bad = with_exif(make_jpeg(), ex.encode_exif(ex.ExifData(64, 48, "2026:03:03:12:16:29", make="Generalplus", model="CBB3")))
    out, res = ex.fix_jpeg(bad, FALLBACK, keep_make=True)
    assert (res.make, res.model) == ("Generalplus", "CBB3")
    assert (exif_of(out).make, exif_of(out).model) == ("Generalplus", "CBB3")


def test_zero_date_in_exif_falls_back_to_mtime():
    bad = with_exif(make_jpeg(), ex.encode_exif(ex.ExifData(64, 48, "0000:00:00 00:00:00")))
    _, res = ex.fix_jpeg(bad, FALLBACK)
    assert res.date_source == "mtime" and res.date_time == "2026:03:03 12:16:29"
    assert res.exif_was_present


def test_orientation_is_preserved_and_dimensions_come_from_the_frame():
    # The camera says 640x480 in EXIF while the frame is bigger; the frame wins.
    src = make_jpeg(96, 32)
    lying = with_exif(src, ex.encode_exif(ex.ExifData(640, 480, "2026:03:03 12:16:29", orientation=6)))
    out, res = ex.fix_jpeg(lying, FALLBACK)
    assert (res.width, res.height, res.orientation) == (96, 32, 6)
    assert exif_of(out).orientation == 6


def test_big_endian_exif_with_corrupt_makernote_is_read():
    src = make_jpeg()
    date = b"2026:03:03:12:16:29\x00"
    tiff_len_guess = 200  # the MakerNote offset will point past the end, as the camera's does
    tiff = tiff_big_endian(
        [
            (ex.TAG_MAKE, ex.TYPE_ASCII, 12, b"Generalplus\x00"),
            (ex.TAG_MODEL, ex.TYPE_ASCII, 4, b"CBB3"),
            (ex.TAG_ORIENTATION, ex.TYPE_SHORT, 1, struct.pack(">H", 8)),
            (ex.TAG_DATETIME, ex.TYPE_ASCII, 20, date),
        ],
        [
            (ex.TAG_DATETIME_ORIGINAL, ex.TYPE_ASCII, 20, date),
            (0x927C, ex.TYPE_UNDEFINED, 100, struct.pack(">I", tiff_len_guess)),  # MakerNote, offset past the end
        ],
    )
    jpeg = with_exif(src, ex.EXIF_HEADER + tiff)
    out, res = ex.fix_jpeg(jpeg, datetime(2000, 1, 1))
    assert res.orientation == 8
    assert res.date_source == "exif" and res.date_time == "2026:03:03 12:16:29"
    assert (res.old_make, res.old_model) == ("Generalplus", "CBB3")
    assert out[ex.scan_jpeg(out).exif_start + 10 : ex.scan_jpeg(out).exif_start + 12] == b"II"  # rebuilt little-endian


def test_scan_data_is_byte_identical():
    src = make_jpeg(80, 60)
    bad = with_exif(src, ex.encode_exif(ex.ExifData(64, 48, "2026:03:03:12:16:29")))
    for original in (src, bad):
        out, _ = ex.fix_jpeg(original, FALLBACK)
        before, after = ex.scan_jpeg(original), ex.scan_jpeg(out)
        assert original[: before.exif_start] == out[: after.exif_start]
        assert original[before.exif_end :] == out[after.exif_end :]
        assert out[after.exif_end :].endswith(b"\xff\xd9")


def test_fix_is_idempotent():
    src = make_jpeg()
    once, _ = ex.fix_jpeg(src, FALLBACK)
    twice, res = ex.fix_jpeg(once, datetime(2000, 1, 1))
    assert twice == once and res.date_source == "exif"


def test_rejects_non_jpeg():
    with pytest.raises(ValueError):
        ex.fix_jpeg(b"not a jpeg", FALLBACK)
    with pytest.raises(ValueError):
        ex.fix_jpeg(b"\xff\xd8\xff\xd9", FALLBACK)  # SOI/EOI only: no frame header


# ---- AVI date rewrite ----------------------------------------------------------------------------------


def test_minimal_avi_dates_are_found_and_patched(tmp_path):
    path = tmp_path / "PICT0001.AVI"
    original = minimal_avi()
    path.write_bytes(original)
    with open(path, "rb") as fh:
        info = ex.read_avi(fh)
    assert (info.width, info.height) == (1440, 1080)
    assert info.duration_seconds == pytest.approx(3.0, abs=0.01)
    assert [f.chunk_id for f in info.date_fields] == ["IDIT", "ICRD"]
    assert ex.has_bogus_avi_date(info)

    with open(path, "r+b") as fh:
        assert ex.patch_avi_dates(fh, info, RECORDED) == (["IDIT", "ICRD"], [])
    patched = path.read_bytes()
    assert len(patched) == len(original)
    with open(path, "rb") as fh:
        after = ex.read_avi(fh)
    assert not ex.has_bogus_avi_date(after)
    assert after.date_fields[0].value.rstrip("\x00") == "Fri Aug  7 18:45:12 2026\n"
    assert after.date_fields[1].value.rstrip("\x00") == "2026-08-07"
    date_bytes = {i for f in info.date_fields for i in range(f.offset, f.offset + f.size)}
    changed = {i for i in range(len(original)) if original[i] != patched[i]}
    assert changed and changed <= date_bytes


def test_genuine_avi_date_is_left_alone(tmp_path):
    path = tmp_path / "v.avi"
    original = minimal_avi(idit=b"Sat Aug  1 10:00:00 2026\n\x00", icrd=b"")
    path.write_bytes(original)
    with open(path, "r+b") as fh:
        info = ex.read_avi(fh)
        assert not ex.has_bogus_avi_date(info)
        assert ex.patch_avi_dates(fh, info, RECORDED) == ([], [])
    assert path.read_bytes() == original


def test_too_small_date_field_is_reported_not_truncated(tmp_path):
    path = tmp_path / "v.avi"
    path.write_bytes(minimal_avi(idit=b"Jun 29 12:00:00 2010", icrd=b""))  # no weekday: 20 bytes of room
    with open(path, "r+b") as fh:
        info = ex.read_avi(fh)
        assert ex.has_bogus_avi_date(info)
        assert ex.patch_avi_dates(fh, info, RECORDED) == ([], ["IDIT"])


def test_read_avi_rejects_other_files(tmp_path):
    with pytest.raises(ValueError):
        ex.read_avi(io.BytesIO(b"not a video"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not on PATH")
def test_ffmpeg_avi_with_injected_camera_date_is_fixed_and_still_valid(tmp_path):
    plain = tmp_path / "plain.avi"
    ffmpeg_avi(plain)
    path = tmp_path / "PICT0002.AVI"
    original = inject_dates(plain.read_bytes())
    path.write_bytes(original)
    assert ffprobe_tags(path).get("TAG:creation_time", "").startswith("2010-06-29")  # ffmpeg sees the fake date too

    with open(path, "r+b") as fh:
        info = ex.read_avi(fh)
        assert [f.chunk_id for f in info.date_fields] == ["IDIT", "ICRD"]
        assert ex.patch_avi_dates(fh, info, RECORDED) == (["IDIT", "ICRD"], [])

    assert len(path.read_bytes()) == len(original)
    tags = ffprobe_tags(path)
    assert tags["TAG:creation_time"].startswith("2026-08-07 18:45:12")
    assert tags["TAG:date"] == "2026-08-07"
    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    assert decode.returncode == 0 and decode.stderr == ""


# ---- camera detection ----------------------------------------------------------------------------------


def test_is_charmera_volume(tmp_path):
    assert not ex.is_charmera_volume(tmp_path)  # no DCIM
    dcim = tmp_path / "DCIM"
    dcim.mkdir()
    assert not ex.is_charmera_volume(tmp_path)  # DCIM alone is any camera
    (dcim / "PICT0001.JPG").write_bytes(make_jpeg())
    assert not ex.is_charmera_volume(tmp_path)  # a JPEG without the Generalplus comment
    (dcim / "PICT0002.JPG").write_bytes(make_jpeg(comment=b"GPEncoder 1.0"))
    assert ex.is_charmera_volume(tmp_path)  # signature within the first 4 KiB
    (dcim / "PICT0002.JPG").unlink()
    (tmp_path / "SPIDCIM").mkdir()
    assert ex.is_charmera_volume(tmp_path)  # the sibling folder


def test_media_root_and_listing(tmp_path):
    root = card(tmp_path)
    (root / "DCIM" / "100MEDIA").mkdir()
    (root / "DCIM" / "100MEDIA" / "PICT0003.jpeg").write_bytes(b"x")
    (root / "DCIM" / "PICT0001.JPG").write_bytes(b"x")
    (root / "DCIM" / "PICT0002.AVI").write_bytes(b"x")
    (root / "DCIM" / "._PICT0001.JPG").write_bytes(b"x")
    (root / "DCIM" / "notes.txt").write_bytes(b"x")
    assert ex.media_root(root) == root / "DCIM"
    assert ex.media_root(root / "DCIM") == root / "DCIM"
    assert [p.name for p in ex.list_media(ex.media_root(root))] == ["PICT0003.jpeg", "PICT0001.JPG", "PICT0002.AVI"]


def test_hash_token_round_trip():
    assert ex.token_from_name("IMG_20260303_121629_0123456789abcdef.jpg") == "0123456789abcdef"
    assert ex.token_from_name("VID_20260303_121629_0123456789ABCDEF_1.mp4") is None  # collision suffix is not a token
    assert ex.token_from_name("PICT0001.JPG") is None
    assert ex.output_name("photo", FALLBACK, "0123456789abcdef", ".jpg") == "IMG_20260303_121629_0123456789abcdef.jpg"


# ---- the command ---------------------------------------------------------------------------------------


def test_cli_copies_fixes_names_preserves_mtime_and_dedups(tmp_path, capsys):
    root = card(tmp_path)
    src = root / "DCIM" / "PICT0001.JPG"
    src.write_bytes(with_exif(make_jpeg(), ex.encode_exif(ex.ExifData(64, 48, "2026:03:03:12:16:29", make="Generalplus", model="CBB3"))))
    set_mtime(src, FALLBACK)
    (root / "DCIM" / "._PICT0001.JPG").write_bytes(b"\x00\x05\x16\x07")
    before = src.read_bytes()
    out = tmp_path / "fixed"

    assert run_cli("--source", str(root), "--out", str(out)) == 0
    text = capsys.readouterr().out
    assert "PICT0001.JPG -> IMG_" in text and "extra colon repaired" in text and "64x48" in text
    assert "1 photos fixed" in text

    produced = sorted(p.name for p in out.iterdir())
    assert len(produced) == 1 and NAME_RE.match(produced[0])
    assert produced[0].startswith("IMG_20260303_121629_")
    assert produced[0] == "IMG_20260303_121629_" + ex.hash_token(ex.sha256_file(src)) + ".jpg"
    dst = out / produced[0]
    assert dst.stat().st_mtime == pytest.approx(src.stat().st_mtime, abs=1e-6)
    assert src.read_bytes() == before  # the source is never touched
    assert exif_of(dst.read_bytes()).date_time_original == "2026:03:03 12:16:29"
    assert not list(out.glob("*.charmera-tmp"))

    assert run_cli("--source", str(root), "--out", str(out)) == 0
    text = capsys.readouterr().out
    assert "skipped" in text and "0 photos fixed" in text and "1 skipped" in text
    assert sorted(p.name for p in out.iterdir()) == produced


def test_cli_source_can_be_any_folder_and_keep_names(tmp_path, capsys):
    folder = tmp_path / "loose"
    folder.mkdir()
    (folder / "a.jpeg").write_bytes(make_jpeg())
    out = tmp_path / "out"
    assert run_cli("--source", str(folder), "--out", str(out), "--keep-names") == 0
    assert [p.name for p in out.iterdir()] == ["a.jpeg"]
    assert run_cli("--source", str(folder), "--out", str(out), "--keep-names") == 0
    assert "already exists" in capsys.readouterr().out
    assert [p.name for p in out.iterdir()] == ["a.jpeg"]


def test_dry_run_writes_nothing(tmp_path, capsys):
    root = card(tmp_path)
    (root / "DCIM" / "PICT0001.JPG").write_bytes(make_jpeg())
    (root / "DCIM" / "PICT0002.AVI").write_bytes(minimal_avi())
    out = tmp_path / "fixed"
    assert run_cli("--source", str(root), "--out", str(out), "--dry-run", "--verbose") == 0
    text = capsys.readouterr().out
    assert not out.exists()
    assert text.count("would write") == 2
    assert "replaces 2010-06-29 in IDIT, ICRD" in text
    assert "Dry run, nothing written" in text


def test_cli_fixes_avi_date_in_the_copy(tmp_path, capsys):
    root = card(tmp_path)
    src = root / "DCIM" / "PICT0002.AVI"
    original = minimal_avi()
    src.write_bytes(original)
    set_mtime(src, RECORDED)
    out = tmp_path / "fixed"

    assert run_cli("--source", str(root), "--out", str(out)) == 0
    text = capsys.readouterr().out
    assert "1440x1080" in text and "replaces 2010-06-29 in IDIT, ICRD" in text and "1 videos date-fixed" in text
    produced = [p for p in out.iterdir()]
    assert len(produced) == 1 and produced[0].name.startswith("VID_20260807_184512_") and produced[0].suffix == ".avi"
    with open(produced[0], "rb") as fh:
        after = ex.read_avi(fh)
    assert not ex.has_bogus_avi_date(after)
    assert after.date_fields[0].value.rstrip("\x00") == "Fri Aug  7 18:45:12 2026\n"
    assert produced[0].stat().st_size == len(original)
    assert produced[0].stat().st_mtime == pytest.approx(src.stat().st_mtime, abs=1e-6)
    assert src.read_bytes() == original


def test_cli_video_copy_leaves_the_avi_unchanged(tmp_path):
    root = card(tmp_path)
    original = minimal_avi()
    (root / "DCIM" / "PICT0002.AVI").write_bytes(original)
    out = tmp_path / "fixed"
    assert run_cli("--source", str(root), "--out", str(out), "--video", "copy") == 0
    produced = list(out.iterdir())
    assert len(produced) == 1 and produced[0].read_bytes() == original


def test_cli_transcode_falls_back_without_ffmpeg(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ex, "find_ffmpeg", lambda: None)
    root = card(tmp_path)
    (root / "DCIM" / "PICT0002.AVI").write_bytes(minimal_avi())
    out = tmp_path / "fixed"
    assert run_cli("--source", str(root), "--out", str(out), "--video", "transcode") == 0
    text = capsys.readouterr().out
    assert "ffmpeg is not on PATH" in text and "Falling back" in text
    produced = list(out.iterdir())
    assert len(produced) == 1 and produced[0].suffix == ".avi"
    with open(produced[0], "rb") as fh:
        assert not ex.has_bogus_avi_date(ex.read_avi(fh))


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not on PATH")
def test_cli_transcode_produces_mp4_with_creation_time(tmp_path, capsys):
    root = card(tmp_path)
    plain = tmp_path / "plain.avi"
    ffmpeg_avi(plain)
    src = root / "DCIM" / "PICT0002.AVI"
    src.write_bytes(inject_dates(plain.read_bytes()))
    set_mtime(src, RECORDED)
    out = tmp_path / "fixed"

    assert run_cli("--source", str(root), "--out", str(out), "--video", "transcode") == 0
    assert "1 videos transcoded" in capsys.readouterr().out
    produced = list(out.iterdir())
    assert len(produced) == 1 and produced[0].name.startswith("VID_20260807_184512_") and produced[0].suffix == ".mp4"
    assert produced[0].read_bytes()[4:8] == b"ftyp"
    assert produced[0].stat().st_mtime == pytest.approx(src.stat().st_mtime, abs=1e-6)
    tags = ffprobe_tags(produced[0])
    assert tags["nb_streams"] == "2"
    assert tags["TAG:creation_time"].startswith("2026-08-07T18:45:12")


def test_cli_reports_bad_files_and_exits_1(tmp_path, capsys):
    root = card(tmp_path)
    (root / "DCIM" / "PICT0001.JPG").write_bytes(b"this is not a jpeg")
    (root / "DCIM" / "PICT0002.JPG").write_bytes(make_jpeg())
    out = tmp_path / "fixed"
    assert run_cli("--source", str(root), "--out", str(out)) == 1
    text = capsys.readouterr().out
    assert "PICT0001.JPG FAILED" in text and "1 photos fixed" in text and "1 failed" in text
    assert len(list(out.iterdir())) == 1


def test_cli_missing_source_dir(tmp_path, capsys):
    assert run_cli("--source", str(tmp_path / "nope"), "--out", str(tmp_path / "out")) == 1
    assert "not a directory" in capsys.readouterr().err


def test_cli_autodetect_uses_content_check(tmp_path, capsys, monkeypatch):
    other = tmp_path / "usbstick"
    (other / "DCIM").mkdir(parents=True)
    root = card(tmp_path)
    (root / "DCIM" / "PICT0001.JPG").write_bytes(make_jpeg())
    out = tmp_path / "fixed"
    monkeypatch.setattr(ex, "candidate_volumes", lambda: [other, root])
    assert run_cli("--out", str(out)) == 0
    assert f"Charmera found at {root}" in capsys.readouterr().out
    assert len(list(out.iterdir())) == 1

    monkeypatch.setattr(ex, "candidate_volumes", lambda: [other])
    assert run_cli("--out", str(out)) == 1
    assert "no Charmera found" in capsys.readouterr().err
