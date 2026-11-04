# The card: why phones reject it, and how to fix it

`card-check` and `card-fix` deal with the microSD card, not with the camera's firmware.

## The problem

The Charmera formats its card as exFAT, in one MBR partition of type 0x07, and it never clears the exFAT
VolumeDirty flag (bit 1 of VolumeFlags, bytes 106..107 of the boot sector). Every write session leaves the flag
set. Mac, Linux, Windows and iPhone mount the card anyway. Android does not: it offers to format the card
instead. Formatting it on the phone does not help for long, because the camera writes to the card again
on the next shot and the flag is back.

The fix that holds is FAT32. Two details matter:

- The MBR partition type must be 0x0C (FAT32 LBA). `mkfs.fat` on Linux leaves the old 0x07 in the partition
  table, and iPhones refuse a FAT32 card whose type byte says 0x07. The SD Association formatter and Windows
  write 0x0C.
- FAT32 cannot hold a file larger than 4 GiB. Photos and the camera's short clips are far below that.

Tested by the community that worked this out (sparkyfen's Charmera notes, the source for the findings above): with FAT32 and type 0x0C the same card mounts on Android,
iPhone, Mac and Linux. If the camera ever reformats the card back to exFAT, `card-check` will show it, and
`card-fix --format-fat32` puts FAT32 back.

Clearing the dirty bit (`card-fix --clear-dirty`) is the quick alternative when you want to keep exFAT, for
instance for files over 4 GiB. It works until the next time the camera writes to the card; then it has to be
done again before the card goes into an Android phone.

## Find the right disk first

Both commands take the whole disk, not a partition. Everything `card-fix` does is confirmed by typing the device
path (or `FORMAT`) back, but typing the wrong disk's path back would still destroy that disk. Check twice.

Linux, including a Steam Deck in desktop mode (Konsole):

```
lsblk -o NAME,SIZE,RM,TYPE,MODEL,MOUNTPOINTS
python -m charmera_toolkit card-check --list
```

The card is the removable disk of the right size: `/dev/sdX` through a USB reader, `/dev/mmcblk0` in the Deck's
own slot (its partitions are `mmcblk0p1`...). Unmount before fixing: `udisksctl unmount -b /dev/mmcblk0p1` or
`sudo umount /dev/sdX1`. Reading and writing a block device needs `sudo`; on the Deck set a password with
`passwd` once if you have not.

Windows:

- Disk Management (Win+X, Disk Management): the card is "Disk N" with "Removable" in the left column.
- PowerShell: `Get-Disk` lists Number, FriendlyName, Size and BusType (USB or SD for a card).
- `python -m charmera_toolkit card-check --list` prints the same with the drive letters that sit on each disk.

Use `\\.\PhysicalDriveN` with that number, or just the card's drive letter (`E:`); the tool resolves the letter to
its physical drive. Both need an administrator prompt (right-click PowerShell or Terminal, "Run as administrator").

## Commands

```
python -m charmera_toolkit card-check --list              # candidate disks; nothing is opened for writing
python -m charmera_toolkit card-check /dev/sdX            # or \\.\PhysicalDrive2, or E:, or --image card.img
```

`card-check` reads the MBR and the first partition's boot sector and prints: the partitions (type, start, size), a
GPT if there is one, the filesystem (exFAT, FAT32, FAT16/12 or unknown), for exFAT the VolumeFlags with the dirty and
media-failure bits and the boot-region checksum, for FAT32 the BPB (sectors per cluster, reserved sectors, FATs,
total sectors, FAT size, root cluster, geometry, drive number), the backup boot sector, FSInfo and the first FAT
entries. Then a verdict per platform and the fix command. Exit code 0 means no problem, 1 means a problem was found,
2 means it could not look. It never writes.

```
python -m charmera_toolkit card-fix /dev/sdX --clear-dirty       # exFAT: clear VolumeDirty, main and backup boot sector
python -m charmera_toolkit card-fix /dev/sdX --fix-mbr-type      # FAT32 behind a 0x07 entry: set the type byte to 0x0C
python -m charmera_toolkit card-fix /dev/sdX --format-fat32      # erase: new MBR (one 0x0C partition at 1 MiB) and FAT32
        [--label CHARMERA] [--cluster-size 32K]
```

What `--format-fat32` writes: the first 4 MiB zeroed (so nothing of the old exFAT boot region survives), an MBR with
one partition of type 0x0C starting at sector 2048, and a FAT32 volume with 512-byte sectors, 32 reserved sectors,
two FATs, FSInfo at sector 1, backup boot sector at 6, root directory in cluster 2, the usual 63/255 geometry, drive
number 0x80, OEM name MSWIN4.1, a random serial, the label in the boot sector and as a root-directory entry. Cluster
size follows the usual table (4 KiB up to 8 GiB, 8 KiB to 16 GiB, 16 KiB to 32 GiB, 32 KiB above) unless
`--cluster-size` says otherwise; it is reduced automatically for very small volumes so the cluster count stays at or
above 65525, which is what makes a volume FAT32.

`--clear-dirty` changes two bytes in the main boot sector and the same two in the backup boot sector (sector 12 of the
volume). The boot-region checksum in sector 11 excludes exactly those bytes, so it stays valid and is not touched.

Before writing, `card-fix` prints the device, its size and every operation, then asks you to type the device path
back (or `FORMAT` for a reformat). `--yes --i-know-what-i-am-doing` skips the question for scripts; on an image
file `--yes` alone is enough. It refuses a disk larger than 512 GiB, a mounted card (Linux: anything in
/proc/mounts for the disk or its partitions), and on Windows it locks and dismounts the card's volumes first and
stops if that fails (close Explorer windows showing the card). Writes go through O_SYNC and fsync, and the result
is checked and printed afterwards.

`--image FILE` runs any of this on a disk image instead of a device; the tests use that.

## When exFAT is what you want

Keep exFAT only if you need files over 4 GiB. Then, after every camera session and before the card goes into an
Android phone: unmount, optionally `fsck.exfat -y /dev/sdX1`, then `card-fix /dev/sdX --clear-dirty`.
