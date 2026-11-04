# exif-fix: the Charmera's broken EXIF and the wrong video date

`charmera exif-fix` copies the photos and videos off a Charmera card and repairs the copies. The card is
only ever read.

## The problem

The Generalplus firmware writes an EXIF block that standard tools reject, and a wrong date into every
video. These defects were documented and fixed by two community projects, jphastings/charmera (Go) and
RickCaleg/charmera-importer (C#); `charmera_toolkit/exif.py` is a port of their logic.

In every JPEG:

- `DateTime`, `DateTimeOriginal` and `DateTimeDigitized` are written as `2026:03:03:12:16:29`, a colon
  where EXIF wants a space. Readers reject the value, so the capture time is lost and photo libraries
  sort the pictures by import date.
- `PixelXDimension`/`PixelYDimension` say 640x480 while the frame is 1440x1080.
- The MakerNote offset points at the end of the EXIF block, i.e. at data that is not there. exiftool
  reports `Bad ExifIFD offset for MakerNoteUnknown` and refuses to write the file; stricter parsers drop
  the whole block.
- `Make`/`Model` are `Generalplus`/`CBB3`, the chip rather than the camera, so libraries group the
  photos under "Generalplus CBB3".
- Some files carry no usable date at all.

In every AVI (Motion-JPEG video plus PCM audio in a RIFF container):

- The recording date inside the file is always 2010-06-29, stored as text in the standard AVI date
  chunks: `IDIT` in the header list (`hdrl`, C `asctime` form, `Tue Jun 29 12:00:00 2010`) and `ICRD` in
  the `INFO` list (ISO form, `2010-06-29`). ffmpeg and ffprobe report it as `creation_time`, so every
  Charmera video sorts into 2010.
- The real recording time is the file's modification time, which the camera writes correctly to the
  card.

## The fix

Photos. The old EXIF block is never edited in place, because its offsets cannot be trusted. Instead:

1. The JPEG's marker segments are walked up to the scan data to find the APP1 Exif segment (if any) and
   the frame header (SOF), which gives the true dimensions.
2. A bounds-checked reader salvages what is usable from the old block, little or big endian:
   `Orientation`, `Make`, `Model` and the three date tags. The MakerNote is ignored.
3. The best date is chosen: `DateTimeOriginal`, then `DateTimeDigitized`, then `DateTime`, each
   normalised from the extra-colon form. Empty and zeroed dates are rejected. With no usable EXIF date
   the file's modification time is used.
4. A fresh little-endian EXIF is written: IFD0 with `Make`, `Model`, `Orientation`, `DateTime`,
   `YCbCrPositioning` and the Exif IFD pointer; Exif IFD with `FNumber` 2.4, `ExifVersion` 0230,
   `DateTimeOriginal`, `DateTimeDigitized`, `ComponentsConfiguration`, `FlashpixVersion`, `ColorSpace`
   sRGB, `PixelXDimension`/`PixelYDimension` from the frame, `FocalLengthIn35mmFilm` 35, `LensMake` and
   `LensModel` (the published spec: fixed 35 mm-equivalent f/2.4; the real focal length is not published
   and is not invented).
5. The new segment replaces the old APP1, or goes right after the JFIF APP0 when there was none. Every
   other byte of the file, including the compressed image data, is copied unchanged.

`Make`/`Model` become `Kodak`/`Charmera`; `--keep-make` keeps the camera's `Generalplus`/`CBB3`.

Videos. The default, `--video fix-date`, copies the AVI and rewrites the date chunks in the copy, in
place: same text form (asctime, EXIF or ISO, trailing newline kept), same byte length, NUL-padded, so
no other byte of the file moves and the RIFF structure stays valid. Only a chunk holding exactly
2010-06-29 is rewritten; a genuine date is left alone and reported. A chunk too small for the new text
(which cannot happen with the forms above, since they are fixed width) is left alone and reported rather
than truncated into something unreadable.

`--video transcode` runs ffmpeg instead: `-c:v libx264 -crf 26 -preset medium -c:a aac -b:a 128k
-ar 44100 -metadata creation_time=<mtime>` into an MP4, the same settings the Go tool uses. Without
ffmpeg on PATH the command says so once and falls back to fix-date. `--video copy` copies the AVI
unchanged.

Output files keep the source's modification time, so the real recording time survives even in tools
that only look at file dates.

## The command

```
charmera exif-fix [--source DIR] --out DIR [--video {fix-date,transcode,copy}]
                  [--keep-make] [--keep-names] [--dry-run] [--verbose]
```

- `--source DIR`: the card root (its `DCIM` is read, recursively) or any folder of Charmera files. When
  omitted, mounted volumes are checked by content: `/media/*/*`, `/run/media/*/*` and `/mnt/*` on Linux,
  drive letters D: to Z: on Windows. A volume is a Charmera when it has a `DCIM` directory plus either a
  `SPIDCIM` sibling directory or a JPEG whose first 4 KiB contain the Generalplus `GPEncoder` comment.
  Volume labels are never trusted. With no match, or more than one, the command stops and asks for
  `--source`.
- `--out DIR`: created if needed. Outputs are named `IMG_YYYYMMDD_HHMMSS_<hash>.jpg` and
  `VID_YYYYMMDD_HHMMSS_<hash>.avi` (`.mp4` when transcoded), the time being the source's modification
  time and the hash the first 16 hex digits of the SHA-256 of the source file. On a second run every
  source whose hash token already exists in the output directory is skipped, so the output directory
  is its own import record. `--keep-names` keeps the source names instead and skips outputs that
  already exist.
- Files are `.jpg`, `.jpeg` and `.avi`, any case. Names starting with `._` (macOS metadata) are
  skipped.
- `--dry-run` prints the per-file report and writes nothing, not even the output directory.
- `--verbose` adds what the camera wrote: the old Make/Model, orientation and raw dates, and the AVI
  date chunks with their offsets.
- Each file gets one line: the output name, the dimensions, the orientation, the date and where it came
  from (`exif`, with `extra colon repaired` when that happened, or `mtime`), and the Make/Model written.
  For videos: which chunks were rewritten, or why none were. A final line gives the counts.
- Exit code 0 when every file was handled, 1 when any file failed (the others are still processed) or
  no source could be found.

Example:

```
$ charmera exif-fix --source /media/me/CHARMERA --out ~/Pictures/charmera
PICT0001.JPG -> IMG_20260303_121629_4f1c9a0b7e2d3c55.jpg: 1440x1080, orientation 1, date 2026:03:03 12:16:29 from exif, extra colon repaired, make/model Kodak/Charmera (was Generalplus/CBB3)
PICT0002.AVI -> VID_20260303_121702_a90b3e77c1d2f004.avi: 1440x1080, date 2026:03:03 12:17:02 from mtime replaces 2010-06-29 in IDIT, ICRD
Done: 1 photos fixed, 1 videos date-fixed, 0 skipped (already in /home/me/Pictures/charmera), 0 failed
```

## Verifying

`python -m pytest tests/test_exif.py` rebuilds EXIF on generated JPEGs and checks it with the module's
own reader and, when Pillow is installed, with Pillow's independent one (dimensions, dates, Make/Model,
FNumber, lens tags, identical decoded pixels). When ffmpeg is on PATH it also generates a real
MJPEG/PCM AVI, injects the camera's date chunks the way the C# importer lays them out, and confirms with
ffprobe that `creation_time` changes from 2010-06-29 to the file time while the file still decodes.

## Limitations

- The AVI chunk layout (IDIT in `hdrl` as asctime, ICRD in `INFO` as an ISO date) is what the C#
  importer's code and tests expect of real Charmera files; it was not re-verified here against a video
  from an actual camera. The rewrite handles every form that code accepts, and leaves anything else
  untouched, so an unexpected layout results in an unchanged copy and a line saying so.
- `creation_time` written by `--video transcode` has no time zone, like the camera's clock; players that
  assume UTC may show it shifted.
- Output names use the modification time, which on a Charmera is the capture time. For files copied off
  the card by other means first, the EXIF date inside the photo may differ from the name.
