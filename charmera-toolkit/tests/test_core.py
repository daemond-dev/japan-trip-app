"""Core tests: checksum, settings, image parsing, verify and pack logic.

Image-dependent tests run only when CHARMERA_STOCK_IMAGE points at a local original-series dump.
Everything that writes uses synthetic images so no real firmware file is produced by the test run.
"""
import os
import struct
import zlib
from pathlib import Path

import pytest

from charmera_toolkit import checksum, image, pack, settings, sounds, verify


# --- checksum ------------------------------------------------------------------------------------------


def test_byte_sum_and_names():
    assert checksum.byte_sum(b"\x01\x02\x03\x04") == 10
    assert checksum.byte_sum(b"\xff" * 0x1000000) == (0xFF * 0x1000000) & 0xFFFFFFFF
    n = checksum.parse_update_name("JH_5408202610261427_0F065D55.bin")
    assert (n.product, n.stamp, n.checksum) == ("5408", "202610261427", 0x0F065D55)
    assert n.filename == "JH_5408202610261427_0F065D55.bin"
    with pytest.raises(ValueError):
        checksum.parse_update_name("FW_5408202610261427_0F065D55.bin")
    with pytest.raises(ValueError):
        checksum.build_update_name(b"\x00", stamp=checksum.STOCK_TIMESTAMP)
    assert checksum.build_update_name(b"\x00" * 100, stamp="202610261200").filename == "JH_5408202610261200_00000000.bin"


# --- settings ------------------------------------------------------------------------------------------


def make_record(ui_sound=5, playback=5):
    body = bytearray(252)
    body[0] = 1
    body[settings.OFF_PLAYBACK_VOLUME] = playback
    body[settings.OFF_UI_SOUND] = ui_sound
    return bytes(body) + (zlib.crc32(bytes(body)) & 0xFFFFFFFF).to_bytes(4, "little")


def synthetic_image(records=(), with_sounds=True):
    """A fake 2 MiB image with a GP+ pack holding five tiny WAVs and the given settings records."""
    img = bytearray(b"\xff" * checksum.FLASH_SIZE)
    img[0:4] = b"SPII"
    img[0x200:0x204] = b"\x18\xf0\x9f\xe5"
    # build stamps so detect_build finds the original series (software hash will not match: that is expected)
    img[image.ORIGINAL_2025.stamp_offset: image.ORIGINAL_2025.stamp_offset + 12] = image.ORIGINAL_2025.stamp.encode()
    img[image.ORIGINAL_2025.build_offset: image.ORIGINAL_2025.build_offset + 16] = image.ORIGINAL_2025.build_string
    img[image.ORIGINAL_2025.compiled_offset: image.ORIGINAL_2025.compiled_offset + 11] = image.ORIGINAL_2025.compiled
    if with_sounds:
        base = image.RESOURCE_BASE
        img[base: base + 0x18] = b"GP+\x00\x03\x00\x03\x00" + b"\x00" * 16
        names = ["BEEPWAV", "CLICKWAV", "IMGWAV", "POWEROFF_AUDIOWAV", "POWERON_AUDIOWAV", "ZZZGPZP"]
        sector = 4
        off = base + 0x18
        for i, n in enumerate(names):
            img[off: off + 20] = n.encode().ljust(20, b"\x00")
            img[off + 20: off + 24] = struct.pack("<I", sector)
            if n != "ZZZGPZP":
                bits = 16 if n == "BEEPWAV" else 8
                rate = 11025 if n in ("BEEPWAV", "IMGWAV") else 8000
                data = bytes([0x80 if bits == 8 else 0x11]) * 400
                wav = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * bits // 8, bits // 8, bits) + b"data" + struct.pack("<I", len(data)) + data
                p = base + sector * 0x200
                img[p: p + len(wav)] = wav
            else:
                p = base + sector * 0x200
                img[p: p + 8] = b"GPZP\x00\x00\x00\x00"
            off += 24
            sector += 2
    for i, rec in enumerate(records):
        o = settings.SETTINGS_BASE + i * settings.RECORD_STRIDE
        img[o: o + 256] = rec
        img[o + 256: o + 512] = b"\x00" * 256
    img[settings.PHOTO_DIRECTORY: settings.PHOTO_DIRECTORY + 0x200] = b"\x00" * 0x200
    return bytes(img)


def test_settings_records_and_current():
    img = synthetic_image([make_record(5), make_record(3), b"\x00" * 256])
    recs = settings.parse_settings(img)
    assert len(recs) == 3
    assert [r.crc_ok for r in recs] == [True, True, False]
    # the firmware loads the sector before the first erased one; the zero record is not erased, so it is
    # selected and fails its CRC (the camera would fall back to defaults)
    assert settings.firmware_current_sector(img) == settings.SETTINGS_BASE + 2 * settings.RECORD_STRIDE
    with pytest.raises(ValueError):
        settings.current_record(img)
    img2 = synthetic_image([make_record(5), make_record(3)])
    cur = settings.current_record(img2)
    assert cur.ui_sound == 3
    new = cur.with_byte(settings.OFF_UI_SOUND, 0)
    assert new.crc_ok and new.ui_sound == 0 and new.playback_volume == 5
    assert settings.diff_records(cur, new) == [settings.OFF_UI_SOUND]


def test_set_ui_sound_rewrites_store():
    img = synthetic_image([make_record(5), make_record(4)])
    out = sounds.set_ui_sound(img, 0)
    recs = settings.parse_settings(out)
    assert len(recs) == 1 and recs[0].crc_ok and recs[0].ui_sound == 0
    assert out[settings.SETTINGS_BASE + 256: settings.SETTINGS_BASE + 512] == b"\x00" * 256
    assert settings.current_record(out).ui_sound == 0
    assert out[: settings.SETTINGS_BASE] == img[: settings.SETTINGS_BASE]
    assert out[settings.SETTINGS_END:] == img[settings.SETTINGS_END:]
    # an image with an erased directory gets a zeroed one
    bad = bytearray(img); bad[settings.PHOTO_DIRECTORY: settings.PHOTO_DIRECTORY + 0x200] = b"\xff" * 0x200
    assert not settings.photo_directory_consistent(bytes(bad))
    fixed = sounds.set_ui_sound(bytes(bad), 0)
    assert settings.photo_directory_consistent(fixed) and fixed[settings.PHOTO_DIRECTORY: settings.PHOTO_DIRECTORY + 8] == b"\x00" * 8


def test_mute_pcm_and_replace(tmp_path):
    img = synthetic_image([make_record()])
    slots = image.sound_slots(img)
    assert {s.entry.name for s in slots} == set(image.SOUND_SLOTS)
    muted = sounds.mute_pcm(img)
    for s in image.sound_slots(muted):
        assert muted[s.data_offset: s.data_offset + s.data_len] == bytes([s.silence_byte]) * s.data_len
        assert muted[s.entry.offset: s.data_offset] == img[s.entry.offset: s.data_offset]
    # replacement: a 1 kHz-ish stereo 16-bit 44.1 kHz clip is converted to the slot's format and length
    import wave
    p = tmp_path / "clip.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"".join(struct.pack("<hh", (i % 50) * 600 - 15000, (i % 50) * 600 - 15000) for i in range(2000)))
    rep = sounds.replace_sound(img, "BEEP.WAV", p)
    s = [x for x in image.sound_slots(rep) if x.entry.name == "BEEPWAV"][0]
    assert s.data_len == 400 and rep[s.entry.offset: s.data_offset] == img[s.entry.offset: s.data_offset]
    assert rep[s.data_offset: s.data_offset + s.data_len] != img[s.data_offset: s.data_offset + s.data_len]
    with pytest.raises(ValueError):
        sounds.replace_sound(img, "NOPE.WAV", p)


def test_pack_prepare_and_verify_synthetic(tmp_path):
    img = synthetic_image([make_record()])
    payload = pack.prepare_payload(img)
    assert len(payload) == checksum.UPDATE_FILE_SIZE
    name = checksum.build_update_name(payload, "202611041200")
    path = tmp_path / name.filename
    checks = verify.verify_update(path, payload)
    by = {c.name: c for c in checks}
    assert by["filename"].ok and by["checksum"].ok and by["stamp"].ok and by["size"].ok and by["magic"].ok
    assert by["series"].ok and not by["software-region"].ok  # synthetic software region is not stock
    # a renamed file fails the checksum
    bad = tmp_path / "JH_5408202611041200_00000001.bin"
    assert not {c.name: c for c in verify.verify_update(bad, payload)}["checksum"].ok
    # a 2 MiB file is flagged, and the extra-erase arithmetic matches the verified updater
    full = {c.name: c for c in verify.verify_update(tmp_path / name.filename, img)}
    assert "size-2mib" in full and not full["size-2mib"].ok and not full["final-erase"].ok
    assert image.updater_extra_erase(0x1E0000) == 0x1F0000
    assert image.updater_extra_erase(0x169000) == 0x170000
    assert image.updater_extra_erase(0x170000) == 0x180000
    assert image.updater_extra_erase(0x200000) == 0x210000
    assert image.updater_extra_erase(0x168E9A) == 0x170000
    odd = {c.name: c for c in verify.verify_update(tmp_path / name.filename, payload[:-1])}
    assert not odd["size"].ok
    # blanking photos leaves a zeroed directory, blanking settings leaves the bank erased
    blank = pack.prepare_payload(img, blank_settings=True, blank_photos=True)
    assert settings.parse_settings(blank) == []
    assert blank[settings.PHOTO_DIRECTORY: settings.PHOTO_DIRECTORY + 0x200] == b"\x00" * 0x200
    assert blank[settings.PHOTO_DIRECTORY + 0x200: settings.PHOTO_DIRECTORY + 0x400] == b"\xff" * 0x200
    with pytest.raises(ValueError):
        pack.prepare_payload(img[:-1] + b"\x00")


# --- against the real stock image -----------------------------------------------------------------------


def test_stock_image_identity(stock_image):
    info = image.summarize(stock_image)
    assert info["kind"] == "dump" and info["magic_ok"]
    assert info["series"] == "original-2025" and all(info["series_checks"].values())
    assert set(info["series_checks"]) == {"stamp", "build_string", "compiled", "code_sha256", "sensor_table", "software_sha256"}
    assert info["software_sha256"] == image.ORIGINAL_2025.software_sha256
    assert info["tail_blank"] is True
    assert len(info["resources"]) == 43
    names = [s.entry.display_name for s in info["sounds"]]
    assert names == ["BEEP.WAV", "CLICK.WAV", "IMG.WAV", "POWEROFF_AUDIO.WAV", "POWERON_AUDIO.WAV"]
    fmts = {s.entry.display_name: (s.rate, s.bits, s.data_len) for s in info["sounds"]}
    assert fmts["BEEP.WAV"] == (11025, 16, 4032)
    assert fmts["IMG.WAV"] == (11025, 8, 5480)
    assert fmts["POWERON_AUDIO.WAV"] == (8000, 8, 6097)
    assert info["current_settings"].ui_sound == 5 and info["current_settings"].playback_volume == 5
    assert info["resources_end"] == 0x168E9A
    # the published stock update file is exactly the first 0x1E0000 bytes with byte sum 0F065D55 for the
    # keychain-camera-plus dump; the Project-Sirius dump differs only above 0x170000
    payload = pack.prepare_payload(stock_image)
    if info["sha256"] == "cc3b4b2fd7eb71ba5a441decdc86984261fac8418ee3e105d7e2b3c38b7ea42f":
        assert checksum.byte_sum(payload) == 0x0F065D55


def test_stock_image_mute_paths_do_not_touch_code(stock_image):
    muted = sounds.mute_pcm(stock_image)
    diff = [i for i in range(0, len(stock_image), 0x10000) if muted[i: i + 0x10000] != stock_image[i: i + 0x10000]]
    assert all(0x120000 <= b < 0x170000 for b in diff)
    quiet = sounds.set_ui_sound(stock_image, 0)
    diff = [i for i in range(0, len(stock_image), 0x10000) if quiet[i: i + 0x10000] != stock_image[i: i + 0x10000]]
    assert diff == [0x170000]
    assert settings.current_record(quiet).ui_sound == 0
