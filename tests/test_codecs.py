"""Video formats (H.264 / H.265 / AV1): python3 -m unittest tests.test_codecs

The auto rule, the fallback order, detection and its cache (with fake encoders),
the settings/config/CLI side, the recorder's plan, the per-codec segment
container and the exporter's command lines. A few real 320x240 encodes (skipped
without the encoders) check that each format's buffer saves to a proper MP4.
"""

from __future__ import annotations

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento import codecs, config, settings  # noqa: E402
from momento.ringbuffer import RingBuffer, Segment, Selection  # noqa: E402

AMD_VCN4 = codecs.Detection(vendor="amd", present=("vah264enc", "vah265enc", "vaav1enc"),
                            works={"vah264enc": True, "vah265enc": True, "vaav1enc": True})
AMD_VCN3 = codecs.Detection(vendor="amd", present=("vah264enc", "vah265enc", "vaav1enc"),
                            works={"vah264enc": True, "vah265enc": True, "vaav1enc": False})
INTEL_ARC = codecs.Detection(vendor="intel", present=("vah264enc", "vah265enc", "vaav1lpenc"),
                             works={"vah264enc": True, "vah265enc": True, "vaav1lpenc": True})
NVIDIA_RTX40 = codecs.Detection(vendor="nvidia", present=("nvh264enc", "nvh265enc", "nvav1enc"),
                                works={"nvh264enc": True, "nvh265enc": True, "nvav1enc": True})
# Stock Fedora Mesa on RDNA3: H.264/H.265 VA encode switched off, AV1 works.
FEDORA_RDNA3 = codecs.Detection(vendor="amd", present=("vah264enc", "vah265enc", "vaav1enc"),
                                works={"vah264enc": False, "vah265enc": False, "vaav1enc": True})
# Stock Fedora on an older GPU: no hardware encoder works at all.
NOTHING = codecs.Detection(vendor="amd", present=("vah264enc",), works={"vah264enc": False})


def _have(*tools) -> bool:
    return all(shutil.which(t) for t in tools)


def _gst_has(element: str) -> bool:
    return _have("gst-inspect-1.0") and subprocess.run(
        ["gst-inspect-1.0", element], capture_output=True).returncode == 0


class AutoRuleTest(unittest.TestCase):
    def test_amd_vcn4_records_h264_for_now(self):
        # AMD -> AV1 is off since the 2026-09-27 VCN hang: H.264 works in hardware here.
        self.assertEqual(AMD_VCN4.auto(), "h264")
        self.assertEqual(codecs.plan("auto", AMD_VCN4), ["h264"])
        self.assertEqual(codecs.allowed(AMD_VCN4), ["auto", "h264", "h265", "av1"])  # still manual choices

    def test_everything_else_records_h264(self):
        for det in (AMD_VCN3, INTEL_ARC, NVIDIA_RTX40, codecs.UNKNOWN):
            self.assertEqual(det.auto(), "h264", det)

    def test_the_amd_av1_rule_is_there_but_off(self):
        self.assertEqual(codecs.AUTO_RULES, ())
        source = Path(codecs.__file__).read_text()
        self.assertIn('# ("amd", "vaav1enc", "av1"),', source)   # ready to turn back on

    def test_a_rule_still_works_when_turned_on(self):
        with mock.patch.object(codecs, "AUTO_RULES", (("amd", "vaav1enc", "av1"),)):
            self.assertEqual(AMD_VCN4.auto(), "av1")
            self.assertEqual(AMD_VCN3.auto(), "h264")

    def test_amd_av1_needs_the_va_encoder_to_work(self):
        # an AV1 encoder that exists but fails its test encode (VCN 3) is no reason
        self.assertEqual(AMD_VCN3.auto(), "h264")
        other = codecs.Detection(vendor="amd", present=("vah264enc", "nvav1enc"),
                                 works={"vah264enc": True, "nvav1enc": True})
        self.assertEqual(other.auto(), "h264")   # the rule names vaav1enc on the AMD driver

    def test_no_hardware_h264_picks_another_hardware_format(self):
        self.assertEqual(FEDORA_RDNA3.auto(), "av1")
        only_hevc = codecs.Detection(vendor="intel", present=("vah264enc", "vah265enc"),
                                     works={"vah264enc": False, "vah265enc": True})
        self.assertEqual(only_hevc.auto(), "h265")

    def test_software_is_never_a_reason(self):
        self.assertEqual(NOTHING.auto(), "h264")   # records in H.264 in software, as before
        self.assertEqual(codecs.allowed(NOTHING), ["auto", "h264"])

    def test_rule_table_is_small_and_valid(self):
        for vendor, encoder, fmt in codecs.AUTO_RULES:
            self.assertIn(vendor, codecs.PCI_VENDORS.values())
            self.assertEqual(codecs.format_of(encoder), fmt)
            self.assertIn(encoder, codecs.HW_ENCODERS[fmt])


class PlanTest(unittest.TestCase):
    """The format to record in, then the fallbacks: AV1 -> H.265 -> H.264."""

    def test_fallback_order(self):
        self.assertEqual(codecs.FALLBACK, ("av1", "h265", "h264"))
        self.assertEqual(codecs.plan("av1", AMD_VCN4), ["av1", "h265", "h264"])
        self.assertEqual(codecs.plan("auto", AMD_VCN4), ["h264"])
        self.assertEqual(codecs.plan("h265", AMD_VCN4), ["h265", "h264"])   # never up the list
        self.assertEqual(codecs.plan("h264", AMD_VCN4), ["h264"])
        self.assertEqual(codecs.plan("auto", AMD_VCN3), ["h264"])

    def test_formats_that_cant_record_are_skipped(self):
        self.assertEqual(codecs.plan("av1", AMD_VCN3), ["h265", "h264"])
        self.assertEqual(codecs.plan("av1", NOTHING), ["h264"])
        self.assertEqual(codecs.effective("av1", AMD_VCN3), "h265")
        self.assertEqual(codecs.plan("av1", FEDORA_RDNA3), ["av1", "h264"])

    def test_failed_formats_are_skipped(self):
        self.assertEqual(codecs.plan("av1", AMD_VCN4, failed={"av1"}), ["h265", "h264"])
        self.assertEqual(codecs.plan("av1", AMD_VCN4, failed={"av1", "h265"}), ["h264"])
        self.assertEqual(codecs.plan("auto", FEDORA_RDNA3, failed={"av1"}), ["h264"])
        self.assertEqual(codecs.plan("h264", AMD_VCN4, failed={"h264"}), ["h264"])   # always last

    def test_unknown_detection_tries_everything(self):
        self.assertEqual(codecs.plan("av1", None), ["av1", "h265", "h264"])
        self.assertEqual(codecs.plan("auto", None), ["h264"])
        self.assertEqual(codecs.plan("bogus", AMD_VCN4), ["h264"])   # read as auto

    def test_allowed(self):
        self.assertEqual(codecs.allowed(AMD_VCN4), ["auto", "h264", "h265", "av1"])
        self.assertEqual(codecs.allowed(AMD_VCN3), ["auto", "h264", "h265"])
        self.assertEqual(codecs.allowed(None), list(codecs.CHOICES))

    def test_encoders_per_format(self):
        self.assertEqual(AMD_VCN4.encoders("av1"), ["vaav1enc"])
        self.assertEqual(INTEL_ARC.encoders("av1"), ["vaav1lpenc"])
        self.assertEqual(AMD_VCN3.encoders("av1"), [])
        self.assertEqual(codecs.UNKNOWN.encoders("h265"), list(codecs.HW_ENCODERS["h265"]))

    def test_words(self):
        self.assertEqual(codecs.unavailable_message(["av1"]), "Your graphics chip can't record AV1")
        self.assertEqual(codecs.unavailable_message(["av1", "h265"]),
                         "Your graphics chip can't record H.265 or AV1")
        self.assertEqual(codecs.unavailable_message([]), "")
        self.assertEqual(codecs.HINTS["h264"], "Plays everywhere")
        self.assertEqual([codecs.label(f) for f in codecs.CHOICES], ["Auto", "H.264", "H.265", "AV1"])

    def test_format_of_and_container(self):
        from momento import ringbuffer

        self.assertEqual(codecs.format_of("vaav1enc"), "av1")
        self.assertEqual(codecs.format_of("nvh265enc"), "h265")
        self.assertEqual(codecs.format_of("x264enc"), "h264")
        self.assertEqual(codecs.format_of("something"), "h264")
        self.assertEqual(codecs.container("h264"), ("mpegtsmux", ".ts"))
        self.assertEqual(codecs.container("h265"), ("mpegtsmux", ".ts"))
        self.assertEqual(codecs.container("av1"), ("matroskamux", ".mkv"))
        self.assertEqual({s for _m, s in codecs.CONTAINERS.values()}, set(ringbuffer.SEGMENT_SUFFIXES))


class DetectorTest(unittest.TestCase):
    """Detection runs once, off the caller's thread, and is cached per key (fake encoders)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "formats.json"
        self.key = {"probe": codecs.PROBE_VERSION, "encoders": ["vaav1enc", "vah264enc"], "driver": "Mesa 26.2.1"}
        self.probes = []

    def detector(self, works=None, key=None):
        present = ("vah264enc", "vaav1enc")
        gpu = [{"node": "renderD128", "vendor": "0x1002", "device": "0x15bf", "driver": "amdgpu"}]

        def probe(names):
            self.probes.append(tuple(names))
            return dict(works if works is not None else {"vah264enc": True, "vaav1enc": True})
        return codecs.Detector(probe=probe, key=lambda: (present, gpu, key or self.key), path=self.path)

    def run_detection(self, det):
        done = threading.Event()
        got = []
        det.ensure(lambda d: (got.append(d), done.set()))
        self.assertTrue(done.wait(5))
        return got[0]

    def test_first_run_probes_and_caches(self):
        det = self.run_detection(self.detector())
        self.assertEqual(self.probes, [("vah264enc", "vaav1enc")])
        self.assertEqual((det.vendor, det.auto()), ("amd", "h264"))
        data = json.loads(self.path.read_text())
        self.assertEqual((data["key"], data["works"]), (self.key, {"vah264enc": True, "vaav1enc": True}))

    def test_same_key_reads_the_cache(self):
        self.run_detection(self.detector())
        self.probes.clear()
        det = self.run_detection(self.detector(works={}))
        self.assertEqual(self.probes, [])                 # no test encode
        self.assertEqual(det.works, {"vah264enc": True, "vaav1enc": True})

    def test_new_key_probes_again(self):
        self.run_detection(self.detector())
        self.probes.clear()
        det = self.run_detection(self.detector(works={"vah264enc": True, "vaav1enc": False},
                                               key={**self.key, "driver": "Mesa 26.3.0"}))
        self.assertEqual(len(self.probes), 1)
        self.assertFalse(det.hardware("av1"))

    def test_broken_cache_probes_again(self):
        self.path.write_text("{not json")
        self.run_detection(self.detector())
        self.assertEqual(len(self.probes), 1)

    def test_once_per_process_and_wait(self):
        det = self.detector()
        self.assertIsNone(det.ready())
        first = det.wait(timeout=5)
        self.assertIs(det.wait(timeout=5), first)
        got = []
        det.ensure(got.append)                            # ready: called at once
        self.assertEqual(got, [first])
        self.assertEqual(len(self.probes), 1)

    def test_a_failing_key_leaves_formats_unknown(self):
        def boom():
            raise RuntimeError("no GStreamer")
        with self.assertLogs("momento.codecs", "ERROR"):
            det = self.run_detection(codecs.Detector(probe=lambda n: {}, key=boom, path=self.path))
        self.assertFalse(det.known)
        self.assertEqual(det.auto(), "h264")
        self.assertEqual(codecs.allowed(det), list(codecs.CHOICES))

    def test_sandbox_never_probes(self):
        det = codecs.Detector()                          # no probe/key given, under the test sandbox
        self.assertEqual(det.ready().auto(), "h264")
        self.assertEqual(det.ready().works, {})

    def test_cached_reads_without_the_key(self):
        self.run_detection(self.detector())
        with mock.patch.object(codecs, "cache_path", return_value=self.path):
            self.assertTrue(codecs.cached().hardware("av1"))
        self.assertIsNone(codecs.load_cache(self.path / "missing"))

    def test_gpus_and_vendor(self):
        root = Path(self._tmp.name) / "drm"
        for node, vendor, device in (("renderD128", "0x1002", "0x15bf"), ("renderD129", "0x10de", "0x2684")):
            dev = root / node / "device"
            dev.mkdir(parents=True)
            (dev / "vendor").write_text(vendor + "\n")
            (dev / "device").write_text(device + "\n")
        (root / "card1").mkdir()
        gpus = codecs.gpus(root)
        self.assertEqual([(g["node"], g["vendor"], g["device"]) for g in gpus],
                         [("renderD128", "0x1002", "0x15bf"), ("renderD129", "0x10de", "0x2684")])
        self.assertEqual(codecs.vendor_of(gpus), "amd")
        self.assertIsNone(codecs.vendor_of([]))

    def test_probe_child_output_and_a_crash(self):
        """probe_encoders reads the child's lines; an encoder that kills the child counts as failed
        and the ones after it are tested in a new child."""
        calls = []

        def run(cmd, **kw):
            names = [a for a in cmd[5:] if not a.startswith("--")]
            calls.append(names)
            lines = []
            for n in names:
                if n == "vah265enc":
                    break                                # the child crashed here
                lines.append(json.dumps({"encoder": n, "ok": n != "vah264enc"}))
            return subprocess.CompletedProcess(cmd, 0 if len(lines) == len(names) else -11,
                                               "\n".join(lines) + "\n", "")
        with mock.patch.object(codecs.subprocess, "run", side_effect=run), \
                self.assertLogs("momento.codecs", "WARNING"):
            got = codecs.probe_encoders(["vah264enc", "vah265enc", "vaav1enc"])
        self.assertEqual(got, {"vah264enc": False, "vah265enc": False, "vaav1enc": True})
        self.assertEqual(calls, [["vah264enc", "vah265enc", "vaav1enc"], ["vaav1enc"]])

    def test_probe_pipeline_is_tiny(self):
        desc = codecs.probe_pipeline("vaav1enc")
        self.assertIn("num-buffers=10", desc)
        self.assertIn("width=320,height=240", desc)
        self.assertIn("vaav1enc name=enc ! fakesink", desc)


class FormatSettingTest(unittest.TestCase):
    """[capture] format and the "format" setting."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.toml"

    def test_default_and_values(self):
        self.assertEqual(config.DEFAULTS["capture"]["format"], "auto")
        self.assertEqual(settings.current(config.load(self.path))["format"], "auto")
        for given, want in (("AV1", "av1"), ("hevc", "h265"), ("H.264", "h264"), (" auto ", "auto"),
                            ("h.265", "h265"), ("avc", "h264")):
            self.assertEqual(settings.normalize("format", given), want)
        for bad in ("vp9", "", True, "h266"):
            with self.assertRaises(ValueError):
                settings.normalize("format", bad)
        with self.assertRaisesRegex(ValueError, r"^format: choose one of: auto, h264, h265, av1"):
            settings.validate({"format": "vp9"})

    def test_apply_writes_capture_format(self):
        changed = settings.apply({"format": "av1"}, self.path)
        self.assertEqual(changed, {"format": "av1"})
        self.assertIn('format = "av1"', self.path.read_text())
        self.assertEqual(config.load(self.path)["capture"]["format"], "av1")
        self.assertEqual(settings.apply({"format": "AV1"}, self.path), {})
        self.assertNotIn("format", settings.LIVE_KEYS)        # a change restarts recording

    def test_hand_edited_nonsense_reads_as_auto(self):
        self.path.write_text('[capture]\nformat = "vp9"\n')
        with self.assertLogs("momento.codecs", "WARNING"):
            self.assertEqual(settings.current(config.load(self.path))["format"], "auto")

    def test_video_tab_and_help(self):
        self.assertIn(("Video", ("resolution", "fps", "quality", "format")), settings.TABS)
        self.assertIn("format", settings.KEYS)

    def test_describe(self):
        cfg = config.load(self.path)
        d = settings.describe(cfg, devices={"outputs": [], "inputs": []}, formats=AMD_VCN3)
        self.assertEqual(d["choices"]["format"], ["auto", "h264", "h265", "av1"])
        self.assertEqual(d["values"]["format"], "auto")
        self.assertEqual(d["format_allowed"], ["auto", "h264", "h265"])
        self.assertEqual((d["format_auto"], d["format_effective"]), ("h264", "h264"))
        cfg["capture"]["format"] = "av1"
        d = settings.describe(cfg, devices={"outputs": [], "inputs": []}, formats=AMD_VCN3)
        self.assertEqual(d["format_effective"], "h265")      # can't record AV1: the fallback
        d = settings.describe(cfg, devices={"outputs": [], "inputs": []}, formats=AMD_VCN4, failed={"av1"})
        self.assertEqual((d["format_auto"], d["format_effective"]), ("h264", "h265"))

    def test_describe_without_a_detection(self):
        with mock.patch.object(codecs, "cached", return_value=None):
            d = settings.describe(config.load(self.path), devices={"outputs": [], "inputs": []})
        self.assertEqual(d["format_allowed"], list(codecs.CHOICES))
        self.assertIsNone(d["format_auto"])
        self.assertIsNone(d["format_effective"])


class CLIFormatTest(unittest.TestCase):
    def test_lines(self):
        from momento import cli

        self.assertEqual(cli.format_line("auto", "av1"), "auto (AV1)")
        self.assertEqual(cli.format_line("auto", None), "auto")
        self.assertEqual(cli.format_line("h265", "h265"), "H.265")
        self.assertEqual(cli.format_line("av1", "h264"), "AV1, recording in H.264 instead")
        self.assertEqual(cli.format_note("av1", "h264"),
                         "Your graphics chip can't record AV1, so Momento records in H.264.")
        self.assertIsNone(cli.format_note("av1", "av1"))
        self.assertIsNone(cli.format_note("auto", "h264"))

    def test_set_format_without_the_daemon(self):
        from momento import cli, ipc

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            out = []
            with mock.patch.object(ipc, "request", side_effect=ipc.DaemonNotRunning("off")), \
                    mock.patch.object(codecs, "cached", return_value=AMD_VCN3), \
                    mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                self.assertEqual(cli.main(["--config", str(path), "set", "format", "av1"]), 0)
            self.assertIn('format = "av1"', path.read_text())
            self.assertTrue(out[0].startswith("format = av1"), out)
            self.assertIn("Your graphics chip can't record AV1, so Momento records in H.265.", out)


class RingBufferFormatsTest(unittest.TestCase):
    """.ts and .mkv segments share one numbering; recovery and clearing know both."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_numbering_recovery_and_strays(self):
        from momento import ringbuffer

        ring = RingBuffer(3600, directory=self.dir)
        ring.recover()
        for i, (suffix, codec) in enumerate(((".ts", "h264"), (".mkv", "av1"))):
            p = self.dir / f"seg{i:08d}{suffix}"
            p.write_bytes(b"x" * 10)
            ring.opened(p, 100.0 + 10 * i, session=f"s{i}", width=1920, height=1080, fps=60, codec=codec, audio=True)
            ring.closed(p, 110.0 + 10 * i)
        (self.dir / "seg00000007.mkv").write_bytes(b"crash")        # unfinished: not in the index
        again = RingBuffer(3600, directory=self.dir)
        self.assertEqual(again.recover(), 2)                         # after seg00000001.mkv
        self.assertFalse((self.dir / "seg00000007.mkv").exists())
        self.assertEqual([s.path.name for s in again._segments], ["seg00000000.ts", "seg00000001.mkv"])
        self.assertEqual(ringbuffer.segment_number("/b/seg00000123.mkv"), 123)
        self.assertEqual(ringbuffer.segment_number("/b/seg00000123.ts"), 123)
        self.assertIsNone(ringbuffer.segment_number("/b/index.jsonl"))
        # A save never joins the two formats: the newest run, with the reason.
        sel = again.select_last(20)
        self.assertEqual([s.path.name for s in sel.segments], ["seg00000001.mkv"])
        self.assertEqual(sel.note, "earlier footage used a different video format")
        again.release(sel)
        again.clear()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [])

    def test_daemon_cleanup_removes_mkv(self):
        from momento import daemon

        for name in ("seg00000001.ts", "seg00000002.mkv", "x.tmp"):
            (self.dir / name).write_bytes(b"x")
        daemon._clean_dir(self.dir)
        self.assertFalse(self.dir.exists())


class RecorderFormatTest(unittest.TestCase):
    """The recorder's plan: which encoders, in which order, and the fallback on a failed start."""

    def setUp(self):
        from momento import pipeline

        self.p = pipeline
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["capture"].update(source="test", target="screen")
        self.cfg["buffer"]["dir"] = str(Path(self._tmp.name) / "buffer")
        self.states = []
        self.detector = codecs.Detector(key=lambda: None)
        for patch in (mock.patch.object(codecs, "DETECTOR", self.detector),
                      mock.patch.object(pipeline, "_have", lambda name: name != "vah264lpenc")):
            patch.start()
            self.addCleanup(patch.stop)

    def recorder(self, **capture):
        self.cfg["capture"].update(capture)
        rec = self.p.Recorder(self.cfg, RingBuffer(3600), lambda s, m: self.states.append((s, m)))
        rec._build_and_play = mock.Mock()
        self.addCleanup(rec._cancel_retry)
        return rec

    def encoders(self, rec):
        return [(v.encoder, v.zero_copy) for v in rec._variants]

    def test_auto_on_amd_vcn4_is_h264(self):
        self.detector._result = AMD_VCN4
        rec = self.recorder()
        rec.start()
        self.assertEqual(rec._formats, ["h264"])
        self.assertEqual(self.encoders(rec)[0], ("vah264enc", True))

    def test_av1_on_amd_vcn4(self):
        self.detector._result = AMD_VCN4
        rec = self.recorder(format="av1")
        rec.start()
        self.assertEqual(rec._formats, ["av1", "h265", "h264"])
        names = [e for e, _z in self.encoders(rec)]
        self.assertEqual(names[:4], ["vaav1enc", "vaav1enc", "vah265enc", "vah265enc"])
        self.assertEqual(names[4:], [n for n in self.p.ENCODER_ORDER if n != "vah264lpenc"
                                     for _ in ((0, 1) if n in self.p.VA_ENCODERS else (0,))])
        self.assertEqual(self.encoders(rec)[0], ("vaav1enc", True))    # zero-copy first
        self.assertEqual(self.states[-1], ("starting", None))
        rec._build_and_play.assert_called_once()

    def test_explicit_h264_needs_no_detection(self):
        rec = self.recorder(format="h264")                 # detection never ran (ready() is None)
        rec.start()
        self.assertEqual(rec._formats, ["h264"])
        self.assertEqual(self.encoders(rec)[0], ("vah264enc", True))

    def test_named_encoder_wins(self):
        self.detector._result = AMD_VCN4
        rec = self.recorder(encoder="x264enc")
        rec.start()
        self.assertEqual(self.encoders(rec), [("x264enc", False)])

    def test_start_waits_for_the_detection(self):
        started = threading.Event()
        callbacks = []
        self.detector.ensure = lambda cb=None: (callbacks.append(cb), started.set())
        rec = self.recorder(format="av1")                  # needs to know (so does auto)
        with mock.patch.object(self.p.GLib, "idle_add", side_effect=lambda fn, *a: fn(*a)):
            rec.start()
            self.assertTrue(started.is_set())
            self.assertEqual(self.states, [("starting", None)])
            rec._build_and_play.assert_not_called()
            rec.start()                                    # a second start while waiting: nothing new
            self.assertEqual(len(callbacks), 1)
            self.detector._result = AMD_VCN4
            callbacks[0](AMD_VCN4)
        self.assertEqual(rec._formats[0], "av1")
        rec._build_and_play.assert_called_once()

    def test_stop_while_waiting_drops_the_start(self):
        callbacks = []
        self.detector.ensure = lambda cb=None: callbacks.append(cb)
        rec = self.recorder()
        with mock.patch.object(self.p.GLib, "idle_add", side_effect=lambda fn, *a: fn(*a)):
            rec.start()
            rec.stop()
            self.detector._result = AMD_VCN4
            callbacks[0](AMD_VCN4)
            rec._build_and_play.assert_not_called()
            rec.start()                                    # started again: goes ahead now
        rec._build_and_play.assert_called_once()

    def test_failed_format_falls_back_once(self):
        self.detector._result = AMD_VCN4
        rec = self.recorder(format="av1")
        rec.start()
        with mock.patch.object(self.p.GLib, "idle_add") as idle, \
                self.assertLogs("momento.pipeline", "WARNING") as logs:
            rec._next_variant_or_fail("encoder failed")            # AV1 zero-copy -> AV1 via videoconvert
            self.assertIsNone(rec.format_fallback)
            rec._next_variant_or_fail("encoder failed")            # AV1 -> H.265
        self.assertEqual(idle.call_count, 2)
        self.assertEqual(rec._variants[rec._variant_idx].encoder, "vah265enc")
        self.assertEqual(rec.format_fallback, ("av1", "h265"))
        self.assertEqual(self.detector.failed, {"av1"})
        self.assertIn("AV1 didn't start", "\n".join(logs.output))
        # The next start skips AV1 straight away (no second fallback to report).
        rec2 = self.recorder(format="av1")
        rec2.start()
        self.assertEqual(rec2._formats, ["h265", "h264"])
        self.assertIsNone(rec2.format_fallback)

    def test_a_source_failure_never_changes_the_format(self):
        self.detector._result = AMD_VCN4
        rec = self.recorder(format="av1")
        rec.start()
        rec._variant_idx = 1                                       # the last AV1 variant
        with mock.patch.object(rec, "_error_and_retry") as retry:
            rec._next_variant_or_fail("stream ended", source_lost=True)
        retry.assert_called_once()
        self.assertEqual(rec._variant_idx, 1)
        self.assertEqual(self.detector.failed, set())

    def test_format_effective_follows_the_encoder(self):
        rec = self.recorder()
        self.assertIsNone(rec.format_effective)
        for enc, fmt in (("vaav1enc", "av1"), ("nvh265enc", "h265"), ("x264enc", "h264")):
            self.assertEqual(codecs.format_of(enc), fmt)


@unittest.skipUnless(_have("gst-launch-1.0", "gst-inspect-1.0"), "needs GStreamer tools")
class PipelineContainerTest(unittest.TestCase):
    """The pipeline built for each format: its encoder, parser, muxer and segment names (not played)."""

    def test_each_format(self):
        from momento import pipeline as p

        cases = {"h264": ("vah264enc", "h264parse", "mpegtsmux", ".ts"),
                 "h265": ("vah265enc", "h265parse", "mpegtsmux", ".ts"),
                 "av1": ("vaav1enc", "av1parse", "matroskamux", ".mkv")}
        for fmt, (encoder, parser, muxer, suffix) in cases.items():
            if not all(p._have(n) for n in (encoder, parser, muxer)):
                continue
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                cfg = copy.deepcopy(config.DEFAULTS)
                cfg["capture"].update(source="test", target="screen", format=fmt)
                cfg["buffer"]["dir"] = tmp
                rec = p.Recorder(cfg, RingBuffer(3600), lambda s, m: None)
                rec.source_name = "test"
                rec.test_size = (320, 240)
                rec._known_size = (320, 240)
                pl = rec._build(p._Variant(encoder, False))
                try:
                    names = {el.get_factory().get_name() for el in pl.children}
                    self.assertTrue({encoder, parser, "splitmuxsink"} <= names, names)
                    mux = pl.get_by_name("mux")
                    self.assertEqual(mux.get_property("muxer").get_factory().get_name(), muxer)
                    self.assertEqual(mux.get_property("location"), str(Path(tmp) / f"seg%08d{suffix}"))
                    if muxer == "matroskamux":   # byte-joinable segments (see codecs.MUXER_OPTIONS)
                        self.assertTrue(mux.get_property("muxer").get_property("streamable"))
                finally:
                    pl.set_state(p.Gst.State.NULL)


class ExporterCommandTest(unittest.TestCase):
    """The ffmpeg command lines per codec (ffmpeg itself is not run)."""

    def run_export(self, codec, sessions=1):
        from momento import exporter

        cmds = []

        def fake_run(cmd, out):
            cmds.append(cmd)
            Path(out).write_bytes(b"mp4")
        tmp = Path(tempfile.mkdtemp(prefix="momento-cmd-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        suffix = codecs.container(codec)[1]
        segs = []
        for i in range(4):
            p = tmp / f"seg{i:08d}{suffix}"
            p.write_bytes(b"x")
            segs.append(Segment(p, 100.0 + 10 * i, 110.0 + 10 * i, session=f"s{i * sessions // 4}",
                                width=1920, height=1080, fps=60, codec=codec, audio=True))
        sel = Selection(segs, offset=0.0, duration=40.0, start=100.0, end=140.0)
        with mock.patch.object(exporter, "_run", side_effect=fake_run), \
                mock.patch.object(exporter, "_keyframe_times", return_value=[0.0, 1.0]):
            exporter.export(sel, tmp / "out" / "clip.mp4")
        return cmds

    def test_h264(self):
        (cmd,) = self.run_export("h264")
        self.assertIn("-c", cmd)
        self.assertNotIn("-tag:v", cmd)
        self.assertTrue(any(a.startswith("concatf:") for a in cmd))
        self.assertEqual(cmd[-3:-1], ["-f", "mp4"])

    def test_h265_gets_the_hvc1_tag(self):
        (cmd,) = self.run_export("h265")
        i = cmd.index("-tag:v")
        self.assertEqual(cmd[i + 1], "hvc1")
        self.assertLess(i, cmd.index("-movflags"))

    def test_av1_from_matroska(self):
        (cmd,) = self.run_export("av1")
        self.assertNotIn("-tag:v", cmd)
        listed = Path(cmd[cmd.index("-i") + 1].removeprefix("concatf:")).name
        self.assertTrue(listed.endswith(".segments.txt"))
        self.assertEqual(cmd[-3:-1], ["-f", "mp4"])

    def test_join_keeps_the_codec_options(self):
        cmds = self.run_export("h265", sessions=2)
        self.assertEqual(len(cmds), 3)                             # two pieces, one join
        self.assertTrue(all("hvc1" in c for c in cmds))
        self.assertIn("concat", cmds[-1])


# --- a few real encodes (320x240, a few seconds each) --------------------------------------

SEG_SECONDS = 2
SEG_COUNT = 3


def make_format_segments(d: Path, fmt: str) -> list[Path]:
    """~SEG_COUNT keyframe-aligned 2 s segments in ``fmt`` + AAC, muxed as the recorder does."""
    from momento import pipeline as p

    encoder = {"h264": "vah264enc", "h265": "vah265enc", "av1": "vaav1enc"}[fmt]
    muxer, suffix = codecs.container(fmt)
    parse = p.PARSERS[fmt].replace("name=parse ", "")
    settings_ = "key-int-max=30" + (" hierarchical-level=1" if fmt == "av1" else " b-frames=0")
    frames = SEG_COUNT * SEG_SECONDS * 30
    audio_bufs = int(SEG_COUNT * SEG_SECONDS * 44100 / 1024)
    cmd = (f"gst-launch-1.0 -q -e videotestsrc num-buffers={frames} pattern=ball "
           f"! video/x-raw,width=320,height=240,framerate=30/1 ! videoconvert ! video/x-raw,format=NV12 "
           f"! {encoder} {settings_} "
           f"! {parse} ! queue ! mux.video "
           f"audiotestsrc num-buffers={audio_bufs} wave=ticks ! audioconvert ! audioresample "
           f"! avenc_aac ! aacparse ! queue ! mux.audio_0 "
           f"splitmuxsink name=mux muxer=\"{codecs.muxer_description(muxer)}\" send-keyframe-requests=true "
           f"max-size-time={SEG_SECONDS * 10**9} location={d}/seg%08d{suffix}")
    subprocess.run(cmd, shell=True, capture_output=True, timeout=60, check=True)
    return sorted(d.glob(f"seg*{suffix}"))


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration,format_name:stream=codec_type,codec_name,codec_tag_string,"
                          "start_time,width,height,pix_fmt", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(_have("gst-launch-1.0", "gst-inspect-1.0", "ffmpeg", "ffprobe"), "needs GStreamer + ffmpeg")
class FormatExportTest(unittest.TestCase):
    """Each format's buffer saves to a proper MP4: the codec, AAC audio, the right duration."""

    EXPECT = {"h264": ("h264", "avc1"), "h265": ("hevc", "hvc1"), "av1": ("av1", "av01")}

    def export_format(self, fmt, offset, duration):
        from momento import exporter

        encoder = {"h264": "vah264enc", "h265": "vah265enc", "av1": "vaav1enc"}[fmt]
        if not (_gst_has(encoder) and _gst_has("avenc_aac")):
            self.skipTest(f"needs {encoder} and avenc_aac")
        tmp = Path(tempfile.mkdtemp(prefix=f"momento-{fmt}-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "buffer").mkdir()
        try:
            paths = make_format_segments(tmp / "buffer", fmt)
        except (subprocess.SubprocessError, OSError) as e:
            self.skipTest(f"cannot record {fmt} here: {e}")
        self.assertGreaterEqual(len(paths), SEG_COUNT)
        segs = [Segment(p, 1000.0 + i * SEG_SECONDS, 1000.0 + (i + 1) * SEG_SECONDS, session="s",
                        width=320, height=240, fps=30, codec=fmt, audio=True)
                for i, p in enumerate(paths[:SEG_COUNT])]
        sel = Selection(segs, offset=offset, duration=duration, start=1000.0 + offset,
                        end=1000.0 + offset + duration)
        out = exporter.export(sel, tmp / "out" / f"{fmt}.mp4")
        info = ffprobe(out)
        streams = {s["codec_type"]: s for s in info["streams"]}
        self.assertEqual(sorted(streams), ["audio", "video"])
        self.assertEqual((streams["video"]["codec_name"], streams["video"]["codec_tag_string"]), self.EXPECT[fmt])
        self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (320, 240))
        self.assertEqual(streams["video"]["pix_fmt"], "yuv420p")          # 8-bit, as the recorder writes
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        self.assertIn("mp4", info["format"]["format_name"])
        self.assertLess(abs(float(info["format"]["duration"]) - duration), 1.0, info["format"])
        frames = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                                 "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(out)],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertLess(abs(int(frames) - duration * 30), 31, f"{frames} frames for {duration} s")  # every segment
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"],
                             capture_output=True, text=True)
        self.assertEqual((dec.returncode, dec.stderr.strip()), (0, ""))
        return info

    def test_h264(self):
        self.export_format("h264", 0.0, 6.0)

    def test_h265(self):
        self.export_format("h265", 0.0, 6.0)

    def test_av1(self):
        self.export_format("av1", 0.0, 6.0)

    def test_av1_mid_segment(self):
        self.export_format("av1", 1.0, 4.0)


def _gst_ok() -> bool:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: F401
        return _have("ffmpeg", "ffprobe")
    except (ImportError, ValueError):
        return False


@unittest.skipUnless(_gst_ok(), "needs GStreamer (PyGObject) and ffmpeg")
class RecorderFormatLiveTest(unittest.TestCase):
    """The real recorder with the 320x240 test source, a few seconds per format, then a save."""

    RUN_SECONDS = 5

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="momento-live-fmt-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.ring = RingBuffer(3600)
        det = codecs.Detector(key=lambda: None)
        det._result = codecs.Detection(known=False)       # try what GStreamer has, as a fresh machine would
        patch = mock.patch.object(codecs, "DETECTOR", det)
        patch.start()
        self.addCleanup(patch.stop)

    def record(self, fmt):
        from gi.repository import GLib

        from momento import pipeline

        encoder = {"h264": "vah264enc", "h265": "vah265enc", "av1": "vaav1enc"}[fmt]
        if not (pipeline._have(encoder) and pipeline._have("avenc_aac")):
            self.skipTest(f"needs {encoder} and avenc_aac")
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["capture"].update(source="test", target="screen", resolution="native", format=fmt)
        cfg["buffer"].update(segment_seconds=2, dir=str(self.tmp / "buffer"))
        states = []
        loop = GLib.MainLoop()
        rec = pipeline.Recorder(cfg, self.ring, lambda s, m: states.append((s, m)))
        rec.test_size = (320, 240)
        GLib.timeout_add(self.RUN_SECONDS * 1000, lambda: (rec.flush(loop.quit), False)[1])
        GLib.timeout_add((self.RUN_SECONDS + 10) * 1000, lambda: (loop.quit(), False)[1])
        rec.start()
        loop.run()
        rec.stop()
        self.assertNotIn("error", [s for s, _m in states], states)
        self.assertEqual((rec.encoder_name, rec.format_effective), (encoder, fmt))
        return rec

    def save(self, seconds):
        from momento import exporter

        sel = self.ring.select_last(seconds)
        try:
            out = exporter.export(sel, self.tmp / "out" / f"clip{time.monotonic_ns()}.mp4")
        finally:
            self.ring.release(sel)
        return sel, out

    def test_each_format_records_and_saves(self):
        for fmt, (name, tag) in FormatExportTest.EXPECT.items():
            with self.subTest(format=fmt):
                self.record(fmt)
                suffix = codecs.container(fmt)[1]
                segs = [s for s in self.ring._segments if s.codec == fmt]
                self.assertGreaterEqual(len(segs), 2)
                self.assertTrue(all(s.path.suffix == suffix and s.path.exists() for s in segs), segs)
                sel, out = self.save(4)
                self.assertEqual({s.codec for s in sel.segments}, {fmt})   # never joined with another format
                info = ffprobe(out)
                streams = {s["codec_type"]: s for s in info["streams"]}
                self.assertEqual((streams["video"]["codec_name"], streams["video"]["codec_tag_string"]), (name, tag))
                self.assertEqual(streams["audio"]["codec_name"], "aac")
                self.assertEqual(streams["video"]["pix_fmt"], "yuv420p")   # 8-bit NV12 in, whatever the format
                self.assertLess(abs(float(info["format"]["duration"]) - 4.0), 1.0, info["format"])
        # A format change mid-buffer: a longer save keeps only the newest format's run.
        sel, _out = self.save(60)
        self.assertEqual(sel.note, "earlier footage used a different video format")
        self.assertEqual({s.codec for s in sel.segments}, {"av1"})
        # Recovery after a restart knows both kinds of segment files.
        again = RingBuffer(3600, directory=self.tmp / "buffer")
        n = again.recover()
        self.assertEqual({s.path.suffix for s in again._segments}, {".ts", ".mkv"})
        self.assertEqual(n, max(int(s.path.stem[3:]) for s in again._segments) + 1)


if __name__ == "__main__":
    unittest.main()
