#!/usr/bin/env python3
"""Sort photographs into a YEAR / MONTH / DAY tree based on their capture date.

The capture date is resolved from EXIF metadata when available, with an
explicit and logged fallback chain:

    EXIF DateTimeOriginal  ->  EXIF DateTimeDigitized  ->  Image DateTime
                           ->  filesystem mtime (last resort)

Resulting layout (default formats)::

    <destination>/
        2026/
            Jan/
                2026-01-26/
                    DSC00123.ARW
                    DSC00123.JPG

Multiple DCF folders
--------------------
A camera card holds one or more DCF directories (``100MSDCF``, ``101MSDCF``,
...). The DCF standard caps a directory at 9999 files, and the body must open a
new one when its file counter wraps past ``DSC09999`` back to ``DSC00001``.
Consequently **two different photographs can share the same file name across
two folders**, and they may well belong to the same shooting day.

This script therefore:

* enumerates every DCF folder of a card in a single pass, in sorted order;
* keeps the originating folder number as provenance;
* resolves a name clash by suffixing the folder number
  (``DSC00001.ARW`` and ``DSC00001_101.ARW``) rather than by an
  order-dependent counter, so the result is stable across runs;
* never overwrites, and skips byte-identical files.

Flattening several folders into a shared temporary directory before sorting
would instead *create* the clash, silently, at ``cp`` time. Do not do that.

Design constraints
------------------
* Non-destructive by default: files are *copied* (``shutil.copy2``, which
  preserves mtime/atime and permissions). ``--move`` is opt-in.
* Idempotent: re-running on the same card is safe. A file already present at
  the destination with an identical SHA-256 digest is skipped, not duplicated.
* Reproducible: month labels use a fixed English table instead of ``%b``,
  which is locale-dependent. Every transfer is recorded in an optional CSV
  manifest so the operation can be audited or reversed.
* Fail-safe: ``--dry-run`` prints the full plan without touching the disk.

Dependency
----------
``exifread`` (pure Python, reads both JPEG and TIFF-based raw formats such as
Sony ARW, Nikon NEF, Canon CR2, Adobe DNG)::

    pip install exifread

If ``exifread`` is not installed the script still runs, but every file falls
back to its filesystem modification time, which is unreliable.

Output
------
The terminal shows a progress bar per phase plus warnings and errors; the full
per-file trace is written to a timestamped plain-text report under
``<destination>/_import_reports/``. ``tqdm`` is an optional dependency: without
it the bars are simply absent.

Usage
-----
    python sort_imgs.py --dry-run
    python sort_imgs.py --source /Volumes/Alpha7/DCIM --destination ~/Pictures/staging
    python sort_imgs.py --source /Volumes/Alpha7/DCIM/100MSDCF /Volumes/Alpha7/DCIM/101MSDCF
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

try:  # optional, but strongly recommended
    import exifread
except ImportError:  # pragma: no cover - environment dependent
    exifread = None  # type: ignore[assignment]

try:  # optional: progress bars
    from tqdm import tqdm
except ImportError:  # pragma: no cover - environment dependent
    class tqdm:  # type: ignore[no-redef]
        """Minimal stand-in so the script runs without the tqdm package.

        Iteration and the ``write`` class method are the only features used
        here; everything else is a no-op.
        """

        def __init__(self, iterable=None, **_kwargs):
            self._iterable = [] if iterable is None else iterable

        def __iter__(self):
            return iter(self._iterable)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def update(self, _n=1):
            pass

        def close(self):
            pass

        def set_postfix_str(self, _text):
            pass

        @staticmethod
        def write(message, **_kwargs):
            print(message)

LOGGER = logging.getLogger("photo_sorter")

#: Console handler, set by :func:`setup_logging`. Kept module-level so
#: :func:`announce` can tell whether a message already reached the terminal.
CONSOLE_HANDLER: Optional[logging.Handler] = None

# --------------------------------------------------------------------------
# Configuration defaults
# --------------------------------------------------------------------------

#: Card root. May be the DCIM directory (all DCF folders are then discovered
#: automatically) or a single DCF folder.
DEFAULT_SOURCE = "/Volumes/Alpha7/DCIM"

#: Root of the sorted year/month/day tree, i.e. the staging area.
#: None -> a fresh timestamped directory in the system temp folder.
DEFAULT_DESTINATION: Optional[str] = None

#: DCF directory names: three digits (100-999) followed by five characters,
#: e.g. 100MSDCF on Sony bodies, or 10160126 when "Folder Name: Date" is used.
DCF_DIR_PATTERN = re.compile(r"^(\d{3})[0-9A-Za-z_]{5}$")

#: Locale-independent month labels, so the tree is identical on any machine.
MONTH_ABBR: Tuple[str, ...] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

#: Extensions considered as media. Matching is case-insensitive.
DEFAULT_EXTENSIONS: Tuple[str, ...] = (
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff",
    ".arw", ".dng", ".nef", ".cr2", ".cr3", ".raf", ".orf", ".rw2",
    ".mp4", ".mov", ".avi", ".mts",
)

#: EXIF tags probed in order of decreasing trustworthiness.
EXIF_DATE_TAGS: Tuple[str, ...] = (
    "EXIF DateTimeOriginal",   # instant the shutter fired
    "EXIF DateTimeDigitized",  # instant the file was written by the camera
    "Image DateTime",          # last modification recorded in IFD0
)

#: Accepted serialisations of an EXIF datetime string.
EXIF_DATE_FORMATS: Tuple[str, ...] = (
    "%Y:%m:%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d",
)

HASH_CHUNK_SIZE = 1 << 20  # 1 MiB


# --------------------------------------------------------------------------
# Logging and progress
# --------------------------------------------------------------------------

class TqdmLoggingHandler(logging.Handler):
    """Console handler that routes records through ``tqdm.write``.

    Writing to ``sys.stdout`` directly while a progress bar is active corrupts
    the bar; ``tqdm.write`` clears it, prints, and redraws.
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - I/O
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(report_path: Optional[Path], verbose: bool) -> None:
    """Wire the two output channels.

    * Console: warnings and errors only, so the progress bar stays readable.
      ``--verbose`` lowers it to DEBUG for troubleshooting.
    * Report file: the full per-file trace at DEBUG level, with timestamps.
      This is the audit trail the terminal no longer shows.
    """
    global CONSOLE_HANDLER

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = TqdmLoggingHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(console)
    CONSOLE_HANDLER = console

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = logging.FileHandler(report_path, mode="w", encoding="utf-8")
        report.setLevel(logging.DEBUG)
        report.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        root.addHandler(report)


def announce(message: str) -> None:
    """Log *message* and make sure it reaches the console exactly once.

    Used for the few lines that must stay visible even though the console
    handler is muted: the run header and the final summary.
    """
    LOGGER.info(message)
    if CONSOLE_HANDLER is not None and CONSOLE_HANDLER.level > logging.INFO:
        tqdm.write(message)


def progress(iterable, desc: str, unit: str, enabled: bool, total: Optional[int] = None):
    """Wrap *iterable* in a progress bar unless bars are disabled."""
    if not enabled:
        return iterable
    return tqdm(iterable, desc=desc, unit=unit, total=total, leave=False)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

class Plan(NamedTuple):
    """A single resolved source -> destination transfer."""

    source: Path
    target: Path
    captured_at: datetime
    date_origin: str  # which tag (or fallback) provided ``captured_at``
    folder: str       # originating DCF folder tag, e.g. "100"
    status: str       # "new" | "renamed" | "duplicate"


# --------------------------------------------------------------------------
# Source discovery
# --------------------------------------------------------------------------

def folder_tag(directory: Path) -> str:
    """Short provenance tag for a source directory.

    ``100MSDCF`` -> ``100``. Anything that is not a DCF directory falls back to
    its sanitised name, so the tag stays usable inside a file name.
    """
    match = DCF_DIR_PATTERN.match(directory.name)
    if match:
        return match.group(1)
    return re.sub(r"[^0-9A-Za-z]+", "-", directory.name).strip("-") or "src"


def discover_source_dirs(root: Path) -> List[Path]:
    """Expand *root* into the list of directories actually holding media.

    * A DCF directory is returned as-is.
    * A DCIM directory is expanded into its DCF children, sorted by number, so
      ``100MSDCF`` is always processed before ``101MSDCF``. This ordering is
      what makes name-clash resolution deterministic.
    * Anything else is returned as-is.
    """
    if DCF_DIR_PATTERN.match(root.name):
        return [root]

    children = sorted(
        (child for child in root.iterdir()
         if child.is_dir() and DCF_DIR_PATTERN.match(child.name)),
        key=lambda path: path.name,
    )
    if children:
        LOGGER.info(
            "Discovered %d DCF folder(s) under %s: %s",
            len(children), root, ", ".join(child.name for child in children),
        )
        return children

    return [root]


# --------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------

def _parse_exif_datetime(raw: str) -> Optional[datetime]:
    """Parse an EXIF datetime string, tolerating the usual camera quirks."""
    value = raw.strip().replace("\x00", "")
    if not value or value.startswith("0000"):
        # Some bodies write all-zero placeholders when the clock is unset.
        return None
    for fmt in EXIF_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def read_capture_datetime(path: Path) -> Tuple[datetime, str]:
    """Return ``(capture_datetime, origin)`` for *path*.

    ``origin`` is the name of the EXIF tag used, or ``"mtime"`` when the
    function had to fall back to the filesystem modification time. The origin
    is propagated to the manifest so that low-confidence rows can be audited
    afterwards.
    """
    if exifread is not None:
        try:
            with path.open("rb") as handle:
                # details=False skips makernotes and thumbnails: much faster
                # and sufficient for date tags.
                tags = exifread.process_file(handle, details=False)
        except Exception as exc:  # corrupted file, unsupported container, ...
            LOGGER.debug("EXIF read failed for %s: %s", path.name, exc)
            tags = {}

        for tag in EXIF_DATE_TAGS:
            if tag in tags:
                parsed = _parse_exif_datetime(str(tags[tag]))
                if parsed is not None:
                    return parsed, tag

    # Fallback. Note that st_mtime is *not* the capture time in general; it is
    # only a usable proxy when the file has never been rewritten. Logged at
    # DEBUG so the console is not flooded: build_plan emits one aggregated
    # warning instead, and the report keeps the per-file detail.
    LOGGER.debug("No usable EXIF date for %s; falling back to mtime", path.name)
    return datetime.fromtimestamp(path.stat().st_mtime), "mtime"


# --------------------------------------------------------------------------
# Filesystem helpers
# --------------------------------------------------------------------------

def iter_media_files(
    directory: Path,
    extensions: Sequence[str],
    recursive: bool,
    exclude_root: Optional[Path] = None,
) -> Iterator[Path]:
    """Yield media files in *directory*, sorted for deterministic ordering."""
    allowed = {ext.lower() for ext in extensions}
    pattern = "**/*" if recursive else "*"
    for candidate in sorted(directory.glob(pattern)):
        if not candidate.is_file():
            continue
        if candidate.name.startswith("."):  # ._AppleDouble, .DS_Store, ...
            continue
        if candidate.suffix.lower() not in allowed:
            continue
        if exclude_root is not None and _is_within(candidate, exclude_root):
            # Guards against a destination nested inside a source.
            continue
        yield candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def file_digest(path: Path) -> str:
    """SHA-256 of the file content, streamed to keep memory flat."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_content(left: Path, right: Path) -> bool:
    """Cheap size test first, full digest only if the sizes match."""
    if left.stat().st_size != right.stat().st_size:
        return False
    return file_digest(left) == file_digest(right)


# --------------------------------------------------------------------------
# Name resolution
# --------------------------------------------------------------------------

def candidate_names(
    stem: str, suffix: str, tag: str, policy: str,
) -> Iterator[str]:
    """Yield destination names in decreasing order of preference.

    ``policy`` controls the use of the DCF folder tag:

    * ``"conflict"`` (default) - plain name first, folder-tagged name on clash.
      ``DSC00001.ARW`` then ``DSC00001_101.ARW``.
    * ``"always"`` - every file carries its folder tag. Uniform, and immune to
      clashes by construction, at the cost of longer names.
    * ``"never"`` - plain name, then a numeric counter. Order-dependent; kept
      for users who want the original names untouched.

    The generator is infinite, so name resolution always terminates.
    """
    tagged = f"{stem}_{tag}"
    if policy == "always":
        yield f"{tagged}{suffix}"
        base = tagged
    else:
        yield f"{stem}{suffix}"
        if policy == "conflict":
            yield f"{tagged}{suffix}"
            base = tagged
        else:
            base = stem

    index = 1
    while True:
        yield f"{base}__{index}{suffix}"
        index += 1


def resolve_target(
    source: Path,
    target_dir: Path,
    tag: str,
    policy: str,
    reserved: Dict[Path, Path],
) -> Tuple[Path, str]:
    """Pick a non-destructive destination path inside *target_dir*.

    *reserved* maps already-planned destinations to their source file, so that
    two files processed in the same run cannot be assigned the same target.

    Returns ``(path, status)`` where status is one of:

    * ``"new"``       - the preferred name was free;
    * ``"duplicate"`` - an identical file is already there or already planned
      (the transfer is skipped);
    * ``"renamed"``   - the preferred name was taken by a *different* file.
    """
    for position, name in enumerate(candidate_names(source.stem, source.suffix, tag, policy)):
        candidate = target_dir / name

        planned = reserved.get(candidate)
        if planned is not None:
            # Same destination already claimed earlier in this run.
            if _same_content(planned, source):
                return candidate, "duplicate"
            continue

        if not candidate.exists():
            return candidate, "new" if position == 0 else "renamed"

        if _same_content(candidate, source):
            return candidate, "duplicate"

    raise RuntimeError("unreachable: candidate_names is infinite")


# --------------------------------------------------------------------------
# Path construction
# --------------------------------------------------------------------------

def build_target_dir(
    destination: Path,
    captured_at: datetime,
    month_format: Optional[str],
    day_format: str,
) -> Path:
    """Build ``<destination>/<year>/<month>/<day>``."""
    year = f"{captured_at.year:04d}"
    month = (
        captured_at.strftime(month_format)
        if month_format
        else MONTH_ABBR[captured_at.month - 1]
    )
    day = captured_at.strftime(day_format)
    return destination / year / month / day


# --------------------------------------------------------------------------
# Planning and execution
# --------------------------------------------------------------------------

def build_plan(
    source_dirs: Sequence[Path],
    destination: Path,
    extensions: Sequence[str],
    recursive: bool,
    month_format: Optional[str],
    day_format: str,
    naming_policy: str,
    show_progress: bool = True,
) -> List[Plan]:
    """Resolve every transfer before touching the disk.

    Planning is kept separate from execution so that ``--dry-run`` exercises
    exactly the same code path as a real run, minus the I/O.
    """
    # Enumerate first: globbing is cheap compared to EXIF parsing, and knowing
    # the total up front lets the progress bar show a meaningful ETA.
    entries: List[Tuple[Path, str]] = []
    for directory in source_dirs:
        tag = folder_tag(directory)
        found = list(iter_media_files(directory, extensions, recursive, exclude_root=destination))
        LOGGER.info("Folder %s: %d file(s) matched", directory.name, len(found))
        entries.extend((media, tag) for media in found)

    plan: List[Plan] = []
    reserved: Dict[Path, Path] = {}

    for media, tag in progress(entries, "Reading metadata", "file", show_progress, len(entries)):
        captured_at, origin = read_capture_datetime(media)
        target_dir = build_target_dir(destination, captured_at, month_format, day_format)
        target, status = resolve_target(media, target_dir, tag, naming_policy, reserved)
        reserved[target] = media
        plan.append(Plan(media, target, captured_at, origin, tag, status))

    fallbacks = sum(1 for item in plan if item.date_origin == "mtime")
    if fallbacks:
        LOGGER.warning(
            "%d of %d file(s) had no usable EXIF date and were sorted on their "
            "filesystem mtime (see the report for the list)", fallbacks, len(plan),
        )

    return plan


def execute_plan(
    plan: Sequence[Plan], move: bool, dry_run: bool, show_progress: bool = True,
) -> Dict[str, int]:
    """Perform the transfers and return per-status counters."""
    counters = {"new": 0, "renamed": 0, "duplicate": 0, "failed": 0}
    verb = "MOVE" if move else "COPY"
    label = "Moving" if move else "Copying"

    for item in progress(plan, label, "file", show_progress, len(plan)):
        if item.status == "duplicate":
            counters["duplicate"] += 1
            LOGGER.info("SKIP  %s (already present as %s)", item.source.name, item.target)
            continue

        LOGGER.info("%s  [%s] %s -> %s", verb, item.folder, item.source.name, item.target)
        if dry_run:
            counters[item.status] += 1
            continue

        try:
            item.target.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(item.source), str(item.target))
            else:
                # copy2 preserves timestamps and permission bits.
                shutil.copy2(item.source, item.target)
        except OSError as exc:
            counters["failed"] += 1
            LOGGER.error("FAILED %s: %s", item.source, exc)
            continue

        counters[item.status] += 1

    return counters


def report_merged_days(plan: Sequence[Plan]) -> None:
    """Log the days that were fed by more than one DCF folder.

    A folder roll-over in the middle of a shooting day is exactly the case
    where name clashes occur, so it is worth surfacing explicitly.
    """
    per_day: Dict[Path, set] = defaultdict(set)
    for item in plan:
        per_day[item.target.parent].add(item.folder)

    for day, folders in sorted(per_day.items()):
        if len(folders) > 1:
            LOGGER.info("Day %s merges folders %s", day.name, ", ".join(sorted(folders)))


def write_manifest(plan: Sequence[Plan], manifest_path: Path) -> None:
    """Write an audit trail of the run (one row per source file)."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "target", "captured_at", "date_origin", "folder", "status"])
        for item in plan:
            writer.writerow([
                str(item.source),
                str(item.target),
                item.captured_at.isoformat(timespec="seconds"),
                item.date_origin,
                item.folder,
                item.status,
            ])
    LOGGER.info("Manifest written to %s", manifest_path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sort photographs into a year/month/day tree using EXIF metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-s", "--source", type=Path, nargs="+", default=[Path(DEFAULT_SOURCE)],
        help="One or more directories to sort. A DCIM directory is expanded "
             "into its DCF folders (100MSDCF, 101MSDCF, ...) automatically.",
    )
    parser.add_argument(
        "-d", "--destination", "--output-directory", type=Path,
        default=Path(DEFAULT_DESTINATION) if DEFAULT_DESTINATION else None,
        help="Root of the sorted tree. Falls back to DEFAULT_DESTINATION, then "
             "to a fresh 'photo_import_<timestamp>' directory in the system "
             "temp folder.",
    )
    parser.add_argument(
        "--naming", choices=("conflict", "always", "never"), default="conflict",
        help="Use of the DCF folder number in destination names: only on a "
             "name clash, on every file, or never.",
    )
    parser.add_argument(
        "--day-format", default="%Y-%m-%d",
        help="strftime pattern for the day directory. Use '%%d-%%m-%%Y' for 26-01-2026.",
    )
    parser.add_argument(
        "--month-format", default=None,
        help="strftime pattern for the month directory. Omitted: fixed English "
             "abbreviations (Jan, Feb, ...), which are locale-independent.",
    )
    parser.add_argument(
        "--extensions", nargs="+", default=list(DEFAULT_EXTENSIONS),
        help="Extensions to process, case-insensitive.",
    )
    parser.add_argument(
        "--recursive", action="store_true",
        help="Descend into sub-directories of each source folder.",
    )
    parser.add_argument(
        "--move", action="store_true",
        help="Move instead of copy. Destructive on the source; off by default.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the transfer plan without writing anything.",
    )
    parser.add_argument(
        "--report", type=Path, default=None,
        help="Path of the plain-text run report. Defaults to "
             "'<destination>/_import_reports/sort_imgs_<timestamp>.txt'. Under "
             "--dry-run no report is written unless this option is given.",
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Do not write a run report.",
    )
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Disable progress bars (useful when piping the output to a file).",
    )
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="Optional CSV path recording every planned transfer.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Mirror the full per-file trace on the console, not just in the report.",
    )
    return parser.parse_args(argv)


def resolve_report_path(args: argparse.Namespace, destination: Path) -> Optional[Path]:
    """Decide where the run report goes, if anywhere.

    Under ``--dry-run`` the default is *no* report, since writing one would
    contradict the promise that a dry run touches nothing. An explicit
    ``--report`` still wins.
    """
    if args.no_report:
        return None
    if args.report is not None:
        return args.report
    if args.dry_run:
        return None
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return destination / "_import_reports" / f"sort_imgs_{stamp}.txt"


def default_destination() -> Path:
    """Timestamped staging directory, used when no destination is configured."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return Path(tempfile.gettempdir()) / f"photo_import_{stamp}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    # The destination must be known before logging starts, since the report
    # lives inside it by default.
    destination: Path = args.destination or default_destination()
    report_path = resolve_report_path(args, destination)
    setup_logging(report_path, verbose=args.verbose)
    show_progress = not args.no_progress

    LOGGER.info("Command: %s", " ".join(sys.argv))

    if exifread is None:
        LOGGER.warning(
            "exifread is not installed; every file will fall back to its "
            "filesystem mtime. Install it with: pip install exifread"
        )

    source_dirs: List[Path] = []
    for root in args.source:
        if not root.is_dir():
            LOGGER.error("Source directory not found: %s", root)
            LOGGER.error("On macOS, removable media is mounted under /Volumes (with an 's').")
            return 2
        for directory in discover_source_dirs(root):
            if directory not in source_dirs:
                source_dirs.append(directory)

    announce(f"Sources     : {', '.join(str(d) for d in source_dirs)}")
    announce(f"Destination : {destination}")
    announce("Mode        : {}{}".format(
        "move" if args.move else "copy", " (dry-run)" if args.dry_run else ""))
    if report_path is not None:
        announce(f"Report      : {report_path}")

    plan = build_plan(
        source_dirs=source_dirs,
        destination=destination,
        extensions=args.extensions,
        recursive=args.recursive,
        month_format=args.month_format,
        day_format=args.day_format,
        naming_policy=args.naming,
        show_progress=show_progress,
    )

    if not plan:
        LOGGER.warning("No media file matched in the given source(s)")
        return 0

    counters = execute_plan(plan, move=args.move, dry_run=args.dry_run,
                            show_progress=show_progress)
    report_merged_days(plan)

    if args.manifest is not None:
        write_manifest(plan, args.manifest)

    announce(
        "Done: {} new, {} renamed, {} duplicates skipped, {} failed "
        "({} files scanned)".format(
            counters["new"], counters["renamed"], counters["duplicate"],
            counters["failed"], len(plan),
        )
    )
    return 1 if counters["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())