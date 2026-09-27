"""Listing the clips folder (momento/media.py) on a throwaway tree."""

from tests import _sandbox  # noqa: F401  (must come first)

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from momento import media
from momento.media import MediaItem


def touch(path: Path, mtime: float, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


class Scan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="momento-media-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "My Clips"   # custom output dir, with a space
        self.images = self.root / "Images"
        T = 1_700_000_000
        self.T = T
        touch(self.root / "Momento_2026-09-01_10-00-00_5m.mp4", T + 10, b"a" * 5)
        touch(self.root / "Momento_2026-09-01_12-00-00_30s.mp4", T + 30, b"b" * 7)
        touch(self.images / "Momento_2026-09-01_11-00-00.png", T + 20, b"c" * 3)
        touch(self.images / "Momento_2026-09-01_13-00-00.png", T + 40)
        # temp files the exporter / screenshot writer leave while they work
        for name in (".Momento_x.tmp.mp4", ".Momento_x.part0.mp4", ".Momento_x.segments.txt",
                     ".momento-abc123.tmp", ".hidden.mp4"):
            touch(self.root / name, T + 99)
        touch(self.images / ".momento-q1w2e3.tmp", T + 99)
        touch(self.images / ".Momento_y.png", T + 99)
        # junk: other extensions, wrong folder, directories named like media
        touch(self.root / "notes.txt", T + 99)
        touch(self.root / "stray.png", T + 99)          # screenshots live in Images/
        touch(self.images / "stray.mp4", T + 99)         # clips live in the top folder
        touch(self.root / "sub" / "nested.mp4", T + 99)  # not recursive
        (self.root / "folder.mp4").mkdir()

    def test_lists_clips_and_shots_newest_first(self):
        items = media.scan(self.root)
        self.assertEqual([(i.path.name, i.kind) for i in items], [
            ("Momento_2026-09-01_13-00-00.png", "shot"),
            ("Momento_2026-09-01_12-00-00_30s.mp4", "clip"),
            ("Momento_2026-09-01_11-00-00.png", "shot"),
            ("Momento_2026-09-01_10-00-00_5m.mp4", "clip"),
        ])
        clip = items[1]
        self.assertIsInstance(clip, MediaItem)
        self.assertEqual(clip.path, self.root / "Momento_2026-09-01_12-00-00_30s.mp4")
        self.assertEqual(clip.size, 7)
        self.assertEqual(clip.mtime, self.T + 30)
        with self.assertRaises(Exception):
            clip.size = 1                                 # frozen

    def test_mtime_decides_order_not_name(self):
        os.utime(self.root / "Momento_2026-09-01_10-00-00_5m.mp4", (self.T + 50, self.T + 50))
        self.assertEqual(media.scan(self.root)[0].path.name, "Momento_2026-09-01_10-00-00_5m.mp4")

    def test_same_mtime_orders_by_name(self):
        touch(self.root / "Momento_b.mp4", self.T + 60)
        touch(self.root / "Momento_a.mp4", self.T + 60)
        names = [i.path.name for i in media.scan(self.root)[:2]]
        self.assertEqual(names, ["Momento_b.mp4", "Momento_a.mp4"])

    def test_uppercase_extension(self):
        touch(self.root / "Imported.MP4", self.T + 70)
        self.assertEqual(media.scan(self.root)[0].path.name, "Imported.MP4")

    def test_accepts_str_and_tilde(self):
        self.assertEqual(len(media.scan(str(self.root))), 4)
        with mock.patch.dict(os.environ, {"HOME": self._tmp.name}):
            self.assertEqual(media.images_dir("~/My Clips"), self.images)
            self.assertEqual(len(media.scan("~/My Clips")), 4)

    def test_images_dir(self):
        from momento import screenshot
        self.assertEqual(media.images_dir(self.root), self.root / screenshot.IMAGES_DIR)

    def test_missing_dirs(self):
        self.assertEqual(media.scan(self.root / "nope"), [])
        shutil.rmtree(self.images)
        self.assertEqual([i.kind for i in media.scan(self.root)], ["clip", "clip"])
        only_images = Path(self._tmp.name) / "only-shots"
        touch(only_images / "Images" / "a.png", self.T)
        self.assertEqual([i.kind for i in media.scan(only_images)], ["shot"])

    def test_output_dir_is_a_file(self):
        self.assertEqual(media.scan(self.root / "notes.txt"), [])

    def test_file_vanishing_mid_scan_is_skipped(self):
        real_scandir = os.scandir
        victim = self.root / "Momento_2026-09-01_12-00-00_30s.mp4"

        class Listing(list):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def racy_scandir(path):
            with real_scandir(path) as it:
                entries = Listing(it)
            if Path(path) == self.root:
                victim.unlink()                           # deleted after the directory read
            return entries

        with mock.patch.object(media.os, "scandir", racy_scandir):
            items = media.scan(self.root)
        self.assertNotIn(victim, [i.path for i in items])
        self.assertEqual(len(items), 3)

    def test_stat_error_is_skipped(self):
        class Entry:
            name = "Momento_gone.mp4"
            path = str(self.root / "Momento_gone.mp4")

            def is_file(self):
                raise FileNotFoundError(2, "gone")

        class Listing(list):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(media.os, "scandir", lambda p: Listing([Entry()])):
            self.assertEqual(media.scan(self.root), [])


class FilterAndNearest(unittest.TestCase):
    def setUp(self):
        mk = lambda n, k, t: MediaItem(Path(n), k, float(t), 1)  # noqa: E731
        self.items = [mk("e.png", "shot", 50), mk("d.mp4", "clip", 40), mk("c.png", "shot", 30),
                      mk("b.mp4", "clip", 20), mk("a.mp4", "clip", 10)]

    def test_filter(self):
        self.assertEqual(media.filter_items(self.items, "all"), self.items)
        self.assertIsNot(media.filter_items(self.items, "all"), self.items)
        self.assertEqual([i.path.name for i in media.filter_items(self.items, "clip")],
                         ["d.mp4", "b.mp4", "a.mp4"])
        self.assertEqual([i.path.name for i in media.filter_items(self.items, "shot")],
                         ["e.png", "c.png"])
        self.assertEqual(media.filter_items([], "clip"), [])
        with self.assertRaises(ValueError):
            media.filter_items(self.items, "video")

    def test_nearest(self):
        self.assertEqual(media.nearest([], 5.0), -1)
        self.assertEqual(media.nearest(self.items, 40.0), 1)       # exact
        self.assertEqual(media.nearest(self.items, 999.0), 0)      # newer than all
        self.assertEqual(media.nearest(self.items, 0.0), 4)        # older than all
        self.assertEqual(media.nearest(self.items, 33.0), 2)
        self.assertEqual(media.nearest(self.items, 35.0), 1)       # tie: the newer one
        # keeping your place: selected screenshot c (30) -> clips-only view
        clips = media.filter_items(self.items, "clip")
        self.assertEqual(clips[media.nearest(clips, 30.0)].path.name, "d.mp4")


class ClipDuration(unittest.TestCase):
    def run_result(self, rc=0, out=""):
        return subprocess.CompletedProcess([], rc, stdout=out, stderr="")

    def test_parses_ffprobe(self):
        with mock.patch.object(media.subprocess, "run", return_value=self.run_result(0, "61.043000\n")) as run:
            self.assertAlmostEqual(media.clip_duration("/x/clip.mp4"), 61.043)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ffprobe")
        self.assertIn("format=duration", argv)
        self.assertEqual(argv[-1], "/x/clip.mp4")
        self.assertTrue(run.call_args.kwargs.get("timeout"))

    def test_failures_are_none(self):
        cases = [self.run_result(1, ""), self.run_result(0, "N/A\n"), self.run_result(0, ""),
                 self.run_result(0, "nan\n")]
        for res in cases:
            with mock.patch.object(media.subprocess, "run", return_value=res):
                self.assertIsNone(media.clip_duration("/x/clip.mp4"), res)
        for exc in (FileNotFoundError(2, "ffprobe"), subprocess.TimeoutExpired("ffprobe", 3)):
            with mock.patch.object(media.subprocess, "run", side_effect=exc):
                self.assertIsNone(media.clip_duration("/x/clip.mp4"))

    def test_real_file_that_is_not_a_video(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(b"not a video")
            f.flush()
            self.assertIsNone(media.clip_duration(f.name))


class Deletable(unittest.TestCase):
    """The gallery deletes only clips directly in the clips folder and screenshots directly
    in its Images folder: never anything else, never through a symlink."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("MOMENTO_TEST_SANDBOX"))
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.out = root / "Momento"
        (self.out / "Images" / "deeper").mkdir(parents=True)
        self.clip = self.out / "Replay_a.mp4"
        self.shot = self.out / "Images" / "Momento_a.png"
        self.other = root / "other.mp4"
        for f in (self.clip, self.shot, self.other, self.out / "notes.txt", self.out / ".x.tmp.mp4",
                  self.out / "Images" / "deeper" / "b.png", self.out / "Images" / "c.mp4"):
            f.write_bytes(b"x")
        (self.out / "link.mp4").symlink_to(self.other)
        (self.out / "dir.mp4").mkdir()

    def test_allowed(self):
        self.assertTrue(media.deletable(self.clip, self.out))
        self.assertTrue(media.deletable(self.shot, self.out))
        self.assertTrue(media.deletable(str(self.clip), str(self.out)))

    def test_refused(self):
        for bad in (self.other, self.out / "notes.txt", self.out / ".x.tmp.mp4", self.out / "link.mp4",
                    self.out / "dir.mp4", self.out / "Images" / "deeper" / "b.png",
                    self.out / "Images" / "c.mp4", self.out / "Images" / ".." / ".." / "other.mp4",
                    self.out / "missing.mp4"):
            self.assertFalse(media.deletable(bad, self.out), bad)
        with self.assertRaises(ValueError):
            media.delete(self.other, self.out, to_trash=False)
        self.assertTrue(self.other.exists())

    def test_delete_for_good_or_to_the_trash(self):
        from unittest import mock

        self.assertEqual(media.delete(self.clip, self.out, to_trash=False), "deleted")
        self.assertFalse(self.clip.exists())
        with mock.patch.object(media, "trash") as trash:
            self.assertEqual(media.delete(self.shot, self.out), "trashed")
        trash.assert_called_once_with(self.shot)


class ImportIsLight(unittest.TestCase):
    def test_no_qt_or_gi(self):
        code = ("import sys, momento.media; "
                "bad = [m for m in ('PySide6', 'gi') if m in sys.modules]; "
                "sys.exit(1 if bad else 0)")
        env = dict(os.environ)
        r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=env)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
