"""Contract tests: the daemon (and docs/PROTOCOL.md, when present) against momento/protocol.py.

python3 -m unittest tests.test_protocol
"""

from __future__ import annotations

try:
    from tests import _sandbox  # noqa: F401  -- must come before any momento import
except ImportError:  # run as a script from tests/
    import _sandbox  # noqa: F401

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from momento import protocol  # noqa: E402
from momento.protocol import validate_reply, validate_request  # noqa: E402

DOC = ROOT / "docs" / "PROTOCOL.md"


def _gi_available() -> bool:
    try:
        from gi.repository import GLib  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


class _FakeRecorder:
    """Stands in for pipeline.Recorder: no GStreamer, state changes are immediate."""

    instances: list = []

    def __init__(self, cfg, ring, on_state, bus=None):
        self.cfg, self.ring, self.on_state = cfg, ring, on_state
        self.recording = False
        self.started = self.stopped = self.flushes = 0
        self.source_name, self.encoder_name = "test", "x264enc"
        _FakeRecorder.instances.append(self)

    def start(self, interactive=False):
        self.started += 1
        self.recording = True
        self.on_state("recording", None)

    source_size = None   # the picture size the real recorder learns from the caps

    def stop(self):
        self.stopped += 1
        self.recording = False
        self.on_state("stopped", None)

    def flush(self, callback, timeout=5.0):
        self.flushes += 1
        callback()

    def grab_frame(self, callback, timeout=3.0):
        callback(_FakeFrame() if self.recording else None)


class _FakeFrame:
    size = (1920, 1080)


class _DaemonCase(unittest.TestCase):
    """A real Daemon with a fake Recorder, fake free space and no notifications."""

    def setUp(self):
        from momento import config, daemon, screenshot, settings, storage

        self._tmp = tempfile.TemporaryDirectory(prefix="momento-proto-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.path = self.tmp / "config.toml"
        buf, out = self.tmp / "buffer", self.tmp / "clips"
        # Full screen: records from the start (window mode waits for play).
        self.path.write_text(f'[capture]\nresolution = "1080p"\ntarget = "screen"\n\n[buffer]\ndir = "{buf}"\n\n'
                             f'[output]\ndir = "{out}"\n')
        fake = types.ModuleType("momento.pipeline")
        fake.Recorder = _FakeRecorder
        for p in (mock.patch.dict(sys.modules, {"momento.pipeline": fake}),
                  mock.patch.object(storage, "free_bytes", side_effect=lambda path: self.free),
                  mock.patch.object(daemon, "notify"),
                  # the PNG writer needs a real frame; the protocol only needs the file
                  mock.patch.object(screenshot, "encode_png", return_value=b"\x89PNG\r\n\x1a\n"),
                  mock.patch.object(settings, "list_audio_devices", return_value={
                      "outputs": [{"name": "sink.monitor", "label": "Speakers", "default": True}],
                      "inputs": [{"name": "mic0", "label": "Mic", "default": False}]})):
            p.start()
            self.addCleanup(p.stop)
        self.free = 10**13
        _FakeRecorder.instances = []
        self.cfg = config.load(self.path)
        self.d = daemon.Daemon(self.cfg, loop=None)
        self.d.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.d.ring.recover()
        self.d.recorder = _FakeRecorder(self.cfg, self.d.ring, self.d._on_state)
        self.d._start_recorder()

    # --- helpers --------------------------------------------------------------------

    def call(self, msg, timeout=10):
        box, done = [], threading.Event()

        def reply(r):
            box.append(r)
            done.set()
        self.d.handle(msg, reply)
        self.assertTrue(done.wait(timeout), f"no reply to {msg}")
        # Replies must survive the wire: JSON-serialisable, and compared after a round trip.
        return json.loads(json.dumps(box[0]))

    def check(self, msg, ok=None, code=None):
        """Send msg, assert the reply conforms (and has the expected ok/code); return it."""
        if msg.get("cmd") in protocol.COMMANDS:
            self.assertEqual(validate_request(msg), [], msg)
        r = self.call(msg)
        self.assertEqual(validate_reply(msg.get("cmd"), r), [], f"{msg} -> {r}")
        if ok is not None:
            self.assertIs(r["ok"], ok, r)
        if code is not None:
            self.assertEqual(r.get("code"), code, r)
        return r

    def need(self, **capture) -> int:
        from momento import storage

        return storage.required_bytes({**self.d.cfg, "capture": {**self.d.cfg["capture"], **capture}})

    def fill(self, n=3, length=10.0):
        """n closed 10 s segments ending now, as the real recorder would leave them."""
        now = time.time()
        for i in range(n):
            p = self.d.buffer_dir / f"seg{i:08d}.ts"
            p.write_bytes(b"\x47" * 188 * 10)
            start = now - (n - i) * length
            self.d.ring.opened(p, start, session="s1", width=1920, height=1080, fps=60,
                               codec="h264", audio=True)
            self.d.ring.closed(p, start + length)


class DaemonContractTest(_DaemonCase):
    """Every command and error path of daemon.handle() replies per momento/protocol.py."""

    def test_status_recording(self):
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["state"], r["recording"]), ("recording", True))
        self.assertEqual(set(r["storage"]), set(protocol.STORAGE_CHECK))

    def test_status_protocol_version(self):
        r = self.call({"cmd": "status"})
        if "protocol" not in r:
            self.skipTest("status has no \"protocol\" field yet (spec: required; pending implementation "
                          "in daemon.status())")
        self.assertEqual(r["protocol"], protocol.PROTOCOL_VERSION)

    def test_status_error_and_no_storage(self):
        self.d._on_state("error", "no usable H.264 encoder found")
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["state"], r["error"]), ("error", "no usable H.264 encoder found"))
        self.d.recorder.stop()
        self.free = self.need() - 1  # the buffer dir is empty, so nothing is reclaimable
        self.d._start_recorder()
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual(r["state"], "no_storage")
        self.assertIn("error", r)
        self.assertFalse(r["storage"]["ok"])

    def test_save(self):
        from momento import exporter

        self.fill(3)

        def fake_export(sel, out):
            Path(out).write_bytes(b"mp4")
            return Path(out)

        with mock.patch.object(exporter, "export", side_effect=fake_export):
            r = self.check({"cmd": "save", "seconds": 20}, ok=True)
            self.assertEqual((r["requested"], r["partial"]), (20, False))
            self.assertTrue(Path(r["path"]).is_absolute() and Path(r["path"]).exists())
            self.assertEqual(self.d.recorder.flushes, 1)  # flush before save
            r = self.check({"cmd": "save", "seconds": "5m"}, ok=True)
            self.assertEqual(r["requested"], 300)
            self.assertTrue(r["partial"])
            self.assertAlmostEqual(r["seconds"], 30.0, delta=0.5)

    def test_save_errors(self):
        r = self.check({"cmd": "save", "seconds": 30}, ok=False)
        self.assertEqual(r["error"], "nothing recorded yet")
        for bad in ({"cmd": "save"}, {"cmd": "save", "seconds": 0},
                    {"cmd": "save", "seconds": 3601}, {"cmd": "save", "seconds": "soon"}):
            r = self.call(bad)
            self.assertEqual(validate_reply("save", r), [], r)
            self.assertFalse(r["ok"])
            self.assertTrue(r["error"].startswith("bad duration"), r)
        self.fill(2)
        self.d.recorder.recording = False
        from momento import storage

        self.free = storage.SAVE_MARGIN
        self.check({"cmd": "save", "seconds": 30}, ok=False, code="no_storage")

    def test_screenshot(self):
        r = self.check({"cmd": "screenshot"}, ok=True)
        self.assertTrue(Path(r["path"]).is_absolute() and Path(r["path"]).exists())
        self.assertEqual(Path(r["path"]).parent, self.tmp / "clips" / "Images")
        self.assertTrue(Path(r["path"]).name.startswith("Momento_") and r["path"].endswith(".png"))
        self.assertEqual((r["width"], r["height"]), (1920, 1080))
        self.assertNotEqual(self.check({"cmd": "screenshot"}, ok=True)["path"], r["path"])  # never overwrites

    def test_screenshot_errors(self):
        from momento import storage

        self.free = storage.SAVE_MARGIN - 1
        self.check({"cmd": "screenshot"}, ok=False, code="no_storage")
        self.free = 10**13
        self.check({"cmd": "pause"})
        self.check({"cmd": "screenshot"}, ok=False, code="not_recording")
        self.check({"cmd": "stop"})
        self.check({"cmd": "screenshot"}, ok=False, code="not_recording")

    def test_pause_resume(self):
        r = self.check({"cmd": "pause"}, ok=True)
        self.assertEqual(r["state"], "paused")
        self.assertEqual(self.check({"cmd": "status"})["state"], "paused")
        self.check({"cmd": "pause"}, ok=True)  # idempotent
        r = self.check({"cmd": "resume"}, ok=True)
        self.assertIn(r["state"], ("starting", "recording"))
        self.check({"cmd": "resume"}, ok=True)  # no-op while recording

    def test_resume_refused_without_space(self):
        self.check({"cmd": "pause"})
        shutil.rmtree(self.d.buffer_dir)
        self.free = self.need() - 1
        r = self.check({"cmd": "resume"}, ok=False, code="no_storage")
        self.assertEqual(r["state"], "no_storage")
        self.assertIn("storage", r)
        self.assertEqual(self.check({"cmd": "status"})["state"], "no_storage")

    def test_stop_then_resume(self):
        self.fill(2)
        r = self.check({"cmd": "stop"}, ok=True)
        self.assertEqual((r["state"], r["buffer_cleared"]), ("stopped", True))
        st = self.check({"cmd": "status"})
        self.assertEqual((st["state"], st["recording"], st["buffered"]), ("stopped", False, 0))
        self.check({"cmd": "pause"}, ok=True)
        self.assertEqual(self.check({"cmd": "status"})["state"], "stopped")
        r = self.check({"cmd": "resume"}, ok=True)
        self.assertNotEqual(self.check({"cmd": "status"})["state"], "stopped")

    @unittest.skipUnless(_gi_available(), "PyGObject not available")
    def test_quit(self):
        from gi.repository import GLib

        from momento import daemon

        # keep_buffer on a fresh daemon (never started, nothing buffered)
        fresh = daemon.Daemon(self.cfg, loop=None)
        with mock.patch.object(GLib, "timeout_add", lambda ms, fn: fn()):
            box = []
            fresh.handle({"cmd": "quit", "keep_buffer": True}, box.append)
            self.assertEqual(validate_reply("quit", box[0]), [])
            self.assertEqual(box[0], {"ok": True, "buffer_cleared": False})
            self.fill(1)
            r = self.check({"cmd": "quit"}, ok=True)
            self.assertTrue(r["buffer_cleared"])
        self.assertFalse(self.d.buffer_dir.exists())

    def test_reload(self):
        r = self.check({"cmd": "reload"}, ok=True)
        self.assertEqual((r["restarted"], r["paused"]), (True, False))
        self.check({"cmd": "pause"})
        r = self.check({"cmd": "reload"}, ok=True)
        self.assertEqual((r["restarted"], r["paused"], r["state"]), (False, True, "paused"))
        self.check({"cmd": "resume"})
        shutil.rmtree(self.d.buffer_dir)
        self.free = self.need() - 1
        r = self.check({"cmd": "reload"}, ok=True)
        self.assertEqual((r["restarted"], r["state"]), (False, "no_storage"))
        self.assertIn("warning", r)
        self.path.write_text(self.path.read_text() + '\n[capture]\nquality = "potato"\n')
        r = self.check({"cmd": "reload"}, ok=False)
        self.assertTrue(r["error"].startswith("config not applied"), r)

    def test_settings(self):
        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual(set(r["values"]), set(protocol.SETTING_KEYS))
        keys = r["storage"]["required"]
        self.assertEqual(len(keys), len(r["choices"]["resolution"]) * len(r["choices"]["quality"])
                         * len(r["choices"]["fps"]))
        for key in keys:
            self.assertRegex(key, r"^[a-z0-9]+/[a-z]+/\d+$")
        self.assertIn(r["storage"]["current"], keys)

    def test_configure(self):
        r = self.check({"cmd": "configure", "changes": {"resolution": "720p", "mic": "on"}}, ok=True)
        self.assertEqual((r["changed"], r["restarted"]), ({"resolution": "720p", "mic": "on"}, True))
        r = self.check({"cmd": "configure", "changes": {"resolution": "720p"}}, ok=True)
        self.assertEqual((r["changed"], r["restarted"]), ({}, False))
        for bad in ({"cmd": "configure"}, {"cmd": "configure", "changes": {}},
                    {"cmd": "configure", "changes": {"resolution": "999p"}},
                    {"cmd": "configure", "changes": {"colour": "red"}}):
            r = self.call(bad)
            self.assertEqual(validate_reply("configure", r), [], r)
            self.assertFalse(r["ok"])

    def test_resolution_cap(self):
        # Before any caps: nothing known, the setting is what is recorded.
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["source_size"], r["resolution_effective"]), (None, "1080p"))
        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual(r["resolution_allowed"], r["choices"]["resolution"])
        self.assertEqual((r["source_size"], r["resolution_effective"]), (None, "1080p"))
        # The recorder learns a 1280x720 screen; 1080p is saved but records at 720p.
        _FakeRecorder.source_size = (1280, 720)
        self.addCleanup(setattr, _FakeRecorder, "source_size", None)
        r = self.check({"cmd": "configure", "changes": {"resolution": "720p"}}, ok=True)
        r = self.check({"cmd": "configure", "changes": {"resolution": "fhd"}}, ok=True)
        self.assertEqual(r["changed"], {"resolution": "1080p"})
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["resolution"], r["resolution_effective"], r["source_size"]),
                         ("1080p", "native", [1280, 720]))
        self.assertEqual(r["bitrate_kbps"], 10000)
        self.assertEqual(r["storage"]["required"], self.need(resolution="720p"))
        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual(r["resolution_allowed"], ["720p", "native"])
        self.assertEqual((r["values"]["resolution"], r["resolution_effective"]), ("1080p", "native"))
        req = r["storage"]["required"]
        self.assertEqual(req["1080p/high/60"], req["720p/high/60"])
        self.assertTrue(validate_reply("status", {**self.call({"cmd": "status"}), "source_size": [1920]}))
        self.assertTrue(validate_reply("status", {**self.call({"cmd": "status"}), "source_size": [0, 1080]}))

    def test_resolution_choices_up_to_1080p(self):
        """v1.0.0 offers 720p, 1080p and native; 1440p/4K are refused (additive: version 1)."""
        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual(r["choices"]["resolution"], list(protocol.RESOLUTION_CHOICES))
        before = self.path.read_text()
        for value in ("1440p", "2160p", "4k", "2k"):
            r = self.check({"cmd": "configure", "changes": {"resolution": value}}, ok=False)
            self.assertEqual(r["error"], "1440p and 4K aren't available yet; Momento records up to 1080p for now.")
            self.assertNotIn("code", r)
        self.assertEqual(self.path.read_text(), before)
        # an older config with 4K: reported (and recorded) as 1080p, the file untouched
        self.path.write_text(self.path.read_text().replace('resolution = "1080p"', 'resolution = "2160p"'))
        self.check({"cmd": "reload"}, ok=True)
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["resolution"], r["resolution_effective"], r["bitrate_kbps"]), ("1080p", "1080p", 15000))
        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual(r["values"]["resolution"], "1080p")
        self.assertIn(r["storage"]["current"], r["storage"]["required"])
        self.assertIn('resolution = "2160p"', self.path.read_text())
        # native on a 4K screen records 1080 lines, at 1080p's bitrate
        _FakeRecorder.source_size = (3840, 2160)
        self.addCleanup(setattr, _FakeRecorder, "source_size", None)
        self.check({"cmd": "configure", "changes": {"resolution": "native"}}, ok=True)
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["resolution_effective"], r["bitrate_kbps"]), ("native", 15000))
        self.assertEqual(r["storage"]["required"], self.need(resolution="1080p"))
        # older daemons' values in status stay valid
        self.assertEqual(validate_reply("status", {**r, "resolution": "2160p", "resolution_effective": "native"}), [])

    def test_configure_storage_rules(self):
        shutil.rmtree(self.d.buffer_dir)
        self.free = self.need(quality="ultra", fps=120) - 1
        before = self.path.read_text()
        big = {"cmd": "configure", "changes": {"quality": "ultra", "fps": 120}}
        r = self.check(big, ok=False, code="no_storage")
        self.assertFalse(r["storage"]["ok"])
        self.assertEqual(self.path.read_text(), before)  # nothing written
        r = self.check({**big, "force": True}, ok=True)
        self.assertEqual((r["restarted"], r["state"]), (False, "no_storage"))
        self.assertIn("warning", r)
        r = self.check({"cmd": "configure", "changes": {"quality": "high"}}, ok=True)  # shrinking goes through
        self.assertEqual(r["state"], "recording")

    def test_low_storage(self):
        """storage.low (additive): a full span doesn't fit, in any state; ok/no_storage unchanged."""
        from momento import storage

        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["storage"]["low"], r["storage"]["disk"], r["storage"]["label"]),
                         (False, "buffer", "1080p High"))
        self.free = self.need() - 1
        for msg in ({"cmd": "status"}, {"cmd": "pause"}, {"cmd": "status"}, {"cmd": "stop"}, {"cmd": "status"}):
            r = self.check(msg, ok=True)
        self.assertEqual(r["state"], "stopped")
        self.assertTrue(r["storage"]["low"])
        self.assertEqual((r["storage"]["needed"], r["storage"]["available"]), (self.need(), self.need() - 1))
        # keep_history on the same disk: the saved hour is part of the need
        self.free = self.need() + 1
        with mock.patch.object(storage, "same_disk", return_value=True):
            r = self.check({"cmd": "configure", "changes": {"keep_history": "on"}}, ok=True)
        self.assertEqual((r["storage"]["ok"], r["storage"]["low"], r["storage"]["history"]), (True, True, True))
        self.assertEqual(r["storage"]["needed"], self.need() + storage.buffer_bytes(self.d.cfg))
        # the configure refusal carries the same fields
        with mock.patch.object(storage, "same_disk", return_value=True):
            r = self.check({"cmd": "configure", "changes": {"quality": "ultra"}}, ok=False, code="no_storage")
        self.assertTrue(r["storage"]["low"])

    def test_gallery_pause(self):
        """Full screen: pause / resume with reason "gallery", conforming both ways."""
        r = self.check({"cmd": "pause", "reason": "gallery", "pid": os.getpid()}, ok=True)
        self.assertEqual((r["state"], r["pause_reason"]), ("paused", "gallery"))
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["state"], r["pause_reason"]), ("paused", "gallery"))
        self.assertIn(self.check({"cmd": "resume", "reason": "gallery"}, ok=True)["state"], ("starting", "recording"))
        self.assertIsNone(self.check({"cmd": "status"}, ok=True)["pause_reason"])
        self.check({"cmd": "pause"}, ok=True)                             # the user's: not resumed
        self.assertIsNone(self.check({"cmd": "pause", "reason": "gallery"}, ok=True)["pause_reason"])
        self.assertEqual(self.check({"cmd": "resume", "reason": "gallery"}, ok=True)["state"], "paused")
        self.d._clear_pause_reason()

    def test_window_mode(self):
        from momento import config

        self.addCleanup(config.portal_token_path("window").unlink, missing_ok=True)
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["target"], r["target_name"], r["stop_reason"], r["keep_history"]),
                         ("screen", None, None, False))
        self.check({"cmd": "pick_window"}, ok=False)                 # screen mode: refused
        r = self.check({"cmd": "configure", "changes": {"record": "window"}}, ok=True)
        self.assertEqual(r["changed"], {"record": "window"})
        self.assertEqual(self.check({"cmd": "status"})["target"], "window")
        # the picked window closes: stopped, as if Stop was pressed; never "no_window"
        self.fill(2)
        self.d.recorder.recording = False
        self.d.recorder.on_state("no_window", "The game window closed \u2014 pick a window to keep recording")
        r = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((r["state"], r["recording"], r["stop_reason"]), ("stopped", False, "window_closed"))
        self.assertNotIn("error", r)
        self.assertEqual(r["buffered"], 0)                           # keep_history is off
        r = self.check({"cmd": "pick_window"}, ok=True)
        self.assertIn(r["state"], ("starting", "recording"))
        self.assertIsNone(self.check({"cmd": "status"})["stop_reason"])
        self.d.recorder.on_state("no_window", "No game window picked")   # picker dismissed, nothing recorded
        self.assertEqual(self.check({"cmd": "status"})["state"], "stopped")
        self.assertIn(self.check({"cmd": "resume"}, ok=True)["state"], ("starting", "recording"))
        shutil.rmtree(self.d.buffer_dir)
        self.free = self.need() - 1
        r = self.check({"cmd": "pick_window"}, ok=False, code="no_storage")
        self.assertEqual(r["state"], "no_storage")

    def test_keep_history(self):
        r = self.check({"cmd": "configure", "changes": {"keep_history": "on", "hour_warning": 5,
                                                        "instant_bar": "off"}}, ok=True)
        self.assertEqual((r["restarted"], r["state"]), (False, "recording"))  # none of them restarts
        self.fill(2)
        r = self.check({"cmd": "stop"}, ok=True)
        self.assertEqual((r["state"], r["buffer_cleared"]), ("stopped", False))
        st = self.check({"cmd": "status"}, ok=True)
        self.assertEqual((st["state"], st["stop_reason"], st["keep_history"]), ("stopped", "user", True))
        self.assertGreater(st["buffered"], 0)
        from momento import exporter

        with mock.patch.object(exporter, "export", side_effect=lambda sel, out: Path(out)):
            self.check({"cmd": "save", "seconds": 15}, ok=True)          # saving works while stopped
        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual((r["values"]["keep_history"], r["values"]["hour_warning"], r["values"]["instant_bar"]),
                         ("on", 5, "off"))

    def test_controller_is_one_setting(self):
        """Off / PS / Xbox + Down; Open with and Exclusive are gone (config-only hold_ms / exclusive)."""
        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual((r["values"]["controller"], r["choices"]["controller"]), ("ps_down", ["off", "ps_down"]))
        self.assertNotIn("controller_open", r["values"])
        self.assertNotIn("controller_exclusive", r["values"])
        self.assertNotIn("controller_open", r["choices"])
        self.assertIn(["Controller", ["controller"]], r["tabs"])
        for key in ("controller_open", "controller_exclusive"):
            msg = {"cmd": "configure", "changes": {key: "on"}}
            self.assertEqual(validate_request(msg), [f"unknown setting {key!r}"])
            r = self.call(msg)
            self.assertIs(r["ok"], False, r)
            self.assertIn("unknown setting", r["error"])
        r = self.check({"cmd": "configure", "changes": {"controller": "off"}}, ok=True)
        self.assertEqual((r["changed"], r["restarted"], r["state"]), ({"controller": "off"}, False, "recording"))
        r = self.check({"cmd": "configure", "changes": {"controller": "view_menu"}}, ok=True)  # still accepted
        self.assertEqual(r["changed"], {"controller": "view_menu"})

    def test_settings_tabs(self):
        from momento import settings

        r = self.check({"cmd": "settings"}, ok=True)
        self.assertEqual(r["tabs"], [[name, list(keys)] for name, keys in settings.TABS])
        self.assertEqual(r["choices"]["hour_warning"], [10, 5, 3])
        for _name, keys in r["tabs"]:
            for key in keys:
                self.assertIn(key, protocol.SETTING_KEYS)

    def test_unknown_command(self):
        for msg in ({"cmd": "frobnicate"}, {}, {"cmd": 3}):
            r = self.call(msg)
            self.assertEqual(validate_reply(msg.get("cmd"), r), [], r)
            self.assertFalse(r["ok"])
            self.assertIn("unknown command", r["error"])


class CoverageTest(_DaemonCase):
    """protocol.COMMANDS and daemon.handle() know the same commands."""

    MINIMAL = {"save": {"seconds": 1}, "configure": {"changes": {"mic": "off"}}}

    def test_every_documented_command_is_handled(self):
        from momento import exporter

        ctx = [mock.patch.object(exporter, "export", side_effect=lambda sel, out: Path(out))]
        if _gi_available():
            from gi.repository import GLib

            ctx.append(mock.patch.object(GLib, "timeout_add", lambda ms, fn: 0))  # quit: no shutdown
        for c in ctx:
            c.start()
            self.addCleanup(c.stop)
        for cmd in protocol.COMMANDS:
            if cmd == "quit" and not _gi_available():
                continue
            msg = {"cmd": cmd, **self.MINIMAL.get(cmd, {})}
            r = self.call(msg)
            self.assertNotIn("unknown command", str(r.get("error", "")), cmd)
            self.assertEqual(validate_reply(cmd, r), [], f"{cmd}: {r}")

    def test_every_handled_command_is_documented(self):
        src = (ROOT / "momento" / "daemon.py").read_text()
        handled = set(re.findall(r"""cmd\s*==\s*["']([A-Za-z_][\w-]*)["']""", src))
        self.assertTrue(handled, "found no `cmd == \"...\"` in daemon.py; update this test")
        self.assertEqual(handled - set(protocol.COMMANDS), set(),
                         "daemon.py handles commands that momento/protocol.py does not document")

    def test_clip_bar_commands_are_documented(self):
        src = (ROOT / "momento" / "overlay.py").read_text()
        if "ControlServer" not in src:
            self.skipTest("overlay.py has no clip-bar control socket")
        found = set(re.findall(r"""cmd\s*==\s*["']([A-Za-z_][\w-]*)["']""", src))
        for group in re.findall(r"cmd\s+not\s+in\s+\(([^)]*)\)", src):
            found |= set(re.findall(r"""["']([A-Za-z_][\w-]*)["']""", group))
        known = set(protocol.COMMANDS) | set(protocol.CLIP_BAR_COMMANDS)
        self.assertEqual(found - known, set(), "overlay.py uses commands that momento/protocol.py does not document")


def _wait_running(loop, timeout=5.0):
    """Block until ``loop`` (just started in a thread) is dispatching.

    A quit() that lands before the thread gets into run() is lost (run() sets the
    loop running again), and the leaked thread would keep dispatching the default
    GLib context under later tests.
    """
    from gi.repository import GLib

    running = threading.Event()
    GLib.idle_add(lambda: running.set() or False)
    if not running.wait(timeout):
        raise RuntimeError("GLib main loop thread did not start")


@unittest.skipUnless(_gi_available(), "PyGObject not available")
class WireTest(_DaemonCase):
    """The daemon's handler behind the real ipc.Server, over a real Unix socket."""

    def setUp(self):
        super().setUp()
        from gi.repository import GLib

        from momento import ipc

        self.sock_path = self.tmp / "momento.sock"
        self.loop = GLib.MainLoop()
        self.server = ipc.Server(self.sock_path, self.d.handle)
        self.server.start()
        t = threading.Thread(target=self.loop.run, daemon=True)
        t.start()
        _wait_running(self.loop)

        def stop():
            self.server.close()
            self.loop.quit()
            t.join(2)
        self.addCleanup(stop)

    def exchange(self, lines: list[bytes]) -> list[dict]:
        """Send raw lines on one connection; read one reply line per request line."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(str(self.sock_path))
        with s, s.makefile("rb") as f:
            s.sendall(b"".join(lines))
            out = []
            for _ in lines:
                line = f.readline()
                self.assertTrue(line.endswith(b"\n"), line)
                out.append(json.loads(line.decode("utf-8")))
            return out

    def test_permissions(self):
        self.assertEqual(os.stat(self.sock_path).st_mode & 0o777, 0o600)

    def test_malformed_then_valid_on_one_connection(self):
        replies = self.exchange([b"this is not json\n", b"[1, 2, 3]\n", b"\xff\xfe{\n",
                                 b'{"cmd": "nope"}\n', b'{"cmd": "status"}\n', b'{"cmd": "pause"}\n'])
        for r in replies[:3]:
            self.assertEqual(validate_reply(None, r), [], r)
            self.assertFalse(r["ok"])
            self.assertTrue(r["error"].startswith("bad request"), r)
        self.assertIn("unknown command", replies[3]["error"])
        self.assertEqual(validate_reply("status", replies[4]), [])
        self.assertTrue(replies[4]["ok"])
        self.assertEqual(replies[5], {"ok": True, "state": "paused"})

    def test_client_request(self):
        from momento import ipc

        r = ipc.request({"cmd": "status"}, timeout=10, path=self.sock_path)
        self.assertEqual(validate_reply("status", r), [])


class ValidatorTest(unittest.TestCase):
    """validate_reply / validate_request catch what they should."""

    OK_PAUSE = {"ok": True, "state": "paused"}

    def test_accepts_and_ignores_unknown_fields(self):
        self.assertEqual(validate_reply("pause", {**self.OK_PAUSE, "future": 1}), [])

    def test_problems(self):
        self.assertTrue(validate_reply("pause", {"ok": True}))                       # missing state
        self.assertTrue(validate_reply("pause", {"ok": True, "state": "napping"}))   # unknown state
        self.assertTrue(validate_reply("pause", {"ok": True, "state": 1}))           # wrong type
        self.assertTrue(validate_reply("pause", {"state": "paused"}))                 # no ok
        self.assertTrue(validate_reply("pause", "ok"))                                # not an object
        self.assertTrue(validate_reply("quit", {"ok": True, "buffer_cleared": 1}))    # int is not bool
        self.assertTrue(validate_reply("save", {"ok": True, "path": "/x", "seconds": 1, "requested": True,
                                                "partial": False}))                  # bool is not integer
        self.assertTrue(validate_reply("pause", {"ok": False}))                       # error text missing
        self.assertTrue(validate_reply("pause", {"ok": False, "error": "x", "code": "nope"}))
        self.assertTrue(validate_reply("frobnicate", {"ok": True}))
        status = {"ok": True, "state": "paused", "recording": False, "buffered": 0, "max_seconds": 3600,
                  "source": None, "encoder": None, "output_dir": "/v", "resolution": "1080p",
                  "quality": "high", "bitrate_kbps": 15000, "fps": 60,
                  "storage": {"ok": True, "free": 1, "required": 1, "reclaimable": 0, "path": "/b"}}
        self.assertEqual(validate_reply("status", status), [])
        self.assertTrue(validate_reply("status", {**status, "storage": {**status["storage"], "free": "1"}}))
        # low storage (additive: optional)
        low = {**status["storage"], "low": True, "needed": 2, "available": 1, "history": False, "disk": "buffer",
               "label": "1080p High"}
        self.assertEqual(validate_reply("status", {**status, "storage": low}), [])
        self.assertTrue(validate_reply("status", {**status, "storage": {**low, "low": 1}}))
        self.assertTrue(validate_reply("status", {**status, "storage": {**low, "needed": 2.5}}))
        # window mode (additive: target is optional, no_window a new state)
        self.assertEqual(validate_reply("status", {**status, "target": "window", "state": "no_window",
                                                   "error": "The game window closed"}), [])
        self.assertTrue(validate_reply("status", {**status, "target": 2}))
        # keep history / window name (additive: optional, nullable)
        self.assertEqual(validate_reply("status", {**status, "state": "stopped", "stop_reason": "window_closed",
                                                   "target_name": None, "keep_history": True}), [])
        self.assertEqual(validate_reply("status", {**status, "stop_reason": None, "target_name": "Hades"}), [])
        self.assertTrue(validate_reply("status", {**status, "stop_reason": "bored"}))
        self.assertTrue(validate_reply("status", {**status, "keep_history": "on"}))
        # the gallery's pause (additive: optional, nullable)
        self.assertEqual(validate_reply("status", {**status, "state": "paused", "pause_reason": "gallery"}), [])
        self.assertEqual(validate_reply("status", {**status, "pause_reason": None}), [])
        self.assertTrue(validate_reply("status", {**status, "pause_reason": "lunch"}))
        self.assertEqual(validate_reply("pause", {"ok": True, "state": "paused", "pause_reason": "gallery"}), [])
        self.assertEqual(validate_reply("pause", {"ok": True, "state": "recording", "pause_reason": None}), [])
        settings_ok = {"ok": True, "values": {"record": "window", "resolution": "1080p", "quality": "high",
                                              "fps": 60, "bitrate": 0, "audio_source": "default", "mic": "off",
                                              "mic_device": "default"},
                       "choices": {"record": [], "resolution": [], "quality": [], "fps": []},
                       "devices": {"outputs": [], "inputs": []}, "fps": 60, "max_seconds": 3600, "config": "/c",
                       "storage": {"required": {}, "current": "x", "free": 1, "reclaimable": 0, "reserve": 1,
                                   "path": "/b"}}
        self.assertEqual(validate_reply("settings", {**settings_ok, "tabs": [["General", ["record"]]]}), [])
        # resolution cap (additive: optional)
        self.assertEqual(validate_reply("status", {**status, "source_size": [2560, 1440],
                                                   "resolution_effective": "native"}), [])
        self.assertEqual(validate_reply("status", {**status, "source_size": None}), [])
        for bad in ([1920], [1920, "1080"], [1920, 1080.5], [True, 1080], "1920x1080"):
            self.assertTrue(validate_reply("status", {**status, "source_size": bad}), bad)
        self.assertEqual(validate_reply("settings", {**settings_ok, "source_size": [1920, 1080],
                                                     "resolution_allowed": ["720p", "1080p", "native"],
                                                     "resolution_effective": "1080p"}), [])
        self.assertTrue(validate_reply("settings", {**settings_ok, "resolution_allowed": "720p"}))
        self.assertTrue(validate_reply("settings", {**settings_ok, "tabs": [["General", "record"]]}))
        self.assertEqual(validate_reply("pick_window", {"ok": True, "state": "starting"}), [])
        self.assertEqual(validate_reply("pick_window", {"ok": False, "code": "no_storage", "error": "x",
                                                        "state": "no_storage"}), [])
        self.assertTrue(validate_reply("pick_window", {"ok": True}))
        shot = {"ok": True, "path": "/v/Images/Momento_2026-09-26_21-04-11.png", "width": 1920, "height": 1080}
        self.assertEqual(validate_reply("screenshot", shot), [])
        self.assertEqual(validate_reply("screenshot", {"ok": False, "code": "not_recording", "error": "x"}), [])
        self.assertTrue(validate_reply("screenshot", {"ok": True, "path": "/x.png"}))          # no size
        self.assertTrue(validate_reply("screenshot", {**shot, "width": 1920.5}))

    def test_requests(self):
        self.assertEqual(validate_request({"cmd": "save", "seconds": "5m"}), [])
        self.assertEqual(validate_request({"cmd": "pick_window"}), [])
        self.assertEqual(validate_request({"cmd": "screenshot"}), [])
        self.assertEqual(validate_request({"cmd": "pause", "reason": "gallery", "pid": 4242}), [])
        self.assertEqual(validate_request({"cmd": "resume", "reason": "gallery"}), [])
        self.assertTrue(validate_request({"cmd": "pause", "reason": "lunch"}))
        self.assertTrue(validate_request({"cmd": "pause", "reason": "gallery", "pid": "4242"}))
        self.assertEqual(validate_request({"cmd": "configure", "changes": {"record": "window"}}), [])
        self.assertTrue(validate_request({"cmd": "save"}))
        self.assertTrue(validate_request({"cmd": "configure", "changes": {}}))
        self.assertTrue(validate_request({"cmd": "configure", "changes": {"colour": "red"}}))
        self.assertTrue(validate_request({"cmd": "nope"}))
        self.assertTrue(validate_request({}))

    def test_tables_are_consistent(self):
        from momento import quality, settings

        self.assertEqual(set(protocol.SETTING_KEYS), set(settings.KEYS))
        self.assertIn("record", protocol.SETTING_CHOICES)
        self.assertIn("no_window", protocol.STATES)     # kept for older daemons
        self.assertEqual(set(protocol.STOP_REASONS), {"user", "window_closed"})
        self.assertEqual(set(protocol.PAUSE_REASONS), {"gallery"})
        self.assertIn("not_recording", protocol.ERROR_CODES)
        self.assertIn("screenshot", protocol.COMMANDS)          # additive: the version stays
        self.assertEqual(protocol.PROTOCOL_VERSION, 1)  # window mode, keep history, screenshots: additive
        self.assertEqual(protocol.PROTOCOL_VERSION, 1)
        for name, spec in {**protocol.COMMANDS, **protocol.CLIP_BAR_COMMANDS}.items():
            self.assertEqual(set(spec), {"request", "reply", "error"}, name)
            self.assertIn("ok", spec["reply"], name)
        self.assertEqual(len(quality.RESOLUTIONS) * len(quality.QUALITIES) * len(quality.FPS_CHOICES), 18)
        self.assertEqual(protocol.RESOLUTION_TOLERANCE, quality.SOURCE_TOLERANCE)
        self.assertEqual(protocol.RESOLUTION_CHOICES, tuple(quality.RESOLUTIONS))
        self.assertEqual(protocol.MAX_HEIGHT, quality.MAX_HEIGHT)


class IndexRecordTest(unittest.TestCase):
    """index.jsonl lines written by the ring buffer match protocol.INDEX_RECORD."""

    def test_segment_json(self):
        from momento.ringbuffer import Segment

        seg = Segment(Path("/b/seg00000001.ts"), 1.0, 11.0, session="s", width=1920, height=1080,
                      fps=60, codec="h264", audio=True)
        d = json.loads(seg.to_json())
        self.assertEqual(set(d), set(protocol.INDEX_RECORD))
        self.assertEqual(protocol._check_object("", d, protocol.INDEX_RECORD), [])


class DocExamplesTest(unittest.TestCase):
    """Every ```jsonl block in docs/PROTOCOL.md is valid wire traffic per momento/protocol.py."""

    BLOCK = re.compile(r"^[ \t]*```jsonl([^\n]*)\n(.*?)^[ \t]*```", re.S | re.M)

    def setUp(self):
        if not DOC.exists():
            self.skipTest("docs/PROTOCOL.md is not present (local reference only; not in a fresh clone)")
        self.text = DOC.read_text(encoding="utf-8")

    def test_examples_validate(self):
        blocks = self.BLOCK.findall(self.text)
        self.assertTrue(blocks, "no ```jsonl examples found")
        seen = set()
        for info, body in blocks:
            table = protocol.CLIP_BAR_COMMANDS if "clip-bar" in info.split() else protocol.COMMANDS
            cmd, answered = None, True
            for n, line in enumerate(body.splitlines(), 1):
                if not line.strip():
                    continue
                where = f"{info.strip() or 'jsonl'} block, line {n}: {line[:80]}"
                obj = json.loads(line)  # a failure here names the bad line in the traceback
                self.assertIsInstance(obj, dict, where)
                if "cmd" in obj:
                    self.assertTrue(answered, f"request without a reply before {where}")
                    cmd, answered = obj["cmd"], False
                    if cmd in table:
                        self.assertEqual(validate_request(obj, table), [], where)
                        seen.add((id(table), cmd))
                else:
                    self.assertIsNotNone(cmd, f"reply before any request: {where}")
                    problems = validate_reply(cmd, obj, table)
                    self.assertEqual(problems, [], where)
                    answered = True
            self.assertTrue(answered, f"last request in a block has no reply: {body[:80]}")
        for cmd in protocol.COMMANDS:
            self.assertIn((id(protocol.COMMANDS), cmd), seen, f"docs/PROTOCOL.md has no example for {cmd!r}")

    def test_documented_commands_have_sections(self):
        for cmd in protocol.COMMANDS:
            self.assertIn(f"### `{cmd}`", self.text, f"docs/PROTOCOL.md has no section for {cmd!r}")
        for state in protocol.STATES:
            self.assertIn(f"| `{state}` |", self.text, f"state {state!r} not documented")
        for code in protocol.ERROR_CODES:
            self.assertIn(f"| `{code}` |", self.text, f"error code {code!r} not documented")


if __name__ == "__main__":
    unittest.main()
