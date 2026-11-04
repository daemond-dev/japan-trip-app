# Using the toolkit

All commands run as `python -m charmera_toolkit <command>` (or `charmera <command>` after `pip install -e .`).
Python 3.9 or newer, Windows or Linux, no extra packages.

## Read-only commands

```
charmera inspect my_backup.bin --settings --resources
charmera verify JH_5408202611041200_0F065D55.bin --stock my_backup.bin
charmera sounds list my_backup.bin
charmera sounds extract my_backup.bin --out sounds/
charmera card-check --list
charmera card-check /dev/sdX            # Linux
charmera card-check \\.\PhysicalDrive2  # Windows (run as administrator)
charmera exif-fix --source E:\ --out C:\Photos\charmera
```

`inspect` tells you what a file is: dump or update file, which build it carries, whether its software region is
byte-identical to the stock original-series firmware, what the resource pack and sound slots contain, which settings
record is current (and whether UI sounds are on), and whether it holds another owner's photos.

`verify` is the gate before a card. It checks the filename checksum, the stamp, the size rule, the magic, the build, and
reports anything that differs from stock. A FAIL line means do not use the file.

## Commands that write files on your computer

```
charmera pack --input my_backup.bin --out out/                       # prints the plan only
charmera pack --input my_backup.bin --out out/ --i-understand-this-file-flashes-a-camera
charmera sounds mute my_backup.bin --out muted.bin --method settings  # prints the plan only
charmera sounds mute my_backup.bin --out muted.bin --method both --i-understand-this-is-a-mod
charmera sounds replace my_backup.bin --out custom.bin --slot IMG.WAV --wav shutter.wav --i-understand-this-is-a-mod
```

The gate flags exist so that nothing flashable appears by accident. A modified image goes through `pack` and then `verify`
before it is allowed near a card.

## Commands that write to hardware

`card-fix` and `restore` print what they are about to do and wait for you to type the target back. See docs/CARDS.md and
docs/RECOVERY.md.
