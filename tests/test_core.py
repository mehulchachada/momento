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
from unittest import mock
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

    def test_shorter_replay_length_keeps_the_newest(self):
        ring = RingBuffer(max_seconds=3600, margin=0, directory=self.tmp)
        paths = _fill(ring, self.tmp, 120)   # 20 minutes, [1000, 2200)
        sel = ring.select(1000.0, 1015.0)    # an export still reading the oldest two
        ring.set_max_seconds(300)
        self.assertEqual(ring.max_seconds, 300)
        # the newest 5 min (the segment straddling the limit stays) + the pinned ones
        self.assertEqual([p for p in paths if p.exists()], paths[:2] + paths[89:])
        ring.release(sel)
        self.assertEqual([p for p in paths if p.exists()], paths[89:])
        self.assertAlmostEqual(ring.buffered_seconds(), 300.0)
        self.assertAlmostEqual(ring.select_last(3600).start, 1890.0)
        self.assertEqual([e["file"] for e in _index_lines(self.tmp)], [p.name for p in paths[89:]])
        # longer again: nothing comes back, the ring grows from here
        ring.set_max_seconds(3600)
        _fill_from(ring, self.tmp, 120, 60, base=1000.0)
        self.assertAlmostEqual(ring.buffered_seconds(), 910.0)


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
                    self.assertEqual(cli.main(["--config", str(path), "set", "resolution", "hd"]), 0)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["--config", str(path), "set", "quality", "insane"]), 1)
            finally:
                ipc.request = orig
            cfg = config.load(path)
            self.assertFalse(cfg["audio"]["desktop"])
            self.assertEqual(cfg["capture"]["resolution"], "720p")
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
            refusal = {"ok": False, "code": "no_storage", "error": "1080p Ultra needs 35.2 GB free, 9.4 GB available"}
            with mock.patch.object(ipc, "request", return_value=refusal) as req, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["--config", str(path), "set", "resolution", "fhd"]), 1)
            req.assert_called_once_with({"cmd": "configure", "changes": {"resolution": "1080p"}, "origin": "set"},
                                        timeout=30)
            self.assertIn("35.2 GB", err.getvalue())

    def test_set_resolution_above_1080p_is_refused(self):
        """1440p and 4K are not offered yet: one friendly line, nothing sent or saved."""
        import contextlib
        import io
        from unittest import mock

        from momento import cli, config, ipc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            for value in ("4k", "4K", "2k", "1440p", "2160p", "uhd", "qhd"):
                err, out = io.StringIO(), io.StringIO()
                with mock.patch.object(ipc, "request") as req, \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(cli.main(["--config", str(path), "set", "resolution", value]), 1, value)
                req.assert_not_called()
                self.assertEqual(err.getvalue(),
                                 "1440p and 4K aren't available yet; Momento records up to 1080p for now.\n")
                self.assertEqual(out.getvalue(), "")
            self.assertFalse(path.exists())
            self.assertEqual(config.load(path)["capture"]["resolution"], "1080p")

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
            req.assert_called_once_with({"cmd": "configure", "changes": {"controller": "on"}, "origin": "set"},
                                        timeout=30)
            text = out.getvalue()
            self.assertIn("controller = left_paddle", text)
            self.assertIn("Saved. Press Left paddle to open or close the bar.", text)   # a tap by default
            self.assertNotIn("paused", text)             # a controller change never waits for resume
            out = io.StringIO()
            with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--config", str(path), "set", "controller", "off"]), 0)
                self.assertEqual(cli.main(["--config", str(path), "settings"]), 0)
            self.assertIn("controller: off", out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["set", "controllr", "on"])

    def test_controller_watch_follows_config(self):
        """`momento controller --watch` hands the configured shortcut to the watch tool: a tap by default."""
        import contextlib
        import io
        from unittest import mock

        from momento import cli, config, gamepad

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            out = io.StringIO()
            with mock.patch.object(gamepad, "main", return_value=0) as watch, contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--config", str(path), "controller", "--watch"]), 0)
            watch.assert_called_once_with(["--watch", "--chord", "mode+dpad_down", "--hold-ms", "0"])
            self.assertIn("press PS / Xbox + Down to open or close the bar", out.getvalue())
            config.set_value("controller", "open_chord", ["select", "start"], path)   # the old default
            config.set_value("controller", "hold_ms", 300, path)    # hand-edited (no setting)
            out = io.StringIO()
            with mock.patch.object(gamepad, "main", return_value=0) as watch, contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--config", str(path), "controller", "--watch"]), 0)
            watch.assert_called_once_with(["--watch", "--chord", "select+start", "--hold-ms", "300"])
            self.assertIn("hold View + Menu (0.3 s) to open or close the bar", out.getvalue())

    def test_set_controller_two_buttons(self):
        """`momento set controller`: ps_down, off, or two buttons; one or three get a plain
        error. Open with and Exclusive are no settings any more."""
        import contextlib
        import io
        from unittest import mock

        from momento import cli, config, ipc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            out = io.StringIO()
            with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--config", str(path), "set", "controller", "l1+r1"]), 0)
                self.assertEqual(config.load_controller(path)["chord"], ("tl", "tr"))
                self.assertEqual(cli.main(["--config", str(path), "set", "controller", "ps_down"]), 0)
                self.assertEqual(cli.main(["--config", str(path), "set", "controller", "view_menu"]), 0)
            self.assertIn("controller = view_menu", out.getvalue())
            self.assertEqual(config.load_controller(path)["chord"], ("select", "start"))
            for value in ("left_paddle", "ps", "l1+r1+select"):
                err = io.StringIO()
                with mock.patch.object(ipc, "request") as req, contextlib.redirect_stderr(err):
                    self.assertEqual(cli.main(["--config", str(path), "set", "controller", value]), 1, value)
                req.assert_not_called()                              # nothing sent, nothing saved
                self.assertIn("a controller shortcut is two buttons pressed together", err.getvalue())
            self.assertEqual(config.load_controller(path)["chord"], ("select", "start"))
            for key in ("controller_open", "controller_exclusive"):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    cli.main(["--config", str(path), "set", key, "on"])
                self.assertIn("invalid choice", err.getvalue())

    def test_storage_line(self):
        from momento import cli

        line = cli.storage_line({"ok": False, "free": 3_100_000_000, "required": 7_200_000_000, "reclaimable": 0})
        self.assertEqual(line, "3.1 GB free, needs 7.2 GB \u2014 not enough")
        line = cli.storage_line({"ok": True, "free": 3e9, "required": 7e9, "reclaimable": 5e9})
        self.assertEqual(line, "3.0 GB free + 5.0 GB buffer, needs 7.0 GB \u2014 ok")
        # a newer daemon: "needs" is the full span (with Keep history's hour); "low" when it won't fit
        sto = {"ok": True, "free": 9e9, "required": 7e9, "reclaimable": 0, "low": True, "needed": 14e9,
               "available": 9e9, "history": True, "disk": "buffer", "label": "1080p High"}
        self.assertEqual(cli.storage_line(sto), "9.0 GB free, needs 14.0 GB \u2014 low")
        self.assertEqual(cli.low_storage_line({"state": "recording", "max_seconds": 3600, "storage": sto}),
                         "Low storage: 60 min at 1080p High with Keep history needs 14.0 GB, 9.0 GB free. "
                         "Free up space.")
        self.assertIsNone(cli.low_storage_line({"state": "no_storage", "storage": sto}))  # the state says it
        self.assertIsNone(cli.low_storage_line({"state": "recording", "storage": {**sto, "low": False}}))
        self.assertIsNone(cli.low_storage_line({"state": "recording", "storage": {"ok": True}}))

    def test_status_shows_low_storage(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, ipc

        sto = {"ok": False, "free": 5_100_000_000, "required": 8_200_000_000, "reclaimable": 0, "path": "/b",
               "low": True, "needed": 8_200_000_000, "available": 5_100_000_000, "history": False,
               "disk": "buffer", "label": "1080p High"}
        reply = {"ok": True, "state": "paused", "recording": False, "buffered": 12, "max_seconds": 3600,
                 "resolution": "1080p", "quality": "high", "fps": 60, "bitrate_kbps": 15000,
                 "output_dir": "/v", "storage": sto}
        out = io.StringIO()
        with mock.patch.object(ipc, "request", return_value=reply), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 0)
        self.assertIn("  warning: Low storage: 60 min at 1080p High needs 8.2 GB, 5.1 GB free. Free up space.",
                      out.getvalue())
        out = io.StringIO()
        with mock.patch.object(ipc, "request", return_value={**reply, "storage": {**sto, "low": False}}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 0)
        self.assertNotIn("warning", out.getvalue())


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
        from momento import config, quality, settings

        self.config, self.settings, self.quality = config, settings, quality
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"
        self.path.write_text("# Momento\n[capture]\n# pick one\nresolution = \"1080p\"\n\n[audio]\ndesktop = true\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_validate_normalizes(self):
        v = self.settings.validate({"resolution": "FHD", "quality": "ULTRA", "bitrate": "0",
                                    "audio_source": "@DEFAULT_MONITOR@", "mic": True,
                                    "mic_device": "@DEFAULT_SOURCE@"})
        self.assertEqual(v, {"resolution": "1080p", "quality": "ultra", "bitrate": 0,
                             "audio_source": "default", "mic": "on", "mic_device": "default"})
        self.assertEqual(self.settings.validate({"audio_source": "Off", "mic": "no"}),
                         {"audio_source": "off", "mic": "off"})
        self.assertEqual(self.settings.validate({"audio_source": "ROG Ally.monitor"}),
                         {"audio_source": "ROG Ally.monitor"})
        # `momento set resolution 480p` (or sd)
        for value in ("480p", "480P", "sd", " SD "):
            self.assertEqual(self.settings.validate({"resolution": value}), {"resolution": "480p"}, value)
        self.assertTrue(self.settings.KEYS["resolution"].startswith("480p, 720p, 1080p, native ("))
        self.assertEqual(self.settings.apply({"resolution": "sd"}, self.path), {"resolution": "480p"})
        self.assertEqual(self.config.load(self.path)["capture"]["resolution"], "480p")

    def test_validate_rejects(self):
        for bad in ({"resolution": "999p"}, {"quality": "max"}, {"bitrate": 500}, {"bitrate": "fast"},
                    {"mic": "maybe"}, {"audio_source": ""}, {"mic_device": "a\nb"}, {"volume": 3}):
            with self.assertRaises(ValueError, msg=bad) as cm:
                self.settings.validate(bad)
            self.assertTrue(str(cm.exception).startswith(next(iter(bad))), cm.exception)
        with self.assertRaises(ValueError):
            self.settings.validate(["resolution"])
        with self.assertRaises(ValueError) as cm:
            self.settings.validate({"resolution": "999p"})
        self.assertEqual(str(cm.exception), "resolution: choose one of: 480p, 720p, 1080p, native")

    def test_validate_refuses_1440p_and_4k(self):
        for value in ("1440p", "2160p", "4k", "4K", "2k", "UHD", "qhd", " 4k "):
            with self.assertRaises(self.settings.Unavailable, msg=value) as cm:
                self.settings.validate({"resolution": value, "quality": "ultra"})
            self.assertEqual(str(cm.exception),
                             "1440p and 4K aren't available yet; Momento records up to 1080p for now.")
        before = self.path.read_text()
        with self.assertRaises(ValueError):
            self.settings.apply({"resolution": "4k", "mic": "on"}, self.path)
        self.assertEqual(self.path.read_text(), before)

    def test_older_config_with_1440p_or_4k(self):
        """A config saved with 1440p/2160p (or 2k/4k) records at 1080p; the file is left alone."""
        for saved in ("1440p", "2160p", "4k", "2K"):
            self.path.write_text(f'[capture]\nresolution = "{saved}"\n')
            cfg = self.config.load(self.path)
            self.assertEqual(self.settings.current(cfg)["resolution"], "1080p", saved)
            d = self.settings.describe(cfg, devices={"outputs": [], "inputs": []}, source=(3840, 2160))
            self.assertEqual((d["values"]["resolution"], d["resolution_effective"]), ("1080p", "1080p"))
            self.assertEqual(self.quality.resolution(cfg["capture"]), (1920, 1080))
            self.assertEqual(self.quality.bitrate_kbps(cfg["capture"]), 15_000)
            # another setting changed: the resolution line stays as it was
            self.assertEqual(self.settings.apply({"mic": "on"}, self.path), {"mic": "on"})
            self.assertIn(f'resolution = "{saved}"', self.path.read_text())
            # choosing 1080p (what it records at) changes nothing but writes it down
            self.assertEqual(self.settings.apply({"resolution": "1080p"}, self.path), {})
            self.assertIn('resolution = "1080p"', self.path.read_text())

    def test_apply_writes_and_keeps_comments(self):
        changed = self.settings.apply({"resolution": "720p", "quality": "ultra",
                                       "audio_source": "ROG Ally.monitor", "mic": "on",
                                       "mic_device": "alsa_input.usb-Blue_Yeti.analog-stereo"}, self.path)
        self.assertEqual(set(changed), {"resolution", "quality", "audio_source", "mic", "mic_device"})
        text = self.path.read_text()
        self.assertIn("# pick one", text)
        cfg = self.config.load(self.path)
        self.assertEqual(cfg["capture"]["resolution"], "720p")
        self.assertEqual(cfg["capture"]["quality"], "ultra")
        self.assertEqual(cfg["audio"]["desktop_device"], "ROG Ally.monitor")
        self.assertTrue(cfg["audio"]["microphone"])
        self.assertEqual(cfg["audio"]["microphone_device"], "alsa_input.usb-Blue_Yeti.analog-stereo")
        self.assertEqual(self.settings.current(cfg)["audio_source"], "ROG Ally.monitor")
        # same values again -> nothing changed
        self.assertEqual(self.settings.apply({"resolution": "720p", "mic": "on"}, self.path), {})
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
        self.assertEqual(d["choices"]["resolution"], ["480p", "720p", "1080p", "native"])
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
        self.bodies = []

        def note(bus, summary, body="", icon=""):
            self.notes.append(summary)
            self.bodies.append((body, icon))
        nt = mock.patch.object(daemon, "notify", side_effect=note)
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

    # --- the gallery's pause (Full screen) ------------------------------------------
    def gallery_pause(self, pid=None):
        return self.call({"cmd": "pause", "reason": "gallery", "pid": os.getpid() if pid is None else pid})

    def gallery_resume(self):
        return self.call({"cmd": "resume", "reason": "gallery"})

    def dead_pid(self):
        import subprocess

        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_gallery_pauses_full_screen_and_resumes(self):
        rec = self.d.recorder
        self.assertIsNone(self.d.status()["pause_reason"])
        self.assertEqual(self.gallery_pause(), {"ok": True, "state": "paused", "pause_reason": "gallery"})
        st = self.d.status()
        self.assertEqual((st["state"], st["recording"], st["pause_reason"]), ("paused", False, "gallery"))
        self.assertEqual(rec.stopped, 1)
        self.assertEqual(self.gallery_pause()["pause_reason"], "gallery")     # again: still the gallery's
        self.assertEqual(rec.stopped, 1)
        self.assertTrue(self.gallery_resume()["ok"])
        st = self.d.status()
        self.assertEqual((st["state"], st["pause_reason"]), ("recording", None))
        self.assertEqual(rec.started, 2)
        self.gallery_resume()                                              # nothing held: no-op
        self.assertEqual(rec.started, 2)

    def test_gallery_leaves_window_mode_recording(self):
        self.d.cfg["capture"]["target"] = "window"
        rec = self.d.recorder
        r = self.gallery_pause()
        self.assertEqual((r["state"], r["pause_reason"]), ("recording", None))
        self.assertEqual((rec.stopped, self.d.status()["state"]), (0, "recording"))
        self.gallery_resume()
        self.assertEqual(rec.started, 1)

    def test_gallery_keeps_a_user_pause_or_stop(self):
        rec = self.d.recorder
        self.call({"cmd": "pause"})                                       # the user paused first
        self.assertIsNone(self.gallery_pause()["pause_reason"])
        self.gallery_resume()
        self.assertEqual((self.d.status()["state"], rec.started), ("paused", 1))
        self.call({"cmd": "stop"})
        self.assertIsNone(self.gallery_pause()["pause_reason"])
        self.gallery_resume()
        st = self.d.status()
        self.assertEqual((st["state"], st["pause_reason"], rec.started), ("stopped", None, 1))

    def test_user_pause_during_gallery_takes_over(self):
        rec = self.d.recorder
        self.gallery_pause()
        self.assertEqual(self.call({"cmd": "pause"}), {"ok": True, "state": "paused"})
        self.assertIsNone(self.d.status()["pause_reason"])                 # the user's pause now
        self.gallery_resume()
        self.assertEqual((self.d.status()["state"], rec.started), ("paused", 1))

    def test_user_resume_during_gallery_takes_over(self):
        rec = self.d.recorder
        self.gallery_pause()
        self.call({"cmd": "resume"})
        self.assertEqual((self.d.status()["state"], rec.started), ("recording", 2))
        self.call({"cmd": "pause"})                                        # and pauses again later
        self.gallery_resume()                                              # the gallery closing: no-op
        self.assertEqual((self.d.status()["state"], rec.started), ("paused", 2))

    def test_gallery_pause_resumes_when_the_bar_is_gone(self):
        rec = self.d.recorder
        self.assertTrue(self.d._process_alive(os.getpid()))
        self.gallery_pause()                                               # a live bar: kept
        self.assertTrue(self.d._check_gallery_owner())
        self.assertEqual(self.d.status()["pause_reason"], "gallery")
        self.gallery_resume()
        self.gallery_pause(pid=self.dead_pid())                            # crashed or killed
        self.assertEqual(self.d.status()["state"], "paused")
        self.assertFalse(self.d._check_gallery_owner())
        st = self.d.status()
        self.assertEqual((st["state"], st["pause_reason"], rec.started), ("recording", None, 3))
        self.assertFalse(self.d._gallery_timer)

    def test_gallery_pause_resumes_when_the_resident_bar_exits(self):
        """The resident bar exits (recycled after the gallery, 75, or crashed) holding it."""
        import types

        from momento import config

        rec = self.d.recorder
        self.d._schedule_bar_restart = lambda: None                        # no restart timer here
        for code in (config.BAR_RECYCLE_EXIT, -9):
            proc = types.SimpleNamespace(pid=4242)
            self.d.bar_proc = proc
            self.gallery_pause(pid=4242)
            self.assertEqual(self.d.status()["pause_reason"], "gallery")
            self.d._bar_exited(proc, code)
            st = self.d.status()
            self.assertEqual((st["state"], st["pause_reason"]), ("recording", None), code)
        self.assertEqual(rec.started, 3)
        other = types.SimpleNamespace(pid=4343)                            # another bar's exit
        self.d.bar_proc = other
        self.gallery_pause(pid=4242)
        self.d._bar_exited(other, 1)
        self.assertEqual(self.d.status()["pause_reason"], "gallery")
        self.d._clear_pause_reason()

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
        self.assertEqual(set(st["storage"]), {"ok", "free", "required", "reclaimable", "path",
                                              "low", "needed", "available", "history", "disk", "label"})
        self.assertTrue(st["storage"]["low"])
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
        self.assertEqual(self.notes, ["Momento: low storage"])  # still recording: a heads-up
        self.free = storage.LOW_WATER - 1
        self.d._storage_tick()
        self.assertEqual(rec.stopped, 1)
        st = self.d.status()
        self.assertEqual(st["state"], "no_storage")
        self.assertIn("Disk almost full", st["error"])
        self.assertEqual(self.notes, ["Momento: low storage", "Momento: not enough disk space"])
        self.d._storage_tick()
        self.assertEqual(len(self.notes), 2)  # notified once
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
        self.d._storage_tick()
        self.assertEqual(self.d.status()["state"], "paused")
        self.assertEqual(self.notes, ["Momento: low storage"])  # capture untouched; warned once

    # --- low storage: a full span doesn't fit ------------------------------------------

    def _full(self, **buffer):
        """Bytes needed for a full span at the running settings (+ buffer overrides)."""
        from momento import storage

        return storage.check({**self.d.cfg, "buffer": {**self.d.cfg["buffer"], **buffer}})["needed"]

    def test_low_storage_notified_once_then_rearmed(self):
        from momento import storage

        rec = self.d.recorder
        need = self._need()
        self.assertEqual(self._full(), need)          # without Keep history: buffer + reserve
        self.free = need + (10 << 30)
        self.d._storage_tick()
        self.assertEqual(self.notes, [])
        self.assertFalse(self.d.status()["storage"]["low"])
        self.free = need - (1 << 30)                  # a restart wouldn't fit; capture keeps going
        self.d._storage_tick()
        self.assertEqual(self.notes, ["Momento: low storage"])
        self.assertEqual(self.bodies, [(f"15 min at 1080p High needs {storage.human(need)}, "
                                        f"{storage.human(need - (1 << 30))} free. Free up space.", "dialog-warning")])
        st = self.d.status()
        self.assertEqual((st["state"], st["storage"]["low"], st["storage"]["ok"]), ("recording", True, False))
        self.assertEqual((st["storage"]["needed"], st["storage"]["available"]), (need, need - (1 << 30)))
        for _ in range(3):
            self.d._storage_tick()                    # every 30 s: no repeat
        self.assertEqual(rec.stopped, 0)
        # back above the line, but inside the hysteresis: the bar clears, the warning stays spent
        self.free = need + (512 << 20)
        self.d._storage_tick()
        self.assertFalse(self.d.status()["storage"]["low"])
        self.free = need - 1
        self.d._storage_tick()
        self.assertEqual(len(self.notes), 1)          # no flapping around the line
        # clearly recovered: re-armed, so the next drop warns again
        self.free = need + storage.REARM_MARGIN
        self.d._storage_tick()
        self.assertEqual(len(self.notes), 1)
        self.free = need - 1
        self.d._storage_tick()
        self.assertEqual(self.notes, ["Momento: low storage"] * 2)

    def test_low_storage_counts_keep_history(self):
        from momento import storage

        buf = storage.buffer_bytes(self.d.cfg)
        need = self._need()
        # room for the buffer, not for the buffer + the hour Keep history saves
        self.free = need + buf // 2
        with mock.patch.object(storage, "same_disk", return_value=True):
            self.assertEqual(self._full(keep_history=True), need + buf)
            r = self.call({"cmd": "configure", "changes": {"keep_history": "on"}})
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["state"], "recording")
            self.assertEqual(self.notes, ["Momento: low storage"])
            sto = r["storage"]
            self.assertEqual((sto["ok"], sto["low"], sto["history"]), (True, True, True))
            self.assertEqual(sto["needed"], need + buf)
            self.assertIn("with Keep history", storage.low_message(sto, 3600))
            # turning it off drops the need below what is free: re-armed at once
            self.call({"cmd": "configure", "changes": {"keep_history": "off"}})
            self.assertFalse(self.d.status()["storage"]["low"])
            self.call({"cmd": "configure", "changes": {"keep_history": "on"}})
        self.assertEqual(self.notes, ["Momento: low storage"] * 2)

    def test_low_storage_on_the_clips_disk(self):
        from momento import storage

        out = self.d.cfg["output"]["dir"]
        buf = storage.buffer_bytes(self.d.cfg)
        self.out_free = buf + storage.RESERVE - 1
        free = mock.patch.object(storage, "free_bytes",
                                 side_effect=lambda path: self.out_free if str(path) == out else 10**13)
        with free, mock.patch.object(storage, "same_disk", return_value=False):
            self.call({"cmd": "configure", "changes": {"keep_history": "on"}})
            self.assertEqual(self.notes, ["Momento: low storage"])
            self.assertTrue(self.bodies[0][0].startswith("Keep history needs "))
            sto = self.d.status()["storage"]
            self.assertEqual((sto["disk"], sto["low"], sto["ok"]), ("output", True, True))
            self.out_free = buf + storage.RESERVE + (512 << 20)     # inside the hysteresis
            self.d._storage_tick()
            self.out_free = buf + storage.RESERVE - 1
            self.d._storage_tick()
            self.assertEqual(len(self.notes), 1)
            self.out_free = buf + storage.RESERVE + storage.REARM_MARGIN
            self.d._storage_tick()
            self.out_free = buf + storage.RESERVE - 1
            self.d._storage_tick()
        self.assertEqual(self.notes, ["Momento: low storage"] * 2)

    def test_settings_change_raising_the_need_warns(self):
        from momento import storage

        # Keep history on and room for 1080p High (buffer + hour), not for 1080p High 120 fps
        self.call({"cmd": "configure", "changes": {"keep_history": "on"}})
        with mock.patch.object(storage, "same_disk", return_value=True):
            self.free = self._full() + (1 << 30)
            self.d._storage_tick()
            self.assertEqual(self.notes, [])
            r = self.call({"cmd": "configure", "changes": {"fps": 120}})
            self.assertEqual((r["ok"], r["restarted"]), (True, True))   # a restart still fits
            self.assertEqual(self.d.status()["state"], "recording")
            self.assertEqual(self.notes, ["Momento: low storage"])
            self.assertEqual(r["storage"]["label"], "1080p High 120 fps")
            self.assertTrue(r["storage"]["low"])
            self.d._storage_tick()
        self.assertEqual(self.notes, ["Momento: low storage"])

    def test_blocked_start_is_not_notified_twice(self):
        self.d.recorder.stop()
        rec = FakeRecorder(self.d.cfg, self.d.ring, self.d._on_state)
        self.d.recorder = rec
        self.free = self._need() - 1
        self.call({"cmd": "reload"})
        self.assertEqual(self.d.status()["state"], "no_storage")
        self.d._storage_tick()
        self.assertEqual(self.notes, ["Momento: not enough disk space"])

    def test_low_storage_at_daemon_start(self):
        from gi.repository import GLib

        from momento import daemon, ipc, storage

        def boot(free, keep_history):
            self.notes.clear()
            self.free = free
            cfg = {**self.d.cfg, "buffer": {**self.d.cfg["buffer"], "keep_history": keep_history}}
            d = daemon.Daemon(cfg, loop=None)
            with mock.patch.object(ipc, "Server"), mock.patch.object(GLib, "timeout_add_seconds", return_value=0), \
                    mock.patch.object(daemon.Daemon, "start_bar"), \
                    mock.patch.object(daemon.Daemon, "_sync_controller"), \
                    mock.patch.object(storage, "same_disk", return_value=True):
                d.start()
            return d

        need = self._need()
        d = boot(need + (1 << 30), keep_history=True)     # starts, but the saved hour won't fit
        self.assertEqual(d.status()["state"], "recording")
        self.assertEqual(self.notes, ["Momento: low storage"])
        d = boot(need - 1, keep_history=False)            # blocked: only today's notification
        self.assertEqual(d.status()["state"], "no_storage")
        self.assertEqual(self.notes, ["Momento: not enough disk space"])
        d = boot(need + (1 << 30), keep_history=False)    # fits: nothing to say
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

    def test_save_longer_than_the_replay_is_refused(self):
        self.assertEqual(self.d.status()["max_seconds"], 900)              # the default: 15 minutes
        self.d.recorder.recording = False  # no flush round trip
        r = self.call({"cmd": "save", "seconds": "30m"})
        self.assertEqual(r, {"ok": False, "code": "too_long",
                             "error": "Can't save 30m: the replay only keeps the last 15 minutes. "
                                      "Save 15m or less, or choose a longer Replay length in settings."})
        self.assertEqual(self.notes, [])                                   # an answer, not a notification
        r = self.call({"cmd": "save", "seconds": 900})                     # the whole replay is fine
        self.assertNotEqual(r.get("code"), "too_long")
        self.call({"cmd": "configure", "changes": {"replay_length": 30}})
        self.assertNotEqual(self.call({"cmd": "save", "seconds": "30m"}).get("code"), "too_long")

    def test_cli_save_longer_than_the_replay(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, daemon, ipc

        reply = {"ok": False, "code": "too_long", "error": daemon.too_long_message(1800, 900)}
        err = io.StringIO()
        with mock.patch.object(ipc, "request", return_value=reply), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["save", "30m"]), 1)
        self.assertEqual(err.getvalue(),
                         "momento: Can't save 30m: the replay only keeps the last 15 minutes. Save 15m or less, "
                         "or choose a longer Replay length in settings.\n"
                         "To keep more: momento set replay_length 30m\n")

    def test_configure_refuses_what_does_not_fit(self):
        from momento import config, storage

        self.free = self._need(quality="ultra", fps=120) - 1
        before = self.path.read_text()
        r = self.call({"cmd": "configure", "changes": {"quality": "ultra", "fps": 120}})
        self.assertEqual((r["ok"], r["code"]), (False, "no_storage"))
        self.assertTrue(r["error"].startswith("1080p Ultra 120 fps needs "), r["error"])
        self.assertIn(" available", r["error"])
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.d.recorder.started, 1)
        # force writes anyway; the daemon then sits in no_storage
        r = self.call({"cmd": "configure", "changes": {"quality": "ultra", "fps": 120}, "force": True})
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
        self.assertEqual(st["current"], "1080p/high/auto")     # fps auto, the default
        self.assertEqual(len(st["required"]), 4 * 3 * 3)       # 480p, 720p, 1080p, native x auto, 60, 120
        self.assertEqual(st["required"]["1080p/high/auto"], storage.required_bytes(self.d.cfg))
        self.assertEqual(st["required"]["1080p/high/auto"], st["required"]["1080p/high/60"])  # refresh unknown


class SizedRecorder(FakeRecorder):
    """A fake recorder that "negotiates" a picture size, as the real one learns it from the caps."""

    size = (1920, 1080)

    def start(self, interactive=False):
        self.source_size = SizedRecorder.size
        super().start(interactive)


class DaemonResolutionCapTest(unittest.TestCase):
    """The daemon caps the resolution by the recorded picture: status, settings, storage."""

    setUp = DaemonControlTest.setUp
    tearDown = DaemonControlTest.tearDown
    call = DaemonControlTest.call

    def use_sized(self, size):
        SizedRecorder.size = size
        sys.modules["momento.pipeline"].Recorder = SizedRecorder
        self.d.recorder.stop()
        self.d.recorder = SizedRecorder(self.d.cfg, self.d.ring, self.d._on_state)
        self.d.recorder.start()

    def need(self, source=None, **capture):
        from momento import storage

        return storage.required_bytes({**self.d.cfg, "capture": {**self.d.cfg["capture"], **capture}}, source)

    def settings_reply(self):
        from unittest import mock

        from momento import settings

        with mock.patch.object(settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}):
            return self.call({"cmd": "settings"})

    def test_unknown_source_changes_nothing(self):
        st = self.d.status()
        self.assertEqual((st["source_size"], st["resolution_effective"], st["bitrate_kbps"]), (None, "1080p", 15000))
        r = self.settings_reply()
        self.assertEqual(r["resolution_allowed"], ["480p", "720p", "1080p", "native"])
        self.assertIsNone(r["source_size"])

    def test_1080p_setting_on_a_720p_window(self):
        from momento import config

        self.call({"cmd": "configure", "changes": {"resolution": "720p"}})
        self.use_sized((1280, 720))
        self.assertEqual(self.d.source_size, (1280, 720))
        # 1080p would not fit on this disk, what is really recorded (720p) does
        self.free = self.need(resolution="720p") + 10
        self.assertGreater(self.need(resolution="1080p"), self.free)
        r = self.call({"cmd": "configure", "changes": {"resolution": "fhd"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["restarted"], r["state"]), (True, "recording"))
        self.assertEqual(config.load(self.path)["capture"]["resolution"], "1080p")   # saved as asked
        st = self.d.status()
        self.assertEqual((st["resolution"], st["resolution_effective"], st["source_size"]),
                         ("1080p", "native", [1280, 720]))
        self.assertEqual(st["bitrate_kbps"], 10000)
        self.assertEqual(st["storage"]["required"], self.need(resolution="720p"))
        self.assertTrue(st["storage"]["ok"])
        r = self.settings_reply()
        self.assertEqual(r["resolution_allowed"], ["480p", "720p", "native"])
        self.assertEqual((r["values"]["resolution"], r["resolution_effective"], r["source_size"]),
                         ("1080p", "native", [1280, 720]))
        req = r["storage"]["required"]
        self.assertEqual(req["1080p/high/60"], req["720p/high/60"])
        self.assertEqual(req["1080p/ultra/120"], req["720p/ultra/120"])
        self.assertEqual(req["native/high/60"], req["720p/high/60"])
        # a pause keeps what is known; so does a reload
        self.call({"cmd": "pause"})
        self.assertEqual(self.d.status()["source_size"], [1280, 720])
        self.call({"cmd": "reload"})
        self.assertEqual(self.d.status()["resolution_effective"], "native")

    def test_4k_screen_records_1080p(self):
        """Native and an older config's 4K on a 4K screen: 1080 lines, and it costs 1080p."""
        from momento import config

        self.path.write_text(self.path.read_text().replace('resolution = "1080p"', 'resolution = "2160p"'))
        self.use_sized((3840, 2160))
        self.call({"cmd": "reload"})
        st = self.d.status()
        self.assertEqual((st["resolution"], st["resolution_effective"], st["bitrate_kbps"]),
                         ("1080p", "1080p", 15000))
        self.assertEqual(st["storage"]["required"], self.need(resolution="1080p"))
        self.assertEqual(st["storage"]["label"], "1080p High")
        self.assertEqual(config.load(self.path)["capture"]["resolution"], "2160p")   # file left alone
        r = self.settings_reply()
        self.assertEqual(r["choices"]["resolution"], ["480p", "720p", "1080p", "native"])
        self.assertEqual(r["resolution_allowed"], ["480p", "720p", "1080p", "native"])
        self.assertEqual((r["values"]["resolution"], r["resolution_effective"]), ("1080p", "1080p"))
        self.assertEqual(r["storage"]["current"], "1080p/high/auto")
        self.assertEqual(r["storage"]["required"]["native/high/60"], r["storage"]["required"]["1080p/high/60"])
        # 4K can't be chosen again
        r = self.call({"cmd": "configure", "changes": {"resolution": "4k"}})
        self.assertEqual(r, {"ok": False,
                             "error": "1440p and 4K aren't available yet; Momento records up to 1080p for now."})
        self.assertEqual(config.load(self.path)["capture"]["resolution"], "2160p")
        # native: the screen scaled down to 1080 lines, at 1080p's bitrate
        r = self.call({"cmd": "configure", "changes": {"resolution": "native"}})
        self.assertTrue(r["ok"], r)
        st = self.d.status()
        self.assertEqual((st["resolution"], st["resolution_effective"], st["bitrate_kbps"]),
                         ("native", "native", 15000))
        self.assertEqual(st["storage"]["required"], self.need(resolution="1080p"))

    def test_screen_change_is_followed(self):
        self.use_sized((3840, 2160))
        self.call({"cmd": "configure", "changes": {"resolution": "1080p"}})
        st = self.d.status()
        self.assertEqual((st["resolution_effective"], st["bitrate_kbps"]), ("1080p", 15000))
        SizedRecorder.size = (1280, 720)                  # another monitor on the next start
        self.call({"cmd": "reload"})
        st = self.d.status()
        self.assertEqual((st["source_size"], st["resolution_effective"], st["bitrate_kbps"]),
                         ([1280, 720], "native", 10000))  # 720 lines: the 720p class

    def test_new_window_forgets_the_size(self):
        self.use_sized((1280, 720))
        self.assertEqual(self.d.status()["resolution_effective"], "native")
        SizedRecorder.size = None                         # the next picker is still open
        self.call({"cmd": "configure", "changes": {"record": "window"}})
        st = self.d.status()
        self.assertEqual((st["source_size"], st["resolution_effective"]), (None, "1080p"))
        SizedRecorder.size = (1001, 701)
        r = self.call({"cmd": "pick_window"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.d.status()["source_size"], [1001, 701])
        SizedRecorder.size = None
        self.call({"cmd": "stop"})
        self.call({"cmd": "resume"})                      # play from stopped: a new window
        self.assertIsNone(self.d.status()["source_size"])

    def test_low_storage_warning_counts_what_is_recorded(self):
        from momento import storage

        self.use_sized((1280, 720))
        self.free = self.need((1280, 720)) + 10         # 1080p wouldn't fit; the 720p really recorded does
        self.assertGreater(self.need(), self.free)
        st = self.d.status()["storage"]
        self.assertEqual((st["ok"], st["low"], st["label"]), (True, False, "720p High"))
        self.assertEqual(st["needed"], self.need(resolution="720p"))
        self.free = self.need((1280, 720)) - 10
        st = self.d.status()["storage"]
        self.assertTrue(st["low"])
        self.assertTrue(storage.low_message(st, 3600).startswith("Low storage: 60 min at 720p High needs "))
        # Keep history counts the saved hour at the recorded size too
        self.d.cfg["buffer"]["keep_history"] = True
        self.d.cfg["output"]["dir"] = self._tmp.name      # clips on the buffer's disk
        self.free = 10**13
        st = self.d.status()["storage"]
        self.assertEqual(st["needed"], self.need(resolution="720p") + storage.buffer_bytes(
            {**self.d.cfg, "capture": {**self.d.cfg["capture"], "resolution": "720p"}}))

    def test_configure_refusal_counts_the_source(self):
        self.use_sized((1280, 720))
        # 1080p Ultra records as 720p Ultra here: refused only when that doesn't fit
        self.free = self.need((1280, 720), resolution="1080p", quality="ultra") - 1
        r = self.call({"cmd": "configure", "changes": {"quality": "ultra"}})
        self.assertEqual((r["ok"], r.get("code")), (False, "no_storage"))
        self.assertEqual(r["storage"]["required"], self.need(resolution="720p", quality="ultra"))
        self.free += 1
        r = self.call({"cmd": "configure", "changes": {"quality": "ultra"}})
        self.assertTrue(r["ok"], r)


class StorageTest(unittest.TestCase):
    def cfg(self, **capture):
        from momento import config

        cfg = config.load(Path(tempfile.gettempdir()) / "momento-no-such-config.toml")
        cfg["capture"].update(capture)
        cfg["buffer"]["max_seconds"] = 3600   # the numbers below are for a 60-minute replay
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
        self.assertEqual(storage.label(self.cfg(resolution="720p", quality="ultra")), "720p Ultra")
        self.assertEqual(storage.label(self.cfg(resolution="2160p", quality="ultra")), "1080p Ultra")  # older config
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
        self.assertEqual((req["free"], req["reclaimable"], req["current"]), (need - 100, 7, "1080p/high/auto"))
        self.assertEqual(req["required"]["1080p/high/60"], need)
        self.assertEqual(req["required"]["1080p/high/auto"], need)            # refresh unknown: 60 fps
        self.assertLess(req["required"]["720p/standard/60"], req["required"]["1080p/ultra/120"])
        self.assertLess(req["required"]["480p/high/60"], req["required"]["720p/high/60"])
        self.assertEqual(sorted(req["required"]), sorted(f"{r}/{q}/{f}" for r in ("480p", "720p", "1080p", "native")
                                                         for q in ("standard", "high", "ultra")
                                                         for f in ("auto", 60, 120)))
        self.assertEqual(len(req["required"]), 36)        # 480p, 720p, 1080p, native x 3 x (auto, 60, 120)
        self.assertEqual(cfg["capture"]["resolution"], "1080p")  # not mutated

    def test_full_span_need(self):
        from unittest import mock

        from momento import storage

        for fps in (60, 120):
            cfg = self.cfg(resolution="1080p", quality="high", fps=fps)
            need = storage.required_bytes(cfg)
            with mock.patch.object(storage, "free_bytes", return_value=need - 1):
                chk = storage.check(cfg)
            self.assertEqual((chk["ok"], chk["low"], chk["needed"], chk["available"]), (False, True, need, need - 1))
            self.assertEqual((chk["history"], chk["disk"]), (False, "buffer"))
            with mock.patch.object(storage, "free_bytes", return_value=need):
                self.assertFalse(storage.check(cfg)["low"])
        self.assertEqual(storage.check(self.cfg(fps=120))["label"], "1080p High 120 fps")
        # 120 fps: 1.5x the video bits, rounded to whole Mbps (15 -> 22 Mbps at 1080p High)
        self.assertEqual(storage.buffer_bytes(self.cfg(fps=120)), int((22_000 + 160) * 1000 / 8 * 3600 * 1.05))
        # the buffer length and an explicit bitrate count too
        cfg = self.cfg(bitrate_kbps=50_000)
        cfg["buffer"]["max_seconds"] = 1800
        self.assertEqual(storage.check(cfg)["needed"], int((50_000 + 160) * 1000 / 8 * 1800 * 1.05) + (1 << 30))

    def test_full_span_need_with_keep_history(self):
        from unittest import mock

        from momento import storage

        cfg = self.cfg(fps=120)
        cfg["buffer"]["keep_history"] = True
        buf, need = storage.buffer_bytes(cfg), storage.required_bytes(cfg)
        self.assertEqual(storage.history_bytes(cfg), buf)
        # same disk: buffer + the saved hour + reserve
        with mock.patch.object(storage, "same_disk", return_value=True), \
                mock.patch.object(storage, "free_bytes", return_value=need + buf - 1):
            chk = storage.check(cfg)
        self.assertEqual((chk["ok"], chk["low"], chk["history"], chk["needed"]), (True, True, True, need + buf))
        with mock.patch.object(storage, "same_disk", return_value=True), \
                mock.patch.object(storage, "free_bytes", return_value=need + buf):
            self.assertFalse(storage.check(cfg)["low"])
        # the clips on another disk: the hour is checked there
        out = cfg["output"]["dir"]
        free = {cfg["buffer"]["dir"]: 10**13, out: buf}
        with mock.patch.object(storage, "same_disk", return_value=False), \
                mock.patch.object(storage, "free_bytes", side_effect=lambda p: free[str(p)]):
            chk = storage.check(cfg)
            self.assertEqual((chk["ok"], chk["low"], chk["disk"], chk["history"]), (True, True, "output", True))
            self.assertEqual((chk["needed"], chk["available"]), (buf + (1 << 30), buf))
            self.assertTrue(storage.low_message(chk, 3600).startswith("Low storage: Keep history needs "))
            free[out] = 10**13
            chk = storage.check(cfg)
            self.assertEqual((chk["low"], chk["history"], chk["needed"]), (False, False, need))
        # off: nothing extra
        cfg["buffer"]["keep_history"] = False
        self.assertEqual(storage.history_bytes(cfg), 0)

    def test_low_message(self):
        from momento import storage

        chk = {"ok": True, "low": True, "needed": 8_200_000_000, "available": 5_100_000_000,
               "history": False, "disk": "buffer", "label": "1080p High"}
        self.assertEqual(storage.low_message(chk, 3600),
                         "Low storage: 60 min at 1080p High needs 8.2 GB, 5.1 GB free. Free up space.")
        self.assertEqual(storage.low_message({**chk, "history": True, "needed": 15_400_000_000}, 1800),
                         "Low storage: 30 min at 1080p High with Keep history needs 15.4 GB, 5.1 GB free. "
                         "Free up space.")
        self.assertEqual(storage.span(90), "1m30s")
        self.assertEqual(storage.span(3600), "60 min")

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


class ResolutionCapTest(unittest.TestCase):
    """Presets taller than the recorded picture are not offered and never upscaled;
    nothing is recorded taller than 1080 lines (v1.0.0)."""

    ALL = ["480p", "720p", "1080p", "native"]

    def test_offered_up_to_1080p(self):
        from momento import quality

        self.assertEqual(quality.MAX_HEIGHT, 1080)
        self.assertEqual(list(quality.RESOLUTIONS), self.ALL)
        self.assertEqual(quality.LATER, ("1440p", "2160p"))
        self.assertEqual(quality.TALLEST, "1080p")
        # kept for later: the presets and their bitrates are still known
        self.assertEqual(quality.PRESETS["2160p"], (3840, 2160))
        self.assertEqual(quality.later_message(),
                         "1440p and 4K aren't available yet; Momento records up to 1080p for now.")

    def test_raising_max_height_offers_1440p(self):
        """One place to re-enable 1440p/4K: quality.MAX_HEIGHT (the lists derive from it)."""
        from momento import quality

        # a private copy of the module with MAX_HEIGHT = 1440 (the real one is untouched)
        src = Path(quality.__file__).read_text()
        self.assertEqual(src.count("\nMAX_HEIGHT = 1080\n"), 1)
        ns: dict = {}
        exec(compile(src.replace("\nMAX_HEIGHT = 1080\n", "\nMAX_HEIGHT = 1440\n"), quality.__file__, "exec"), ns)
        self.assertEqual(list(ns["RESOLUTIONS"]), ["480p", "720p", "1080p", "1440p", "native"])
        self.assertEqual((ns["LATER"], ns["TALLEST"]), (("2160p",), "1440p"))
        self.assertEqual(ns["later_message"](), "4K isn't available yet; Momento records up to 1440p for now.")
        self.assertEqual(ns["native_size"]((3840, 2160)), (2560, 1440))
        self.assertEqual(quality.MAX_HEIGHT, 1080)

    def test_allowed_table(self):
        from momento import quality

        table = {
            (1920, 1080): self.ALL,
            (2560, 1440): self.ALL,
            (3440, 1440): self.ALL,
            (2560, 1080): self.ALL,                                    # 21:9 1080p
            (3840, 2160): self.ALL,
            (5120, 2880): self.ALL,
            (1920, 1200): self.ALL,                                    # 16:10
            (1280, 800): ["480p", "720p", "native"],                   # Steam Deck
            (1280, 720): ["480p", "720p", "native"],                   # a 720p window
            (1270, 710): ["480p", "720p", "native"],                   # within 2 % of 720
            (1001, 701): ["480p", "native"],                           # 720 is more than 2 % taller
            (1000, 600): ["480p", "native"],                           # a small window
            (854, 480): ["480p", "native"],                            # a 480p window
            (640, 480): ["480p", "native"],                            # 4:3: 480p with bars
            (800, 471): ["480p", "native"],                            # within 2 % of 480
            (800, 470): ["native"],                                    # 480 is more than 2 % taller
            (640, 360): ["native"],                                    # a tiny window: its own size
            (1920, 1070): self.ALL,                                    # a window a bit short of 1080
            (1920, 1050): ["480p", "720p", "native"],                  # 1080 is more than 2 % taller
            (1080, 1920): self.ALL,                                    # portrait
        }
        for source, want in table.items():
            self.assertEqual(quality.allowed_resolutions(source), want, source)
            self.assertEqual(quality.allowed_resolutions(list(source)), want, source)
        for unknown in (None, [], [0, 1080], ["1920", "1080"], [True, 1080], (1920,), "1920x1080"):
            self.assertEqual(quality.allowed_resolutions(unknown), self.ALL, unknown)
            self.assertIsNone(quality.source_size(unknown))

    def test_effective_and_rate_class(self):
        from momento import quality

        self.assertEqual(quality.effective_resolution("1080p", (1920, 1200)), "1080p")
        self.assertEqual(quality.effective_resolution("native", (640, 480)), "native")
        self.assertEqual(quality.effective_resolution("native", (3840, 2160)), "native")
        self.assertEqual(quality.effective_resolution("1080p", None), "1080p")      # unknown: as set
        self.assertEqual(quality.effective_resolution("1080P", (1280, 720)), "native")
        # an older config's 1440p / 4K records at 1080p (or the picture's size below that)
        self.assertEqual(quality.effective_resolution("2160p", (3840, 2160)), "1080p")
        self.assertEqual(quality.effective_resolution("4k", None), "1080p")
        self.assertEqual(quality.effective_resolution("1440p", (1920, 1080)), "1080p")
        self.assertEqual(quality.effective_resolution("2160p", (1280, 720)), "native")
        for source, cls in (((1920, 1080), "1080p"), ((1920, 1200), "1440p"), ((3440, 1440), "1440p"),
                            ((1280, 720), "720p"), ((1001, 701), "720p"), ((640, 480), "480p"),
                            ((854, 480), "480p"), ((800, 489), "480p"), ((800, 500), "720p"),
                            ((640, 360), "480p"), ((2, 2), "480p"),
                            ((3840, 2160), "2160p"), ((5120, 2880), "2160p"), (None, "1080p")):
            self.assertEqual(quality.rate_class(source), cls, source)
        self.assertEqual(quality.height_label((2560, 1440)), "1440p")
        self.assertIsNone(quality.height_label(None))

    def test_preset_names(self):
        from momento import quality

        for name, want in (("480p", "480p"), ("480P", "480p"), ("sd", "480p"), (" SD ", "480p"),
                           ("hd", "720p"), ("720p", "720p"), ("1080P", "1080p"), ("native", "native"), ("fhd", "1080p"),
                           ("1440p", "1080p"), ("2160p", "1080p"), ("4k", "1080p"), ("2K", "1080p"),
                           ("uhd", "1080p"), ("qhd", "1080p")):
            self.assertEqual(quality.preset(name), want, name)
            self.assertEqual(quality.offered(name), want, name)
        with self.assertRaises(ValueError):
            quality.preset("999p")
        self.assertEqual(quality.offered("999P"), "999p")
        self.assertEqual(quality.resolution({"resolution": "2160p"}), (1920, 1080))
        self.assertEqual(quality.resolution({"resolution": "native"}), None)
        with self.assertRaises(ValueError):
            quality.resolution({"resolution": "8k"})

    def test_native_is_scaled_down_to_1080_lines(self):
        from momento import quality

        for source, want in (((3840, 2160), (1920, 1080)),         # 4K
                             ((2560, 1440), (1920, 1080)),         # 1440p
                             ((3440, 1440), (2580, 1080)),         # ultrawide: aspect kept
                             ((5120, 1440), (3840, 1080)),         # 32:9
                             ((1920, 1200), (1728, 1080)),         # 16:10, a bit taller
                             ((2560, 1600), (1728, 1080)),
                             ((1080, 1920), (608, 1080)),          # portrait
                             ((1920, 1080), (1920, 1080)),         # as it is
                             ((2560, 1080), (2560, 1080)),
                             ((1280, 800), (1280, 800)),
                             ((1271, 713), (1270, 712)),           # even numbers
                             ((1, 1), (2, 2)),
                             (None, None)):
            got = quality.native_size(source)
            self.assertEqual(got, want, source)
            if got:
                self.assertLessEqual(got[1], 1080)
                self.assertEqual((got[0] % 2, got[1] % 2), (0, 0))
        self.assertEqual(quality.recorded_size("native", (3840, 2160)), (1920, 1080))
        self.assertEqual(quality.recorded_size("1080p", (3840, 2160)), (1920, 1080))
        self.assertEqual(quality.recorded_size("720p", (3840, 2160)), (1280, 720))
        self.assertEqual(quality.recorded_size("480p", (3840, 2160)), (854, 480))
        self.assertEqual(quality.recorded_size("480p", (3440, 1440)), (854, 480))     # bars, not stretched
        self.assertEqual(quality.recorded_size("480p", (640, 480)), (854, 480))       # 4:3: bars at the sides
        self.assertEqual(quality.recorded_size("480p", (800, 450)), (800, 450))       # never upscaled
        self.assertEqual(quality.recorded_size("sd", None), (854, 480))
        self.assertEqual(quality.recorded_size("2160p", (3840, 2160)), (1920, 1080))   # older config
        self.assertEqual(quality.recorded_size("1080p", (1271, 713)), (1270, 712))    # never upscaled
        self.assertEqual(quality.recorded_size("1080p", None), (1920, 1080))
        self.assertIsNone(quality.recorded_size("native", None))

    def test_bitrate_uses_what_is_recorded(self):
        from momento import quality

        def kbps(source=None, **cap):
            return quality.bitrate_kbps({"resolution": "1080p", "quality": "high", "fps": 60, **cap}, source)

        self.assertEqual(kbps(), 15_000)                          # unknown source: as configured
        self.assertEqual(kbps((3840, 2160)), 15_000)
        self.assertEqual(kbps((1280, 720)), 10_000)               # 1080p on a 720p window costs 720p
        self.assertEqual(kbps((1280, 720), fps=120), 15_000)      # 10 x 1.5, like 720p at 120
        self.assertEqual(kbps((1280, 720), bitrate_kbps=50_000), 50_000)    # explicit wins
        self.assertEqual(kbps((1920, 1080), resolution="720p"), 10_000)     # fits: unchanged
        # native: the size really recorded, at most 1080 lines
        for source, want in (((3840, 2160), 15_000), ((2560, 1440), 15_000), ((3440, 1440), 15_000),
                             ((1920, 1080), 15_000), ((1280, 720), 10_000), ((1280, 800), 15_000),
                             (None, 15_000)):
            self.assertEqual(kbps(source, resolution="native"), want, source)
        # an older config's 4K / 1440p: what 1080p costs
        self.assertEqual(kbps(resolution="2160p"), 15_000)
        self.assertEqual(kbps((3840, 2160), resolution="2160p", quality="ultra"), 25_000)
        self.assertEqual(kbps((1280, 720), resolution="1440p"), 10_000)
        # 480p, and anything recorded shorter than 480 lines: 480p's row
        self.assertEqual(kbps((1920, 1080), resolution="480p"), 5_000)
        self.assertEqual(kbps((800, 450), resolution="480p"), 5_000)     # recorded as native, same class
        self.assertEqual(kbps((640, 360)), 5_000)                        # 1080p on a tiny window
        self.assertEqual(kbps((640, 360), resolution="native", quality="ultra"), 8_000)

    def test_480p_preset(self):
        from momento import quality

        self.assertEqual(quality.PRESETS["480p"], (854, 480))              # 16:9, even width
        self.assertEqual(list(quality.RESOLUTIONS)[0], "480p")             # smallest first
        self.assertEqual((quality.LABELS["480p"], quality.ALIASES["sd"]), ("480p", "480p"))
        self.assertEqual(quality.DEFAULT_RESOLUTION, "1080p")              # the default is unchanged
        want = {("standard", 60): 3_000, ("high", 60): 5_000, ("ultra", 60): 8_000,
                # x1.5 at 120 fps, rounded like the other rows (4.5 -> 4, 7.5 -> 8)
                ("standard", 120): 4_000, ("high", 120): 8_000, ("ultra", 120): 12_000}
        for (q, f), kbps in want.items():
            self.assertEqual(quality.bitrate_kbps({"resolution": "480p", "quality": q, "fps": f}), kbps, (q, f))
        for q in quality.QUALITIES:                                        # below 720p at every level
            cap = {"quality": q, "fps": 60}
            self.assertLess(quality.bitrate_kbps({**cap, "resolution": "480p"}),
                            quality.bitrate_kbps({**cap, "resolution": "720p"}))
        self.assertAlmostEqual(quality.buffer_gb(5_000), 2.25)             # an hour at 480p High

    def test_storage_uses_what_is_recorded(self):
        from unittest import mock

        from momento import config, storage

        cfg = config.load(Path(tempfile.gettempdir()) / "momento-no-such-config.toml")
        cfg["capture"].update(resolution="1080p", quality="high", fps=60)
        p720 = {**cfg, "capture": {**cfg["capture"], "resolution": "720p"}}
        on_720 = storage.required_bytes(cfg, (1280, 720))
        self.assertEqual(on_720, storage.required_bytes(p720))
        self.assertLess(on_720, storage.required_bytes(cfg))
        self.assertEqual(storage.required_bytes(cfg, (3840, 2160)), storage.required_bytes(cfg))
        self.assertEqual(storage.buffer_bytes(cfg, (1280, 720)), storage.buffer_bytes(p720))
        with mock.patch.object(storage, "free_bytes", return_value=on_720):
            self.assertTrue(storage.check(cfg, source=(1280, 720))["ok"])
            self.assertFalse(storage.check(cfg)["ok"])
            req = storage.requirements(cfg, source=(1280, 720))
        self.assertEqual(storage.label(cfg, (1280, 720)), "720p High")          # what is recorded
        self.assertEqual(storage.label(cfg, (800, 450)), "450p High")
        self.assertEqual(storage.label(cfg, (1280, 800)), "800p High")
        self.assertEqual(storage.label(cfg, (3840, 2160)), "1080p High")
        self.assertEqual(storage.label(cfg), "1080p High")
        self.assertEqual(req["current"], "1080p/high/60")
        self.assertEqual(req["required"]["1080p/high/60"], on_720)
        self.assertEqual(req["required"]["720p/high/60"], on_720)
        self.assertEqual(req["required"]["native/high/60"], on_720)
        self.assertNotIn("2160p/high/60", req["required"])
        # 480p: the smallest need; a window shorter than 480 lines costs what 480p does
        p480 = {**cfg, "capture": {**cfg["capture"], "resolution": "480p"}}
        need_480 = storage.required_bytes(p480)
        self.assertLess(need_480, storage.required_bytes(p720))
        self.assertEqual(storage.required_bytes(p480, (3840, 2160)), need_480)
        self.assertEqual(storage.required_bytes(cfg, (800, 450)), need_480)
        self.assertEqual(req["required"]["480p/high/60"], need_480)
        self.assertEqual(storage.label(p480), "480p High")
        with mock.patch.object(storage, "free_bytes", return_value=need_480):
            self.assertTrue(storage.check(p480)["ok"])
            self.assertFalse(storage.check(p720)["ok"])
            self.assertTrue(storage.check(cfg, source=(800, 450))["ok"])
        # native on a 4K screen records (and costs) 1080p
        native = {**cfg, "capture": {**cfg["capture"], "resolution": "native"}}
        self.assertEqual(storage.required_bytes(native, (3840, 2160)), storage.required_bytes(cfg))
        # an older config's 4K counts as the 1080p it records at; the config is not changed
        old = {**cfg, "capture": {**cfg["capture"], "resolution": "2160p"}}
        self.assertEqual(storage.required_bytes(old), storage.required_bytes(cfg))
        self.assertEqual(storage.label(old), "1080p High")
        self.assertEqual(storage.current_key(old), "1080p/high/60")
        self.assertEqual(old["capture"]["resolution"], "2160p")          # not mutated


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

    def test_recycled_bar_restarts_at_once(self):
        """A bar that exits with BAR_RECYCLE_EXIT (after the gallery) is replaced right away:
        no backoff timer, no warning, and it doesn't count towards the crash backoff."""
        from momento import config, daemon

        self.d.start_bar()
        with self.assertNoLogs("momento.daemon", "WARNING"):
            for i in range(3):
                self.procs[-1].exit(config.BAR_RECYCLE_EXIT)
                self.wait_until(lambda: len(self.procs) == i + 2)
                self.assertIs(self.d.bar_proc, self.procs[-1])
        self.assertEqual(self.timers, [])
        self.assertEqual(self.d._bar_backoff, daemon.BAR_BACKOFF_MIN)
        self.procs[-1].exit(1)                      # a real crash still backs off from the start
        self.wait_until(lambda: len(self.timers) == 1)
        self.assertEqual(self.timers[0][0], 1000)
        self.timers[0][1]()
        self.d.stop()                               # stopping: a recycle exit starts nothing
        self.procs[-1].exit(config.BAR_RECYCLE_EXIT)
        time.sleep(0.1)
        self.assertEqual(len(self.procs), 5)

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
        # native may scale a screen taller than 1080 lines down (at the same size it passes through)
        self.assertIn("videoscale add-borders=true", screen._video_chain(self.pipeline._Variant("x264enc", False)))

    def pin(self, rec, size, known=None, first=None):
        """Plan the output like _build (the source's size ``known`` in advance, or not),
        then feed the source's first caps (``first``, default ``size``) to
        Recorder._pin_size, as the probe would (no pipeline).

        Returns (the size the output is pinned to or None, the encoder bitrate set
        at runtime or None). ``self.chain`` is the video chain as built,
        ``self.caps_sets`` the caps the "size" capsfilter was given at runtime."""
        from unittest import mock

        Gst = self.pipeline.Gst
        first = first or size
        rec._known_size = known
        rec._prepare_size()
        self.chain = rec._video_chain(self.pipeline._Variant("x264enc", False))
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        capsfilter.set_property("caps", Gst.Caps.from_string(rec._output_caps()))   # as built
        self.caps_sets = []
        capsfilter.connect("notify::caps", lambda el, _p: self.caps_sets.append(el.get_property("caps")))
        info = mock.Mock()
        info.get_event.return_value = Gst.Event.new_caps(
            Gst.Caps.from_string(f"video/x-raw,width={first[0]},height={first[1]}"))
        with mock.patch.object(rec, "_encoder_settings") as settings:
            self.assertEqual(rec._pin_size(None, info, (capsfilter, object(), "x264enc")), Gst.PadProbeReturn.OK)
        out = rec._output_size()
        caps = capsfilter.get_property("caps").get_structure(0)
        if out is not None:
            self.assertEqual((caps.get_int("width")[1], caps.get_int("height")[1]), out)
        else:
            self.assertFalse(caps.has_field("width"))
        if rec._locked_size is None:
            self.assertEqual(self.caps_sets, [])       # the preset's (or the source's own) size as built
        self.assertEqual(rec.source_size, tuple(first))
        return rec._locked_size, (settings.call_args[0][2] if settings.called else None)

    def test_native_never_records_taller_than_1080p(self):
        rec = self.recorder(target="screen", resolution="native")
        self.assertEqual(self.pin(rec, (3840, 2160)), ((1920, 1080), None))   # 1080p's bitrate already
        self.assertEqual(rec.resolution_effective, "native")
        self.assertEqual(self.pin(rec, (3440, 1440)), ((2580, 1080), None))   # ultrawide: aspect kept
        self.assertEqual(self.pin(rec, (2560, 1440)), ((1920, 1080), None))
        self.assertEqual(self.pin(rec, (1920, 1080)), (None, None))           # its own size, as it is
        self.assertEqual(self.pin(rec, (1280, 720)), (None, 10_000))          # 720p's bitrate
        win = self.recorder(target="window", resolution="native")
        win.start(interactive=True)
        self.assertEqual(self.pin(win, (3840, 2160)), ((1920, 1080), None))   # a 4K window
        self.assertEqual(self.pin(win, (1271, 713)), ((1270, 712), 10_000))   # locked, even numbers

    def test_presets_and_an_older_4k_config(self):
        rec = self.recorder(target="screen", resolution="1080p")
        self.assertEqual(self.pin(rec, (3840, 2160)), (None, None))           # scaled by the preset
        self.assertEqual(rec.resolution_effective, "1080p")
        self.assertEqual(self.pin(rec, (1271, 713)), ((1270, 712), 10_000))   # never upscaled
        self.assertEqual(rec.resolution_effective, "native")
        sd = self.recorder(target="screen", resolution="sd")                  # the alias
        self.assertEqual((sd.size_name, sd.size), ("480p", (854, 480)))
        chain = sd._video_chain(self.pipeline._Variant("x264enc", False))
        self.assertIn("width=854,height=480", chain)
        self.assertIn("add-borders=true", chain)                              # other shapes get bars
        self.assertEqual(self.pin(sd, (1920, 1080)), (None, None))
        self.assertEqual(sd.resolution_effective, "480p")
        old = self.recorder(target="screen", resolution="2160p")              # records at 1080p
        self.assertEqual((old.size_name, old.size), ("1080p", (1920, 1080)))
        self.assertIn("width=1920,height=1080", old._video_chain(self.pipeline._Variant("x264enc", False)))
        self.assertEqual(self.pin(old, (3840, 2160)), (None, None))
        self.assertEqual(old.resolution_effective, "1080p")


    def test_size_known_before_the_pipeline_is_built(self):
        """The portal said how big the stream is: the "size" capsfilter is built at the
        final output size and the first caps change nothing (no renegotiation)."""
        cases = (
            # target, resolution, source -> recorded, effective
            ("window", "native", (1920, 1080), (1920, 1080), "native"),   # the live bug: 1080p window, native
            ("window", "native", (1271, 713), (1270, 712), "native"),     # locked, even numbers
            ("window", "1080p", (1271, 713), (1270, 712), "native"),      # capped: never upscaled
            ("screen", "1080p", (1280, 720), (1280, 720), "native"),      # capped screen
            ("screen", "native", (3840, 2160), (1920, 1080), "native"),   # shrink to 1080 lines
            ("screen", "native", (3440, 1440), (2580, 1080), "native"),   # ultrawide, aspect kept
            ("window", "native", (3840, 2160), (1920, 1080), "native"),   # a 4K window
            ("screen", "1080p", (3840, 2160), (1920, 1080), "1080p"),     # the preset scales it
            ("screen", "720p", (1920, 1200), (1280, 720), "720p"),
            ("screen", "480p", (1920, 1080), (854, 480), "480p"),
            ("screen", "480p", (1920, 1200), (854, 480), "480p"),        # 16:10: bars, not stretched
            ("window", "480p", (800, 450), (800, 450), "native"),        # a tiny window: never upscaled
        )
        from momento import quality

        for target, res, source, recorded, effective in cases:
            with self.subTest(target=target, res=res, source=source):
                rec = self.recorder(target=target, resolution=res)
                rec.start(interactive=True)
                _, runtime_kbps = self.pin(rec, source, known=source)
                self.assertIsNone(runtime_kbps)                                         # nothing at runtime
                self.assertIn(f"width={recorded[0]},height={recorded[1]}", self.chain)            # built that way
                self.assertEqual(self.caps_sets, [])
                self.assertEqual((rec.source_size, rec.resolution_effective), (source, effective))
                self.assertEqual(rec._kbps, quality.bitrate_kbps(rec.cfg["capture"], source))
        rec = self.recorder(target="screen", resolution="native")
        self.pin(rec, (1920, 1080), known=(1920, 1080))
        self.assertNotIn("width=", self.chain)            # native screen at its own size: passes through
        self.assertEqual(self.caps_sets, [])

    def test_runtime_pin_is_a_fallback(self):
        """Unknown in advance (or not what the portal said): the capsfilter changes once,
        only when its caps differ, and the change opens the renegotiation grace."""
        rec = self.recorder(resolution="native")
        rec.start(interactive=True)
        self.assertEqual(self.pin(rec, (1920, 1080)), ((1920, 1080), None))     # unknown: pinned at runtime
        self.assertEqual(len(self.caps_sets), 1)
        rec._settle_from = 0.0
        self.assertEqual(self.pin(rec, (1280, 720), known=(1920, 1080)), ((1280, 720), 10_000))  # differs
        self.assertEqual(len(self.caps_sets), 1)
        self.assertGreater(rec._settle_from, 0.0)                               # a change was made just now
        screen = self.recorder(target="screen", resolution="1080p")
        self.assertEqual(self.pin(screen, (1920, 1080)), (None, None))           # the preset's caps: left alone
        self.assertEqual(self.caps_sets, [])
        self.assertEqual(self.pin(screen, (1920, 1200), known=(1920, 1080)), (None, None))  # same output
        self.assertEqual(self.caps_sets, [])

    def test_test_source_and_portal_announce_the_size(self):
        import os

        from unittest import mock

        rec = self.recorder(source="test")
        rec.test_size = (1280, 720)
        rec.start(interactive=True)
        self.assertEqual(rec._known_size, (1280, 720))
        rec = self.recorder(source="portal")
        rec.start(interactive=True)
        rec._portal = mock.Mock(stream_size=(1920, 1080))
        rec._on_portal_ready(os.open(os.devnull, os.O_RDONLY), 77)
        self.assertEqual(rec._known_size, (1920, 1080))
        rec._build_and_play.assert_called_once()
        rec._close_portal()
        self.assertIsNone(rec._known_size)                                      # the next session says its own
        rec._portal = mock.Mock(stream_size=None)                               # a portal that doesn't say
        rec._on_portal_ready(os.open(os.devnull, os.O_RDONLY), 78)
        self.assertIsNone(rec._known_size)
        rec._close_portal()

    def started(self, **capture):
        """A recorder whose portal stream just started playing (no real pipeline)."""
        import os

        from unittest import mock

        self.token.parent.mkdir(parents=True, exist_ok=True)
        self.token.write_text("tok")
        rec = self.recorder(**capture)
        rec.start(interactive=True)
        rec._portal = mock.Mock(stream_size=(1920, 1080))
        rec._on_portal_ready(os.open(os.devnull, os.O_RDONLY), 77)
        self.addCleanup(rec._close_portal)
        rec._settle_from = time.monotonic()           # what _build_and_play does
        rec.source_size = (1920, 1080)
        return rec

    def test_early_buffers_removed_restarts_instead_of_closing(self):
        from unittest import mock

        GLib = self.pipeline.GLib
        rec = self.started(resolution="native")
        self.states.clear()
        with mock.patch.object(self.pipeline.GLib, "idle_add") as idle:
            rec._on_pipeline_failure("all buffers have been removed", source_lost=True)
        self.assertEqual(self.states, [])                            # not no_window: not the window closing
        idle.assert_called_once_with(rec._retry_build)
        self.assertTrue(self.token.exists())
        self.assertFalse(rec._stop_requested)
        self.assertEqual(rec._known_size, (1920, 1080))              # rebuilt at the size now known
        rec._retry_build()
        self.assertEqual(rec._build_and_play.call_count, 2)
        # Once per start: failing again (the node really is gone) ends it as before.
        rec._settle_from = time.monotonic()
        with mock.patch.object(GLib, "idle_add") as idle:
            rec._on_pipeline_failure("all buffers have been removed", source_lost=True)
        idle.assert_not_called()
        self.assertEqual(self.states, [("no_window", self.pipeline.WINDOW_CLOSED)])
        self.assertFalse(self.token.exists())

    def test_late_node_destruction_closes_the_window(self):
        from unittest import mock

        rec = self.started(resolution="native")
        rec._got_fragment = rec.recording = True
        rec._settle_from = time.monotonic() - 10                     # well after the start
        self.states.clear()
        with mock.patch.object(self.pipeline.GLib, "idle_add") as idle:
            rec._on_pipeline_failure("all buffers have been removed", source_lost=True)
        idle.assert_not_called()
        self.assertEqual(self.states, [("no_window", self.pipeline.WINDOW_CLOSED)])
        self.assertFalse(self.token.exists())

    def test_early_failure_without_the_portal_session_closes(self):
        from unittest import mock

        rec = self.started(resolution="native")
        rec._got_fragment = rec.recording = True
        rec._portal.close()
        rec._portal = None                                           # the session already went away
        with mock.patch.object(self.pipeline.GLib, "idle_add") as idle:
            rec._on_pipeline_failure("all buffers have been removed", source_lost=True)
        idle.assert_not_called()
        self.assertEqual(self.states[-1], ("no_window", self.pipeline.WINDOW_CLOSED))

    def test_other_early_failures_are_not_restarted(self):
        from unittest import mock

        rec = self.started(resolution="native")
        rec._got_fragment = True
        with mock.patch.object(self.pipeline.GLib, "idle_add") as idle:
            rec._on_pipeline_failure("encoder hiccup")               # not the source
        idle.assert_not_called()
        self.assertEqual(self.states[-1], ("error", self.pipeline.WINDOW_STOPPED))

    def test_screen_mode_early_failure_restarts_on_the_same_stream(self):
        from unittest import mock

        rec = self.started(target="screen", resolution="native")
        self.states.clear()
        with mock.patch.object(self.pipeline.GLib, "idle_add") as idle:
            rec._on_pipeline_failure("all buffers have been removed", source_lost=True)
        idle.assert_called_once_with(rec._retry_build)
        self.assertEqual(self.states, [])
        self.assertEqual(rec._retry_id, 0)                           # no 3 s error retry, no new portal session
        self.assertIsNotNone(rec._pw_fd)


@unittest.skipUnless(_have_gst(), "GStreamer (PyGObject) not available")
class PortalSizeHintTest(unittest.TestCase):
    """The portal's stream size is only a hint (KDE announces a scaled monitor's logical size),
    and the MOMENTO_DEBUG_CAPTURE_ONLY debug pipeline."""

    def setUp(self):
        import copy

        from momento import config, pipeline

        self.pipeline = pipeline
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["capture"].update(source="portal", target="screen", resolution="1080p")
        self.cfg["buffer"]["dir"] = str(Path(self._tmp.name) / "buffer")
        self.states = []

    def recorder(self, **capture):
        self.cfg["capture"].update(capture)
        rec = self.pipeline.Recorder(self.cfg, RingBuffer(3600), lambda s, m: self.states.append((s, m)))
        rec._plan_variants = lambda: [self.pipeline._Variant("vah264enc", True)]
        rec._start_portal = mock.Mock()          # never the real portal
        rec._build_and_play = mock.Mock()
        self.addCleanup(rec._cancel_retry)
        self.addCleanup(rec._close_portal)
        return rec

    def ready(self, rec, announced, displays):
        rec.start(interactive=True)
        rec._portal = mock.Mock(stream_size=announced)
        with mock.patch.object(self.pipeline, "native_display_sizes", return_value=displays):
            rec._on_portal_ready(os.open(os.devnull, os.O_RDONLY), 77)
        rec._build_and_play.assert_called_once()
        return rec._known_size

    def test_scaled_monitor_logical_size_is_ignored(self):
        """1600x900 on a 1920x1080 screen at 120 %: never planned (it would pin 1600x900)."""
        rec = self.recorder()
        self.assertIsNone(self.ready(rec, (1600, 900), {(1920, 1080), (1080, 1920)}))
        rec._prepare_size()
        rec._video_chain(self.pipeline._Variant("vah264enc", True))
        self.assertIsNone(rec._locked_size)                       # the preset's 1920x1080, not pinned smaller
        self.assertEqual(rec._output_caps(), "video/x-raw(memory:VAMemory),format=NV12,width=1920,height=1080")
        # The first caps (the real 1920x1080) then change nothing: no renegotiation.
        Gst = self.pipeline.Gst
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        capsfilter.set_property("caps", Gst.Caps.from_string(rec._output_caps()))
        changed = []
        capsfilter.connect("notify::caps", lambda *_a: changed.append(True))
        info = mock.Mock()
        info.get_event.return_value = Gst.Event.new_caps(Gst.Caps.from_string("video/x-raw,width=1920,height=1080"))
        rec._pin_size(None, info, (capsfilter, None, "vah264enc"))
        self.assertEqual(changed, [])
        self.assertEqual(rec.source_size, (1920, 1080))

    def test_monitor_size_matching_a_display_is_used(self):
        self.assertEqual(self.ready(self.recorder(), (1920, 1080), {(1920, 1080), (1080, 1920)}), (1920, 1080))
        # A 1280x800 screen really is smaller than 1080p: planned (capped) before the stream runs.
        rec = self.recorder()
        self.assertEqual(self.ready(rec, (1280, 800), {(1280, 800), (800, 1280)}), (1280, 800))
        rec._prepare_size()
        self.assertEqual(rec._locked_size, (1280, 800))

    def test_monitor_size_without_readable_displays_is_ignored(self):
        self.assertIsNone(self.ready(self.recorder(), (1920, 1080), set()))
        self.assertIsNone(self.ready(self.recorder(), None, {(1920, 1080)}))

    def test_window_size_is_used_as_announced(self):
        token = self.pipeline.config.portal_token_path("window")
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("tok")
        self.addCleanup(token.unlink, missing_ok=True)
        rec = self.recorder(target="window")
        self.assertEqual(self.ready(rec, (1280, 720), set()), (1280, 720))

    def test_native_display_sizes_reads_connected_preferred_modes(self):
        root = Path(self._tmp.name) / "drm"
        for name, status, modes in (("card1-eDP-1", "connected", "1920x1080\n1920x1080\n1680x1050\n"),
                                    ("card1-DP-1", "disconnected", ""),
                                    ("card1-DP-2", "connected", "2560x1440\n1920x1080\n"),
                                    ("card1-Writeback-1", "unknown", "")):
            (root / name).mkdir(parents=True)
            (root / name / "status").write_text(status + "\n")
            (root / name / "modes").write_text(modes)
        self.assertEqual(self.pipeline.native_display_sizes(root),
                         {(1920, 1080), (1080, 1920), (2560, 1440), (1440, 2560)})
        self.assertEqual(self.pipeline.native_display_sizes(root / "missing"), set())

    def test_capture_only_switch(self):
        env = self.pipeline.CAPTURE_ONLY_ENV
        for value, on in (("1", True), ("yes", True), ("", False), ("0", False), ("false", False)):
            with mock.patch.dict(os.environ, {env: value}):
                self.assertEqual(self.pipeline.capture_only(), on, value)
        with mock.patch.dict(os.environ):
            os.environ.pop(env, None)
            self.assertFalse(self.pipeline.capture_only())

    def test_capture_only_pipeline_is_source_into_fakesink(self):
        Gst = self.pipeline.Gst
        rec = self.recorder(source="test")
        rec.source_name = "test"
        for variant, memory in ((self.pipeline._Variant("vah264enc", True), None),
                                (self.pipeline._Variant("x264enc", False), "video/x-raw")):
            if variant.zero_copy and not self.pipeline._have("vapostproc"):
                continue
            with mock.patch.dict(os.environ, {self.pipeline.CAPTURE_ONLY_ENV: "1"}), \
                    self.assertLogs("momento.pipeline", "WARNING") as logs:
                pl = rec._build(variant)
            self.assertIn("capture only", "\n".join(logs.output))
            names = {el.get_factory().get_name() for el in pl.children}
            self.assertEqual(names, {"videotestsrc", "capsfilter", "fakesink"})   # no scaler, encoder, mux, audio
            caps = pl.get_by_name("capture_caps").get_property("caps")
            if memory is None:   # offered what vapostproc takes (DMA-BUF / VA memory), as when recording
                self.assertTrue(any(caps.get_features(i).contains("memory:DMABuf") for i in range(caps.get_size())))
            else:
                self.assertEqual(caps.to_string(), memory)
            pl.set_state(Gst.State.NULL)

    def test_debug_stage_switch(self):
        stage_env, alias = self.pipeline.STAGE_ENV, self.pipeline.CAPTURE_ONLY_ENV
        with mock.patch.dict(os.environ):
            os.environ.pop(stage_env, None)
            os.environ.pop(alias, None)
            self.assertIsNone(self.pipeline.debug_stage())
            for stage in self.pipeline.STAGES:
                os.environ[stage_env] = f" {stage.upper()} "
                self.assertEqual(self.pipeline.debug_stage(), stage)
            os.environ.pop(stage_env)
            os.environ[alias] = "1"                                    # the old switch: capture
            self.assertEqual(self.pipeline.debug_stage(), "capture")
            os.environ[stage_env] = "encode"                           # the stage wins over the alias
            self.assertEqual(self.pipeline.debug_stage(), "encode")
            os.environ.pop(alias)
            os.environ[stage_env] = "gpu"
            with self.assertLogs("momento.pipeline", "WARNING") as logs:
                self.assertIsNone(self.pipeline.debug_stage())         # unknown: a normal recording
            self.assertIn("gpu", "\n".join(logs.output))

    def _stage_pipeline(self, rec, variant, stage):
        env = {self.pipeline.STAGE_ENV: stage} if stage else {}
        with mock.patch.dict(os.environ, env):
            for name in (self.pipeline.CAPTURE_ONLY_ENV,) + (() if stage else (self.pipeline.STAGE_ENV,)):
                os.environ.pop(name, None)
            with self.assertLogs("momento.pipeline", "WARNING") as logs:
                self.pipeline.log.warning("marker")                    # so a stage that logs nothing still passes here
                pl = rec._build(variant)
        self.addCleanup(pl.set_state, self.pipeline.Gst.State.NULL)
        names = {el.get_factory().get_name() for el in pl.children}
        warnings = [line for line in logs.output if "marker" not in line]
        return pl, names, warnings

    def test_debug_stages_build_the_expected_elements(self):
        p = self.pipeline
        if not (p._have("vah264enc") and p._have("vapostproc")):
            self.skipTest("needs vah264enc and vapostproc")
        rec = self.recorder(source="test")
        rec.source_name = "test"
        v = p._Variant("vah264enc", True)
        convert = {"videotestsrc", "vapostproc", "capsfilter", "queue", "videorate"}
        mux = {"splitmuxsink", "h264parse"}
        audio = {"audiotestsrc", "avenc_aac"} if p._have("avenc_aac") else {"audiotestsrc"}
        cases = {
            "convert": (convert | {"fakesink"}, {"vah264enc"} | mux | audio),
            "encode": (convert | {"vah264enc", "fakesink"}, mux | audio),
            "noaudio": (convert | {"vah264enc"} | mux, audio | {"fakesink"}),
            "lowpower": (convert | {"vah264enc"} | mux | audio, {"fakesink"}),
        }
        for stage, (present, absent) in cases.items():
            with self.subTest(stage=stage):
                pl, names, warnings = self._stage_pipeline(rec, v, stage)
                self.assertTrue(present <= names, present - names)
                self.assertFalse(absent & names, absent & names)
                self.assertEqual(len(warnings), 1, warnings)            # one clear warning naming the stage
                self.assertIn(f"{p.STAGE_ENV}={stage}", warnings[0])
                if stage in ("convert", "encode"):
                    # The real chain's conversion: NV12 in VA memory at the final size.
                    self.assertEqual(pl.get_by_name("size").get_property("caps").to_string(),
                                     "video/x-raw(memory:VAMemory), format=(string)NV12, "
                                     "width=(int)1920, height=(int)1080")
                enc = pl.get_by_name("enc")
                if enc is not None:
                    self.assertEqual(enc.get_property("key-int-max"), rec.fps)   # the normal settings
                    self.assertEqual(enc.get_property("bitrate"), rec._kbps)
                    lowpower = stage == "lowpower"
                    self.assertEqual(enc.get_property("target-usage"), 2 if lowpower else 4)
                    self.assertEqual(enc.get_property("ref-frames"), 1 if lowpower else 3)
                    self.assertEqual(enc.get_property("dct8x8"), not lowpower)
        # Capture via the stage name: the source straight into a fakesink.
        _pl, names, warnings = self._stage_pipeline(rec, v, "capture")
        self.assertEqual(names, {"videotestsrc", "capsfilter", "fakesink"})
        self.assertIn("capture only", warnings[0])

    def test_unknown_debug_stage_records_normally(self):
        p = self.pipeline
        if not (p._have("vah264enc") and p._have("vapostproc")):
            self.skipTest("needs vah264enc and vapostproc")
        rec = self.recorder(source="test")
        rec.source_name = "test"
        v = p._Variant("vah264enc", True)
        _pl, normal, none = self._stage_pipeline(rec, v, None)
        self.assertEqual(none, [])
        pl, names, warnings = self._stage_pipeline(rec, v, "bogus")
        self.assertEqual(names, normal)
        self.assertTrue({"splitmuxsink", "vah264enc", "audiotestsrc"} <= names)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("bogus", warnings[0])
        self.assertEqual(pl.get_by_name("enc").get_property("target-usage"), 4)

    def _stage_recorder(self, *factories):
        p = self.pipeline
        for name in ("vah264enc", "vapostproc") + factories:
            if not p._have(name):
                self.skipTest(f"needs {name}")
        rec = self.recorder(source="test")
        rec.source_name = "test"
        return rec, p._Variant("vah264enc", True)

    def _stage_format_variant(self, rec, stage):
        """The first variant the h265 / av1 stage plans (a detection where every VA encoder works)."""
        from momento import codecs

        p = self.pipeline
        det = codecs.Detection(vendor="amd", present=("vah264enc", "vah265enc", "vaav1enc"),
                               works={"vah264enc": True, "vah265enc": True, "vaav1enc": True})
        rec._plan_variants = lambda: p.Recorder._plan_variants(rec)
        with mock.patch.dict(os.environ, {p.STAGE_ENV: stage}), \
                mock.patch.object(codecs, "DETECTOR", codecs.Detector(key=lambda: None)), \
                self.assertLogs("momento.pipeline", "WARNING") as logs:
            rec._start_capture(det)
        return rec._variants[0], logs.output

    def test_codec_stages_force_the_format(self):
        """h265 / av1: the normal recording in that format, same bitrate and GOP, no B-frames."""
        p = self.pipeline
        rec, _v = self._stage_recorder("vah265enc", "h265parse", "vaav1enc", "av1parse", "matroskamux")
        audio = {"audiotestsrc", "avenc_aac"} if p._have("avenc_aac") else {"audiotestsrc"}
        cases = {"h265": ("vah265enc", "h265parse", "mpegtsmux", "h265", ".ts"),
                 "av1": ("vaav1enc", "av1parse", "matroskamux", "av1", ".mkv")}
        for stage, (encoder, parser, muxer, codec, suffix) in cases.items():
            with self.subTest(stage=stage):
                v, planned = self._stage_format_variant(rec, stage)
                self.assertEqual(v, p._Variant(encoder, True))
                self.assertEqual(len(planned), 1, planned)                     # one clear warning naming it
                self.assertIn(f"{p.STAGE_ENV}={stage}", planned[0])
                pl, names, warnings = self._stage_pipeline(rec, v, stage)
                self.assertEqual(warnings, [])                                 # the build itself is normal
                self.assertTrue({"vapostproc", "splitmuxsink", encoder, parser} | audio <= names, names)
                self.assertFalse({"vah264enc", "h264parse", "fakesink"} & names)
                mux = pl.get_by_name("mux")
                self.assertEqual(mux.get_property("muxer").get_factory().get_name(), muxer)
                self.assertTrue(mux.get_property("location").endswith(f"seg%08d{suffix}"))
                enc = pl.get_by_name("enc")
                self.assertEqual(enc.get_property("bitrate"), rec._kbps)
                self.assertEqual(enc.get_property("key-int-max"), rec.fps)
                if stage == "h265":
                    self.assertEqual(enc.get_property("b-frames"), 0)
                else:
                    self.assertEqual(enc.get_property("hierarchical-level"), 1)   # no future references
                # The ring buffer is told the real codec, so sessions of another codec are never joined.
                rec._pipeline, rec._params = pl, None
                self.assertEqual(rec._stream_params()["codec"], codec)
                rec._pipeline, rec._params = None, None
                # A runtime bitrate change keeps the format's settings.
                rec._encoder_settings(enc, encoder, 7000)
                self.assertEqual(enc.get_property("bitrate"), 7000)

    def test_codec_stage_without_the_encoder_falls_back(self):
        from momento import codecs

        p = self.pipeline
        rec, _v = self._stage_recorder()
        det = codecs.Detection(vendor="intel", present=("vah264enc",), works={"vah264enc": True})
        rec._plan_variants = lambda: p.Recorder._plan_variants(rec)
        with mock.patch.dict(os.environ, {p.STAGE_ENV: "av1"}), \
                mock.patch.object(codecs, "DETECTOR", codecs.Detector(key=lambda: None)), \
                self.assertLogs("momento.pipeline", "WARNING"):
            rec._start_capture(det)
        self.assertEqual(rec._formats, ["h264"])           # no AV1 (nor H.265) here: H.264
        self.assertEqual(rec._variants[0].encoder, "vah264enc")

    def test_lowbitrate_stage(self):
        p = self.pipeline
        rec, v = self._stage_recorder()
        pl, names, warnings = self._stage_pipeline(rec, v, "lowbitrate")
        self.assertTrue({"vah264enc", "h264parse", "splitmuxsink", "audiotestsrc"} <= names)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f"{p.STAGE_ENV}=lowbitrate", warnings[0])
        enc = pl.get_by_name("enc")
        self.assertEqual(enc.get_property("bitrate"), round(rec._kbps * 0.6))
        self.assertEqual(enc.get_property("rate-control").value_nick, "cbr")
        self.assertEqual(enc.get_property("target-usage"), 4)
        rec._encoder_settings(enc, "vah264enc", 10000)            # 10 Mbps Standard 1080p60 -> 6 Mbps
        self.assertEqual(enc.get_property("bitrate"), 6000)

    def test_vbr_stage(self):
        p = self.pipeline
        rec, v = self._stage_recorder()
        probe = p.Gst.ElementFactory.make("vah264enc", None)
        p.Gst.util_set_object_arg(probe, "rate-control", "qvbr")
        want = "qvbr" if probe.get_property("rate-control").value_nick == "qvbr" else "vbr"
        pl, names, warnings = self._stage_pipeline(rec, v, "vbr")
        self.assertTrue({"vah264enc", "h264parse", "splitmuxsink", "audiotestsrc"} <= names)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f"{p.STAGE_ENV}=vbr", warnings[0])
        self.assertIn(want.upper(), warnings[0])
        enc = pl.get_by_name("enc")
        self.assertEqual(enc.get_property("rate-control").value_nick, want)
        self.assertEqual(enc.get_property("bitrate"), rec._kbps)                  # the target, as with CBR
        self.assertEqual(enc.get_property("key-int-max"), rec.fps)
        self.assertEqual(enc.get_property("b-frames"), 0)
        self.assertEqual(enc.get_property("target-usage"), 4)

    def test_lowprio_stage(self):
        p = self.pipeline
        Gst = p.Gst
        rec, v = self._stage_recorder()
        pl, names, warnings = self._stage_pipeline(rec, v, "lowprio")
        self.assertTrue({"vah264enc", "h264parse", "splitmuxsink", "audiotestsrc"} <= names)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f"{p.STAGE_ENV}=lowprio", warnings[0])
        enc = pl.get_by_name("enc")
        self.assertEqual(enc.get_property("bitrate"), rec._kbps)
        self.assertEqual(enc.get_property("rate-control").value_nick, "cbr")
        # A 2-frame leaky queue right before the encoder.
        encq = pl.get_by_name("encq")
        self.assertEqual(encq.get_factory().get_name(), "queue")
        self.assertEqual(encq.get_property("max-size-buffers"), 2)
        self.assertEqual(encq.get_property("max-size-time"), 0)
        self.assertEqual(encq.get_property("leaky").value_nick, "downstream")
        self.assertIs(encq.get_static_pad("src").get_peer().get_parent_element(), enc)
        self.assertEqual(p.GLib.ThreadPool.get_max_unused_threads(), 0)
        # The encq thread (and only it) is lowered when it starts, by a sync bus handler.
        with mock.patch.object(p.os, "setpriority") as nice, mock.patch.object(p.os, "sched_setscheduler") as sched:
            for owner, kind in ((pl.get_by_name("size"), Gst.StreamStatusType.ENTER),
                                (encq, Gst.StreamStatusType.CREATE),
                                (encq, Gst.StreamStatusType.LEAVE)):
                pl.get_bus().post(Gst.Message.new_stream_status(owner, kind, owner))
            nice.assert_not_called()
            sched.assert_not_called()
            pl.get_bus().post(Gst.Message.new_stream_status(encq.get_static_pad("src"),
                                                           Gst.StreamStatusType.ENTER, encq))
            nice.assert_called_once_with(os.PRIO_PROCESS, threading.get_native_id(), 19)
            sched.assert_called_once()
            self.assertEqual(sched.call_args.args[:2], (0, os.SCHED_IDLE))
        # Other stages have no such queue.
        pl, _names, _w = self._stage_pipeline(rec, v, None)
        self.assertIsNone(pl.get_by_name("encq"))

    def test_mux_less_stage_reports_recording_and_flushes_at_once(self):
        p = self.pipeline
        if not (p._have("vah264enc") and p._have("vapostproc")):
            self.skipTest("needs vah264enc and vapostproc")
        Gst = p.Gst
        rec = self.recorder(source="test")
        rec.source_name = "test"
        pl, _names, _w = self._stage_pipeline(rec, p._Variant("vah264enc", True), "encode")
        rec._pipeline = pl
        msg = Gst.Message.new_state_changed(pl, Gst.State.PAUSED, Gst.State.PLAYING, Gst.State.VOID_PENDING)
        rec._on_message(pl.get_bus(), msg)
        self.assertTrue(rec.recording)
        self.assertEqual(self.states[-1], ("recording", None))
        done = mock.Mock()
        with mock.patch.object(p.GLib, "idle_add", lambda fn, *a: fn(*a)), \
                mock.patch.object(p.GLib, "timeout_add", return_value=0):
            rec.flush(done)
        done.assert_called_once_with()
        self.assertEqual(rec._flush_waiters, [])
        rec._pipeline = None


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
        self.stream_props = {}   # the Start response's stream properties

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
                bus.respond(args[2], {"streams": [(77, bus.stream_props)], "restore_token": "fresh-token"})
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

    def run_portal(self, target, source_types=3, stream_props=None):
        from momento import portal

        bus = _FakePortalBus(source_types)
        bus.stream_props = stream_props or {}
        got = {}
        p = portal.ScreenCastPortal(bus, self.config.portal_token_path(target), False,
                                    portal.SOURCE_WINDOW if target == "window" else portal.SOURCE_MONITOR)
        p.start(lambda fd, node: got.update(fd=fd, node=node), lambda msg: got.update(error=msg))
        self.portal = p
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

    def test_stream_size(self):
        import dbus

        from momento import portal

        got, _ = self.run_portal("window", stream_props={
            "size": dbus.Struct((dbus.Int32(1280), dbus.Int32(720)), signature="ii"),
            "position": dbus.Struct((dbus.Int32(0), dbus.Int32(0)), signature="ii"),
            "source_type": dbus.UInt32(2)})
        self.assertEqual(got.get("node"), 77, got)
        self.assertEqual(self.portal.stream_size, (1280, 720))
        got, _ = self.run_portal("screen")                          # a portal that doesn't say
        self.assertEqual(got.get("node"), 77, got)
        self.assertIsNone(self.portal.stream_size)
        self.assertEqual(portal.stream_size((77, {"size": (3840, 2160)})), (3840, 2160))
        for stream in ((77,), (77, {}), (77, None), (77, {"size": (0, 720)}), (77, {"size": "big"}),
                       (77, {"size": (1280,)}), None):
            self.assertIsNone(portal.stream_size(stream), stream)

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
                         {"enabled": True, "chord": ("mode", "dpad_down"), "hold_ms": 0, "exclusive": True})
        self.assertEqual(self.config.load_controller(self.path), self.config.controller(cfg))
        cur = self.settings.current(cfg)
        self.assertEqual(cur["controller"], "ps_down")
        self.assertNotIn("controller_open", cur)                    # tap / exclusive: config only
        self.assertNotIn("controller_exclusive", cur)
        d = self.settings.describe(cfg, devices={"outputs": [], "inputs": []})
        self.assertEqual(d["choices"]["controller"], ["off", "ps_down"])   # what the bar offers
        self.assertEqual(self.settings.controller_label("ps_down"), "PS / Xbox + Down")
        self.assertIsInstance(d["controller_available"], bool)

    def test_explicit_chord_is_kept(self):
        """A config that sets open_chord keeps it; one that doesn't gets the new default."""
        self.path.write_text('[controller]\nopen_chord = ["select", "start"]\n')
        cfg = self.config.load(self.path)
        self.assertEqual(self.config.controller(cfg)["chord"], ("select", "start"))
        self.assertEqual(self.settings.current(cfg)["controller"], "view_menu")
        self.path.write_text('[controller]\nexclusive = true\n')
        self.assertEqual(self.config.load_controller(self.path)["chord"], ("mode", "dpad_down"))

    def test_bad_values_fall_back(self):
        self.path.write_text('[controller]\nopen_chord = ["select", "turbo"]\nhold_ms = -3\n')
        with self.assertLogs("momento.config", "WARNING"):
            ctl = self.config.load_controller(self.path)
        self.assertEqual((ctl["chord"], ctl["hold_ms"]), (("mode", "dpad_down"), 0))     # the defaults
        self.path.write_text("[controller\n")                       # broken TOML: defaults
        self.assertTrue(self.config.load_controller(self.path)["enabled"])
        self.assertTrue(self.config.load_controller(Path(self._tmp.name) / "missing.toml")["enabled"])

    def test_normalize(self):
        v = self.settings.validate
        self.assertEqual(v({"controller": "View + Menu"}), {"controller": "view_menu"})
        self.assertEqual(v({"controller": "PS / Xbox + Down"}), {"controller": "ps_down"})
        self.assertEqual(v({"controller": "ps+down"}), {"controller": "ps_down"})
        self.assertEqual(v({"controller": "Guide + D-pad Down"}), {"controller": "ps_down"})
        self.assertEqual(v({"controller": ["home", "dpad_down"]}), {"controller": "ps_down"})
        self.assertEqual(v({"controller": "xbox+up"}), {"controller": "mode+dpad_up"})
        self.assertEqual(v({"controller": "left paddle"}), {"controller": "left_paddle"})
        self.assertEqual(v({"controller": "l3+r3"}), {"controller": "l3_r3"})
        self.assertEqual(v({"controller": "Guide+South"}), {"controller": "mode+south"})
        self.assertEqual(v({"controller": ["view", "menu"]}), {"controller": "view_menu"})
        self.assertEqual(v({"controller": False}), {"controller": "off"})
        self.assertEqual(v({"controller": "yes"}), {"controller": "on"})
        for bad in ({"controller": "turbo"}, {"controller": "select+turbo"}, {"controller_exclusive": "on"},
                    {"controller_open": "tap"}):
            with self.assertRaises(ValueError, msg=bad) as cm:
                v(bad)
            self.assertTrue(str(cm.exception).startswith(next(iter(bad))))
        self.assertEqual(self.settings.controller_label("select+mode"), "Select + Mode")
        self.assertEqual(self.settings.controller_label("right_paddle"), "Right paddle")
        # with the symbols of the controller in use (the bar's extra choice)
        self.assertEqual(self.settings.controller_label("view_menu", "playstation"), "Create + Options")
        self.assertEqual(self.settings.controller_label("view_menu", "xbox"), "View + Menu")
        self.assertEqual(self.settings.controller_label("tl+tr", "playstation"), "L1 + R1")
        self.assertEqual(self.settings.controller_label("south+east", "playstation"), "Cross + Circle")
        self.assertEqual(self.settings.controller_label("tl+tr", "nintendo"), "L + R")
        self.assertEqual(self.settings.controller_label("left_paddle", "xbox"), "Left paddle")
        self.assertEqual(self.settings.controller_label("l3_r3", "playstation"), "L3 + R3")
        self.assertEqual(self.settings.controller_label("ps_down", "playstation"), "PS / Xbox + Down")
        self.assertEqual(self.settings.controller_label("select+dpad_up", "nintendo"), "Minus + Up")

    def test_two_button_rule(self):
        """New shortcuts are two buttons (`momento set`); a config with another size keeps
        working and logs a warning once."""
        check = self.settings.check_new_shortcut
        for ok in ("off", "on", "ps_down", "view_menu", "l3_r3", "tl+tr"):
            check(ok)
        for bad in ("left_paddle", "right_paddle", "mode", "tl+tr+select"):
            with self.assertRaises(ValueError, msg=bad) as cm:
                check(bad)
            self.assertIn("two buttons pressed together", str(cm.exception))
        self.config._warned_chords.clear()
        self.path.write_text('[controller]\nopen_chord = ["right_paddle"]\n')
        with self.assertLogs("momento.config", "WARNING") as logs:
            self.assertEqual(self.config.load_controller(self.path)["chord"], ("right_paddle",))
        self.assertIn("two buttons", logs.output[0])
        with self.assertNoLogs("momento.config", "WARNING"):
            self.config.load_controller(self.path)                  # once

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

    def test_hold_and_exclusive_are_config_only(self):
        """No settings for them; a hand-edited hold_ms / exclusive still works, and
        changing the shortcut leaves them alone."""
        self.config.set_value("controller", "hold_ms", 500, self.path)
        self.config.set_value("controller", "exclusive", False, self.path)
        ctl = self.config.load_controller(self.path)
        self.assertEqual((ctl["hold_ms"], ctl["exclusive"]), (500, False))
        self.assertEqual(self.settings.apply({"controller": "off"}, self.path), {"controller": "off"})
        self.assertEqual(self.settings.apply({"controller": "ps_down"}, self.path), {"controller": "ps_down"})
        ctl = self.config.load_controller(self.path)
        self.assertEqual((ctl["hold_ms"], ctl["exclusive"]), (500, False))
        self.assertEqual(self.config.DEFAULTS["controller"]["hold_ms"], 0)
        self.assertIs(self.config.DEFAULTS["controller"]["exclusive"], True)

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

    def use_view_menu(self):
        """The old default, View + Menu, set explicitly."""
        from momento import config

        config.set_value("controller", "open_chord", ["select", "start"], self.path)
        self.d.cfg = config.load(self.path)

    def use_hold(self):
        """A hand-edited [controller] hold_ms = 300 (config only, no setting)."""
        from momento import config

        config.set_value("controller", "hold_ms", 300, self.path)
        self.d.cfg = config.load(self.path)

    def test_chord_opens_bar_without_grabbing(self):
        self.use_view_menu()
        self.use_hold()
        self.d._sync_controller()
        self.assertEqual(self.made[-1]["navigate"], False)
        self.assertEqual(self.devs[-1].mask, (self.gamepad.EV_KEY,))   # key events only
        self.chord(hold=0.2)                                         # shorter than the 0.3 s hold
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

    def test_default_opens_on_press(self):
        """No [controller] hold_ms: a tap opens the bar, nothing to wait for."""
        self.use_view_menu()
        self.d._sync_controller()
        hub = self.d.pads
        self.assertEqual((self.made[-1]["hold_ms"], hub.hold), (0, 0))
        g, dev = self.gamepad, self.devs[-1]
        for code in (g.BTN_SELECT, g.BTN_START):
            dev.push(g.EV_KEY, code, 1)
        hub.process(dev.fileno())                                      # no tick: fires on press
        self.assertEqual(self.opened, [100.0])
        self.assertEqual(self.devs[-1].grab_calls, 0)

    def test_shortcut_change_keeps_a_hand_edited_hold(self):
        """hold_ms is config only: changing the shortcut applies live and keeps the hold."""
        self.use_view_menu()
        self.use_hold()
        self.d._sync_controller()
        rec, hub = self.d.recorder, self.d.pads
        self.assertAlmostEqual(hub.hold, 0.3)
        r = self.call({"cmd": "configure", "changes": {"controller": "ps_down"}})
        self.assertEqual((r["ok"], r["restarted"], r["changed"]), (True, False, {"controller": "ps_down"}))
        self.assertIs(self.d.recorder, rec)                            # recording untouched
        self.assertEqual(rec.stopped, 0)
        self.assertEqual(self.d.pads.chord, ("mode", "dpad_down"))
        self.assertAlmostEqual(self.d.pads.hold, 0.3)

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

    def test_default_ps_down_holds_the_pad_while_mode_is_down(self):
        """PS/Xbox/Home + D-pad Down: the pad is held from the mode press until mode and
        Down are both let go, so the game never sees the D-pad; the mask is keys + hat."""
        g = self.gamepad
        self.d._sync_controller()
        self.assertEqual(self.made[-1]["chord"], ("mode", "dpad_down"))
        self.assertIs(self.made[-1]["chord_grab"], True)                 # [controller] exclusive
        hub, dev = self.d.pads, self.devs[-1]
        dev.mask_honoured = True
        self.assertEqual(dev.mask, (g.EV_KEY, g.EV_ABS))                 # the hat, never the sticks
        dev.push(g.EV_ABS, g.ABS_HAT0Y, 1)                                # D-pad alone: opens nothing
        dev.push(g.EV_ABS, g.ABS_HAT0Y, 0)
        hub.process(dev.fileno())
        self.assertEqual(self.opened, [])
        dev.push(g.EV_KEY, g.BTN_MODE, 1)
        hub.process(dev.fileno())
        self.assertTrue(dev.grabbed)
        dev.push(g.EV_ABS, g.ABS_HAT0Y, 1)
        hub.process(dev.fileno())
        self.assertEqual(self.opened, [100.0])
        self.assertTrue(dev.grabbed)                                      # until mode is let go
        dev.push(g.EV_KEY, g.BTN_MODE, 0)
        hub.process(dev.fileno())
        self.assertTrue(dev.grabbed)                                      # ...and Down
        dev.push(g.EV_ABS, g.ABS_HAT0Y, 0)
        hub.process(dev.fileno())
        self.assertFalse(dev.grabbed)
        self.assertFalse(hub.grabbing)                                   # never the bar's grab

    def test_exclusive_off_shares_the_pad(self):
        g = self.gamepad
        self.d._sync_controller()
        from momento import config

        config.set_value("controller", "exclusive", False, self.path)   # config only, no setting
        self.d.cfg = config.load(self.path)
        self.d._sync_controller()
        hub, dev = self.d.pads, self.devs[-1]
        self.assertFalse(hub.chord_grab)
        dev.push(g.EV_KEY, g.BTN_MODE, 1)
        dev.push(g.EV_ABS, g.ABS_HAT0Y, 1)
        hub.process(dev.fileno())
        self.assertEqual(self.opened, [100.0])
        self.assertEqual(dev.grab_calls, 0)

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
        self.assertEqual(cur["replay_length"], 15)
        d = self.settings.describe(cfg, devices={"outputs": [], "inputs": []})
        self.assertEqual(d["choices"]["replay_length"], [15, 30, 60])
        self.assertEqual(d["max_seconds"], 900)
        self.assertEqual(d["choices"]["keep_history"], ["off", "on"])
        self.assertEqual(d["choices"]["hour_warning"], [10, 5, 3])
        self.assertEqual(d["choices"]["instant_bar"], ["on", "off"])
        self.assertEqual(d["tabs"], [["General", ["record", "replay_length", "keep_history"]],
                                     ["Video", ["resolution", "fps", "quality", "format"]],
                                     ["Audio", ["audio_source", "mic", "mic_device", "sounds"]],
                                     ["Controller", ["controller"]],
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

    def test_replay_length_defaults_to_15_minutes(self):
        self.assertEqual(self.config.DEFAULTS["buffer"]["max_seconds"], 900)
        self.assertEqual(self.config.load(self.path)["buffer"]["max_seconds"], 900)
        # a hand-edited length up to 60 minutes is taken as it is; longer is capped
        self.path.write_text("[buffer]\nmax_seconds = 1234\n")
        self.assertEqual(self.config.load(self.path)["buffer"]["max_seconds"], 1234)
        self.path.write_text("[buffer]\nmax_seconds = 7200\n")
        self.assertEqual(self.config.load(self.path)["buffer"]["max_seconds"], 3600)

    def test_replay_length_values(self):
        v = self.settings.validate
        for given, minutes in (("15m", 15), (30, 30), ("60 min", 60), ("1h", 60), ("30", 30),
                               (1800, 30), ("900s", 15), (" 15 minutes ", 15)):
            self.assertEqual(v({"replay_length": given}), {"replay_length": minutes}, given)
        for bad in (45, "2h", "90s", "10m", True, "soon", "", 0):
            with self.assertRaises(ValueError, msg=bad) as cm:
                v({"replay_length": bad})
            self.assertEqual(str(cm.exception), "replay_length: choose 15m, 30m, 60m")

    def test_replay_length_writes_max_seconds(self):
        for minutes, seconds in ((30, 1800), (60, 3600), (15, 900)):
            self.assertEqual(self.settings.apply({"replay_length": minutes}, self.path), {"replay_length": minutes})
            self.assertEqual(self.config.load(self.path)["buffer"]["max_seconds"], seconds)
            self.assertEqual(self.settings.current(self.config.load(self.path))["replay_length"], minutes)
        self.assertIn("# mine", self.path.read_text())
        self.assertEqual(self.settings.apply({"replay_length": "15m"}, self.path), {})
        # a hand-edited length reads as its whole minutes; choosing that again keeps it
        self.path.write_text("[buffer]\nmax_seconds = 910\n")
        self.assertEqual(self.settings.current(self.config.load(self.path))["replay_length"], 15)
        self.assertEqual(self.settings.apply({"replay_length": 15}, self.path), {})
        self.assertEqual(self.config.load(self.path)["buffer"]["max_seconds"], 910)

    def test_cli_sets_replay_length(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, ipc

        out = io.StringIO()
        with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                mock.patch.object(self.settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "replay_length", "30m"]), 0)
            self.assertEqual(cli.main(["--config", str(self.path), "settings"]), 0)
        self.assertIn("replay_length = 30m", out.getvalue())
        self.assertIn("replay: keeps the last 30 min", out.getvalue())
        self.assertEqual(self.config.load(self.path)["buffer"]["max_seconds"], 1800)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.main(["--config", str(self.path), "set", "nope", "15m"])
        self.assertEqual(cli.main(["--config", str(self.path), "set", "replay_length", "45m"]), 1)
        # with the daemon running it goes through configure and applies right away
        out = io.StringIO()
        reply = {"ok": True, "changed": {"replay_length": 15}, "restarted": False, "paused": False}
        with mock.patch.object(ipc, "request", return_value=reply) as req, contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "replay_length", "15m"]), 0)
        self.assertEqual(req.call_args[0][0], {"cmd": "configure", "changes": {"replay_length": 15}, "origin": "set"})
        self.assertIn("replay_length = 15m", out.getvalue())
        self.assertIn("applies right away", out.getvalue())

    def test_hand_edited_warning_falls_back(self):
        self.path.write_text("[buffer]\nwarn_minutes = 45\n")
        with self.assertLogs("momento.config", "WARNING"):
            self.assertEqual(self.config.warn_minutes(self.config.load(self.path)), 10)
        self.assertEqual(self.config.warn_minutes({"buffer": {"warn_minutes": 4}}), 4)

    def test_live_keys(self):
        self.assertEqual(set(self.settings.LIVE_KEYS),
                         {"controller", "replay_length", "keep_history", "hour_warning", "instant_bar",
                          "sounds"})

    def test_sounds_setting(self):
        """Settings -> Audio -> Menu sounds: [ui] sounds, on by default, live, read by the bar."""
        cfg = self.config.load(self.path)
        self.assertIs(self.config.DEFAULTS["ui"]["sounds"], True)
        self.assertEqual(self.settings.current(cfg)["sounds"], "on")
        self.assertTrue(self.config.load_bar_sounds(self.path))
        d = self.settings.describe(cfg, devices={"outputs": [], "inputs": []})
        self.assertEqual(d["choices"]["sounds"], ["on", "off"])
        self.assertIn("sounds", self.settings.LIVE_KEYS)
        self.assertEqual(self.settings.validate({"sounds": False}), {"sounds": "off"})
        with self.assertRaises(ValueError):
            self.settings.validate({"sounds": "loud"})
        self.assertEqual(self.settings.apply({"sounds": "off"}, self.path), {"sounds": "off"})
        self.assertIn("# mine", self.path.read_text())
        self.assertIs(self.config.load(self.path)["ui"]["sounds"], False)
        self.assertFalse(self.config.load_bar_sounds(self.path))
        self.assertEqual(self.settings.current(self.config.load(self.path))["sounds"], "off")
        # a hand-broken value or file reads as the default (on)
        self.path.write_text("[ui]\nsounds = \"maybe\"\n")
        self.assertTrue(self.config.load_bar_sounds(self.path))
        self.path.write_text("[ui\n")
        self.assertTrue(self.config.load_bar_sounds(self.path))
        self.assertTrue(self.config.load_bar_sounds(self.path.with_name("missing.toml")))

    def test_cli_sets_sounds(self):
        import contextlib
        import io
        from unittest import mock

        from momento import cli, ipc

        out = io.StringIO()
        with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                mock.patch.object(self.settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "sounds", "off"]), 0)
            self.assertEqual(cli.main(["--config", str(self.path), "settings"]), 0)
        self.assertIn("sounds = off", out.getvalue())
        self.assertIn("sounds: off", out.getvalue())
        self.assertIs(self.config.load(self.path)["ui"]["sounds"], False)
        self.assertEqual(cli.main(["--config", str(self.path), "set", "sounds", "loud"]), 1)
        out = io.StringIO()
        reply = {"ok": True, "changed": {"sounds": "on"}, "restarted": False, "paused": False}
        with mock.patch.object(ipc, "request", return_value=reply) as req, contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.path), "set", "sounds", "on"]), 0)
        self.assertEqual(req.call_args[0][0], {"cmd": "configure", "changes": {"sounds": "on"}, "origin": "set"})
        self.assertIn("applies right away", out.getvalue())

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
        self.assertIn("history: kept when recording stops; every 15 min of recording is saved to the clips folder",
                      text)
        self.assertIn("warning: 5 min before the 15 min replay is full", text)
        self.assertNotIn("hour", text.split("record:", 1)[1])      # (the key hour_warning aside)
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

    ST = {"ok": True, "state": "recording", "recording": True, "buffered": 60.0, "max_seconds": 3600,
          "target": "screen", "resolution": "1080p", "fps": 60, "quality": "high", "bitrate_kbps": 10000,
          "resolution_effective": "native", "source_size": [1280, 720]}

    def test_status_shows_the_resolution_really_recorded(self):
        _code, text = self.run_cli(["status"], [self.ST])
        self.assertIn("video: 1080p, recording at 720p (your screen's size), 60 fps, high (10 Mbps)", text)
        win = {**self.ST, "target": "window", "source_size": [1001, 701]}
        _code, text = self.run_cli(["status"], [win])
        self.assertIn("video: 1080p, recording at 1001\u00d7701 (the window's size), 60 fps", text)
        for fits in ({**self.ST, "resolution": "720p", "resolution_effective": "720p"},
                     {**self.ST, "resolution": "native"},
                     {k: v for k, v in self.ST.items() if k not in ("resolution_effective", "source_size")}):
            _code, text = self.run_cli(["status"], [fits])
            self.assertNotIn("recording at", text)
            self.assertIn(f"video: {fits['resolution']} 60 fps", text)

    def test_set_resolution_above_the_screen_notes_it(self):
        conf = {"ok": True, "changed": {"resolution": "1080p"}, "restarted": True, "paused": False,
                "state": "starting"}
        code, text = self.run_cli(["set", "resolution", "fhd"], [conf, self.ST])
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines(), ["resolution = 1080p", "Recording restarted with the new setting.",
                                             "Your screen is 720p, so this records at 720p; "
                                             "a bigger size would only waste space."])
        code, text = self.run_cli(["set", "resolution", "720p"],
                                  [{**conf, "changed": {"resolution": "720p"}}, self.ST])
        self.assertNotIn("waste", text)
        win = {**self.ST, "target": "window", "source_size": [1001, 701]}
        _code, text = self.run_cli(["set", "resolution", "720p"], [{**conf, "changed": {"resolution": "720p"}}, win])
        self.assertIn("The window is 1001\u00d7701, so this records at 1001\u00d7701;", text)
        # no size known yet (or no status): nothing to add
        _code, text = self.run_cli(["set", "resolution", "1080p"], [conf, {**self.ST, "source_size": None}])
        self.assertNotIn("waste", text)
        _code, text = self.run_cli(["set", "resolution", "1080p"], [conf, OSError("gone")])
        self.assertNotIn("waste", text)

    def test_settings_show_the_resolution_really_recorded(self):
        from unittest import mock

        from momento import settings, storage

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            # an older config's 4K: shown (and recorded) as 1080p; the file is left alone
            path.write_text(f'[capture]\nresolution = "2160p"\ntarget = "screen"\n[buffer]\ndir = "{d}/buf"\n')
            with mock.patch.object(settings, "list_audio_devices", return_value={"outputs": [], "inputs": []}), \
                    mock.patch.object(storage, "free_bytes", return_value=10**12):
                _code, text = self.run_cli(["--config", str(path), "settings"], [self.ST])
                self.assertIn("resolution: 1080p, records at 720p (your screen's size)", text)
                self.assertIn("bitrate: 10 Mbps (automatic)", text)
                self.assertIn("disk use: about 1.2 GB for the full buffer", text)
                _code, text = self.run_cli(["--config", str(path), "settings"], [OSError("not running")])
                self.assertIn("resolution: 1080p\n", text)
                self.assertIn("bitrate: 15 Mbps (automatic)", text)
            self.assertIn('resolution = "2160p"', path.read_text())

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

    def test_shorter_length_mid_session(self):
        self.assertEqual(self.feed(60, 40, keep=True), [])               # 40 min of a 60-min replay
        self.h.relength(3600, 900)
        self.assertEqual(self.feed(60, 1, keep=True, length=900), [("warn", 240.0)])   # next mark at 45:00
        self.assertEqual(self.feed(60, 4, keep=True, length=900), [("mark", 60.0)])
        self.assertEqual(self.feed(60, 15, keep=True, length=900), [("warn", 600.0), ("mark", 60.0)])

    def test_longer_length_counts_from_the_last_mark(self):
        self.assertEqual(self.feed(60, 30, keep=True, length=900)[-1], ("mark", 60.0))   # marks at 15, 30
        self.h.relength(900, 3600)
        self.assertEqual(self.feed(60, 50, keep=True), [("warn", 600.0)])   # 80:00; the next mark is at 90:00
        self.assertEqual(self.feed(60, 10, keep=True), [("mark", 60.0)])

    def test_no_session_start_warning_once_it_is_gone(self):
        self.feed(60, 40, keep=False, length=900)                        # the start was replaced at 15:00
        self.h.relength(900, 3600)
        self.assertEqual(self.feed(60, 120, keep=False), [("mark", 60.0), ("mark", 60.0)])

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
        # These tests are about a 60-minute replay (the longest); 15 minutes has its own.
        self.path.write_text(self.path.read_text() + "max_seconds = 3600\n")   # [buffer] is last
        self.d.cfg["buffer"]["max_seconds"] = 3600
        self.d.ring.max_seconds = 3600
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

    def test_replay_length_applies_without_restarting(self):
        rec = self.d.recorder
        self.record(40)
        self.assertAlmostEqual(self.d.status()["buffered"], 2400.0)
        r = self.call({"cmd": "configure", "changes": {"replay_length": "15m"}})
        self.assertEqual((r["ok"], r["restarted"], r["changed"]), (True, False, {"replay_length": 15}))
        self.assertEqual((rec.stopped, self.d.recorder is rec), (0, True))   # recording never restarted
        st = self.d.status()
        self.assertEqual(st["max_seconds"], 900)
        self.assertAlmostEqual(st["buffered"], 900.0)
        kept = sorted(p.name for p in self.buf.glob("*.ts"))
        self.assertEqual(kept[-1], "seg00000039.ts")                   # the newest is kept
        self.assertLessEqual(len(kept), 17)                            # 15 min + the 30 s margin, in 1-min pieces
        self.assertEqual((self.sent, self.exports), ([], []))           # nothing fired for footage already there
        self.assertEqual(self.d.cfg["buffer"]["max_seconds"], 900)
        r = self.call({"cmd": "configure", "changes": {"replay_length": 60}})
        self.assertEqual((r["restarted"], self.d.status()["max_seconds"]), (False, 3600))

    def test_reload_takes_a_hand_edited_length(self):
        self.record(20)
        self.path.write_text(self.path.read_text().replace("max_seconds = 3600", "max_seconds = 600"))
        self.call({"cmd": "reload"})
        self.assertEqual(self.d.status()["max_seconds"], 600)
        self.assertAlmostEqual(self.d.status()["buffered"], 600.0)

    def use_length(self, minutes):
        r = self.call({"cmd": "configure", "changes": {"replay_length": minutes}})
        self.assertEqual((r["ok"], self.d.status()["max_seconds"]), (True, minutes * 60))

    def test_15_minute_warning_says_15_minutes(self):
        self.use_length(15)
        self.record(4)
        self.assertEqual(self.sent, [])
        self.record(1)                            # a 10-minute warning on a 15-minute replay
        self.assertEqual(self.sent, [("Momento: 15 minutes almost full",
                                      "In 10 min the start of this session starts being replaced. "
                                      "Save anything you want from it now.")])
        self.record(30)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(any("hour" in text for pair in self.sent for text in pair))

    def test_15_minutes_saved_with_history(self):
        self.use_length(15)
        self.call({"cmd": "configure", "changes": {"keep_history": "on", "hour_warning": 5}})
        self.record(10)
        self.assertEqual(self.sent, [("Momento: 15 minutes almost full",
                                      "In 5 min the last 15 minutes are saved to Videos and a new stretch starts.")])
        self.record(5)                            # the mark
        self.wait_for(lambda: len(self.sent) == 2)
        duration, _pins, out = self.exports[0]
        self.assertAlmostEqual(duration, 900.0)
        self.assertTrue(out.name.endswith("_15m.mp4"), out.name)
        self.assertEqual(self.sent[1][0], "Saved the last 15 minutes to Videos")
        self.free = 0                             # the next one doesn't fit
        self.record(15)
        self.assertEqual(self.sent[-1][0], "Momento: couldn't save the last 15 minutes — disk full")
        self.assertFalse(any("hour" in text for pair in self.sent for text in pair))

    def test_30_minute_warning(self):
        self.use_length(30)
        self.record(19)
        self.assertEqual(self.sent, [])
        self.record(1)
        self.assertEqual(self.sent, [("Momento: 30 minutes almost full",
                                      "In 10 min the start of this session starts being replaced. "
                                      "Save anything you want from it now.")])

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

    def test_title_never_in_the_log(self):
        """A window title can be private (a browser tab): the log says only how long it is."""
        with self.assertLogs("momento.daemon", "INFO") as cm:
            self.session("tok-1")
            self.wait_name("Elden Ring")
        text = "\n".join(cm.output)
        self.assertNotIn("Elden Ring", text)
        self.assertIn("recording window (window title: <redacted, 10 chars>)", text)

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



class SettingsChangeLogTest(unittest.TestCase):
    """The log says which settings each recording used: one line per change, old -> new."""

    # The configure tests run against a fake Recorder, like DaemonControlTest.
    setUp = DaemonControlTest.setUp
    tearDown = DaemonControlTest.tearDown
    call = DaemonControlTest.call

    def logged(self, msg):
        """The daemon's INFO lines while ``msg`` is handled, and its reply."""
        with self.assertLogs("momento.daemon", "INFO") as cm:
            r = self.call(msg)
        return [rec.getMessage() for rec in cm.records], r

    def about_settings(self, lines):
        return [line for line in lines if line.startswith("settings ")]

    def test_describe_names_only_the_changed_keys(self):
        from momento import config, settings

        cfg = config.load(self.path)
        before = settings.current(cfg)
        after = dict(before, resolution="720p", quality="ultra", fps=120, format="av1", replay_length=30,
                     bitrate=12000, keep_history="on", mic_device="default")      # mic_device: the same
        changes = settings.diff(before, after)
        self.assertEqual(list(changes), ["replay_length", "resolution", "quality", "fps", "format", "bitrate",
                                         "keep_history"])
        self.assertEqual(settings.describe_changes(changes),
                         "replay length 15m -> 30m, resolution 1080p -> 720p, quality high -> ultra, fps auto -> 120, "
                         "format auto -> av1, bitrate auto -> 12000 kbps, keep history off -> on")
        self.assertEqual(settings.diff(before, {"fps": "auto"}), {})           # auto: the default
        self.assertEqual(settings.describe_changes(settings.diff(before, {"fps": 60})), "fps auto -> 60")
        self.assertEqual(settings.describe_request({"fps": "75\n", "colour": "red"}), "fps=75?, colour=?")

    def test_a_change_from_the_bar_is_one_line(self):
        lines, r = self.logged({"cmd": "configure", "origin": "bar",
                                "changes": {"resolution": "720p", "quality": "ultra", "fps": 120, "format": "av1",
                                            "mic": "off"}})                            # mic: already off
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.about_settings(lines),
                         ["settings changed: resolution 1080p -> 720p, quality high -> ultra, fps auto -> 120, "
                          "format auto -> av1 (from the bar)"])

    def test_origin(self):
        lines, _r = self.logged({"cmd": "configure", "origin": "set", "changes": {"fps": 120}})
        self.assertEqual(self.about_settings(lines), ["settings changed: fps auto -> 120 (from momento set)"])
        lines, _r = self.logged({"cmd": "configure", "origin": "evil\nline", "changes": {"fps": 60}})
        self.assertEqual(self.about_settings(lines), ["settings changed: fps 120 -> 60 (from a client)"])
        lines, _r = self.logged({"cmd": "configure", "changes": {"keep_history": "on"}})    # live: no restart
        self.assertEqual(self.about_settings(lines), ["settings changed: keep history off -> on (from a client)"])
        lines, _r = self.logged({"cmd": "configure", "origin": "bar", "changes": {"keep_history": "on"}})
        self.assertEqual(self.about_settings(lines), ["settings unchanged (from the bar): keep history=on"])
        from momento.protocol import validate_request

        self.assertEqual(validate_request({"cmd": "configure", "changes": {"fps": 60}, "origin": "bar"}), [])
        self.assertEqual(validate_request({"cmd": "configure", "changes": {"fps": 60}, "origin": "x"}),
                         ["unknown origin 'x'"])

    def test_refused_changes_say_why(self):
        from momento import config

        saved = self.path.read_text()
        lines, r = self.logged({"cmd": "configure", "origin": "bar", "changes": {"fps": 75}})
        self.assertFalse(r["ok"])
        self.assertEqual(self.about_settings(lines),
                         ["settings change refused (from the bar): fps: choose one of: auto, 60, 120 (asked: fps=75)"])
        lines, r = self.logged({"cmd": "configure", "origin": "set", "changes": {"resolution": "1440p"}})
        self.assertFalse(r["ok"])
        self.assertEqual(len(self.about_settings(lines)), 1)
        self.assertIn("settings change refused (from momento set): 1440p and 4K aren't available yet",
                      self.about_settings(lines)[0])
        self.free = 0                                                   # no room for a bigger buffer
        lines, r = self.logged({"cmd": "configure", "origin": "bar", "changes": {"quality": "ultra"}})
        self.assertEqual(r.get("code"), "no_storage")
        [line] = self.about_settings(lines)
        self.assertTrue(line.startswith("settings change refused (from the bar): quality high -> ultra: "
                                        "not enough disk space: "), line)
        self.assertEqual(self.path.read_text(), saved)                  # nothing was written
        self.assertEqual(config.load(self.path)["capture"]["quality"], "high")

    def test_config_reload(self):
        self.path.write_text(self.path.read_text().replace('resolution = "1080p"',
                                                           'resolution = "720p"\nfps = 120'))
        lines, r = self.logged({"cmd": "reload"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.about_settings(lines),
                         ["settings changed: resolution 1080p -> 720p, fps auto -> 120 (config reload)"])
        lines, _r = self.logged({"cmd": "reload"})
        self.assertEqual(self.about_settings(lines),
                         ["settings unchanged (config reload): restarting capture with the same settings"])
        # A change from the bar that also picks up a hand edit: each on its own line.
        self.path.write_text(self.path.read_text().replace("fps = 120", "fps = 60"))
        lines, _r = self.logged({"cmd": "configure", "origin": "bar", "changes": {"quality": "standard"}})
        self.assertEqual(self.about_settings(lines),
                         ["settings changed: quality high -> standard (from the bar)",
                          "settings changed: fps 120 -> 60 (from the config file)"])
        self.path.write_text(self.path.read_text().replace("fps = 60", "fps = 75"))
        lines, r = self.logged({"cmd": "reload"})
        self.assertFalse(r["ok"])
        [line] = self.about_settings(lines)
        self.assertTrue(line.startswith("settings not applied (config reload): config not applied: "), line)

    def test_clip_saved_says_format_size_and_fps(self):
        from momento import daemon
        from momento.ringbuffer import Segment, Selection

        seg = Segment(Path("seg00000001.mkv"), 0.0, 10.0, width=1280, height=720, fps=120, codec="av1")
        self.assertEqual(daemon.clip_params(Selection([seg], 0.0, 10.0, 0.0, 10.0)), "AV1 1280x720 @ 120 fps")
        old = Segment(Path("seg00000001.ts"), 0.0, 10.0, fps=59.94)                  # an older index line
        self.assertEqual(daemon.clip_params(Selection([old], 0.0, 10.0, 0.0, 10.0)), "format ? size ? @ 59.94 fps")


class AutoFpsRuleTest(unittest.TestCase):
    """fps "auto" (the default) follows the recorded screen's refresh rate."""

    def test_the_rule(self):
        from momento import quality

        for hz, fps in ((60, 60), (75, 60), (90, 60), (99.9, 60), (100, 120), (119.88, 120), (120, 120),
                        (144, 120), (165, 120), (240, 120), (59.94, 60)):
            with self.subTest(hz=hz):
                self.assertEqual(quality.auto_fps(hz), fps)
                self.assertEqual(quality.fps({"fps": "auto"}, hz), fps)
                self.assertEqual(quality.fps({}, hz), fps)                    # auto is the default
        for unknown in (None, 0, -60, float("nan"), float("inf"), "120", True):
            with self.subTest(unknown=unknown):
                self.assertEqual(quality.auto_fps(unknown), 60)              # unknown: 60
        self.assertEqual(quality.hz_label(119.88), "120")
        self.assertIsNone(quality.hz_label(None))

    def test_the_setting(self):
        from momento import config, quality

        self.assertEqual(config.DEFAULTS["capture"]["fps"], "auto")
        self.assertEqual(quality.FPS_CHOICES, ("auto", 60, 120))
        for value, setting in ((None, "auto"), ("", "auto"), ("auto", "auto"), ("AUTO", "auto"), (60, 60),
                               (120, 120), ("120", 120), ("60 fps", 60), (120.0, 120)):
            self.assertEqual(quality.fps_setting({"fps": value}), setting, value)
        for bad in (75, 90, 144, "fast", True, 60.5, [60]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                quality.fps({"fps": bad}, 120)

    def test_explicit_configs_are_unchanged(self):
        """An explicit 60 or 120 records at that rate whatever the screen runs at."""
        from momento import quality

        for hz in (None, 60, 120, 144):
            self.assertEqual(quality.fps({"fps": 60}, hz), 60)
            self.assertEqual(quality.fps({"fps": 120}, hz), 120)
            self.assertEqual(quality.bitrate_kbps({"fps": 60, "resolution": "1080p", "quality": "high"}, None, hz),
                             15_000)
            self.assertEqual(quality.bitrate_kbps({"fps": 120, "resolution": "1080p", "quality": "high"}, None, hz),
                             22_000)

    def test_bitrate_and_storage_at_auto(self):
        import copy

        from momento import config, quality, storage

        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["capture"].update(resolution="1080p", quality="high", fps="auto")
        at = {f: copy.deepcopy(cfg) for f in (60, 120)}
        for f, c in at.items():
            c["capture"]["fps"] = f
        self.assertEqual(quality.bitrate_kbps(cfg["capture"], None, 120), 22_000)    # 15 Mbps x 1.5
        self.assertEqual(quality.bitrate_kbps(cfg["capture"], None, 60), 15_000)
        self.assertEqual(quality.bitrate_kbps(cfg["capture"]), 15_000)               # unknown: 60 fps
        for hz, f in ((144, 120), (120, 120), (90, 60), (60, 60), (None, 60)):
            with self.subTest(hz=hz):
                self.assertEqual(storage.buffer_bytes(cfg, None, hz), storage.buffer_bytes(at[f]))
                self.assertEqual(storage.required_bytes(cfg, None, hz), storage.required_bytes(at[f]))
        self.assertEqual(storage.label(cfg, refresh=120), "1080p High 120 fps")
        self.assertEqual(storage.label(cfg, refresh=60), "1080p High")
        self.assertEqual(storage.label(cfg), "1080p High")
        self.assertEqual(storage.current_key(cfg), "1080p/high/auto")
        with mock.patch.object(storage, "free_bytes", return_value=10**13):
            chk = storage.check(cfg, 0, None, 120)
            req = storage.requirements(cfg, 0, None, 120)
        self.assertEqual((chk["required"], chk["label"]), (storage.required_bytes(at[120]), "1080p High 120 fps"))
        self.assertEqual(req["required"]["1080p/high/auto"], req["required"]["1080p/high/120"])
        self.assertLess(req["required"]["1080p/high/60"], req["required"]["1080p/high/auto"])

    def test_settings(self):
        from momento import config, settings

        self.assertEqual(settings.validate({"fps": "auto"}), {"fps": "auto"})
        self.assertEqual(settings.validate({"fps": "Auto"}), {"fps": "auto"})
        self.assertEqual(settings.validate({"fps": "120"}), {"fps": 120})
        for bad in (75, "", None, True):
            with self.assertRaisesRegex(ValueError, "choose one of: auto, 60, 120"):
                settings.validate({"fps": bad})
        self.assertEqual(settings.writes("fps", "auto"), [("capture", "fps", "auto")])
        cfg = config.load(Path(tempfile.gettempdir()) / "momento-no-such-config.toml")
        self.assertEqual(settings.current(cfg)["fps"], "auto")
        d = settings.describe(cfg, devices={"outputs": [], "inputs": []}, refresh=144)
        self.assertEqual(d["choices"]["fps"], ["auto", 60, 120])
        self.assertEqual((d["values"]["fps"], d["fps"], d["fps_effective"], d["refresh_hz"]), ("auto", 120, 120, 144))
        d = settings.describe(cfg, devices={"outputs": [], "inputs": []})
        self.assertEqual((d["fps"], d["fps_effective"], d["refresh_hz"]), (60, 60, None))
        cfg["capture"]["fps"] = 75                                      # a hand edit it can't be: shown as is
        self.assertEqual(settings.current(cfg)["fps"], 75)

    def test_set_fps_auto(self):
        import contextlib
        import io

        from momento import cli, config, ipc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text("[capture]\nfps = 60\n")
            self.assertEqual(config.load(path)["capture"]["fps"], 60)     # an explicit 60 stays 60
            out = io.StringIO()
            with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("no")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--config", str(path), "set", "fps", "auto"]), 0)
                self.assertIn("fps = auto", out.getvalue())
                self.assertEqual(config.load(path)["capture"]["fps"], "auto")
                self.assertEqual(cli.main(["--config", str(path), "set", "fps", "120"]), 0)
                self.assertEqual(config.load(path)["capture"]["fps"], 120)
                self.assertEqual(cli.main(["--config", str(path), "set", "fps", "75"]), 1)
            self.assertIn('fps = 120', path.read_text())
        with mock.patch.object(ipc, "request", return_value={"ok": True, "changed": {"fps": "auto"},
                                                             "restarted": True, "paused": False,
                                                             "state": "starting"}) as req, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["set", "fps", "auto"]), 0)
        self.assertEqual(req.call_args[0][0]["changes"], {"fps": "auto"})

    def test_cli_says_what_auto_records_at(self):
        from momento import cli

        self.assertEqual(cli.fps_line("auto", 120, 120), "120 fps (auto: your screen is 120 Hz)")
        self.assertEqual(cli.fps_line("auto", 120, 144), "120 fps (auto: your screen is 144 Hz)")
        self.assertEqual(cli.fps_line("auto", None, None), "60 fps (auto: matches your screen)")
        self.assertEqual(cli.fps_line(120, 120, 60), "120 fps")
        self.assertEqual(cli.fps_line(60), "60 fps")                       # an older daemon's status
        _code, text = CLIStatusTest.run_cli(self, ["status"], [{**CLIStatusTest.ST, "fps": "auto", "fps_effective": 120,
                                                                "refresh_hz": 120, "bitrate_kbps": 15000}])
        self.assertIn("video: 1080p, recording at 720p (your screen's size), 120 fps (auto: your screen is 120 Hz), "
                      "high (15 Mbps)", text)


class RefreshRecorder(FakeRecorder):
    """A fake recorder whose stream announces the screen's refresh, as the real one learns it."""

    hz = 120.0
    hints = []

    def start(self, interactive=False):
        RefreshRecorder.hints.append(getattr(self, "refresh_hz", None))   # what the daemon handed over
        self.refresh_hz = RefreshRecorder.hz
        super().start(interactive)


class DaemonAutoFpsTest(unittest.TestCase):
    """The daemon reports fps / fps_effective / refresh_hz and counts storage at the effective rate."""

    setUp = DaemonControlTest.setUp
    tearDown = DaemonControlTest.tearDown
    call = DaemonControlTest.call

    def use(self, hz):
        RefreshRecorder.hz, RefreshRecorder.hints = hz, []
        sys.modules["momento.pipeline"].Recorder = RefreshRecorder
        self.d.recorder = RefreshRecorder(self.d.cfg, self.d.ring, self.d._on_state)
        self.d.recorder.start()

    def test_status_before_the_refresh_is_known(self):
        st = self.call({"cmd": "status"})
        self.assertEqual((st["fps"], st["fps_effective"], st["refresh_hz"]), ("auto", 60, None))
        self.assertEqual(st["bitrate_kbps"], 15_000)
        self.assertEqual(protocol_problems("status", st), [])

    def test_status_and_storage_on_a_120_hz_screen(self):
        from momento import storage

        with self.assertLogs("momento.daemon", "INFO") as cm:
            self.use(120.0)
        self.assertIn("the recorded screen runs at 120 Hz", [r.getMessage() for r in cm.records])
        st = self.call({"cmd": "status"})
        self.assertEqual((st["fps"], st["fps_effective"], st["refresh_hz"]), ("auto", 120, 120.0))
        self.assertEqual(st["bitrate_kbps"], 22_000)
        self.assertEqual(st["storage"]["label"], "1080p High 120 fps")
        cfg120 = {**self.d.cfg, "capture": {**self.d.cfg["capture"], "fps": 120}}
        self.assertEqual(st["storage"]["required"], storage.required_bytes(cfg120))
        self.assertEqual(protocol_problems("status", st), [])
        with mock.patch.object(settings_module(), "list_audio_devices",
                               return_value={"outputs": [], "inputs": []}):
            r = self.call({"cmd": "settings"})
        self.assertEqual((r["values"]["fps"], r["fps_effective"], r["refresh_hz"]), ("auto", 120, 120.0))
        self.assertEqual(r["storage"]["current"], "1080p/high/auto")
        self.assertEqual(r["storage"]["required"]["1080p/high/auto"], r["storage"]["required"]["1080p/high/120"])
        # a reload hands the known refresh to the new recorder: it plans at 120 from the start
        r = self.call({"cmd": "configure", "changes": {"quality": "standard"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(RefreshRecorder.hints[-1], 120.0)
        self.assertEqual(r["storage"]["label"], "1080p Standard 120 fps")

    def test_explicit_60_on_a_120_hz_screen(self):
        self.use(144.0)
        r = self.call({"cmd": "configure", "changes": {"fps": 60}})
        self.assertTrue(r["ok"], r)
        st = self.call({"cmd": "status"})
        self.assertEqual((st["fps"], st["fps_effective"], st["refresh_hz"]), (60, 60, 144.0))
        self.assertEqual(st["bitrate_kbps"], 15_000)

    def test_a_60_hz_screen(self):
        self.use(60.0)
        st = self.call({"cmd": "status"})
        self.assertEqual((st["fps"], st["fps_effective"], st["refresh_hz"]), ("auto", 60, 60.0))

    def test_another_window_forgets_the_refresh(self):
        self.use(120.0)
        self.d._forget_target_name()
        self.assertIsNone(self.d.refresh_hz)
        self.assertEqual(self.call({"cmd": "status"})["fps_effective"], 60)


def settings_module():
    from momento import settings

    return settings


def protocol_problems(cmd, reply):
    from momento import protocol

    return protocol.validate_reply(cmd, reply)


class CaptureLogTest(unittest.TestCase):
    """Every capture start logs one line with everything that defines the recording."""

    def setUp(self):
        import copy

        from momento import codecs, config, pipeline
        from momento.ringbuffer import RingBuffer

        self.pipeline, self.codecs = pipeline, codecs
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["capture"].update(source="portal", target="screen", resolution="720p", quality="ultra", fps=120,
                                   format="av1")
        self.cfg["buffer"]["dir"] = str(Path(tmp.name) / "buffer")
        self.ring = RingBuffer(3600)
        failed = set(codecs.DETECTOR.failed)
        self.addCleanup(lambda: (codecs.DETECTOR.failed.clear(), codecs.DETECTOR.failed.update(failed)))
        self.enterContext(mock.patch.object(codecs, "START_GUARD"))              # no start marker on disk
        self.enterContext(mock.patch.object(pipeline.GLib, "idle_add", side_effect=lambda fn, *a: fn(*a)))

    def recorder(self, variants, known=(1920, 1080), fail=(), **capture):
        """A Recorder about to build its pipeline for a portal stream of size ``known``;
        building fails for the encoders in ``fail``. No GStreamer pipeline runs."""
        self.cfg["capture"].update(capture)
        rec = self.pipeline.Recorder(self.cfg, self.ring, lambda s, m: None)
        rec.source_name = "portal"
        rec.window_mode = rec.target == "window"
        rec._stop_requested = False
        rec._known_size = known
        rec._formats = list(dict.fromkeys(self.codecs.format_of(e) for e, _z in variants))
        rec._variants = [self.pipeline._Variant(e, z) for e, z in variants]
        rec._variant_idx = 0
        Gst = self.pipeline.Gst

        def build(v):
            rec._prepare_size()
            rec._stage = None
            rec._video_chain(v)                     # sets the "size" caps, as _build does
            if v.encoder in fail:
                raise RuntimeError(f"{v.encoder} failed")
            enc = mock.Mock()
            enc.get_factory.return_value.get_name.return_value = v.encoder
            fake = mock.Mock()
            fake.get_by_name.side_effect = lambda name: enc if name == "enc" else None
            fake.set_state.return_value = Gst.StateChangeReturn.ASYNC
            return fake
        rec._build = build
        self.addCleanup(setattr, rec, "_pipeline", None)
        return rec

    def lines(self, fn):
        with self.assertLogs("momento.pipeline", "INFO") as cm:
            fn()
        return [r.getMessage() for r in cm.records if r.getMessage().startswith("recording: ")]

    def test_describe_capture(self):
        self.assertEqual(self.pipeline.describe_capture((1280, 720), 120, "ultra", 22000, "av1", "vaav1enc", True,
                                                        False, "portal"),
                         "1280x720 @ 120 fps, ultra, 22000 kbps, AV1 (vaav1enc, zero-copy), full screen, portal")
        self.assertEqual(self.pipeline.describe_capture(None, 60, "high", 9000, "h264", "x264enc", False, True, "test",
                                                        kbps_by_hand=True, fallback=("h265", "h264"), stage="vbr"),
                         "size from the first frame @ 60 fps, high, 9000 kbps (set by hand), "
                         "H.264 (x264enc, not zero-copy; H.265 didn't start), window, test, debug stage vbr")

    def test_line_at_every_start_has_every_field(self):
        rec = self.recorder([("vaav1enc", True)])
        self.assertEqual(self.lines(rec._build_and_play),
                         ["recording: 1280x720 @ 120 fps, ultra, 22000 kbps, AV1 (vaav1enc, zero-copy), "
                          "full screen, portal"])
        rec._pipeline = None
        self.assertEqual(len(self.lines(rec._build_and_play)), 1)             # a restart logs it again

    def test_window_mode_never_names_the_window(self):
        rec = self.recorder([("x264enc", False)], known=(1600, 900), target="window", resolution="1080p",
                            quality="high", fps=60, format="h264")
        [line] = self.lines(rec._build_and_play)
        self.assertEqual(line, "recording: 1600x900 @ 60 fps, high, 15000 kbps, H.264 (x264enc, not zero-copy), "
                               "window, portal")

    def test_format_fallback_logs_the_format_really_recorded(self):
        rec = self.recorder([("vaav1enc", True), ("vah264enc", True)], fail=("vaav1enc",))
        self.assertEqual(self.lines(rec._build_and_play),
                         ["recording: 1280x720 @ 120 fps, ultra, 22000 kbps, H.264 (vah264enc, zero-copy; "
                          "AV1 didn't start), full screen, portal"])
        self.assertEqual(rec.format_fallback, ("av1", "h264"))

    def test_first_frame_replan_logs_the_final_values(self):
        Gst = self.pipeline.Gst
        rec = self.recorder([("vah264enc", True)], known=None, resolution="native", quality="high", fps=60,
                            format="h264")
        self.assertEqual(self.lines(rec._build_and_play),
                         ["recording: size from the first frame @ 60 fps, high, 15000 kbps, "
                          "H.264 (vah264enc, zero-copy), full screen, portal"])
        info = mock.Mock()
        info.get_event.return_value = Gst.Event.new_caps(Gst.Caps.from_string("video/x-raw,width=1280,height=720"))
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        capsfilter.set_property("caps", Gst.Caps.from_string(rec._output_caps()))
        with mock.patch.object(rec, "_encoder_settings"):
            lines = self.lines(lambda: rec._pin_size(None, info, (capsfilter, object(), "vah264enc")))
        self.assertEqual(lines, ["recording: 1280x720 @ 60 fps, high, 10000 kbps, H.264 (vah264enc, zero-copy), "
                                 "full screen, portal (set by the first frame)"])
        rec._source_seen = False                                    # the same size again: nothing new to say
        with mock.patch.object(rec, "_encoder_settings"):
            self.assertEqual(self.lines(lambda: rec._pin_size(None, info, (capsfilter, object(), "vah264enc"))), [])


    def first_caps(self, rec, caps, encoder="vaav1enc"):
        """Feed the source's first caps to Recorder._pin_size (a real "size" and "rate"
        capsfilter as built, no pipeline). Returns (the "recording:" lines logged, the
        bitrate the encoder got then or None, the rate capsfilter)."""
        Gst = self.pipeline.Gst
        info = mock.Mock()
        info.get_event.return_value = Gst.Event.new_caps(Gst.Caps.from_string(caps))
        size = Gst.ElementFactory.make("capsfilter", None)
        size.set_property("caps", Gst.Caps.from_string(rec._output_caps()))
        rate = Gst.ElementFactory.make("capsfilter", None)
        rate.set_property("caps", Gst.Caps.from_string(rec._rate_caps()))
        with mock.patch.object(rec, "_encoder_settings") as settings:
            lines = self.lines(lambda: rec._pin_size(None, info, (size, object(), encoder, rate, None)))
        return lines, (settings.call_args[0][2] if settings.called else None), rate

    KWIN_120 = "video/x-raw,width=1920,height=1080,framerate=0/1,max-framerate=120/1"

    def test_auto_logs_the_screen_refresh(self):
        """fps auto: the line says why ("auto: 120 Hz screen"); the first caps settle it."""
        rec = self.recorder([("vaav1enc", True)], fps="auto")
        self.assertEqual(self.lines(rec._build_and_play),
                         ["recording: 1280x720 @ 60 fps (auto: screen refresh not known yet), ultra, 15000 kbps, "
                          "AV1 (vaav1enc, zero-copy), full screen, portal"])
        lines, kbps, rate = self.first_caps(rec, self.KWIN_120)
        self.assertEqual(lines, ["recording: 1280x720 @ 120 fps (auto: 120 Hz screen), ultra, 22000 kbps, "
                                 "AV1 (vaav1enc, zero-copy), full screen, portal (set by the first frame)"])
        self.assertEqual((rec.fps, rec.refresh_hz, kbps), (120, 120.0, 22000))   # GOP + bitrate follow
        self.assertEqual(rate.get_property("caps").get_structure(0).get_fraction("framerate")[1:], (120, 1))

    def test_auto_plans_with_the_known_refresh(self):
        """A refresh known before the start (an earlier session, the daemon): built at its
        rate, and the same first caps change nothing."""
        rec = self.recorder([("vaav1enc", True)], fps="auto")
        rec.refresh_hz = 120.0
        self.assertEqual(self.lines(rec._build_and_play),
                         ["recording: 1280x720 @ 120 fps (auto: 120 Hz screen), ultra, 22000 kbps, "
                          "AV1 (vaav1enc, zero-copy), full screen, portal"])
        self.assertIn("framerate=120/1", rec._video_chain(self.pipeline._Variant("vaav1enc", True)))
        lines, kbps, rate = self.first_caps(rec, self.KWIN_120)
        self.assertEqual((lines, kbps), ([], None))
        # the screen was switched to 60 Hz meanwhile: back to 60 at the first frame
        rec._pipeline = None
        self.lines(rec._build_and_play)
        lines, kbps, _rate = self.first_caps(rec, "video/x-raw,width=1920,height=1080,framerate=0/1,"
                                                  "max-framerate=60/1")
        self.assertEqual(lines, ["recording: 1280x720 @ 60 fps (auto: 60 Hz screen), ultra, 15000 kbps, "
                                 "AV1 (vaav1enc, zero-copy), full screen, portal (set by the first frame)"])

    def test_a_60_hz_screen_says_so(self):
        rec = self.recorder([("vaav1enc", True)], fps="auto")
        self.lines(rec._build_and_play)
        lines, kbps, _rate = self.first_caps(rec, "video/x-raw,width=1920,height=1080,framerate=60/1")  # wlroots
        self.assertEqual(lines, ["recording: 1280x720 @ 60 fps (auto: 60 Hz screen), ultra, 15000 kbps, "
                                 "AV1 (vaav1enc, zero-copy), full screen, portal (set by the first frame)"])
        self.assertIsNone(kbps)                                     # nothing to change on the encoder

    def test_explicit_rate_ignores_the_screen(self):
        rec = self.recorder([("vaav1enc", True)])                 # fps = 120 (setUp)
        self.lines(rec._build_and_play)
        lines, kbps, _rate = self.first_caps(rec, "video/x-raw,width=1920,height=1080,framerate=0/1,"
                                                  "max-framerate=60/1")
        self.assertEqual((lines, kbps, rec.fps, rec.refresh_hz), ([], None, 120, 60.0))
        self.assertIsNone(rec.fps_why())

    def test_first_caps_are_logged(self):
        rec = self.recorder([("vaav1enc", True)], fps="auto")
        self.lines(rec._build_and_play)
        Gst = self.pipeline.Gst
        info = mock.Mock()
        info.get_event.return_value = Gst.Event.new_caps(Gst.Caps.from_string(self.KWIN_120))
        with self.assertLogs("momento.pipeline", "INFO") as cm, mock.patch.object(rec, "_encoder_settings"):
            rec._pin_size(None, info, (None, None, "vaav1enc"))
        [line] = [r.getMessage() for r in cm.records if r.getMessage().startswith("source's first caps")]
        self.assertIn("max-framerate=(fraction)120/1", line)       # what the compositor announced

    def test_caps_refresh(self):
        Gst = self.pipeline.Gst
        rec = self.recorder([("vaav1enc", True)])

        def hz(caps, source="portal"):
            rec.source_name = source
            return rec._caps_refresh(Gst.Caps.from_string(caps).get_structure(0))
        self.assertEqual(hz(self.KWIN_120), 120.0)                                  # KWin / Mutter
        self.assertEqual(hz("video/x-raw,framerate=0/1,max-framerate=144/1"), 144.0)
        self.assertEqual(hz("video/x-raw,framerate=60/1"), 60.0)                    # wlroots
        self.assertAlmostEqual(hz("video/x-raw,framerate=120000/1001"), 119.88, places=2)
        self.assertIsNone(hz("video/x-raw,framerate=0/1"))                          # variable, no max
        self.assertIsNone(hz("video/x-raw,width=1920,height=1080"))
        self.assertEqual(hz("video/x-raw,framerate=0/1,max-framerate=120/1", "gamescope"), 120.0)
        self.assertIsNone(hz("video/x-raw,framerate=30/1", "x11"))                   # the grabber's own rate


class RateCapsTest(unittest.TestCase):
    """Changing the frame rate after videorate never reaches the source (no renegotiation)."""

    def test_reconfigure_is_dropped_at_videorate(self):
        import copy

        from momento import config, pipeline
        from momento.ringbuffer import RingBuffer

        Gst = pipeline.Gst
        cfg = copy.deepcopy(config.DEFAULTS)
        rec = pipeline.Recorder(cfg, RingBuffer(3600), lambda s, m: None)
        for guarded in (True, False):
            with self.subTest(guarded=guarded):
                p = Gst.parse_launch("videotestsrc name=src num-buffers=1 ! video/x-raw,width=64,height=64 ! "
                                     "videorate name=vrate ! capsfilter name=rate caps=video/x-raw,framerate=60/1 ! "
                                     "fakesink")
                seen = []

                def probe(_pad, info):
                    if info.get_event().type == Gst.EventType.RECONFIGURE:
                        seen.append(1)
                    return Gst.PadProbeReturn.OK
                p.get_by_name("src").get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_UPSTREAM, probe)
                self.addCleanup(p.set_state, Gst.State.NULL)
                p.set_state(Gst.State.PAUSED)
                p.get_state(5 * Gst.SECOND)
                rec.fps = 120
                guard = p.get_by_name("vrate").get_static_pad("sink") if guarded else None
                self.assertTrue(rec._apply_rate_caps(p.get_by_name("rate"), guard))
                self.assertFalse(rec._apply_rate_caps(p.get_by_name("rate"), guard))   # already there
                self.assertEqual(len(seen), 0 if guarded else 1)
                p.set_state(Gst.State.NULL)


if __name__ == "__main__":
    unittest.main()
