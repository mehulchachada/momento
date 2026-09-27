"""`momento report`: python3 -m unittest tests.test_report

Everything outside is faked: journalctl, coredumpctl, dmesg and `momento status` go
through a fake ``run``; /proc, /etc and /sys come from a fixture tree; the log files
are fixtures in a temp dir. Nothing here reaches the live daemon, its journal or config.
"""

from __future__ import annotations

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401

import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import cli, codecs, report  # noqa: E402

HOME = "/var/home/alice"
REDACT = dict(home=HOME, user="alice", host="ally-pc")


def redactor():
    return report.Redactor(**REDACT)


class RedactionTest(unittest.TestCase):
    def test_home_user_and_host(self):
        r = redactor()
        text = ("clips: /var/home/alice/Videos/Momento\n"
                "config: /home/alice/.config/momento/config.toml\n"
                "uid=1000(alice) gid=1000(alice) on ally-pc; ally-pc.local answered\n"
                "image: ghcr.io/ublue-os/bazzite-deck:stable\n"
                "/var/home/alice2/other stays, alice-deck stays, Alice stays\n")
        out = r(text)
        self.assertIn("clips: ~/Videos/Momento", out)
        self.assertIn("config: ~/.config/momento/config.toml", out)
        self.assertIn("uid=1000(<user>) gid=1000(<user>) on <host>; <host>.local answered", out)
        self.assertIn("bazzite-deck:stable", out)
        self.assertIn("alice-deck stays, Alice stays", out)
        self.assertNotIn("/var/home/alice/", out)
        self.assertNotIn("ally-pc", out)

    def test_home_itself_and_bazzite_names(self):
        # the Bazzite default: user and computer both "bazzite"; the distro's name stays
        r = report.Redactor(home="/var/home/bazzite", user="bazzite", host="bazzite")
        out = r("Linux: Bazzite 44 (Kinoite)\nhome=/var/home/bazzite\nbazzite-deck:stable\n")
        self.assertEqual(out, "Linux: Bazzite 44 (Kinoite)\nhome=~\nbazzite-deck:stable\n")

    def test_short_user_name_is_left(self):
        out = report.Redactor(home="/home/me", user="me", host="x")("tell me more, /home/me/a")
        self.assertEqual(out, "tell me more, ~/a")

    def test_emails_and_addresses(self):
        out = redactor()(
            "mail alice.smith+x@example.co.uk now\n"
            "Started app-flatpak@1000.service and user@1000.service\n"
            "peer 192.168.1.20:8080 and 10.0.0.1.\n"
            "fe80::1ff:fe23:4567:890a and ::1\n"
            "controller aa:bb:cc:dd:ee:0f\n"
            "Mesa 25.1.4, Bazzite 44.20260831.0, GStreamer 1.26.5, kernel 7.2.1-ogc3.1.fc44.x86_64\n"
            "time 12:30:45 at 2026-09-27T12:30:45+02:00, a 1.2.3.4.5 version\n")
        self.assertIn("mail <email> now", out)
        self.assertIn("app-flatpak@1000.service and user@1000.service", out)
        self.assertIn("peer <ip>:8080 and <ip>.", out)
        self.assertIn("<ip> and <ip>", out)
        self.assertIn("controller <mac>", out)
        self.assertIn("Mesa 25.1.4, Bazzite 44.20260831.0, GStreamer 1.26.5, kernel 7.2.1-ogc3.1.fc44.x86_64", out)
        self.assertIn("time 12:30:45 at 2026-09-27T12:30:45+02:00, a 1.2.3.4.5 version", out)

    def test_window_titles(self):
        out = redactor()(
            "   record: window: Inbox (3) - alice@example.com - Firefox\n"
            "2026-09-01 10:00:00,1 momento.daemon INFO: recording window: Secret Chat\n"
            "momento.windowname DEBUG: window title from KDE 1 restore data: 'Bank - Chromium'\n"
            "2026-09-27 12:30:45 daemon INFO daemon: recording window (window title: <redacted, 10 chars>)\n")
        self.assertIn("   record: window (window title: <redacted, 39 chars>)", out)
        self.assertIn("INFO: recording window (window title: <redacted, 11 chars>)", out)
        self.assertIn("restore data (window title: <redacted, 15 chars>)", out)
        self.assertIn("daemon: recording window (window title: <redacted, 10 chars>)", out)   # already hidden
        for secret in ("Inbox", "Secret Chat", "Bank", "alice@example.com"):
            self.assertNotIn(secret, out)


class FakeRun:
    """Stands in for report.run: {program or (program, arg): (code, out, err) | None}."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, cmd, timeout=20, env=None):
        self.calls.append(list(cmd))
        if cmd[0] == sys.executable:                 # python -m momento status|settings
            key = ("momento", cmd[-1])
        elif cmd[0] == "journalctl":
            key = ("journalctl", "-k" if "-k" in cmd else cmd[cmd.index("-b") + 1])
        else:
            key = cmd[0]
        return self.table.get(key, self.table.get(cmd[0]))


def fixture_root(d: Path) -> Path:
    """A tiny /etc, /proc and /sys: one AMD GPU on renderD128, a 1080p panel."""
    (d / "etc").mkdir(parents=True)
    (d / "etc/os-release").write_text('NAME="Bazzite"\nPRETTY_NAME="Bazzite 44 (FROM Fedora Kinoite)"\n'
                                      'VERSION="44.20260831.0 (Kinoite)"\nID=bazzite\n')
    (d / "proc").mkdir()
    (d / "proc/cpuinfo").write_text("processor\t: 0\nmodel name\t: AMD Ryzen Z1 Extreme\n")
    (d / "proc/meminfo").write_text("MemTotal:       16069108 kB\nMemFree: 1 kB\n")
    drm = d / "sys/class/drm"
    dev = d / "sys/devices/pci0000:00/0000:c4:00.0"
    (dev / "driver_target").mkdir(parents=True)
    (dev / "vendor").write_text("0x1002\n")
    (dev / "device").write_text("0x15BF\n")
    drivers = d / "sys/bus/pci/drivers/amdgpu"
    drivers.mkdir(parents=True)
    os.symlink(drivers, dev / "driver")
    (drm / "renderD128").mkdir(parents=True)
    os.symlink(dev, drm / "renderD128/device")
    panel = drm / "card1-eDP-1"
    panel.mkdir()
    (panel / "status").write_text("connected\n")
    (panel / "enabled").write_text("enabled\n")
    (panel / "modes").write_text("1920x1080\n1280x720\n")
    return d


DETECTION = {"version": codecs.PROBE_VERSION, "vendor": "amd", "checked": 1790000000,
             "present": ["vah264enc", "vah265enc", "vaav1enc"],
             "works": {"vah264enc": True, "vah265enc": True, "vaav1enc": True},
             "key": {"driver": "Mesa Gallium driver 25.1.4 for AMD Radeon Graphics (radeonsi, phoenix)",
                     "gst": "GStreamer 1.26.5"}}

STATUS_OUT = (f"    state: recording\n   record: window: My Bank - Firefox\n"
              f"   output: {HOME}/Videos/Momento\n")
KERNEL_OUT = ("2026-09-27T10:00:00 kernel: usb 1-1: new device\n"
              "2026-09-27T10:05:00 kernel: amdgpu 0000:c4:00.0: amdgpu: ring vcn_unified_0 timeout, signaled seq=1\n"
              "2026-09-27T10:05:01 kernel: amdgpu 0000:c4:00.0: amdgpu: GPU reset begin!\n"
              "2026-09-27T10:06:00 kernel: wlan0: associated with 192.168.1.1\n")


class ReportBuildTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = fixture_root(self.tmp / "root")
        self.logdir = self.tmp / "logs"
        self.logdir.mkdir()
        (self.logdir / "momento.log").write_text(
            f"2026-09-27 10:00:00 daemon INFO daemon: Momento 0.1.0 daemon running (socket {HOME}/x)\n"
            "2026-09-27 10:00:05 daemon INFO pipeline: starting capture: source=portal encoder=vaav1enc\n")
        (self.logdir / "bar.log").write_text("2026-09-27 10:01:00 bar INFO overlay: clip bar loaded (layer-shell)\n")
        self.env = {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland"}
        # the program lookups inside (lspci) never find anything real either
        self.enterContext(mock.patch.object(report.shutil, "which", return_value=None))

    def table(self, over=None, **more):
        t = {
            ("momento", "status"): (0, STATUS_OUT, ""),
            ("momento", "settings"): (0, f"    clips: {HOME}/Videos/Momento\n   config: {HOME}/.config/momento/config.toml\n", ""),
            ("journalctl", "0"): (0, "2026-09-27T10:00:00+0200 momento[12]: this boot line\n", ""),
            ("journalctl", "-1"): (0, "2026-09-26T22:00:00+0200 momento[9]: previous boot line\n", ""),
            ("journalctl", "-k"): (0, KERNEL_OUT, ""),
            "coredumpctl": (0, "TIME PID UID GID SIG COREFILE EXE SIZE\n"
                               "Sat 2026-09-26 21:00:00 CEST 4242 1000 1000 SIGSEGV present /usr/bin/python3.14 12M\n", ""),
            "plasmashell": (0, "plasmashell 6.6.1\n", ""),
            "lspci": (0, 'c4:00.0 "Display controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Phoenix1" -rc0\n', ""),
        }
        t.update(over or {})
        t.update(more)
        return t

    def build(self, table=None, detection=DETECTION):
        run = FakeRun(self.table() if table is None else table)
        text = report.build(run=run, now=datetime(2026, 9, 27, 12, 30), root=self.root, env=self.env,
                            log_dir=self.logdir, redactor=redactor(), detection=detection)
        return text, run

    def test_sections_and_redaction(self):
        text, run = self.build()
        self.assertTrue(text.startswith("Momento problem report, made 2026-09-27 12:30\n"))
        self.assertIn("What's in this file:", text)
        self.assertIn("Nothing is uploaded; you choose whether to share it.", text)
        for title in ("About this PC", "Momento status", "Momento settings", "Video formats",
                      "Momento's log (system journal", "Momento's log files", "Graphics driver messages",
                      "Momento crashes"):
            self.assertIn(f"== {title}", text)
        # about this PC, from the fixture tree
        self.assertIn("Linux:     Bazzite 44 (FROM Fedora Kinoite), 44.20260831.0 (Kinoite)", text)
        self.assertIn("Desktop:   KDE, wayland, plasmashell 6.6.1", text)
        self.assertIn("Processor: AMD Ryzen Z1 Extreme", text)
        self.assertIn("Memory:    15.3 GB", text)
        self.assertIn("Screen:    1920x1080 (largest connected display)", text)
        self.assertIn("Graphics:  AMD [1002:15bf], driver amdgpu", text)
        self.assertIn("driver version: Mesa Gallium driver 25.1.4", text)
        self.assertIn("GStreamer: GStreamer 1.26.5", text)
        # Momento: status (title hidden) and settings (home folder as ~)
        self.assertIn("   record: window (window title: <redacted, 17 chars>)", text)
        self.assertIn("    clips: ~/Videos/Momento", text)
        self.assertNotIn("My Bank", text)
        self.assertNotIn(HOME, text)
        # formats, journal (previous boot first), files, kernel (filtered), crashes
        self.assertIn("AV1:       yes (vaav1enc)", text)
        self.assertIn("Graphics:  AMD\n", text)
        self.assertIn("Auto:      AV1", text)
        self.assertLess(text.index("-- previous boot --"), text.index("previous boot line"))
        self.assertLess(text.index("previous boot line"), text.index("-- this boot --"))
        self.assertLess(text.index("-- this boot --"), text.index("this boot line"))
        self.assertIn("-- momento.log (last 2 lines) --", text)
        self.assertIn("-- bar.log (last 1 line) --", text)
        self.assertIn("daemon running (socket ~/x)", text)
        self.assertIn("ring vcn_unified_0 timeout", text)
        self.assertIn("GPU reset begin!", text)
        self.assertNotIn("usb 1-1", text)
        self.assertNotIn("wlan0", text)
        self.assertIn("SIGSEGV present /usr/bin/python3.14", text)
        # the crash list only, for Momento's unit, never a dump
        crash = next(c for c in run.calls if c[0] == "coredumpctl")
        self.assertEqual(crash[:2], ["coredumpctl", "list"])
        self.assertIn("COREDUMP_USER_UNIT=momento.service", crash)
        self.assertNotIn("info", crash)
        self.assertNotIn("dump", crash)

    def test_journal_budget(self):
        many = "".join(f"line {i}\n" for i in range(report.JOURNAL_LINES))
        text, run = self.build(self.table({("journalctl", "0"): (0, many, "")}))
        self.assertNotIn("-- previous boot --", text)          # this boot filled the budget
        self.assertFalse(any("-1" in c for c in run.calls if c[0] == "journalctl" and "-k" not in c))

    def test_graceful_without_tools(self):
        table = {("momento", "status"): (1, "", "momento: daemon is not running (start it with `momento daemon`)\n"),
                 ("momento", "settings"): (0, "   record: window\n", "")}
        text, _run = self.build(table, detection={})
        self.assertIn("journalctl isn't available on this system", text)
        self.assertIn("The kernel log can only be read with admin rights here, so it was skipped.", text)
        self.assertIn("coredumpctl isn't available on this system", text)
        self.assertIn("momento: daemon is not running", text)
        self.assertIn("Not tested yet", text)
        self.assertIn("driver version: unknown", text)

    def test_kernel_log_not_readable(self):
        # journalctl -k shows nothing without the right group; dmesg is restricted
        text, _run = self.build(self.table({("journalctl", "-k"): (0, "", "Hint: not seeing messages"),
                                              "dmesg": (1, "", "dmesg: read kernel buffer failed: "
                                                                 "Operation not permitted\n")}))
        self.assertIn("The kernel log can only be read with admin rights here", text)
        text, _run = self.build(self.table({("journalctl", "-k"): (1, "", "no access"),
                                              "dmesg": (0, KERNEL_OUT, "")}))
        self.assertIn("GPU reset begin!", text)                  # dmesg when journalctl can't
        text, _run = self.build(self.table({("journalctl", "-k"): (0, "kernel: usb 1-1: hello\n", "")}))
        self.assertIn("No graphics driver errors in this boot's kernel log.", text)

    def test_no_crashes_and_unreadable_journal(self):
        text, _run = self.build(self.table({("journalctl", "0"): (1, "", "Failed to open journal\n")},
                                           coredumpctl=(1, "", "No coredumps found.\n")))
        self.assertIn("No crashes recorded.", text)
        self.assertIn("The system log can't be read: Failed to open journal", text)

    def test_a_broken_section_does_not_cost_the_report(self):
        with mock.patch.object(report, "formats_section", side_effect=RuntimeError("boom")):
            text, _run = self.build()
        self.assertIn("(this part failed: RuntimeError: boom)", text)
        self.assertIn("== Momento crashes", text)

    def test_no_log_files(self):
        empty = self.tmp / "none"
        self.assertEqual(len(report.files_section(empty)), 1)
        self.assertIn("No log files in", report.files_section(empty)[0])

    def test_write(self):
        run = FakeRun(self.table())
        path = report.write(self.tmp / "out.txt", run=run, root=self.root, env=self.env, log_dir=self.logdir,
                            redactor=redactor(), detection=DETECTION)
        self.assertEqual(path, self.tmp / "out.txt")
        self.assertIn("== About this PC ==", path.read_text())
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".")], [])   # no temp left
        self.assertEqual(report.default_path(datetime(2026, 9, 27, 12, 5), Path("/x")),
                         Path("/x/Momento-report-2026-09-27_12-05.txt"))
        self.assertEqual(report.default_path(datetime(2026, 9, 27, 12, 5)).parent, Path.home())

    def test_momento_output_runs_this_package(self):
        run = FakeRun({("momento", "status"): (0, "state: recording\n", "")})
        seen = {}

        def spy(cmd, timeout=20, env=None):
            seen.update(cmd=cmd, env=env)
            return run(cmd, timeout, env)
        self.assertEqual(report.momento_output(["status"], "/c.toml", spy), ["state: recording"])
        self.assertEqual(seen["cmd"], [sys.executable, "-m", "momento", "--config", "/c.toml", "status"])
        self.assertTrue(seen["env"]["PYTHONPATH"].startswith(str(report.PACKAGE_DIR.parent)))
        self.assertEqual(report.momento_output(["status"], None, lambda *a, **k: None),
                         ["could not run `momento status`"])

    def test_install_method(self):
        data = self.tmp / "data"
        pkg = data / "momento" / "momento"
        pkg.mkdir(parents=True)
        (data / "momento" / "VERSION").write_text("v1.0.0\n")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
            self.assertEqual(report.install_method(pkg, env={}).split(",")[0], "install.sh (v1.0.0)")
        self.assertEqual(report.install_method(Path("/usr/share/momento/momento"), env={}),
                         "distro package (/usr/share/momento)")
        self.assertEqual(report.install_method(pkg, env={"FLATPAK_ID": "x"}), "Flatpak")


class ReportCommandTest(unittest.TestCase):
    def test_report_command(self):
        out = io.StringIO()
        with mock.patch.object(report, "write", return_value=Path("/h/Momento-report-x.txt")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["report"]), 0)
        self.assertEqual(out.getvalue(), "/h/Momento-report-x.txt\nAttach this file to your issue on GitHub\n")
        err = io.StringIO()
        with mock.patch.object(report, "write", side_effect=OSError("read-only")), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["report"]), 1)
        self.assertIn("couldn't write the report: read-only", err.getvalue())


if __name__ == "__main__":
    unittest.main()
