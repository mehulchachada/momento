"""Core tests: python3 -m unittest discover -s tests (or python3 -m unittest tests.test_core)."""

from __future__ import annotations

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
        with self.assertRaises(RuntimeError):
            self.ipc.Server(self.sock, lambda m, r: None).start()


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
                     ["pause"], ["resume"], ["stop"], ["quit"]):
            self.assertEqual(cli.build_parser().parse_args(argv).command, argv[0])

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
        FakeRecorder.instances.append(self)

    def start(self):
        self.started += 1
        self.recording = True
        self.on_state("recording", None)

    def stop(self):
        self.stopped += 1
        self.recording = False
        self.on_state("stopped", None)


class DaemonControlTest(unittest.TestCase):
    """pause / resume / settings / configure against a fake Recorder (no GStreamer)."""

    def setUp(self):
        import types
        from unittest import mock

        from momento import config, daemon

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"
        self.path.write_text("[capture]\nresolution = \"1080p\"\n")
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


if __name__ == "__main__":
    unittest.main()
