"""Tests for the probe / backup / restore commands.

No hardware and no real flashrom: every test puts a fake `flashrom` first on PATH. The fake is a small
Python script that understands `--version`, a bare probe, `-r FILE` (writes the fake chip's 2 MiB) and
`-w FILE` (records what was written). Its behaviour is switched through FAKE_FLASHROM_MODE. Nothing in
this file can touch a real programmer.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from charmera_toolkit import cli, flash
from charmera_toolkit.flash import BUILD_STAMPS, FLASH_SIZE, TAIL_START

FAKE_FLASHROM = r'''
import json, os, sys, time

state = os.environ["FAKE_FLASHROM_STATE"]
mode = os.environ.get("FAKE_FLASHROM_MODE", "ok")
argv = sys.argv[1:]
with open(os.path.join(state, "calls.log"), "a") as f:
    f.write(json.dumps(argv) + "\n")

if argv == ["--version"]:
    print("flashrom 1.4.0 on Linux 6.1.0 (x86_64)")
    print("flashrom is free software, get the source code at https://flashrom.org")
    sys.exit(0)

if mode == "hang":
    time.sleep(20)
    sys.exit(0)

programmer = chip = read_to = write_from = None
i = 0
while i < len(argv):
    a = argv[i]
    if a == "-p":
        programmer = argv[i + 1]; i += 2
    elif a == "-c":
        chip = argv[i + 1]; i += 2
    elif a == "-r":
        read_to = argv[i + 1]; i += 2
    elif a == "-w":
        write_from = argv[i + 1]; i += 2
    else:
        print("fake flashrom: unexpected argument %r" % a, file=sys.stderr)
        sys.exit(3)

chip_path = os.path.join(state, "chip.bin")

def chip_bytes():
    if os.path.exists(chip_path):
        with open(chip_path, "rb") as f:
            return f.read()
    body = (b"SPII" + bytes(range(256)) * 8192)[:0x1E0000]
    return body + b"\xff" * 0x20000

def bump_counter(name):
    p = os.path.join(state, name)
    n = int(open(p).read()) if os.path.exists(p) else 0
    n += 1
    open(p, "w").write(str(n))
    return n

if mode == "nochip":
    print("No EEPROM/flash device found.")
    print("Note: flashrom can never write if the flash chip isn't found automatically.")
    sys.exit(1)

print('Found Unknown flash chip "%s" (2048 kB, SPI) on %s.' % (chip or "SFDP-capable chip", programmer))

if read_to is not None:
    if mode == "readfail":
        print("Error: Reading flash failed, chip returned no data.", file=sys.stderr)
        sys.exit(1)
    data = bytearray(chip_bytes())
    n = bump_counter("reads")
    if mode == "readvary" and n >= 2:
        data[0x1000 + n] ^= 0xFF
    if mode == "readshort":
        data = data[: len(data) // 2]
    with open(read_to, "wb") as f:
        f.write(bytes(data))
    print("Reading flash... done.")
    sys.exit(0)

if write_from is not None:
    with open(write_from, "rb") as f:
        data = f.read()
    with open(os.path.join(state, "written.bin"), "wb") as f:
        f.write(data)
    if mode == "writefail":
        print("Erasing and writing flash chip... FAILED at 0x00010000!", file=sys.stderr)
        sys.exit(1)
    new = bytearray(data)
    if mode == "writecorrupt":
        new[0x175000] ^= 0x01
    with open(chip_path, "wb") as f:
        f.write(bytes(new))
    print("Erasing and writing flash chip... Erase/write done.")
    print("Verifying flash... VERIFIED.")
    sys.exit(0)

print("No operations were specified.")
sys.exit(0)
'''


def make_image(fill: int = 0x5A) -> bytes:
    """A 2 MiB image shaped like a stock dump: magic, both build stamps, a 0xFF tail."""
    body = bytearray(bytes([fill]) * TAIL_START)
    body[0:4] = b"SPII"
    for offset, stamp in BUILD_STAMPS:
        body[offset : offset + len(stamp)] = stamp
    return bytes(body) + b"\xff" * (FLASH_SIZE - TAIL_START)


class FakeFlashrom:
    def __init__(self, state: Path):
        self.state = state

    def set_mode(self, mode: str, monkeypatch) -> None:
        monkeypatch.setenv("FAKE_FLASHROM_MODE", mode)

    def set_chip(self, data: bytes) -> None:
        (self.state / "chip.bin").write_bytes(data)

    def calls(self) -> list:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    def written(self) -> bytes:
        return (self.state / "written.bin").read_bytes()

    def wrote_anything(self) -> bool:
        return (self.state / "written.bin").exists()


@pytest.fixture
def fake_flashrom(tmp_path, monkeypatch) -> FakeFlashrom:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    script = bin_dir / "flashrom_fake.py"
    script.write_text(FAKE_FLASHROM, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "flashrom.bat").write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        shim = bin_dir / "flashrom"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_FLASHROM_STATE", str(state))
    monkeypatch.delenv("FAKE_FLASHROM_MODE", raising=False)
    return FakeFlashrom(state)


def run(argv: list) -> int:
    """Run one of this module's subcommands the way cli.py wires them, isolated from other modules."""
    parser = argparse.ArgumentParser(prog="charmera")
    sub = parser.add_subparsers(dest="command")
    flash.register(sub)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


# ---------------------------------------------------------------------------------------------- wiring


def test_cli_registers_the_three_commands():
    parser = cli.build_parser()
    help_text = parser.format_help()
    for name in ("probe", "backup", "restore"):
        assert name in help_text


def test_chip_args_default_and_auto():
    assert flash.chip_args("SFDP-capable chip") == ["-c", "SFDP-capable chip"]
    assert flash.chip_args("auto") == []
    assert flash.chip_args("AUTO") == []
    assert flash.chip_args("") == []
    assert flash.chip_args(None) == []


# ---------------------------------------------------------------------------------------------- probe


def test_parse_probe_found_sfdp():
    text = (
        "flashrom 1.4.0 on Linux 6.1.0 (x86_64)\n"
        "Using clock_gettime for delay loops (clk_id: 1, resolution: 1ns).\n"
        'Found Unknown flash chip "SFDP-capable chip" (2048 kB, SPI) on ch341a_spi.\n'
        "===\n"
        "This flash part has status UNTESTED for operations: WP\n"
        "No operations were specified.\n"
    )
    parsed = flash.parse_probe_output(text)
    assert parsed.ok
    assert parsed.found == [
        {"vendor": "Unknown", "name": "SFDP-capable chip", "size_kb": "2048", "bus": "SPI", "programmer": "ch341a_spi"}
    ]
    assert any("UNTESTED" in n for n in parsed.notes)
    assert not parsed.no_device


def test_parse_probe_no_device():
    text = "flashrom 1.2 on Linux\nNo EEPROM/flash device found.\nNote: flashrom can never write if the flash chip isn't found automatically.\n"
    parsed = flash.parse_probe_output(text)
    assert parsed.no_device
    assert not parsed.ok
    assert parsed.found == []


def test_parse_probe_multiple_definitions():
    text = (
        'Found Winbond flash chip "W25Q16.V" (2048 kB, SPI) on ch341a_spi.\n'
        'Found Winbond flash chip "W25Q16JV_M" (2048 kB, SPI) on ch341a_spi.\n'
        'Multiple flash chip definitions match the detected chip(s): "W25Q16.V", "W25Q16JV_M"\n'
        "Please specify which chip definition to use with the -c <chipname> option.\n"
    )
    parsed = flash.parse_probe_output(text)
    assert len(parsed.found) == 2
    assert parsed.multiple == ["W25Q16.V", "W25Q16JV_M"]
    assert not parsed.ok


def test_parse_probe_programmer_failure():
    text = (
        "Error: Could not find a CH341A device, or the device could not be opened.\n"
        "Error: Programmer initialization failed.\n"
    )
    parsed = flash.parse_probe_output(text)
    assert not parsed.ok
    assert len(parsed.problems) == 2


def test_probe_command_finds_chip(fake_flashrom, capsys):
    assert run(["probe"]) == 0
    out = capsys.readouterr().out
    assert "flashrom 1.4.0" in out
    assert 'Found: Unknown "SFDP-capable chip", 2048 kB, SPI on ch341a_spi' in out
    calls = fake_flashrom.calls()
    assert calls[0] == ["--version"]
    assert calls[1] == ["-p", "ch341a_spi", "-c", "SFDP-capable chip"]
    assert not fake_flashrom.wrote_anything()


def test_probe_command_chip_auto_omits_c(fake_flashrom):
    assert run(["probe", "--chip", "auto"]) == 0
    assert fake_flashrom.calls()[1] == ["-p", "ch341a_spi"]


def test_probe_command_no_chip(fake_flashrom, monkeypatch, capsys):
    fake_flashrom.set_mode("nochip", monkeypatch)
    assert run(["probe"]) == 1
    out = capsys.readouterr().out
    assert "No flash chip was detected" in out


def test_flashrom_missing_gives_install_hints(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert run(["probe"]) == 1
    err = capsys.readouterr().err
    assert "flashrom was not found" in err
    assert "--flashrom" in err


def test_flashrom_explicit_path_missing(tmp_path, capsys):
    assert run(["probe", "--flashrom", str(tmp_path / "nope")]) == 1
    err = capsys.readouterr().err
    assert "is not a file" in err


def test_run_flashrom_timeout(fake_flashrom, monkeypatch):
    fake_flashrom.set_mode("hang", monkeypatch)
    exe = flash.find_flashrom(None)
    with pytest.raises(flash.FlashromError) as info:
        flash.run_flashrom(exe, ["-p", "ch341a_spi"], timeout=0.5, echo=False)
    assert "did not finish" in str(info.value)


# ---------------------------------------------------------------------------------------------- backup


def test_backup_happy_path(fake_flashrom, tmp_path, capsys):
    image = make_image()
    fake_flashrom.set_chip(image)
    out_dir = tmp_path / "backups"
    assert run(["backup", "--out", str(out_dir), "--label", "mine"]) == 0

    bins = sorted(out_dir.glob("mine_*.bin"))
    assert len(bins) == 1, list(out_dir.iterdir())
    final = bins[0]
    assert ".read" not in final.name
    assert final.read_bytes() == image
    assert not list(out_dir.glob("*.read*.bin"))

    expected_sha = flash.sha256_hex(image)
    sha_file = out_dir / (final.stem + ".sha256")
    assert sha_file.read_text() == f"{expected_sha}  {final.name}\n"

    manifest = json.loads((out_dir / (final.stem + ".manifest.json")).read_text())
    assert manifest["file"] == final.name
    assert manifest["sha256"] == expected_sha
    assert manifest["size"] == FLASH_SIZE
    assert manifest["size_ok"] is True
    assert manifest["magic_ok"] is True
    assert manifest["build_stamps"] == {"0x18964": "JH5408  20250520", "0x218C4": "202505201029"}
    assert manifest["build_stamps_ok"] is True
    assert manifest["tail_blank"] is True
    assert manifest["public_dump"] is None
    assert manifest["reads"] == 2
    assert manifest["reads_identical"] is True
    assert manifest["programmer"] == "ch341a_spi"
    assert manifest["chip"] == "SFDP-capable chip"
    assert manifest["flashrom_version"].startswith("flashrom 1.4.0")
    assert manifest["warnings"] == []
    assert manifest["timestamp_utc"].endswith("+00:00")

    calls = fake_flashrom.calls()
    reads = [c for c in calls if "-r" in c]
    assert len(reads) == 2
    for c in reads:
        assert c[:4] == ["-p", "ch341a_spi", "-c", "SFDP-capable chip"]
    assert not any("-w" in c for c in calls)
    assert not fake_flashrom.wrote_anything()

    out = capsys.readouterr().out
    assert expected_sha in out


def test_backup_reads_n_times_and_chip_auto(fake_flashrom, tmp_path):
    fake_flashrom.set_chip(make_image())
    assert run(["backup", "--out", str(tmp_path / "b"), "--reads", "3", "--chip", "auto"]) == 0
    reads = [c for c in fake_flashrom.calls() if "-r" in c]
    assert len(reads) == 3
    assert all("-c" not in c for c in reads)
    manifest = json.loads(next((tmp_path / "b").glob("*.manifest.json")).read_text())
    assert manifest["chip"] == "auto"
    assert manifest["reads"] == 3


def test_backup_differing_reads_exits_1_and_keeps_reads(fake_flashrom, tmp_path, monkeypatch, capsys):
    fake_flashrom.set_chip(make_image())
    fake_flashrom.set_mode("readvary", monkeypatch)
    out_dir = tmp_path / "b"
    assert run(["backup", "--out", str(out_dir)]) == 1
    kept = sorted(p.name for p in out_dir.iterdir())
    assert len(kept) == 2
    assert all(".read" in n and n.endswith(".bin") for n in kept)
    assert not list(out_dir.glob("*.sha256"))
    assert not list(out_dir.glob("*.manifest.json"))
    captured = capsys.readouterr()
    assert "not identical" in captured.err
    assert "first difference at 0x1002" in captured.out


def test_backup_flashrom_read_failure(fake_flashrom, tmp_path, monkeypatch, capsys):
    fake_flashrom.set_mode("readfail", monkeypatch)
    assert run(["backup", "--out", str(tmp_path / "b")]) == 1
    captured = capsys.readouterr()
    assert "read 1 failed" in captured.err
    assert "chip returned no data" in captured.out


def test_backup_keeps_blank_chip_with_warnings(fake_flashrom, tmp_path, capsys):
    fake_flashrom.set_chip(b"\xff" * FLASH_SIZE)
    out_dir = tmp_path / "b"
    assert run(["backup", "--out", str(out_dir)]) == 0
    final = next(out_dir.glob("charmera_*.bin"))
    assert final.read_bytes() == b"\xff" * FLASH_SIZE
    manifest = json.loads((out_dir / (final.stem + ".manifest.json")).read_text())
    assert manifest["magic_ok"] is False
    assert manifest["build_stamps_ok"] is False
    assert manifest["build_stamps"] == {"0x18964": None, "0x218C4": None}
    assert manifest["tail_blank"] is True
    assert any("SPII" in w for w in manifest["warnings"])
    out = capsys.readouterr().out
    assert "kept anyway" in out


def test_backup_short_read_is_saved_with_size_warning(fake_flashrom, tmp_path, monkeypatch):
    fake_flashrom.set_chip(make_image())
    fake_flashrom.set_mode("readshort", monkeypatch)
    out_dir = tmp_path / "b"
    assert run(["backup", "--out", str(out_dir)]) == 0
    manifest = json.loads(next(out_dir.glob("*.manifest.json")).read_text())
    assert manifest["size"] == FLASH_SIZE // 2
    assert manifest["size_ok"] is False
    assert manifest["tail_blank"] is False
    assert any("expected 2,097,152" in w for w in manifest["warnings"])


def test_backup_recognizes_public_dump(fake_flashrom, tmp_path, monkeypatch, capsys):
    image = make_image(0x11)
    fake_flashrom.set_chip(image)
    monkeypatch.setitem(flash.PUBLIC_DUMPS, flash.sha256_hex(image), "example/public-dump")
    out_dir = tmp_path / "b"
    assert run(["backup", "--out", str(out_dir)]) == 0
    manifest = json.loads(next(out_dir.glob("*.manifest.json")).read_text())
    assert manifest["public_dump"] == "example/public-dump"
    assert "example/public-dump" in capsys.readouterr().out


def test_backup_label_is_sanitized(fake_flashrom, tmp_path):
    fake_flashrom.set_chip(make_image())
    out_dir = tmp_path / "b"
    assert run(["backup", "--out", str(out_dir), "--label", "my cam/#1"]) == 0
    final = next(out_dir.glob("*.bin"))
    assert final.name.startswith("my_cam_1_")


def test_backup_rejects_zero_reads(fake_flashrom, tmp_path, capsys):
    assert run(["backup", "--out", str(tmp_path / "b"), "--reads", "0"]) == 1
    assert "--reads" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------- restore


def test_restore_refuses_wrong_size(fake_flashrom, tmp_path, capsys):
    image = tmp_path / "short.bin"
    image.write_bytes(make_image()[:-1])
    assert run(["restore", "--image", str(image)]) == 1
    err = capsys.readouterr().err
    assert "2,097,152" in err
    assert "Nothing was written" in err
    assert not fake_flashrom.wrote_anything()
    assert fake_flashrom.calls() == []


def test_restore_refuses_wrong_magic(fake_flashrom, tmp_path, capsys):
    data = bytearray(make_image())
    data[0:4] = b"XXXX"
    image = tmp_path / "nomagic.bin"
    image.write_bytes(bytes(data))
    assert run(["restore", "--image", str(image)]) == 1
    assert "SPII" in capsys.readouterr().err
    assert not fake_flashrom.wrote_anything()


def test_restore_refuses_missing_build_stamp_without_flag(fake_flashrom, tmp_path, capsys):
    data = bytearray(make_image())
    offset, stamp = BUILD_STAMPS[1]
    data[offset : offset + len(stamp)] = b"202601010000"
    image = tmp_path / "other.bin"
    image.write_bytes(bytes(data))
    assert run(["restore", "--image", str(image)]) == 1
    err = capsys.readouterr().err
    assert "0x218C4" in err
    assert "--allow-unknown-build" in err
    assert not fake_flashrom.wrote_anything()


def test_restore_missing_build_stamp_allowed_with_flag(fake_flashrom, tmp_path, monkeypatch, capsys):
    data = bytearray(make_image())
    offset, stamp = BUILD_STAMPS[0]
    data[offset : offset + len(stamp)] = b"X" * len(stamp)
    image = tmp_path / "other.bin"
    image.write_bytes(bytes(data))
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert run(["restore", "--image", str(image), "--allow-unknown-build"]) == 1
    captured = capsys.readouterr()
    assert "continuing because --allow-unknown-build" in captured.out
    assert "Stopped. Nothing was written." in captured.out
    assert not fake_flashrom.wrote_anything()


def test_restore_declined_writes_nothing(fake_flashrom, tmp_path, monkeypatch, capsys):
    image = tmp_path / "stock.bin"
    image.write_bytes(make_image())
    monkeypatch.setattr("builtins.input", lambda prompt="": "restore")  # wrong case is a refusal
    assert run(["restore", "--image", str(image)]) == 1
    assert "Stopped. Nothing was written." in capsys.readouterr().out
    assert not fake_flashrom.wrote_anything()
    assert not any("-w" in c for c in fake_flashrom.calls())


def test_restore_happy_path(fake_flashrom, tmp_path, monkeypatch, capsys):
    data = make_image()
    image = tmp_path / "stock.bin"
    image.write_bytes(data)
    fake_flashrom.set_chip(make_image(0x00))  # the chip holds something else before the write
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return "RESTORE"

    monkeypatch.setattr("builtins.input", fake_input)
    assert run(["restore", "--image", str(image)]) == 0

    assert prompts and "RESTORE" in prompts[0]
    assert fake_flashrom.written() == data
    calls = fake_flashrom.calls()
    ops = [c for c in calls if "-w" in c or "-r" in c]
    assert len(ops) == 2
    assert ops[0][:4] == ["-p", "ch341a_spi", "-c", "SFDP-capable chip"]
    assert ops[0][4:] == ["-w", str(image)]
    assert ops[1][4] == "-r"
    out = capsys.readouterr().out
    assert flash.sha256_hex(data) in out
    assert "Plan:" in out
    assert "Restore verified" in out
    assert not Path(ops[1][5]).exists()  # the read-back temp file is removed after a verified restore


def test_restore_readback_mismatch_exits_nonzero(fake_flashrom, tmp_path, monkeypatch, capsys):
    data = make_image()
    image = tmp_path / "stock.bin"
    image.write_bytes(data)
    fake_flashrom.set_mode("writecorrupt", monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "RESTORE")
    assert run(["restore", "--image", str(image)]) == 1
    captured = capsys.readouterr()
    assert "does not match the image after writing" in captured.err
    assert "block 23" in captured.out  # 0x175000 is in the settings store block
    assert "settings store" in captured.out
    readback = [c for c in fake_flashrom.calls() if "-r" in c][0]
    assert Path(readback[5]).exists()  # kept for diagnosis


def test_restore_write_failure_exits_nonzero(fake_flashrom, tmp_path, monkeypatch, capsys):
    image = tmp_path / "stock.bin"
    image.write_bytes(make_image())
    fake_flashrom.set_mode("writefail", monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "RESTORE")
    assert run(["restore", "--image", str(image)]) == 1
    captured = capsys.readouterr()
    assert "flashrom -w failed" in captured.err
    assert "FAILED at 0x00010000" in captured.out
    assert not any("-r" in c for c in fake_flashrom.calls())


def test_restore_prints_block_diff_against_backup(fake_flashrom, tmp_path, monkeypatch, capsys):
    stock = make_image()
    backup = bytearray(stock)
    backup[0x170000:0x170100] = b"\x01" * 0x100  # a settings record only the owner has
    backup[0x1A5800:0x1A5810] = b"\x02" * 0x10  # second photo slot
    image = tmp_path / "stock.bin"
    image.write_bytes(stock)
    backup_path = tmp_path / "mine.bin"
    backup_path.write_bytes(bytes(backup))
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert run(["restore", "--image", str(image), "--backup", str(backup_path)]) == 1
    out = capsys.readouterr().out
    assert "2 of 32 differ" in out
    assert "block 23  0x170000-0x17FFFF      256 bytes differ  (settings store)" in out
    assert "block 26  0x1A0000-0x1AFFFF       16 bytes differ  (internally stored photos)" in out
    assert "30 blocks are identical" in out
    assert flash.sha256_hex(bytes(backup)) in out


def test_restore_identical_backup(fake_flashrom, tmp_path, monkeypatch, capsys):
    stock = make_image()
    image = tmp_path / "stock.bin"
    image.write_bytes(stock)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert run(["restore", "--image", str(image), "--backup", str(image)]) == 1
    assert "identical to the backup in all 32 blocks" in capsys.readouterr().out


def test_restore_notes_public_dump(fake_flashrom, tmp_path, monkeypatch, capsys):
    stock = make_image(0x33)
    image = tmp_path / "public.bin"
    image.write_bytes(stock)
    monkeypatch.setitem(flash.PUBLIC_DUMPS, flash.sha256_hex(stock), "example/public-dump")
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert run(["restore", "--image", str(image)]) == 1
    out = capsys.readouterr().out
    assert "public dump from example/public-dump" in out
    assert "last resort" in out


def test_restore_without_flashrom_stops_before_prompt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    image = tmp_path / "stock.bin"
    image.write_bytes(make_image())
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt without flashrom"))
    assert run(["restore", "--image", str(image)]) == 1
    assert "flashrom was not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------- helpers


def test_analyze_image_and_block_diff():
    stock = make_image()
    info = flash.analyze_image(stock)
    assert info["size_ok"] and info["magic_ok"] and info["build_stamps_ok"] and info["tail_blank"]
    assert info["public_dump"] is None
    assert flash.image_warnings(info) == []

    other = bytearray(stock)
    other[0x1F0000] = 0x00
    diffs = flash.block_diff(stock, bytes(other))
    assert [(d.index, d.differing_bytes, d.region) for d in diffs] == [(31, 1, "tail, 0xFF in every known dump")]
    info2 = flash.analyze_image(bytes(other))
    assert info2["tail_blank"] is False
    assert any("not all 0xFF" in w for w in flash.image_warnings(info2))

    shorter = flash.block_diff(stock, stock[:FLASH_SIZE - 10])
    assert shorter[-1].index == 31 and shorter[-1].differing_bytes == 10
