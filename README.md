# Takeout Rescue

Rescues your photos from a Google Takeout download.

When you download Google Photos with Google Takeout, the real "taken" date of each photo is hidden in a separate
`.json` file, so your files all show the day you downloaded them. The same photo also shows up in several album
folders, and long file names get chopped off.

Takeout Rescue reads that export and copies everything into a tidy library:

    OUTPUT/2019/07/IMG_20190714_153012.jpg
    OUTPUT/2020/03/birthday_party.jpg
    OUTPUT/Unknown date/mystery.jpg

* The dates are fixed (file date, plus the date and GPS location written inside JPEGs).
* Duplicate copies are copied once. The album names are kept in a report file.
* Your original download folder is **never changed**. Only copies are made.
* A report (`takeout_rescue_report.csv`) lists where every file came from and went.
* Safe to run again: files that are already in the output folder are skipped ("already done"), so a second
  run creates nothing new. If you already have your own `takeout_rescue_report.csv` there, the report is saved
  as `takeout_rescue_report (2).csv` instead.
* Nothing is dropped silently. Files that are not photos or videos (for example `.txt` or `Thumbs.db`) are
  **not copied**, but they are counted as "Not copied (unknown type)" and listed in the summary and the report.
  Files or folders that could not be read are listed as "Could not read".

## Where the date comes from

In this order: the real "taken" time in Google's JSON file, the date inside the JPEG (EXIF), the date in the
file name, and last the JSON `creationTime`. `creationTime` is only the day the photo was **uploaded**, so it is
used as a last resort and counted separately ("Dates guessed from upload date"). Photos from before 1971 are
fine.

## How to run it

1. Unzip your Takeout download first.
2. Install Python 3.12 and run `python -m pip install piexif` (optional, needed to write dates inside JPEGs).
3. Double-click `run.bat`, or run `python -m takeout_rescue`.
4. Choose the download folder, press Scan, choose where to save, press "Fix my photos".

From the command line:

    python -m takeout_rescue scan  <export_folder>
    python -m takeout_rescue apply <export_folder> <output_folder>
    python -m takeout_rescue undo  <output_folder>

`undo` deletes only the files that `apply` created (it keeps a list in `.takeout_rescue_manifest.json`, written
before copying starts, so even an interrupted run can be undone). A file you changed since is left alone and
reported. If the manifest is damaged, `apply` stops with an error instead of replacing it.

## Try it with fake data

    python tools/make_fake_takeout.py fake_data
    python -m takeout_rescue scan fake_data/Takeout
    python -m takeout_rescue apply fake_data/Takeout fake_data_out

## Run the tests

    python -m unittest discover -s tests -v

Set `TAKEOUT_RESCUE_NO_PIEXIF=1` to test as if piexif were not installed.

## Current limits

* No zip support yet. Unzip the download first.
* Google Photos only. iCloud is not supported yet.
* HEIC, PNG, GIF, WebP and videos only get their file dates fixed. Nothing is written inside them.
* Time zones: the dates in Google's JSON are real moments in time. They are converted to **this computer's
  local time** for the `YYYY/MM` folder, the date written inside the JPEG and the file date. A photo taken at
  23:30 on December 31 in your time zone lands in December. Dates read from EXIF or from a file name have no
  time zone, so they are used exactly as written. If your computer is set to another time zone than the one the
  photo was taken in, a photo near midnight on the last day of a month can land in the neighbouring month.
* Very old dates (before 1970) cannot always be set as the file date on Windows. The photo is still copied to the
  right folder and the report says "file date not set".
* Photos with no date anywhere go to `Unknown date`.
