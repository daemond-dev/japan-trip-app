"""probe / backup / restore: full-flash backup and restore of the Charmera through flashrom.

The camera's firmware lives in a Puya PY25D16HB, a 16 Mbit (2 MiB) SPI flash in an SOP8 package with
visible legs (REPORTED by the teardown; a second owner confirmed a clip read). flashrom does not know the
part by name; community reads used a CH341A with `-c "SFDP-capable chip"` (REPORTED). This module is a
careful layer over flashrom and never touches USB itself.

Safety rules:

- `probe` and `backup` only run flashrom without an operation or with `-r`. They cannot write.
- `restore` refuses an image that is not exactly 2 MiB, does not start with the "SPII" magic, or lacks
  the original-series build stamps (unless --allow-unknown-build). It prints the SHA-256, a 64 KiB
  block diff against the owner's backup when one is given, and the exact plan, and only runs
  `flashrom -w` after the owner types RESTORE. Afterwards it reads the chip back and compares.
- Electrical warning that software cannot enforce: the "H" in PY25D16HB means a 2.3 to 3.6 V part
  (VERIFIED from Puya's part-number coding). Program it at 3.3 V, never through a 1.8 V adapter, and not
  with an unmodified black CH341A board, which drives its data lines at 5 V. Disconnect the camera's
  battery first. See docs/RECOVERY.md before clipping anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import __version__
from .checksum import BLOCK_SIZE, FLASH_SIZE

MAGIC = b"SPII"

# Build stamp strings of the original 2025 series at their file offsets (VERIFIED in both public dumps).
BUILD_STAMPS = (
    (0x18964, b"JH5408  20250520"),
    (0x218C4, b"202505201029"),
)

# 0x1E0000..0x200000 is 0xFF in every known dump.
TAIL_START = 0x1E0000
BLOCK_COUNT = FLASH_SIZE // BLOCK_SIZE  # 32 blocks of 64 KiB

# SHA-256 of the two public stock dumps. They are identical in the code and resource area and differ only
# in the settings store (0x170000) and two internally stored photos (0x180000, 0x1A5800) that belong to
# those owners. The owner's own backup is always the preferred restore source.
PUBLIC_DUMPS: Dict[str, str] = {
    "cc3b4b2fd7eb71ba5a441decdc86984261fac8418ee3e105d7e2b3c38b7ea42f": "benbadio/keychain-camera-plus",
    "720ed0b65c91b149b0916498732b0ecf40227736ef5e3c126c487a7b5253436c": "Khimira/Project-Sirius",
}

# Coarse map of the image, used to label the block diff. Boundaries come from the settings module
# (0x170000), the photo offsets in the public dumps and the blank tail.
REGIONS = (
    (0x000000, 0x170000, "code and resources"),
    (0x170000, 0x180000, "settings store"),
    (0x180000, 0x1E0000, "internally stored photos"),
    (0x1E0000, 0x200000, "tail, 0xFF in every known dump"),
)

DEFAULT_PROGRAMMER = "ch341a_spi"
DEFAULT_CHIP = "SFDP-capable chip"
CHIP_AUTO = "auto"

# Seconds. A CH341A reads 2 MiB in well under two minutes and writes plus verifies in a few minutes;
# these are generous so a slow programmer (serprog on a Pico) is not cut off.
VERSION_TIMEOUT = 30.0
PROBE_TIMEOUT = 120.0
READ_TIMEOUT = 900.0
WRITE_TIMEOUT = 1800.0


class FlashromError(Exception):
    """flashrom could not be found or could not be run. The message is written for the owner."""


@dataclass
class FlashromResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# ----------------------------------------------------------------------------------------------------
# Locating and running flashrom
# ----------------------------------------------------------------------------------------------------


def install_hints() -> str:
    """Per-OS pointers for getting flashrom and access to the CH341A. No invented download links."""
    system = platform.system()
    lines = []
    if system == "Windows":
        lines += [
            "Windows: flashrom.org lists Windows builds and the libusb requirement; a build made under",
            "  MSYS2 also works. Install the WinUSB (or libusb-win32) driver for the CH341A (USB ID",
            "  1a86:5512) with Zadig (zadig.akeo.ie) before running flashrom, or it will not see the",
            "  programmer. Pass --flashrom C:\\path\\to\\flashrom.exe if it is not on PATH.",
        ]
    elif system == "Darwin":
        lines += ["macOS: `brew install flashrom`."]
    else:
        lines += [
            "Linux: `sudo apt install flashrom` (Debian, Ubuntu), `sudo dnf install flashrom` (Fedora),",
            "  `sudo pacman -S flashrom` (Arch), or a build from flashrom.org.",
            "  The CH341A (USB ID 1a86:5512) needs udev access: install scripts/99-ch341a.rules",
            "  (see the comment at its top) or run the command with sudo.",
        ]
    lines.append("Point --flashrom at the executable if it is installed somewhere not on PATH.")
    return "\n".join(lines)


def find_flashrom(explicit: Optional[str] = None) -> str:
    """Resolve the flashrom executable or raise FlashromError with install hints."""
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(explicit)
        if found:
            return found
        raise FlashromError(f"--flashrom {explicit!r} is not a file and is not on PATH.\n" + install_hints())
    found = shutil.which("flashrom")
    if found:
        return found
    raise FlashromError("flashrom was not found on PATH.\n" + install_hints())


def run_flashrom(flashrom: str, args: Sequence[str], timeout: float, echo: bool = True) -> FlashromResult:
    """Run flashrom with captured output and a timeout. Raises FlashromError if it cannot run or hangs.

    A non-zero exit is returned, not raised, so callers can show flashrom's own message.
    """
    argv = [flashrom] + list(args)
    if echo:
        print("Running: " + shlex.join(argv))
        sys.stdout.flush()
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FlashromError(
            f"flashrom did not finish within {timeout:.0f} s and was stopped: {shlex.join(argv)}\n"
            "Check the clip contact, the programmer connection and that the camera battery is "
            "disconnected, then try again."
        ) from exc
    except OSError as exc:
        raise FlashromError(f"could not run {flashrom}: {exc}") from exc
    return FlashromResult(argv, proc.returncode, proc.stdout or "", proc.stderr or "")


def flashrom_version(flashrom: str) -> str:
    """First line of `flashrom --version` that names flashrom and a number."""
    res = run_flashrom(flashrom, ["--version"], VERSION_TIMEOUT, echo=False)
    for line in res.output.splitlines():
        if "flashrom" in line.lower() and re.search(r"\d", line):
            return line.strip()
    if not res.ok:
        raise FlashromError(f"{flashrom} --version failed (exit {res.returncode}):\n{res.output.strip()}")
    first = res.output.strip().splitlines()
    return first[0].strip() if first else "unknown"


def chip_args(chip: Optional[str]) -> List[str]:
    """`-c NAME`, or nothing when the chip is "auto" (let flashrom probe its whole table)."""
    if chip is None or chip.strip() == "" or chip.strip().lower() == CHIP_AUTO:
        return []
    return ["-c", chip]


def read_chip(flashrom: str, programmer: str, chip: Optional[str], out_path: Path) -> FlashromResult:
    return run_flashrom(flashrom, ["-p", programmer] + chip_args(chip) + ["-r", str(out_path)], READ_TIMEOUT)


def write_chip(flashrom: str, programmer: str, chip: Optional[str], image_path: Path) -> FlashromResult:
    """flashrom -w: flashrom itself erases, writes and verifies. Only restore calls this."""
    return run_flashrom(flashrom, ["-p", programmer] + chip_args(chip) + ["-w", str(image_path)], WRITE_TIMEOUT)


# ----------------------------------------------------------------------------------------------------
# Parsing flashrom's probe output
# ----------------------------------------------------------------------------------------------------


@dataclass
class ProbeResult:
    found: List[Dict[str, str]] = field(default_factory=list)
    no_device: bool = False
    multiple: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.found) and not self.multiple and not self.no_device


_FOUND_RE = re.compile(
    r'Found (?P<vendor>.+?) flash chip "(?P<name>[^"]+)" \((?P<kb>\d+) kB, (?P<bus>[^)]+)\)'
    r"(?: on (?P<programmer>[^.\s]+))?"
)
_MULTI_RE = re.compile(r"Multiple flash chip definitions match the detected chip\(s\):(?P<names>.*)")


def parse_probe_output(text: str) -> ProbeResult:
    """Pick the lines that matter out of a flashrom run without an operation."""
    result = ProbeResult()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _FOUND_RE.search(line)
        if m:
            result.found.append(
                {
                    "vendor": m.group("vendor"),
                    "name": m.group("name"),
                    "size_kb": m.group("kb"),
                    "bus": m.group("bus"),
                    "programmer": m.group("programmer") or "",
                }
            )
            continue
        if line.startswith("No EEPROM/flash device found"):
            result.no_device = True
            continue
        m = _MULTI_RE.search(line)
        if m:
            result.multiple = re.findall(r'"([^"]+)"', m.group("names"))
            continue
        low = line.lower()
        if "sfdp" in low or "untested" in low or "please specify which chip" in low:
            result.notes.append(line)
            continue
        if (
            low.startswith("error")
            or "failed" in low
            or "libusb_error" in low
            or "could not" in low
            or "couldn't" in low
            or "permission" in low
            or "not found" in low
        ):
            result.problems.append(line)
    return result


# ----------------------------------------------------------------------------------------------------
# Image checks
# ----------------------------------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def region_name(offset: int) -> str:
    for start, end, name in REGIONS:
        if start <= offset < end:
            return name
    return "beyond 2 MiB"


def build_stamps_found(data: bytes) -> Dict[str, Optional[str]]:
    """The expected stamp string at each known offset, or None when it is not there."""
    out: Dict[str, Optional[str]] = {}
    for offset, expected in BUILD_STAMPS:
        chunk = data[offset : offset + len(expected)]
        out[f"0x{offset:X}"] = expected.decode("ascii") if chunk == expected else None
    return out


def analyze_image(data: bytes) -> dict:
    """Everything the manifest and the restore gate want to know about an image."""
    sha = sha256_hex(data)
    stamps = build_stamps_found(data)
    tail = data[TAIL_START:]
    return {
        "size": len(data),
        "size_ok": len(data) == FLASH_SIZE,
        "sha256": sha,
        "magic_ok": data[: len(MAGIC)] == MAGIC,
        "build_stamps": stamps,
        "build_stamps_ok": all(v is not None for v in stamps.values()),
        "tail_blank": len(data) == FLASH_SIZE and tail == b"\xff" * len(tail),
        "public_dump": PUBLIC_DUMPS.get(sha),
    }


def image_warnings(info: dict) -> List[str]:
    out = []
    if not info["size_ok"]:
        out.append(f"size is {info['size']:,} bytes, expected {FLASH_SIZE:,} (2 MiB)")
    if not info["magic_ok"]:
        out.append('the first four bytes are not the "SPII" magic')
    missing = [k for k, v in info["build_stamps"].items() if v is None]
    if missing:
        out.append("original-series build stamp missing at " + ", ".join(missing))
    if info["size_ok"] and not info["tail_blank"]:
        out.append(f"the tail 0x{TAIL_START:X}..0x{FLASH_SIZE:X} is not all 0xFF (it is in every known dump)")
    return out


@dataclass
class BlockDiff:
    index: int
    start: int
    end: int
    differing_bytes: int
    region: str


def block_diff(image: bytes, backup: bytes) -> List[BlockDiff]:
    """64 KiB blocks where the image and the backup differ. A length difference counts as differing."""
    total = max(len(image), len(backup))
    out = []
    for i in range((total + BLOCK_SIZE - 1) // BLOCK_SIZE):
        start = i * BLOCK_SIZE
        end = min(start + BLOCK_SIZE, total)
        a = image[start:end]
        b = backup[start:end]
        if a == b:
            continue
        differing = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
        out.append(BlockDiff(i, start, end, differing, region_name(start)))
    return out


def print_block_diff(image: bytes, backup: bytes) -> List[BlockDiff]:
    diffs = block_diff(image, backup)
    blocks_total = (max(len(image), len(backup)) + BLOCK_SIZE - 1) // BLOCK_SIZE
    if not diffs:
        print(f"Block diff: the image is identical to the backup in all {blocks_total} blocks of 64 KiB.")
        return diffs
    print(f"Block diff (64 KiB blocks), {len(diffs)} of {blocks_total} differ:")
    for d in diffs:
        print(
            f"  block {d.index:2d}  0x{d.start:06X}-0x{d.end - 1:06X}  {d.differing_bytes:7,d} bytes differ"
            f"  ({d.region})"
        )
    print(f"  {blocks_total - len(diffs)} blocks are identical and will not change.")
    return diffs


# ----------------------------------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------------------------------


def _fail(message: str) -> int:
    print("error: " + message, file=sys.stderr)
    return 1


def _print_indented(text: str, prefix: str = "  | ") -> None:
    for line in text.rstrip().splitlines():
        print(prefix + line)


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip())
    return cleaned.strip("._") or "charmera"


def _ask(prompt: str) -> str:
    """Wrapper around input() so a missing stdin counts as a refusal."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def cmd_probe(args: argparse.Namespace) -> int:
    try:
        flashrom = find_flashrom(args.flashrom)
        print(f"flashrom: {flashrom}")
        print(f"version:  {flashrom_version(flashrom)}")
        res = run_flashrom(flashrom, ["-p", args.programmer] + chip_args(args.chip), PROBE_TIMEOUT)
    except FlashromError as exc:
        return _fail(str(exc))

    print("flashrom output:")
    _print_indented(res.output or "(no output)")
    parsed = parse_probe_output(res.output)

    if parsed.found:
        for chip in parsed.found:
            where = f" on {chip['programmer']}" if chip["programmer"] else ""
            print(f'Found: {chip["vendor"]} "{chip["name"]}", {chip["size_kb"]} kB, {chip["bus"]}{where}')
            if chip["size_kb"] != str(FLASH_SIZE // 1024):
                print(
                    f"  warning: expected {FLASH_SIZE // 1024} kB (PY25D16HB). A different size usually "
                    "means a bad clip contact, a back-powered board or the wrong chip definition."
                )
    for note in parsed.notes:
        print(f"note: {note}")
    for problem in parsed.problems:
        print(f"problem: {problem}")

    if parsed.multiple:
        print("flashrom matched several chip definitions: " + ", ".join(f'"{n}"' for n in parsed.multiple))
        print("Pick one with --chip NAME. For this camera the community used --chip \"SFDP-capable chip\".")
        return 1
    if parsed.no_device or not parsed.found:
        if parsed.no_device:
            print("No flash chip was detected.")
        else:
            print("flashrom did not report a chip.")
        print(
            "Usual causes: the clip is not seated or is rotated (pin 1 dot), the camera battery is still\n"
            "connected, the programmer is not at 3.3 V, or flashrom cannot open the USB device (udev rule\n"
            "or sudo on Linux, Zadig driver on Windows). If you ran with --chip auto, retry with the\n"
            'default --chip "SFDP-capable chip": the Puya part is not in flashrom\'s table by name.'
        )
        return 1
    if not res.ok:
        print(f"flashrom exited with status {res.returncode}; treat the result above with care.")
        return 1
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fail(f"cannot create {out_dir}: {exc}")
    if args.reads < 1:
        return _fail("--reads must be at least 1")

    label = _safe_label(args.label)
    started = datetime.now(timezone.utc)
    base = f"{label}_{started.strftime('%Y%m%d-%H%M%SZ')}"

    reads: List[Path] = []
    try:
        flashrom = find_flashrom(args.flashrom)
        version = flashrom_version(flashrom)
        print(f"flashrom: {flashrom}")
        print(f"version:  {version}")
        print(f"Reading the chip {args.reads} time(s) through {args.programmer}, chip {args.chip!r}.")
        for i in range(1, args.reads + 1):
            path = out_dir / f"{base}.read{i}.bin"
            res = read_chip(flashrom, args.programmer, args.chip, path)
            if not res.ok:
                print("flashrom output:")
                _print_indented(res.output or "(no output)")
                return _fail(
                    f"flashrom read {i} failed (exit {res.returncode}). Nothing was written to the chip.\n"
                    "Check the clip contact and orientation, that the battery is disconnected, that the\n"
                    "programmer runs at 3.3 V, and that flashrom can open the USB device. Run `probe` first."
                )
            if not path.is_file():
                return _fail(f"flashrom exited 0 but {path} does not exist")
            data = path.read_bytes()
            print(f"read {i}: {len(data):,} bytes, sha256 {sha256_hex(data)}")
            reads.append(path)
    except FlashromError as exc:
        return _fail(str(exc))

    first = reads[0].read_bytes()
    mismatched = False
    for path in reads[1:]:
        other = path.read_bytes()
        if other == first:
            continue
        mismatched = True
        first_diff = next((k for k, (x, y) in enumerate(zip(first, other)) if x != y), min(len(first), len(other)))
        differing = sum(1 for x, y in zip(first, other) if x != y) + abs(len(first) - len(other))
        print(f"{path.name} differs from {reads[0].name}: first difference at 0x{first_diff:X}, {differing:,} bytes")
    if mismatched:
        print("The reads are kept for diagnosis:")
        for path in reads:
            print(f"  {path}")
        return _fail(
            "the reads are not identical, so no backup was written. Unstable reads usually mean a poor\n"
            "clip contact or a back-powered board (battery still connected). Reseat the clip, disconnect\n"
            "the battery and run backup again; compare the kept reads to see which bytes move."
        )

    final = out_dir / f"{base}.bin"
    reads[0].replace(final)
    for path in reads[1:]:
        path.unlink()

    info = analyze_image(first)
    warnings = image_warnings(info)
    for w in warnings:
        print(f"warning: {w}")
    if warnings and (not info["magic_ok"] or not info["size_ok"]):
        print("The file is kept anyway: a blank or corrupt chip still deserves a backup of what is on it.")
    sha = info["sha256"]
    sha_path = out_dir / f"{base}.sha256"
    sha_path.write_text(f"{sha}  {final.name}\n", encoding="utf-8")

    manifest = {
        "toolkit_version": __version__,
        "timestamp_utc": started.replace(microsecond=0).isoformat(),
        "label": label,
        "file": final.name,
        "sha256_file": sha_path.name,
        "flashrom_path": flashrom,
        "flashrom_version": version,
        "programmer": args.programmer,
        "chip": args.chip if chip_args(args.chip) else CHIP_AUTO,
        "reads": args.reads,
        "reads_identical": True,
        "size": info["size"],
        "size_ok": info["size_ok"],
        "sha256": sha,
        "magic_ok": info["magic_ok"],
        "build_stamps": info["build_stamps"],
        "build_stamps_ok": info["build_stamps_ok"],
        "tail_blank": info["tail_blank"],
        "public_dump": info["public_dump"],
        "warnings": warnings,
    }
    manifest_path = out_dir / f"{base}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"backup:   {final}")
    print(f"sha256:   {sha}")
    print(f"checksum: {sha_path}")
    print(f"manifest: {manifest_path}")
    if info["public_dump"]:
        print(f"note: this image is byte-identical to the public dump from {info['public_dump']}.")
    print("Keep the three files together, and keep a second copy somewhere else.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    image_path = Path(args.image)
    try:
        image = image_path.read_bytes()
    except OSError as exc:
        return _fail(f"cannot read {image_path}: {exc}")

    info = analyze_image(image)
    refusals: List[str] = []
    if not info["size_ok"]:
        refusals.append(f"{image_path.name} is {info['size']:,} bytes; a full-flash image is exactly {FLASH_SIZE:,}")
    if not info["magic_ok"]:
        refusals.append(f'{image_path.name} does not start with the "SPII" magic')
    if not info["build_stamps_ok"]:
        missing = ", ".join(k for k, v in info["build_stamps"].items() if v is None)
        text = f"the original-series build stamps are missing at {missing}"
        if args.allow_unknown_build:
            print(f"warning: {text}; continuing because --allow-unknown-build was given.")
            print("Be sure this image was built for this camera's edition and series.")
        else:
            refusals.append(
                text + ". Flashing an image from another edition or series boots but freezes capture\n"
                "(REPORTED). Pass --allow-unknown-build only if you know this image is for this camera."
            )
    if refusals:
        for r in refusals:
            print(f"refusing: {r}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1

    print(f"image:    {image_path}")
    print(f"size:     {info['size']:,} bytes")
    print(f"sha256:   {info['sha256']}")
    print("magic:    SPII")
    for offset, value in info["build_stamps"].items():
        print(f"stamp:    {offset} {value if value else '(missing)'}")
    print(f"tail:     {'all 0xFF' if info['tail_blank'] else 'NOT all 0xFF (every known dump is)'}")
    if info["public_dump"]:
        print(
            f"note:     this is the public dump from {info['public_dump']}. Its settings store and two\n"
            "          internal photos belong to that owner. Your own backup is the preferred source;\n"
            "          use a public dump only as a last resort."
        )

    if args.backup:
        backup_path = Path(args.backup)
        try:
            backup = backup_path.read_bytes()
        except OSError as exc:
            return _fail(f"cannot read backup {backup_path}: {exc}")
        print(f"backup:   {backup_path}")
        print(f"          {len(backup):,} bytes, sha256 {sha256_hex(backup)}")
        if len(backup) != FLASH_SIZE:
            print(f"warning: the backup is not {FLASH_SIZE:,} bytes; the diff below covers what exists.")
        print_block_diff(image, backup)
    else:
        print("No --backup given, so there is nothing to compare against. If you have your own backup,")
        print("pass it with --backup to see exactly which blocks will change.")

    try:
        flashrom = find_flashrom(args.flashrom)
        version = flashrom_version(flashrom)
    except FlashromError as exc:
        return _fail(str(exc))
    print(f"flashrom: {flashrom}")
    print(f"version:  {version}")

    write_argv = [flashrom, "-p", args.programmer] + chip_args(args.chip) + ["-w", str(image_path)]
    print("Plan:")
    print(f"  1. {shlex.join(write_argv)}")
    print("     flashrom erases, writes and verifies the whole 2 MiB chip.")
    print("  2. read the chip back once and compare its SHA-256 with the image.")
    print("Before you continue: battery disconnected, programmer at 3.3 V, clip seated with pin 1 on the dot,")
    print("and `probe` reports a 2048 kB chip. Do not unplug anything until this command finishes.")

    answer = _ask("Type RESTORE to write the chip, anything else to stop: ")
    if answer.strip() != "RESTORE":
        print("Stopped. Nothing was written.")
        return 1

    try:
        res = write_chip(flashrom, args.programmer, args.chip, image_path)
    except FlashromError as exc:
        return _fail(str(exc) + "\nThe chip may be partly written. Keep the clip on and retry restore.")
    print("flashrom output:")
    _print_indented(res.output or "(no output)")
    if not res.ok:
        return _fail(
            f"flashrom -w failed (exit {res.returncode}). The chip may hold a mix of old and new data.\n"
            "Next steps: keep everything connected, do not power the camera, check the output above for\n"
            "the reason (contact, power, verify failure), reseat the clip and run restore again until\n"
            "flashrom reports VERIFIED. Only then disconnect."
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="charmera-readback-"))
    readback_path = tmp_dir / "readback.bin"
    try:
        res = read_chip(flashrom, args.programmer, args.chip, readback_path)
    except FlashromError as exc:
        return _fail(str(exc) + "\nThe write finished but the read-back could not run. Run `backup` to check.")
    if not res.ok or not readback_path.is_file():
        print("flashrom output:")
        _print_indented(res.output or "(no output)")
        return _fail(
            f"the read-back after writing failed (exit {res.returncode}). The write itself reported\n"
            "success, but it is not confirmed. Run `backup` and compare its SHA-256 with the image."
        )
    readback = readback_path.read_bytes()
    readback_sha = sha256_hex(readback)
    print(f"read-back: {len(readback):,} bytes, sha256 {readback_sha}")
    if readback_sha != info["sha256"]:
        diffs = block_diff(image, readback)
        print(f"{len(diffs)} block(s) differ between the image and the read-back:")
        for d in diffs[:16]:
            print(f"  block {d.index:2d}  0x{d.start:06X}  {d.differing_bytes:,} bytes  ({d.region})")
        print(f"The read-back is kept at {readback_path}")
        return _fail(
            "the chip does not match the image after writing.\n"
            "Next steps: do not power the camera; check the clip contact and that the battery is\n"
            "disconnected; run `probe`; then run restore again. If it keeps failing on the same blocks,\n"
            "compare the kept read-back with the image to see whether the bytes are stuck or random."
        )
    try:
        readback_path.unlink()
        tmp_dir.rmdir()
    except OSError:
        pass
    print("Restore verified: the chip now matches the image.")
    print("Disconnect the clip, reconnect the battery and power the camera on.")
    return 0


# ----------------------------------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--flashrom", metavar="PATH", help="flashrom executable (default: search PATH)")
    parser.add_argument(
        "--programmer",
        default=DEFAULT_PROGRAMMER,
        metavar="NAME",
        help=f"flashrom programmer string, passed to -p (default: {DEFAULT_PROGRAMMER}; "
        "a Pico running pico-serprog is serprog:dev=/dev/ttyACM0 or serprog:dev=COM3)",
    )
    parser.add_argument(
        "--chip",
        default=DEFAULT_CHIP,
        metavar="NAME",
        help=f'flashrom chip definition, passed to -c (default: "{DEFAULT_CHIP}", which is how the '
        "community read this Puya part; `auto` omits -c and lets flashrom probe)",
    )


def register(subparsers) -> None:
    probe = subparsers.add_parser(
        "probe",
        help="detect the flash chip through flashrom (read-only)",
        description="Show flashrom's version and ask it to detect the chip on the programmer. "
        "Never reads or writes the chip contents.",
    )
    _add_common(probe)
    probe.set_defaults(func=cmd_probe)

    backup = subparsers.add_parser(
        "backup",
        help="read the whole 2 MiB flash through flashrom, N times, and keep it only if the reads agree",
        description="Read the chip --reads times (default 2), require the reads to be identical, check "
        "size and magic, and write <label>_<UTC time>.bin with a .sha256 and a .manifest.json next to it. "
        "A chip that fails the checks is still saved, with warnings. Never writes to the chip.",
    )
    _add_common(backup)
    backup.add_argument("--out", required=True, metavar="DIR", help="directory for the backup files")
    backup.add_argument("--reads", type=int, default=2, metavar="N", help="how many times to read (default 2)")
    backup.add_argument("--label", default="charmera", metavar="NAME", help="file name prefix (default charmera)")
    backup.set_defaults(func=cmd_backup)

    restore = subparsers.add_parser(
        "restore",
        help="write a full 2 MiB image to the chip through flashrom (asks for a typed RESTORE)",
        description="Check the image (2 MiB, SPII magic, original-series build stamps), show a 64 KiB "
        "block diff against --backup, show the SHA-256 and the plan, wait for the word RESTORE, then run "
        "flashrom -w (erase, write, verify) and read the chip back to compare.",
    )
    _add_common(restore)
    restore.add_argument("--image", required=True, metavar="FILE", help="full 2 MiB image to write")
    restore.add_argument("--backup", metavar="FILE", help="your own backup, to show what will change")
    restore.add_argument(
        "--allow-unknown-build",
        action="store_true",
        help="write an image whose build stamps are not the original 2025 series (other editions freeze)",
    )
    restore.set_defaults(func=cmd_restore)
