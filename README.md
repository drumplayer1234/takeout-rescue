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

## How to run it

1. Unzip your Takeout download first.
2. Install Python 3.12 and run `python -m pip install piexif` (optional, needed to write dates inside JPEGs).
3. Double-click `run.bat`, or run `python -m takeout_rescue`.
4. Choose the download folder, press Scan, choose where to save, press "Fix my photos".

From the command line:

    python -m takeout_rescue scan  <export_folder>
    python -m takeout_rescue apply <export_folder> <output_folder>
    python -m takeout_rescue undo  <output_folder>

`undo` deletes only the files that `apply` created (it keeps a list in `.takeout_rescue_manifest.json`).

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
* Dates are treated as UTC (Google stores UTC), so a photo taken late at night may land in a different day or
  month than your local clock shows.
* Photos with no date anywhere go to `Unknown date`.
