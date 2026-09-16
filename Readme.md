# sort_imgs

Sort photographs and videos into a `year / month / day` directory tree using the
capture date stored in their EXIF metadata. Written for offloading a camera card
(Sony A7, `DCIM/100MSDCF`) into a staging area before archiving or editing.

```
staging/
└── 2026/
    └── Jan/
        └── 2026-01-26/
            ├── DSC00123.ARW
            ├── DSC00123.JPG
            └── DSC00001_101.ARW    <- folder roll-over, see below
```

## Requirements

* Python 3.9 or later (standard library only, plus two optional packages).
* [`exifread`](https://pypi.org/project/exifread/) — pure Python, reads both
  JPEG and TIFF-based raw formats (Sony ARW, Nikon NEF, Canon CR2/CR3, DNG).
* [`tqdm`](https://pypi.org/project/tqdm/) — progress bars. Optional: without
  it the script runs silently through each phase, with no loss of function.

```bash
pip install exifread tqdm
```

The script runs without `exifread`, but then every file falls back to its
filesystem modification time, which is **not** the capture date. Install it.

## Quick start

```bash
# 1. Inspect the plan without writing anything. Always do this first.
python sort_imgs.py --dry-run

# 2. Run for real. Point at DCIM: every DCF folder is picked up automatically.
python sort_imgs.py --source /Volumes/Alpha7/DCIM --destination ~/Pictures/staging

# 3. Same, with an audit trail of every transfer.
python sort_imgs.py -s /Volumes/Alpha7/DCIM -d ~/Pictures/staging --manifest import.csv
```

## Handling several DCF folders (100MSDCF, 101MSDCF, ...)

### Why the camera creates them

Camera cards follow the **DCF** standard (Design rule for Camera File system).
A `DCIM` directory holds folders named `NNNXXXXX` — three digits from 100 to
999, then five characters (`MSDCF` is Sony's signature) — and each folder is
capped at **9999 files**. A new folder appears when:

1. the current folder reaches 9999 files;
2. the file counter reaches `DSC09999` and wraps back to `DSC00001` — the body
   *must* open a new folder, otherwise it would write a duplicate file name;
3. the photographer forces a new folder from the menu, or the body is set to
   `Folder Name: Date`, which opens one folder per shooting day (`10160126`).

Case 2 is the dangerous one: `100MSDCF/DSC00001.ARW` and
`101MSDCF/DSC00001.ARW` are **two different photographs sharing a file name**,
and a roll-over can happen in the middle of a single shooting day, so both land
in the same destination directory.

### How this script handles them

Pass the `DCIM` directory to `--source` and every DCF folder is discovered and
processed in ascending order (`100`, then `101`, ...). Specific folders can also
be listed explicitly:

```bash
python sort_imgs.py -s /Volumes/Alpha7/DCIM/100MSDCF /Volumes/Alpha7/DCIM/101MSDCF
```

On a name clash, the destination file is suffixed with the **DCF folder
number**, not with an arbitrary counter:

```
100MSDCF/DSC00001.ARW  ->  2026/Jan/2026-01-26/DSC00001.ARW
101MSDCF/DSC00001.ARW  ->  2026/Jan/2026-01-26/DSC00001_101.ARW
```

This preserves provenance and, more importantly, is **deterministic**: the same
card always produces the same names, whatever the processing order, so a second
run recognises the files as duplicates instead of creating new ones. A run ends
with an explicit line for every day fed by more than one folder:

```
INFO    Day 2026-01-26 merges folders 100, 101
```

Set the policy with `--naming`:

| Value | Behaviour |
| --- | --- |
| `conflict` *(default)* | Plain name, folder number appended only on a clash. |
| `always` | Every file carries its folder number. Uniform, immune to clashes by construction. |
| `never` | Plain name, then an order-dependent `__1`, `__2` counter. Least predictable. |

> **Do not merge the folders manually first.** Copying `100MSDCF` and
> `101MSDCF` into a shared temporary directory *creates* the clash it is meant
> to avoid, silently — `cp` overwrites without warning — and doubles the I/O on
> the card. Let the script enumerate the folders and keep the provenance.

## Configuration

Two constants at the top of the file set the defaults, so routine runs need no
arguments:

```python
DEFAULT_SOURCE = "/Volumes/Alpha7/DCIM"
DEFAULT_DESTINATION: Optional[str] = None   # None -> temp staging directory
```

When `DEFAULT_DESTINATION` is `None` and `--destination` is not given, the tree
is created in `<system temp>/photo_import_<YYYYmmddTHHMMSS>` — a fresh directory
on every run. Set the constant to a fixed path to always land in the same place.

## Options

| Option | Default | Purpose |
| --- | --- | --- |
| `-s`, `--source` | `DEFAULT_SOURCE` | One or more directories. A `DCIM` root is expanded into its DCF folders. |
| `-d`, `--destination`, `--output-directory` | `DEFAULT_DESTINATION` | Root of the sorted tree. |
| `--naming` | `conflict` | Use of the DCF folder number in destination names. |
| `--day-format` | `%Y-%m-%d` | `strftime` pattern for the day directory. |
| `--month-format` | *(fixed table)* | `strftime` pattern for the month directory. |
| `--extensions` | 19 photo/video extensions | Extensions to process, case-insensitive. |
| `--recursive` | off | Descend into sub-directories of each source folder. |
| `--move` | off | Move instead of copy. **Destructive on the source.** |
| `--dry-run` | off | Print the plan, write nothing. |
| `--report` | `<destination>/_import_reports/sort_imgs_<timestamp>.txt` | Plain-text run report. |
| `--no-report` | off | Do not write a run report. |
| `--no-progress` | off | Disable progress bars. |
| `--manifest` | none | CSV audit trail of every planned transfer. |
| `-v`, `--verbose` | off | Mirror the full per-file trace on the console. |

Exit code is `0` on success, `1` if at least one transfer failed, `2` if a
source directory does not exist.

### Directory naming

A day directory cannot be named `26/01/2026` — `/` is the path separator. The
default `%Y-%m-%d` is used because ISO 8601 sorts chronologically in any file
browser. For a day-first layout:

```bash
python sort_imgs.py --day-format "%d-%m-%Y"     # 26-01-2026
```

Month directories use a **fixed English table** (`Jan`, `Feb`, …) rather than
`%b`, which would yield `janv.` under a French locale and produce a different
tree on each machine. Override with `--month-format` if needed, for example
`--month-format "%m"` for `01`.

## Behaviour

### Console output and run report

The terminal shows a progress bar per phase (`Reading metadata`, then
`Copying`), the run header, the final summary, and nothing else except warnings
and errors. The full per-file trace goes to a timestamped plain-text report:

```
Sources     : /Volumes/Alpha7/DCIM/100MSDCF, /Volumes/Alpha7/DCIM/101MSDCF
Destination : /Users/leo/Pictures/staging
Mode        : copy
Report      : /Users/leo/Pictures/staging/_import_reports/sort_imgs_20260126T181203.txt
Reading metadata: 100%|██████████████████| 412/412 [00:06<00:00, 63.4file/s]
Copying:         100%|██████████████████| 412/412 [01:48<00:00,  3.8file/s]
Done: 409 new, 2 renamed, 1 duplicates skipped, 0 failed (412 files scanned)
```

The report holds the same lines the terminal used to print, timestamped, plus
the exact command line that produced them:

```
2026-01-26 18:12:03,161 INFO    Command: sort_imgs.py -s /Volumes/Alpha7/DCIM
2026-01-26 18:12:03,162 INFO    Folder 100MSDCF: 287 file(s) matched
2026-01-26 18:12:09,884 INFO    COPY  [101] DSC00001.ARW -> .../2026-01-26/DSC00001_101.ARW
2026-01-26 18:12:09,885 INFO    SKIP  DSC00002.JPG (already present as ...)
```

Two consequences worth knowing:

* Files with no usable EXIF date produce **one** aggregated warning on the
  console; the per-file detail is in the report at `DEBUG` level. `--verbose`
  mirrors everything on the console.
* Under `--dry-run` no report is written, since that would contradict the
  promise that a dry run touches nothing. Pass `--report path.txt` explicitly if
  you want one anyway.

Progress bars are drawn on `stderr`, so `python sort_imgs.py > run.log` captures
the header and summary without the bar escape sequences. Use `--no-progress` for
fully non-interactive contexts (cron, CI).

### Date resolution

The capture date is read from the first tag that yields a valid value:

1. `EXIF DateTimeOriginal` — when the shutter fired. Preferred.
2. `EXIF DateTimeDigitized` — when the camera wrote the file.
3. `Image DateTime` — last modification recorded in IFD0.
4. Filesystem `mtime` — last resort, logged as a `WARNING` on every occurrence.

The tag actually used is recorded in the `date_origin` column of the manifest,
so files sorted on a weak basis can be audited afterwards. All-zero placeholder
dates (written by bodies with an unset clock) are rejected.

### Non-destructive by design

* Files are **copied** by default, with `shutil.copy2` so timestamps and
  permission bits survive. `--move` is opt-in.
* The run is **idempotent**. A file already present at the destination with an
  identical SHA-256 digest is skipped, not duplicated. Re-running on the same
  card is safe.
* Nothing is ever overwritten. A destination name taken by a *different* file is
  resolved as described above.
* Destinations are resolved for the whole batch *before* any I/O, so `--dry-run`
  exercises the same code path as a real run, minus the writes. Two files
  planned in the same run cannot be assigned the same target.

Digests are only computed when file sizes match, so the deduplication check
costs nothing in the common case.

### Manifest

`--manifest import.csv` writes one row per source file:

| Column | Content |
| --- | --- |
| `source` | Absolute path of the original file. |
| `target` | Absolute path of the destination. |
| `captured_at` | Resolved capture datetime, ISO 8601. |
| `date_origin` | EXIF tag used, or `mtime`. |
| `folder` | Originating DCF folder number, e.g. `101`. |
| `status` | `new`, `renamed`, or `duplicate`. |

The manifest is written even under `--dry-run`, which makes it a convenient way
to review a large import in a spreadsheet before committing to it.

## Troubleshooting

**`Source directory not found`** — on macOS, removable media is mounted under
`/Volumes` (with an `s`), not `/Volume`. Check the mount point with
`ls /Volumes`.

**Everything logs `falling back to mtime`** — `exifread` is missing, or the files
are videos. `exifread` does not parse MP4/MOV containers; those are sorted on
`mtime`, which is usually correct on a card that has never been rewritten.

**A DCF folder was not picked up** — discovery matches `NNNXXXXX` exactly
(three digits plus five characters). Pass the folder explicitly to `--source` if
it was renamed.

## Known limitations

* **No timezone handling.** `OffsetTimeOriginal` is ignored, so a body whose
  clock was not adjusted while travelling will sort into the wrong day near
  midnight.
* **RAW+JPEG pairs are sorted independently.** If their timestamps straddle
  midnight they land in different day directories.
* **`--naming always` weakens deduplication.** Since every file gets a distinct
  folder-tagged name, two byte-identical files coming from two folders no longer
  collide and are both copied. The default `conflict` policy does not have this
  behaviour.
* **No post-copy verification.** The destination digest is not re-read after
  writing, so a silently failing card or drive would not be detected.