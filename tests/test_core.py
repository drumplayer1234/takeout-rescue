"""Tests for takeout_rescue.core.  Standard library only.

Run from the project folder:
    python -m unittest discover -s tests -v
"""

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from takeout_rescue import core  # noqa: E402


def _load_generator():
    path = os.path.join(ROOT, "tools", "make_fake_takeout.py")
    spec = importlib.util.spec_from_file_location("make_fake_takeout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class FakeTakeoutTestCase(unittest.TestCase):
    """Builds the fake export once per test class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="takeout_rescue_test_")
        cls.fake_root = os.path.join(cls.tmp, "fake")
        cls.export_dir = os.path.join(cls.fake_root, "Takeout")
        _load_generator().make_fake_takeout(cls.fake_root)
        with open(os.path.join(cls.fake_root, "expected.json"), encoding="utf-8") as f:
            cls.expected = json.load(f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

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
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def test_summary_counts(self):
        self.assertEqual(self.plan.summary, self.expected_summary())

    def test_plain_summary_mentions_counts(self):
        text = self.plan.plain_summary()
        self.assertIn("Found 15 photos", text)
        self.assertIn("2 duplicate copies will be skipped", text)

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
        core.apply(self.plan, self.out_dir)
        for f in self.expected_files():
            if f["taken"] is None:
                continue
            mtime = os.path.getmtime(os.path.join(self.out_dir, *f["path"].split("/")))
            self.assertAlmostEqual(mtime, f["taken"], delta=2, msg=f["path"])

    def test_date_sources_and_albums(self):
        by_source = {it.rel_source.replace(os.sep, "/"): it for it in self.plan.items}
        for f in self.expected_files():
            item = by_source[f["sources"][0]] if f["sources"][0] in by_source else \
                by_source[f["sources"][-1]]
            self.assertEqual(item.date_source, f["date_source"], f["path"])
            self.assertEqual(item.taken, f["taken"], f["path"])
            self.assertEqual(item.albums, f["albums"], f["path"])
            self.assertEqual(item.lat is not None, f["has_geo"], f["path"])

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
        with open(os.path.join(self.out_dir, core.MANIFEST_NAME), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(len([p for p in manifest["files"] if p != core.REPORT_NAME]),
                         self.expected["summary"]["unique"])

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
        removed = core.undo(self.out_dir)
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

    def test_second_run_is_undoable(self):
        core.apply(self.plan, self.out_dir)
        core.apply(core.scan(self.export_dir), self.out_dir)   # names get " (2)" etc.
        core.undo(self.out_dir)
        self.assertEqual(list_files(self.out_dir), set())

    @unittest.skipIf(core.piexif is None, "piexif not available")
    def test_exif_written(self):
        import piexif
        core.apply(self.plan, self.out_dir)
        # JSON-dated photo with no EXIF gets date written.
        exif = piexif.load(os.path.join(self.out_dir, "2019", "07", "IMG_20190714_153012.jpg"))
        self.assertEqual(exif["Exif"][piexif.ExifIFD.DateTimeOriginal], b"2019:07:14 15:30:12")
        self.assertEqual(exif["0th"][piexif.ImageIFD.DateTime], b"2019:07:14 15:30:12")
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


class SmallPieceTests(unittest.TestCase):

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

    def test_filename_dates(self):
        t = lambda *a: core._to_epoch(*a)  # noqa: E731
        cases = {
            "IMG_20190714_153012.jpg": t(2019, 7, 14, 15, 30, 12),
            "PXL_20190714_153012345.jpg": t(2019, 7, 14, 15, 30, 12),
            "VID_20190714_153012.mp4": t(2019, 7, 14, 15, 30, 12),
            "Screenshot_2019-07-14-15-30-12.png": t(2019, 7, 14, 15, 30, 12),
            "2019-07-14 15.30.12.jpg": t(2019, 7, 14, 15, 30, 12),
            "IMG-20190714-WA0001.jpg": t(2019, 7, 14),
            "IMG_5001.jpg": None,
            "IMG_20191399_153012.jpg": None,      # month 13 is not a date
        }
        for name, want in cases.items():
            self.assertEqual(core.filename_taken_time(name), want, name)

    def test_creation_time_fallback_and_zero_geo(self):
        data = {"creationTime": {"timestamp": "1700000000"},
                "photoTakenTime": {"timestamp": "0"},
                "geoData": {"latitude": 0.0, "longitude": 0.0}}
        self.assertEqual(core.json_taken_time(data), 1700000000)
        self.assertIsNone(core.json_geo(data))


if __name__ == "__main__":
    unittest.main()
