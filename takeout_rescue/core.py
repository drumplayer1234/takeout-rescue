"""Takeout Rescue core logic (no GUI code in here).

Typical use:
    plan = scan("C:/Takeout")
    print(plan.summary)
    apply(plan, "C:/CleanPhotos", progress_cb=print)

Time zones:
* Dates in Google's JSON files are real moments in time (UTC).  They are
  converted to the computer's local time, so a photo taken at 23:30 on
  December 31 lands in the December folder.
* Dates that come from EXIF or from a file name are "wall clock" times with no
  time zone.  They are used exactly as written.
"""

import bisect
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# piexif is optional.  Set TAKEOUT_RESCUE_NO_PIEXIF=1 to pretend it is missing.
try:
    if os.environ.get("TAKEOUT_RESCUE_NO_PIEXIF"):
        raise ImportError("piexif disabled by environment variable")
    import piexif
except ImportError:
    piexif = None

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp", ".heic", ".heif", ".avif",
    ".tif", ".tiff", ".bmp",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
    ".mp4", ".mov", ".mp", ".avi", ".3gp", ".mpg", ".mpeg", ".m4v", ".mkv", ".webm",
    ".mts", ".m2ts", ".wmv", ".flv",
}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}

REPORT_NAME = "takeout_rescue_report.csv"
MANIFEST_NAME = ".takeout_rescue_manifest.json"
UNKNOWN_FOLDER = "Unknown date"

SUPPLEMENTAL = "supplemental-metadata"

# "-edited" in the languages Google Photos uses.
EDITED_SUFFIXES = ["-edited", "-bearbeitet", "-modifié", "-modificato", "-editado",
                   "-bewerkt", "-redigerad", "-muokattu", "-edytowane"]

# Date sources, in priority order.
SRC_JSON = "json"
SRC_EXIF = "exif"
SRC_FILENAME = "filename"
SRC_UPLOAD = "upload_date"      # JSON creationTime: when it was uploaded, not taken
SRC_UNKNOWN = "unknown"

# Only try to match a cut-off json name when the media name is this long.
LONG_NAME = 40
MIN_PREFIX = 20
MANIFEST_FLUSH_EVERY = 25
MAX_LISTED = 20                 # names listed in the text summary


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
    taken: Optional[dt.datetime] = None   # local wall-clock time (no tzinfo), or None
    mtime: Optional[float] = None    # epoch seconds for the file date, or None
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
    unknown_files: List[str] = field(default_factory=list)   # not photos/videos, not copied
    unreadable: List[str] = field(default_factory=list)      # files/folders we could not read

    def summary_lines(self) -> List[str]:
        return format_summary(self.summary, self.piexif_available,
                              self.unknown_files, self.unreadable)

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


# Each suffix in both its composed and decomposed (macOS) spelling.
_EDITED_FORMS = sorted({form for s in EDITED_SUFFIXES
                        for form in (s, unicodedata.normalize("NFD", s))}, key=len, reverse=True)


def _strip_edited(base: str):
    """'IMG_1234-bearbeitet' -> ('IMG_1234', True).  Otherwise (base, False)."""
    low = base.lower()
    for form in _EDITED_FORMS:
        if low.endswith(form):
            return base[: -len(form)], True
    return base, False


def is_edited_name(filename: str) -> bool:
    base, _ = _split_number(os.path.splitext(filename)[0])
    return _strip_edited(base)[1]


def _name_variants(media_name: str):
    """([lower-case full names to try, best first], '(1)' style number)."""
    stem, ext = os.path.splitext(media_name)
    base, num = _split_number(stem)
    plain_base, _ = _strip_edited(base)
    fulls = [media_name.lower()]
    for extra in ((plain_base + ext).lower(),      # the original of an edited copy
                  (base + ext).lower()):           # IMG_1234(1).jpg -> IMG_1234.jpg
        if extra not in fulls:
            fulls.append(extra)
    return fulls, num


def _exact_sidecar(media_name: str, json_names: Dict[str, str]) -> Optional[str]:
    fulls, num = _name_variants(media_name)
    # The media's own name first...
    own = fulls[0]
    tries = [own + ".json", own + "." + SUPPLEMENTAL + ".json"]
    # ...then the original photo's name.  For a numbered copy Google puts the
    # number after the media name: IMG_1234(1).jpg -> IMG_1234.jpg(1).json
    # (we never fall back to the un-numbered json: that belongs to another photo).
    for full in fulls[1:]:
        tries += [full + num + ".json", full + "." + SUPPLEMENTAL + num + ".json"]
    for t in tries:
        if t in json_names:
            return json_names[t]
    return None


def _build_index(json_names: Dict[str, str]):
    """(sorted json names without '.json' and number, {that name: [(number, real name)]})."""
    cores: Dict[str, list] = {}
    for lower, real in json_names.items():
        if lower.endswith(".json"):
            core, jnum = _split_number(lower[: -len(".json")])
            cores.setdefault(core, []).append((jnum, real))
    return sorted(cores), cores


def find_sidecar(media_name: str, json_names: Dict[str, str], title_of=None,
                 claimed: Optional[Dict[str, str]] = None, index=None) -> Optional[str]:
    """Return the sidecar file name for `media_name`, or None.

    `json_names` maps lower-case json file names in the same folder to their
    real names (album metadata.json already left out).

    For cut-off (prefix) matches two extra rules apply:
    * `title_of(json_name)` gives the "title" inside that json (or None).  When
      there is a title it must be this media file's name (or the original's name).
    * `claimed` maps json names to the media file that already uses them.  A json
      is claimed by only one non-edited media file.
    """
    real = _exact_sidecar(media_name, json_names)
    if real:
        return real

    fulls, num = _name_variants(media_name)
    keys, cores = index if index is not None else _build_index(json_names)

    # Google cuts the whole json name to about 46 characters, so the json name
    # can be a prefix of "<media name>.supplemental-metadata".
    found = set()
    for full in fulls:
        target = full + "." + SUPPLEMENTAL
        # The suffix was cut: "<media name>.supplemental-me.json".  Safe at any length.
        start = full + "."
        i = bisect.bisect_left(keys, start)
        while i < len(keys) and keys[i].startswith(start):
            if target.startswith(keys[i]):
                found.add(keys[i])
            i += 1
        # The media name itself was cut.  Only a long name can have been cut.
        if len(media_name) >= LONG_NAME:
            for n in range(MIN_PREFIX, len(target) + 1):
                if target[:n] in cores:
                    found.add(target[:n])

    edited = is_edited_name(media_name)
    for core in sorted(found, key=len, reverse=True):      # longest match first
        for jnum, real in cores[core]:
            if jnum != num:      # the number in the json name must match the media's
                continue
            # An edited copy may share its original's json; others may not.
            if claimed is not None and not edited and claimed.get(real, media_name) != media_name:
                continue
            if title_of is not None:
                title = title_of(real)
                if title is not None and title.lower() not in fulls:
                    continue
            if claimed is not None and not edited:
                claimed[real] = media_name
            return real
    return None


def read_sidecar(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def match_sidecars(folder: str, media_names: List[str], json_names: Dict[str, str],
                   cache: Optional[dict] = None) -> Dict[str, str]:
    """{media name: json name} for one folder.  `cache` fills with {json name: data}."""
    cache = {} if cache is None else cache
    index = _build_index(json_names)

    def data_of(real):
        if real not in cache:
            cache[real] = read_sidecar(os.path.join(folder, real))
        return cache[real]

    def title_of(real):
        data = data_of(real)
        title = data.get("title") if data else None
        return title if isinstance(title, str) and title.strip() else None

    claimed: Dict[str, str] = {}
    result: Dict[str, str] = {}
    # Exact names first, so a cut-off match can never steal them.
    for name in media_names:
        real = _exact_sidecar(name, json_names)
        if real:
            result[name] = real
            if not is_edited_name(name):
                claimed.setdefault(real, name)
    for name in media_names:
        if name not in result:
            real = find_sidecar(name, json_names, title_of, claimed, index)
            if real:
                result[name] = real
    for real in set(result.values()):       # read the ones not read for a title
        data_of(real)
    return result


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

_UTC = dt.timezone.utc
_EPOCH0 = dt.datetime(1970, 1, 1, tzinfo=_UTC)
_MIN_YEAR = 1826                  # the first photograph was taken around then
_MIN_EPOCH = int((dt.datetime(_MIN_YEAR, 1, 1, tzinfo=_UTC) - _EPOCH0).total_seconds())


def _parse_epoch(value) -> Optional[int]:
    """Turn a JSON timestamp (usually a string) into epoch seconds, or None.

    Missing values and "0" (Google's "no date") are None.  Dates before 1970 are
    fine; anything before 1826 or after tomorrow is treated as garbage.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):      # "abc", "nan", "1e999"
        return None
    if n == 0:
        return None
    if _MIN_EPOCH <= n <= int(time.time()) + 86400:
        return n
    return None


def _json_time(data: dict, key: str) -> Optional[int]:
    block = data.get(key)
    if isinstance(block, dict):
        return _parse_epoch(block.get("timestamp"))
    return None


def json_taken_time(data: dict) -> Optional[int]:
    """photoTakenTime as epoch seconds (or None).  creationTime is NOT used here."""
    return _json_time(data, "photoTakenTime")


def json_creation_time(data: dict) -> Optional[int]:
    """creationTime (the upload date) as epoch seconds, or None."""
    return _json_time(data, "creationTime")


def _local_from_epoch(n: int) -> dt.datetime:
    """Local wall-clock time (no tzinfo) for a real moment in time."""
    utc = _EPOCH0 + dt.timedelta(seconds=n)     # fromtimestamp() fails for negatives on Windows
    try:
        return utc.astimezone().replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        # Windows has no local time before 1970: use the offset the computer has at 1970.
        try:
            offset = (_EPOCH0 + dt.timedelta(days=1)).astimezone().utcoffset()
            return (utc + offset).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return utc.replace(tzinfo=None)


def _naive_mtime(naive: dt.datetime) -> Optional[float]:
    """File date for a wall-clock time, or None when this computer cannot do it."""
    try:
        return time.mktime(naive.timetuple())
    except (OverflowError, ValueError, OSError):
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
# Dates from EXIF and file names (wall-clock times)
# --------------------------------------------------------------------------

def _naive_date(y, mo, d, h=0, mi=0, s=0) -> Optional[dt.datetime]:
    try:
        stamp = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    if stamp.year < _MIN_YEAR or stamp > dt.datetime.now() + dt.timedelta(days=1):
        return None
    return stamp


def exif_taken_time(path: str) -> Optional[dt.datetime]:
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
        return _naive_date(*m.groups()) if m else None
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


def filename_taken_time(filename: str) -> Optional[dt.datetime]:
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(filename)
        if m:
            stamp = _naive_date(*m.groups())
            if stamp is not None:
                return stamp
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
    """(folders, unknown files, unreadable folders), sorted for stable results.

    folders is a list of (folder, media names, json_names_for_folder).
    unknown files are neither photos/videos nor json: they will not be copied.
    """
    folders, unknown, unreadable = [], [], []

    def onerror(err):
        unreadable.append(os.path.relpath(err.filename or export_dir, export_dir) + os.sep)

    for folder, dirs, files in os.walk(export_dir, onerror=onerror):
        dirs.sort(key=str.lower)
        json_names = {
            f.lower(): f
            for f in files
            if f.lower().endswith(".json") and f.lower() != "metadata.json"
        }
        names = []
        for name in sorted(files, key=str.lower):
            if _is_media(name):
                names.append(name)
            elif not name.lower().endswith(".json"):
                unknown.append(os.path.relpath(os.path.join(folder, name), export_dir))
        if names:
            folders.append((folder, names, json_names))
    return folders, unknown, unreadable


@dataclass
class _Candidate:
    path: str
    rel: str
    sha: str
    album: Optional[str]
    json_path: Optional[str]
    data: Optional[dict]
    taken: Optional[dt.datetime]
    mtime: Optional[float]
    date_source: str
    geo: Optional[tuple]
    edited: bool


def _describe(folder, name, sidecar, export_dir, sha, cache) -> _Candidate:
    path = os.path.join(folder, name)
    json_path = os.path.join(folder, sidecar) if sidecar else None
    data = cache.get(sidecar) if sidecar else None

    # Best date first: photoTakenTime, EXIF, file name, and only then the upload date.
    taken, mtime, source = None, None, SRC_UNKNOWN
    epoch = json_taken_time(data) if data else None
    if epoch is not None:
        taken, mtime, source = _local_from_epoch(epoch), float(epoch), SRC_JSON
    if taken is None:
        taken = exif_taken_time(path)
        if taken is not None:
            source = SRC_EXIF
    if taken is None:
        taken = filename_taken_time(name)
        if taken is not None:
            source = SRC_FILENAME
    if taken is not None and mtime is None:
        mtime = _naive_mtime(taken)
    if taken is None and data:
        epoch = json_creation_time(data)
        if epoch is not None:
            taken, mtime, source = _local_from_epoch(epoch), float(epoch), SRC_UPLOAD

    return _Candidate(
        path=path,
        rel=os.path.relpath(path, export_dir),
        sha=sha,
        album=_album_name(export_dir, folder),
        json_path=json_path,
        data=data,
        taken=taken,
        mtime=mtime,
        date_source=source,
        geo=json_geo(data) if data else None,
        edited=is_edited_name(name),
    )


_DATE_RANK = {SRC_JSON: 0, SRC_EXIF: 1, SRC_FILENAME: 1, SRC_UPLOAD: 2, SRC_UNKNOWN: 3}


def _pick_representative(group: List[_Candidate]) -> _Candidate:
    """Which copy of a duplicate group do we keep?  Best date info first (a real
    taken date beats the upload date), then a copy from a 'Photos from YYYY'
    folder, then the shortest path."""
    def rank(c: _Candidate):
        return (
            _DATE_RANK[c.date_source],
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

    folders, unknown_files, unreadable = _find_media(export_dir)
    total = sum(len(names) for _, names, _ in folders)
    candidates: List[_Candidate] = []
    done = 0
    for folder, names, json_names in folders:
        cache: Dict[str, Optional[dict]] = {}
        sidecars = match_sidecars(folder, names, json_names, cache)
        for name in names:
            done += 1
            path = os.path.join(folder, name)
            try:
                sha = _sha256(path)
                candidates.append(_describe(folder, name, sidecars.get(name), export_dir, sha, cache))
            except OSError:
                unreadable.append(os.path.relpath(path, export_dir))
            if progress_cb:
                progress_cb(done, total, name)

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
            mtime=rep.mtime,
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
    unknown_files.sort(key=str.lower)
    unreadable.sort(key=str.lower)

    summary = {
        "total_media": len(candidates),
        "unique": len(items),
        "duplicates_removed": len(candidates) - len(items),
        "dates_json": sum(1 for it in items if it.date_source == SRC_JSON),
        "dates_exif": sum(1 for it in items if it.date_source == SRC_EXIF),
        "dates_filename": sum(1 for it in items if it.date_source == SRC_FILENAME),
        "dates_upload": sum(1 for it in items if it.date_source == SRC_UPLOAD),
        "dates_unknown": sum(1 for it in items if it.date_source == SRC_UNKNOWN),
        "edited": sum(1 for c in candidates if c.edited),
        "with_location": sum(1 for it in items if it.lat is not None),
        "not_copied_unknown": len(unknown_files),
        "could_not_read": len(unreadable),
    }
    return Plan(export_dir, items, len(candidates), summary, piexif is not None,
                unknown_files, unreadable)


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _list_names(title: str, names) -> List[str]:
    lines = [title]
    lines += ["  " + n for n in names[:MAX_LISTED]]
    if len(names) > MAX_LISTED:
        lines.append("  ... and %d more (the report lists all of them)" % (len(names) - MAX_LISTED))
    return lines


def format_summary(s: Dict[str, int], piexif_ok: bool, unknown_files=(), unreadable=()) -> List[str]:
    """Lines of text for the command line."""
    lines = [
        "Total photos/videos found      : %d" % s["total_media"],
        "Unique files                   : %d" % s["unique"],
        "Duplicates removed             : %d" % s["duplicates_removed"],
        "Dates from Google's JSON       : %d" % s["dates_json"],
        "Dates from EXIF                : %d" % s["dates_exif"],
        "Dates from file name           : %d" % s["dates_filename"],
        "Dates guessed from upload date : %d" % s["dates_upload"],
        "Date unknown                   : %d" % s["dates_unknown"],
        "Edited copies                  : %d" % s["edited"],
        "Files with location            : %d" % s["with_location"],
        "Not copied (unknown type)      : %d" % s["not_copied_unknown"],
        "Could not read                 : %d" % s["could_not_read"],
    ]
    if unknown_files:
        lines += _list_names("Not copied (unknown type):", list(unknown_files))
    if unreadable:
        lines += _list_names("Could not read:", list(unreadable))
    if not piexif_ok:
        lines.append("Note: piexif is not installed, so dates and locations will not be "
                     "written inside the JPEG files (file dates are still fixed). "
                     "Install it with: pip install piexif")
    return lines


def plain_summary(s: Dict[str, int], piexif_ok: bool = True) -> str:
    """A friendly sentence or two for the GUI."""
    if s["total_media"] == 0:
        text = "I could not find any photos or videos in that folder."
    else:
        parts = []
        if s["dates_json"]:
            parts.append("%d from Google's notes" % s["dates_json"])
        if s["dates_exif"]:
            parts.append("%d from the photo itself" % s["dates_exif"])
        if s["dates_filename"]:
            parts.append("%d from the file name" % s["dates_filename"])
        if s["dates_upload"]:
            parts.append("%d guessed from the upload date" % s["dates_upload"])
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
    if s["not_copied_unknown"]:
        text += " %s will not be copied because I do not know the type (see the report)." % \
            _plural(s["not_copied_unknown"], "file")
    if s["could_not_read"]:
        text += " %s could not be read." % _plural(s["could_not_read"], "file or folder")
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
    return os.path.join("%04d" % item.taken.year, "%02d" % item.taken.month)


def _unsafe_rel(rel: str) -> bool:
    """True for paths that are absolute or climb out of a folder with '..'."""
    return os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/")


def _dms(value: float):
    value = abs(value)
    deg = int(value)
    minutes = int((value - deg) * 60)
    seconds = int(round((value - deg - minutes / 60.0) * 3600 * 10000))
    if seconds >= 60 * 10000:       # 59.99999 seconds rounds up to a whole minute
        seconds -= 60 * 10000
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        deg += 1
    return ((deg, 1), (minutes, 1), (seconds, 10000))


def _write_exif(path: str, item: MediaItem) -> bool:
    """Add date (and GPS) to a JPEG copy if missing.  True if the file changed."""
    if piexif is None or os.path.splitext(path)[1].lower() not in JPEG_EXTENSIONS:
        return False
    exif = piexif.load(path)
    changed = False

    has_date = bool(exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal))
    if item.taken is not None and not has_date:
        raw = item.taken.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
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


def _csv_cell(value):
    """Stop Excel from running a cell that starts with = + - @ as a formula."""
    if isinstance(value, str) and value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def _load_manifest(path: str) -> dict:
    """The manifest as {"files": [entry dicts], "dirs": [...]}.

    A missing manifest is an empty one.  A damaged one is an error: we must not
    replace it, or the files it lists could never be undone.
    """
    manifest = {"files": [], "dirs": []}
    if not os.path.lexists(path):
        return manifest
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
        if not isinstance(old, dict):
            raise ValueError("not a list of files")
        for entry in old.get("files", []):
            if isinstance(entry, str):          # first version: just paths
                entry = {"path": entry}
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("a file entry is not understood")
            manifest["files"].append(entry)
        for rel in old.get("dirs", []):
            if not isinstance(rel, str):
                raise ValueError("a folder entry is not understood")
            if not _unsafe_rel(rel):
                manifest["dirs"].append(rel)
    except (OSError, ValueError, AttributeError, TypeError) as e:
        raise TakeoutError("The list of files from an earlier run (%s) is damaged or cannot be "
                           "read (%s). Nothing was changed. Please fix or move that file "
                           "and try again." % (path, e))
    return manifest


def _save_manifest(path: str, manifest: dict):
    """Write the manifest in one piece (a crash never leaves half a file)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(manifest, version=2), f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _same_file(dest: str, dest_rel: str, item: MediaItem, size: int, source_shas: dict) -> bool:
    """Is the file at `dest` this item, copied by an earlier run?"""
    if source_shas.get(os.path.normcase(dest_rel)) == item.sha256:    # the manifest says so
        return True
    try:
        return os.path.getsize(dest) == size and _sha256(dest) == item.sha256
    except OSError:
        return False


def _choose_destination(item: MediaItem, out_dir: str, rel_folder: str, used: set,
                        source_shas: dict):
    """("done", dest_rel) if this item is already in out_dir, else ("new", dest_rel).
    A new name is 'name', 'name (2)', 'name (3)'... whichever is free."""
    stem, ext = os.path.splitext(os.path.basename(item.source))
    size = os.path.getsize(item.source)
    n = 1
    while True:
        name = stem + ext if n == 1 else "%s (%d)%s" % (stem, n, ext)
        dest_rel = os.path.join(rel_folder, name)
        dest = os.path.join(out_dir, dest_rel)
        key = os.path.normcase(dest)
        if key not in used:
            if not os.path.lexists(dest):
                used.add(key)
                return "new", dest_rel
            if _same_file(dest, dest_rel, item, size, source_shas):
                used.add(key)
                return "done", dest_rel
        n += 1


def _report_name(out_dir: str, manifest_paths: set) -> str:
    """takeout_rescue_report.csv, or '... (2).csv' if that name belongs to someone else."""
    stem, ext = os.path.splitext(REPORT_NAME)
    n = 1
    while True:
        name = REPORT_NAME if n == 1 else "%s (%d)%s" % (stem, n, ext)
        if os.path.normcase(name) in manifest_paths or not os.path.lexists(os.path.join(out_dir, name)):
            return name
        n += 1


def _set_file_date(path: str, mtime: Optional[float]) -> bool:
    """Set the file date.  False if it could not be set (very old dates)."""
    if mtime is None:
        return False
    try:
        os.utime(path, (mtime, mtime))
        return True
    except (OSError, OverflowError, ValueError):
        return False


_WIN_EPOCH_OFFSET = 11644473600          # seconds from 1601-01-01 to 1970-01-01
_kernel32 = None


def _filetime_from_unix(secs: float) -> Optional[int]:
    """Windows FILETIME (100 ns ticks since 1601) for unix seconds, or None if not representable."""
    try:
        if not math.isfinite(secs):
            return None
        ticks = int((secs + _WIN_EPOCH_OFFSET) * 10_000_000)
    except (OverflowError, ValueError, TypeError):
        return None
    return ticks if 0 <= ticks < 2 ** 64 else None


def _set_creation_time(path: str, secs: Optional[float]) -> bool:
    """Set the Windows 'Created' time to match the file date.  No-op (True) elsewhere.
    False if it could not be set."""
    if os.name != "nt":
        return True
    if secs is None:
        return False
    ticks = _filetime_from_unix(secs)
    if ticks is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        global _kernel32
        if _kernel32 is None:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
            k.CreateFileW.restype = wintypes.HANDLE
            k.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                      ctypes.POINTER(wintypes.FILETIME),
                                      ctypes.POINTER(wintypes.FILETIME)]
            k.SetFileTime.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.CloseHandle.restype = wintypes.BOOL
            _kernel32 = k
        k = _kernel32
        invalid = ctypes.c_void_p(-1).value
        # FILE_WRITE_ATTRIBUTES; share read|write|delete; OPEN_EXISTING; FILE_FLAG_BACKUP_SEMANTICS
        handle = k.CreateFileW(path, 0x100, 0x7, None, 3, 0x02000000, None)
        if handle is None or handle == invalid:
            return False
        try:
            ft = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
            return bool(k.SetFileTime(handle, ctypes.byref(ft), None, None))
        finally:
            k.CloseHandle(handle)
    except Exception:
        return False


def apply(plan: Plan, out_dir: str,
          progress_cb: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Copy the unique files into out_dir/YYYY/MM.  Never touches the input.

    Safe to run again: files that are already in out_dir are skipped.

    Returns {"copied", "already_done", "errors", "exif_written", "file_date_not_set",
    "not_copied_unknown", "could_not_read", "report" (file name or None),
    "warnings" (list of text)}.
    """
    out_dir = os.path.abspath(out_dir)
    if _inside(out_dir, plan.export_dir):
        raise TakeoutError("The save folder is inside your Google Photos download folder. "
                           "Please choose a different place to save the clean photos.")

    # Keep earlier manifest entries so a second run can still be undone fully.
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    manifest = _load_manifest(manifest_path)      # stops here if it is damaged
    entries = {os.path.normcase(e["path"]): e for e in manifest["files"]}
    source_shas = {k: e["source_sha256"] for k, e in entries.items() if e.get("source_sha256")}

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
            try:
                rel = os.path.relpath(m, out_dir)
            except ValueError:      # different drive
                continue
            if rel != "." and not _unsafe_rel(rel) and os.path.isdir(m) \
                    and rel not in manifest["dirs"]:
                manifest["dirs"].append(rel)

    result = {"copied": 0, "already_done": 0, "errors": 0, "exif_written": 0,
              "file_date_not_set": 0, "not_copied_unknown": len(plan.unknown_files),
              "could_not_read": len(plan.unreadable), "report": None, "warnings": []}
    warnings = result["warnings"]

    make_dirs(out_dir)

    # Step 1: decide where every file goes, and write that down BEFORE copying,
    # so even a run that is interrupted can be undone.
    used: set = set()
    jobs = []       # [item, "new" | "done" | "error", dest_rel, manifest entry, error text]
    for item in plan.items:
        rel_folder = _dest_folder(item)
        try:
            status, dest_rel = _choose_destination(item, out_dir, rel_folder, used, source_shas)
            entry = None
            if status == "new":
                make_dirs(os.path.join(out_dir, rel_folder))
                entry = entries.get(os.path.normcase(dest_rel))
                if entry is None:
                    entry = {"path": dest_rel}
                    entries[os.path.normcase(dest_rel)] = entry
                    manifest["files"].append(entry)
                entry.update(source_sha256=item.sha256, size=None, sha256=None)
            jobs.append([item, status, dest_rel, entry, ""])
        except Exception as e:
            jobs.append([item, "error", "", None, str(e)])
    try:
        _save_manifest(manifest_path, manifest)
    except OSError as e:
        raise TakeoutError("Could not write the list of copied files in %s (%s). "
                           "Nothing was copied." % (out_dir, e))

    # Step 2: copy.
    rows = []
    total = len(jobs)
    try:
        for i, (item, status, dest_rel, entry, error) in enumerate(jobs, 1):
            action = "copied"
            if status == "error":
                result["errors"] += 1
                action = "error: %s" % error
            elif status == "done":
                result["already_done"] += 1
                action = "already done"
            else:
                dest = os.path.join(out_dir, dest_rel)
                try:
                    shutil.copy2(item.source, dest)       # copy2 keeps the old file date
                except Exception as e:
                    result["errors"] += 1
                    action = "error: %s" % e
                    try:
                        os.remove(dest)                   # half a copy is no use
                    except OSError:
                        pass
                else:
                    result["copied"] += 1
                    try:
                        os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)   # a read-only original
                    except OSError:
                        pass
                    try:
                        if _write_exif(dest, item):
                            action = "copied + metadata written"
                            result["exif_written"] += 1
                    except Exception as e:                # bad EXIF should not stop the run
                        action = "copied (could not write metadata: %s)" % e
                    try:
                        entry.update(size=os.path.getsize(dest), sha256=_sha256(dest))
                    except OSError:
                        pass
                    if item.taken is not None and not _set_file_date(dest, item.mtime):
                        result["file_date_not_set"] += 1
                        action += "; file date not set"
                    elif item.taken is not None and not _set_creation_time(dest, item.mtime):
                        action += "; created date not set"
            taken_text = item.taken.strftime("%Y-%m-%d %H:%M:%S") if item.taken else ""
            rows.append([
                item.rel_source, dest_rel if status != "error" else "", taken_text,
                item.date_source, "; ".join(item.albums), action,
                "" if item.lat is None else item.lat,
                "" if item.lon is None else item.lon,
                item.description,
            ])
            if i % MANIFEST_FLUSH_EVERY == 0:
                try:
                    _save_manifest(manifest_path, manifest)
                except OSError:
                    pass            # tried again below
            if progress_cb:
                progress_cb(i, total, os.path.basename(item.source))
    finally:
        try:
            _save_manifest(manifest_path, manifest)
        except OSError as e:
            warnings.append("Could not save the list of copied files (%s). "
                            "Undo may not find everything." % e)

    for rel in plan.unknown_files:
        rows.append([rel, "", "", "", "", "not copied (unknown type)", "", "", ""])
    for rel in plan.unreadable:
        rows.append([rel, "", "", "", "", "not copied (could not read)", "", "", ""])

    # Step 3: the report.  The manifest lists it first, so a locked or failed
    # report can never leave files that undo does not know about.
    report = _report_name(out_dir, {os.path.normcase(e["path"]) for e in manifest["files"]})
    if os.path.normcase(report) not in entries:
        manifest["files"].append({"path": report})
    try:
        _save_manifest(manifest_path, manifest)
    except OSError as e:
        warnings.append("Could not save the list of copied files (%s). "
                        "Undo may not find everything." % e)
    try:
        with open(os.path.join(out_dir, report), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["source", "destination", "taken_time", "date_source", "albums",
                        "action", "latitude", "longitude", "description"])
            w.writerows([_csv_cell(cell) for cell in row] for row in rows)
        result["report"] = report
    except OSError as e:
        warnings.append("Could not write the report file %s (is it open in another program?): %s. "
                        "Your photos were copied anyway." % (report, e))
    if result["file_date_not_set"]:
        warnings.append("%s could not get their old file date (dates before 1970 or too old for "
                        "this computer). The photos are in the right folders; see the report."
                        % _plural(result["file_date_not_set"], "file"))
    return result


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

def undo(out_dir: str, problems: Optional[List[str]] = None) -> int:
    """Delete only the files listed in the manifest (and folders we made, if
    they are now empty).  Returns the number of files removed.

    A file that changed since apply made it is left alone.  Problems (left-alone
    files, files that could not be removed) are added to the `problems` list.
    """
    problems = [] if problems is None else problems
    out_dir = os.path.abspath(out_dir)
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise TakeoutError("No Takeout Rescue manifest found in " + out_dir)
    manifest = _load_manifest(manifest_path)
    own_manifest = os.path.normcase(os.path.realpath(manifest_path))

    removed = 0
    failed = []     # entries we could not remove: they stay in the manifest
    for entry in manifest["files"]:
        rel = entry["path"]
        path = os.path.join(out_dir, rel)
        if not _inside(path, out_dir):      # never delete outside the output folder
            continue
        if os.path.normcase(os.path.realpath(path)) == own_manifest:
            continue
        if not os.path.isfile(path):
            continue
        try:
            if entry.get("sha256"):
                if os.path.getsize(path) != entry.get("size") or _sha256(path) != entry["sha256"]:
                    problems.append("%s: changed since, left alone" % rel)
                    continue
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)    # read-only files cannot be removed
            except OSError:
                pass
            os.remove(path)
            removed += 1
        except OSError as e:
            problems.append("%s: could not be removed (%s)" % (rel, e))
            failed.append(entry)

    # Remove folders we created, deepest first, only if empty.
    for rel in sorted(manifest["dirs"], key=lambda r: r.count(os.sep), reverse=True):
        path = os.path.join(out_dir, rel)
        if _inside(path, out_dir) and os.path.isdir(path):
            try:
                os.rmdir(path)
            except OSError:
                pass

    if failed:
        manifest["files"] = failed          # so undo can be tried again
        _save_manifest(manifest_path, manifest)
    else:
        os.remove(manifest_path)
    return removed
