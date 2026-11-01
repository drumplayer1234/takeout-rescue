"""Make a small, realistic fake Google Takeout export for testing.

    python tools/make_fake_takeout.py <dest_dir>

Creates <dest_dir>/Takeout/Google Photos/... and <dest_dir>/expected.json.
Uses only the standard library.
"""

import calendar
import json
import os
import struct
import sys

# --------------------------------------------------------------------------
# Tiny valid JPEG (1x1 grey), built from its segments so it is easy to read.
# --------------------------------------------------------------------------

def _segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


_DC_TABLE = bytes([0x00]) + bytes([1] + [0] * 15) + bytes([0x00])      # one code: category 0
_AC_TABLE = bytes([0x10]) + bytes([1] + [0] * 15) + bytes([0x00])      # one code: end of block
MINIMAL_JPEG = (
    b"\xff\xd8"                                                         # SOI
    + _segment(0xDB, b"\x00" + b"\x01" * 64)                            # quantisation table
    + _segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")           # 1x1, 1 component
    + _segment(0xC4, _DC_TABLE)
    + _segment(0xC4, _AC_TABLE)
    + _segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")                       # start of scan
    + b"\x3f"                                                           # scan data
    + b"\xff\xd9"                                                       # EOI
)


def make_jpeg(tag: str, exif_date: str = None) -> bytes:
    """A valid JPEG that is unique for each `tag` (stored in a COM segment).
    If exif_date ('YYYY:MM:DD HH:MM:SS') is given, a tiny EXIF block is added."""
    out = MINIMAL_JPEG[:2]
    if exif_date:
        out += _exif_segment(exif_date)
    out += _segment(0xFE, ("takeout-rescue-fake:" + tag).encode("ascii"))
    return out + MINIMAL_JPEG[2:]


def _exif_segment(date_text: str) -> bytes:
    """Hand-made APP1 holding only DateTimeOriginal (little-endian TIFF)."""
    value = date_text.encode("ascii") + b"\x00"                         # 20 bytes
    ifd0 = struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, 26) + struct.pack("<I", 0)
    exif_ifd = (struct.pack("<H", 1)
                + struct.pack("<HHII", 0x9003, 2, len(value), 44)
                + struct.pack("<I", 0))
    tiff = b"II" + struct.pack("<HI", 42, 8) + ifd0 + exif_ifd + value
    return _segment(0xE1, b"Exif\x00\x00" + tiff)


def make_video(tag: str) -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + tag.encode("ascii")


# --------------------------------------------------------------------------
# Helpers for writing files
# --------------------------------------------------------------------------

def epoch(y, mo, d, h=0, mi=0, s=0) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))


def sidecar(title, taken=None, geo=None, description=""):
    """A JSON sidecar shaped like the real thing."""
    creation = epoch(2024, 2, 1, 9, 0, 0)       # pretend "download" date
    data = {
        "title": title,
        "description": description,
        "imageViews": "3",
        "creationTime": {"timestamp": str(creation), "formatted": "Feb 1, 2024"},
        "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0,
                    "latitudeSpan": 0.0, "longitudeSpan": 0.0},
        "url": "https://photos.google.com/photo/fake",
    }
    if taken is not None:
        data["photoTakenTime"] = {"timestamp": str(taken), "formatted": "whenever"}
    if geo:
        data["geoData"]["latitude"], data["geoData"]["longitude"] = geo
    return data


def make_fake_takeout(dest_dir: str) -> dict:
    """Build the fake export.  Returns the expected.json content."""
    dest_dir = os.path.abspath(dest_dir)
    root = os.path.join(dest_dir, "Takeout", "Google Photos")
    expected_files = []

    def write(folder, name, data):
        path = os.path.join(root, folder)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, name), "wb") as f:
            f.write(data)

    def write_json(folder, name, obj):
        write(folder, name, json.dumps(obj, indent=2).encode("utf-8"))

    def expect(path, taken, source, src, albums=(), geo=False):
        # "sources" are relative to the Takeout folder (what the user would pick).
        expected_files.append({"path": path, "taken": taken, "date_source": source,
                               "sources": ["Google Photos/" + s for s in src],
                               "albums": list(albums), "has_geo": geo})

    P18, P19, P20 = "Photos from 2018", "Photos from 2019", "Photos from 2020"
    P21, P22, BEACH = "Photos from 2021", "Photos from 2022", "Beach Trip"
    SEP = "/"

    # --- 2018 ---------------------------------------------------------------
    # Normal "<name>.json", a "(1)" duplicate-number pair, and an "-edited" copy.
    t = epoch(2018, 5, 1, 10, 0, 0)
    write(P18, "IMG_5001.jpg", make_jpeg("IMG_5001"))
    write_json(P18, "IMG_5001.jpg.json", sidecar("IMG_5001.jpg", t))
    expect("2018/05/IMG_5001.jpg", t, "json", [P18 + SEP + "IMG_5001.jpg"])

    t2 = epoch(2018, 5, 2, 11, 0, 0)
    write(P18, "IMG_5001(1).jpg", make_jpeg("IMG_5001(1)"))
    write_json(P18, "IMG_5001.jpg(1).json", sidecar("IMG_5001.jpg", t2))
    expect("2018/05/IMG_5001(1).jpg", t2, "json", [P18 + SEP + "IMG_5001(1).jpg"])

    # The edited copy has no JSON of its own: it uses IMG_5001.jpg.json.
    write(P18, "IMG_5001-edited.jpg", make_jpeg("IMG_5001-edited"))
    expect("2018/05/IMG_5001-edited.jpg", t, "json", [P18 + SEP + "IMG_5001-edited.jpg"])

    # A date only inside the file (EXIF), no JSON, no date in the name.
    # Needs piexif to be read, so there is a fallback path for when it is off.
    t_exif = epoch(2018, 12, 24, 18, 30, 0)
    write(P18, "exif_only.jpg", make_jpeg("exif_only", exif_date="2018:12:24 18:30:00"))
    expect("2018/12/exif_only.jpg", t_exif, "exif", [P18 + SEP + "exif_only.jpg"])

    # --- 2019 ---------------------------------------------------------------
    # Same bytes in "Photos from 2019" and the "Beach Trip" album.
    t = epoch(2019, 7, 14, 15, 30, 12)
    pic = make_jpeg("IMG_20190714_153012")
    write(P19, "IMG_20190714_153012.jpg", pic)
    write_json(P19, "IMG_20190714_153012.jpg.json", sidecar("IMG_20190714_153012.jpg", t))
    write(BEACH, "IMG_20190714_153012.jpg", pic)
    write_json(BEACH, "IMG_20190714_153012.jpg.json", sidecar("IMG_20190714_153012.jpg", t))
    expect("2019/07/IMG_20190714_153012.jpg", t, "json",
           [P19 + SEP + "IMG_20190714_153012.jpg", BEACH + SEP + "IMG_20190714_153012.jpg"],
           albums=[BEACH])

    # Newer ".supplemental-metadata.json" (plus geoData); album copy has a
    # truncated suffix ".suppl.json".
    t = epoch(2019, 8, 2, 19, 45, 0)
    pic = make_jpeg("sunset")
    write(P19, "sunset.jpg", pic)
    write_json(P19, "sunset.jpg.supplemental-metadata.json",
               sidecar("sunset.jpg", t, geo=(40.7128, -74.0060), description="Sunset at the pier"))
    write(BEACH, "sunset.jpg", pic)
    write_json(BEACH, "sunset.jpg.suppl.json", sidecar("sunset.jpg", t, geo=(40.7128, -74.0060)))
    expect("2019/08/sunset.jpg", t, "json",
           [P19 + SEP + "sunset.jpg", BEACH + SEP + "sunset.jpg"], albums=[BEACH], geo=True)

    # Two different photos with the same name, same month: the second gets " (2)".
    # (Items are processed in source-path order, so "Beach Trip" comes first.)
    t_a = epoch(2019, 9, 9, 9, 9, 9)
    write(P19, "IMG_0001.jpg", make_jpeg("IMG_0001-A"))
    write_json(P19, "IMG_0001.jpg.json", sidecar("IMG_0001.jpg", t_a))
    t_b = epoch(2019, 9, 20, 10, 0, 0)
    write(BEACH, "IMG_0001.jpg", make_jpeg("IMG_0001-B"))
    write_json(BEACH, "IMG_0001.jpg.json", sidecar("IMG_0001.jpg", t_b))
    expect("2019/09/IMG_0001.jpg", t_b, "json", [BEACH + SEP + "IMG_0001.jpg"], albums=[BEACH])
    expect("2019/09/IMG_0001 (2).jpg", t_a, "json", [P19 + SEP + "IMG_0001.jpg"])

    # Album folders carry a metadata.json that is NOT a photo sidecar.
    write_json(BEACH, "metadata.json", {
        "title": BEACH, "description": "", "access": "",
        "date": {"timestamp": str(epoch(2019, 7, 14)), "formatted": "Jul 14, 2019"},
    })

    # --- 2020 ---------------------------------------------------------------
    # Truncated suffix: ".supplemental-metad.json"
    t = epoch(2020, 3, 15, 18, 0, 0)
    write(P20, "birthday_party.jpg", make_jpeg("birthday_party"))
    write_json(P20, "birthday_party.jpg.supplemental-metad.json", sidecar("birthday_party.jpg", t))
    expect("2020/03/birthday_party.jpg", t, "json", [P20 + SEP + "birthday_party.jpg"])

    # No JSON, no EXIF, no date in the name.
    write(P20, "mystery.jpg", make_jpeg("mystery"))
    expect("Unknown date/mystery.jpg", None, "unknown", [P20 + SEP + "mystery.jpg"])

    # --- 2021 ---------------------------------------------------------------
    # Long name whose JSON name was cut off by Google (46 characters).
    t = epoch(2021, 12, 25, 8, 0, 0)
    long_name = "a_very_long_filename_that_got_cut_off_by_google.jpg"
    write(P21, long_name, make_jpeg("long_name"))
    write_json(P21, long_name[:46] + ".json", sidecar(long_name, t))
    expect("2021/12/" + long_name, t, "json", [P21 + SEP + long_name])

    # No JSON, but the file name holds the date.
    t = epoch(2021, 7, 4, 10, 15, 0)
    write(P21, "PXL_20210704_101500123.jpg", make_jpeg("PXL_20210704"))
    expect("2021/07/PXL_20210704_101500123.jpg", t, "filename", [P21 + SEP + "PXL_20210704_101500123.jpg"])

    # --- 2022 ---------------------------------------------------------------
    t = epoch(2022, 6, 10, 12, 0, 0)
    write(P22, "holiday_clip.mp4", make_video("holiday_clip"))
    write_json(P22, "holiday_clip.mp4.json", sidecar("holiday_clip.mp4", t))
    expect("2022/06/holiday_clip.mp4", t, "json", [P22 + SEP + "holiday_clip.mp4"])

    expected_files.sort(key=lambda e: e["path"])
    expected = {
        "files": expected_files,
        "summary": {
            "total_media": 15, "unique": 13, "duplicates_removed": 2,
            "dates_json": 10, "dates_exif": 1, "dates_filename": 1, "dates_unknown": 1,
            "edited": 1, "with_location": 1,
        },
        # Without piexif the EXIF-only photo cannot be read, so it has no date.
        "without_piexif": {
            "path": "2018/12/exif_only.jpg",
            "fallback_path": "Unknown date/exif_only.jpg",
        },
    }
    with open(os.path.join(dest_dir, "expected.json"), "w", encoding="utf-8") as f:
        json.dump(expected, f, indent=2)
    return expected


def main(argv):
    if len(argv) != 2:
        print("Usage: python tools/make_fake_takeout.py <dest_dir>")
        return 2
    expected = make_fake_takeout(argv[1])
    print("Fake export written to %s (%d unique photos expected)"
          % (os.path.join(os.path.abspath(argv[1]), "Takeout", "Google Photos"),
             len(expected["files"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
