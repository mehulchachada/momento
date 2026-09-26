"""Core tests: python3 -m unittest discover -s tests (or python3 -m unittest tests.test_core)."""

from __future__ import annotations

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import durations  # noqa: E402
from momento.ringbuffer import RingBuffer, Segment, Selection  # noqa: E402


class DurationsTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(durations.parse("15s"), 15)
        self.assertEqual(durations.parse("5m"), 300)
        self.assertEqual(durations.parse("1h"), 3600)
        self.assertEqual(durations.parse("90"), 90)
        self.assertEqual(durations.parse(" 2 min "), 120)
        for bad in ("", "abc", "5x", "0s", "2h", "-5s"):
            with self.assertRaises(ValueError, msg=bad):
                durations.parse(bad)

    def test_presets_parse_to_their_seconds(self):
        for seconds, lab in durations.PRESETS:
            self.assertEqual(durations.parse(lab), seconds)
            self.assertEqual(durations.label(seconds), lab if lab != "60m" else "60m")

    def test_label_and_clock(self):
        self.assertEqual(durations.label(15), "15s")
        self.assertEqual(durations.label(60), "1m")
        self.assertEqual(durations.label(200), "3m20s")
        self.assertEqual(durations.label(299.6), "5m")
        self.assertEqual(durations.clock(75), "1:15")
        self.assertEqual(durations.clock(3600), "1:00:00")


def _fill(ring: RingBuffer, d: Path, n: int, length: float = 10.0, base: float = 1000.0) -> list[Path]:
    paths = []
    for i in range(n):
        p = d / f"seg{i:05d}.ts"
        p.write_bytes(b"x")
        ring.opened(p, base + i * length)
        ring.closed(p, base + (i + 1) * length)
        paths.append(p)
    return paths


class RingBufferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="momento-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_select_offsets(self):
        ring = RingBuffer(max_seconds=3600)
        paths = _fill(ring, self.tmp, 6)  # [1000, 1060)
        sel = ring.select(1023.0, 1047.0)
        self.assertEqual([s.path for s in sel.segments], paths[2:5])
        self.assertAlmostEqual(sel.offset, 3.0)
        self.assertAlmostEqual(sel.duration, 24.0)
        self.assertAlmostEqual(sel.start, 1023.0)
        self.assertAlmostEqual(sel.end, 1047.0)
        ring.release(sel)

    def test_select_clamps_to_available(self):
        ring = RingBuffer(max_seconds=3600)
        _fill(ring, self.tmp, 3)  # [1000, 1030)
        sel = ring.select(1030.0 - 300, 1030.0)
        self.assertAlmostEqual(sel.offset, 0.0)
        self.assertAlmostEqual(sel.duration, 30.0)
        ring.release(sel)
        self.assertAlmostEqual(ring.buffered_seconds(), 30.0)

    def test_select_ignores_open_segment_and_empty(self):
        ring = RingBuffer(max_seconds=3600)
        self.assertIsNone(ring.select(0, 1e12))
        p = self.tmp / "open.ts"
        p.write_bytes(b"x")
        ring.opened(p, 1000.0)
        self.assertIsNone(ring.select(0, 1e12))
        self.assertEqual(ring.buffered_seconds(), 0.0)

    def test_prune_past_max(self):
        ring = RingBuffer(max_seconds=30, margin=0)
        paths = _fill(ring, self.tmp, 10)  # [1000, 1100); cutoff 1070
        alive = [p for p in paths if p.exists()]
        self.assertEqual(alive, paths[6:])
        self.assertLessEqual(ring.buffered_seconds(), 30)

    def test_pinning_prevents_prune(self):
        ring = RingBuffer(max_seconds=30, margin=0)
        paths = _fill(ring, self.tmp, 4)  # [1000, 1040)
        sel = ring.select(1000.0, 1015.0)  # pins seg0, seg1
        self.assertEqual([s.path for s in sel.segments], paths[:2])
        more = _fill_from(ring, self.tmp, 4, 6)  # up to 1100 -> cutoff 1070
        self.assertTrue(paths[0].exists() and paths[1].exists())
        self.assertFalse(paths[2].exists())
        ring.release(sel)
        self.assertFalse(paths[0].exists() or paths[1].exists())
        self.assertTrue(more[-1].exists())


def _fill_from(ring, d, start_index, count, length=10.0, base=1000.0):
    paths = []
    for i in range(start_index, start_index + count):
        p = d / f"seg{i:05d}.ts"
        p.write_bytes(b"x")
        ring.opened(p, base + i * length)
        ring.closed(p, base + (i + 1) * length)
        paths.append(p)
    return paths


# --- exporter -------------------------------------------------------------------

SEG_SECONDS = 2
SEG_COUNT = 6


def _have(*tools) -> bool:
    return all(shutil.which(t) for t in tools)


def _gst_has(element: str) -> bool:
    return subprocess.run(["gst-inspect-1.0", element], capture_output=True).returncode == 0


def make_segments(d: Path, width: int = 320, height: int = 240, pattern: str = "ball") -> list[Path]:
    """~SEG_COUNT keyframe-aligned 2 s MPEG-TS segments with H.264 + AAC (1 s GOP).

    Each call is its own "capture session": timestamps start over.
    """
    frames = SEG_COUNT * SEG_SECONDS * 30
    audio_bufs = int(SEG_COUNT * SEG_SECONDS * 44100 / 1024)
    encoders = ["x264enc tune=zerolatency key-int-max=30", "vah264enc key-int-max=30",
                "openh264enc gop-size=30"]
    if _have("gst-launch-1.0", "gst-inspect-1.0") and _gst_has("avenc_aac"):
        for enc in encoders:
            if not _gst_has(enc.split()[0]):
                continue
            cmd = (
                f"gst-launch-1.0 -q -e videotestsrc num-buffers={frames} pattern={pattern} "
                f"! video/x-raw,width={width},height={height},framerate=30/1 ! videoconvert ! {enc} "
                f"! h264parse config-interval=-1 ! queue ! mux.video "
                f"audiotestsrc num-buffers={audio_bufs} wave=ticks ! audioconvert ! audioresample "
                f"! avenc_aac ! aacparse ! queue ! mux.audio_0 "
                f"splitmuxsink name=mux muxer=mpegtsmux max-size-time={SEG_SECONDS * 10**9} "
                f"location={d}/seg%05d.ts"
            )
            if subprocess.run(cmd, shell=True, capture_output=True, timeout=120).returncode == 0:
                segs = sorted(d.glob("seg*.ts"))
                if len(segs) >= SEG_COUNT - 1:
                    return segs
            for p in d.glob("seg*.ts"):
                p.unlink()
    # Fallback: ffmpeg's own segmenter (keeps continuous timestamps too).
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30:duration={SEG_COUNT * SEG_SECONDS}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={SEG_COUNT * SEG_SECONDS}",
         "-c:v", "libx264", "-g", "30", "-c:a", "aac",
         "-f", "segment", "-segment_time", str(SEG_SECONDS), "-segment_format", "mpegts",
         str(d / "seg%05d.ts")],
        check=True, capture_output=True, timeout=120,
    )
    return sorted(d.glob("seg*.ts"))


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,start_time,duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def first_video_packet(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
         "-show_entries", "packet=pts_time,flags", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)["packets"][0]


@unittest.skipUnless(_have("ffmpeg", "ffprobe"), "ffmpeg/ffprobe not installed")
class ExporterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="momento-export-"))
        cls.segdir = cls.tmp / "buffer"
        cls.segdir.mkdir()
        try:
            cls.paths = make_segments(cls.segdir)
        except (subprocess.SubprocessError, OSError) as e:
            raise unittest.SkipTest(f"cannot generate fixture footage: {e}")
        if len(cls.paths) < 4:
            raise unittest.SkipTest("fixture generation produced too few segments")
        cls.base = 1_000_000.0
        cls.segments = [
            Segment(p, cls.base + i * SEG_SECONDS, cls.base + (i + 1) * SEG_SECONDS)
            for i, p in enumerate(cls.paths)
        ]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _check(self, out: Path, expected: float):
        from momento import exporter  # noqa: F401

        info = probe(out)
        types = sorted(s["codec_type"] for s in info["streams"])
        self.assertEqual(types, ["audio", "video"])
        duration = float(info["format"]["duration"])
        self.assertLess(abs(duration - expected), 1.0, f"duration {duration} vs {expected}")
        for s in info["streams"]:
            self.assertLess(abs(float(s["start_time"])), 0.1, f"{s['codec_type']} starts late: {s}")
        pkt = first_video_packet(out)
        self.assertIn("K", pkt["flags"], "first video packet is not a keyframe")
        # Keyframe is the first thing shown (pts may trail 0 by the B-frame delay).
        self.assertLess(abs(float(pkt["pts_time"])), 0.1)
        self.assertFalse(any(p.name.startswith(".") for p in out.parent.iterdir()), "temp files left behind")
        # Decodes cleanly from the first packet (no missing-reference errors).
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"],
                             capture_output=True, text=True)
        self.assertEqual((dec.returncode, dec.stderr.strip()), (0, ""))

    def test_export_mid_segment(self):
        from momento import exporter

        ring = RingBuffer(3600)
        ring._segments = [Segment(s.path, s.start, s.end) for s in self.segments]
        since = self.base + SEG_SECONDS + 1.3
        until = since + 5.5
        sel = ring.select(since, until)
        self.assertAlmostEqual(sel.offset, 1.3)
        out = exporter.export(sel, self.tmp / "out" / "mid.mp4")
        ring.release(sel)
        self.assertTrue(out.exists())
        self._check(out, 5.5)

    def test_export_whole_buffer(self):
        from momento import exporter

        segs = self.segments[:-1]  # the last one may be ragged; mirror a closed buffer
        total = segs[-1].end - segs[0].start
        sel = Selection(segs, offset=0.0, duration=total, start=segs[0].start, end=segs[-1].end)
        out = exporter.export(sel, self.tmp / "out" / "all.mp4")
        self._check(out, total)

    def test_export_tail(self):
        from momento import exporter

        segs = self.segments[-3:-1]
        sel = Selection(segs, offset=0.6, duration=3.0, start=segs[0].start + 0.6, end=segs[0].start + 3.6)
        out = exporter.export(sel, self.tmp / "out" / "tail.mp4")
        self._check(out, 3.0)

    def test_missing_segment_raises(self):
        from momento import exporter

        seg = Segment(self.tmp / "nope.ts", 0.0, 2.0)
        sel = Selection([seg], 0.0, 2.0, 0.0, 2.0)
        with self.assertRaises(exporter.ExportError):
            exporter.export(sel, self.tmp / "out" / "fail.mp4")
        self.assertFalse((self.tmp / "out" / "fail.mp4").exists())


class OutputPathTest(unittest.TestCase):
    def test_unique_and_template(self):
        from momento import exporter

        tmp = Path(tempfile.mkdtemp(prefix="momento-out-"))
        try:
            cfg = {"output": {"dir": str(tmp / "clips"), "filename": "Replay_{date}_{time}_{length}.mp4"}}
            when = datetime(2026, 9, 26, 18, 5, 7)
            p1 = exporter.output_path(cfg, 300, when)
            self.assertEqual(p1.name, "Replay_2026-09-26_18-05-07_5m.mp4")
            self.assertTrue(p1.parent.is_dir())
            p1.touch()
            p2 = exporter.output_path(cfg, 300, when)
            self.assertEqual(p2.name, "Replay_2026-09-26_18-05-07_5m_2.mp4")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --- ipc --------------------------------------------------------------------------

def _gi_available() -> bool:
    try:
        from gi.repository import GLib  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


@unittest.skipUnless(_gi_available(), "PyGObject not available")
class IPCTest(unittest.TestCase):
    def setUp(self):
        from gi.repository import GLib

        from momento import ipc

        self.tmp = Path(tempfile.mkdtemp(prefix="momento-ipc-"))
        self.sock = self.tmp / "test.sock"
        self.ctx = GLib.MainContext.default()
        self.loop = GLib.MainLoop()
        self.handler_threads = []

        def handler(msg, reply):
            self.handler_threads.append(threading.current_thread())
            if msg.get("cmd") == "echo":
                reply({"ok": True, "echo": msg.get("value")})
            elif msg.get("cmd") == "later":
                # Asynchronous reply from the main loop, after a delay.
                GLib.timeout_add(200, lambda: (reply({"ok": True, "late": True}), False)[1])
            elif msg.get("cmd") == "boom":
                raise RuntimeError("kaboom")
            else:
                reply({"ok": False, "error": "unknown"})

        self.server = ipc.Server(self.sock, handler)
        self.server.start()
        self.loop_thread = threading.Thread(target=self.loop.run, daemon=True)
        self.loop_thread.start()
        # Wait until the loop dispatches: a quit() that lands before run() starts is
        # lost, and the leaked thread would keep running the default context under
        # later tests (e.g. test_gamepad's GLib adapter test).
        running = threading.Event()
        GLib.idle_add(lambda: running.set() or False)
        self.assertTrue(running.wait(5), "GLib main loop thread did not start")
        self.ipc = ipc

    def tearDown(self):
        self.server.close()
        self.loop.quit()
        self.loop_thread.join(2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        r = self.ipc.request({"cmd": "echo", "value": [1, "two"]}, timeout=5, path=self.sock)
        self.assertEqual(r, {"ok": True, "echo": [1, "two"]})
        self.assertEqual(oct(os.stat(self.sock).st_mode & 0o777), "0o600")
        self.assertIs(self.handler_threads[0], self.loop_thread)

    def test_async_reply_and_concurrency(self):
        results = {}

        def call(name, cmd):
            results[name] = self.ipc.request({"cmd": cmd}, timeout=5, path=self.sock)

        threads = [threading.Thread(target=call, args=(f"l{i}", "later")) for i in range(3)]
        threads.append(threading.Thread(target=call, args=("e", "echo")))
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(results["l0"], {"ok": True, "late": True})
        self.assertEqual(results["e"]["ok"], True)

    def test_handler_exception_becomes_error(self):
        with self.assertLogs("momento.ipc", "ERROR"):
            r = self.ipc.request({"cmd": "boom"}, timeout=5, path=self.sock)
        self.assertFalse(r["ok"])
        self.assertIn("kaboom", r["error"])

    def test_not_running(self):
        with self.assertRaises(self.ipc.DaemonNotRunning):
            self.ipc.request({"cmd": "status"}, timeout=1, path=self.tmp / "missing.sock")
        stale = self.tmp / "stale.sock"
        import socket

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(stale))
        s.close()  # socket file remains, nobody listening
        with self.assertRaises(self.ipc.DaemonNotRunning):
            self.ipc.request({"cmd": "status"}, timeout=1, path=stale)
        # A new server takes over a stale socket file.
        server = self.ipc.Server(stale, lambda m, r: r({"ok": True}))
        server.start()
        try:
            self.assertEqual(self.ipc.request({"cmd": "x"}, timeout=5, path=stale), {"ok": True})
        finally:
            server.close()
        self.assertFalse(stale.exists())

    def test_refuses_to_steal_live_socket(self):
        second = self.ipc.Server(self.sock, lambda m, r: None)
        with self.assertRaises(RuntimeError):
            second.start()
        # The failed second server shutting down must leave the live socket alone.
        second.close()
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(self.ipc.request({"cmd": "echo", "value": 1}, timeout=5, path=self.sock)["echo"], 1)


class CLITest(unittest.TestCase):
    def test_parser(self):
        from momento import cli

        args = cli.build_parser().parse_args(["save", "5m"])
        self.assertEqual((args.command, args.duration), ("save", 300))
        args = cli.build_parser().parse_args(["-v", "--config", "/x.toml", "status"])
        self.assertEqual((args.command, args.verbose, str(args.config)), ("status", 1, "/x.toml"))
        import contextlib
        import io

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.build_parser().parse_args(["save", "3h"])
        for argv in (["set", "audio_source", "off"], ["set", "mic", "on"], ["set", "mic_device", "default"],
                     ["pause"], ["resume"], ["stop"], ["quit"], ["screenshot"]):
            self.assertEqual(cli.build_parser().parse_args(argv).command, argv[0])

    def test_screenshot(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, ipc

        sent = []

        def fake(reply):
            def request(msg, timeout=None, **_):
                sent.append((msg, timeout))
                if isinstance(reply, Exception):
                    raise reply
                return reply
            return request

        ok = {"ok": True, "path": "/v/Momento/Images/Momento_2026-09-26_21-04-11.png", "width": 1920, "height": 1080}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ipc, "request", fake(ok)), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["screenshot"]), 0)
        self.assertEqual(out.getvalue().strip(), ok["path"])
        self.assertEqual(sent[0][0], {"cmd": "screenshot"})
        refused = {"ok": False, "code": "not_recording", "error": "Not recording"}
        with mock.patch.object(ipc, "request", fake(refused)), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["screenshot"]), 1)
            with mock.patch.object(ipc, "request", fake(ipc.DaemonNotRunning("no socket"))):
                self.assertEqual(cli.main(["screenshot"]), 1)
        self.assertIn("Not recording", err.getvalue())

    def test_set_writes_config(self):
        import contextlib
        import io

        from momento import cli, config, ipc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            orig = ipc.request

            def not_running(*a, **k):
                raise ipc.DaemonNotRunning("no")
            ipc.request = not_running
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["--config", str(path), "set", "audio_source", "off"]), 0)
                    self.assertEqual(cli.main(["--config", str(path), "set", "resolution", "4k"]), 0)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["--config", str(path), "set", "quality", "insane"]), 1)
            finally:
                ipc.request = orig
            cfg = config.load(path)
            self.assertFalse(cfg["audio"]["desktop"])
            self.assertEqual(cfg["capture"]["resolution"], "2160p")
            self.assertEqual(cfg["capture"]["quality"], "high")

    def test_set_storage(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, config, ipc, storage

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            # daemon off: saved anyway, warning only
            err = io.StringIO()
            with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                    mock.patch.object(storage, "free_bytes", return_value=10), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["--config", str(path), "set", "quality", "ultra"]), 0)
            self.assertIn("won't record", err.getvalue())
            self.assertEqual(config.load(path)["capture"]["quality"], "ultra")
            # daemon refuses: fatal, message shown
            err = io.StringIO()
            refusal = {"ok": False, "code": "no_storage", "error": "2160p Ultra needs 35.2 GB free, 9.4 GB available"}
            with mock.patch.object(ipc, "request", return_value=refusal) as req, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["--config", str(path), "set", "resolution", "4k"]), 1)
            req.assert_called_once_with({"cmd": "configure", "changes": {"resolution": "2160p"}}, timeout=30)
            self.assertIn("35.2 GB", err.getvalue())

    def test_set_controller(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, config, ipc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            config.set_value("controller", "open_chord", ["left_paddle"], path)
            out = io.StringIO()
            reply = {"ok": True, "changed": {"controller": "left_paddle"}, "restarted": False, "paused": True}
            with mock.patch.object(ipc, "request", return_value=reply) as req, contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--config", str(path), "set", "controller", "on"]), 0)
            req.assert_called_once_with({"cmd": "configure", "changes": {"controller": "on"}}, timeout=30)
            text = out.getvalue()
            self.assertIn("controller = left_paddle", text)
            self.assertIn("Hold Left paddle (0.3 s) to open or close the bar", text)
            self.assertNotIn("paused", text)             # a controller change never waits for resume
            out = io.StringIO()
            with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--config", str(path), "set", "controller", "off"]), 0)
                self.assertEqual(cli.main(["--config", str(path), "settings"]), 0)
            self.assertIn("controller: off", out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["set", "controllr", "on"])

    def test_storage_line(self):
        from momento import cli

        line = cli.storage_line({"ok": False, "free": 3_100_000_000, "required": 7_200_000_000, "reclaimable": 0})
        self.assertEqual(line, "3.1 GB free, needs 7.2 GB \u2014 not enough")
        line = cli.storage_line({"ok": True, "free": 3e9, "required": 7e9, "reclaimable": 5e9})
        self.assertEqual(line, "3.0 GB free + 5.0 GB buffer, needs 7.0 GB \u2014 ok")


SINKS_JSON = json.dumps([
    {"index": 36, "name": "ROG Ally", "description": "ROG Ally", "monitor_source": "ROG Ally.monitor",
     "properties": {"media.class": "Audio/Sink", "device.description": "ROG Ally"}},
    {"index": 58, "name": "alsa_output.pci-0000_09_00.1.hdmi-stereo",
     "description": "Radeon High Definition Audio Controller Digital Stereo (HDMI)",
     "monitor_source": "alsa_output.pci-0000_09_00.1.hdmi-stereo.monitor", "properties": {}},
])
SOURCES_JSON = json.dumps([
    {"index": 36, "name": "ROG Ally.monitor", "description": "Monitor of ROG Ally",
     "monitor_source": "ROG Ally", "properties": {"device.class": "monitor", "media.class": "Audio/Sink"}},
    {"index": 58, "name": "alsa_output.pci-0000_09_00.1.hdmi-stereo.monitor",
     "description": "Monitor of Radeon HDMI", "properties": {}},
    {"index": 70, "name": "alsa_input.usb-Blue_Yeti.analog-stereo", "description": "Yeti Stereo Microphone",
     "properties": {"device.class": "sound", "media.class": "Audio/Source"}},
])


class SettingsTest(unittest.TestCase):
    def setUp(self):
        from momento import config, settings

        self.config, self.settings = config, settings
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"
        self.path.write_text("# Momento\n[capture]\n# pick one\nresolution = \"1080p\"\n\n[audio]\ndesktop = true\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_validate_normalizes(self):
        v = self.settings.validate({"resolution": "4K", "quality": "ULTRA", "bitrate": "0",
                                    "audio_source": "@DEFAULT_MONITOR@", "mic": True,
                                    "mic_device": "@DEFAULT_SOURCE@"})
        self.assertEqual(v, {"resolution": "2160p", "quality": "ultra", "bitrate": 0,
                             "audio_source": "default", "mic": "on", "mic_device": "default"})
        self.assertEqual(self.settings.validate({"audio_source": "Off", "mic": "no"}),
                         {"audio_source": "off", "mic": "off"})
        self.assertEqual(self.settings.validate({"audio_source": "ROG Ally.monitor"}),
                         {"audio_source": "ROG Ally.monitor"})

    def test_validate_rejects(self):
        for bad in ({"resolution": "999p"}, {"quality": "max"}, {"bitrate": 500}, {"bitrate": "fast"},
                    {"mic": "maybe"}, {"audio_source": ""}, {"mic_device": "a\nb"}, {"volume": 3}):
            with self.assertRaises(ValueError, msg=bad) as cm:
                self.settings.validate(bad)
            self.assertTrue(str(cm.exception).startswith(next(iter(bad))), cm.exception)
        with self.assertRaises(ValueError):
            self.settings.validate(["resolution"])

    def test_apply_writes_and_keeps_comments(self):
        changed = self.settings.apply({"resolution": "1440p", "quality": "ultra",
                                       "audio_source": "ROG Ally.monitor", "mic": "on",
                                       "mic_device": "alsa_input.usb-Blue_Yeti.analog-stereo"}, self.path)
        self.assertEqual(set(changed), {"resolution", "quality", "audio_source", "mic", "mic_device"})
        text = self.path.read_text()
        self.assertIn("# pick one", text)
        cfg = self.config.load(self.path)
        self.assertEqual(cfg["capture"]["resolution"], "1440p")
        self.assertEqual(cfg["capture"]["quality"], "ultra")
        self.assertEqual(cfg["audio"]["desktop_device"], "ROG Ally.monitor")
        self.assertTrue(cfg["audio"]["microphone"])
        self.assertEqual(cfg["audio"]["microphone_device"], "alsa_input.usb-Blue_Yeti.analog-stereo")
        self.assertEqual(self.settings.current(cfg)["audio_source"], "ROG Ally.monitor")
        # same values again -> nothing changed
        self.assertEqual(self.settings.apply({"resolution": "1440p", "mic": "on"}, self.path), {})
        # off keeps the chosen device for later; default goes back to the placeholder
        self.settings.apply({"audio_source": "off", "mic_device": "default"}, self.path)
        cfg = self.config.load(self.path)
        self.assertFalse(cfg["audio"]["desktop"])
        self.assertEqual(cfg["audio"]["desktop_device"], "ROG Ally.monitor")
        self.assertEqual(cfg["audio"]["microphone_device"], "@DEFAULT_SOURCE@")
        cur = self.settings.current(cfg)
        self.assertEqual((cur["audio_source"], cur["mic"], cur["mic_device"]), ("off", "on", "default"))
        self.settings.apply({"audio_source": "default"}, self.path)
        self.assertEqual(self.settings.current(self.config.load(self.path))["audio_source"], "default")

    def test_apply_is_all_or_nothing(self):
        before = self.path.read_text()
        with self.assertRaises(ValueError):
            self.settings.apply({"resolution": "720p", "quality": "bogus"}, self.path)
        self.assertEqual(self.path.read_text(), before)

    def test_describe(self):
        d = self.settings.describe(self.config.load(self.path), devices={"outputs": [], "inputs": []})
        self.assertTrue(d["ok"])
        self.assertEqual(d["values"]["resolution"], "1080p")
        self.assertIn("native", d["choices"]["resolution"])
        self.assertEqual(d["fps"], 60)
        self.assertEqual(d["config"], str(self.path))

    def test_parse_devices(self):
        d = self.settings.parse_devices(SINKS_JSON, SOURCES_JSON, "ROG Ally", "alsa_input.usb-Blue_Yeti.analog-stereo")
        self.assertEqual(d["outputs"], [
            {"name": "ROG Ally.monitor", "label": "ROG Ally", "default": True},
            {"name": "alsa_output.pci-0000_09_00.1.hdmi-stereo.monitor",
             "label": "Radeon High Definition Audio Controller Digital Stereo (HDMI)", "default": False},
        ])
        self.assertEqual(d["inputs"], [{"name": "alsa_input.usb-Blue_Yeti.analog-stereo",
                                        "label": "Yeti Stereo Microphone", "default": True}])
        self.assertEqual(self.settings.parse_devices("not json", None), {"outputs": [], "inputs": []})

    def test_list_audio_devices(self):
        import shutil as sh
        from unittest import mock

        outputs = {("-f", "json", "list", "sinks"): SINKS_JSON, ("-f", "json", "list", "sources"): SOURCES_JSON,
                   ("get-default-sink",): "alsa_output.pci-0000_09_00.1.hdmi-stereo\n",
                   ("get-default-source",): "alsa_input.usb-Blue_Yeti.analog-stereo\n"}

        def run(argv, **_):
            out = outputs.get(tuple(argv[1:]))
            return subprocess.CompletedProcess(argv, 0 if out is not None else 1, out or "", "")

        with mock.patch.object(sh, "which", return_value="/usr/bin/pactl"):
            d = self.settings.list_audio_devices(run=run)
        self.assertEqual([o["default"] for o in d["outputs"]], [False, True])
        self.assertEqual(len(d["inputs"]), 1)
        with mock.patch.object(sh, "which", return_value=None):
            self.assertEqual(self.settings.list_audio_devices(run=run), {"outputs": [], "inputs": []})

        def broken(argv, **_):
            raise OSError("boom")
        with mock.patch.object(sh, "which", return_value="/usr/bin/pactl"):
            self.assertEqual(self.settings.list_audio_devices(run=broken), {"outputs": [], "inputs": []})


class FakeRecorder:
    instances = []

    def __init__(self, cfg, ring, on_state, bus=None):
        self.cfg, self.on_state = cfg, on_state
        self.recording = False
        self.started = self.stopped = 0
        self.interactive = []  # the interactive flag of every start()
        FakeRecorder.instances.append(self)

    def start(self, interactive=False):
        self.started += 1
        self.interactive.append(interactive)
        self.recording = True
        self.on_state("recording", None)

    def stop(self):
        self.stopped += 1
        self.recording = False
        self.on_state("stopped", None)

    frame = "a frame"   # what grab_frame hands over (None: no frame came)

    def grab_frame(self, callback, timeout=3.0):
        callback(self.frame if self.recording else None)


class _FakeFrame:
    size = (1920, 1080)


class ScreenshotFileTest(unittest.TestCase):
    """Where screenshots go and how they are named: never over an existing file."""

    def test_dir_and_name(self):
        from momento import screenshot

        cfg = {"output": {"dir": "/v/Momento"}}
        self.assertEqual(screenshot.images_dir(cfg), Path("/v/Momento/Images"))
        self.assertEqual(screenshot.file_name(datetime(2026, 9, 26, 21, 4, 11)), "Momento_2026-09-26_21-04-11.png")

    def test_write_new_never_overwrites(self):
        from momento import screenshot

        with tempfile.TemporaryDirectory() as d:
            images = Path(d) / "Momento" / "Images"          # created on first use
            a = screenshot.write_new(images, "Momento_x.png", b"one")
            b = screenshot.write_new(images, "Momento_x.png", b"two")
            (images / "Momento_x_3.png").write_bytes(b"someone else's")
            c = screenshot.write_new(images, "Momento_x.png", b"three")
            self.assertEqual([p.name for p in (a, b, c)], ["Momento_x.png", "Momento_x_2.png", "Momento_x_4.png"])
            self.assertEqual([p.read_bytes() for p in (a, b, c)], [b"one", b"two", b"three"])
            self.assertEqual((images / "Momento_x_3.png").read_bytes(), b"someone else's")
            self.assertEqual(sorted(p.name for p in images.iterdir()),
                             ["Momento_x.png", "Momento_x_2.png", "Momento_x_3.png", "Momento_x_4.png"])


class DaemonControlTest(unittest.TestCase):
    """pause / resume / settings / configure against a fake Recorder (no GStreamer)."""

    def setUp(self):
        import types
        from unittest import mock

        from momento import config, daemon

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"
        # Full screen, so these tests start recording like a login does; window
        # mode (the default) has its own tests.
        self.path.write_text("[capture]\nresolution = \"1080p\"\ntarget = \"screen\"\n")
        fake = types.ModuleType("momento.pipeline")
        fake.Recorder = FakeRecorder
        patcher = mock.patch.dict(sys.modules, {"momento.pipeline": fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeRecorder.instances = []
        from momento import storage

        self.free = 10**13  # plenty of disk unless a test says otherwise
        fb = mock.patch.object(storage, "free_bytes", side_effect=lambda path: self.free)
        fb.start()
        self.addCleanup(fb.stop)
        self.notes = []
        nt = mock.patch.object(daemon, "notify", side_effect=lambda bus, summary, body="", icon="": self.notes.append(summary))
        nt.start()
        self.addCleanup(nt.stop)
        cfg = config.load(self.path)
        cfg["buffer"]["dir"] = str(Path(self._tmp.name) / "buffer")
        self.path.write_text(self.path.read_text() + f"\n[buffer]\ndir = \"{cfg['buffer']['dir']}\"\n")
        self.d = daemon.Daemon(cfg, loop=None)
        self.d.recorder = FakeRecorder(cfg, self.d.ring, self.d._on_state)
        self.d.recorder.start()

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, msg, timeout=5):
        box = []
        done = threading.Event()

        def reply(r):
            box.append(r)
            done.set()
        self.d.handle(msg, reply)
        self.assertTrue(done.wait(timeout))
        return box[0]

    def test_pause_resume(self):
        rec = self.d.recorder
        self.assertEqual(self.d.status()["state"], "recording")
        self.assertEqual(self.call({"cmd": "pause"}), {"ok": True, "state": "paused"})
        st = self.d.status()
        self.assertEqual((st["state"], st["recording"]), ("paused", False))
        self.assertEqual(rec.stopped, 1)
        self.assertEqual(self.call({"cmd": "pause"})["state"], "paused")  # idempotent
        self.assertEqual(rec.stopped, 1)
        r = self.call({"cmd": "resume"})
        self.assertTrue(r["ok"])
        self.assertEqual(rec.started, 2)
        self.assertEqual(self.d.status()["state"], "recording")
        self.call({"cmd": "resume"})
        self.assertEqual(rec.started, 2)

    def test_settings_and_configure(self):
        from momento import config, settings
        from unittest import mock

        with mock.patch.object(settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}):
            r = self.call({"cmd": "settings"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["values"]["resolution"], "1080p")
        self.assertEqual(r["config"], str(self.path))

        bad = self.call({"cmd": "configure", "changes": {"resolution": "999p"}})
        self.assertFalse(bad["ok"])
        self.assertIn("resolution", bad["error"])
        self.assertFalse(self.call({"cmd": "configure", "changes": {}})["ok"])

        r = self.call({"cmd": "configure", "changes": {"resolution": "720p", "mic": "on"}})
        self.assertEqual((r["ok"], r["restarted"]), (True, True))
        self.assertEqual(r["changed"], {"resolution": "720p", "mic": "on"})
        self.assertEqual(config.load(self.path)["capture"]["resolution"], "720p")
        self.assertEqual(self.d.cfg["capture"]["resolution"], "720p")
        self.assertIs(self.d.recorder, FakeRecorder.instances[-1])
        self.assertEqual(self.d.recorder.started, 1)

        r = self.call({"cmd": "configure", "changes": {"resolution": "720p"}})
        self.assertEqual((r["ok"], r["restarted"]), (True, False))  # nothing changed, no restart

        self.call({"cmd": "pause"})
        r = self.call({"cmd": "configure", "changes": {"quality": "ultra"}})
        self.assertEqual((r["ok"], r["restarted"], r["paused"]), (True, False, True))
        self.assertEqual(self.d.recorder.started, 0)  # stays paused
        self.assertEqual(self.d.status()["state"], "paused")
        self.call({"cmd": "resume"})
        self.assertEqual(self.d.recorder.started, 1)

    # --- disk space -----------------------------------------------------------------

    def _need(self, **capture):
        from momento import storage

        cfg = {**self.d.cfg, "capture": {**self.d.cfg["capture"], **capture}}
        return storage.required_bytes(cfg)

    def test_refuses_to_start_then_autostarts(self):
        from momento import storage

        need = self._need()
        self.d.recorder.stop()
        self.d.state = "stopped"
        rec = FakeRecorder(self.d.cfg, self.d.ring, self.d._on_state)
        self.d.recorder = rec
        self.free = need - 1
        self.assertFalse(self.d._start_recorder(reclaimable=0))
        self.assertEqual(rec.started, 0)
        st = self.d.status()
        self.assertEqual(st["state"], "no_storage")
        self.assertFalse(st["recording"])
        self.assertIn("Not enough free space: needs", st["error"])
        self.assertEqual(st["storage"]["required"], need)
        self.assertFalse(st["storage"]["ok"])
        self.assertEqual(set(st["storage"]), {"ok", "free", "required", "reclaimable", "path"})
        self.assertEqual(len(self.notes), 1)
        # resume while blocked: refused with a code, no second notification
        r = self.call({"cmd": "resume"})
        self.assertEqual((r["ok"], r["code"], r["state"]), (False, "no_storage", "no_storage"))
        self.assertIn("needs", r["error"])
        self.d._storage_tick()
        self.assertEqual((rec.started, len(self.notes)), (0, 1))
        # space appears -> the next tick starts capture
        self.free = need
        self.assertTrue(self.d._storage_tick())
        self.assertEqual(rec.started, 1)
        st = self.d.status()
        self.assertEqual(st["state"], "recording")
        self.assertNotIn("error", st)
        self.assertTrue(st["storage"]["ok"])

    def test_own_buffer_counts_as_reclaimable(self):
        need = self._need()
        buf = Path(self.d.cfg["buffer"]["dir"])
        buf.mkdir(parents=True, exist_ok=True)
        (buf / "seg00001.ts").write_bytes(b"x" * 5000)
        self.free = need - 4000
        self.call({"cmd": "pause"})
        r = self.call({"cmd": "resume"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.d.status()["storage"]["reclaimable"], 5000)

    def test_stops_when_disk_runs_low(self):
        from momento import storage

        rec = self.d.recorder
        self.free = storage.LOW_WATER
        self.d._storage_tick()
        self.assertEqual(rec.stopped, 0)
        self.free = storage.LOW_WATER - 1
        self.d._storage_tick()
        self.assertEqual(rec.stopped, 1)
        st = self.d.status()
        self.assertEqual(st["state"], "no_storage")
        self.assertIn("Disk almost full", st["error"])
        self.assertEqual(len(self.notes), 1)
        self.d._storage_tick()
        self.assertEqual(len(self.notes), 1)  # notified once
        # A restart would delete the kept footage, so buffer bytes don't count here.
        buf = Path(self.d.cfg["buffer"]["dir"])
        buf.mkdir(parents=True, exist_ok=True)
        (buf / "seg00001.ts").write_bytes(b"x" * 10_000)
        self.free = self._need() - 5000
        self.d._storage_tick()
        self.assertEqual(rec.started, 1)
        self.free = self._need()
        self.d._storage_tick()
        self.assertEqual(rec.started, 2)
        self.assertEqual(self.d.status()["state"], "recording")

    def test_paused_is_left_alone(self):
        self.call({"cmd": "pause"})
        self.free = 0
        self.d._storage_tick()
        self.assertEqual(self.d.status()["state"], "paused")
        self.assertEqual(self.notes, [])

    def test_save_refused_without_space(self):
        from momento import storage

        buf = Path(self.d.cfg["buffer"]["dir"])
        buf.mkdir(parents=True, exist_ok=True)
        seg = buf / "seg00001.ts"
        seg.write_bytes(b"x" * 1000)
        now = time.time()
        self.d.ring.opened(seg, now - 20)
        self.d.ring.closed(seg, now - 10)
        self.d.recorder.recording = False  # no flush round trip
        self.free = storage.SAVE_MARGIN + 999
        r = self.call({"cmd": "save", "seconds": 30})
        self.assertEqual((r["ok"], r["code"]), (False, "no_storage"))
        self.assertIn("Not enough space to save this clip", r["error"])
        self.assertEqual(self.d.ring._segments[0].pins, 0)  # released
        self.assertEqual(len(self.notes), 1)

    def test_configure_refuses_what_does_not_fit(self):
        from momento import config, storage

        self.free = self._need(resolution="1440p", quality="ultra") - 1
        before = self.path.read_text()
        r = self.call({"cmd": "configure", "changes": {"resolution": "1440p", "quality": "ultra"}})
        self.assertEqual((r["ok"], r["code"]), (False, "no_storage"))
        self.assertTrue(r["error"].startswith("1440p Ultra needs "), r["error"])
        self.assertIn(" available", r["error"])
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.d.recorder.started, 1)
        # force writes anyway; the daemon then sits in no_storage
        r = self.call({"cmd": "configure", "changes": {"resolution": "1440p", "quality": "ultra"}, "force": True})
        self.assertTrue(r["ok"])
        self.assertEqual((r["restarted"], r["state"]), (False, "no_storage"))
        self.assertIn("warning", r)
        self.assertEqual(config.load(self.path)["capture"]["quality"], "ultra")
        # a change that doesn't raise the requirement always goes through
        r = self.call({"cmd": "configure", "changes": {"mic_device": "default"}})
        self.assertTrue(r["ok"])
        r = self.call({"cmd": "configure", "changes": {"quality": "high"}})
        self.assertTrue(r["ok"])
        self.assertEqual((r["restarted"], r["state"]), (True, "recording"))
        self.assertEqual(self.d.status()["storage"]["required"],
                         storage.required_bytes(self.d.cfg))

    def test_settings_reports_requirements(self):
        from momento import settings, storage
        from unittest import mock

        with mock.patch.object(settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}):
            r = self.call({"cmd": "settings"})
        st = r["storage"]
        self.assertEqual(st["free"], self.free)
        self.assertEqual(st["current"], "1080p/high/60")
        self.assertEqual(len(st["required"]), 5 * 3 * 2)
        self.assertEqual(st["required"]["1080p/high/60"], storage.required_bytes(self.d.cfg))


class StorageTest(unittest.TestCase):
    def cfg(self, **capture):
        from momento import config

        cfg = config.load(Path(tempfile.gettempdir()) / "momento-no-such-config.toml")
        cfg["capture"].update(capture)
        return cfg

    def test_buffer_math(self):
        from momento import storage

        cfg = self.cfg(resolution="1080p", quality="high", fps=60)
        video = 15_000 * 1000 / 8 * 3600
        self.assertAlmostEqual(video / 1e9, 6.75)
        audio = 160 * 1000 / 8 * 3600
        self.assertEqual(storage.buffer_bytes(cfg), int((video + audio) * 1.05))
        self.assertEqual(storage.required_bytes(cfg), storage.buffer_bytes(cfg) + (1 << 30))
        self.assertEqual(storage.human(storage.buffer_bytes(cfg)), "7.2 GB")
        cfg["audio"]["desktop"] = cfg["audio"]["microphone"] = False
        self.assertEqual(storage.buffer_bytes(cfg), int(video * 1.05))
        cfg["audio"]["microphone"] = True
        self.assertEqual(storage.audio_kbps(cfg), 160)
        cfg["buffer"]["max_seconds"] = 1800
        self.assertEqual(storage.buffer_bytes(cfg), int((video + audio) / 2 * 1.05))
        # 120 fps costs 1.5x the video bits
        self.assertGreater(storage.buffer_bytes(self.cfg(fps=120)), storage.buffer_bytes(self.cfg(fps=60)))

    def test_human_and_label(self):
        from momento import storage

        self.assertEqual(storage.human(7_200_000_000), "7.2 GB")
        self.assertEqual(storage.human(512 << 20), "536.9 MB")
        self.assertEqual(storage.human(1.5e12), "1.5 TB")
        self.assertEqual(storage.human(12), "12 B")
        self.assertEqual(storage.label(self.cfg(resolution="1440p", quality="ultra")), "1440p Ultra")
        self.assertEqual(storage.label(self.cfg(fps=120)), "1080p High 120 fps")
        self.assertEqual(storage.label(self.cfg(bitrate_kbps=50000)), "1080p at 50 Mbps")

    def test_check_and_requirements(self):
        from unittest import mock

        from momento import storage

        cfg = self.cfg()
        need = storage.required_bytes(cfg)
        with mock.patch.object(storage, "free_bytes", return_value=need - 100):
            chk = storage.check(cfg)
            self.assertFalse(chk["ok"])
            self.assertEqual((chk["free"], chk["required"], chk["reclaimable"]), (need - 100, need, 0))
            self.assertEqual(chk["path"], cfg["buffer"]["dir"])
            self.assertTrue(storage.check(cfg, reclaimable=100)["ok"])
            req = storage.requirements(cfg, reclaimable=7)
        self.assertEqual((req["free"], req["reclaimable"], req["current"]), (need - 100, 7, "1080p/high/60"))
        self.assertEqual(req["required"]["1080p/high/60"], need)
        self.assertLess(req["required"]["720p/standard/60"], req["required"]["2160p/ultra/120"])
        self.assertEqual(len(req["required"]), 30)
        self.assertEqual(cfg["capture"]["resolution"], "1080p")  # not mutated

    def test_free_bytes_uses_existing_parent(self):
        from momento import storage

        with tempfile.TemporaryDirectory() as d:
            self.assertGreater(storage.free_bytes(Path(d) / "a" / "b" / "c"), 0)
            (Path(d) / "x.ts").write_bytes(b"12345")
            self.assertEqual(storage.dir_bytes(d), 5)
            self.assertEqual(storage.dir_bytes(Path(d) / "missing"), 0)


# --- persistent ring buffer / multi-session export --------------------------------

P240 = {"width": 320, "height": 240, "fps": 30, "codec": "h264", "audio": True}
P360 = {"width": 640, "height": 360, "fps": 30, "codec": "h264", "audio": True}


def _feed(ring: RingBuffer, paths, session: str, t0: float, length: float = 10.0, params=None,
          write: bool = True) -> float:
    """Report paths to ring as one capture session starting at wall-clock t0; returns the end."""
    t = t0
    for p in paths:
        if write:
            Path(p).write_bytes(b"x" * 188)
        ring.opened(p, t, session=session, **(params or P240))
        t += length
        ring.closed(p, t)
    return t


def _index_lines(d: Path) -> list[dict]:
    return [json.loads(line) for line in (d / "index.jsonl").read_text().splitlines()]


class LiveBufferedTest(unittest.TestCase):
    """The replay time ticks every second while recording, not every 10 s segment."""

    def test_open_segment_counts_when_live(self):
        from momento.ringbuffer import RingBuffer

        ring = RingBuffer(3600)
        ring.opened("/nonexistent/seg0.ts", 1000.0, session="s")
        ring.closed("/nonexistent/seg0.ts", 1010.0)
        ring.opened("/nonexistent/seg1.ts", 1010.0, session="s")
        self.assertAlmostEqual(ring.buffered_seconds(), 10.0)                       # closed only
        self.assertAlmostEqual(ring.buffered_seconds(live=True, now=1013.5), 13.5)  # + open part
        # a segment left open by a crash never counts more than the cap
        self.assertAlmostEqual(ring.buffered_seconds(live=True, now=5000.0), 10.0 + RingBuffer.LIVE_SEGMENT_CAP)


class PersistentRingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="momento-ring-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seg(self, n: int) -> Path:
        return self.tmp / f"seg{n:08d}.ts"

    def test_index_written_and_reloaded(self):
        ring = RingBuffer(3600, directory=self.tmp)
        self.assertEqual(ring.recover(), 0)
        end = _feed(ring, [self.seg(i) for i in range(3)], "s1", 1000.0)
        _feed(ring, [self.seg(i) for i in range(3, 5)], "s2", end + 500, params=P360)
        lines = _index_lines(self.tmp)
        self.assertEqual([d["file"] for d in lines], [self.seg(i).name for i in range(5)])
        self.assertEqual(lines[0], {"file": self.seg(0).name, "start": 1000.0, "end": 1010.0, "session": "s1",
                                    "width": 320, "height": 240, "fps": 30, "codec": "h264", "audio": True})
        self.assertEqual((lines[4]["session"], lines[4]["width"]), ("s2", 640))

        again = RingBuffer(3600, directory=self.tmp)  # "service restart"
        self.assertEqual(again.recover(), 5)
        self.assertAlmostEqual(again.buffered_seconds(), 50.0)
        sel = again.select_last(3600)
        self.assertEqual([s.path for s in sel.segments], [self.seg(3), self.seg(4)])  # newest compatible tail
        self.assertEqual(sel.segments[0].session, "s2")
        again.release(sel)

    def test_recover_after_crash(self):
        ring = RingBuffer(3600, directory=self.tmp)
        ring.recover()
        _feed(ring, [self.seg(i) for i in range(4)], "s1", 1000.0)
        ring.opened(self.seg(4), 1040.0, session="s1", **P240)  # being written when we "crash"
        self.seg(4).write_bytes(b"partial")
        self.seg(1).unlink()  # missing file
        self.seg(2).write_bytes(b"")  # zero-sized file
        (self.tmp / "seg00000099.ts").write_bytes(b"stray")
        with open(self.tmp / "index.jsonl", "a") as f:
            f.write('{"file": "seg0000')  # torn last line

        again = RingBuffer(3600, directory=self.tmp)
        self.assertEqual(again.recover(), 4)  # numbering continues after seg3
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.ts")), [self.seg(0).name, self.seg(3).name])
        self.assertEqual([d["file"] for d in _index_lines(self.tmp)], [self.seg(0).name, self.seg(3).name])
        self.assertAlmostEqual(again.buffered_seconds(), 20.0)

        # An unfinished segment left by a pipeline that died in this process is dropped too.
        again.opened(self.seg(4), 1040.0, session="s2", **P240)
        self.seg(4).write_bytes(b"partial")
        self.assertEqual(again.recover(), 4)
        self.assertFalse(self.seg(4).exists())

    def test_retention_by_footage_not_wall_clock(self):
        ring = RingBuffer(max_seconds=30, margin=0, directory=self.tmp)
        ring.recover()
        _feed(ring, [self.seg(i) for i in range(3)], "old", 1000.0)
        # Two days later: the wall-clock window would have dropped all of "old".
        _feed(ring, [self.seg(i) for i in range(3, 5)], "new", 1000.0 + 2 * 86400)
        alive = sorted(p.name for p in self.tmp.glob("*.ts"))
        # 20 s of "new" + seg2 (10 s, straddles the 30 s limit) + seg1 (footage newer than it = 30 s).
        self.assertEqual(alive, [self.seg(i).name for i in range(1, 5)])
        self.assertEqual([d["file"] for d in _index_lines(self.tmp)], alive)  # index compacted
        self.assertAlmostEqual(ring.buffered_seconds(), 30.0)
        _feed(ring, [self.seg(5)], "new2", 1000.0 + 3 * 86400)
        self.assertFalse(self.seg(1).exists())
        self.assertEqual(len(_index_lines(self.tmp)), 4)

    def test_select_last_across_gap(self):
        ring = RingBuffer(3600)
        a = [self.seg(i) for i in range(3)]
        b = [self.seg(i) for i in range(3, 5)]
        _feed(ring, a, "a", 1000.0)  # footage 1000-1030
        _feed(ring, b, "b", 5000.0)  # footage 5000-5020 (paused in between)
        sel = ring.select_last(25)
        self.assertEqual([s.path for s in sel.segments], [a[2]] + b)
        self.assertAlmostEqual(sel.offset, 5.0)
        self.assertAlmostEqual(sel.duration, 25.0)
        self.assertAlmostEqual(sel.start, 1025.0)
        self.assertAlmostEqual(sel.end, 5020.0)
        self.assertIsNone(sel.note)
        runs = [(r.segments[0].session, len(r.segments), r.offset, r.duration) for r in sel.runs()]
        self.assertEqual(runs, [("a", 1, 5.0, 5.0), ("b", 2, 0.0, 20.0)])
        ring.release(sel)
        # More than recorded: everything, across the gap.
        sel = ring.select_last(3600)
        self.assertAlmostEqual(sel.duration, 50.0)
        self.assertEqual(len(sel.segments), 5)
        ring.release(sel)
        # `until` ignores newer footage and cuts the newest segment.
        sel = ring.select_last(12, until=5015.0)
        self.assertEqual([s.path for s in sel.segments], b)
        self.assertEqual((sel.offset, sel.duration, sel.end), (3.0, 12.0, 5015.0))
        self.assertEqual([(r.offset, r.duration) for r in sel.runs()], [(3.0, 12.0)])
        ring.release(sel)
        self.assertIsNone(RingBuffer(60).select_last(10))

    def test_select_last_stops_at_param_change(self):
        ring = RingBuffer(3600)
        _feed(ring, [self.seg(i) for i in range(3)], "a", 1000.0, params=P240)
        _feed(ring, [self.seg(i) for i in range(3, 5)], "b", 2000.0, params=P360)
        sel = ring.select_last(40)
        self.assertEqual([s.session for s in sel.segments], ["b", "b"])
        self.assertAlmostEqual(sel.duration, 20.0)
        self.assertEqual(sel.note, "earlier footage used a different resolution")
        ring.release(sel)
        sel = ring.select_last(15)  # fits in the newest session: nothing left out
        self.assertIsNone(sel.note)
        ring.release(sel)

    def test_clear(self):
        ring = RingBuffer(3600, directory=self.tmp)
        ring.recover()
        _feed(ring, [self.seg(i) for i in range(3)], "a", 1000.0)
        ring.clear()
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(ring.buffered_seconds(), 0.0)

    def test_page_cache_dropped_for_old_segments(self):
        from unittest import mock

        dropped = []

        def fake_fadvise(fd, offset, length, advice):
            self.assertEqual((offset, length, advice), (0, 0, os.POSIX_FADV_DONTNEED))
            dropped.append(Path(os.readlink(f"/proc/self/fd/{fd}")).name)

        ring = RingBuffer(3600)
        paths = [self.seg(i) for i in range(6)]
        with mock.patch.object(os, "posix_fadvise", fake_fadvise, create=True):
            _feed(ring, paths, "a", 1000.0)  # newest closes at 1060
            # Closed >= 30 s before the newest close, each advised once.
            self.assertEqual(dropped, [p.name for p in paths[:3]])
            paths[3].unlink()  # gone by the time it is due: ignored
            _feed(ring, [self.seg(6)], "a", 1060.0)
            self.assertEqual(dropped, [p.name for p in paths[:3]])


@unittest.skipUnless(_have("ffmpeg", "ffprobe"), "ffmpeg/ffprobe not installed")
class MultiSessionExportTest(unittest.TestCase):
    """Real footage from three capture runs: a, b (same parameters) and c (another resolution)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="momento-multi-"))
        cls.sessions = {}
        try:
            for name, (w, h, pattern) in {"a": (320, 240, "ball"), "b": (320, 240, "smpte"),
                                          "c": (640, 360, "ball")}.items():
                d = cls.tmp / name
                d.mkdir()
                # Drop the possibly ragged last segment, like a closed buffer.
                cls.sessions[name] = make_segments(d, w, h, pattern)[:SEG_COUNT - 1]
        except (subprocess.SubprocessError, OSError) as e:
            raise unittest.SkipTest(f"cannot generate fixture footage: {e}")
        if any(len(v) < 4 for v in cls.sessions.values()):
            raise unittest.SkipTest("fixture generation produced too few segments")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ring(self, *names) -> RingBuffer:
        ring = RingBuffer(3600)
        t = 1_000_000.0
        for name in names:
            params = P360 if name == "c" else P240
            t = _feed(ring, self.sessions[name], name, t, length=SEG_SECONDS, params=params, write=False)
            t += 600  # ten minutes paused
        return ring

    def test_two_sessions_same_params(self):
        from unittest import mock

        from momento import exporter

        ring = self.ring("a", "b")
        per_session = len(self.sessions["b"]) * SEG_SECONDS
        want = per_session + 3.0  # 3 s from the end of "a", then all of "b"
        sel = ring.select_last(want)
        self.assertEqual(len(sel.runs()), 2)
        dropped = []
        real = os.posix_fadvise
        with mock.patch.object(os, "posix_fadvise",
                               lambda fd, *a: (dropped.append(os.readlink(f"/proc/self/fd/{fd}")), real(fd, *a))):
            out = exporter.export(sel, self.tmp / "out" / "joined.mp4")
        self.assertEqual(sorted(Path(p) for p in dropped), sorted(s.path.resolve() for s in sel.segments))
        ring.release(sel)
        ExporterTest._check(self, out, want)
        info = probe(out)
        durs = [float(s["duration"]) for s in info["streams"]]
        self.assertLess(abs(durs[0] - durs[1]), 0.1, f"audio/video lengths drift apart: {durs}")

    def test_mismatched_params_keep_newest_tail(self):
        from momento import exporter

        ring = self.ring("a", "c")
        sel = ring.select_last(3600)
        self.assertEqual(sel.note, "earlier footage used a different resolution")
        self.assertEqual({s.session for s in sel.segments}, {"c"})
        out = exporter.export(sel, self.tmp / "out" / "tail.mp4")
        ring.release(sel)
        expected = len(self.sessions["c"]) * SEG_SECONDS
        ExporterTest._check(self, out, expected)
        size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", str(out)],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(size, "640,360")

    def test_export_refuses_mixed_selection(self):
        from momento import exporter

        a, c = self.sessions["a"], self.sessions["c"]
        segs = [Segment(a[0], 0.0, 2.0, session="a", **P240), Segment(c[0], 10.0, 12.0, session="c", **P360)]
        with self.assertRaises(exporter.ExportError):
            exporter.export(Selection(segs, 0.0, 4.0, 0.0, 12.0), self.tmp / "out" / "mixed.mp4")
        self.assertFalse((self.tmp / "out" / "mixed.mp4").exists())


class DaemonBufferTest(unittest.TestCase):
    """The buffer survives shutdown/restart; only an explicit quit clears it."""

    setUp = DaemonControlTest.setUp
    tearDown = DaemonControlTest.tearDown
    call = DaemonControlTest.call

    def test_save_until_ends_clip_earlier(self):
        import time as _t
        buf = self.fill()  # three 10 s segments ending at 1030.0 (wall clock)
        self.assertTrue(buf.exists())
        now = _t.time()
        captured = {}
        orig = self.d._export
        self.d._export = lambda seconds, t_req, when, reply: (captured.update(t=t_req), reply({"ok": True}))
        try:
            self.call({"cmd": "save", "seconds": 5, "until": now - 2.0})
            self.assertAlmostEqual(captured["t"], now - 2.0, places=3)
            self.call({"cmd": "save", "seconds": 5, "until": now - 7200})  # too old: ignored
            self.assertGreater(captured["t"], now - 1)
            self.call({"cmd": "save", "seconds": 5, "until": True})  # not a number: ignored
            self.assertGreater(captured["t"], now - 1)
        finally:
            self.d._export = orig

    def test_stop_keeps_service_running(self):
        buf = self.fill()
        r = self.call({"cmd": "stop"})
        self.assertEqual(r, {"ok": True, "state": "stopped", "buffer_cleared": True})
        self.assertEqual(list(buf.glob("*.ts")), [])
        st = self.call({"cmd": "status"})
        self.assertEqual((st["ok"], st["state"], st["recording"], st["buffered"]), (True, "stopped", False, 0))
        self.assertFalse(self.d._stopping)  # the daemon (and its hotkey) stay up
        self.assertEqual(self.call({"cmd": "resume"})["ok"], True)
        self.assertNotEqual(self.call({"cmd": "status"})["state"], "stopped")

    def fill(self) -> Path:
        buf = Path(self.d.cfg["buffer"]["dir"])
        self.d.ring.recover()
        _feed(self.d.ring, [buf / f"seg{i:08d}.ts" for i in range(3)], "s1", 1000.0)
        return buf

    def test_shutdown_keeps_buffer_for_next_start(self):
        from momento import daemon

        buf = self.fill()
        self.d.stop()  # SIGTERM / service restart / reboot
        self.assertEqual(len(list(buf.glob("*.ts"))), 3)
        again = daemon.Daemon(self.d.cfg, loop=None)
        self.assertEqual(again.ring.recover(), 3)
        self.assertAlmostEqual(again.status()["buffered"], 30.0)

    @unittest.skipUnless(_gi_available(), "PyGObject not available")
    def test_quit_clears_unless_keep_buffer(self):
        from unittest import mock

        from gi.repository import GLib

        buf = self.fill()
        with mock.patch.object(GLib, "timeout_add", lambda ms, fn: fn()):
            self.assertEqual(self.call({"cmd": "quit", "keep_buffer": True}), {"ok": True, "buffer_cleared": False})
            self.assertEqual(len(list(buf.glob("*.ts"))), 3)
            self.d._stopping = False  # same daemon object, second shutdown
            self.assertEqual(self.call({"cmd": "quit"}), {"ok": True, "buffer_cleared": True})
        self.assertFalse(buf.exists())


class DaemonScreenshotTest(unittest.TestCase):
    """The screenshot command against a fake Recorder; the PNG writer is stubbed."""

    setUp_ = DaemonControlTest.setUp
    call = DaemonControlTest.call

    def setUp(self):
        from unittest import mock

        from momento import screenshot

        self.setUp_()
        self.addCleanup(self._tmp.cleanup)
        self.d.cfg["output"]["dir"] = str(Path(self._tmp.name) / "clips")   # never the real ~/Videos
        self.saved = []

        def fake_save(frame, cfg, when=None):
            path = screenshot.write_new(screenshot.images_dir(cfg), screenshot.file_name(when), b"png")
            self.saved.append((frame, path))
            return path
        p = mock.patch.object(screenshot, "save", side_effect=fake_save)
        p.start()
        self.addCleanup(p.stop)
        self.d.recorder.frame = _FakeFrame()

    def notified(self):
        from gi.repository import GLib

        while GLib.MainContext.default().iteration(False):
            pass
        return self.notes

    def test_saved_while_recording(self):
        r = self.call({"cmd": "screenshot"})
        self.assertTrue(r["ok"], r)
        path = Path(r["path"])
        self.assertEqual(path.parent, Path(self.d.cfg["output"]["dir"]) / "Images")
        self.assertRegex(path.name, r"^Momento_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d(_\d+)?\.png$")
        self.assertEqual((r["width"], r["height"]), (1920, 1080))
        self.assertTrue(path.exists())
        self.assertIn("Screenshot saved", self.notified())
        again = self.call({"cmd": "screenshot"})
        self.assertNotEqual(again["path"], r["path"])            # never overwrites

    def test_refused_unless_recording(self):
        self.call({"cmd": "pause"})
        r = self.call({"cmd": "screenshot"})
        self.assertEqual((r["ok"], r["code"]), (False, "not_recording"))
        self.call({"cmd": "stop"})
        self.assertEqual(self.call({"cmd": "screenshot"})["code"], "not_recording")
        self.call({"cmd": "resume"})
        self.d.recorder.recording = False                       # starting / failed
        self.assertEqual(self.call({"cmd": "screenshot"})["code"], "not_recording")
        self.assertEqual(self.saved, [])

    def test_no_frame_and_no_space(self):
        from momento import storage

        self.d.recorder.frame = None
        r = self.call({"cmd": "screenshot"})
        self.assertFalse(r["ok"])
        self.assertNotIn("code", r)
        self.assertIn("Momento: screenshot failed", self.notified())
        self.d.recorder.frame = _FakeFrame()
        self.free = storage.SAVE_MARGIN - 1
        r = self.call({"cmd": "screenshot"})
        self.assertEqual((r["ok"], r["code"]), (False, "no_storage"))
        self.assertEqual(self.saved, [])


class FakeBarProc:
    """Stands in for the resident bar's Popen: wait() blocks until exit()."""

    def __init__(self):
        self._done = threading.Event()
        self.returncode = None
        self.terminated = 0

    def exit(self, code=1):
        self.returncode = code
        self._done.set()

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("bar", timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.exit(-15)

    def kill(self):
        self.exit(-9)


@unittest.skipUnless(_gi_available(), "PyGObject not available")
class DaemonBarTest(unittest.TestCase):
    """The daemon keeps the clip bar loaded, restarts it with backoff, and falls back."""

    setUp_control = DaemonControlTest.setUp
    tearDown = DaemonControlTest.tearDown
    call = DaemonControlTest.call

    def setUp(self):
        from unittest import mock

        from gi.repository import GLib

        from momento import daemon

        self.setUp_control()
        self.procs = []
        self.timers = []   # (ms, fn) restarts scheduled on the main loop
        self.spawned = []  # one-shot bars

        def popen():
            self.procs.append(FakeBarProc())
            return self.procs[-1]
        for target, attr, new in ((daemon, "_popen_bar", popen),
                                  (daemon, "spawn_overlay", lambda: self.spawned.append(1)),
                                  (GLib, "idle_add", lambda fn, *a: fn(*a)),
                                  (GLib, "timeout_add", lambda ms, fn: self.timers.append((ms, fn)) or len(self.timers)),
                                  (GLib, "source_remove", lambda tag: None)):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)
        self.d._bar_managed = True  # what Daemon.start() sets

    def wait_until(self, cond, timeout=3):
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(cond())

    def test_default_on(self):
        from momento import config

        self.assertIs(config.DEFAULTS["ui"]["keep_bar_loaded"], True)
        self.assertTrue(self.d.keep_bar_loaded())

    def test_unmanaged_daemon_never_starts_a_bar(self):
        self.d._bar_managed = False
        self.d.start_bar()
        self.call({"cmd": "reload"})
        self.assertEqual(self.procs, [])

    def test_restart_with_backoff(self):
        from momento import daemon

        self.d.start_bar()
        self.d.start_bar()                          # only ever one
        self.assertEqual(len(self.procs), 1)
        delays = []
        logs = self.assertLogs("momento.daemon", "WARNING")
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)
        for i in range(8):
            self.procs[-1].exit(1)                  # the bar crashed
            self.wait_until(lambda: len(self.timers) == i + 1)
            self.assertIsNone(self.d.bar_proc)
            ms, fn = self.timers[-1]
            delays.append(ms)
            fn()                                    # the timer fires: a new bar
            self.assertEqual(len(self.procs), i + 2)
        self.assertEqual(delays, [1000, 2000, 4000, 8000, 16000, 32000, 60000, 60000])
        # a bar that ran long enough counts as healthy: the backoff starts over
        self.d._bar_started -= daemon.BAR_STABLE_SECONDS
        self.procs[-1].exit(1)
        self.wait_until(lambda: len(self.timers) == 9)
        self.assertEqual(self.timers[-1][0], 1000)

    def test_not_restarted_while_stopping(self):
        self.d.start_bar()
        bar = self.procs[0]
        self.d.stop()                               # SIGTERM / service stop
        self.assertEqual(bar.terminated, 1)
        time.sleep(0.1)
        self.assertEqual(self.timers, [])
        self.d.start_bar()
        self.assertEqual(len(self.procs), 1)        # nothing new while stopping

    def test_reload_follows_the_setting(self):
        self.d.start_bar()
        bar = self.procs[0]
        self.path.write_text(self.path.read_text() + "\n[ui]\nkeep_bar_loaded = false\n")
        self.assertTrue(self.call({"cmd": "reload"})["ok"])
        self.assertFalse(self.d.keep_bar_loaded())
        self.assertEqual(bar.terminated, 1)
        self.assertIsNone(self.d.bar_proc)
        time.sleep(0.1)
        self.assertEqual(self.timers, [])           # stopped on purpose: not restarted
        self.path.write_text(self.path.read_text().replace("keep_bar_loaded = false", "keep_bar_loaded = true"))
        self.call({"cmd": "reload"})
        self.assertEqual(len(self.procs), 2)

    def test_hotkey_toggles_resident_or_falls_back(self):
        from unittest import mock

        from momento import overlay

        with mock.patch.object(overlay, "toggle", return_value=True) as tog:
            self.d.open_bar()
            self.wait_until(lambda: tog.call_count == 1)
        time.sleep(0.05)
        self.assertEqual(self.spawned, [])          # the resident bar handled it
        with mock.patch.object(overlay, "toggle", return_value=False):
            self.d.open_bar()                       # no resident bar reachable
            self.wait_until(lambda: self.spawned == [1])

    def test_keep_bar_loaded_false_is_the_old_path(self):
        from unittest import mock

        from momento import overlay

        self.d.cfg["ui"]["keep_bar_loaded"] = False
        self.d.start_bar()
        self.assertEqual(self.procs, [])            # nothing kept loaded
        with mock.patch.object(overlay, "toggle", side_effect=AssertionError("must not be used")):
            self.d.open_bar()                       # spawned right away, as before
        self.assertEqual(self.spawned, [1])

    def test_cli_overlay_resident_flag(self):
        from momento import cli

        self.assertTrue(cli.build_parser().parse_args(["overlay", "--resident"]).resident)
        self.assertFalse(cli.build_parser().parse_args(["overlay"]).resident)


# ---------------------------------------------------------------- window mode


class RecordSettingTest(unittest.TestCase):
    """The "record" setting: one window (default) or the full screen."""

    def setUp(self):
        from momento import config, settings

        self.config, self.settings = config, settings
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.toml"
        self.path.write_text("# mine\n[capture]\nresolution = \"1080p\"\n")

    def test_validate(self):
        v = self.settings.validate
        self.assertEqual(v({"record": "window"}), {"record": "window"})
        self.assertEqual(v({"record": " Game  Window "}), {"record": "window"})
        self.assertEqual(v({"record": "Full screen"}), {"record": "screen"})
        self.assertEqual(v({"record": "fullscreen"}), {"record": "screen"})
        for bad in ("tv", "", 2):
            with self.assertRaises(ValueError) as cm:
                v({"record": bad})
            self.assertTrue(str(cm.exception).startswith("record: choose one of: screen, window"))

    def test_default_apply_and_describe(self):
        cfg = self.config.load(self.path)
        self.assertEqual(cfg["capture"]["target"], "window")
        self.assertEqual(self.settings.current(cfg)["record"], "window")
        self.assertEqual(self.settings.apply({"record": "Full screen"}, self.path), {"record": "screen"})
        self.assertEqual(self.settings.apply({"record": "game"}, self.path), {"record": "window"})
        self.assertIn("# mine", self.path.read_text())
        cfg = self.config.load(self.path)
        self.assertEqual(cfg["capture"]["target"], "window")
        d = self.settings.describe(cfg, devices={"outputs": [], "inputs": []})
        self.assertEqual(d["values"]["record"], "window")
        self.assertEqual(d["choices"]["record"], ["screen", "window"])
        self.assertEqual(self.settings.RECORD_LABELS, {"screen": "Full screen", "window": "Window"})
        # a hand-edited unknown value reads as the default
        self.assertEqual(self.config.capture_target({"target": "Monitor 2"}), "screen")

    def test_cli(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, ipc

        self.assertEqual(cli.build_parser().parse_args(["set", "record", "window"]).key, "record")
        out = io.StringIO()
        with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "record", "screen"]), 0)
            self.assertEqual(self.config.load(self.path)["capture"]["target"], "screen")
            self.assertEqual(cli.main(["--config", str(self.path), "set", "record", "window"]), 0)
        self.assertEqual(self.config.load(self.path)["capture"]["target"], "window")
        out = io.StringIO()
        with mock.patch.object(self.settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "settings"]), 0)
        self.assertIn("record: window (only the window you pick, when you press play", out.getvalue())
        # daemon running: the restart message tells the user a dialog is coming
        out = io.StringIO()
        reply = {"ok": True, "changed": {"record": "window"}, "restarted": True, "paused": False}
        with mock.patch.object(ipc, "request", return_value=reply), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "record", "window"]), 0)
        self.assertIn("Pick your game window", out.getvalue())

    def test_token_paths(self):
        from momento import config

        self.assertEqual(config.portal_token_path("screen").name, "portal_token")   # the original name
        self.assertEqual(config.portal_token_path("window").name, "portal_token_window")
        self.assertTrue(str(config.portal_token_path("window")).startswith(os.environ["MOMENTO_TEST_SANDBOX"]))
        self.assertFalse(config.forget_portal_token("window"))


class DaemonWindowTest(unittest.TestCase):
    """Window mode in the daemon: no_window, pick_window, resume, configure (fake Recorder)."""

    # the same fake daemon as DaemonControlTest, without running its tests again
    call = DaemonControlTest.call
    _need = DaemonControlTest._need
    tearDown = DaemonControlTest.tearDown

    CLOSED = "The game window closed — pick a window to keep recording"

    def setUp(self):
        DaemonControlTest.setUp(self)
        from momento import config

        self.token = config.portal_token_path("window")
        self.addCleanup(self.token.unlink, missing_ok=True)

    def window_mode(self):
        self.call({"cmd": "configure", "changes": {"record": "window"}})
        return self.d.recorder

    def test_status_reports_target(self):
        self.assertEqual(self.d.status()["target"], "screen")
        rec = self.window_mode()
        self.assertEqual(self.d.status()["target"], "window")
        self.assertIs(rec, FakeRecorder.instances[-1])

    def fill_window(self, rec) -> float:
        """Three closed 10 s segments of this session; returns status.buffered."""
        buf = Path(self.d.cfg["buffer"]["dir"])
        buf.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for i in range(3):
            seg = buf / f"seg{i:08d}.ts"
            seg.write_bytes(b"x" * 188)
            self.d.ring.opened(seg, now - 30 + 10 * i)
            self.d.ring.closed(seg, now - 20 + 10 * i)
        return self.d.status()["buffered"]

    def test_window_closed_stops_like_stop(self):
        rec = self.window_mode()
        self.assertGreater(self.fill_window(rec), 0)
        self.notes.clear()
        rec.recording = False
        rec.on_state("no_window", self.CLOSED)   # what the Recorder reports when the window closes
        st = self.d.status()
        self.assertEqual((st["state"], st["recording"], st["stop_reason"]), ("stopped", False, "window_closed"))
        self.assertNotIn("error", st)
        self.assertEqual(st["buffered"], 0)       # keep_history is off by default: cleared
        self.assertEqual(list(Path(self.d.cfg["buffer"]["dir"]).glob("*.ts")), [])
        self.assertEqual(self.notes, ["Momento: game closed"])
        rec.on_state("no_window", self.CLOSED)   # e.g. the portal session closing as well
        self.assertEqual(len(self.notes), 1)      # one notification
        self.assertEqual(self.d.status()["state"], "stopped")
        started = rec.started
        self.d._storage_tick()                    # nothing restarts it by itself
        self.assertEqual((rec.started, self.d.status()["state"]), (started, "stopped"))

    def test_window_closed_keeps_history_when_asked(self):
        from unittest import mock

        from momento import daemon

        self.call({"cmd": "configure", "changes": {"keep_history": "on"}})
        rec = self.window_mode()
        before = self.fill_window(rec)
        bodies = []
        with mock.patch.object(daemon, "notify", side_effect=lambda bus, summary, body="", icon="": bodies.append(body)):
            rec.recording = False
            rec.on_state("no_window", self.CLOSED)
        st = self.d.status()
        self.assertEqual((st["state"], st["stop_reason"], st["keep_history"]), ("stopped", "window_closed", True))
        self.assertEqual(st["buffered"], before)  # kept, and saveable
        self.assertEqual(len(bodies), 1)
        self.assertIn("Your replay is kept", bodies[0])

    def test_nothing_picked_goes_back_to_where_play_was_pressed(self):
        rec = self.window_mode()
        self.notes.clear()
        # A new session (play from stopped) whose picker is dismissed: stopped again.
        self.call({"cmd": "stop"})
        self.call({"cmd": "resume"})
        rec = self.d.recorder
        rec.on_state("starting", None)
        rec.on_state("no_window", "No game window picked — press play to pick one")
        st = self.d.status()
        self.assertEqual((st["state"], st["stop_reason"]), ("stopped", None))
        self.assertNotIn("error", st)
        self.assertEqual(self.notes, [])          # nothing was recorded: no notification
        # A session with footage (play from pause): paused, so play continues it.
        self.call({"cmd": "resume"})
        self.fill_window(rec)
        self.call({"cmd": "pause"})
        self.call({"cmd": "resume"})
        rec.on_state("starting", None)
        rec.on_state("no_window", "No game window picked — press play to pick one")
        self.assertEqual(self.d.status()["state"], "paused")
        self.assertEqual(self.notes, [])

    def test_pick_window(self):
        self.assertFalse(self.call({"cmd": "pick_window"})["ok"])   # screen mode: nothing to pick
        rec = self.window_mode()
        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.token.write_text("old-window")
        stopped, started = rec.stopped, rec.started
        r = self.call({"cmd": "pick_window"})
        self.assertEqual(r, {"ok": True, "state": "recording"})
        self.assertFalse(self.token.exists())                        # the stored window is dropped
        self.assertEqual((rec.stopped, rec.started), (stopped + 1, started + 1))
        self.assertTrue(rec.interactive[-1])                         # so the picker may open
        # from pause (or no_window) it records again
        self.call({"cmd": "pause"})
        self.assertTrue(self.call({"cmd": "pick_window"})["ok"])
        self.assertEqual(self.d.status()["state"], "recording")
        self.assertFalse(self.d.paused)

    def test_pick_window_without_space(self):
        rec = self.window_mode()
        self.free = self._need() - 1
        r = self.call({"cmd": "pick_window"})
        self.assertEqual((r["ok"], r["code"], r["state"]), (False, "no_storage", "no_storage"))
        self.assertEqual(rec.interactive[-1:], [True])               # nothing new started

    def test_play_from_stopped_picks_a_new_window(self):
        rec = self.window_mode()
        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.token.write_text("the-game")
        self.call({"cmd": "pause"})
        self.call({"cmd": "resume"})              # from paused: the same window
        self.assertTrue(self.token.exists())
        self.assertTrue(rec.interactive[-1])     # the portal may still ask
        self.call({"cmd": "stop"})
        self.d.hours.footage = 1234.0
        started = rec.started
        r = self.call({"cmd": "resume"})          # from stopped: a new session, pick again
        self.assertEqual(r, {"ok": True, "state": "recording"})
        self.assertFalse(self.token.exists())
        self.assertEqual((rec.started, rec.interactive[-1]), (started + 1, True))
        self.assertEqual(self.d.hours.footage, 0)  # the hour marks count from zero
        self.assertIsNone(self.d.status()["stop_reason"])

    def test_screen_mode_play_from_stopped_keeps_its_token(self):
        from momento import config

        screen = config.portal_token_path("screen")
        screen.parent.mkdir(parents=True, exist_ok=True)
        screen.write_text("monitor")
        self.addCleanup(screen.unlink, missing_ok=True)
        self.call({"cmd": "stop"})
        self.assertEqual(self.call({"cmd": "resume"})["state"], "recording")
        self.assertTrue(screen.exists())          # restored as before, no forced picker

    def test_configure_record_switch(self):
        from momento import config

        first = self.d.recorder
        r = self.call({"cmd": "configure", "changes": {"record": "window"}})
        self.assertEqual((r["ok"], r["changed"], r["restarted"]), (True, {"record": "window"}, True))
        self.assertEqual(first.stopped, 1)                           # a new portal session
        rec = self.d.recorder
        self.assertIsNot(rec, first)
        self.assertEqual(rec.interactive, [True])                    # the user switched: picker allowed
        self.assertEqual(rec.cfg["capture"]["target"], "window")
        self.assertEqual(config.load(self.path)["capture"]["target"], "window")
        # any other change in window mode restarts without asking (the stored window is restored)
        self.call({"cmd": "configure", "changes": {"resolution": "720p"}})
        self.assertEqual(self.d.recorder.interactive, [False])
        r = self.call({"cmd": "configure", "changes": {"record": "screen"}})
        self.assertEqual(r["changed"], {"record": "screen"})
        self.assertEqual(self.d.status()["target"], "screen")


def _have_gst() -> bool:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


@unittest.skipUnless(_have_gst(), "GStreamer (PyGObject) not available")
class RecorderWindowTest(unittest.TestCase):
    """pipeline.Recorder's window-mode decisions, without building a real pipeline."""

    def setUp(self):
        import copy

        from momento import config, pipeline
        from momento.ringbuffer import RingBuffer

        self.pipeline = pipeline
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token = config.portal_token_path("window")
        self.addCleanup(self.token.unlink, missing_ok=True)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["capture"].update(source="portal", target="window")
        self.cfg["buffer"]["dir"] = str(Path(self._tmp.name) / "buffer")
        self.states = []
        self.ring = RingBuffer(3600)

    def recorder(self, **capture):
        from unittest import mock

        self.cfg["capture"].update(capture)
        rec = self.pipeline.Recorder(self.cfg, self.ring, lambda s, m: self.states.append((s, m)))
        rec._plan_variants = lambda: [self.pipeline._Variant("x264enc", False)]
        rec._start_portal = mock.Mock()          # never the real portal
        rec._build_and_play = mock.Mock()
        self.addCleanup(rec._cancel_retry)
        return rec

    def test_automatic_start_without_a_window_does_not_ask(self):
        rec = self.recorder()
        rec.start()                               # daemon start / reload: no token -> no picker
        self.assertEqual(self.states, [("no_window", self.pipeline.WINDOW_NOT_PICKED)])
        rec._start_portal.assert_not_called()
        self.assertTrue(rec.window_mode)
        rec.start(interactive=True)               # resume / pick_window: the picker may open
        rec._start_portal.assert_called_once()
        self.assertEqual(self.states[-1][0], "starting")

    def test_stored_window_is_restored(self):
        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.token.write_text("tok")
        rec = self.recorder()
        rec.start()
        rec._start_portal.assert_called_once()
        self.assertEqual(self.states, [("starting", None)])

    def test_window_closed_stops_without_retry(self):
        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.token.write_text("tok")
        rec = self.recorder()
        rec.start()
        rec._got_fragment = rec.recording = True
        rec._on_pipeline_failure("stream disconnected", source_lost=True)
        self.assertEqual(self.states[-1], ("no_window", self.pipeline.WINDOW_CLOSED))
        self.assertEqual(rec._retry_id, 0)                           # no automatic retry
        self.assertTrue(rec._stop_requested)
        self.assertFalse(rec.recording)
        self.assertFalse(self.token.exists())                        # that window is gone for good
        rec._on_pipeline_failure("late error", source_lost=True)     # later messages are ignored
        self.assertEqual(len([s for s, _ in self.states if s == "no_window"]), 1)

    def test_other_failure_keeps_the_window(self):
        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.token.write_text("tok")
        rec = self.recorder()
        rec.start()
        rec._got_fragment = True
        rec._on_pipeline_failure("encoder hiccup")                   # not the source
        self.assertEqual(self.states[-1], ("error", self.pipeline.WINDOW_STOPPED))  # not "the window closed"
        self.assertTrue(self.token.exists())                         # resume restores it quietly
        self.assertEqual(rec._retry_id, 0)

    def test_portal_outcomes(self):
        for message, expect in (("cancelled", self.pipeline.WINDOW_NOT_PICKED),
                                ("session closed", self.pipeline.WINDOW_NOT_PICKED)):
            self.token.parent.mkdir(parents=True, exist_ok=True)
            self.token.write_text("tok")
            self.states.clear()
            rec = self.recorder()
            rec.start()
            rec._on_portal_error(message)
            self.assertEqual(self.states[-1], ("no_window", expect), message)
            self.assertFalse(self.token.exists(), message)
            self.assertEqual(rec._retry_id, 0)
        rec = self.recorder()
        rec.start(interactive=True)
        rec._on_portal_error("this desktop cannot share single windows")
        self.assertEqual(self.states[-1][0], "error")
        self.assertEqual(rec._retry_id, 0)                           # no retry loop in window mode

    def test_screen_mode_still_retries(self):
        rec = self.recorder(target="screen")
        rec.start()
        self.assertFalse(rec.window_mode)
        rec._got_fragment = True
        rec._on_pipeline_failure("stream disconnected", source_lost=True)
        self.assertEqual(self.states[-1][0], "error")
        self.assertNotEqual(rec._retry_id, 0)

    def test_native_window_size_is_locked(self):
        rec = self.recorder(resolution="native")
        rec.start(interactive=True)
        self.assertTrue(rec._lock_size())
        chain = rec._video_chain(self.pipeline._Variant("x264enc", False))
        self.assertIn("videoscale add-borders=true", chain)          # resizes are scaled into the first size
        self.assertIn("capsfilter name=size", chain)
        screen = self.recorder(target="screen", resolution="native")
        screen.start()
        self.assertFalse(screen._lock_size())
        self.assertNotIn("videoscale", screen._video_chain(self.pipeline._Variant("x264enc", False)))


class _FakeMatch:
    def __init__(self, bus, key):
        self.bus, self.key = bus, key

    def remove(self):
        self.bus.receivers.pop(self.key, None)


class _FakePortalBus:
    """Just enough of a dbus-python session bus for ScreenCastPortal (no real D-Bus)."""

    def __init__(self, source_types=3):
        self.receivers = {}
        self.calls = []
        self.props = {"version": 5, "AvailableCursorModes": 3, "AvailableSourceTypes": source_types}

    def get_unique_name(self):
        return ":1.42"

    def get_object(self, name, path, **_):
        return _FakePortalObject(self, path)

    def add_signal_receiver(self, handler, signal_name=None, dbus_interface=None, path=None, **_):
        key = (signal_name, path)
        self.receivers[key] = handler
        return _FakeMatch(self, key)

    def respond(self, options, results):
        token = str(options["handle_token"])
        path = f"/org/freedesktop/portal/desktop/request/1_42/{token}"
        self.receivers[("Response", path)](0, results)


class _FakePortalObject:
    def __init__(self, bus, path):
        self.bus, self.path = bus, path

    def get_dbus_method(self, member, dbus_interface=None):
        bus = self.bus

        def call(*args, reply_handler=None, error_handler=None, **_):
            bus.calls.append((member, args))
            if member == "Get":
                return bus.props[args[1]]
            if member == "CreateSession":
                bus.respond(args[0], {"session_handle": "/org/freedesktop/portal/desktop/session/1_42/s"})
            elif member == "SelectSources":
                bus.respond(args[1], {})
            elif member == "Start":
                bus.respond(args[2], {"streams": [(77, {})], "restore_token": "fresh-token"})
            elif member == "OpenPipeWireRemote":
                reply_handler(os.open(os.devnull, os.O_RDONLY))
            elif member == "Close":
                if reply_handler:
                    reply_handler()
        return call


def _have_dbus() -> bool:
    try:
        import dbus  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have_dbus(), "dbus-python not available")
class PortalWindowTest(unittest.TestCase):
    """ScreenCastPortal against a fake bus: source type and token file per target."""

    def setUp(self):
        from momento import config

        self.config = config
        for t in ("screen", "window"):
            self.addCleanup(config.portal_token_path(t).unlink, missing_ok=True)
        config.portal_token_path("window").parent.mkdir(parents=True, exist_ok=True)
        config.portal_token_path("screen").write_text("screen-token")
        config.portal_token_path("window").write_text("window-token")

    def run_portal(self, target, source_types=3):
        from momento import portal

        bus = _FakePortalBus(source_types)
        got = {}
        p = portal.ScreenCastPortal(bus, self.config.portal_token_path(target), False,
                                    portal.SOURCE_WINDOW if target == "window" else portal.SOURCE_MONITOR)
        p.start(lambda fd, node: got.update(fd=fd, node=node), lambda msg: got.update(error=msg))
        if "fd" in got:
            os.close(got["fd"])
        select = next((a for m, a in bus.calls if m == "SelectSources"), None)
        return got, (dict(select[1]) if select else None)

    def test_window_types_and_token(self):
        got, options = self.run_portal("window")
        self.assertEqual(got.get("node"), 77, got)
        self.assertEqual(int(options["types"]), 2)                    # a single window
        self.assertEqual(str(options["restore_token"]), "window-token")
        self.assertEqual(int(options["persist_mode"]), 2)
        self.assertEqual(self.config.portal_token_path("window").read_text(), "fresh-token")
        self.assertEqual(self.config.portal_token_path("screen").read_text(), "screen-token")  # untouched

    def test_screen_types_and_token(self):
        got, options = self.run_portal("screen")
        self.assertEqual(int(options["types"]), 1)                    # a monitor, as before
        self.assertEqual(str(options["restore_token"]), "screen-token")
        self.assertEqual(self.config.portal_token_path("screen").read_text(), "fresh-token")
        self.assertEqual(self.config.portal_token_path("window").read_text(), "window-token")

    def test_desktop_without_window_sharing(self):
        got, options = self.run_portal("window", source_types=1)
        self.assertIsNone(options)
        self.assertEqual(got, {"error": "this desktop cannot share single windows"})


class ControllerSettingTest(unittest.TestCase):
    """[controller] in config.py / settings.py (no devices involved)."""

    def setUp(self):
        from momento import config, settings

        self.config, self.settings = config, settings
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.toml"
        self.path.write_text("# mine\n[capture]\nresolution = \"1080p\"\n")

    def test_defaults(self):
        cfg = self.config.load(self.path)
        self.assertEqual(self.config.controller(cfg),
                         {"enabled": True, "chord": ("select", "start"), "hold_ms": 300, "exclusive": True})
        self.assertEqual(self.config.load_controller(self.path), self.config.controller(cfg))
        cur = self.settings.current(cfg)
        self.assertEqual((cur["controller"], cur["controller_exclusive"]), ("view_menu", "on"))
        d = self.settings.describe(cfg, devices={"outputs": [], "inputs": []})
        self.assertEqual(d["choices"]["controller"], ["off", "view_menu", "left_paddle", "right_paddle", "l3_r3"])
        self.assertIsInstance(d["controller_available"], bool)

    def test_bad_values_fall_back(self):
        self.path.write_text('[controller]\nopen_chord = ["select", "turbo"]\nhold_ms = -3\n')
        with self.assertLogs("momento.config", "WARNING"):
            ctl = self.config.load_controller(self.path)
        self.assertEqual((ctl["chord"], ctl["hold_ms"]), (("select", "start"), 300))
        self.path.write_text("[controller\n")                       # broken TOML: defaults
        self.assertTrue(self.config.load_controller(self.path)["enabled"])
        self.assertTrue(self.config.load_controller(Path(self._tmp.name) / "missing.toml")["enabled"])

    def test_normalize(self):
        v = self.settings.validate
        self.assertEqual(v({"controller": "View + Menu"}), {"controller": "view_menu"})
        self.assertEqual(v({"controller": "left paddle"}), {"controller": "left_paddle"})
        self.assertEqual(v({"controller": "l3+r3"}), {"controller": "l3_r3"})
        self.assertEqual(v({"controller": "Guide+South"}), {"controller": "mode+south"})
        self.assertEqual(v({"controller": ["view", "menu"]}), {"controller": "view_menu"})
        self.assertEqual(v({"controller": False, "controller_exclusive": "no"}),
                         {"controller": "off", "controller_exclusive": "off"})
        self.assertEqual(v({"controller": "yes"}), {"controller": "on"})
        for bad in ({"controller": "turbo"}, {"controller": "select+turbo"}, {"controller_exclusive": "maybe"}):
            with self.assertRaises(ValueError, msg=bad) as cm:
                v(bad)
            self.assertTrue(str(cm.exception).startswith(next(iter(bad))))
        self.assertEqual(self.settings.controller_label("select+mode"), "Select + Mode")
        self.assertEqual(self.settings.controller_label("right_paddle"), "Right paddle")

    def test_apply_writes_a_list_and_reads_back(self):
        changed = self.settings.apply({"controller": "left_paddle"}, self.path)
        self.assertEqual(changed, {"controller": "left_paddle"})
        text = self.path.read_text()
        self.assertIn("# mine", text)
        self.assertIn('open_chord = ["left_paddle"]', text)
        self.assertEqual(self.config.controller(self.config.load(self.path))["chord"], ("left_paddle",))
        self.assertEqual(self.settings.apply({"controller": "off"}, self.path), {"controller": "off"})
        self.assertFalse(self.config.load(self.path)["controller"]["enabled"])
        # "on" re-enables and reports the shortcut it turned back on
        self.assertEqual(self.settings.apply({"controller": "on"}, self.path), {"controller": "left_paddle"})
        self.assertEqual(self.settings.apply({"controller": "on"}, self.path), {})
        self.assertEqual(self.settings.apply({"controller": "select+mode"}, self.path),
                         {"controller": "select+mode"})
        self.assertEqual(self.config.load(self.path)["controller"]["open_chord"], ["select", "mode"])
        self.assertEqual(self.settings.apply({"controller_exclusive": "off"}, self.path),
                         {"controller_exclusive": "off"})
        self.assertFalse(self.config.controller(self.config.load(self.path))["exclusive"])

    def test_example_config_matches_defaults(self):
        import tomllib

        from momento import config

        example = tomllib.loads((Path(__file__).resolve().parent.parent / "data/config.example.toml").read_text())
        self.assertEqual(example["controller"], config.DEFAULTS["controller"])


class DaemonControllerTest(unittest.TestCase):
    """The daemon's controller hub: chord only, never grabs, follows the config."""

    call = DaemonControlTest.call
    tearDown = DaemonControlTest.tearDown

    def setUp(self):
        DaemonControlTest.setUp(self)
        from momento import gamepad

        self.gamepad = gamepad
        self.clock = [100.0]
        self.devs, self.hubs, self.made = [], [], []
        self.opened = []
        self.d.open_bar = lambda: self.opened.append(self.clock[0])
        self.d.pad_factory = self.factory
        self.d._pads_managed = True               # what start() sets

    def factory(self, **kw):
        self.made.append(kw)
        dev = self.gamepad.FakeDevice(path=f"/fake/pad{len(self.devs)}")
        self.devs.append(dev)
        self.addCleanup(dev.close)
        hub = self.gamepad.Gamepads(lister=lambda: [dev.path], opener=lambda _p: dev, hotplug="off",
                                    watchdog_thread=False, clock=lambda: self.clock[0], **kw)
        self.hubs.append(hub)
        return hub

    def chord(self, hold=0.3):
        g, dev, hub = self.gamepad, self.devs[-1], self.d.pads
        for code in (g.BTN_SELECT, g.BTN_START):
            dev.push(g.EV_KEY, code, 1)
        hub.process(dev.fileno())
        self.clock[0] += hold
        hub.tick()
        for code in (g.BTN_SELECT, g.BTN_START):
            dev.push(g.EV_KEY, code, 0)
        hub.process(dev.fileno())

    def test_chord_opens_bar_without_grabbing(self):
        self.d._sync_controller()
        self.assertEqual(self.made[-1]["navigate"], False)
        self.assertEqual(self.devs[-1].mask, (self.gamepad.EV_KEY,))   # key events only
        self.chord(hold=0.2)                                         # shorter than the 0.3 s default
        self.assertEqual(self.opened, [])
        self.chord()
        self.assertEqual(len(self.opened), 1)
        self.assertAlmostEqual(self.opened[0], 100.5)                # pressed at 100.2, held 0.3 s
        self.assertEqual(self.devs[-1].grab_calls, 0)
        self.assertFalse(self.d.pads.grabbing)

    def test_configure_controller_does_not_restart_recording(self):
        self.d._sync_controller()
        rec = self.d.recorder
        r = self.call({"cmd": "configure", "changes": {"controller": "l3_r3"}})
        self.assertEqual((r["ok"], r["restarted"], r["changed"]), (True, False, {"controller": "l3_r3"}))
        self.assertIs(self.d.recorder, rec)
        self.assertEqual(rec.stopped, 0)
        self.assertEqual(self.d.pads.chord, ("thumbl", "thumbr"))
        self.assertEqual(len(self.hubs), 1)                            # same hub, new chord
        r = self.call({"cmd": "configure", "changes": {"controller": "off"}})
        self.assertEqual(r["changed"], {"controller": "off"})
        self.assertIsNone(self.d.pads)
        self.assertTrue(self.devs[-1].closed)
        r = self.call({"cmd": "configure", "changes": {"controller": "on"}})
        self.assertEqual(r["changed"], {"controller": "l3_r3"})
        self.assertIsNotNone(self.d.pads)
        self.assertEqual(self.d.pads.chord, ("thumbl", "thumbr"))
        r = self.call({"cmd": "configure", "changes": {"controller": "on"}})
        self.assertEqual((r["ok"], r["changed"], r["restarted"]), (True, {}, False))
        self.assertIs(self.d.recorder, rec)

    def test_mixed_change_reloads_once(self):
        self.d._sync_controller()
        r = self.call({"cmd": "configure", "changes": {"controller": "right_paddle", "resolution": "720p"}})
        self.assertEqual((r["ok"], r["restarted"]), (True, True))
        self.assertEqual(self.d.pads.chord, ("right_paddle",))

    def test_disabled_and_stop(self):
        from momento import config

        config.set_value("controller", "enabled", False, self.path)
        self.d.cfg = config.load(self.path)
        self.d._sync_controller()
        self.assertEqual(self.made, [])
        config.set_value("controller", "enabled", True, self.path)
        self.call({"cmd": "reload"})
        self.assertIsNotNone(self.d.pads)
        dev = self.devs[-1]
        self.d.stop()
        self.assertIsNone(self.d.pads)
        self.assertTrue(dev.closed)

    def test_real_devices_never_opened_in_tests(self):
        self.d.pad_factory = None                  # the real Gamepads: refuses under the sandbox
        self.d._sync_controller()
        self.assertIsNone(self.d.pads)



# ------------------------------------------------ keep history, hour marks, window name


class HistorySettingsTest(unittest.TestCase):
    """keep_history / hour_warning / instant_bar in settings.py + config.py, and the tabs."""

    def setUp(self):
        from momento import config, settings

        self.config, self.settings = config, settings
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.toml"
        self.path.write_text("# mine\n[capture]\nresolution = \"1080p\"\n")

    def test_defaults_and_describe(self):
        cfg = self.config.load(self.path)
        self.assertEqual((cfg["buffer"]["keep_history"], cfg["buffer"]["warn_minutes"]), (False, 10))
        cur = self.settings.current(cfg)
        self.assertEqual((cur["keep_history"], cur["hour_warning"], cur["instant_bar"]), ("off", 10, "on"))
        d = self.settings.describe(cfg, devices={"outputs": [], "inputs": []})
        self.assertEqual(d["choices"]["keep_history"], ["off", "on"])
        self.assertEqual(d["choices"]["hour_warning"], [10, 5, 3])
        self.assertEqual(d["choices"]["instant_bar"], ["on", "off"])
        self.assertEqual(d["tabs"], [["General", ["record", "keep_history"]],
                                     ["Video", ["resolution", "fps", "quality"]],
                                     ["Audio", ["audio_source", "mic", "mic_device"]],
                                     ["Controller", ["controller", "controller_exclusive"]],
                                     ["Misc", ["hour_warning", "instant_bar"]]])
        for _name, keys in self.settings.TABS:
            for key in keys:
                self.assertIn(key, d["values"])
                self.assertIn(key, self.settings.KEYS)
        json.dumps(d)  # travels over the socket as is

    def test_validate(self):
        v = self.settings.validate
        self.assertEqual(v({"keep_history": True, "instant_bar": "no", "hour_warning": "5m"}),
                         {"keep_history": "on", "instant_bar": "off", "hour_warning": 5})
        self.assertEqual(v({"hour_warning": 3}), {"hour_warning": 3})
        self.assertEqual(v({"hour_warning": "7 min"}), {"hour_warning": 7})   # any whole number 3-10
        for bad in ({"hour_warning": 2}, {"hour_warning": 11}, {"hour_warning": "soon"},
                    {"hour_warning": True}, {"hour_warning": 4.5}, {"keep_history": "maybe"},
                    {"instant_bar": 2}):
            with self.assertRaises(ValueError, msg=bad) as cm:
                v(bad)
            self.assertTrue(str(cm.exception).startswith(next(iter(bad))), cm.exception)

    def test_apply_writes_config_keys(self):
        changed = self.settings.apply({"keep_history": "on", "hour_warning": 3, "instant_bar": "off"}, self.path)
        self.assertEqual(changed, {"keep_history": "on", "hour_warning": 3, "instant_bar": "off"})
        self.assertIn("# mine", self.path.read_text())
        cfg = self.config.load(self.path)
        self.assertIs(cfg["buffer"]["keep_history"], True)
        self.assertEqual(cfg["buffer"]["warn_minutes"], 3)
        self.assertIs(cfg["ui"]["keep_bar_loaded"], False)
        self.assertEqual(self.settings.apply({"hour_warning": "3"}, self.path), {})

    def test_hand_edited_warning_falls_back(self):
        self.path.write_text("[buffer]\nwarn_minutes = 45\n")
        with self.assertLogs("momento.config", "WARNING"):
            self.assertEqual(self.config.warn_minutes(self.config.load(self.path)), 10)
        self.assertEqual(self.config.warn_minutes({"buffer": {"warn_minutes": 4}}), 4)

    def test_live_keys(self):
        self.assertEqual(set(self.settings.LIVE_KEYS),
                         {"controller", "controller_exclusive", "keep_history", "hour_warning", "instant_bar"})

    def test_example_config_documents_them(self):
        import tomllib

        example = tomllib.loads((Path(__file__).resolve().parent.parent / "data/config.example.toml").read_text())
        d = self.config.DEFAULTS
        self.assertEqual(example["capture"]["target"], d["capture"]["target"])
        self.assertEqual(d["capture"]["target"], "window")
        for key in ("keep_history", "warn_minutes", "max_seconds", "segment_seconds"):
            self.assertEqual(example["buffer"][key], d["buffer"][key], key)
        self.assertEqual(example["ui"], d["ui"])

    def test_cli_shows_them(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, ipc

        out = io.StringIO()
        with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                mock.patch.object(self.settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "keep_history", "on"]), 0)
            self.assertEqual(cli.main(["--config", str(self.path), "set", "hour_warning", "5"]), 0)
            self.assertEqual(cli.main(["--config", str(self.path), "settings"]), 0)
        text = out.getvalue()
        self.assertIn("history: kept when recording stops", text)
        self.assertIn("hour mark: warn 5 min before the 60m mark", text)
        self.assertIn("clip bar: kept loaded", text)
        # daemon running: a live setting says so instead of "restarted"/"paused"
        out = io.StringIO()
        reply = {"ok": True, "changed": {"instant_bar": "off"}, "restarted": False, "paused": True}
        with mock.patch.object(ipc, "request", return_value=reply), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "instant_bar", "off"]), 0)
        self.assertIn("applies right away", out.getvalue())
        self.assertNotIn("paused", out.getvalue())


class CLIStatusTest(unittest.TestCase):
    def run_cli(self, argv, replies):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, ipc

        out = io.StringIO()
        with mock.patch.object(ipc, "request", side_effect=list(replies)), contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_status_says_why_and_which_window(self):
        st = {"ok": True, "state": "stopped", "recording": False, "buffered": 754.0, "max_seconds": 3600,
              "target": "window", "target_name": "Elden Ring", "stop_reason": "window_closed",
              "keep_history": True, "resolution": "1080p", "fps": 60, "quality": "high", "bitrate_kbps": 15000}
        code, text = self.run_cli(["status"], [st])
        self.assertEqual(code, 0)
        self.assertIn("state: stopped (the recorded window closed)", text)
        self.assertIn("record: window: Elden Ring", text)
        self.assertIn("history: kept when recording stops", text)

    def test_stop_and_resume_messages(self):
        _code, text = self.run_cli(["stop"], [{"ok": True, "state": "stopped", "buffer_cleared": False}])
        self.assertIn("The replay is kept", text)
        _code, text = self.run_cli(["stop"], [{"ok": True, "state": "stopped", "buffer_cleared": True}])
        self.assertIn("history cleared", text)
        _code, text = self.run_cli(["resume"], [{"ok": True, "state": "stopped", "target": "window"},
                                                {"ok": True, "state": "starting"}])
        self.assertIn("Pick the window", text)
        _code, text = self.run_cli(["resume"], [{"ok": True, "state": "paused", "target": "window"},
                                                {"ok": True, "state": "starting"}])
        self.assertIn("Recording resumed", text)


class RingListenerTest(unittest.TestCase):
    def test_on_closed_gets_each_closed_segment(self):
        with tempfile.TemporaryDirectory() as d:
            ring = RingBuffer(3600, directory=d)
            ring.recover()
            seen = []
            ring.on_closed = seen.append
            _feed(ring, [Path(d) / f"seg{i:08d}.ts" for i in range(2)], "s1", 1000.0)
            self.assertEqual([round(s.duration) for s in seen], [10, 10])
            ring.closed(Path(d) / "seg00000009.ts", 2000.0)     # not open: nothing to report
            self.assertEqual(len(seen), 2)

            def boom(_seg):
                raise RuntimeError("listener bug")
            ring.on_closed = boom
            with self.assertLogs("momento.ringbuffer", "ERROR"):
                _feed(ring, [Path(d) / "seg00000005.ts"], "s1", 1100.0)   # recording is not affected
            self.assertAlmostEqual(ring.buffered_seconds(), 30.0)


class HourMarksTest(unittest.TestCase):
    """daemon.HourMarks: pure footage bookkeeping (no clock)."""

    def setUp(self):
        from momento import daemon

        self.h = daemon.HourMarks()

    def feed(self, seconds, n, keep, warn=600, length=3600):
        events = []
        for _ in range(n):
            events += self.h.add(seconds, length, warn, keep)
        return events

    def test_warn_once_then_mark_without_history(self):
        self.assertEqual(self.feed(10, 299, keep=False), [])            # 49:50
        self.assertEqual(self.feed(10, 1, keep=False), [("warn", 600.0)])  # 50:00, 10 min left
        self.assertEqual(self.feed(10, 59, keep=False), [])
        self.assertEqual(self.feed(10, 1, keep=False), [("mark", 10.0)])   # 60:00, at the end of this piece
        self.assertEqual(self.feed(10, 360, keep=False), [("mark", 10.0)])  # no warning before later marks

    def test_every_hour_with_history(self):
        events = self.feed(60, 120, keep=True, warn=300)                 # two hours in 1-minute pieces
        self.assertEqual(events, [("warn", 300.0), ("mark", 60.0), ("warn", 300.0), ("mark", 60.0)])
        self.assertEqual(self.h.marks, 2)

    def test_mark_inside_a_piece(self):
        self.h.add(3590, 3600, 600, True)
        self.assertEqual(self.h.add(60, 3600, 600, True), [("mark", 10.0)])  # 10 s into this piece

    def test_warns_once_per_mark(self):
        self.feed(10, 330, keep=False, warn=180)                           # 55 min, warn at 57
        self.assertEqual(self.h.warned, 0)
        self.h.reset()
        self.h.add(3100, 3600, 600, False)                                # one big step past the warn point
        self.assertEqual(self.h.warned, 1)
        self.assertEqual(self.h.add(10, 3600, 600, True), [])              # once per mark

    def test_short_buffer_skips_the_warning(self):
        self.assertEqual(self.feed(10, 30, keep=True, warn=600, length=300), [("mark", 10.0)])

    def test_span_label(self):
        from momento import daemon

        self.assertEqual(daemon.span_label(3600), "60 minutes")
        self.assertEqual(daemon.span_label(60), "1 minute")
        self.assertEqual(daemon.span_label(90), "1m30s")


@unittest.skipUnless(_gi_available(), "PyGObject not available")
class DaemonHourTest(unittest.TestCase):
    """The daemon's hour marks: warn, save each hour with keep_history, disk full (fake Recorder)."""

    call = DaemonControlTest.call
    tearDown = DaemonControlTest.tearDown

    def setUp(self):
        from unittest import mock

        from gi.repository import GLib

        from momento import daemon, exporter

        DaemonControlTest.setUp(self)
        self.sent = []      # (summary, body)
        self.exports = []   # (duration, pins while exporting, out path)
        self.buf = Path(self.d.cfg["buffer"]["dir"])
        self.buf.mkdir(parents=True, exist_ok=True)
        self.d.ring.recover()
        self.clips = Path(self._tmp.name) / "clips"
        self.d.cfg["output"]["dir"] = str(self.clips)
        self.n = 0
        self.t = 1_000_000.0

        def fake_export(sel, out):
            self.exports.append((sel.duration, [s.pins for s in sel.segments], Path(out)))
            Path(out).write_bytes(b"mp4")
            return Path(out)
        for target, attr, new in (
                (daemon, "notify", lambda bus, summary, body="", icon="": self.sent.append((summary, body))),
                (exporter, "export", fake_export),
                (GLib, "idle_add", lambda fn, *a: fn(*a))):     # the worker's result, delivered at once
            patcher = mock.patch.object(target, attr, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, minutes, length=60.0):
        """``minutes`` of footage in ``length``-second segments, as the recorder closes them."""
        for _ in range(int(minutes * 60 / length)):
            seg = self.buf / f"seg{self.n:08d}.ts"
            seg.write_bytes(b"x" * 188)
            self.d.ring.opened(seg, self.t, session="s", width=1920, height=1080, fps=60, codec="h264",
                               audio=True)
            self.d.ring.closed(seg, self.t + length)
            self.n += 1
            self.t += length

    def wait_for(self, cond, timeout=5):
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(cond())

    def test_warning_without_history(self):
        self.record(49)
        self.assertEqual(self.sent, [])
        self.record(1)
        self.assertEqual(self.sent, [("Momento: 60 minutes almost full",
                                      "In 10 min the start of this session starts being replaced. "
                                      "Save anything you want from it now.")])
        self.record(80)                           # past the mark and on: no save, no more warnings
        self.assertEqual((len(self.sent), self.exports), (1, []))
        self.assertAlmostEqual(self.d.status()["buffered"], 3600.0)   # the ring keeps rolling

    def test_every_hour_is_saved_with_history(self):
        self.call({"cmd": "configure", "changes": {"keep_history": "on", "hour_warning": 5}})
        rec = self.d.recorder
        self.record(55)
        self.assertEqual(self.sent, [("Momento: 60 minutes almost full",
                                      "In 5 min this hour is saved to Videos and a new hour starts.")])
        self.record(5)                            # the mark
        self.wait_for(lambda: len(self.sent) == 2)
        self.assertEqual(len(self.exports), 1)
        duration, pins, out = self.exports[0]
        self.assertAlmostEqual(duration, 3600.0)
        self.assertTrue(all(p >= 1 for p in pins))                    # pinned while exporting
        self.assertTrue(out.name.endswith("_60m.mp4"), out.name)      # same name pattern as a save
        self.assertEqual(out.parent, self.clips)
        self.assertEqual(self.sent[1][0], "Saved the last hour to Videos")
        self.assertTrue(all(s.pins == 0 for s in self.d.ring._segments))  # released
        self.record(60)                           # the second hour: warned and saved again
        self.wait_for(lambda: len(self.exports) == 2)
        self.assertEqual([s for s, _ in self.sent].count("Momento: 60 minutes almost full"), 2)
        self.assertEqual((rec.stopped, self.d.recorder is rec), (0, True))  # recording never restarted

    def test_hour_not_saved_when_the_disk_is_full(self):
        from momento import storage

        self.call({"cmd": "configure", "changes": {"keep_history": "on"}})
        self.record(59)
        self.free = storage.SAVE_MARGIN          # the clips folder can't take an hour
        self.record(1)
        self.assertEqual(self.exports, [])
        self.assertIn("disk full", self.sent[-1][0])
        self.assertIn("Recording continues", self.sent[-1][1])
        self.assertTrue(all(s.pins == 0 for s in self.d.ring._segments))
        self.assertEqual(self.d.recorder.stopped, 0)
        self.assertEqual(self.d.status()["state"], "recording")

    def test_pauses_continue_the_session_and_stop_starts_a_new_one(self):
        self.record(30)
        self.call({"cmd": "pause"})
        self.call({"cmd": "resume"})
        self.record(20)
        self.assertEqual(len(self.sent), 1)      # 30 + 20 min: the warning (pauses don't reset)
        self.call({"cmd": "stop"})
        self.call({"cmd": "resume"})              # a new session
        self.record(49)
        self.assertEqual(len(self.sent), 1)
        self.record(1)
        self.assertEqual(len(self.sent), 2)


class DaemonHistoryTest(unittest.TestCase):
    """Stop with and without keep_history; saving while stopped (fake Recorder)."""

    call = DaemonControlTest.call
    tearDown = DaemonControlTest.tearDown

    def setUp(self):
        DaemonControlTest.setUp(self)
        self.buf = Path(self.d.cfg["buffer"]["dir"])
        self.d.ring.recover()
        _feed(self.d.ring, [self.buf / f"seg{i:08d}.ts" for i in range(3)], "s1", time.time() - 40)

    def test_stop_clears_by_default(self):
        r = self.call({"cmd": "stop"})
        self.assertEqual(r, {"ok": True, "state": "stopped", "buffer_cleared": True})
        st = self.d.status()
        self.assertEqual((st["buffered"], st["stop_reason"], st["keep_history"]), (0, "user", False))
        self.assertEqual(self.call({"cmd": "save", "seconds": 30}), {"ok": False, "error": "nothing recorded yet"})

    def test_stop_keeps_history_and_saves_from_stopped(self):
        from unittest import mock

        from momento import exporter

        r = self.call({"cmd": "configure", "changes": {"keep_history": "on"}})
        self.assertEqual((r["restarted"], r["changed"]), (False, {"keep_history": "on"}))
        self.assertIs(self.d.recorder, FakeRecorder.instances[-1])
        self.assertEqual(self.d.recorder.stopped, 0)                 # not restarted
        r = self.call({"cmd": "stop"})
        self.assertEqual(r, {"ok": True, "state": "stopped", "buffer_cleared": False})
        self.assertEqual(len(list(self.buf.glob("*.ts"))), 3)
        st = self.d.status()
        self.assertEqual((st["state"], st["stop_reason"], st["keep_history"]), ("stopped", "user", True))
        self.assertAlmostEqual(st["buffered"], 30.0)
        self.d.cfg["output"]["dir"] = str(Path(self._tmp.name) / "clips")
        with mock.patch.object(exporter, "export", side_effect=lambda sel, out: Path(out)):
            r = self.call({"cmd": "save", "seconds": 20})
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(r["seconds"], 20.0)
        self.assertEqual(self.d.status()["state"], "stopped")        # saving doesn't start anything
        # turning it off later doesn't delete what was kept; the next stop does
        self.call({"cmd": "configure", "changes": {"keep_history": "off"}})
        self.assertAlmostEqual(self.d.status()["buffered"], 30.0)
        self.call({"cmd": "resume"})
        self.assertIsNone(self.d.status()["stop_reason"])
        self.assertTrue(self.call({"cmd": "stop"})["buffer_cleared"])
        self.assertEqual(self.d.status()["buffered"], 0)

    def test_pause_never_clears(self):
        self.call({"cmd": "pause"})
        self.assertAlmostEqual(self.d.status()["buffered"], 30.0)
        self.assertIsNone(self.d.status()["stop_reason"])

    def test_hour_warning_is_live(self):
        r = self.call({"cmd": "configure", "changes": {"hour_warning": 3}})
        self.assertEqual((r["restarted"], r["changed"]), (False, {"hour_warning": 3}))
        self.assertEqual(self.d.cfg["buffer"]["warn_minutes"], 3)
        self.assertEqual(self.d.recorder.stopped, 0)


@unittest.skipUnless(_gi_available(), "PyGObject not available")
class DaemonStartTest(unittest.TestCase):
    """Daemon.start(): full screen records at once; window mode waits for play."""

    tearDown = DaemonControlTest.tearDown

    def setUp(self):
        from unittest import mock

        from gi.repository import GLib

        from momento import daemon, ipc

        DaemonControlTest.setUp(self)
        self.procs = []

        class Server:
            def __init__(self, *_a):
                pass

            def start(self):
                pass

            def close(self):
                pass
        for target, attr, new in ((ipc, "Server", Server),
                                  (daemon, "_popen_bar", lambda: self.procs.append(FakeBarProc()) or self.procs[-1]),
                                  (GLib, "timeout_add_seconds", lambda *a: 0),
                                  (GLib, "source_remove", lambda tag: None),
                                  (daemon.Daemon, "_sync_controller", lambda self: None)):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    def daemon(self, target):
        from momento import config, daemon

        config.set_value("capture", "target", target, self.path)
        cfg = config.load(self.path)
        cfg["hotkey"]["enabled"] = False
        d = daemon.Daemon(cfg, loop=None)
        d.start()
        self.addCleanup(d.stop)
        return d

    def test_window_mode_waits_for_play(self):
        d = self.daemon("window")
        rec = d.recorder
        self.assertEqual(rec.started, 0)                 # no picker at login
        st = d.status()
        self.assertEqual((st["state"], st["recording"], st["stop_reason"], st["target_name"]),
                         ("stopped", False, None, None))
        box = []
        d.handle({"cmd": "resume"}, box.append)
        self.assertEqual((rec.started, rec.interactive), (1, [True]))  # play: the picker may open
        self.assertEqual(d.status()["state"], "recording")

    def test_full_screen_starts_at_once(self):
        d = self.daemon("screen")
        self.assertEqual((d.recorder.started, d.recorder.interactive), (1, [False]))
        self.assertEqual(d.status()["state"], "recording")


class TargetNameTest(unittest.TestCase):
    """status.target_name: the picked window's title, looked up off the main loop."""

    call = DaemonControlTest.call
    tearDown = DaemonControlTest.tearDown

    def setUp(self):
        from momento import config

        DaemonControlTest.setUp(self)
        self.token = config.portal_token_path("window")
        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.token.unlink, missing_ok=True)
        self.asked = []
        self.names = {"tok-1": "Elden Ring", "tok-2": None, "tok-3": "Hades II"}
        self.d.name_lookup = lambda token: (self.asked.append(token), self.names.get(token))[1]
        self.call({"cmd": "configure", "changes": {"record": "window"}})

    def session(self, token):
        """The portal saved ``token`` and the recording started."""
        self.token.write_text(token)
        self.d.recorder.on_state("starting", None)
        self.d.recorder.on_state("recording", None)

    def wait_name(self, name):
        deadline = time.monotonic() + 3
        while self.d.status()["target_name"] != name and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.d.status()["target_name"], name)

    def test_lookup_once_per_session(self):
        self.assertIsNone(self.d.status()["target_name"])
        self.session("tok-1")
        self.wait_name("Elden Ring")
        self.d.recorder.on_state("recording", None)       # same session: not asked again
        self.assertEqual(self.asked, ["tok-1"])
        self.session("tok-2")                               # restored, but the name is unknown
        time.sleep(0.05)
        self.assertEqual(self.asked, ["tok-1", "tok-2"])
        self.wait_name("Elden Ring")                        # the name from before stays

    def test_cleared_on_new_window_and_full_screen(self):
        self.session("tok-1")
        self.wait_name("Elden Ring")
        self.call({"cmd": "pick_window"})                   # a new window: forget the old name
        self.assertIsNone(self.d.status()["target_name"])
        self.session("tok-3")
        self.wait_name("Hades II")
        self.call({"cmd": "configure", "changes": {"record": "screen"}})
        self.assertIsNone(self.d.status()["target_name"])
        self.assertIsNone(self.d.target_name)

    def test_real_lookup_never_runs_in_tests(self):
        import types
        from unittest import mock

        fake = types.ModuleType("momento.windowname")
        fake.title_for_token = mock.Mock(side_effect=AssertionError("must not reach KWin"))
        self.d.name_lookup = None
        with mock.patch.dict(sys.modules, {"momento.windowname": fake}):
            self.session("tok-1")
            time.sleep(0.05)
        fake.title_for_token.assert_not_called()
        self.assertIsNone(self.d.status()["target_name"])

    def test_window_title_wrapper(self):
        import types
        from unittest import mock

        from momento import daemon

        class Bus:
            closed = 0

            def close(self):
                Bus.closed += 1
        # never a real session bus in tests
        patcher = mock.patch.object(daemon, "_private_bus", Bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        fake = types.ModuleType("momento.windowname")
        for result, expect in (("Hades II", "Hades II"), (None, None), ("  ", None), (3, None)):
            fake.title_for_token = lambda token, bus=None, r=result: r if isinstance(bus, Bus) else "wrong bus"
            # the package attribute too: once imported, `from . import windowname` reads it
            with mock.patch.dict(sys.modules, {"momento.windowname": fake}), \
                    mock.patch.object(sys.modules["momento"], "windowname", fake, create=True):
                self.assertEqual(daemon._window_title("tok"), expect)
        self.assertEqual(Bus.closed, 4)                     # its own connection, closed after each lookup

        def boom(token, bus=None):
            raise RuntimeError("no KWin")
        fake.title_for_token = boom
        pkg = sys.modules["momento"]
        with mock.patch.dict(sys.modules, {"momento.windowname": fake}), \
                mock.patch.object(pkg, "windowname", fake, create=True):
            self.assertIsNone(daemon._window_title("tok"))
        with mock.patch.dict(sys.modules, {"momento.windowname": None}):   # module missing
            had = pkg.__dict__.pop("windowname", None)
            try:
                self.assertIsNone(daemon._window_title("tok"))
            finally:
                if had is not None:
                    pkg.windowname = had


if __name__ == "__main__":
    unittest.main()
