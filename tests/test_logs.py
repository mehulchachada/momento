"""The log files and `momento logs`: python3 -m unittest tests.test_logs

The files live in a temporary XDG_STATE_HOME; journalctl and the file manager are
faked. Nothing here reaches the live daemon, its journal or config.
"""

from __future__ import annotations

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401

import contextlib
import io
import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import cli, logs  # noqa: E402


class LogFilesTest(unittest.TestCase):
    """The file handler: its folder, rotation, the line format, window titles."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}))
        root = logging.getLogger()
        before = (root.level, sys.excepthook, threading.excepthook, logs._hooked)

        def restore():
            h = logs.file_handler()
            if h is not None:
                root.removeHandler(h)
                h.close()
            root.setLevel(before[0])
            sys.excepthook, threading.excepthook, logs._hooked = before[1], before[2], before[3]
        self.addCleanup(restore)

    def test_setup(self):
        self.assertEqual(logs.log_dir(), self.state / "momento" / "logs")
        path = logs.setup("daemon")
        self.assertEqual(path, self.state / "momento" / "logs" / "momento.log")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        h = logs.file_handler()
        self.assertIsInstance(h, logging.handlers.RotatingFileHandler)
        self.assertEqual((h.maxBytes, h.backupCount), (2_000_000, 4))    # 2 MB x 5 files
        logging.getLogger("momento.pipeline").info("starting capture: source=%s", "portal")
        logging.getLogger("momento.daemon").debug("not at INFO")
        from momento.logs import hidden
        logging.getLogger("momento.daemon").info("recording window (window title: %s)", hidden("Secret Tab"))
        h.flush()
        lines = path.read_text().splitlines()
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d daemon INFO pipeline: "
                                   r"starting capture: source=portal$")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("daemon: recording window (window title: <redacted, 10 chars>)"))
        self.assertNotIn("Secret", path.read_text())
        # the bar gets its own file; setting up again replaces the handler
        bar = logs.setup("bar")
        self.assertEqual(bar.name, "bar.log")
        self.assertEqual(sum(1 for x in logging.getLogger().handlers if x.get_name() == logs.HANDLER_NAME), 1)
        logging.getLogger("momento.overlay").warning("layer-shell setup failed")
        logs.file_handler().flush()
        self.assertIn(" bar WARNING overlay: layer-shell setup failed", bar.read_text())

    def test_rotation(self):
        path = logs.setup("daemon")
        h = logs.file_handler()
        h.maxBytes = 300                                   # small, to see it roll over
        for i in range(40):
            logging.getLogger("momento.daemon").info("event %02d %s", i, "x" * 40)
        names = sorted(p.name for p in path.parent.iterdir())
        self.assertEqual(names, ["momento.log", "momento.log.1", "momento.log.2", "momento.log.3", "momento.log.4"])
        self.assertLessEqual(max(p.stat().st_size for p in path.parent.iterdir()), 300)
        # read back in time order across the rotated files
        entries = logs.read_entries(logs.file_paths(path.parent))
        events = [t.split("event ")[1][:2] for _s, t in entries]
        self.assertEqual(events, sorted(events))
        self.assertEqual(events[-1], "39")

    def test_uncaught_errors_reach_the_file(self):
        path = logs.setup("daemon")
        try:
            raise ValueError("bad thing")
        except ValueError:
            exc = sys.exc_info()
        with contextlib.redirect_stderr(io.StringIO()):
            sys.excepthook(*exc)
        logs.file_handler().flush()
        text = path.read_text()
        self.assertIn("daemon ERROR momento: uncaught error", text)
        self.assertIn("ValueError: bad thing", text)

    def test_unwritable_folder(self):
        blocker = self.state / "momento"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the folder should be")
        with self.assertLogs("momento.logs", "WARNING"):
            self.assertIsNone(logs.setup("daemon"))
        self.assertIsNone(logs.file_handler())

    def test_windowname_debug_hides_the_title(self):
        from momento import windowname

        class Store:
            def Lookup(self, *a, **k):
                return [], ("KDE", 1, b"")

        class Bus:
            def get_object(self, *a, **k):
                return Store()
        with mock.patch.object(windowname, "parse_restore_data", return_value="Private Chat"), \
                self.assertLogs("momento.windowname", "DEBUG") as cm:
            self.assertEqual(windowname.title_for_token("tok", bus=Bus()), "Private Chat")
        self.assertNotIn("Private", "\n".join(cm.output))
        self.assertIn("<redacted, 12 chars>", "\n".join(cm.output))


def logs_args(*argv):
    return cli.build_parser().parse_args(["logs", *argv])


class LogsCommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}))
        self.dir = self.state / "momento" / "logs"

    def run_main(self, *argv, boot=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(logs, "boot_time", return_value=boot), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["logs", *argv])
        return code, out.getvalue(), err.getvalue()

    def write_logs(self, n=300):
        self.dir.mkdir(parents=True)
        start = datetime(2026, 9, 27, 10, 0, 0)
        with open(self.dir / "momento.log", "w") as f:
            for i in range(n):
                f.write(f"{(start + timedelta(seconds=2 * i)):%Y-%m-%d %H:%M:%S} daemon INFO daemon: d{i}\n")
            f.write("Traceback (most recent call last):\n  boom\n")
        with open(self.dir / "bar.log", "w") as f:
            f.write(f"{start + timedelta(seconds=1):%Y-%m-%d %H:%M:%S} bar INFO overlay: b0\n")

    def test_parser(self):
        a = logs_args()
        self.assertEqual((a.since, a.boot, a.all, a.lines, a.follow, a.open), (None, False, False, None, False, False))
        a = logs_args("--since", "1h", "-n", "50", "-f")
        self.assertEqual((a.since, a.lines, a.follow), ("1h", 50, True))
        self.assertTrue(logs_args("--boot").boot)
        self.assertTrue(logs_args("--all").all)
        self.assertTrue(logs_args("--open").open)
        for bad in (["--boot", "--all"], ["--since", "1h", "--all"], ["-n", "0"], ["-n", "x"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                logs_args(*bad)
        self.assertEqual(logs.parse_since("1h"), timedelta(hours=1))
        self.assertEqual(logs.parse_since("30 min"), timedelta(minutes=30))
        self.assertEqual(logs.parse_since("2d"), timedelta(days=2))
        for bad in ("", "1x", "0h", "soon"):
            with self.assertRaises(ValueError):
                logs.parse_since(bad)
        code, _out, err = self.run_main("--since", "soon")
        self.assertEqual(code, 2)
        self.assertIn("not a time span", err)

    def test_files_default_boot_and_all(self):
        self.write_logs()
        code, out, err = self.run_main()
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), logs.DEFAULT_LINES)
        self.assertEqual(lines[-2:], ["Traceback (most recent call last):", "  boom"])   # kept with its line
        self.assertIn("momento logs --open", err)
        # both files, in time order
        code, out, _err = self.run_main("--all")
        lines = out.splitlines()
        self.assertEqual(len(lines), 300 + 1 + 2)
        self.assertTrue(lines[0].endswith("d0") and lines[1].endswith("b0") and lines[2].endswith("d1"))
        # this boot only: started 10 s in
        code, out, _err = self.run_main("--boot", boot=datetime(2026, 9, 27, 10, 0, 10))
        self.assertTrue(out.splitlines()[0].endswith("d5"))
        code, out, _err = self.run_main("--boot", "-n", "3", boot=datetime(2026, 9, 27, 10, 0, 10))
        self.assertEqual(len(out.splitlines()), 3)

    def test_since(self):
        self.write_logs()
        with mock.patch.object(logs, "datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 27, 10, 10, 0)
            code, out, _err = self.run_main("--since", "1m")
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].endswith("d270"))                  # 10:09:00

    def test_journal_fallback(self):
        calls = []

        class R:
            returncode, stdout, stderr = 0, "Sep 27 10:00:00 momento[1]: hello\n", ""

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return R()
        with mock.patch.object(logs.shutil, "which", return_value="/usr/bin/journalctl"), \
                mock.patch.object(logs.subprocess, "run", side_effect=fake_run):
            code, out, _err = self.run_main()
            self.assertEqual((code, out), (0, "Sep 27 10:00:00 momento[1]: hello\n"))
            self.assertEqual(calls[-1], ["journalctl", "--user", "-u", "momento.service", "--no-pager", "-q",
                                         "--no-hostname", "-o", "short", "-b", "0", "-n", "200"])
            self.run_main("--since", "1h")
            self.assertEqual(calls[-1][-1], "--since=-3600s")
            self.assertNotIn("-b", calls[-1])
            self.run_main("--all")
            self.assertNotIn("-b", calls[-1])
            self.assertNotIn("-n", calls[-1])
            R.stdout = ""
            code, _out, err = self.run_main()
            self.assertEqual(code, 0)
            self.assertIn("Nothing from Momento in the system log yet", err)
            R.returncode, R.stderr = 1, "Failed to open journal"
            code, _out, err = self.run_main()
            self.assertEqual(code, 1)
            self.assertIn("can't read the system log: Failed to open journal", err)
        with mock.patch.object(logs.shutil, "which", return_value="/usr/bin/journalctl"), \
                mock.patch.object(logs.subprocess, "call", return_value=0) as call:
            self.assertEqual(self.run_main("-f")[0], 0)
            self.assertEqual(call.call_args[0][0][-1], "-f")

    def test_no_journal_no_files(self):
        with mock.patch.object(logs.shutil, "which", return_value=None):
            code, out, err = self.run_main()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No Momento logs yet", err)
        self.assertIn("momento/logs", err)

    def test_open(self):
        with mock.patch.object(logs, "open_folder", return_value=True) as opened:
            code, out, _err = self.run_main("--open")
        self.assertEqual(code, 0)
        opened.assert_called_once_with(self.dir)
        self.assertTrue(self.dir.is_dir())
        with mock.patch.object(logs, "open_folder", return_value=False):
            code, _out, err = self.run_main("--open")
        self.assertEqual(code, 1)
        self.assertIn("xdg-open", err)

    def test_open_folder_programs(self):
        with mock.patch.object(logs.shutil, "which", return_value=None):
            self.assertFalse(logs.open_folder("/x"))
        with mock.patch.object(logs.shutil, "which", side_effect=lambda n: "/usr/bin/gio" if n == "gio" else None), \
                mock.patch.object(logs.subprocess, "Popen") as popen:
            self.assertTrue(logs.open_folder("/x"))
        self.assertEqual(popen.call_args[0][0], ["gio", "open", "/x"])

    def test_follow(self):
        self.dir.mkdir(parents=True)
        cur = self.dir / "momento.log"
        cur.write_text("old line\n")
        out = io.StringIO()

        def append(text):
            with cur.open("a") as f:
                f.write(text)
        steps = iter([
            lambda: append("new 1\nhalf"),
            lambda: append(" done\n"),
            lambda: (cur.rename(self.dir / "momento.log.1"), cur.write_text("after rotation\n")),
            lambda: (self.dir / "bar.log").write_text("bar line\n"),
        ])
        ticks = []

        def sleep(_s):
            ticks.append(1)
            step = next(steps, None)
            if step:
                step()
        logs.follow([cur, self.dir / "bar.log"], out=out, stop=lambda: len(ticks) > 5, sleep=sleep)
        self.assertEqual(out.getvalue(), "new 1\nhalf done\nafter rotation\nbar line\n")


if __name__ == "__main__":
    unittest.main()
