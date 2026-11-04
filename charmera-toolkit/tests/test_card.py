"""card-check / card-fix against image files only. No device is ever opened."""
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from charmera_toolkit import card
from charmera_toolkit import cli

MIB = 1024 * 1024
S = card.SECTOR
IMAGE_ARGS = {"yes": True, "i_know_what_i_am_doing": False}


def sparse_image(path: Path, size: int) -> Path:
    with open(path, "wb") as f:
        f.truncate(size)
    return path


def exfat_boot_region(size: int, flags: int) -> bytes:
    """Sectors 0..11 of a hand-made exFAT volume: boot sector, 8 extended boot sectors, OEM
    parameters, a reserved sector and the checksum sector."""
    boot = bytearray(S)
    boot[0:3] = b"\xEB\x76\x90"
    boot[3:11] = b"EXFAT   "
    volume_sectors = size // S - 2048
    struct.pack_into(
        "<QQIIIIIIBBHBBBBB",
        boot,
        64,
        2048,  # PartitionOffset
        volume_sectors,  # VolumeLength
        128,  # FatOffset
        64,  # FatLength
        256,  # ClusterHeapOffset
        (volume_sectors - 256) // 8,  # ClusterCount
        4,  # FirstClusterOfRootDirectory
        0xC0FFEE11,  # VolumeSerialNumber
        0,  # revision minor
        1,  # revision major
        flags,  # VolumeFlags
        9,  # BytesPerSectorShift
        3,  # SectorsPerClusterShift
        1,  # NumberOfFats
        0x80,  # DriveSelect
        7,  # PercentInUse
    )
    boot[510:512] = b"\x55\xAA"
    ext = bytearray(S)
    ext[508:512] = b"\x00\x00\x55\xAA"
    region = bytes(boot) + bytes(ext) * 8 + bytes(S) * 2
    checksum = card.exfat_boot_checksum(region)
    return region + struct.pack("<I", checksum) * (S // 4)


def make_exfat_image(path: Path, size: int = 32 * MIB, flags: int = card.EXFAT_FLAG_DIRTY) -> Path:
    sparse_image(path, size)
    region = exfat_boot_region(size, flags)
    with open(path, "r+b") as fh:
        card.write_at(fh, 0, card.build_mbr(2048, size // S - 2048, ptype=0x07, disk_signature=0xABCD1234))
        card.write_at(fh, 2048 * S, region)  # main boot region, sectors 0..11
        card.write_at(fh, (2048 + 12) * S, region)  # backup boot region, sectors 12..23
    return path


def check(path: Path) -> card.CheckResult:
    with open(path, "rb") as fh:
        return card.check_disk(fh, os.path.getsize(path))


# ---------------------------------------------------------------------------------------------
# exFAT dirty bit


def test_exfat_dirty_bit_is_parsed_reported_and_cleared(tmp_path):
    img = make_exfat_image(tmp_path / "exfat.img")
    before = check(img)
    assert before.boot is not None and before.boot.kind == "exfat"
    x = before.boot.exfat
    assert x.dirty and not x.media_failure and x.volume_flags == 0x0002
    assert x.bytes_per_sector == 512 and x.serial == 0xC0FFEE11 and x.percent_in_use == 7
    assert before.exfat_checksum_ok is True
    assert before.exit_code == 1
    assert any("VolumeDirty" in p for p in before.problems)
    assert before.platforms["Android"].startswith("refuses")
    assert before.platforms["iPhone"].startswith("mounts")
    assert before.fixes[0] == "--clear-dirty"
    report = card.format_report(before, "--image exfat.img")
    assert "card-fix --image exfat.img --clear-dirty" in report
    assert "card-fix --image exfat.img --format-fat32" in report

    with open(img, "r+b") as fh:
        checksum_before = card.read_at(fh, (2048 + 11) * S, S)
        res = card.clear_exfat_dirty_bit(fh, 2048 * S)
        assert (res.old_flags, res.new_flags) == (0x0002, 0x0000)
        assert res.sectors_written == [0, 12]
        main = card.parse_exfat_boot_sector(card.read_at(fh, 2048 * S, S))
        backup = card.parse_exfat_boot_sector(card.read_at(fh, (2048 + 12) * S, S))
        assert not main.dirty and not backup.dirty
        # Only the two flag bytes changed.
        region = card.read_at(fh, 2048 * S, 11 * S)
        assert card.exfat_boot_checksum(region) == struct.unpack_from("<I", checksum_before)[0]
        assert card.read_at(fh, (2048 + 11) * S, S) == checksum_before

    after = check(img)
    assert after.exit_code == 0
    assert after.exfat_checksum_ok is True
    assert after.platforms["Android"].startswith("mounts")
    assert "--format-fat32" in after.fixes  # still recommended as the durable fix


def test_exfat_other_flag_bits_survive_the_clear(tmp_path):
    img = make_exfat_image(tmp_path / "exfat.img", flags=card.EXFAT_FLAG_DIRTY | card.EXFAT_FLAG_ACTIVE_FAT | card.EXFAT_FLAG_MEDIA_FAILURE)
    with open(img, "r+b") as fh:
        res = card.clear_exfat_dirty_bit(fh, 2048 * S)
    assert res.new_flags == card.EXFAT_FLAG_ACTIVE_FAT | card.EXFAT_FLAG_MEDIA_FAILURE
    after = check(img)
    assert any("MediaFailure" in p for p in after.problems)


def test_exfat_boot_checksum_ignores_flags_and_percent_in_use():
    region = exfat_boot_region(32 * MIB, 0)[: 11 * S]
    base = card.exfat_boot_checksum(region)
    altered = bytearray(region)
    altered[106] = 0xFF
    altered[107] = 0xFF
    altered[112] = 0x42
    assert card.exfat_boot_checksum(bytes(altered)) == base
    altered[108] ^= 1
    assert card.exfat_boot_checksum(bytes(altered)) != base


# ---------------------------------------------------------------------------------------------
# MBR type


def test_mbr_type_detection_and_fix(tmp_path):
    img = sparse_image(tmp_path / "fat.img", 64 * MIB)
    with open(img, "r+b") as fh:
        card.format_fat32(fh, 64 * MIB, serial=1)
        assert card.set_partition_type(fh, 1, 0x07) == (0x0C, 0x07)
    broken = check(img)
    assert broken.partition.type == 0x07
    assert broken.exit_code == 1
    assert broken.platforms["iPhone"].startswith("refuses")
    assert broken.platforms["Android"] == "mounts"
    assert broken.fixes == ["--fix-mbr-type"]
    with open(img, "r+b") as fh:
        assert card.fix_mbr_type_for_fat32(fh) == (0x07, 0x0C)
    fixed = check(img)
    assert fixed.partition.type == 0x0C and fixed.exit_code == 0
    assert all(v == "mounts" for v in fixed.platforms.values())


def test_fix_mbr_type_refuses_when_not_fat32(tmp_path):
    img = make_exfat_image(tmp_path / "exfat.img")
    with open(img, "r+b") as fh:
        with pytest.raises(card.CardError):
            card.fix_mbr_type_for_fat32(fh)
        assert card.parse_mbr(card.read_at(fh, 0, S)).first.type == 0x07


def test_parse_mbr_and_chs():
    mbr = card.parse_mbr(card.build_mbr(2048, 100000, disk_signature=0x11223344))
    assert mbr.signature_ok and mbr.disk_signature == 0x11223344 and not mbr.gpt_protective
    p = mbr.first
    assert (p.index, p.type, p.start_lba, p.sectors) == (1, 0x0C, 2048, 100000)
    assert p.chs_start == (0x20, 0x21, 0x00)  # LBA 2048 with 255 heads and 63 sectors per track
    assert card.lba_to_chs(0) == (0, 1, 0)
    assert card.lba_to_chs(1024 * 255 * 63) == (0xFE, 0xFF, 0xFF)
    assert card.parse_mbr(card.build_mbr(2048, 1 << 30)).first.chs_end == (0xFE, 0xFF, 0xFF)
    assert all(mbr.partitions[i].empty for i in (1, 2, 3))


# ---------------------------------------------------------------------------------------------
# FAT32 formatting


def fsck_fat():
    return shutil.which("fsck.fat") or shutil.which("dosfsck")


@pytest.mark.parametrize("size_mib", [64, 300])
def test_format_fat32_and_read_it_back(tmp_path, size_mib):
    size = size_mib * MIB
    img = sparse_image(tmp_path / f"card{size_mib}.img", size)
    log = []
    with open(img, "r+b") as fh:
        plan = card.format_fat32(fh, size, label="charmera", serial=0xDEADBEEF, log=log.append)
    assert log and plan.label == "CHARMERA"

    r = check(img)
    assert r.exit_code == 0, r.problems
    assert not r.notes, r.notes
    mbr = r.mbr
    assert mbr.signature_ok and not mbr.gpt_protective
    p = mbr.first
    assert p.type == 0x0C and p.start_lba == 2048 and p.status == 0
    assert p.sectors == size // S - 2048
    assert p.chs_start == (0x20, 0x21, 0x00)

    f = r.boot.fat
    assert r.boot.kind == "fat32"
    assert f.bytes_per_sector == 512 and f.reserved_sectors == 32 and f.num_fats == 2
    assert f.total_sectors == p.sectors and f.hidden_sectors == 2048
    assert f.root_cluster == 2 and f.fsinfo_sector == 1 and f.backup_boot_sector == 6
    assert f.sectors_per_track == 63 and f.heads == 255 and f.drive_number == 0x80 and f.ext_boot_sig == 0x29
    assert f.serial == 0xDEADBEEF and f.label == "CHARMERA   " and f.fs_type == "FAT32   " and f.oem_name == "MSWIN4.1"
    assert f.media == 0xF8 and f.root_entries == 0
    assert f.cluster_count >= card.FAT32_MIN_CLUSTERS
    assert f.cluster_count == plan.cluster_count and f.fat_size == plan.fat_sectors
    assert f.sectors_per_cluster == plan.sectors_per_cluster
    if size_mib == 300:
        assert f.sectors_per_cluster == 8  # 4 KiB, the usual size for volumes up to 8 GiB
    else:
        assert f.sectors_per_cluster == 1  # shrunk so the volume still has 65525 clusters
    # The FAT really is big enough for every cluster.
    assert (f.cluster_count + 2) * 4 <= f.fat_size * 512

    assert r.fat_entries == [0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF]
    assert r.backup_matches is True
    assert r.fsinfo.signatures_ok
    assert (r.fsinfo.lead, r.fsinfo.struct_sig, r.fsinfo.trail) == (0x41615252, 0x61417272, 0xAA550000)
    assert r.fsinfo.free_count == f.cluster_count - 1 and r.fsinfo.next_free == 3

    base = 2048 * S
    with open(img, "rb") as fh:
        assert card.read_at(fh, base, S) == card.read_at(fh, base + 6 * S, S)
        assert card.read_at(fh, base + S, S) == card.read_at(fh, base + 7 * S, S)
        assert card.read_at(fh, base + 2 * S, S)[510:] == b"\x55\xAA"
        assert card.read_fat_entries(fh, base, f, 3, fat_index=1) == [0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF]
        # the rest of both FATs is zero
        for i in range(2):
            fat_off = base + (f.reserved_sectors + i * f.fat_size) * S
            assert card.read_at(fh, fat_off + 12, S - 12) == bytes(S - 12)
            assert card.read_at(fh, fat_off + S, (f.fat_size - 1) * S) == bytes((f.fat_size - 1) * S)
        root = card.read_at(fh, base + f.data_start_sector * S, f.sectors_per_cluster * S)
    assert root[0:11] == b"CHARMERA   " and root[11] == 0x08
    assert root[32:] == bytes(len(root) - 32)


@pytest.mark.parametrize("size_mib", [64, 300])
def test_fsck_fat_accepts_the_formatted_volume(tmp_path, size_mib):
    tool = fsck_fat()
    if tool is None:
        pytest.skip("fsck.fat not installed; the FAT32 invariants are covered by our own reader above")
    size = size_mib * MIB
    img = sparse_image(tmp_path / "card.img", size)
    with open(img, "r+b") as fh:
        card.format_fat32(fh, size)
    p = check(img).partition
    part = tmp_path / "partition.img"
    with open(img, "rb") as src, open(part, "wb") as dst:
        src.seek(p.start_lba * S)
        remaining = p.sectors * S
        while remaining:
            chunk = src.read(min(remaining, MIB))
            dst.write(chunk)
            remaining -= len(chunk)
    proc = subprocess.run([tool, "-n", str(part)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "differences" not in proc.stdout.lower()


def test_format_zeroes_old_exfat_structures(tmp_path):
    img = make_exfat_image(tmp_path / "card.img", 64 * MIB)
    assert check(img).boot.kind == "exfat"
    with open(img, "r+b") as fh:
        card.format_fat32(fh, 64 * MIB)
        assert card.read_at(fh, (2048 + 12) * S, S) == bytes(S)  # old backup boot sector gone
    r = check(img)
    assert r.boot.kind == "fat32" and r.exit_code == 0


def test_plan_cluster_sizes_for_real_cards():
    gib = 1024 * MIB
    for size, expected in ((8 * gib, 4096), (16 * gib, 8192), (32 * gib, 16384), (64 * gib, 32768), (128 * gib, 32768), (512 * gib, 32768)):
        plan = card.plan_fat32(size)
        assert plan.cluster_bytes == expected, size
        assert card.FAT32_MIN_CLUSTERS <= plan.cluster_count < card.FAT32_MAX_CLUSTERS
        assert (plan.cluster_count + 2) * 4 <= plan.fat_sectors * S
        assert plan.partition_sectors < 1 << 32
    assert card.plan_fat32(4 * gib, cluster_size=32768).cluster_bytes == 32768
    with pytest.raises(card.CardError):
        card.plan_fat32(2 * gib, cluster_size=32768)  # 65487 clusters: just under the FAT32 minimum


def test_plan_refusals():
    with pytest.raises(card.CardError):
        card.plan_fat32(513 * 1024 * MIB)  # over 512 GiB
    with pytest.raises(card.CardError):
        card.plan_fat32(16 * MIB)  # too small for 65525 clusters even at 512 bytes
    with pytest.raises(card.CardError):
        card.plan_fat32(64 * MIB, cluster_size=4096)  # explicit size that cannot give 65525 clusters
    with pytest.raises(card.CardError):
        card.plan_fat32(64 * MIB, cluster_size=768)
    with pytest.raises(card.CardError):
        card.plan_fat32(64 * MIB, label="a label that is too long")
    with pytest.raises(card.CardError):
        card.plan_fat32(64 * MIB, label="bad*name")
    assert card.parse_cluster_size("4K") == 4096
    assert card.parse_cluster_size("32KiB") == 32768
    assert card.parse_cluster_size("512") == 512
    with pytest.raises(card.CardError):
        card.parse_cluster_size("3000")


def test_fat32_geometry_matches_the_microsoft_estimate():
    # 300 MiB partition, 4 KiB clusters: the FATGEN formula and the verification agree.
    fat_sectors, clusters = card.fat32_geometry(300 * MIB // S - 2048, 8)
    assert clusters >= card.FAT32_MIN_CLUSTERS
    assert (clusters + 2) * 4 <= fat_sectors * S
    assert (clusters + 2) * 4 > (fat_sectors - 2) * S  # not wasteful either


# ---------------------------------------------------------------------------------------------
# Other layouts card-check must recognise


def test_gpt_protective_mbr_is_detected(tmp_path):
    img = sparse_image(tmp_path / "gpt.img", 64 * MIB)
    with open(img, "r+b") as fh:
        card.write_at(fh, 0, card.build_mbr(1, 64 * MIB // S - 1, ptype=0xEE))
        header = bytearray(S)
        header[0:8] = b"EFI PART"
        struct.pack_into("<QII", header, 72, 2, 128, 128)
        card.write_at(fh, S, bytes(header))
        entry = bytearray(128)
        entry[0:16] = __import__("uuid").UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7").bytes_le
        struct.pack_into("<QQ", entry, 32, 2048, 64 * MIB // S - 34)
        entry[56:56 + 10] = "Photo".encode("utf-16-le")
        card.write_at(fh, 2 * S, bytes(entry) + bytes(128 * 127))
    r = check(img)
    assert r.mbr.gpt_protective
    assert r.exit_code == 1
    assert any("GPT" in p for p in r.problems)
    assert r.gpt_entries[0].type_name == "Microsoft basic data" and r.gpt_entries[0].name == "Photo"
    assert r.fixes == ["--format-fat32"]


def test_superfloppy_and_blank_images(tmp_path):
    flat = sparse_image(tmp_path / "flat.img", 64 * MIB)
    with open(flat, "r+b") as fh:
        plan = card.plan_fat32(64 * MIB)
        card.write_at(fh, 0, card.build_fat32_boot_sector(plan))
    r = check(flat)
    assert r.superfloppy and r.exit_code == 1 and r.fixes == ["--format-fat32"]

    blank = sparse_image(tmp_path / "blank.img", 64 * MIB)
    r = check(blank)
    assert r.exit_code == 1 and r.fixes == ["--format-fat32"]
    assert any("no MBR signature" in p for p in r.problems)


def test_identify_boot_sector():
    assert card.identify_boot_sector(bytes(S)) == "unknown"
    assert card.identify_boot_sector(exfat_boot_region(32 * MIB, 0)[:S]) == "exfat"
    assert card.identify_boot_sector(card.build_fat32_boot_sector(card.plan_fat32(64 * MIB))) == "fat32"
    fat16 = bytearray(S)
    fat16[0:3] = b"\xEB\x3C\x90"
    struct.pack_into("<HBHBHHBHHHII", fat16, 11, 512, 4, 4, 2, 512, 65000, 0xF8, 64, 63, 255, 0, 0)
    fat16[54:62] = b"FAT16   "
    fat16[510:512] = b"\x55\xAA"
    assert card.identify_boot_sector(bytes(fat16)) == "fat16"


# ---------------------------------------------------------------------------------------------
# Command layer, on images


def run_cli(*argv):
    return cli.main(list(argv))


def test_cli_check_and_format_on_image(tmp_path, capsys):
    img = make_exfat_image(tmp_path / "card.img", 64 * MIB)
    assert run_cli("card-check", "--image", str(img)) == 1
    out = capsys.readouterr().out
    assert "VolumeDirty 1" in out and "Android  refuses" in out and "--clear-dirty" in out

    assert run_cli("card-fix", "--image", str(img), "--clear-dirty", "--yes") == 0
    out = capsys.readouterr().out
    assert "VolumeFlags 0x0002 -> 0x0000" in out
    assert run_cli("card-check", "--image", str(img)) == 0

    assert run_cli("card-fix", "--image", str(img), "--format-fat32", "--yes", "--label", "photos") == 0
    out = capsys.readouterr().out
    assert "type 0x0C" in out and "no problem found" in out
    r = check(img)
    assert r.boot.kind == "fat32" and r.boot.fat.label == "PHOTOS     " and r.partition.type == 0x0C

    with open(img, "r+b") as fh:
        card.set_partition_type(fh, 1, 0x07)
    assert run_cli("card-check", "--image", str(img)) == 1
    assert run_cli("card-fix", "--image", str(img), "--fix-mbr-type", "--yes") == 0
    assert check(img).partition.type == 0x0C


def test_cli_fix_refusals(tmp_path, capsys):
    img = make_exfat_image(tmp_path / "card.img", 64 * MIB)
    assert run_cli("card-fix", "--image", str(img)) == 2  # nothing to do
    assert run_cli("card-fix", "--image", str(img), "--format-fat32", "--clear-dirty", "--yes") == 2
    assert run_cli("card-fix", "--image", str(tmp_path / "missing.img"), "--clear-dirty", "--yes") == 2
    assert run_cli("card-fix", "--image", str(img), "--format-fat32", "--yes", "--cluster-size", "4096") == 2
    assert run_cli("card-fix", "--image", str(img), "--format-fat32", "--yes", "--label", "way too long label") == 2
    # Without --yes and without a terminal nothing is written.
    assert run_cli("card-fix", "--image", str(img), "--clear-dirty") == 2
    assert check(img).boot.exfat.dirty
    # A fix that does not apply to this filesystem writes nothing and reports the remaining problem.
    assert run_cli("card-fix", "--image", str(img), "--fix-mbr-type", "--yes") == 1
    assert check(img).partition.type == 0x07
    assert run_cli("card-check", str(img)) == 2  # a regular file given as a device
    assert run_cli("card-check") == 2
    capsys.readouterr()


def test_cli_refuses_oversize_target(tmp_path, monkeypatch):
    img = sparse_image(tmp_path / "huge.img", 64 * MIB)
    real = card.resolve_target

    def fake(target, image):
        t = real(target, image)
        t.size = 600 * 1024 * MIB
        return t

    monkeypatch.setattr(card, "resolve_target", fake)
    assert run_cli("card-fix", "--image", str(img), "--format-fat32", "--yes") == 2
    assert check(img).mbr.signature_ok is False  # untouched


# ---------------------------------------------------------------------------------------------
# Disk listing, parsed from fake data


def test_list_disks_linux_from_fake_sysfs(tmp_path):
    sys_root = tmp_path / "sys"
    dev_root = tmp_path / "dev"
    dev_root.mkdir()

    def disk(name, removable, size_sectors, model=None, mmc_type=None, parts=()):
        d = sys_root / "block" / name
        (d / "device").mkdir(parents=True)
        (d / "removable").write_text(f"{removable}\n")
        (d / "size").write_text(f"{size_sectors}\n")
        if model:
            (d / "device" / "model").write_text(model + "\n")
        if mmc_type:
            (d / "device" / "type").write_text(mmc_type + "\n")
            (d / "device" / "name").write_text("SD64G\n")
        for p in parts:
            (d / p).mkdir()

    disk("sdb", 1, 62333952, model="Ultra", parts=("sdb1",))
    disk("mmcblk0", 0, 124735488, mmc_type="SD", parts=("mmcblk0p1",))
    disk("mmcblk1", 0, 124735488, mmc_type="MMC")
    disk("nvme0n1", 0, 2000409264, model="Samsung SSD", parts=("nvme0n1p1",))
    disk("sdc", 1, 2 * 1024 * 1024 * 1024 * 1024 // 512, model="Big USB disk")
    disk("loop0", 0, 1000)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"{dev_root}/sdb1 /media/me/CHARMERA vfat rw 0 0\n{dev_root}/nvme0n1p1 / ext4 rw 0 0\nproc /proc proc rw 0 0\n")

    disks = {d.path: d for d in card.list_disks_linux(str(sys_root), str(mounts), str(dev_root))}
    assert set(os.path.basename(p) for p in disks) == {"sdb", "mmcblk0", "mmcblk1", "nvme0n1", "sdc"}
    sdb = disks[f"{dev_root}/sdb"]
    assert sdb.removable and sdb.size == 62333952 * 512 and sdb.model == "Ultra"
    assert sdb.mounted == [f"{dev_root}/sdb1 on /media/me/CHARMERA"]
    assert sdb.card_candidate == (True, "")
    mmc = disks[f"{dev_root}/mmcblk0"]
    assert mmc.card_candidate == (True, "") and mmc.bus == "SD card" and mmc.mounted == []
    assert disks[f"{dev_root}/mmcblk1"].card_candidate[0] is False
    nvme = disks[f"{dev_root}/nvme0n1"]
    assert nvme.card_candidate == (False, "not removable") and nvme.mounted
    assert disks[f"{dev_root}/sdc"].card_candidate == (False, "larger than 512 GiB")
    text = card.format_disk_list(list(disks.values()))
    assert "[card?]" in text and "not a card: larger than 512 GiB" in text and "not a card: not removable" in text


def test_windows_list_parsers():
    wmic = "\r\n\r\nNode,Index,InterfaceType,MediaType,Model,Size\r\nPC,0,SCSI,Fixed hard disk media,Samsung SSD 980,1000202273280\r\nPC,2,USB,Removable Media,Generic STORAGE DEVICE USB Device,31914983424\r\n"
    disks = card.parse_wmic_csv(wmic)
    assert [d.path for d in disks] == ["\\\\.\\PhysicalDrive0", "\\\\.\\PhysicalDrive2"]
    assert disks[0].card_candidate == (False, "not removable")
    assert disks[1].card_candidate == (True, "") and disks[1].size == 31914983424
    ps = '"Number","FriendlyName","Size","BusType"\r\n"0","Samsung SSD 980","1000202273280","NVMe"\r\n"1","SD Card","63864569856","SD"\r\n"2","Seagate Expansion","4000787030016","USB"\r\n'
    disks = card.parse_get_disk_csv(ps)
    assert [(d.path, d.card_candidate[0]) for d in disks] == [
        ("\\\\.\\PhysicalDrive0", False),
        ("\\\\.\\PhysicalDrive1", True),
        ("\\\\.\\PhysicalDrive2", False),
    ]
    assert card.parse_wmic_csv("") == [] and card.parse_get_disk_csv("garbage") == []


def test_linux_mounts_for_matches_disk_and_partitions(tmp_path):
    sys_root = tmp_path / "sys"
    (sys_root / "block" / "sdz" / "sdz1").mkdir(parents=True)
    dev = tmp_path / "dev"
    dev.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(f"{dev}/sdz1 /mnt/with\\040space vfat rw 0 0\n{dev}/sdy /mnt/other vfat rw 0 0\n")
    found = card.linux_mounts_for(f"{dev}/sdz", "sdz", str(mounts), str(sys_root))
    assert found == [(f"{dev}/sdz1", "/mnt/with space")]
    assert card.linux_mounts_for(f"{dev}/sdx", "sdx", str(mounts), str(sys_root)) == []


def test_windows_target_spellings_are_recognised():
    for text in ("\\\\.\\PhysicalDrive2", "\\\\?\\PhysicalDrive2", "PhysicalDrive2", "physicaldrive2"):
        m = card._WIN_PHYSICAL.match(text)
        assert m and m.group(1) == "2", text
    assert card._WIN_PHYSICAL.match("PhysicalDrive") is None
    for text in ("E:", "e:", "E", "E:\\"):
        m = card._WIN_LETTER.match(text)
        assert m and m.group(1).upper() == "E", text
    assert card._WIN_LETTER.match("EF:") is None and card._WIN_LETTER.match("/dev/sdb") is None
