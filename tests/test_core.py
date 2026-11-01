"""Tests for takeout_rescue.core.  Standard library only.

Run from the project folder:
    python -m unittest discover -s tests -v

Time zones: where time.tzset exists (Linux, macOS) the tests set TZ to a fixed
zone.  On Windows there is no tzset, so the tests work out what they expect with
the same local-time conversion the program uses.
"""

import builtins
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from takeout_rescue import core  # noqa: E402

EST = "EST5EDT,M3.2.0,M11.1.0"          # a POSIX time zone: needs no tz database
_OLD_TZ = []


def set_tz(value):
    """Switch the time zone (only where time.tzset exists).  Returns the old value."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = value
    time.tzset()
    return old


def setUpModule():
    if hasattr(time, "tzset"):
        _OLD_TZ.append(set_tz(EST))


def tearDownModule():
    if _OLD_TZ:
        if _OLD_TZ[0] is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = _OLD_TZ[0]
        time.tzset()


def _load_generator():
    path = os.path.join(ROOT, "tools", "make_fake_takeout.py")
    spec = importlib.util.spec_from_file_location("make_fake_takeout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()


def hash_tree(folder):
    """{relative path: sha256} for every file under folder."""
    result = {}
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                result[os.path.relpath(path, folder)] = hashlib.sha256(f.read()).hexdigest()
    return result


def list_files(folder):
    found = set()
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            found.add(os.path.relpath(os.path.join(dirpath, name), folder).replace(os.sep, "/"))
    return found


def _force_remove(func, path, _exc):
    """rmtree helper: read-only files cannot be removed on Windows."""
    if not os.path.lexists(path):       # nothing to remove
        return
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def local_from_epoch(n):
    """The local time of a real moment, worked out in the test's own way."""
    utc = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=n)
    return utc.astimezone().replace(tzinfo=None)


def read_manifest(out_dir):
    with open(os.path.join(out_dir, core.MANIFEST_NAME), encoding="utf-8") as f:
        return json.load(f)


def read_report(out_dir, name=core.REPORT_NAME):
    import csv
    with open(os.path.join(out_dir, name), newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


class FakeTakeoutTestCase(unittest.TestCase):
    """Builds the fake export once per test class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="takeout_rescue_test_")
        cls.fake_root = os.path.join(cls.tmp, "fake")
        cls.export_dir = os.path.join(cls.fake_root, "Takeout")
        GEN.make_fake_takeout(cls.fake_root)
        with open(os.path.join(cls.fake_root, "expected.json"), encoding="utf-8") as f:
            cls.expected = json.load(f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, onerror=_force_remove)

    def expected_files(self):
        """expected.json 'files', adjusted when piexif is not available."""
        files = [dict(f) for f in self.expected["files"]]
        if core.piexif is None:
            fb = self.expected["without_piexif"]
            for f in files:
                if f["path"] == fb["path"]:
                    f["path"], f["taken"], f["date_source"] = fb["fallback_path"], None, "unknown"
        return files

    def expected_summary(self):
        s = dict(self.expected["summary"])
        if core.piexif is None:
            s["dates_exif"] -= 1
            s["dates_unknown"] += 1
        return s


class ScanAndApplyTests(FakeTakeoutTestCase):

    def setUp(self):
        self.out_dir = os.path.join(self.tmp, "out_" + self._testMethodName)
        self.before = hash_tree(self.export_dir)
        self.plan = core.scan(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.out_dir, onerror=_force_remove)

    def test_summary_counts(self):
        self.assertEqual(self.plan.summary, self.expected_summary())

    def test_plain_summary_mentions_counts(self):
        text = self.plan.plain_summary()
        self.assertIn("Found 18 photos", text)
        self.assertIn("2 duplicate copies will be skipped", text)
        self.assertIn("1 guessed from the upload date", text)
        self.assertIn("1 file will not be copied", text)

    def test_output_paths_match_expected(self):
        core.apply(self.plan, self.out_dir)
        want = {f["path"] for f in self.expected_files()}
        got = list_files(self.out_dir)
        got -= {core.REPORT_NAME, core.MANIFEST_NAME}
        self.assertEqual(got, want)

    def test_duplicates_copied_once(self):
        core.apply(self.plan, self.out_dir)
        names = [p for p in list_files(self.out_dir) if p.endswith("IMG_20190714_153012.jpg")
                 or p.endswith("sunset.jpg")]
        self.assertEqual(sorted(names), ["2019/07/IMG_20190714_153012.jpg", "2019/08/sunset.jpg"])
        by_source = {it.rel_source.replace(os.sep, "/"): it for it in self.plan.items}
        item = by_source["Google Photos/Photos from 2019/sunset.jpg"]
        self.assertEqual(item.albums, ["Beach Trip"])
        self.assertEqual(len(item.duplicates), 1)

    def test_file_times_match_taken_times(self):
        result = core.apply(self.plan, self.out_dir)
        not_set = {row["destination"] for row in read_report(self.out_dir)
                   if "file date not set" in row["action"]}
        self.assertEqual(len(not_set), result["file_date_not_set"])
        for f in self.expected_files():
            if f["taken"] is None or not f["taken_exact"]:
                continue
            want = time.mktime(dt.datetime.strptime(f["taken"], "%Y-%m-%d %H:%M:%S").timetuple())
            mtime = os.path.getmtime(os.path.join(self.out_dir, *f["path"].split("/")))
            self.assertAlmostEqual(mtime, want, delta=2, msg=f["path"])
        self.assertEqual(not_set, set())

    @unittest.skipUnless(os.name == "nt", "Windows creation time")
    def test_creation_time_matches_file_date(self):
        core.apply(self.plan, self.out_dir)
        checked = 0
        for f in self.expected_files():
            if f["taken"] is None:
                continue
            st = os.stat(os.path.join(self.out_dir, *f["path"].split("/")))
            created = getattr(st, "st_birthtime", st.st_ctime)
            self.assertAlmostEqual(created, st.st_mtime, delta=2, msg=f["path"])
            checked += 1
        self.assertGreater(checked, 0)
        st = os.stat(os.path.join(self.out_dir, "1965", "06", "old_scan_1965.jpg"))
        created = getattr(st, "st_birthtime", st.st_ctime)
        self.assertEqual(core._local_from_epoch(int(created)).year, 1965)
        self.assertFalse([r for r in read_report(self.out_dir)
                          if "created date not set" in r["action"]])

    def test_date_sources_and_albums(self):
        by_source = {it.rel_source.replace(os.sep, "/"): it for it in self.plan.items}
        for f in self.expected_files():
            item = by_source[f["sources"][0]] if f["sources"][0] in by_source else \
                by_source[f["sources"][-1]]
            self.assertEqual(item.date_source, f["date_source"], f["path"])
            if f["taken"] is None:
                self.assertIsNone(item.taken, f["path"])
            else:
                got = item.taken.strftime("%Y-%m-%d %H:%M:%S")
                want = f["taken"] if f["taken_exact"] else f["taken"][:7]
                self.assertEqual(got[:len(want)], want, f["path"])
            self.assertEqual(item.albums, f["albums"], f["path"])
            self.assertEqual(item.lat is not None, f["has_geo"], f["path"])

    def test_new_years_eve_stays_in_december(self):
        # 23:30 local on Dec 31 is already Jan 1 in UTC for most of the world.
        core.apply(self.plan, self.out_dir)
        self.assertIn("2019/12/new_years_eve.jpg", list_files(self.out_dir))
        item = [i for i in self.plan.items if i.rel_source.endswith("new_years_eve.jpg")][0]
        self.assertEqual(item.taken, dt.datetime(2019, 12, 31, 23, 30, 0))

    def test_pre_1971_json_date_is_used(self):
        core.apply(self.plan, self.out_dir)
        self.assertIn("1965/06/old_scan_1965.jpg", list_files(self.out_dir))
        item = [i for i in self.plan.items if i.rel_source.endswith("old_scan_1965.jpg")][0]
        self.assertEqual(item.date_source, "json")
        self.assertEqual(item.taken.year, 1965)

    def test_upload_date_is_counted_separately(self):
        item = [i for i in self.plan.items if i.rel_source.endswith("upload_only.jpg")][0]
        self.assertEqual(item.date_source, "upload_date")
        self.assertEqual(self.plan.summary["dates_upload"], 1)
        self.assertIn("Dates guessed from upload date : 1", "\n".join(self.plan.summary_lines()))

    def test_unknown_type_is_listed_not_dropped(self):
        want = [p.replace("/", os.sep) for p in self.expected["not_copied"]]
        self.assertEqual(self.plan.unknown_files, want)
        self.assertIn("Not copied (unknown type)      : 1", "\n".join(self.plan.summary_lines()))
        result = core.apply(self.plan, self.out_dir)
        self.assertEqual(result["not_copied_unknown"], 1)
        rows = [r for r in read_report(self.out_dir) if r["action"] == "not copied (unknown type)"]
        self.assertEqual([r["source"] for r in rows], want)
        self.assertFalse(any(p.endswith("notes.txt") for p in list_files(self.out_dir)))

    def test_description_and_geo_read(self):
        item = [i for i in self.plan.items if i.rel_source.endswith("sunset.jpg")][0]
        self.assertEqual(item.description, "Sunset at the pier")
        self.assertAlmostEqual(item.lat, 40.7128)
        self.assertAlmostEqual(item.lon, -74.0060)

    def test_input_unchanged(self):
        core.apply(self.plan, self.out_dir)
        self.assertEqual(hash_tree(self.export_dir), self.before)
        # No stray files in the export either.
        self.assertEqual(set(hash_tree(self.export_dir)), set(self.before))

    def test_report_and_manifest_written(self):
        core.apply(self.plan, self.out_dir)
        report = os.path.join(self.out_dir, core.REPORT_NAME)
        with open(report, encoding="utf-8-sig") as f:
            header = f.readline().strip().split(",")
        self.assertEqual(header[:6], ["source", "destination", "taken_time",
                                      "date_source", "albums", "action"])
        manifest = read_manifest(self.out_dir)
        paths = [e["path"] for e in manifest["files"]]
        self.assertEqual(len([p for p in paths if p != core.REPORT_NAME]),
                         self.expected["summary"]["unique"])
        # Every entry knows where it came from and what was created.
        for entry in manifest["files"]:
            if entry["path"] != core.REPORT_NAME:
                self.assertEqual(len(entry["source_sha256"]), 64)
                self.assertEqual(entry["size"], os.path.getsize(os.path.join(self.out_dir, entry["path"])))
                self.assertEqual(len(entry["sha256"]), 64)

    def test_undo_removes_only_created_files(self):
        os.makedirs(os.path.join(self.out_dir, "2019", "07"))
        keep = os.path.join(self.out_dir, "2019", "07", "my_own_photo.jpg")
        with open(keep, "wb") as f:
            f.write(b"mine")
        keep_top = os.path.join(self.out_dir, "notes.txt")
        with open(keep_top, "w") as f:
            f.write("keep me")

        core.apply(self.plan, self.out_dir)
        self.assertGreater(len(list_files(self.out_dir)), 3)
        problems = []
        removed = core.undo(self.out_dir, problems)
        self.assertEqual(problems, [])
        self.assertEqual(removed, self.expected["summary"]["unique"] + 1)  # photos + report
        self.assertEqual(list_files(self.out_dir), {"2019/07/my_own_photo.jpg", "notes.txt"})
        # Folders we made are gone, the one that was already there stays.
        self.assertEqual(sorted(os.listdir(self.out_dir)), ["2019", "notes.txt"])
        self.assertEqual(os.listdir(os.path.join(self.out_dir, "2019")), ["07"])
        self.assertEqual(hash_tree(self.export_dir), self.before)

    def test_refuses_output_inside_export(self):
        bad = os.path.join(self.export_dir, "Google Photos", "clean")
        with self.assertRaises(core.TakeoutError):
            core.apply(self.plan, bad)
        self.assertFalse(os.path.exists(bad))
        with self.assertRaises(core.TakeoutError):
            core.apply(self.plan, self.export_dir)
        self.assertEqual(hash_tree(self.export_dir), self.before)

    def test_second_run_creates_nothing_new(self):
        first = core.apply(self.plan, self.out_dir)
        files_after_first = list_files(self.out_dir)
        photos_after_first = hash_tree(self.out_dir)
        second = core.apply(core.scan(self.export_dir), self.out_dir)   # no " (2)" copies
        self.assertEqual(first["copied"], self.expected["summary"]["unique"])
        self.assertEqual(second["copied"], 0)
        self.assertEqual(second["already_done"], self.expected["summary"]["unique"])
        self.assertEqual(second["errors"], 0)
        self.assertEqual(list_files(self.out_dir), files_after_first)
        photo_names = {p for p in photos_after_first if p not in (core.REPORT_NAME, core.MANIFEST_NAME)}
        self.assertEqual({p: photos_after_first[p] for p in photo_names},
                         {p: hash_tree(self.out_dir)[p] for p in photo_names})
        self.assertFalse(any("(3)" in p for p in files_after_first))
        # The two different IMG_0001.jpg photos are still the same two files.
        self.assertIn("2019/09/IMG_0001 (2).jpg", list_files(self.out_dir))
        # And the second run can be undone fully.
        core.undo(self.out_dir)
        self.assertEqual(list_files(self.out_dir), set())

    def test_run_again_only_refills_a_deleted_photo(self):
        core.apply(self.plan, self.out_dir)
        os.remove(os.path.join(self.out_dir, "2019", "09", "IMG_0001.jpg"))
        result = core.apply(core.scan(self.export_dir), self.out_dir)
        self.assertEqual(result["copied"], 1)
        self.assertEqual(result["already_done"], self.expected["summary"]["unique"] - 1)
        self.assertIn("2019/09/IMG_0001.jpg", list_files(self.out_dir))
        self.assertIn("2019/09/IMG_0001 (2).jpg", list_files(self.out_dir))
        self.assertNotIn("2019/09/IMG_0001 (3).jpg", list_files(self.out_dir))

    def test_identical_file_already_in_output_is_not_copied_again(self):
        # Bytes identical to the source, but not made by us: skipped, and undo keeps it.
        dest = os.path.join(self.out_dir, "2018", "05", "IMG_5001.jpg")
        os.makedirs(os.path.dirname(dest))
        shutil.copyfile(os.path.join(self.export_dir, "Google Photos", "Photos from 2018",
                                     "IMG_5001.jpg"), dest)
        result = core.apply(self.plan, self.out_dir)
        self.assertEqual(result["already_done"], 1)
        self.assertNotIn("2018/05/IMG_5001 (2).jpg", list_files(self.out_dir))
        self.assertNotIn(os.path.join("2018", "05", "IMG_5001.jpg"),
                         [e["path"] for e in read_manifest(self.out_dir)["files"]])
        core.undo(self.out_dir)
        self.assertEqual(list_files(self.out_dir), {"2018/05/IMG_5001.jpg"})

    @unittest.skipIf(core.piexif is None, "piexif not available")
    def test_exif_written(self):
        import piexif
        core.apply(self.plan, self.out_dir)
        # JSON-dated photo with no EXIF gets date written (local time).
        exif = piexif.load(os.path.join(self.out_dir, "2019", "07", "IMG_20190714_153012.jpg"))
        self.assertEqual(exif["Exif"][piexif.ExifIFD.DateTimeOriginal], b"2019:07:14 15:30:12")
        self.assertEqual(exif["0th"][piexif.ImageIFD.DateTime], b"2019:07:14 15:30:12")
        exif = piexif.load(os.path.join(self.out_dir, "2019", "12", "new_years_eve.jpg"))
        self.assertEqual(exif["Exif"][piexif.ExifIFD.DateTimeOriginal], b"2019:12:31 23:30:00")
        # Photo with geoData gets GPS (south/west check on sign refs).
        exif = piexif.load(os.path.join(self.out_dir, "2019", "08", "sunset.jpg"))
        self.assertEqual(exif["GPS"][piexif.GPSIFD.GPSLatitudeRef], b"N")
        self.assertEqual(exif["GPS"][piexif.GPSIFD.GPSLongitudeRef], b"W")
        lat = exif["GPS"][piexif.GPSIFD.GPSLatitude]
        self.assertEqual(lat[0], (40, 1))
        self.assertEqual(lat[1], (42, 1))
        # A photo that already had a date keeps it.
        exif = piexif.load(os.path.join(self.out_dir, "2018", "12", "exif_only.jpg"))
        self.assertEqual(exif["Exif"][piexif.ExifIFD.DateTimeOriginal], b"2018:12:24 18:30:00")

    @unittest.skipUnless(core.piexif is None, "only checks the no-piexif path")
    def test_exif_not_written_without_piexif(self):
        core.apply(self.plan, self.out_dir)
        with open(os.path.join(self.out_dir, "2019", "07", "IMG_20190714_153012.jpg"), "rb") as f:
            self.assertNotIn(b"Exif", f.read())
        self.assertFalse(self.plan.piexif_available)
        self.assertIn("piexif", "\n".join(self.plan.summary_lines()))


class TempExportTestCase(unittest.TestCase):
    """A small export built file by file inside each test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="takeout_rescue_test_")
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_force_remove)
        self.export_dir = os.path.join(self.tmp, "Takeout")
        self.photos = os.path.join(self.export_dir, "Google Photos", "Photos from 2019")
        self.out_dir = os.path.join(self.tmp, "out")
        os.makedirs(self.photos)

    def add(self, name, data=None, folder=None):
        folder = folder or self.photos
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(data if data is not None else GEN.make_jpeg(name))
        return path

    def add_json(self, name, obj, folder=None):
        return self.add(name, json.dumps(obj).encode("utf-8"), folder)

    def scan(self):
        return core.scan(self.export_dir)

    def only_item(self):
        plan = self.scan()
        self.assertEqual(len(plan.items), 1)
        return plan.items[0]


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

class JsonDateTests(unittest.TestCase):

    def test_pre_1971_and_negative_epochs_are_accepted(self):
        n = GEN.utc_epoch(1965, 6, 15, 12)
        self.assertLess(n, 0)
        self.assertEqual(core.json_taken_time({"photoTakenTime": {"timestamp": str(n)}}), n)
        self.assertEqual(core.json_taken_time({"photoTakenTime": {"timestamp": GEN.utc_epoch(1960, 1, 1)}}),
                         GEN.utc_epoch(1960, 1, 1))
        self.assertEqual(core.json_taken_time({"photoTakenTime": {"timestamp": "100"}}), 100)

    def test_garbage_epochs_are_ignored(self):
        for bad in (None, "", "0", 0, "abc", "nan", "1e999", "-1e999", "inf", [], {},
                    "99999999999999999999", str(GEN.utc_epoch(1700, 1, 1)),
                    str(int(time.time()) + 10 * 86400), True):
            self.assertIsNone(core.json_taken_time({"photoTakenTime": {"timestamp": bad}}), repr(bad))
        self.assertIsNone(core.json_taken_time({}))
        self.assertIsNone(core.json_taken_time({"photoTakenTime": "oops"}))

    def test_local_time_of_an_old_epoch(self):
        # Windows cannot do local time before 1970; the program must still answer.
        got = core._local_from_epoch(GEN.utc_epoch(1965, 6, 15, 12))
        self.assertEqual((got.year, got.month), (1965, 6))

    def test_creation_time_is_not_the_taken_time(self):
        data = {"creationTime": {"timestamp": "1700000000"},
                "photoTakenTime": {"timestamp": "0"},
                "geoData": {"latitude": 0.0, "longitude": 0.0}}
        self.assertIsNone(core.json_taken_time(data))
        self.assertEqual(core.json_creation_time(data), 1700000000)
        self.assertIsNone(core.json_geo(data))


class DatePriorityTests(TempExportTestCase):

    def setUp(self):
        super().setUp()
        self.taken = GEN.epoch(2018, 3, 4, 10, 0, 0)

    def test_filename_beats_upload_date(self):
        self.add("IMG_20190714_153012.jpg")
        self.add_json("IMG_20190714_153012.jpg.json", GEN.sidecar("x.jpg"))     # creationTime only
        item = self.only_item()
        self.assertEqual(item.date_source, "filename")
        self.assertEqual(item.taken, dt.datetime(2019, 7, 14, 15, 30, 12))

    @unittest.skipIf(core.piexif is None, "piexif not available")
    def test_exif_beats_upload_date(self):
        self.add("plain.jpg", GEN.make_jpeg("exif", exif_date="2017:05:06 07:08:09"))
        self.add_json("plain.jpg.json", GEN.sidecar("plain.jpg"))
        item = self.only_item()
        self.assertEqual(item.date_source, "exif")
        self.assertEqual(item.taken, dt.datetime(2017, 5, 6, 7, 8, 9))

    def test_photo_taken_time_beats_everything(self):
        self.add("IMG_20190714_153012.jpg")
        self.add_json("IMG_20190714_153012.jpg.json", GEN.sidecar("x.jpg", self.taken))
        item = self.only_item()
        self.assertEqual(item.date_source, "json")
        self.assertEqual(item.taken, dt.datetime(2018, 3, 4, 10, 0, 0))

    def test_upload_date_is_the_last_resort(self):
        self.add("plain.jpg")
        self.add_json("plain.jpg.json", GEN.sidecar("plain.jpg"))
        plan = self.scan()
        self.assertEqual(plan.items[0].date_source, "upload_date")
        self.assertEqual(plan.summary["dates_upload"], 1)
        self.assertEqual(plan.summary["dates_unknown"], 0)

    def test_nothing_known_is_unknown(self):
        self.add("plain.jpg")
        self.assertEqual(self.only_item().date_source, "unknown")

    def test_real_taken_date_beats_upload_date_between_duplicates(self):
        same = GEN.make_jpeg("dup")
        self.add("pic.jpg", same)
        self.add_json("pic.jpg.json", GEN.sidecar("pic.jpg"))                       # upload date only
        album = os.path.join(self.export_dir, "Google Photos", "Zebra Album")
        self.add("pic.jpg", same, album)
        self.add_json("pic.jpg.json", GEN.sidecar("pic.jpg", self.taken), album)    # real date
        item = self.only_item()
        self.assertEqual(item.date_source, "json")
        self.assertEqual(item.taken, dt.datetime(2018, 3, 4, 10, 0, 0))


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset (not on Windows)")
class TimeZoneSwitchTests(TempExportTestCase):

    def switch(self, value):
        old = set_tz(value)

        def restore():
            os.environ["TZ"] = old if old is not None else EST
            time.tzset()
        self.addCleanup(restore)

    def folder_for(self, epoch):
        self.add("p.jpg")
        self.add_json("p.jpg.json", GEN.sidecar("p.jpg", epoch))
        core.apply(self.scan(), self.out_dir)
        return set(os.listdir(self.out_dir))

    def test_json_epoch_goes_to_the_local_month(self):
        n = GEN.utc_epoch(2019, 8, 1, 2, 30)             # 22:30 on July 31 in New York
        self.assertEqual(self.folder_for(n), {"2019", core.MANIFEST_NAME, core.REPORT_NAME})
        self.assertEqual(os.listdir(os.path.join(self.out_dir, "2019")), ["07"])

    def test_same_epoch_in_another_zone_is_another_month(self):
        self.switch("JST-9")                              # 11:30 on August 1 in Tokyo
        self.folder_for(GEN.utc_epoch(2019, 8, 1, 2, 30))
        self.assertEqual(os.listdir(os.path.join(self.out_dir, "2019")), ["08"])

    def test_exif_time_and_mtime_for_json_date(self):
        self.add("p.jpg")
        n = GEN.utc_epoch(2019, 8, 1, 2, 30)
        self.add_json("p.jpg.json", GEN.sidecar("p.jpg", n))
        core.apply(self.scan(), self.out_dir)
        path = os.path.join(self.out_dir, "2019", "07", "p.jpg")
        self.assertAlmostEqual(os.path.getmtime(path), n, delta=2)       # the real moment
        if core.piexif is not None:
            import piexif
            exif = piexif.load(path)
            self.assertEqual(exif["Exif"][piexif.ExifIFD.DateTimeOriginal], b"2019:07:31 22:30:00")

    def test_filename_date_is_wall_clock_in_any_zone(self):
        for zone in (EST, "JST-9", "UTC0"):
            self.switch(zone)
            shutil.rmtree(self.out_dir, ignore_errors=True)
            for name in os.listdir(self.photos):
                os.remove(os.path.join(self.photos, name))
            self.add("IMG_20191231_233000.jpg")
            core.apply(self.scan(), self.out_dir)
            path = os.path.join(self.out_dir, "2019", "12", "IMG_20191231_233000.jpg")
            want = time.mktime(dt.datetime(2019, 12, 31, 23, 30).timetuple())
            self.assertAlmostEqual(os.path.getmtime(path), want, delta=2, msg=zone)


class LocalTimeTests(TempExportTestCase):
    """Same checks as above, but they work on Windows too (no tzset)."""

    def test_json_date_follows_the_computers_local_time(self):
        n = GEN.utc_epoch(2019, 8, 1, 2, 30)
        want = local_from_epoch(n)
        self.add("p.jpg")
        self.add_json("p.jpg.json", GEN.sidecar("p.jpg", n))
        core.apply(self.scan(), self.out_dir)
        path = os.path.join(self.out_dir, "%04d" % want.year, "%02d" % want.month, "p.jpg")
        self.assertTrue(os.path.isfile(path), path)
        self.assertAlmostEqual(os.path.getmtime(path), n, delta=2)
        if core.piexif is not None:
            import piexif
            got = piexif.load(path)["Exif"][piexif.ExifIFD.DateTimeOriginal]
            self.assertEqual(got.decode(), want.strftime("%Y:%m:%d %H:%M:%S"))

    def test_filename_date_is_used_as_written(self):
        self.add("IMG_20191231_233000.jpg")
        core.apply(self.scan(), self.out_dir)
        path = os.path.join(self.out_dir, "2019", "12", "IMG_20191231_233000.jpg")
        self.assertTrue(os.path.isfile(path))
        want = time.mktime(dt.datetime(2019, 12, 31, 23, 30).timetuple())
        self.assertAlmostEqual(os.path.getmtime(path), want, delta=2)

    def test_mktime_overflow_skips_the_file_date(self):
        self.add("IMG_20191231_233000.jpg")
        plan = self.scan()
        with mock.patch.object(core.time, "mktime", side_effect=OverflowError):
            plan = self.scan()
        self.assertIsNone(plan.items[0].mtime)
        result = core.apply(plan, self.out_dir)
        self.assertEqual((result["copied"], result["errors"], result["file_date_not_set"]), (1, 0, 1))
        self.assertIn("file date not set", read_report(self.out_dir)[0]["action"])
        self.assertTrue(result["warnings"])

    def test_utime_failure_is_not_an_error(self):
        # Pre-1970 dates can make os.utime fail on Windows.
        self.add("IMG_20191231_233000.jpg")
        plan = self.scan()
        with mock.patch.object(core.os, "utime", side_effect=OSError("bad date")):
            result = core.apply(plan, self.out_dir)
        self.assertEqual((result["copied"], result["errors"]), (1, 0))
        self.assertEqual(result["file_date_not_set"], 1)
        row = read_report(self.out_dir)[0]
        self.assertIn("file date not set", row["action"])
        self.assertNotIn("error", row["action"])
        self.assertTrue(os.path.isfile(os.path.join(self.out_dir, row["destination"])))


# --------------------------------------------------------------------------
# Applying: manifest, report, re-runs
# --------------------------------------------------------------------------

class ManifestAndReportTests(TempExportTestCase):

    def setUp(self):
        super().setUp()
        for i in range(4):
            self.add("IMG_2019071%d_153012.jpg" % i)

    def test_interrupted_run_can_be_undone(self):
        real_copy = shutil.copy2
        calls = []

        def copy_then_die(src, dst):
            calls.append(dst)
            if len(calls) == 3:
                with open(dst, "wb") as f:
                    f.write(b"half a ph")
                raise KeyboardInterrupt()
            return real_copy(src, dst)

        with mock.patch.object(core.shutil, "copy2", side_effect=copy_then_die):
            with self.assertRaises(KeyboardInterrupt):
                core.apply(self.scan(), self.out_dir)
        manifest = read_manifest(self.out_dir)
        listed = {e["path"] for e in manifest["files"]}
        for dst in calls:
            self.assertIn(os.path.relpath(dst, self.out_dir), listed)     # incl. the half copy
        self.assertEqual(len(listed), 4)                                 # even those never tried
        core.undo(self.out_dir)
        self.assertEqual(list_files(self.out_dir), set())

    def test_failed_copy_keeps_its_path_and_is_one_error(self):
        real_copy = shutil.copy2

        def fail_second(src, dst):
            if "1_153012" in src:
                with open(dst, "wb") as f:
                    f.write(b"half")
                raise OSError("disk full")
            return real_copy(src, dst)

        with mock.patch.object(core.shutil, "copy2", side_effect=fail_second):
            result = core.apply(self.scan(), self.out_dir)
        self.assertEqual((result["copied"], result["errors"]), (3, 1))
        paths = [e["path"] for e in read_manifest(self.out_dir)["files"]]
        self.assertIn(os.path.join("2019", "07", "IMG_20190711_153012.jpg"), paths)
        self.assertNotIn("2019/07/IMG_20190711_153012.jpg", list_files(self.out_dir))   # no half file
        core.undo(self.out_dir)
        self.assertEqual(list_files(self.out_dir), set())

    def test_manifest_is_flushed_regularly(self):
        for i in range(30):
            self.add("IMG_20190801_0000%02d.jpg" % i)
        with mock.patch.object(core, "_save_manifest", wraps=core._save_manifest) as save:
            core.apply(self.scan(), self.out_dir)
        # before copying, after file 25, in the finally, and after adding the report
        self.assertGreaterEqual(save.call_count, 4)

    def test_locked_report_does_not_orphan_files(self):
        real_open = builtins.open

        def locked(path, *args, **kwargs):
            if os.path.basename(str(path)) == core.REPORT_NAME:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(builtins, "open", side_effect=locked):
            result = core.apply(self.scan(), self.out_dir)
        self.assertEqual(result["copied"], 4)
        self.assertIsNone(result["report"])
        self.assertTrue(any("report" in w for w in result["warnings"]))
        self.assertEqual(len([e for e in read_manifest(self.out_dir)["files"]
                              if e["path"] != core.REPORT_NAME]), 4)
        core.undo(self.out_dir)
        self.assertEqual(list_files(self.out_dir), set())

    def test_existing_report_of_the_user_is_not_overwritten(self):
        os.makedirs(self.out_dir)
        mine = os.path.join(self.out_dir, core.REPORT_NAME)
        with open(mine, "w") as f:
            f.write("my own file")
        result = core.apply(self.scan(), self.out_dir)
        self.assertEqual(result["report"], "takeout_rescue_report (2).csv")
        with open(mine) as f:
            self.assertEqual(f.read(), "my own file")
        # A second run replaces our own report instead of making a third one.
        result = core.apply(self.scan(), self.out_dir)
        self.assertEqual(result["report"], "takeout_rescue_report (2).csv")
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, "takeout_rescue_report (3).csv")))
        core.undo(self.out_dir)
        self.assertEqual(list_files(self.out_dir), {core.REPORT_NAME})

    def test_damaged_manifest_stops_apply(self):
        os.makedirs(self.out_dir)
        path = os.path.join(self.out_dir, core.MANIFEST_NAME)
        with open(path, "w") as f:
            f.write("{this is not json")
        with self.assertRaises(core.TakeoutError) as ctx:
            core.apply(self.scan(), self.out_dir)
        self.assertIn("damaged", str(ctx.exception))
        with open(path) as f:
            self.assertEqual(f.read(), "{this is not json")
        self.assertEqual(list_files(self.out_dir), {core.MANIFEST_NAME})
        with self.assertRaises(core.TakeoutError):
            core.undo(self.out_dir)

    def test_manifest_dirs_only_hold_folders_we_made(self):
        # The output folder's parents do not exist yet: they must not be listed.
        out = os.path.join(self.tmp, "new", "deeper", "out")
        core.apply(self.scan(), out)
        dirs = read_manifest(out)["dirs"]
        self.assertTrue(dirs)
        for rel in dirs:
            self.assertNotIn("..", rel.split(os.sep))
            self.assertFalse(os.path.isabs(rel))
        self.assertEqual(sorted(dirs), ["2019", os.path.join("2019", "07")])

    def test_existing_folders_are_not_in_the_manifest(self):
        os.makedirs(os.path.join(self.out_dir, "2019"))
        core.apply(self.scan(), self.out_dir)
        self.assertEqual(read_manifest(self.out_dir)["dirs"], [os.path.join("2019", "07")])

    def test_csv_cells_cannot_be_formulas(self):
        self.add("evil.jpg")
        self.add_json("evil.jpg.json", GEN.sidecar("evil.jpg", GEN.epoch(2019, 7, 1, 12),
                                                    geo=(-33.8, 151.2), description="=HYPERLINK(\"x\")"))
        self.add("plus.jpg")
        self.add_json("plus.jpg.json", GEN.sidecar("plus.jpg", GEN.epoch(2019, 7, 1, 12),
                                                    description="+1+1"))
        self.add("at.jpg")
        self.add_json("at.jpg.json", GEN.sidecar("at.jpg", GEN.epoch(2019, 7, 1, 12),
                                                  description="@SUM(A1)"))
        self.add("minus.jpg")
        self.add_json("minus.jpg.json", GEN.sidecar("minus.jpg", GEN.epoch(2019, 7, 1, 12),
                                                     description="-2+3"))
        self.add("fine.jpg")
        self.add_json("fine.jpg.json", GEN.sidecar("fine.jpg", GEN.epoch(2019, 7, 1, 12),
                                                    description="Nice day = good"))
        core.apply(self.scan(), self.out_dir)
        rows = {r["source"].split(os.sep)[-1]: r for r in read_report(self.out_dir)}
        self.assertEqual(rows["evil.jpg"]["description"], "'=HYPERLINK(\"x\")")
        self.assertEqual(rows["plus.jpg"]["description"], "'+1+1")
        self.assertEqual(rows["at.jpg"]["description"], "'@SUM(A1)")
        self.assertEqual(rows["minus.jpg"]["description"], "'-2+3")
        self.assertEqual(rows["fine.jpg"]["description"], "Nice day = good")
        self.assertEqual(rows["evil.jpg"]["latitude"], "-33.8")      # numbers stay numbers


class SafetyTests(TempExportTestCase):

    def setUp(self):
        super().setUp()
        for i in range(3):
            self.add("IMG_2019071%d_153012.jpg" % i)

    def test_read_only_source_gives_a_writable_copy(self):
        src = os.path.join(self.photos, "IMG_20190710_153012.jpg")
        os.chmod(src, stat.S_IREAD)
        self.addCleanup(os.chmod, src, stat.S_IWRITE | stat.S_IREAD)
        result = core.apply(self.scan(), self.out_dir)
        self.assertEqual((result["copied"], result["errors"]), (3, 0))
        dest = os.path.join(self.out_dir, "2019", "07", "IMG_20190710_153012.jpg")
        self.assertTrue(os.access(dest, os.W_OK))
        if core.piexif is not None:
            self.assertEqual(result["exif_written"], 3)      # the read-only one too
        os.chmod(dest, stat.S_IREAD)                          # undo must cope with read-only
        problems = []
        core.undo(self.out_dir, problems)
        self.assertEqual(problems, [])
        self.assertEqual(list_files(self.out_dir), set())

    def test_undo_leaves_changed_files_alone(self):
        core.apply(self.scan(), self.out_dir)
        changed = os.path.join(self.out_dir, "2019", "07", "IMG_20190711_153012.jpg")
        with open(changed, "ab") as f:
            f.write(b"I edited this")
        problems = []
        removed = core.undo(self.out_dir, problems)
        self.assertEqual(removed, 3)              # two photos + report
        self.assertEqual(list_files(self.out_dir), {"2019/07/IMG_20190711_153012.jpg"})
        self.assertEqual(len(problems), 1)
        self.assertIn("changed since, left alone", problems[0])
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, core.MANIFEST_NAME)))

    def test_undo_keeps_going_after_an_error(self):
        core.apply(self.scan(), self.out_dir)
        real_remove = os.remove

        def stubborn(path):
            if path.endswith("IMG_20190711_153012.jpg"):
                raise PermissionError(13, "in use")
            return real_remove(path)

        problems = []
        with mock.patch.object(core.os, "remove", side_effect=stubborn):
            removed = core.undo(self.out_dir, problems)
        self.assertEqual(removed, 3)
        self.assertEqual(len(problems), 1)
        self.assertIn("IMG_20190711_153012.jpg", problems[0])
        self.assertEqual(list_files(self.out_dir),
                         {"2019/07/IMG_20190711_153012.jpg", core.MANIFEST_NAME})
        # The manifest still knows the stubborn file, so undo can be tried again.
        self.assertEqual(core.undo(self.out_dir), 1)
        self.assertEqual(list_files(self.out_dir), set())

    def test_undo_ignores_manifest_itself_and_outside_paths(self):
        os.makedirs(self.out_dir)
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w") as f:
            f.write("keep")
        inside = os.path.join(self.out_dir, "mine.txt")
        with open(inside, "w") as f:
            f.write("mine too")
        manifest = {"files": [core.MANIFEST_NAME, "../outside.txt", outside, "mine.txt"],
                    "dirs": ["..", "../x", os.path.abspath(self.tmp)]}
        path = os.path.join(self.out_dir, core.MANIFEST_NAME)
        with open(path, "w") as f:
            json.dump(manifest, f)
        removed = core.undo(self.out_dir)
        self.assertEqual(removed, 1)                       # only mine.txt (old-style entry)
        self.assertTrue(os.path.isfile(outside))
        self.assertTrue(os.path.isdir(self.tmp))
        self.assertFalse(os.path.exists(inside))
        self.assertFalse(os.path.exists(path))


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

class ScanTests(TempExportTestCase):

    def test_more_media_types_are_copied(self):
        exts = [".jfif", ".tif", ".tiff", ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf",
                ".orf", ".rw2", ".avif", ".heif", ".mp", ".avi", ".3gp", ".mpg", ".mpeg", ".m4v",
                ".mkv", ".webm", ".mts", ".m2ts", ".wmv", ".flv", ".JPG", ".Heic"]
        for i, ext in enumerate(exts):
            self.add("IMG_20190701_1200%02d%s" % (i, ext), b"data %d" % i)
        plan = self.scan()
        self.assertEqual(plan.summary["unique"], len(exts))
        self.assertEqual(plan.unknown_files, [])
        for ext in exts:
            self.assertIn(ext.lower(), core.MEDIA_EXTENSIONS)

    def test_other_files_are_listed_in_summary_and_report(self):
        self.add("a.jpg")
        self.add("Thumbs.db", b"x")
        self.add("readme.txt", b"x")
        self.add("movie.xyz", b"x")
        self.add_json("a.jpg.json", GEN.sidecar("a.jpg", GEN.epoch(2019, 7, 1, 12)))   # not listed
        plan = self.scan()
        base = os.path.join("Google Photos", "Photos from 2019")
        self.assertEqual(plan.unknown_files, [os.path.join(base, n) for n in
                                               ("movie.xyz", "readme.txt", "Thumbs.db")])
        self.assertEqual(plan.summary["not_copied_unknown"], 3)
        text = "\n".join(plan.summary_lines())
        self.assertIn("Not copied (unknown type)      : 3", text)
        self.assertIn("readme.txt", text)
        core.apply(plan, self.out_dir)
        actions = [r["action"] for r in read_report(self.out_dir)]
        self.assertEqual(actions.count("not copied (unknown type)"), 3)

    def test_unreadable_file_does_not_stop_the_scan(self):
        self.add("good1.jpg")
        self.add("bad.jpg")
        self.add("good2.jpg")
        real_sha = core._sha256

        def sha(path):
            if path.endswith("bad.jpg"):
                raise PermissionError(13, "Permission denied")
            return real_sha(path)

        with mock.patch.object(core, "_sha256", side_effect=sha):
            plan = self.scan()
        self.assertEqual(plan.summary["unique"], 2)
        self.assertEqual(plan.summary["could_not_read"], 1)
        self.assertEqual(plan.unreadable, [os.path.join("Google Photos", "Photos from 2019", "bad.jpg")])
        self.assertIn("Could not read                 : 1", "\n".join(plan.summary_lines()))
        with mock.patch.object(core, "_sha256", side_effect=sha):
            core.apply(plan, self.out_dir)
        actions = [r["action"] for r in read_report(self.out_dir)]
        self.assertIn("not copied (could not read)", actions)

    def test_unreadable_folder_is_recorded(self):
        self.add("good.jpg")
        real_walk = os.walk

        def walk(top, onerror=None, **kwargs):
            self.assertIsNotNone(onerror)
            onerror(PermissionError(13, "Permission denied", os.path.join(top, "Locked Album")))
            return real_walk(top, onerror=onerror, **kwargs)

        with mock.patch.object(core.os, "walk", side_effect=walk):
            plan = self.scan()
        self.assertEqual(plan.unreadable, ["Locked Album" + os.sep])
        self.assertEqual(plan.summary["could_not_read"], 1)
        self.assertEqual(plan.summary["unique"], 1)


class SidecarTests(unittest.TestCase):

    def names(self, *files):
        return {f.lower(): f for f in files}

    def test_sidecar_patterns(self):
        cases = [
            ("IMG_1234.jpg", ["IMG_1234.jpg.json"], "IMG_1234.jpg.json"),
            ("IMG_1234.jpg", ["IMG_1234.jpg.supplemental-metadata.json"],
             "IMG_1234.jpg.supplemental-metadata.json"),
            ("IMG_1234.jpg", ["IMG_1234.jpg.supplemental-metad.json"],
             "IMG_1234.jpg.supplemental-metad.json"),
            ("IMG_1234.jpg", ["IMG_1234.jpg.suppl.json"], "IMG_1234.jpg.suppl.json"),
            ("a_very_long_filename_that_got_cut_off_by_google.jpg",
             ["a_very_long_filename_that_got_cut_off_by_googl.json"],
             "a_very_long_filename_that_got_cut_off_by_googl.json"),
            ("IMG_1234(1).jpg", ["IMG_1234.jpg.json", "IMG_1234.jpg(1).json"],
             "IMG_1234.jpg(1).json"),
            ("IMG_1234(1).jpg", ["IMG_1234.jpg.supplemental-metadata(1).json"],
             "IMG_1234.jpg.supplemental-metadata(1).json"),
            ("IMG_1234(1).jpg", ["IMG_1234.jpg.supplemental-metad(1).json"],
             "IMG_1234.jpg.supplemental-metad(1).json"),
            ("IMG_1234-edited.jpg", ["IMG_1234.jpg.json"], "IMG_1234.jpg.json"),
            ("IMG_1234-edited.jpg", ["IMG_1234.jpg.supplemental-metadata.json"],
             "IMG_1234.jpg.supplemental-metadata.json"),
            ("img_1234.JPG", ["IMG_1234.JPG.json"], "IMG_1234.JPG.json"),
        ]
        for media, jsons, want in cases:
            self.assertEqual(core.find_sidecar(media, self.names(*jsons)), want, media)

    def test_sidecar_no_false_matches(self):
        # A numbered copy must not borrow the un-numbered photo's json.
        self.assertIsNone(core.find_sidecar("IMG_1234(1).jpg", self.names("IMG_1234.jpg.json")))
        # A different photo's json is not a match.
        self.assertIsNone(core.find_sidecar("IMG_9999.jpg", self.names("IMG_1234.jpg.json")))
        # The exact json beats a truncated one.
        self.assertEqual(core.find_sidecar("IMG_1234.jpg",
                                           self.names("IMG_1234.jpg.suppl.json", "IMG_1234.jpg.json")),
                         "IMG_1234.jpg.json")

    def test_short_names_never_use_name_prefix_matching(self):
        # 24 characters cannot have been cut off, so "<name>.json" is another photo.
        self.assertIsNone(core.find_sidecar("a_very_long_filename.jpg",
                                            self.names("a_very_long_filename.json")))
        # A cut-off suffix after the full name is still fine for short names.
        self.assertEqual(core.find_sidecar("a.jpg", self.names("a.jpg.sup.json")), "a.jpg.sup.json")

    def test_edited_names_in_many_languages(self):
        for suffix in core.EDITED_SUFFIXES:
            for form in (suffix, suffix.upper(), unicodedata.normalize("NFD", suffix)):
                name = "IMG_1234" + form + ".jpg"
                self.assertTrue(core.is_edited_name(name), name)
                self.assertTrue(core.is_edited_name("IMG_1234" + form + "(1).jpg"), name)
                self.assertEqual(core.find_sidecar(name, self.names("IMG_1234.jpg.json")),
                                 "IMG_1234.jpg.json", name)
                self.assertEqual(core.find_sidecar("IMG_1234" + form + "(1).jpg",
                                                   self.names("IMG_1234.jpg(1).json")),
                                 "IMG_1234.jpg(1).json", name)
        self.assertEqual(sorted(core.EDITED_SUFFIXES), sorted(
            ["-edited", "-bearbeitet", "-modifié", "-modificato", "-editado",
             "-bewerkt", "-redigerad", "-muokattu", "-edytowane"]))
        self.assertFalse(core.is_edited_name("IMG_1234.jpg"))
        self.assertFalse(core.is_edited_name("my-edited-photo.jpg"))


class SidecarCollisionTests(unittest.TestCase):
    """Long names that Google cut off to the same json name."""

    PRE = "a_really_long_holiday_photo_name_that_gets_cut_"       # 48 characters
    A, B = PRE + "1.jpg", PRE + "2.jpg"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="takeout_rescue_test_")
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_force_remove)
        self.cut = (self.A + ".supplemental-metadata")[:46] + ".json"
        self.assertEqual(self.cut, (self.B + ".supplemental-metadata")[:46] + ".json")

    def write(self, name, obj):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def match(self, media):
        jsons = {n.lower(): n for n in os.listdir(self.tmp) if n.endswith(".json")}
        return core.match_sidecars(self.tmp, media, jsons)

    def test_title_decides_who_gets_a_cut_off_json(self):
        self.write(self.cut, {"title": self.B})
        self.assertEqual(self.match([self.A, self.B]), {self.B: self.cut})

    def test_title_of_the_original_works_for_edited_and_numbered(self):
        self.write(self.cut, {"title": self.A})
        edited = self.A[:-4] + "-edited.jpg"
        got = self.match([self.A, edited, self.B])
        self.assertEqual(got, {self.A: self.cut, edited: self.cut})

    def test_one_json_is_claimed_by_one_media_file(self):
        self.write(self.cut, {"description": "no title in here"})
        self.assertEqual(self.match([self.A, self.B]), {self.A: self.cut})

    def test_exact_match_cannot_be_stolen(self):
        self.write(self.A + ".json", {"title": self.A})
        self.write(self.cut, {"title": self.A})
        got = self.match([self.A, self.B])
        self.assertEqual(got[self.A], self.A + ".json")

    def test_many_media_files_are_matched_quickly(self):
        # The old code compared every media file with every json file (n * n).
        n = 3000
        jsons = {}
        for i in range(n):
            name = "unrelated_photo_with_a_pretty_long_name_%05d.jpg.json" % i
            jsons[name.lower()] = name
        media = ["holiday_photo_with_a_pretty_long_name_%05d.jpg" % i for i in range(n)]
        start = time.time()
        got = core.match_sidecars(self.tmp, media, jsons)
        self.assertEqual(got, {})
        self.assertLess(time.time() - start, 2.0)


class SmallPieceTests(unittest.TestCase):

    def test_filename_dates(self):
        t = lambda *a: core._naive_date(*a)  # noqa: E731
        cases = {
            "IMG_20190714_153012.jpg": t(2019, 7, 14, 15, 30, 12),
            "PXL_20190714_153012345.jpg": t(2019, 7, 14, 15, 30, 12),
            "VID_20190714_153012.mp4": t(2019, 7, 14, 15, 30, 12),
            "Screenshot_2019-07-14-15-30-12.png": t(2019, 7, 14, 15, 30, 12),
            "2019-07-14 15.30.12.jpg": t(2019, 7, 14, 15, 30, 12),
            "IMG-20190714-WA0001.jpg": t(2019, 7, 14),
            "IMG_5001.jpg": None,
            "IMG_20191399_153012.jpg": None,      # month 13 is not a date
            "IMG_21000101_000000.jpg": None,      # in the future
        }
        for name, want in cases.items():
            self.assertEqual(core.filename_taken_time(name), want, name)
        self.assertEqual(core.filename_taken_time("scan_19650615_120000.jpg"),
                         dt.datetime(1965, 6, 15, 12, 0, 0))

    def test_filetime_conversion(self):
        self.assertEqual(core._filetime_from_unix(0), 116444736000000000)
        self.assertEqual(core._filetime_from_unix(-11644473600), 0)
        self.assertIsNone(core._filetime_from_unix(-11644473601))
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(core._filetime_from_unix(bad))

    def test_dms_carries_over_when_seconds_round_to_60(self):
        self.assertEqual(core._dms(10.99999999999), ((11, 1), (0, 1), (0, 10000)))
        self.assertEqual(core._dms(-10.5 + 1e-12 * 0), ((10, 1), (30, 1), (0, 10000)))
        self.assertEqual(core._dms(40.99999999999)[0], (41, 1))
        self.assertEqual(core._dms(33.98333333333), ((33, 1), (59, 1), (0, 10000)))
        # Ordinary values are unchanged.
        self.assertEqual(core._dms(40.7128), ((40, 1), (42, 1), (460800, 10000)))
        self.assertEqual(core._dms(-74.0060), ((74, 1), (0, 1), (216000, 10000)))
        for value in (0.0, 0.9999999, 59.99999999, 89.99999999, 179.999999999):
            deg, minutes, seconds = core._dms(value)
            self.assertLess(minutes[0], 60, value)
            self.assertLess(seconds[0], 60 * 10000, value)


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

class GuiTests(unittest.TestCase):

    def setUp(self):
        try:
            import tkinter
            from takeout_rescue import gui
            self.root = tkinter.Tk()
        except Exception as e:      # no tkinter or no display
            self.skipTest("no window available: %s" % e)
        self.addCleanup(self.root.destroy)
        self.root.withdraw()
        self.gui = gui
        self.app = gui.App(self.root)

    def fix_state(self):
        return str(self.app.btn_fix.cget("state"))

    def test_poll_always_reschedules_and_resets_busy(self):
        self.app.busy = True
        self.app.messages.put(("scan_done", None))
        self.app.messages.put(("progress", 1, 1, "x"))
        scheduled = []
        self.root.after = lambda ms, func=None, *a: scheduled.append(ms)

        def explode(msg):
            raise RuntimeError("boom")

        self.app.handle = explode
        with mock.patch.object(self.gui.messagebox, "showerror") as showerror:
            self.app.poll()
        self.assertFalse(self.app.busy)
        self.assertEqual(scheduled, [100])
        self.assertEqual(showerror.call_count, 2)
        self.assertIn("boom", showerror.call_args[0][1])

    def test_fix_button_stays_off_after_a_run_until_a_new_scan(self):
        class FakePlan:
            def plain_summary(self):
                return "plan"

        self.app.plan = FakePlan()
        self.app.out_dir = "somewhere"
        self.app.update_buttons()
        self.assertEqual(self.fix_state(), "normal")
        counts = {"copied": 1, "already_done": 0, "errors": 0, "warnings": []}
        self.app.busy = True
        self.app.handle(("apply_done", counts))
        self.assertEqual(self.fix_state(), "disabled")
        self.app.update_buttons()
        self.assertEqual(self.fix_state(), "disabled")
        self.app.busy = True
        self.app.handle(("scan_done", FakePlan()))
        self.assertEqual(self.fix_state(), "normal")


if __name__ == "__main__":
    unittest.main()
