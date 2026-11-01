# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-11-01

### Added

- Scan an unzipped Google Photos Takeout export (any folder level: `Takeout`,
  `Google Photos`, or a folder above them) and copy unique files to
  `YYYY/MM/` folders inside a save folder you choose (or `Unknown date/` when no
  date is known).
- JSON sidecar matching for the standard `<name>.json` pattern,
  `.supplemental-metadata.json`, the cut-short JSON names Google makes for long
  file names (about 46 characters; matched by prefix), `(1)` duplicate
  numbering, and `-edited` copies with nine localised suffixes (`-edited`,
  `-bearbeitet`, `-modifié`, `-modificato`, `-editado`, `-bewerkt`,
  `-redigerad`, `-muokattu`, `-edytowane`).
- Date resolution in priority order: `photoTakenTime` (JSON) →
  EXIF `DateTimeOriginal` → file name pattern → upload `creationTime` → unknown.
  The upload date (`creationTime`) is used only as a last resort and is counted
  separately in the summary.
- Exact-duplicate detection by SHA-256: album copies of the same file are
  counted once; the album names are preserved in the CSV report.
- Set Modified timestamp (all platforms) and Windows Created timestamp
  (Windows only) on every copied file whose date is known.
- Write EXIF `DateTimeOriginal`, `DateTimeDigitized`, `DateTime`, and GPS tags
  into JPEG copies when `piexif` is installed, only where the file does not
  already have them. The date written is the one the app worked out; GPS comes
  from the sidecar.
- GPS coordinates read from `geoData` / `geoDataExif` in the JSON sidecar.
- CSV report (`takeout_rescue_report.csv`) with columns: source, destination,
  taken time, date source, albums, action, latitude, longitude, and description.
  Files that were not copied get a row too.
- Manifest file (`.takeout_rescue_manifest.json`) written before copying
  starts, so even an interrupted run can be undone cleanly.
- "Undo last fix" button in the window: enabled when the chosen save folder has
  a manifest, asks for confirmation, and removes only the files the app created
  that have not been changed.
- `undo` command-line subcommand: removes only the files the tool created
  (plus the report and any empty folders it made), checks SHA-256 before
  deleting, and leaves any photo that has changed since untouched and reported.
- Re-run safety: a second `apply` skips files it already copied (recognised by
  the SHA-256 recorded in the manifest, or by an identical file already in the
  save folder); no duplicate copies are created.
- Non-media files and unreadable files are reported in the summary and the
  CSV report; nothing is silently dropped.
- `scan`, `apply`, and `undo` command-line subcommands in addition to the
  tkinter GUI, and `python -m takeout_rescue --version`.
- Four-step GUI with a progress bar, an "Open folder" shortcut after
  copying completes, and the "Undo last fix" button.
- `build_exe.bat` builds a single-file Windows exe (`dist\TakeoutRescue.exe`)
  and `dist\TakeoutRescue-v0.1.0-windows.zip` with PyInstaller.
- Fake Takeout export generator (`tools/make_fake_takeout.py`) covering the
  main sidecar-matching and date-priority scenarios.
- `TAKEOUT_RESCUE_NO_PIEXIF` environment variable to test or run without
  `piexif`.
- Works fully offline; the app makes no network calls and has no telemetry.

[0.1.0]: https://github.com/drumplayer1234/takeout-rescue/releases/tag/v0.1.0
