"""Takeout Rescue core logic (no GUI code in here).

Typical use:
    plan = scan("C:/Takeout")
    print(plan.summary)
    apply(plan, "C:/CleanPhotos", progress_cb=print)

Dates are stored as UTC epoch seconds.  Dates that come from a file name or
from EXIF are "wall clock" times with no time zone, so they are treated as if
they were UTC too.  That keeps the YYYY/MM folder equal to the date you can see
in the name.
"""

import calendar
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# piexif is optional.  Set TAKEOUT_RESCUE_NO_PIEXIF=1 to pretend it is missing.
try:
    if os.environ.get("TAKEOUT_RESCUE_NO_PIEXIF"):
        raise ImportError("piexif disabled by environment variable")
    import piexif
except ImportError:
    piexif = None

MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp4", ".mov"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}

REPORT_NAME = "takeout_rescue_report.csv"
MANIFEST_NAME = ".takeout_rescue_manifest.json"
UNKNOWN_FOLDER = "Unknown date"

SUPPLEMENTAL = "supplemental-metadata"

# Date sources, in priority order.
SRC_JSON = "json"
SRC_EXIF = "exif"
SRC_FILENAME = "filename"
SRC_UNKNOWN = "unknown"


class TakeoutError(Exception):
    """A problem we can explain to the user in plain words."""


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass
class MediaItem:
    """One unique photo/video (duplicates are folded into `duplicates`)."""
    source: str                      # absolute path of the file we will copy
    rel_source: str                  # path relative to the export folder
    sha256: str
    taken: Optional[int] = None      # UTC epoch seconds, or None if unknown
    date_source: str = SRC_UNKNOWN
    lat: Optional[float] = None
    lon: Optional[float] = None
    description: str = ""
    albums: List[str] = field(default_factory=list)
    edited: bool = False
    json_path: Optional[str] = None
    duplicates: List[str] = field(default_factory=list)   # other copies skipped


@dataclass
class Plan:
    export_dir: str
    items: List[MediaItem]
    total_media: int
    summary: Dict[str, int]
    piexif_available: bool

    def summary_lines(self) -> List[str]:
        return format_summary(self.summary, self.piexif_available)

    def plain_summary(self) -> str:
        return plain_summary(self.summary, self.piexif_available)


# --------------------------------------------------------------------------
# Finding the sidecar JSON for a media file
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"^(.*?)(\(\d+\))?$")


def _split_number(stem: str):
    """'IMG_1234(1)' -> ('IMG_1234', '(1)').  No number -> ('IMG_1234', '')."""
    m = _NUM_RE.match(stem)
    return m.group(1), (m.group(2) or "")


def is_edited_name(filename: str) -> bool:
    base, _ = _split_number(os.path.splitext(filename)[0])
    return base.lower().endswith("-edited")


def find_sidecar(media_name: str, json_names: Dict[str, str]) -> Optional[str]:
    """Return the sidecar file name for `media_name`, or None.

    `json_names` maps lower-case json file names in the same folder to their
    real names (album metadata.json already left out).
    """
    stem, ext = os.path.splitext(media_name)
    base, num = _split_number(stem)

    # Names to try, best first: the file's own name, then the original
    # photo's name if this is an "-edited" copy.
    fulls = [media_name.lower()]
    plain_base = base
    if base.lower().endswith("-edited"):
        plain_base = base[: -len("-edited")]
    plain_full = (plain_base + ext).lower()
    if plain_full not in fulls:
        fulls.append(plain_full)
    # IMG_1234(1).jpg -> IMG_1234.jpg
    unnumbered = (base + ext).lower()
    if unnumbered not in fulls:
        fulls.append(unnumbered)

    # 1. Exact names.  First the media's own name...
    own = media_name.lower()
    tries = [own + ".json", own + "." + SUPPLEMENTAL + ".json"]
    # ...then the original photo's name.  For a numbered copy Google puts the
    # number after the media name: IMG_1234(1).jpg -> IMG_1234.jpg(1).json
    # (we never fall back to the un-numbered json: that belongs to another photo).
    for full in fulls[1:]:
        tries += [full + num + ".json", full + "." + SUPPLEMENTAL + num + ".json"]
    for t in tries:
        if t in json_names:
            return json_names[t]

    # 2. Truncated names.  Google cuts the whole json name to about 46
    #    characters, so the json name can be a prefix of
    #    "<media name>.supplemental-metadata" (or of the media name itself).
    best = None
    best_len = -1
    for lower, real in json_names.items():
        core, jnum = _split_number(lower[: -len(".json")])
        if jnum != num:      # the number in the json name must match the media's
            continue
        for full in fulls:
            target = full + "." + SUPPLEMENTAL
            if not target.startswith(core):
                continue
            # Short prefixes are too risky, unless it clearly starts with
            # "<media name>." (a truncated supplemental-metadata suffix).
            if core.startswith(full + ".") or len(core) >= 20:
                if len(core) > best_len:
                    best, best_len = real, len(core)
    return best


def read_sidecar(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _valid_epoch(value) -> Optional[int]:
    """Turn a JSON timestamp (usually a string) into a sane epoch, or None."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    # Between 1971 and 2100.  0 or tiny values mean "no date".
    if 31536000 <= n <= 4102444800:
        return n
    return None


def json_taken_time(data: dict) -> Optional[int]:
    """photoTakenTime, else creationTime, as epoch seconds (or None)."""
    for key in ("photoTakenTime", "creationTime"):
        block = data.get(key)
        if isinstance(block, dict):
            n = _valid_epoch(block.get("timestamp"))
            if n is not None:
                return n
    return None


def json_geo(data: dict):
    """(lat, lon) from geoData (or geoDataExif), ignoring 0,0."""
    for key in ("geoData", "geoDataExif"):
        geo = data.get(key)
        if not isinstance(geo, dict):
            continue
        try:
            lat = float(geo.get("latitude", 0))
            lon = float(geo.get("longitude", 0))
        except (TypeError, ValueError):
            continue
        if (lat or lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


# --------------------------------------------------------------------------
# Dates from EXIF and file names
# --------------------------------------------------------------------------

def _to_epoch(y, mo, d, h=0, mi=0, s=0) -> Optional[int]:
    try:
        stamp = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    return _valid_epoch(calendar.timegm(stamp.timetuple()))


def exif_taken_time(path: str) -> Optional[int]:
    """DateTimeOriginal from a JPEG, or None (also None if piexif is missing)."""
    if piexif is None or os.path.splitext(path)[1].lower() not in JPEG_EXTENSIONS:
        return None
    try:
        exif = piexif.load(path)
        raw = exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        if not raw:
            return None
        text = raw.decode("ascii", "ignore").strip("\x00 ")
        m = re.match(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", text)
        return _to_epoch(*m.groups()) if m else None
    except Exception:
        return None


# Each pattern captures year, month, day and optionally hour, minute, second.
_FILENAME_PATTERNS = [
    # IMG_20190714_153012, PXL_20190714_153012345, VID_20190714_153012
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"),
    # Screenshot_2019-07-14-15-30-12, 2019-07-14 15.30.12, 2019-07-14_15-30-12
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[ _T-](\d{2})[.\-:](\d{2})[.\-:](\d{2})"),
    # IMG-20190714-WA0001 (WhatsApp): date only
    re.compile(r"(?:IMG|VID)-(\d{4})(\d{2})(\d{2})-WA\d+", re.IGNORECASE),
]


def filename_taken_time(filename: str) -> Optional[int]:
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(filename)
        if m:
            epoch = _to_epoch(*m.groups())
            if epoch is not None:
                return epoch
    return None


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _album_name(export_dir: str, folder: str) -> Optional[str]:
    """Album folder name, or None for 'Photos from YYYY' (and the root)."""
    rel = os.path.relpath(folder, export_dir)
    if rel == ".":
        return None
    name = os.path.basename(folder)
    if name.lower() == "google photos" or re.fullmatch(r"Photos from \d{4}", name, re.IGNORECASE):
        return None
    return name


def _is_media(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS


def _find_media(export_dir: str):
    """List of (folder, filename, json_names_for_folder), sorted for stable results."""
    found = []
    for folder, dirs, files in os.walk(export_dir):
        dirs.sort(key=str.lower)
        json_names = {
            f.lower(): f
            for f in files
            if f.lower().endswith(".json") and f.lower() != "metadata.json"
        }
        for name in sorted(files, key=str.lower):
            if _is_media(name):
                found.append((folder, name, json_names))
    return found


@dataclass
class _Candidate:
    path: str
    rel: str
    sha: str
    album: Optional[str]
    json_path: Optional[str]
    data: Optional[dict]
    taken: Optional[int]
    date_source: str
    geo: Optional[tuple]
    edited: bool


def _describe(folder, name, json_names, export_dir, sha) -> _Candidate:
    path = os.path.join(folder, name)
    sidecar = find_sidecar(name, json_names)
    json_path = os.path.join(folder, sidecar) if sidecar else None
    data = read_sidecar(json_path) if json_path else None

    taken, source = None, SRC_UNKNOWN
    if data:
        taken = json_taken_time(data)
        if taken is not None:
            source = SRC_JSON
    if taken is None:
        taken = exif_taken_time(path)
        if taken is not None:
            source = SRC_EXIF
    if taken is None:
        taken = filename_taken_time(name)
        if taken is not None:
            source = SRC_FILENAME

    return _Candidate(
        path=path,
        rel=os.path.relpath(path, export_dir),
        sha=sha,
        album=_album_name(export_dir, folder),
        json_path=json_path,
        data=data,
        taken=taken,
        date_source=source,
        geo=json_geo(data) if data else None,
        edited=is_edited_name(name),
    )


def _pick_representative(group: List[_Candidate]) -> _Candidate:
    """Which copy of a duplicate group do we keep?  Best date info first,
    then a copy from a 'Photos from YYYY' folder, then the shortest path."""
    def rank(c: _Candidate):
        return (
            c.date_source != SRC_JSON,
            c.date_source == SRC_UNKNOWN,
            c.album is not None,
            len(c.rel),
            c.rel.lower(),
        )
    return min(group, key=rank)


def scan(export_dir: str, progress_cb: Optional[Callable[[int, int, str], None]] = None) -> Plan:
    """Look through an unzipped Takeout folder.  Never writes anything."""
    export_dir = os.path.abspath(export_dir)
    if not os.path.isdir(export_dir):
        raise TakeoutError("That folder does not exist: " + export_dir)

    media = _find_media(export_dir)
    candidates: List[_Candidate] = []
    for i, (folder, name, json_names) in enumerate(media, 1):
        sha = _sha256(os.path.join(folder, name))
        candidates.append(_describe(folder, name, json_names, export_dir, sha))
        if progress_cb:
            progress_cb(i, len(media), name)

    # Group identical files.
    groups: Dict[str, List[_Candidate]] = {}
    for c in candidates:
        groups.setdefault(c.sha, []).append(c)

    items: List[MediaItem] = []
    for group in groups.values():
        rep = _pick_representative(group)
        albums = sorted({c.album for c in group if c.album}, key=str.lower)
        # Fill gaps from the other copies (e.g. an album copy with the location).
        geo = rep.geo or next((c.geo for c in group if c.geo), None)
        description = ""
        for c in [rep] + group:
            if c.data and str(c.data.get("description") or "").strip():
                description = str(c.data["description"]).strip()
                break
        items.append(MediaItem(
            source=rep.path,
            rel_source=rep.rel,
            sha256=rep.sha,
            taken=rep.taken,
            date_source=rep.date_source,
            lat=geo[0] if geo else None,
            lon=geo[1] if geo else None,
            description=description,
            albums=albums,
            edited=rep.edited,
            json_path=rep.json_path,
            duplicates=sorted(c.rel for c in group if c is not rep),
        ))
    # Stable order matters: it decides which same-named file gets " (2)".
    items.sort(key=lambda it: it.rel_source.lower())

    summary = {
        "total_media": len(candidates),
        "unique": len(items),
        "duplicates_removed": len(candidates) - len(items),
        "dates_json": sum(1 for it in items if it.date_source == SRC_JSON),
        "dates_exif": sum(1 for it in items if it.date_source == SRC_EXIF),
        "dates_filename": sum(1 for it in items if it.date_source == SRC_FILENAME),
        "dates_unknown": sum(1 for it in items if it.date_source == SRC_UNKNOWN),
        "edited": sum(1 for c in candidates if c.edited),
        "with_location": sum(1 for it in items if it.lat is not None),
    }
    return Plan(export_dir, items, len(candidates), summary, piexif is not None)


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def format_summary(s: Dict[str, int], piexif_ok: bool) -> List[str]:
    """Lines of text for the command line."""
    lines = [
        "Total photos/videos found : %d" % s["total_media"],
        "Unique files              : %d" % s["unique"],
        "Duplicates removed        : %d" % s["duplicates_removed"],
        "Dates from Google's JSON  : %d" % s["dates_json"],
        "Dates from EXIF           : %d" % s["dates_exif"],
        "Dates from file name      : %d" % s["dates_filename"],
        "Date unknown              : %d" % s["dates_unknown"],
        "Edited copies             : %d" % s["edited"],
        "Files with location       : %d" % s["with_location"],
    ]
    if not piexif_ok:
        lines.append("Note: piexif is not installed, so dates and locations will not be "
                     "written inside the JPEG files (file dates are still fixed). "
                     "Install it with: pip install piexif")
    return lines


def plain_summary(s: Dict[str, int], piexif_ok: bool = True) -> str:
    """A friendly sentence or two for the GUI."""
    if s["total_media"] == 0:
        return "I could not find any photos or videos in that folder."
    parts = []
    if s["dates_json"]:
        parts.append("%d from Google's notes" % s["dates_json"])
    if s["dates_exif"]:
        parts.append("%d from the photo itself" % s["dates_exif"])
    if s["dates_filename"]:
        parts.append("%d from the file name" % s["dates_filename"])
    if s["dates_unknown"]:
        parts.append("%d unknown" % s["dates_unknown"])
    text = "Found %s." % _plural(s["total_media"], "photo")
    if parts:
        text += " Dates fixed: " + ", ".join(parts) + "."
    dups = s["duplicates_removed"]
    if dups:
        text += " %d duplicate %s will be skipped." % (dups, "copy" if dups == 1 else "copies")
    if s["with_location"]:
        text += " %d with a location." % s["with_location"]
    if not piexif_ok:
        text += (" (The piexif add-on is missing, so dates are fixed on the files "
                 "but not written inside the photos.)")
    return text


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def _inside(child: str, parent: str) -> bool:
    child = os.path.normcase(os.path.realpath(child))
    parent = os.path.normcase(os.path.realpath(parent))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


def _dest_folder(item: MediaItem) -> str:
    if item.taken is None:
        return UNKNOWN_FOLDER
    when = dt.datetime.fromtimestamp(item.taken, dt.timezone.utc)
    return os.path.join("%04d" % when.year, "%02d" % when.month)


def _unique_name(folder_abs: str, name: str, used: set) -> str:
    """name, or 'name (2)', 'name (3)'... if it is already taken."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while os.path.normcase(os.path.join(folder_abs, candidate)) in used or \
            os.path.exists(os.path.join(folder_abs, candidate)):
        n += 1
        candidate = "%s (%d)" % (stem, n)  + ext
    used.add(os.path.normcase(os.path.join(folder_abs, candidate)))
    return candidate


def _dms(value: float):
    value = abs(value)
    deg = int(value)
    minutes = int((value - deg) * 60)
    seconds = (value - deg - minutes / 60.0) * 3600
    return ((deg, 1), (minutes, 1), (int(round(seconds * 10000)), 10000))


def _write_exif(path: str, item: MediaItem) -> bool:
    """Add date (and GPS) to a JPEG copy if missing.  True if the file changed."""
    if piexif is None or os.path.splitext(path)[1].lower() not in JPEG_EXTENSIONS:
        return False
    exif = piexif.load(path)
    changed = False

    has_date = bool(exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal))
    if item.taken is not None and not has_date:
        stamp = dt.datetime.fromtimestamp(item.taken, dt.timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
        raw = stamp.encode("ascii")
        exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = raw
        exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = raw
        exif["0th"][piexif.ImageIFD.DateTime] = raw
        changed = True

    has_gps = bool(exif.get("GPS", {}).get(piexif.GPSIFD.GPSLatitude))
    if item.lat is not None and item.lon is not None and not has_gps:
        exif["GPS"][piexif.GPSIFD.GPSVersionID] = (2, 3, 0, 0)
        exif["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if item.lat >= 0 else b"S"
        exif["GPS"][piexif.GPSIFD.GPSLatitude] = _dms(item.lat)
        exif["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if item.lon >= 0 else b"W"
        exif["GPS"][piexif.GPSIFD.GPSLongitude] = _dms(item.lon)
        changed = True

    if changed:
        piexif.insert(piexif.dump(exif), path)
    return changed


def apply(plan: Plan, out_dir: str,
          progress_cb: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, int]:
    """Copy the unique files into out_dir/YYYY/MM.  Never touches the input.

    Returns {"copied": n, "errors": n, "exif_written": n}.
    """
    out_dir = os.path.abspath(out_dir)
    if _inside(out_dir, plan.export_dir):
        raise TakeoutError("The save folder is inside your Google Photos download folder. "
                           "Please choose a different place to save the clean photos.")

    # Keep earlier manifest entries so a second run can still be undone fully.
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    manifest = {"files": [], "dirs": []}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                old = json.load(f)
            manifest["files"] = list(old.get("files", []))
            manifest["dirs"] = list(old.get("dirs", []))
        except (OSError, ValueError):
            pass

    def make_dirs(path: str):
        """makedirs, remembering which folders we created (for undo)."""
        missing = []
        p = path
        while not os.path.isdir(p):
            missing.append(p)
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        os.makedirs(path, exist_ok=True)
        for m in reversed(missing):
            rel = os.path.relpath(m, out_dir)
            if rel != "." and rel not in manifest["dirs"]:
                manifest["dirs"].append(rel)

    make_dirs(out_dir)

    used: set = set()
    rows = []
    counts = {"copied": 0, "errors": 0, "exif_written": 0}
    total = len(plan.items)

    for i, item in enumerate(plan.items, 1):
        rel_folder = _dest_folder(item)
        folder_abs = os.path.join(out_dir, rel_folder)
        action = "copied"
        dest_rel = ""
        try:
            make_dirs(folder_abs)
            name = _unique_name(folder_abs, os.path.basename(item.source), used)
            dest = os.path.join(folder_abs, name)
            dest_rel = os.path.join(rel_folder, name)
            shutil.copy2(item.source, dest)       # copy2 keeps the old file date
            manifest["files"].append(dest_rel)    # recorded as soon as it exists
            counts["copied"] += 1
            try:
                if _write_exif(dest, item):
                    action = "copied + metadata written"
                    counts["exif_written"] += 1
            except Exception as e:                # bad EXIF should not stop the run
                action = "copied (could not write metadata: %s)" % e
            if item.taken is not None:
                os.utime(dest, (item.taken, item.taken))
        except Exception as e:
            counts["errors"] += 1
            action = "error: %s" % e
        taken_text = ""
        if item.taken is not None:
            taken_text = dt.datetime.fromtimestamp(item.taken, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows.append([
            item.rel_source, dest_rel, taken_text, item.date_source,
            "; ".join(item.albums), action,
            "" if item.lat is None else item.lat,
            "" if item.lon is None else item.lon,
            item.description,
        ])
        if progress_cb:
            progress_cb(i, total, os.path.basename(item.source))

    report_path = os.path.join(out_dir, REPORT_NAME)
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["source", "destination", "taken_time", "date_source", "albums",
                    "action", "latitude", "longitude", "description"])
        w.writerows(rows)
    if REPORT_NAME not in manifest["files"]:
        manifest["files"].append(REPORT_NAME)

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return counts


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

def undo(out_dir: str) -> int:
    """Delete only the files listed in the manifest (and folders we made, if
    they are now empty).  Returns the number of files removed."""
    out_dir = os.path.abspath(out_dir)
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise TakeoutError("No Takeout Rescue manifest found in " + out_dir)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    removed = 0
    for rel in manifest.get("files", []):
        path = os.path.join(out_dir, rel)
        if not _inside(path, out_dir):      # never delete outside the output folder
            continue
        if os.path.isfile(path):
            os.remove(path)
            removed += 1
    os.remove(manifest_path)

    # Remove folders we created, deepest first, only if empty.
    for rel in sorted(manifest.get("dirs", []), key=lambda r: r.count(os.sep), reverse=True):
        path = os.path.join(out_dir, rel)
        if _inside(path, out_dir) and os.path.isdir(path):
            try:
                os.rmdir(path)
            except OSError:
                pass
    return removed
