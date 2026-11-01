## Takeout Rescue v0.1.0

This is the first public release of Takeout Rescue, a small Windows app that
restores real taken dates, removes album duplicates, and organises your Google
Photos Takeout export into tidy `YYYY/MM/` folders. It copies your files to a
new location and never touches the original export.

### Highlights

- Matches each photo and video to its JSON sidecar, handling `.supplemental-metadata`
  naming, Google's cut-short names for long files (about 46 characters),
  `(1)` duplicate numbering, and `-edited` copies with nine localised suffixes.
- Date priority: `photoTakenTime` → EXIF `DateTimeOriginal` → file name
  pattern → upload `creationTime` → unknown.
- Exact-duplicate detection by SHA-256; album copies are counted once and the
  album names are preserved in the CSV report.
- Sets Modified and Windows Created timestamps on every copied file whose date
  is known.
- Writes EXIF date and GPS into JPEG copies (where missing) when `piexif` is
  installed; the downloadable exe includes it.
- CSV report and manifest for full traceability; re-running skips already-done
  files.
- **"Undo last fix" button** in the window: it asks for confirmation and removes
  only the files the app created that have not been changed since. The
  `undo` command does the same from the command line.
- Four-step tkinter GUI plus `scan` / `apply` / `undo` command-line interface.
- Fully offline — no network calls, no telemetry.

### Download

**`TakeoutRescue-v0.1.0-windows.zip`** is attached to this release.
Extract the zip and run `TakeoutRescue.exe`.

> **SmartScreen warning:** the executable is unsigned. If Windows shows
> "Windows protected your PC", click **More info** → **Run anyway**.

To run from source: Python 3.12+, `pip install -r requirements.txt`, then
`run.bat` or `python -m takeout_rescue`. To build the exe yourself, install
PyInstaller (`pip install pyinstaller`) and run `build_exe.bat`.

### Known limits

- The Takeout download must be unzipped first — `.zip`/`.tgz` files are not
  read directly.
- Google Photos only; iCloud is not supported.
- EXIF date and GPS are written for JPEG files only; other formats (HEIC, PNG,
  WebP, RAW, video) receive only file-timestamp correction.
- Very long file names truncated by Google to the same long prefix may lose
  their sidecar match in edge cases.
- The Windows Created timestamp is set on Windows only.
- This is a v0.1.0 prototype — back up your photos and review the CSV report.

### Feedback

Found a bug or have a suggestion? Please open an issue on the
[GitHub Issues page](https://github.com/drumplayer1234/takeout-rescue/issues).
