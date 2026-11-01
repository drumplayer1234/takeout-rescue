# Takeout Rescue

Rescues dates, removes duplicates, and organises your Google Photos Takeout download into a clean `YYYY/MM/` folder tree.

## The problem

When you download your library with Google Takeout:

- The files do not carry the real taken date — it is hidden in a separate `.json` sidecar.
- The same photo appears in multiple album folders.
- Long file names are cut short in the sidecar's file name, so a photo and its `.json` no longer have matching names.

## What it does

- Matches each photo/video to its JSON sidecar, handling Google's quirks: `.supplemental-metadata` naming, the cut-short names of long files (Google cuts them at about 46 characters; the app matches by prefix), `(1)` duplicate numbering, and localised `-edited` copies (`-edited`, `-bearbeitet`, `-modifié`, `-modificato`, `-editado`, `-bewerkt`, `-redigerad`, `-muokattu`, `-edytowane`).
- Works out the real taken date in priority order: `photoTakenTime` (JSON) → EXIF `DateTimeOriginal` → file name pattern → upload `creationTime` → unknown. Times from the JSON are converted to your computer's local time; EXIF and file-name times are used as written.
- Skips exact duplicate files (album copies, identified by SHA-256) — each unique photo is copied once.
- Copies each unique photo/video into `YYYY/MM/` folders inside the save folder you choose (or `Unknown date/` when no date could be found), and sets the file's Modified and Windows Created timestamps whenever a date is known.
- Writes EXIF date and GPS coordinates into JPEG copies when `piexif` is installed, only where the file does not already have them.
- Never modifies your original export folder.
- Writes a CSV report (`takeout_rescue_report.csv`, in the save folder) with the source, destination, taken time, date source, albums, action, latitude, longitude and description of every copied file, plus a row for every file it did not copy.
- Re-running skips files it already copied.
- Undo removes only the files the tool created that have not been changed.
- Files it did not copy (non-media types, unreadable files) are listed in the summary and the report — nothing is silently dropped.

## Download & run

Download `TakeoutRescue-v0.1.0-windows.zip` from the
[Releases page](https://github.com/drumplayer1234/takeout-rescue/releases),
extract it, and run `TakeoutRescue.exe`.

**Windows SmartScreen warning:** the executable is unsigned. If Windows shows
"Windows protected your PC", click **More info** then **Run anyway**.

> **Unzip your Takeout download first.** The app reads an unzipped folder —
> `.zip` and `.tgz` archives are not supported.

## How to use

Four steps in the window, plus an optional undo:

1. Click **"1. Choose your Google Photos download folder"** and pick the
   unzipped `Takeout` folder. The app searches every sub-folder below the one
   you pick, so the `Google Photos` folder inside `Takeout`, or a folder that
   contains `Takeout`, also works — a `Google Photos` sub-folder is not
   required. Anything in the chosen folder that is not a photo or video (for
   example other Google services in a full Takeout) is not copied and is listed
   as "not copied", and photos or videos found anywhere under it are included, so
   pick the narrowest folder that holds the photos you want.
2. Click **"2. Scan"**. The app reads every photo, video and sidecar and shows a
   summary — nothing is written yet.
3. Click **"3. Choose where to save the clean photos"** and pick a destination
   folder (a new or empty folder is best). It cannot be inside the download
   folder.
4. Click **"4. Fix my photos"** to copy and date-correct everything.

When copying finishes, an **"Open folder"** button appears so you can inspect
the result straight away.

Optional — **"Undo last fix"**: if the save folder chosen in step 3 holds a
previous fix (the app checks for its manifest file), the **"Undo last fix"**
button is enabled. It asks you to confirm, then removes the photos Takeout
Rescue copied there. Any file you have changed since is left alone and shown in
a warning, and your original download is not touched. Because step 3 is only
available after a scan, to undo an older fix in a new session choose and scan a
folder first, then pick the earlier save folder in step 3.

## Safe by design

- **Originals are never touched.** The app only ever reads the export folder;
  every write goes to the separate destination you choose, and a destination
  inside the download folder is refused.
- **Undo.** Click **"Undo last fix"** in the window (it asks first), or run
  `python -m takeout_rescue undo <out_dir>` from the command line. Either way it
  removes only the files the tool created, checks that each photo is unchanged
  before deleting it (compared by SHA-256), also removes the report and any empty
  folders the tool made, and leaves any photo you edited since in place and
  reports it.
- **Re-run safe.** Running again skips files it already copied (recognised from
  the SHA-256 recorded in the manifest, or by an identical file already in
  place). A second run copies nothing new.
- **Fully offline.** The app makes no network calls and sends nothing anywhere;
  it has no telemetry.

## Current limits

- **Must unzip first.** Zip or `.tgz` archives are not read directly.
- **Google Photos only.** iCloud exports are not supported yet.
- **Recognised file types only.** JPEG, PNG, GIF, WebP, HEIC/HEIF, AVIF, TIFF,
  BMP, the RAW types DNG, CR2, CR3, NEF, ARW, RAF, ORF and RW2, and the video
  types MP4, MOV, MP, AVI, 3GP, MPG/MPEG, M4V, MKV, WebM, MTS/M2TS, WMV and FLV.
  Other files are not copied; they are listed as "not copied".
- **EXIF is read and written for JPEG only** (`piexif` required for both; the
  downloadable exe includes it). HEIC, PNG, GIF, WebP, RAW formats, and videos
  get their file's Modified/Created timestamps corrected, but nothing is read
  from or written inside the file itself.
- **Prefix-matching edge case.** When Google cuts a long JSON name short, the app
  matches by prefix and uses the JSON `title` field to disambiguate. If two long
  file names share the same long prefix and the JSON has no `title`, only the
  first one (in name order) gets that sidecar — the other loses it. This is rare
  in practice.
- **Windows-focused.** The Windows Created timestamp is set on Windows only,
  and the **"Open folder"** button uses a Windows-only call. On Linux and macOS
  the Modified timestamp is still set by the code, but no pre-built binary is
  provided.
- **Very old dates (before 1970)** may not be settable as the file date on
  Windows. The photo is still copied to the right year/month folder; the report
  notes "file date not set" and the app shows a warning. Dates before 1826 are
  treated as invalid and ignored.
- **Prototype.** This is v0.1.0. Back up your photos before running, and check
  the CSV report for anything unexpected.

## Run from source

Requires Python 3.12 or later.

```
pip install -r requirements.txt
```

Then double-click `run.bat` or run:

```
python -m takeout_rescue
```

### Build the Windows exe

On Windows, install PyInstaller (the build also needs `piexif`, which is in
`requirements.txt`) and run the build script from the project folder:

```
pip install -r requirements.txt
pip install pyinstaller
build_exe.bat
```

It produces `dist\TakeoutRescue.exe` (a single-file, windowed app) and
`dist\TakeoutRescue-v0.1.0-windows.zip`, which contains the exe. The version in
the zip name is read from `takeout_rescue/__init__.py`. The `build` and `dist`
folders are not tracked by git.

### Command line

```
python -m takeout_rescue scan  <export_dir>
python -m takeout_rescue apply <export_dir> <out_dir>
python -m takeout_rescue undo  <out_dir>
python -m takeout_rescue --version
```

`scan` shows what the app found without copying anything.
`apply` scans and then copies.
`undo` deletes only the files a previous `apply` created.
`--version` prints the version number and exits.

### Try it with fake data

```
python tools/make_fake_takeout.py fake_data
python -m takeout_rescue scan  fake_data/Takeout
python -m takeout_rescue apply fake_data/Takeout fake_data_out
```

### Run the tests

```
python -m unittest discover -s tests -v
```

Set `TAKEOUT_RESCUE_NO_PIEXIF=1` to run the test suite as if `piexif` were not
installed.

## License

MIT — see [LICENSE](LICENSE).
